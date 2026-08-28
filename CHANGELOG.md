# Changelog

All notable changes to calon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written for an operator deciding whether to upgrade: plain language, one line
per user-visible change, describing the effect rather than the implementation.

## [Unreleased]

### Added

- **The booking page now shows what is actually free.** `/book` opens on a month
  calendar with the bookable days lit up, the times for the day you pick beside it (12h or
  24h, your choice), and the details form after that — instead of a blank date and time
  box you had to guess at. Days and times come from the same rules that judge the booking,
  so a closed day, a slot outside your window, one inside the notice period, or one
  already taken simply is not offered. Nothing else about booking changed: the times shown
  are still advisory (two people can click the same one and the second is still rejected,
  with the reasons and alternatives as before), and the page still works with JavaScript
  turned off — it falls back to the plain date and time fields, as does a page that cannot
  reach the availability endpoint.
- **Subscribe to a calendar feed — no developer console needed.** If you cannot (or would
  rather not) register an OAuth app, publish your calendar from its own settings — Google
  Calendar and Outlook both offer a secret iCal address — and paste that address into the
  Calendars panel. calon reads your busy times from it and refuses bookings that clash,
  with the same `PROVIDER_CONFLICT` answer a connected calendar gives. Two things to know:
  it is **read-only** (accepted bookings still reach your calendar through the .ics link,
  not by being written in), and published feeds refresh on the provider's own schedule,
  which can lag by hours. A resource uses either a feed or an OAuth connection, not both.
  Also configurable as `provider = "ics"` with a `feed_url` in `config/calon.toml`.
- **You can now connect a Google Calendar entirely from the operator dashboard.** The
  Calendars panel has a form for the OAuth client id and secret, so connecting a resource
  no longer requires editing `config/calon.toml` on the host and restarting: register the
  OAuth client in Google Cloud Console with the redirect URI the panel prints, paste the
  two values, click Connect with Google, approve. The credentials are stored in `calon.db`
  (back that file up — it now holds the client secret as well as the refresh token) and can
  be removed again with "Forget credentials". A `[calendars.<slug>]` entry in
  `config/calon.toml` still wins wherever you have one, and the form refuses to overwrite
  it. Microsoft 365 still uses the out-of-band setup.
- The operator dashboard now has a **"Connect with Google"** button for each configured
  resource calendar. Instead of running Google's OAuth flow out-of-band and pasting a
  refresh token into `config/calon.toml`, an operator with a `[calendars.<slug>]` entry
  set up (provider, `client_id`, `client_secret`) can now click Connect, authorize on
  Google's own consent screen, and be redirected straight back — connected, with no
  restart required. The OAuth app itself still has to be registered once in Google Cloud
  Console (that step cannot be automated for a self-hosted instance), but the manual
  refresh-token copy-paste is gone. Microsoft 365 is unchanged and still uses the
  out-of-band setup. See `docs/self-hosting.md`.
- `[calendars.<resource_slug>]` now accepts `client_id` and `client_secret` — the
  connected provider's OAuth app credentials. Previously there was nowhere to
  configure them, so a connected Google Calendar or Microsoft 365 calendar could
  never actually refresh an access token and every sync attempt failed silently.
- The operator dashboard's header now has a **Bookings** / **Calendars** navigation menu
  (the Calendars link only appears once at least one `[calendars.<slug>]` resource is
  configured), so the Connect-with-Google panel is one click away from anywhere in the
  operator area instead of requiring a scroll to find.
- The operator dashboard now opens with an **Overview** panel: every function the instance
  exposes (booking form, booking and availability APIs, calendar handoff, external intake,
  calendar sync, API-key access, API docs) with its live status, and the scheduling rules
  currently in force — days, window, duration, slot grid, notice, horizon, buffers, daily
  cap, blackouts — read from the config as calon actually parsed it. Checking what an
  instance is doing no longer means opening a shell on the server.
- The **Calendars** panel is now always shown. A resource with no `[calendars.<slug>]`
  entry gets a "Not configured" row and, folded away underneath it, the exact steps to
  connect it: the redirect URI to register on the OAuth client and the TOML block to
  paste, with that resource's real slug filled in. Previously the whole panel was hidden
  until a calendar was already configured, so there was no way to discover the
  Connect-with-Google flow from the operator area. Nothing about it is required — an
  instance with no calendar configured still works exactly as before, the row simply says
  so instead of showing nothing at all.
