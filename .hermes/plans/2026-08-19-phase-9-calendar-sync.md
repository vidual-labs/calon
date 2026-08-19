# Phase 9 — Optional resource calendar sync (ADR 0009)

Target `0.3.0`. Branch `feat/phase-9-calendar-sync` off `main`.

## What the phase is

Per ADR 0009, optional per-resource sync with a connected Google Calendar or Microsoft
365 calendar, behind a `CalendarProvider` interface:

- **Read:** when evaluating availability (and judging a booking), query the connected
  calendar's free/busy over the candidate window; provider-reported busy time behaves
  exactly like an `existing` booking — it conflicts.
- **Write:** after a booking is accepted, create (or update) the corresponding event on
  the connected calendar, in addition to calon's record and the existing ICS handoff.

Prime directive (`CLAUDE.md` §2) is non-negotiable: a resource with no provider configured
behaves exactly as today; a provider that is unreachable or errors degrades to
calon-only availability rather than failing the booking; the native test suite runs and
passes with no provider configured.

## Design decisions (each justified at implementation per `CLAUDE.md` §8/§10)

1. **Domain stays pure.** Free/busy is a pure value, `FreeBusySpan` (starts_at_utc,
   ends_at_utc, reason), added alongside `BookedSpan`. The rule chain gains
   `free_busy: Sequence[FreeBusySpan] = ()` and a new rule, checked *after* own-booking
   conflicts. A new decision code `PROVIDER_CONFLICT` is added (stable public API —
   new code, never reusing `SLOT_CONFLICT`, per ADR 0009 + `CLAUDE.md` §5).
2. **`CalendarProvider` interface** (edge, `src/calon/calendars/` — *not* `src/calon/
   calendar/`, which would shadow the stdlib module, per `CLAUDE.md` §5). Two-method
   contract (mirrors `source_adapter`):
   - `free_busy(resource_slug, window_start_utc, window_end_utc) -> tuple[FreeBusySpan, ...]`
   - `upsert_event(resource_slug, event) -> None`
   The interface raises `CalendarProviderError` on failure; callers catch and degrade.
   A `FakeCalendar` in-test provider makes the contract testable without any network.
3. **Read path (free/busy) is advisory.** Availability and booking judgment call
   `provider.free_busy(...)` over the candidate window and merge the result into the
   pure `evaluate(...)` / `suggest_slots(...)` as `free_busy`. Any `CalendarProviderError`
   is caught at the edge and the request is judged against calon-only data — this is the
   ADR's "unreachable provider degrades availability" rule.
4. **Write path (write-back) is post-commit and non-blocking.** After
   `database.write()` commits and the booking exists, the route (not the service) calls
   `provider.upsert_event(...)`. A failure here is logged and audited
   (`booking.calendar_sync_failed`) but never fails the acceptance — the booking exists
   in calon, the ICS handoff is correct, and the operator can reconcile. This is the only
   way to honor "an unreachable provider does not take calon down" while still writing
   calon-originated events.
5. **Token storage in SQLite (new `calendar_credential` table).** The ADR's Consequences
   accept credential storage, scoped to the operator's own resource calendars. A
   `calendar_credential` row stores the base64-encoded refresh token / client token per
   `(resource_slug, provider)`; the operator supplies the initial refresh token via a
   helper command or sets it in the TOML for a first bootstrap. Token refresh uses the
   stored refresh token and writes the new one back. This is the smallest path that works
   with the existing SQLite choice (`CLAUDE.md` §4.7) and avoids a new datastore.
6. **Per-resource, opt-in, TOML-driven** (mirrors `[sources.<slug>]`): a new
   `[calendars.<resource_slug>]` key in `config/calon.toml` with `provider`,
   `calendar_id` (or `primary = true`), and a `resource_cal` reference. `CalendarProviderRegistry`
   is built once at boot from these tables, analogous to `SourceRegistry`, and exposed
   as a FastAPI dependency. A resource with no `[calendars.<slug>]` block gets no
   provider and behaves exactly as today.
