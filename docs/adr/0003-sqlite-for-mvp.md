# 3. Use SQLite as the datastore

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

calon needs to persist bookable resources, availability rules, blackout periods, booking
intents, bookings, and an append-only audit log.

The workload is small and known: a single operator's bookings, measured in requests per day
rather than per second. The dominant design goal is that someone can self-host calon on a
cheap server without becoming a database administrator.

The realistic options were SQLite and PostgreSQL.

## Decision

**SQLite, in WAL mode**, accessed through SQLAlchemy 2.0 with Alembic migrations.

Correctness under concurrent booking requests is handled explicitly rather than assumed:
rule evaluation and the booking insert happen inside a single `BEGIN IMMEDIATE` transaction,
with a conflict re-check immediately before the insert. SQLite serialises writers, so two
simultaneous requests for the same slot cannot both be accepted.

PostgreSQL was rejected for the MVP, not on capability but on operational cost. It adds a
second container, a connection string, backup tooling, and tuning — real, permanent overhead
imposed on every self-hoster, to serve a workload SQLite handles without effort.

Using SQLAlchemy rather than raw `sqlite3` is a deliberate hedge: it keeps the migration
path open at close to zero cost, so the decision is reversible if calon ever outgrows it.

## Consequences

- Deployment is one container and one file. Backup is copying that file (with
  `sqlite3 .backup`, not `cp`, because of WAL).
- No connection pool, no separate service to monitor, no network hop.
- **Writes are serialised.** This is a non-issue at the target scale and would become one
  only under sustained concurrent write load, which is not what a booking intake tool does.
- SQLite's type affinity is weaker than Postgres's. Datetimes are stored as UTC strings and
  the domain layer is responsible for them being timezone-aware — enforced by ruff's `DTZ`
  rules and by mypy strictness over `domain/`.
- Migrating to Postgres later means changing the database URL and testing the Alembic
  migrations against it. Because no SQLite-specific SQL is used in the ORM layer, this is a
  contained change rather than a rewrite. If it happens, it gets its own ADR.