- The Calendars panel now shows the exact Google OAuth redirect URI to register on the
  OAuth client, computed from the instance's actual `base_url` — so a `CALON_BASE_URL`
  that doesn't match what's registered in Google Cloud Console (the most common cause of
  a failed connect) is visible in the dashboard instead of only in `docs/self-hosting.md`.

### Changed

- **BREAKING:** an enabled `[calendars.<resource_slug>]` entry now requires both
  `client_id` and `client_secret`; calon refuses to start without them rather
  than booting into a calendar sync that can never work. If you already have a
  `[calendars.*]` block enabled, add both before upgrading (see
  `docs/self-hosting.md`).

### Fixed

- An OpenFlow form's start/end answer with no UTC offset (the common case for a
  form's own date/time fields) was interpreted in the server process's local
  timezone instead of the form's configured one, which could book the wrong hour
  depending on the host's `TZ`.
- An OpenFlow submission whose payload `timestamp` had no UTC offset caused the
  intake endpoint to answer `500` instead of accepting the request.
- Enabling more than one external intake source, with OpenFlow among them, could
  build the OpenFlow adapter from a different source's secret and field mapping
  (or fail to boot claiming OpenFlow has no field mapping when it does), depending
  on the order sources were listed in `config/calon.toml`.
- Logging out of the operator panel revoked the session on the server but never
  cleared the browser's session cookie.
- Timestamps on the operator dashboard were rendered as malformed, unparseable
  ISO 8601 strings (a doubled UTC suffix).
- An accepted booking's `.ics` file, its calendar handoff, and the event written
  back to a connected provider could carry three different `UID`s for the same
  booking, so a calendar could not recognise them as the same event. All three
  now agree on one identity.
- A booking accepted through external intake minted its calendar `UID` from the
  source's own slug instead of the instance's configured host, unlike the native
  booking API and the public booking form.
- The public booking form (`POST /book`) never checked a connected resource's
  calendar for conflicts and never wrote accepted bookings back to it, unlike
  the booking API — a resource with calendar sync configured could be double
  booked through its own booking page.
- The `.ics` calendar file emitted its revision as `SEQ`, a property RFC 5545
  does not register, instead of `SEQUENCE`. Calendar clients ignored the value,
  so a re-download of an amended booking was not guaranteed to update the
  existing calendar entry in place.
- A booking accepted through external intake always reported its start and end
  times in UTC, regardless of the timezone the request declared — unlike the
  native booking API, which reports them in the requester's own timezone as
  documented.
- The Google Calendar and Microsoft 365 OAuth token refresh posted a JSON body;
  both providers' token endpoints require form encoding and rejected it, so a
  connected calendar's access token could never actually be refreshed.
- A Google Calendar write-back sent `event.uid` as the event's own `id`, but
  calon's UID contains characters (`-`, `@`) Google's event-id charset does not
  allow, so the write-back always failed for a connected Google resource.
  Bookings are now keyed by a derived id and identified to Google by `iCalUID`.
- The `freeBusy` request to Google Calendar sent a bare list of calendar-id
  strings; the API requires a list of objects and rejected the request.
- A Microsoft Graph timestamp with no UTC offset (the shape Graph's free/busy
  response actually uses) was reinterpreted in the server process's local
  timezone instead of being read as UTC, which could shift a resource's busy
  time by several hours depending on the host's `TZ`.
- A Google Calendar write-back's create-vs-update decision inspected the
  exception message for the substring `"404"`, which could misfire when the
  request URL happened to contain those digits. It now checks the response's
  actual HTTP status.
- Microsoft 365 free/busy checking called `getFreeBusy`, an action Graph v1.0
  does not have on that path, so it always failed and every resource with a
  connected Microsoft calendar degraded to calon-only availability. It now
  calls the real `getSchedule` action, and only a definite `busy` conflict
  narrows availability.
- A Microsoft 365 calendar write-back's event create/update request omitted
  the required `timeZone` alongside `dateTime`, which Graph rejects as a
  malformed event.
- The operator login's session table never dropped an expired session's own
  record (only ever treating it as invalid), so a long-running instance's
  memory usage grew by one entry per login for its entire uptime.

### Security

- _Nothing yet._
## [0.3.0] - 2026-08-19

### Added

