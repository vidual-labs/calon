# calon

**A lean, self-hostable booking intake tool.** calon captures booking requests, applies
operator-defined scheduling rules, checks availability, and produces a generic calendar
handoff that works with any major calendar.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

> **Status: pre-alpha.** Phase 2 is complete — calon now runs. Booking requests can be
> submitted over HTTP, judged against your rules, and stored; availability can be queried;
> every decision is audited. There is still no release: an accepted booking produces a
> decision but no calendar handoff, and there is no booking form yet. Do not use this in
> production. See the [roadmap](#roadmap).

---

## What calon is

- A **booking-request intake endpoint** — a native web form plus a clean HTTP API.
- A **deterministic scheduling-rules evaluator** — weekdays, hours, timezones, minimum
  notice, maximum advance window, blackout dates, durations, buffers, conflicts.
- A **generic calendar handoff producer** — an ICS file that any calendar accepts, plus
  one-click deeplinks for Google Calendar and Outlook / Microsoft 365.
- An **auditable decision log** — every intake and every accept/reject decision is recorded.
- A **small service you can run on one cheap VPS**, with SQLite and no external dependencies.

## What calon is not

- ❌ **Not a CRM.** No contacts, pipelines, deals, or campaigns.
- ❌ **Not a restaurant or hotel reservation suite.** No tables, covers, room inventory,
  or rate plans.
- ❌ **Not a generic automation platform.** No workflow builder, no trigger/action engine.
- ❌ **Not an OpenFlow plugin,** and not dependent on OpenFlow or any other lead source.
- ❌ **Not a calendar sync engine.** calon does not read or write your provider calendars.

calon is deliberately small. If a feature request implies one of the above, it belongs in a
different tool that talks to calon over its API.

## MVP scope

The first release (`0.1.0`) does exactly this, end to end:

- [x] Accept a booking request from calon's own native intake flow
- [x] Normalize any request into one canonical **booking intent**
- [x] Apply booking rules and operator-defined time windows
- [x] Determine whether a requested slot is valid
- [x] Publish **which slots are free**, so a requester or an external system can pick one
      rather than guess
- [x] Return a structured **accept / reject decision**, with next-available suggestions
- [ ] Produce a generic **add-to-calendar** result that works across calendar ecosystems
- [ ] Expose a source-agnostic intake boundary that external systems can plug into
- [x] Keep a minimal audit trail of every decision

**Explicit non-goals for the MVP:** no CRM, no workflow automation engine, no billing or
payments, no deep calendar-provider write integrations, no dependency on external lead
sources, no multi-tenancy, no AI features.

## How it works

```
  intake (native form | HTTP API | external source)
        │
        ▼
  source adapter ──────────► normalizes to ──────────► canonical BookingIntent
                                                              │
                                                              ▼
                                                    ordered rule chain
                                          (notice · advance · weekday · hours ·
                                           blackout · daily limit · conflict)
                                                              │
                                                              ▼
                                                    Decision (accept | reject
                                                     + reason + suggestions)
                                                              │
                                              ┌───────────────┴───────────────┐
                                              ▼                               ▼
                                     CalendarHandoff                     audit event
                                (ICS file + Google / Outlook            (append-only)
                                          deeplinks)
```

The rule chain is pure and source-agnostic: a request from the native form and a request
from an external system travel the exact same path.

## Quick start

> ⚠️ **Pre-alpha.** The API below works today. Docker packaging arrives in phase 6, and the
> booking form at `/book` in phase 4 — neither exists yet.

```bash
git clone https://github.com/vidual-labs/calon.git
cd calon
cp config/calon.example.toml config/calon.toml   # set your hours, timezone, and rules
cp .env.example .env

make install    # uv sync
make dev        # uvicorn with reload
make check      # ruff + mypy + pytest
```

Both copy steps are optional: with no `.env` and no `config/calon.toml`, calon starts on
the defaults `config/calon.example.toml` documents. The database is created and migrated on
first start.

Then open <http://localhost:8000/docs> for the generated OpenAPI reference.

### The API

```
POST /api/v1/bookings      submit a booking request
GET  /api/v1/availability  list free slots in a window
GET  /healthz              liveness
```

A booking request is answered with a decision either way. `201` means a booking exists;
`200` with `"outcome": "rejected"` means the request was judged and refused, with every
rule it broke and up to three alternatives in the requester's own timezone:

```bash
curl -X POST localhost:8000/api/v1/bookings -H 'content-type: application/json' -d '{
  "resource_slug": "default",
  "start": "2026-09-02T10:00:00+02:00",
  "timezone": "Europe/Berlin",
  "requester": {"name": "Ada Lovelace", "email": "ada@example.com"},
  "subject": "Initial consultation"
}'
```

Availability answers a window of up to 31 days:

```bash
curl -G localhost:8000/api/v1/availability \
  --data-urlencode resource_slug=default \
  --data-urlencode from=2026-09-02T09:00:00+02:00 \
  --data-urlencode to=2026-09-02T17:00:00+02:00
```

**Availability is advisory.** It holds nothing and reserves nothing — a slot it lists can
be taken by someone else a moment later, and the authoritative answer is what happens when
a booking is actually submitted.

## Architecture summary

| Concern | Choice |
| --- | --- |
| Language | Python 3.12 |
| Web framework | FastAPI (OpenAPI schema generated, not hand-written) |
| Storage | SQLite in WAL mode, via SQLAlchemy 2.0 + Alembic |
| Templates | Jinja2, server-rendered, no JavaScript and no build step |
| Calendar | `icalendar` for RFC 5545 output; hand-rolled provider deeplinks |
| Timezones | stdlib `zoneinfo` + the `tzdata` package |
| Tests | pytest + httpx `ASGITransport` + `time-machine` |
| Lint / types | ruff (lint + format), mypy (strict over the domain layer) |
| Packaging | uv, Docker, Docker Compose |

**The core architectural rule:** `src/calon/domain/` is pure. No I/O, no ORM, no framework
imports, no reading the wall clock. All scheduling logic lives there and is unit-testable
without fixtures. Everything else is an edge that adapts to it.

See [`docs/architecture.md`](docs/architecture.md) and the decision records in
[`docs/adr/`](docs/adr/).

## Calendar compatibility

calon starts with **universal compatibility rather than deep integration**:

1. **ICS / iCalendar is the baseline.** A downloadable `.ics` file works with Google
   Calendar, Outlook, Microsoft 365, Apple Calendar, Thunderbird, Fastmail, and everything
   else — with zero credentials and zero OAuth setup.
2. **Provider deeplinks are a convenience layer.** One-click "add to Google Calendar" and
   "add to Outlook" links, offered alongside the ICS file and never instead of it.

calon does **not** write to your calendar provider in the MVP. Direct provider writes
require OAuth applications, consent screens, token storage and refresh, and vendor review —
more work than the entire rest of the MVP, and the most likely thing to break unattended.
They are deferred to a post-1.0, opt-in integration.

See [`docs/calendar-handoff.md`](docs/calendar-handoff.md).

## External intake

calon is **standalone first**. It is fully usable with zero external services configured.

External systems — OpenFlow is one example among many — can submit booking requests later
via a signed webhook. A small **source adapter** translates the provider's payload into
calon's canonical booking intent, and from that point the request is indistinguishable from
a native one. Adapters translate; adapters never decide. The scheduling core has no
knowledge of any provider.

See [`docs/external-intake.md`](docs/external-intake.md).

## Roadmap

| Phase | Deliverable | Version | Status |
| --- | --- | --- | --- |
| 0 | Repository foundation, policy, and decision records | — | done |
| 1 | Pure domain core: rule chain, decisions, slot search | — | done |
| 2 | Persistence, audit log, native intake API, availability query | — | done |
| 3 | Calendar handoff: ICS export and provider deeplinks | — | next |
| 4 | Minimal server-rendered booking UI | — | |
| 5 | External intake framework: adapters, HMAC, idempotency | — | |
| 6 | Docker packaging, self-hosting docs, **first release** | `0.1.0` | |
| 7 | First real provider adapter, once a genuine payload exists | `0.2.0` | |

Post-`0.2.0` candidates: an operator HTML view, requester-facing cancel and reschedule
links, and opt-in direct calendar writes behind a `CalendarWriter` interface.

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) first, and
**open an issue before starting a large pull request** — calon's scope is deliberately
narrow, and it is better to agree that a change fits before you write it.

Security issues should follow [`SECURITY.md`](SECURITY.md) rather than the public tracker.

## License

Copyright (C) 2026 Vidual Labs.

calon is free software, licensed under the **GNU Affero General Public License, version 3
or later** ([`LICENSE`](LICENSE)).

The AGPL is chosen deliberately because calon is a network service. Its section 13 extends
copyleft to users who interact with the software **over a network**, so anyone who runs a
modified calon as a hosted service must offer those users the modified source. Running
calon unmodified for your own bookings triggers no obligation, and because calon is a
standalone service with an HTTP boundary, integrating with it from separate proprietary
software does not make that software a derivative work. The reasoning is recorded in
[`docs/adr/0002-license-agpl-3-0.md`](docs/adr/0002-license-agpl-3-0.md).
