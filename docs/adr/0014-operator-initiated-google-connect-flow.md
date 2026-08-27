# 14. Operator-initiated Google Calendar connect flow, credential stored in SQLite

- **Status:** Accepted
- **Date:** 2026-08-27
- **Supersedes (narrowly):** the parts of
  [ADR 0013](0013-minimal-http-client-and-sqlite-credential-store.md) that say "calon does
  not run OAuth itself" and "not written to a database table". ADR 0009 and ADR 0013
  otherwise stay Accepted and unchanged: the `CalendarProvider` boundary, the `httpx`-only
  transport, and the out-of-band/TOML path are all still exactly as those ADRs describe.
  This ADR adds a second, operator-facing way to reach the same end state (a resource with
  a working refresh token), for Google only.

## Context

ADR 0013 deliberately kept calon out of the OAuth business: the operator runs the
provider's authorization flow themselves, in a browser, outside calon, and pastes the
resulting refresh token into `config/calon.toml`. That is correct for a scripted or
headless deployment, but it is a rough first-run experience for an operator who just wants
to click a button and connect their calendar — and it was flagged as friction rather than a
permanent constraint (ADR 0013's own "Open questions": persisting a rotated token was
deferred only because "adding write-back... deserves its own decision"). This ADR is that
decision.

The maintainer asked for a "Connect with Google" button on the operator dashboard, scoped
explicitly to the operator's own resource calendar (never a per-requester/customer
connection — that would be multi-tenancy and is out of scope per `CLAUDE.md` §3).

## Decision

### calon runs the authorization-code exchange, for Google, operator-gated

Two new routes, both behind the existing operator login (`AuthorisedOperator`, ADR 0010):

- `GET /calendars/{resource_slug}/connect` — builds Google's consent-screen URL and
  redirects the operator's browser to it.
- `GET /calendars/google/callback` — Google's redirect target. Exchanges the returned
  `code` for a refresh token and installs it.

The operator dashboard (`/bookings`) lists every `[calendars.<slug>]` entry with its
connection state and a Connect/Disconnect action.

**What does not change:** the OAuth app itself (`client_id`/`client_secret`) is still
registered by the operator in the provider's developer console and still lives in
`config/calon.toml` — a self-hosted instance cannot avoid that one-time step, since each
deployment needs its own registered redirect URI. Only the *refresh token* — the part that
used to be an out-of-band copy-paste — is now obtained by calon itself. The TOML
`refresh_token` field is unchanged and still works for a scripted/headless setup that never
touches the web panel; it now acts as a bootstrap seed, same as before (ADR 0013), with the
connect flow's DB-stored value taking precedence when both are present.

**Microsoft 365 is out of scope for this ADR.** It keeps the ADR 0013 out-of-band/TOML path
only. A Microsoft connect flow, if wanted later, follows the same shape and is its own
change (Microsoft's device/consent flow differs enough — tenant selection, Graph
permissions — to deserve its own review rather than being force-fit here).

**Explicitly not this:** any end-requester ever connecting their own calendar. The
connect flow only ever writes to the `calendar_credential` row for a resource slug the
operator's own config already names; there is no requester-facing route, no requester
account, and no new identity concept. Building that would be multi-tenancy and a CRM-shaped
feature, both out of bounds per `CLAUDE.md` §3, and was explicitly deferred by the
maintainer rather than built as "a small version" of it.

### CSRF state: signed, not stored

The `state` parameter is an HMAC-SHA256-signed `"<resource_slug>:<timestamp>"`, keyed by
`derive_login_key(CALON_LOGIN)` (`calon.security`, already used to sign operator sessions)
and checked against a 10-minute window on the way back. This mirrors how the external
intake framework already signs a payload instead of remembering one server-side
(`CLAUDE.md` §10, DRY) — no new session-state store, no new dependency, and the state is
worthless to anyone without the operator's login.

### Credential storage: a new `calendar_credential` table

```sql
CREATE TABLE calendar_credential (
    resource_slug     TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    refresh_token     TEXT NOT NULL,
    connected_at_utc  TIMESTAMP NOT NULL,
    updated_at_utc    TIMESTAMP NOT NULL
);
```

