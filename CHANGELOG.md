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
  to three next-available alternatives in the requester's own timezone. Not yet reachable
  over HTTP — see the roadmap.

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