7. **Provider clients: minimal stdlib HTTP** against the public REST APIs. Per
   `CLAUDE.md` §8 a stdlib solution under ~50 lines beats a dependency. The OAuth
   token-refresh loop and the free/busy / upsert calls are all simple
   `urllib.request` / `httpx` calls (httpx is already a transitive dependency via
   FastAPI) with no new runtime dependency. If a dependency turns out to be required,
   it is justified in the PR body and an ADR is written first (`CLAUDE.md` §10).
8. **One ADR for the dependency + token-storage decision.** ADR 0013
   ("minimal HTTP client and SQLite-backed credential store per provider") captures
   decision #7 and #5 above. ADR 0009 is accepted and unchanged.
9. **`max_advance_days` vs provider window limit.** Google's free/busy caps a single
   request at ~1 year; calon's `max_advance_days` default is 60. The provider call
   passes the window directly; no chunking needed for realistic policies.
10. **Idempotent write-back.** `upsert_event` uses the booking's iCal `UID` as the event
     identity on the provider side where the API supports it (Google's
     `calendarId/events/{eventId}` with a caller-chosen event id; Microsoft Graph by
     `internetBusy` + summary) so a retried write does not create a duplicate.

## Non-goals (per `CLAUDE.md` §3 / ADR 0009)

- No two-way sync of arbitrary provider events. calon writes only its own bookings.
- No multi-calendar-per-resource, no attendee management, no recurring-event support.
- No Google/Microsoft OAuth authorization *endpoint* in calon. The operator performs the
  OAuth dance once out-of-band and hands calon the refresh token; calon then only does
  token refresh + API calls.
- No change to the requester-facing ICS/deeplink handoff (ADR 0004 stands).

## Batch plan

### Batch 1 — Pure domain
Files:
- `src/calon/domain/availability.py` — add `FreeBusySpan` (like `BookedSpan`,
  starts_at_utc, ends_at_utc, reason, `covers()` + `conflicts_with()` semantics).
- `src/calon/domain/decision.py` — new `DecisionCode.PROVIDER_CONFLICT` (declared after
  `SLOT_CONFLICT`, before `ACCEPTED`; not a gating code).
- `src/calon/domain/rules.py` — `evaluate(..., free_busy: Sequence[FreeBusySpan] = ())`;
  new `_check_provider_conflicts(start_utc, end_utc, policy, free_busy)` rule that
  buffers the request and rejects with `PROVIDER_CONFLICT` on overlap.
- `src/calon/domain/slots.py` — `suggest_slots(..., free_busy=())` (pass through to
  `evaluate` per candidate).
Tests:
- `tests/domain/test_provider_conflict.py` — unit tests for the new value and rule:
  a busy span that overlaps the requested slot rejects with `PROVIDER_CONFLICT`; one
  that does not overlap accepts; buffered overlap rejects; a `free_busy=()` request
  behaves exactly as today (regression).
- `tests/domain/test_slots_with_free_busy.py` — suggests do not propose a slot that is
  busy per the provider.

Acceptance: `make check` passes; every domain test runs with no provider configured.

### Batch 2 — Edge: config + interface + FakeCalendar
Files:
- `src/calon/config.py` — new `CalendarProviderConfig` dataclass (provider, calendar_id,
  enabled, credentials_file if needed) and a `_calendars(reader)` parser for
  `[calendars.<resource_slug>]` tables, validated at startup with the same style as
  `_sources`. Add `calendars: dict[str, CalendarProviderConfig]` to `OperatorConfig`.
- `src/calon/calendars/__init__.py` — `CalendarProvider` protocol,
  `CalendarProviderError`, `FreeBusyResult`, `UpsertEvent` dataclass, `FakeCalendar`
  (in-memory, used by tests and standalone demos), `CalendarProviderRegistry` (built
  from `OperatorConfig.calendars`, exposes `provider_for(resource_slug) ->
  CalendarProvider | None`).
