# 12. Finalize the external-intake endpoint, HMAC authentication, and stored-decision replay

- **Status:** Accepted
- **Date:** 2026-08-19
- **Supersedes:** no part of [ADR 0005](0005-source-adapter-boundary.md); this ADR records the
  concrete decisions ADR 0005 left open — the exact endpoint path, the authentication scheme,
  the replay-window default, the idempotency mechanism, and boot-time source registration.

## Context

ADR 0005 fixed the *boundary*: every intake path is an adapter, all paths converge on
`booking_service.submit_intent()`, one endpoint serves every source, sources are disabled by
default, and authentication is "HMAC-SHA256 with a timestamp window." It deliberately deferred
the concrete decisions to avoid locking the boundary to an implementation detail. This ADR is
that implementation decision, made now that the framework has shipped against a synthetic test
source.

The two non-obvious choices this ADR forces are:

1. **What a replay returns.** ADR 0005 says a replay "returns the stored original response
   rather than re-evaluating the rules." Storing the *response* means serializing the decision
   (including the human-readable `reason` and any `suggestions`) at the instant of the original
   evaluation. Re-deriving it on replay is exactly the failure ADR 0005 is protecting against:
   the calendar may have changed since, and a re-evaluation would turn a stored rejection into
   an acceptance (or vice versa) because the inputs moved. The decision must be frozen into the
   row.

2. **When a source becomes "configured."** The set of enabled sources must be decided once, at
   boot, from the operator configuration. If it were re-derived per request, the set of slugs
   an unauthenticated caller could discover would be a live probe oracle, violating the
   standalone-first boundary and ADR 0005 rule 4.

## Decision

### The endpoint and its path

The endpoint is `POST /api/v1/{source_slug}`. The source slug is a path segment, not a
sub-path: the router is mounted at `/api/v1` (see `src/calon/api/v1/__init__.py`), so the full
route is `/api/v1/<slug>`. There is intentionally **no** `/intake/` segment between `/api/v1`
and the slug — the path is versioned, and the slug is the only thing that varies. (ADR 0005 and
`docs/external-intake.md` show the path as `POST /api/v1/intake/{source_slug}`; that `/intake/`
segment is dropped in this ADR so that the path is symmetric with the other versioned routes.
`docs/external-intake.md` is updated accordingly.)

The flow is exactly ADR 0005's, and nothing in it reads the wall clock or touches scheduling:

1. Look up the adapter in the boot-built registry. Unknown or unenabled slug → `404` with a
   constant body (`{"detail": "source not configured on this instance"}`), logged
   `intake.404`. The body is identical for a typo and for a configured-but-disabled slug, so a
   caller cannot probe which slugs exist.
2. `adapter.verify(headers, raw_body, now=...)` → on failure `401`
   (`{"detail": "unauthorized request"}`), logged `intake.rejected_signature`. Nothing is
   written.
3. `adapter.parse(raw_body)` → on failure `400` with the parse reason, logged
   `intake.rejected_parse`. Nothing is written.
4. Resolve the idempotency key (`Idempotency-Key` header, falling back to the
   adapter-supplied `source_ref`) and look up the stored row for `(source, idempotency_key)`.
   If a row exists, return the stored decision (step 5's shape, but from the stored row) with
   `200` and `Idempotent-Replay: true`, logged `intake.replayed`.
5. Otherwise run `booking_service.submit_intent(...)` fresh and return `201` with the decision
   and (if accepted) the booking.

### Authentication: HMAC-SHA256

The verifier is stdlib-only (`hmac` + `hashlib`), compared with `hmac.compare_digest`.

- **Signed payload:** the raw body bytes, prefixed by the timestamp, as the string
  `"<timestamp>.<raw body>"`. `sha256=hex(hmac_sha256(secret, "<timestamp>.<raw body>"))`.
- **Headers** (case-insensitive lookup per RFC 7230; the route lowercases the incoming header
  names before verification):
  - `X-Calon-Timestamp`: integer Unix seconds.
  - `X-Calon-Signature`: `sha256=<hex digest>`.
- **Timestamp window:** a configurable per-source `timestamp_window_seconds` (default **300**,
  five minutes). A timestamp outside the window is rejected *alongside* the signature check so a
  malformed or absent timestamp never silently counts as "now."
- **Rejection body:** all authentication failures (bad signature, absent header, non-integer
  timestamp, out-of-window timestamp, no configured secret) return the same constant body and
  status. The per-reason detail is kept in the log (`intake.rejected_signature` with the
  adapter's message), not in the response, so the response is not a reason oracle.

The signature covers the **raw bytes before JSON parsing**. Re-serializing first would change
the digest, so the raw body is read from the wire, signed, and only then decoded.

### Idempotency: stored-decision replay

The `idempotency_key` column on the intent row is part of the stored row and the replay returns
the **stored decision**, not a re-evaluation.

- **Serialization at write time.** `booking_service` serializes the structured
  `Decision` (`code`, `reason`, `suggestions`, …) to the `decision_json` column
  (migration 0002) the instant it is produced. The row is the source of truth for that
  request's outcome.
- **Replay.** A repeat request with the same `(source, idempotency_key)` reads the row in a read
  transaction and returns `decision_json` re-validated into the public `DecisionOut` shape, with
  the booking (if the stored status is `accepted`) and `200 / Idempotent-Replay: true`. The
  rules are not re-run. Because the `reason` and `suggestions` are serialized with the decision,
  a stored rejection replays with its original reason and suggestions intact — a retry can not
  change the outcome even if the calendar has shifted.
- **Concurrency.** Two simultaneous first requests with the same key race on the unique
  `(source, idempotency_key)` constraint. The loser catches `IntegrityError`, re-reads the
  winner's committed row on the same session (whose transaction has advanced), and returns the
  stored decision as a replay rather than a `500`. The only case that re-raises (surfacing a
  `500`) is the rare path where the winner's row is committed but invisible to the loser's
  snapshot; the client's retry then finds the row.
- **Key resolution.** `Idempotency-Key` header first, else the adapter's `source_ref`. A source
  that sends neither has no idempotency guarantee and is not an error — the row is keyed by the
  resolved value, and an absent key simply means the source has not opted into replay safety.

### Boot-time source registration

`main.py` builds a `SourceRegistry` exactly once at startup, from the `[sources.<slug>]` tables
and the adapter modules under `src/calon/intake/external/`. The registry is exposed on
`app.state.source_registry` and injected per request through `get_source_registry`. An enabled
slug with no matching adapter module is a boot-time `RuntimeError` (fail loudly), not a
per-request `500`. A disabled or unconfigured slug simply is not in the registry and therefore
receives the constant `404`. CI runs the full suite with no sources configured, so a dependency
on an external source cannot creep in (ADR 0005 rule 4).

## Consequences

- The external-intake surface is now fully specified and testable end-to-end against a synthetic
  source; `tests/api/test_intake.py` exercises accept, stored-booking persistence, idempotent
  replay (accept and reject), unknown-slug `404`, bad-signature / missing-header / out-of-window
  `401`, and the no-row-on-`401` guarantee.
- The decision codes are unchanged: authentication and parse failures are HTTP-level
  (`401`/`400`) and are deliberately *not* `DecisionCode` values, so the rule engine's public
  code set is not polluted with transport concerns.
- The `decision_json` column is an additive, nullable write; existing installations that run
  without any external source never populate it, and the native path is unaffected.
- The `/intake/` path segment documented in ADR 0005 is dropped. Any integration already built
  against the draft path must adjust; this is called out here because ADRs are immutable and a
  reader skimming ADR 0005 would otherwise see a path the code does not implement.
