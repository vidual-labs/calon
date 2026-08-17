# CLAUDE.md — project memory and coding policy for calon

This file is durable project memory. Read it before making changes. It outranks habit,
convention from other projects, and anything inferred from a single request.

---

## 1. Project purpose

> **calon** is a lean, self-hostable booking intake tool. It captures booking requests,
> applies operator-defined scheduling rules, checks availability, and produces a generic
> calendar handoff that works with any major calendar.

The product name is **always lowercase**: `calon`. Never "Calon", never "CALON". If a
sentence would begin with it, restructure the sentence.

---

## 2. Prime directive: standalone first

**calon must remain fully usable with zero external services configured.**

Reject any change that makes an external lead source, a calendar provider API, a
third-party account, or a network dependency *required* for the core booking flow:
intake → canonical intent → rules → decision → calendar handoff → audit.

External integrations are strictly additive and always optional. If a proposed change
weakens this boundary, say so explicitly and propose the simpler standalone alternative
instead of implementing it.

This explicitly covers calendar-provider sync (Google Calendar, Microsoft 365): calon's own
rule evaluation and conflict detection against its own bookings must keep working correctly
with zero calendar integration configured. When a resource has a provider connected, sync
adds an extra free/busy check and writes calon-originated bookings into that calendar — it
never becomes a precondition for accepting a booking.

This is enforced mechanically, not just by convention: CI runs the full native test suite
with **no** external sources configured. Do not weaken or skip that job.

---

## 3. Scope boundaries

calon **is**: a booking intake endpoint, a scheduling-rules evaluator, a conflict detector
over its own bookings, a generic calendar handoff producer, an auditable decision log.

calon **is not**:

- a CRM (no contacts, pipelines, deals, campaigns)
- a restaurant or hotel reservation suite (no tables, covers, room inventory, rate plans)
- a generic automation or workflow platform
- an OpenFlow plugin, or dependent on any external lead source
- a general-purpose calendar sync or mirroring tool — the optional Google Calendar /
  Microsoft 365 integration only checks free/busy for the booked resource and writes
  calon-originated bookings; it does not two-way sync arbitrary events, calendars, or
  attendees

**If a request implies CRM, workflow automation, payments, multi-tenancy, or AI features:
stop and flag it as out of scope before writing any code.** Do not implement it "just a
small version" — small versions of out-of-scope features are how the boundary erodes.

Planned: optional two-way sync with a resource's calendar (Google Calendar and Microsoft
365) — checking free/busy as an additional availability signal and writing calon-originated
bookings once accepted. Additive and opt-in per §2; see the roadmap in `README.md` and
[ADR 0009](docs/adr/0009-optional-resource-calendar-sync.md).

Deferred by design, not forgotten: requester-facing cancel and reschedule links, an operator
HTML view, and confirmation email. Each requires an explicit decision before it is built.

---

## 4. Architecture principles

1. **`src/calon/domain/` is pure.** No I/O, no ORM imports, no FastAPI imports, no file or
   network access, and no reading the wall clock — the current time is always injected as a
   parameter. The domain layer must be unit-testable with no fixtures and no database.
2. **All intake paths converge on `booking_service.submit_intent()`.** Native intake is
   implemented *as a source adapter* so there is exactly one downstream code path. Never
   put scheduling logic in a route handler, a template, or an adapter.
3. **Adapters translate. Adapters never decide.** A source adapter maps a provider payload
   onto the canonical `BookingIntentIn` and nothing more. Anything it cannot map goes into
   `metadata` untouched. No adapter may read availability rules or emit a decision.
4. **All instants are stored in UTC**, with the relevant IANA timezone string carried
   alongside. Never store or pass a naive `datetime`. Convert at the edges, never in the
   middle.
5. **Prefer extending the existing ordered rule chain over adding a subsystem.** A new
   booking constraint is almost always one new rule plus one new decision code.
6. **The canonical schemas in `src/calon/schemas.py` are the public contract.** Treat
   changes to them as API changes, not refactors.
7. **SQLite is a deliberate choice, not a placeholder.** Do not introduce Postgres, Redis,
   a message queue, or a background worker without an ADR justifying the operational cost
   against the self-hosting goal.

---

## 5. Naming rules

- Product name: lowercase `calon`, always.
- Python: `snake_case` for modules and functions, `PascalCase` for classes,
  `SCREAMING_SNAKE_CASE` for enum members and constants.
- **Decision codes are stable public API.** `SCREAMING_SNAKE_CASE`. Once a code has
  shipped, it is never renamed, never repurposed, and never has its meaning changed. Add a
  new code instead.