One row per connected resource. This reverses the specific ADR 0013 sentence "not written
to a database table" — deliberately, because the situation that justified that sentence
(the refresh token only ever entered calon as a value the operator typed into a file they
already own and back up) no longer holds once calon obtains the token itself. A token
calon obtains has nowhere else to naturally live; SQLite is calon's system of record for
everything else operational (`CLAUDE.md` §4.7), and this is one row, keyed by a slug calon
already treats as a natural key elsewhere in this same subsystem
(`CalendarProviderConfig.slug`, `CalendarProviderRegistry`).

At boot, `CalendarProviderRegistry.from_config` now accepts the DB-stored refresh tokens as
overrides, checked per resource slug: a DB-stored token wins over the TOML's, which is only
a bootstrap seed.

This narrows, but does not fully resolve, ADR 0013's open question about rotation: the
token `complete_connect` persists is the one Google issues *at connect time*, so a restart
picks up a working credential without the operator re-pasting anything, which the
out-of-band/TOML path still requires if its seed token has gone stale. What this ADR does
**not** change is a rotation that happens *during* a running process's uptime — Google may
return a new refresh token from a routine refresh-grant call, and `ProviderTransport`
still only adopts that into the in-process `TokenStore`, exactly as ADR 0013 left it. That
in-flight rotation still is not written back to the database or the TOML. Persisting it
too is a small, separate follow-up (threading a database session into the refresh path),
called out again in Open questions rather than built here.

**No encryption at rest.** The refresh token is stored in plaintext in `calendar.db`,
exactly as `client_secret` is stored in plaintext in `config/calon.toml` today, and exactly
as a requester's name and email are already stored in plaintext in `booking_intent`. calon
has one trust boundary — the host the operator controls — and adding column-level
encryption would need a key stored *somewhere*, which only moves the same problem rather
than solving it, for a self-hosted single-operator tool. `docs/self-hosting.md` is updated
to say `calon.db` deserves the same file-permission care as `config/calon.toml`.

### The registry gains runtime mutators

`CalendarProviderRegistry` was built once at boot and treated as read-only for the rest of
the process (mirroring `SourceRegistry`). A connect (or disconnect) happening through the
web panel must take effect on the very next availability check or write-back — without a
restart, since the whole point of the button is that it does not ask the operator to
restart the process. Two small mutators, `set_provider` and `remove_provider`, are added;
they are the only place outside `from_config` that touches the registry's internal dict.

## Consequences

- **A resource with no `[calendars.<slug>]` entry is unaffected.** No connect route does
  anything until the operator has already registered an OAuth app and put its
  `client_id`/`client_secret` in the TOML — the standalone baseline (`CLAUDE.md` §2) is
  untouched, and the native test suite continues to pass with nothing configured.
- **The Google Cloud OAuth client type changes from "Desktop app" to "Web application".**
  The out-of-band device flow (ADR 0013's original instructions) used a client that needs
  no redirect URI; the new in-app flow needs one (`<base_url>/calendars/google/callback`)
  registered with the provider. `docs/self-hosting.md` is updated with the new steps; an
  operator who already has a working Desktop-app-type out-of-band setup does not need to
  change anything — the TOML `refresh_token` path still works unmodified.
- **One new table, one new migration.** `calendar_credential` — see
  `docs/domain-model.md`. Nothing else in the schema changes.
- **The connect flow is Google-only.** A resource configured for `provider = "microsoft"`
  gets a clear error if the connect route is hit for it, not a silent failure; Microsoft
  365 keeps working exactly as before, on the out-of-band path.
- **No new runtime dependency.** The authorization-code exchange is one more `httpx` POST
  alongside the refresh-grant POST `calon.calendars.oauth` already made; both share the
  same transport module.

## Open questions

- **Whether to persist an in-flight refresh-token rotation** (a new token issued by a
  routine refresh-grant call while the process is already running, not the initial
  connect). Still deferred, exactly as ADR 0013 left it — this ADR only makes the
  *initial* connect durable across a restart.
- **Whether to also build a Microsoft 365 connect flow.** Deferred; the shape would mirror
  this one, but Microsoft's own flow (tenant selection, admin consent for some Graph
  permissions) is enough of a different shape to deserve its own review rather than reusing
  this ADR's specifics.
- **Whether the operator should be able to configure a *new* `[calendars.<slug>]` entry
  entirely from the web panel** (choose a provider, paste `client_id`/`client_secret`,
  without touching the TOML at all). Out of scope here: this ADR only replaces the
  *refresh-token* step with a button; registering the OAuth app itself is still a
  config-file step, deliberately, to keep this change small and reviewable.
