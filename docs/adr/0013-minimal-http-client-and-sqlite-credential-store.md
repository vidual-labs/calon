# 13. Minimal HTTP client and SQLite-backed credential store per provider

- **Status:** Accepted
- **Date:** 2026-08-19
- **Supersedes:** the open note in
  [ADR 0009](0009-optional-resource-calendar-sync.md) — "a minimal
  stdlib-based HTTP client against their REST APIs — to be decided at implementation
  time and justified in the PR per `CLAUDE.md` §8" — and the credential-storage
  surface ADR 0009 scoped. ADR 0009 stays Accepted and unchanged; this ADR records
  the two implementation decisions it deliberately left open.

## Context

ADR 0009 fixed the *boundary*: an optional, per-resource read (free/busy) and write
(event upsert) against a connected Google Calendar or Microsoft 365 calendar, behind a
`CalendarProvider` interface, that degrades to Calon-only availability when the
provider is unreachable. It deliberately left two concrete decisions open:

1. **What client talks to the two provider APIs.** ADR 0009 named the alternatives —
   the full SDKs (`google-api-python-client`, `msal*`) or "a minimal stdlib-based HTTP
   client against their REST APIs" — and deferred the choice to implementation time.
2. **Where the operator's refresh token lives per resource.** ADR 0004 avoided any
   credential-storage surface; ADR 0009 reintroduces it narrowly (the operator's own
   resource calendars) but did not fix the storage mechanism.