- HTTP: plural noun paths under `/api/v1/…`; JSON fields are `snake_case`.
- Database: singular table names (`booking`, `resource`, `audit_event`), `*_utc` suffix on
  every UTC timestamp column so the unit is visible at the call site.
- Branches: `feat/…`, `fix/…`, `docs/…`, `chore/…`, `refactor/…`, `test/…`.
- Commits: [Conventional Commits](https://www.conventionalcommits.org/).
- **Never shadow a stdlib module name.** This is why the calendar code lives in
  `src/calon/calendarkit/` and not `src/calon/calendar/` — `import calendar` resolves to
  the stdlib and shadowing it breaks imports in confusing, hard-to-trace ways. The same
  applies to `types`, `json`, `email`, `secrets`, and `logging`.

---

## 6. Versioning rules

- [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
- **Pre-1.0:** the minor version may contain breaking changes; the patch version is fixes
  only. Breaking changes must be called out in `CHANGELOG.md` under a `### Changed` entry
  beginning with `**BREAKING:**`.
- The HTTP API path version (`/api/v1`) bumps **only** on a breaking contract change, not
  on every release.
- Adding an optional field to a schema is additive. Removing a field, renaming a field,
  changing a type, or changing the meaning of a decision code is breaking.
- The single source of truth for the version is `__version__` in `src/calon/__init__.py`,
  read by `pyproject.toml`.
- Releases are tagged `vX.Y.Z`.

---

## 7. Documentation update rules

A change is **not complete** until its documentation is complete. Specifically:

| If the change… | Then you must update… |
| --- | --- |
| is user-visible in any way | `CHANGELOG.md` under `[Unreleased]` |
| alters scope, quick start, or roadmap | `README.md` |
| makes an architectural decision | a new numbered ADR in `docs/adr/` |
| changes a schema, table, or decision code | `docs/domain-model.md` |
| changes ICS or deeplink output | `docs/calendar-handoff.md` |
| changes the adapter contract or intake auth | `docs/external-intake.md` |
| changes how calon is deployed or configured | `docs/self-hosting.md` and `.env.example` |
| adds a configurable rule | `config/calon.example.toml`, with a comment |

**Changelog style:** written for a human operator deciding whether to upgrade, not a dump
of commit subjects. One line per user-visible change, in plain language, describing the
effect rather than the implementation. Never list internal refactors.

**ADR style:** one decision per file, numbered sequentially, named
`NNNN-short-kebab-title.md`, with Status / Context / Decision / Consequences. ADRs are
immutable once merged — to reverse one, write a new ADR that supersedes it and mark the old
one `Superseded by NNNN`.

---

## 8. Implementation discipline

- **Smallest change that satisfies the requirement.** No speculative abstraction, no
  "we'll need this later" parameters, no interfaces with a single implementation added in
  advance of a second one.
- **No new runtime dependency** without a justification in the pull request description,
  and an ADR if it is architectural. Weigh every dependency against self-hostability. A
  stdlib solution under ~50 lines beats a dependency.
- **Every rule change ships with a unit test.** Every endpoint ships with an integration
  test. Every ICS or deeplink change ships with a golden-file assertion.
- **Test the boundaries, not just the happy path:** DST transitions, requests exactly on a
  window edge, zero-length and negative durations, buffers that overlap adjacent bookings,
  and simultaneous requests for the same slot.
- **No `# type: ignore` in `domain/`** without an inline comment explaining why.
- **Run `make check`** (ruff + mypy + pytest) before reporting work as done. Do not claim
  green without having run it.
- **Never commit secrets**, `calon.db*`, `.env`, or `config/calon.toml`. Only
  `config/calon.example.toml` and `.env.example` are tracked.
- Prefer editing an existing file over creating a new one. Do not create documentation
  files that were not asked for.

---

## 9. Definition of done

- [ ] Behavior implemented at the smallest reasonable scope
- [ ] Unit tests for domain logic; integration tests for endpoints
- [ ] `make check` passes locally
- [ ] `CHANGELOG.md` `[Unreleased]` updated for user-visible changes
- [ ] Affected docs from the table in §7 updated
- [ ] An ADR added if an architectural decision was made
- [ ] No new dependency, or a new dependency that is justified in the PR body
- [ ] The standalone-first boundary (§2) is intact

---

## 10. Ask rather than assume

Stop and ask the maintainer before:

- changing or relicensing the project
- adding any runtime dependency that is not trivially replaceable
- changing the canonical schemas, the decision codes, or the API contract
- introducing authentication, sessions, accounts, or multi-tenancy
- anything that touches the standalone-first boundary in §2
- replacing SQLite, or adding a second datastore or a background worker
- adding a feature that sits near one of the "is not" boundaries in §3

When genuinely uncertain about product intent, ask one specific question rather than
building both options.