- OpenFlow is now a supported external intake source. A form submission from OpenFlow
  signs its body with a single `X-OpenFlow-Signature` header (HMAC-SHA256), and calon
  authenticates it against the shared secret and an anti-replay window anchored on the
  payload's own timestamp. To map one of your OpenFlow forms to a booking, add a
  `[sources.openflow.fields.<formId>]` block: one table per form, naming the OpenFlow
  field id that holds each of start, end, name, email, phone, subject, and the form's
  timezone. Until at least one form is mapped, the source cannot be enabled: the boot
  refuses to start an enabled OpenFlow source with no field map, loudly and at startup.
- `calon.example.toml` now documents the OpenFlow source in full: the single
  signature scheme, the per-form field-mapping table, and the precedence rule (a request
  that also carries the canonical `X-Calon-*` headers is verified by that scheme
  instead).

- Optional resource calendar sync (Google Calendar and Microsoft 365) is now available,
  opt-in per resource. A resource whose operator has connected its own calendar now has
  that calendar's real busy time respected when calon checks availability, and an accepted
  booking is written back to that calendar so the operator does not copy it in by hand. A
  resource with no calendar connected behaves exactly as before: conflicts are checked
  against calon's own bookings only, and the requester still gets the .ics plus deeplinks.
- A new decision code, `PROVIDER_CONFLICT`, is returned (distinct from `SLOT_CONFLICT`)
  when a candidate slot overlaps time the resource's connected calendar already has busy,
  so a requester learns the clash is with the resource's external calendar rather than
  with another booking. See ADR 0009 and ADR 0013.

### Changed

- The operator's per-source config now carries an optional `fields` table (the per-form
  field mapping). This is the single place an external source's field ids are configured;
  a config that references a `fields` table for a source that does not use one is still
  accepted and simply ignored.

- The provider's free/busy read and the booking write-back are now real network calls (a
  Google Calendar free/busy query, or a Microsoft Graph `getFreeBusy` query; a real event
  upsert), both behind a `CalendarProvider` interface and both degrading to Calon-only
  availability when the provider is unreachable — an unreachable calendar makes a slot less
  accurate, it does not take booking down.
- `config/calon.toml` now accepts a `[calendars.<resource_slug>]` block (provider,
  `calendar_id`, `refresh_token`, `enabled`) to opt a resource into calendar sync; the
  refresh token is supplied out-of-band (no OAuth is performed inside calon).
- ADR 0013 records the "minimal HTTP client + token storage" decisions and supersedes the
  open note in ADR 0009.
### Fixed

- _Nothing yet._

## [0.1.0] - 2026-08-18

### Added

- Initial repository foundation: `README.md`, `CLAUDE.md`, `CHANGELOG.md`, and `LICENSE`
  (GNU AGPL-3.0-or-later).
- Contribution, security, and code-of-conduct policies, plus issue and pull request
  templates.
- Architecture decision records covering the license choice, SQLite for the MVP, the
  ICS-first calendar handoff, and the source adapter boundary.
- Project documentation skeleton: architecture, domain model, calendar handoff, external
  intake, and self-hosting.
- Python project scaffolding (`pyproject.toml`, `Makefile`) and continuous integration
  running ruff, mypy, and pytest.
- Example operator configuration (`config/calon.example.toml`) documenting every planned
  scheduling rule, and `.env.example` for runtime settings.
- The scheduling engine that decides whether a booking request can be accepted: it applies
  minimum notice, the advance-booking horizon, allowed weekdays, the daily window, blackout
  periods, an optional per-day cap, and conflicts with existing bookings including their
  buffers. A rejection explains every rule that failed, not just the first, and offers up
  to three next-available alternatives in the requester's own timezone.
- calon now runs. `POST /api/v1/bookings` accepts a booking request, applies your rules,
  and books it or explains why not; `GET /api/v1/availability` lists what is free in a
  window of up to 31 days, so a requester can pick a time instead of guessing at one.
  Availability is advisory — it reserves nothing, and the answer that counts is the one you
  get when you submit.
- Bookings, requests, and decisions are stored in a SQLite database, created and migrated
  automatically at startup. The database file is the entirety of your booking state.
- Every request and every decision is recorded in an append-only audit log, including the
  ones that were refused — those are the rows worth reading when you want to know why
  nobody could book a Friday.
- Your rules come from `config/calon.toml` and are re-read every time calon starts, so
  editing the file and restarting is the whole configuration workflow. A file that cannot
  be understood stops startup rather than being half-applied. calon still runs with no
  configuration file at all, on the defaults `config/calon.example.toml` documents.