Both providers expose a thin REST surface that Calon needs only a handful of calls on:
one free/busy call and one event create/patch per provider, plus the OAuth refresh-token
exchange. That shape is the input to decision 1. Decision 2 is constrained by
`CLAUDE.md` §8 ("never commit secrets; the operator's config is where per-source
secrets live today") and §9 (SQLite as the system of record).

The two non-obvious choices this ADR forces are the ones `CLAUDE.md` §10 (DRY) turns
on:

- **One HTTP client, one refresh discipline, shared by both providers.** The Google
  and Microsoft providers differ only in endpoint shape and the token-endpoint URL; the
  transport, the 401→refresh→retry-once loop, and the token-exchange body/parse are
  identical. Duplicating that logic in two modules (or in two SDKs) would violate the
  DRY rule and double the attack surface for a 10-line refresh loop that both providers
  already need.
- **The refresh token is a secret the operator edits and backs up.** It cannot live in
  code, in the environment (which is not backed up the same way as config), or in a new
  storage subsystem. It lives in the same place every other per-source secret already
  lives.

## Decision

### HTTP client: `httpx`, no full provider SDK

The provider talks to the Google and Microsoft REST APIs with **`httpx` 0.28.x** — the
version already present *transitively* in the dependency tree (FastAPI/Starlette pull it
in). No new third-party dependency is introduced; no provider SDK
(`google-api-python-client`, `msal`, `msal-extensions`) is added.

Rationale:

- **No new runtime dependency** (`CLAUDE.md` §8). `httpx` is already transitive, and
  `httpx.MockTransport` (part of it) is what the provider unit tests stand in for the
  network with — so the same library is exercised in prod and in the test, with no
  extra pin and no new supply-chain surface. `CLAUDE.md` §10 (DRY): adding two SDKs
  would be a far heavier, less auditable surface for two thin REST calls, and would pull
  a long transitive tree (protobuf, `google-auth`, `msal` + `cryptography`).
- **A shared module owns the plumbing.** `src/calon/calendars/oauth.py` holds the
  provider-agnostic `TokenStore`, `OAuthCredentials`, `refresh_access_token(...)` (one
  `POST` to the provider's token URL, `grant_type=refresh_token`, adopting a rotated
  refresh token), and `calendar_error(...)` (which raises `CalendarProviderError`).
  Each provider (`google.py`, `microsoft.py`) is a thin adapter over a single
  `httpx.Client`: one free/busy call and one event upsert, both routed through a
  single `_request` helper that implements the **exactly one** refresh-and-retry on a
  `401` (a second consecutive `401` raises `CalendarProviderError` so a dead grant
  surfaces as a degradation, never a loop). The token endpoint URL differs per provider
  (Google's `oauth2.googleapis.com/token` vs Microsoft's
  `login.microsoftonline.com/common/oauth2/v2.0/token`); nothing else differs.
- **Tested without the network.** Every provider unit test wires a
  `httpx.Client(transport=httpx.MockTransport(scripted))` and pins the exact request
  body and the refresh-and-retry count; no live OAuth flow and no provider credential
  ever touch CI.

### Credential store: the refresh token lives in the operator's config, held in memory

The operator's per-resource refresh token lives in the operator-editable
`config/calon.toml` under `[calendars.<resource_slug>]`, and is loaded into the
provider's in-process `TokenStore` at boot. It is **not** written to a database
table: the phase added only the `ics_uid` column (on the booking) to hold the
iCal UID that makes the write-back re-runnable (the key the provider embeds as the
event's `iCalUID`), and deliberately did not add a `calendar_credential` table. A
token in the operator's editable config is the same place every other per-source
secret already lives (`[sources.<slug>]` secrets), it is backed up and edited
alongside the rest of the configuration, and it keeps the credential-storage surface
as narrow as ADR 0009 allows (the operator's own resource calendars, opt-in, never
any requester's calendar).

- **Source of truth is the TOML.** `[calendars.<slug>] refresh_token` is the value
  the operator obtains out-of-band (no OAuth is performed inside Calon — the
  operator completes the provider's authorization flow elsewhere and pastes in the
  resulting refresh token). Boot loads it into the `CalendarProviderRegistry`, which
  builds the provider with `refresh_token=cfg.refresh_token`.
- **In-process, process-lifetime.** The `TokenStore` holds the current access token,
  its expiry, and the refresh token for the lifetime of the process. A token
  rotation (the provider returning a new refresh token on a refresh) is adopted
  **in memory** and kept there; it is not written back to the TOML. Rotated refresh
  tokens therefore apply for the life of the running process; a restart re-reads the
  original token from the TOML (see the open questions below).
- **No new storage subsystem.** There is no credentials table, no encryption-at-rest
  layer, and no separate store. The only new persistence surface in the whole feature
  is the `ics_uid` column on the booking row.

## Consequences

- **ADR 0009's dependency question is resolved with no *new* dependency.** `httpx`
  is already transitively present; the providers are thin `httpx` adapters, and the
  shared refresh-and-retry discipline lives in one module. No provider SDK is added,
  keeping the dependency surface and audit surface minimal.
- **The token is never a networked or logged value.** It is held only in the operator's
  config file and in process memory for the process lifetime; it is never placed on the
  wire except as the `refresh_token` form field of the (provider's own) token exchange,
  and no endpoint or log line exposes it.
- **Rotation is per-process.** A rotated refresh token is not persisted back to the
  TOML, so a process restart re-reads the original token. For the typical deployment
  (a long-lived process, operator-managed rotation) this is a non-issue; if an
  operator's provider rotates refresh tokens aggressively and they would prefer the
  rotation to survive restarts, that is a small follow-up (persist the rotation back to
  the DB/TOML) and is called out as an open question rather than built speculatively.
- **A resource with no provider configured is byte-for-byte the path it was before.**
  No `ics_uid` is used as a conflict source from a provider, no token is loaded, and
  the boot-time registry for that resource is `None`; the native test suite runs and
  passes with no `[calendars]` block at all.

## Open questions

- **Whether to persist rotated refresh tokens back to the TOML/DB** so a process
  restart does not need the operator to re-paste the token. Deferred: the in-memory
  behaviour is the minimum that satisfies ADR 0009, and adding write-back of secrets to
  the operator's config file is a side effect (mutating a file the operator edits)
  that deserves its own decision.
- **Whether the DB `ics_uid` value should be indexed** for the re-runnable lookup.
  The write-back currently searches by the event's `iCalUID` within the resource's
  calendar window, so a dedicated index is not required; add one only if profiling
  shows the lookup is a cost.