- `tests/test_config_calendars.py` — config-parsing and validation tests (mirroring
  `tests/intake/test_config_sources.py`): unknown keys rejected, missing `provider`
  errors, `provider` value not a known name errors at boot, `enabled=false` means the
  provider is not built.
- `tests/calendars/test_fake_provider.py` — `FakeCalendar` unit tests: free/busy returns
  exactly what was seeded and within window; upsert stores an event keyed by UID.

Acceptance: `make check` passes with zero providers configured (standalone CI unchanged).

### Batch 3 — Wiring + write-back
Files:
- `src/calon/services/availability_service.py` and `src/calon/api/v1/availability.py` —
  look up the resource's provider, call `free_busy(...)` over the window, catch
  `CalendarProviderError` and fall back to no provider, pass the result into
  `suggest_slots(free_busy=...)`.
- `src/calon/services/booking_service.py` — same for the `judge()` closure: pull
  free/busy over the same window as `blackouts` and `existing`, pass into `decide`. A
  `CalendarProviderError` inside `judge` is caught and the decision is re-run without
  provider input, so the booking is still judged (calon-only).
- `src/calon/api/deps.py` — new `CalendarProvidersDep` dependency, read from
  `app.state.calendar_providers` (the `CalendarProviderRegistry`).
- `src/calon/main.py` — build the `CalendarProviderRegistry` from
  `resolved_config.calendars` in the lifespan, store on `app.state.calendar_providers`,
  log the count.
- `src/calon/api/v1/bookings.py` and `src/calon/api/v1/intake.py` — after commit, if
  the booking is accepted and a provider is configured for the resource, call
  `provider.upsert_event(...)`. A failure is logged and appended to the audit log as
  `booking.calendar_sync_failed`; the response is unchanged.
- `src/calon/services/repository.py` (or a new `sync_audit` helper) — helper to append
  the `calendar_sync_failed` audit event.
- Migrations: no new table required (credentials are in TOML for the minimal shape;
  the `calendar_credential` table from design #5 is added in Batch 4 once the real
  OAuth-refresh path needs it and the ADR justifies it).
- Tests:
  - `tests/api/test_availability_with_provider.py` — a `FakeCalendar` seeded with a
    busy span hides the overlapping slot from `/api/v1/availability`; a
    `CalendarProviderError`-raising provider leaves availability unchanged.
  - `tests/api/test_bookings_with_provider.py` — an accepted booking on a resource
    with a `FakeCalendar` appears in the fake's event store keyed by the booking's UID;
    a provider that raises in `upsert_event` does not fail the acceptance and is
    audited.
  - `tests/api/test_standalone_regression.py` — the existing native test suite still
    passes without any provider configured (this is the `CLAUDE.md` §2 mechanical
    check, not a new test but a CI assertion that stands on its own).

Acceptance: `make check` passes; every standalone test still passes; the `FakeCalendar`
is the only provider wired, so no network touches in CI.

### Batch 4 — Google Calendar provider (real client)
Files:
- `src/calon/calendars/google.py` — `GoogleCalendarProvider` implementing
  `CalendarProvider`. Uses `httpx` (already transitive) against the public
  `https://www.googleapis.com/calendar/v3/` endpoints. Token refresh: POST to
  `https://oauth2.googleapis.com/token` with grant_type=refresh_token. Free/busy: POST
  `.../freeBusy/...` scoped to `resource_slug`'s `calendar_id`. Upsert: PATCH
  `.../calendars/{id}/events/{uid}` (or POST if 404) with a caller-chosen event id
  equal to the booking's UID.
- `src/calon/calendars/oauth.py` — a small helper for the one-time operator bootstrap:
  given a code + redirect, exchange for a refresh token (or the operator can paste the
  refresh token directly into the TOML). This is the only part the operator ever runs
  interactively; calon does not run a browser or an authorization code loop.
