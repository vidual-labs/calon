# Changelog

All notable changes to calon are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written for an operator deciding whether to upgrade: plain language, one line
per user-visible change, describing the effect rather than the implementation.

## [Unreleased]

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
- No calendar handoff yet: an accepted booking comes back as a decision, not as something
  you can add to a calendar. That is the next phase — see the roadmap.

### Changed

- _Nothing yet._

### Fixed

- _Nothing yet._

### Security

- _Nothing yet._

<!--
On release, rename [Unreleased] to [X.Y.Z] - YYYY-MM-DD, add a fresh empty [Unreleased]
section above it, drop any sections that are still empty from the released version, and
update the link definitions below.
-->

[Unreleased]: https://github.com/vidual-labs/calon/compare/main...HEAD
