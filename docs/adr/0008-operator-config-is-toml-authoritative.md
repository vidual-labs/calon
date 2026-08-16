# 8. The configuration file is authoritative; the tables are a projection of it

- **Status:** Accepted
- **Date:** 2026-08-16

## Context

Two documents each claimed the operator's scheduling rules.
`config/calon.example.toml` says it "is the source of truth for scheduling rules", while
`docs/domain-model.md` gives `resource`, `availability_policy` and `blackout_period` real
tables with real columns. Until phase 2 nothing read either, so the contradiction cost
nothing. Persistence is where it has to be settled.

There is no admin UI in the MVP, and deliberately so: no admin UI means no login, no
sessions, no CSRF and no password storage. So nothing but the file was ever going to write
those rows. That narrows the question to *how* the file reaches them, not whether some
other writer competes with it.

Three shapes were possible:

1. **File only.** Read the TOML at startup, hand the values straight to the rule chain, and
   drop the three tables. Simplest, but bookings and audit events need a stable
   `resource_id` to point at, and the operator loses any way to see what the running
   instance actually believes.
2. **Tables authoritative, file as a seed.** Write the rows on first boot and let them
   diverge afterwards. This is the shape that grows an admin UI later — and it means an
   edited file silently does nothing, which is a bad surprise for the one interface the
   operator actually has.
3. **File authoritative, tables projected from it at every startup.**

## Decision

**`config/calon.toml` is the source of truth. The tables are a projection of it, refreshed
on every startup.**

Rules are read from the tables at request time, so there is one runtime path and the policy
table is not write-only. They are rewritten from the file every time the process starts, so
the file always wins.

Concretely, at startup:

- the resource is upserted by slug;
- its availability policy row is overwritten;
- its blackout periods are **replaced wholesale** — nothing references a blackout row, the
  list is short, and "exactly what the file says" is easier to reason about than a merge;
- any resource the file has stopped naming is **deactivated, not deleted**, so its bookings
  and audit trail stay readable while nothing new can be booked against it.

**Bookings are never touched.** A booking was accepted under the rules in force when it was
made, and tightening the rules afterwards must not retroactively unmake it. This is also
why a booking's buffered span is materialised on the row rather than recomputed from the
current buffers.

The projection is idempotent, so restarting is always a valid thing to do — which matters,
because restarting is the only way to apply a configuration change.

## Consequences

- Editing `config/calon.toml` and restarting is the whole configuration workflow, and it
  does what it looks like it does.
- A malformed or nonsensical file **fails startup** rather than being partially applied.
  An instance that cannot understand its rules must not accept bookings under guesses.
- The tables are readable with `sqlite3` and a human eye — weekdays as `"0,1,2,3,4"`,
  window times as `"09:00"` — so an operator can confirm what the instance believes
  without running calon.
- Editing the tables by hand is pointless: the next restart overwrites them. That is a
  feature, and it is the reason no `UPDATE` path exists anywhere in the code.
- If an admin UI is ever wanted, this ADR is what has to be superseded first. That is the
  right place for that argument to happen, rather than in whichever pull request first
  needs to write a rule at runtime.