- Two requests for the same slot arriving at the same moment cannot both be accepted.
- **Calendar handoff (phase 3).** An accepted booking now comes back with a `CalendarHandoff`:
  a stable `UID` (`<booking-id>@<CALON_INSTANCE_HOST>`), the event's start/end in UTC, and
  three one-click deeplinks (Google Calendar, Microsoft 365, and Outlook.com). A new endpoint
  `GET /api/v1/bookings/{id}/calendar.ics` serves the RFC 5545 file as
  `text/calendar; charset=utf-8` with `Content-Disposition: attachment`. The ICS event uses
  `METHOD:PUBLISH` and `STATUS:CONFIRMED`, and the `SEQUENCE` field is reserved for
  amendments (post-MVP). The `.ics` endpoint and the accept response's deeplinks are
  **login-gated** because they carry a requester's name and subject — set `CALON_LOGIN`.
- **Operator web panel (phase 3→4 bridge).** A lean, server-rendered panel: `/login` (the
  only public page), `/bookings` (login-gated list of every booking with a "Download .ics"
  link), and `/logout`. The login is a single shared operator key (not per-user accounts);
  the session is an HTTP-only, memory-only cookie. Gated by the same `CALON_LOGIN` and
  `CALON_API_KEY` (optional Bearer) as the `.ics` endpoint. See
  [ADR 0010](adr/0010-operator-login-and-web-panel.md).
- **Public booking form (phase 4).** A public, server-rendered web form at `/book` that
  posts to the same `submit_intent` path as the API. No login required, no JavaScript.
  On acceptance the success page shows the booked slot in the requester's timezone and
  links to the calendar handoff (`.ics`, Google Calendar, Outlook). On rejection the form
  is re-displayed with all entered values preserved and the domain layer's violation
  messages and up-to-three "next available" suggestions rendered inline. See
  [ADR 0011](adr/0011-public-booking-form.md).
- **Docker packaging (phase 6).** `Dockerfile` (multi-stage, Python 3.13, non-privileged
  user, `/healthz` healthcheck) and `docker-compose.yml` (single service, persistent
  `data` volume for the SQLite file, `config/` mounted read-only). Ship with
  `docker compose up -d --build`.
- **External intake (phase 5).** External systems (e.g. OpenFlow) can now submit booking
  requests to `POST /api/v1/<slug>` over a signed webhook. A per-source adapter translates
  the provider's payload into calon's canonical booking intent and hands it to the same
  `submit_intent` path the native form uses — the scheduling core has no knowledge of the
  provider, which is what keeps it standalone. Requests are authenticated with
  HMAC-SHA256 (per-source shared secret and a timestamp window; bad signatures return
  `401`), and a retried request returns the decision first produced rather than
  re-evaluating — a rejection cannot silently become an acceptance because the calendar
  has moved since. Sources are disabled by default and enabled one
  `[sources.<slug>]` config block at a time; unknown slugs return `404`. No
  provider-specific adapter ships in `0.1.0` — the framework is proven with a synthetic
  test source, and [ADR 0012](docs/adr/0012-external-intake-final.md) records the
  concrete decisions (endpoint path, auth scheme, replay semantics, boot-time registry).
- An ASCII logo at the top of `README.md`.

### Security

- New `CALON_LOGIN` runtime setting (see `.env.example`). When set, it gates the operator
  web panel and every endpoint that returns personal data — most importantly
  `GET /api/v1/bookings/{id}/calendar.ics`, which carries a requester's name and subject.
  Without `CALON_LOGIN` the operator surface returns `503` (it fails closed rather than
  open); the public booking API still works.
- New optional `CALON_API_KEY` runtime setting: a shared Bearer token for programmatic
  access to the same operator endpoints (for example, scripting the panel or integrating
  an external system). Unset by default.
- The operator login session is an `HttpOnly`, `SameSite=Lax` cookie whose token is a
  random value the server keeps in **memory**. It is marked `Secure` when `CALON_BASE_URL`
  is `https://`. No session is written to disk; a restart clears all sessions. Nothing is
  stored on disk that would let an attacker open the operator panel or read a requester's
  booking.
- The `.ics` endpoint is now authenticated rather than public. If you have a
  deployment where the previous "public `.ics`" behaviour mattered, see
  [ADR 0010](adr/0010-operator-login-and-web-panel.md).

<!--
On release, rename [Unreleased] to [X.Y.Z] - YYYY-MM-DD, add a fresh empty [Unreleased]
section above it, drop any sections that are still empty from the released version, and
update the link definitions below.
-->

[Unreleased]: https://github.com/vidual-labs/calon/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vidual-labs/calon/releases/tag/v0.1.0
