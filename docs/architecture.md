# Architecture

> Status: planned. No product code exists yet — this describes what will be built in
> phases 1–6. See the [roadmap](../README.md#roadmap).

## The shape of the system

calon does one thing: it turns a booking request into a decision and a calendar handoff,
and records what it did.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  EDGES  (adapt to the core; hold all the I/O)                   │
  │                                                                 │
  │   web/          api/v1/         intake/          calendarkit/   │
  │   Jinja2 form   FastAPI         source           ICS +          │
  │                 routes          adapters         deeplinks      │
  └───────────────┬─────────────────────────────────────────────────┘
                  │  BookingIntentIn (canonical)
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  services/booking_service.py                                    │
  │  The single entry point. Loads rules, calls the domain, writes  │
  │  the outcome, emits audit events. Owns the transaction.         │
  └───────────────┬─────────────────────────────────────────────────┘
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  domain/   PURE — no I/O, no ORM, no framework, no clock        │
  │  rules.py · decision.py · availability.py · slots.py            │
  └─────────────────────────────────────────────────────────────────┘
                  │
                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  models.py + SQLite (WAL)                                       │
  └─────────────────────────────────────────────────────────────────┘
```

## The three rules that matter

**1. The domain layer is pure.** `src/calon/domain/` imports nothing from SQLAlchemy,
FastAPI, or the filesystem, and never reads the wall clock — the current time is passed in.
This is what makes the scheduling logic testable: every awkward case (a DST transition, a
request landing exactly on the window boundary, buffers colliding with an adjacent booking)
is a plain function call with no fixtures.

**2. Every intake path converges on one function.** `booking_service.submit_intent()` is the
only place a booking can be created. Native intake is implemented *as a source adapter*, so
a request from the web form and a request from an external webhook run through identical
code from that point on. There is no "external path" that could drift from the native one.

**3. Adapters translate; adapters never decide.** A source adapter maps a provider payload
onto `BookingIntentIn` and stops. It does not read availability rules, does not check
conflicts, and does not produce a decision. Anything it cannot map goes into `metadata`
untouched.

## Request lifecycle

1. A request arrives — native form, native API, or an external source's webhook.
2. The relevant adapter verifies it (external sources: HMAC-SHA256 over the raw body) and
   translates it into a canonical `BookingIntentIn`.
3. `booking_service.submit_intent()` opens a `BEGIN IMMEDIATE` transaction, persists the
   intent, and loads the resource's availability policy and blackout periods.
4. The domain rule chain evaluates the request against the policy and existing bookings,
   returning a `Decision`.
5. On rejection, the slot search proposes up to three next-available alternatives.
6. On acceptance, a `booking` row is written — with a conflict re-check immediately before
   insert, so two simultaneous requests for the same slot cannot both succeed.
7. A `CalendarHandoff` is produced: an ICS file plus provider deeplinks.
8. Audit events are appended throughout. The audit log is append-only.

## Stack, and why

| Concern | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.12 | `zoneinfo` in the stdlib; timezone handling is the hard part here |
| Framework | FastAPI | Pydantic-native, so the API contract is generated from the schemas rather than written twice |
| Storage | SQLite (WAL) | A booking tool for one operator handles bookings per day, not per second. One file, no daemon, trivial backup |
| ORM | SQLAlchemy 2.0 (typed) | Keeps the SQLite → Postgres door open at near-zero cost |
| Migrations | Alembic | Schema changes should be reviewable from the first one |
| Templates | Jinja2 | Server-rendered forms, no build step, no `node_modules` to audit |
| ICS | `icalendar` | RFC 5545 line folding and escaping are fiddly and easy to get subtly wrong |
| Timezones | `zoneinfo` + `tzdata` | The `tzdata` package guarantees the database exists inside slim containers |
| Deeplinks | stdlib `urllib.parse` | About 30 lines; no dependency justified |
| Tests | pytest, httpx `ASGITransport`, `time-machine` | Real in-process API tests; frozen time for scheduling logic |
| Lint/format | ruff | One tool instead of black + isort + flake8 |
| Types | mypy, strict over `domain/` | Strict where the logic lives, pragmatic at the edges |
| Packaging | uv, Docker Compose | Reproducible installs; one command to self-host |

## Deliberately rejected

- **Postgres for the MVP.** A container, backups, and tuning is real operational cost for a
  workload measured in bookings per day. SQLite in WAL mode with a short `BEGIN IMMEDIATE`
  transaction around the decide-and-insert is sufficient and correct. See
  [ADR 0003](adr/0003-sqlite-for-mvp.md).
- **A JavaScript SPA.** A build toolchain and a second deployment artifact, to render two
  forms. It would make calon harder to self-host, which is the opposite of the goal.
- **A task queue (Celery, Redis, workers).** Nothing in the MVP is asynchronous.
- **Direct calendar-provider writes as the first milestone.** OAuth applications, consent
  screens, per-provider token storage and refresh, and vendor review — more work than the
  rest of the MVP combined, and the most likely thing to break while nobody is watching.
  ICS reaches every calendar with no credentials at all. See
  [ADR 0004](adr/0004-ics-first-calendar-handoff.md).
- **An admin UI in the MVP.** Rules live in `config/calon.toml`. No admin UI means no
  login, no sessions, no CSRF, and no password storage — a whole category of security work
  that a first release does not need.

## Concurrency and correctness

Two simultaneous requests for the same slot must not both be accepted. calon handles this
with a single `BEGIN IMMEDIATE` transaction that spans rule evaluation and insertion, plus
a conflict re-check immediately before the insert. SQLite serializes writers, so this is
sufficient without an advisory lock.

Every instant is stored in UTC, with the relevant IANA timezone carried alongside for
display. Naive datetimes are treated as a bug — ruff's `DTZ` rules are enabled to catch
them at lint time.

## Related documents

- [Domain model](domain-model.md) — schemas, tables, and decision codes
- [Calendar handoff](calendar-handoff.md) — ICS and deeplink output
- [External intake](external-intake.md) — the source adapter contract
- [Self-hosting](self-hosting.md) — deployment
- [Decision records](adr/) — why things are the way they are
