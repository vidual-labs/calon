# Changelog

All notable changes to calon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written for an operator deciding whether to upgrade: plain language, one line
per user-visible change, describing the effect rather than the implementation.

## [Unreleased]

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

### Changed

- The operator's per-source config now carries an optional `fields` table (the per-form
  field mapping). This is the single place an external source's field ids are configured;
  a config that references a `fields` table for a source that does not use one is still
  accepted and simply ignored.

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