Tests:
- `tests/calendars/test_google_provider.py` — unit tests with mocked HTTP: free/busy
  response parsed correctly; a 401 with `invalid_grant` triggers a token refresh and
  one retry; a second 401 raises `CalendarProviderError` (the caller degrades);
  upsert on 200 and on 404-then-POST both work.
- `tests/calendars/test_google_oauth.py` — the refresh-token POST body is correct; the
  token stored after refresh is exactly what the API returned.

Acceptance: `make check` passes; no real Google call is made in any test (all HTTP is
mocked). The provider is behind the same opt-in `[calendars.default]` key.

### Batch 5 — Microsoft Graph provider (real client)
Files:
- `src/calon/calendars/microsoft.py` — `MicrosoftGraphProvider` implementing
  `CalendarProvider` against `https://graph.microsoft.com/v1.0/`. Token refresh: POST
  `https://login.microsoftonline.com/common/oauth2/v2.0/token` with
  `grant_type=refresh_token`. Free/busy: POST
  `.../users/{user}/calendar/getSchedule` or the busy-info endpoint. Upsert: PATCH
  `.../users/{user}/calendarView/...` by the booking's UID (or create then PATCH).
Tests:
- `tests/calendars/test_microsoft_provider.py` — mocked HTTP, same shape as Google.
- `tests/calendars/test_microsoft_oauth.py` — refresh-token body and parse.

Acceptance: `make check` passes; no real Microsoft call in tests; same opt-in key.

### Batch 6 — Docs + final green + PR
Files:
- `docs/adr/0013-*.md` — new ADR: "minimal HTTP client and SQLite-backed credential
  store per provider" (decision #5, #7 above). ADR 0009 stays Accepted and unchanged;
  ADR 0013 supersedes the "to be decided at implementation time" note in it.
- `config/calon.example.toml` — documented `[calendars.<resource_slug>]` block with
  `provider = "google"` / `"microsoft"`, `calendar_id`, `enabled`, and a comment that
  the operator supplies the refresh token out-of-band.
- `.env.example` — no new env vars (tokens live in the TOML, not the env).
- `docs/self-hosting.md` — a "Connecting a resource's calendar" section: how to obtain
  a refresh token for each provider, what to put in the TOML, what happens on provider
  error, how to verify the write-back.
- `docs/domain-model.md` — document the new `PROVIDER_CONFLICT` decision code and the
  `FreeBusySpan` value object.
- `README.md` — roadmap row 9 marked done; a short paragraph in "External intake"
  style about the new "Calendar sync" section.
- `CHANGELOG.md` — `[Unreleased]` / `0.3.0` entry: new feature, opt-in, per-resource,
  degrades gracefully, new decision code, ADR 0013.
- `src/calon/__init__.py` — `__version__` bumped to `0.3.0`.
- `make check` green; push branch to origin; open a PR on `vidual-labs/calon` for
  Richard to review (per user preference: PR is opened, not merged).

## Risks / open questions (resolved at the batch that owns them)

- **Token storage location.** Design #5 stores the refresh token in a new SQLite table
  populated from the TOML at boot. If the operator prefers env-only, the ADR 0013
  discussion may land elsewhere — but TOML keeps the secret in a file the operator edits
  and backs up, consistent with `CLAUDE.md` §8 "never commit secrets" (TOML is where
  all other per-source secrets live today).
- **OAuth bootstrap UX.** Out-of-band by design. If the operator wants a `calon
  calendar-auth <provider>` helper command, that is a small addition in Batch 4/5 and
  does not change the provider contract.
- **Provider rate limits / retries.** A single retry on 401 is enough for the common
  case of an expired access token (refresh, retry). Anything else raises
  `CalendarProviderError` and the caller degrades.
- **Write-back idempotency across providers.** Google allows caller-chosen event id;
  Microsoft Graph does not, so the `upsert_event` there is a create-then-patch flow that
  is re-runnable by UID. Documented in Batch 4/5.
