# 5. Isolate external lead sources behind a source adapter boundary

- **Status:** Accepted
- **Date:** 2026-08-15

## Context

calon must work on its own, with its own intake form, and must *also* be able to accept
booking requests from external systems later — OpenFlow was the motivating example, but it
is one example among many.

The failure mode to avoid is well known and easy to fall into: the first external
integration arrives, its payload shape is convenient, and it leaks inward. Provider fields
appear in core schemas, provider special cases appear in scheduling logic, and a
"source-agnostic" core becomes quietly shaped around one vendor. After that, the second
integration is much harder than the first, and the tool no longer stands alone.

## Decision

All intake — native and external alike — passes through a two-method adapter:

```python
class SourceAdapter(Protocol):
    slug: str

    def verify(self, headers: Mapping[str, str], body: bytes) -> None: ...
    def parse(self, payload: dict) -> BookingIntentIn: ...
```

Four rules give the boundary teeth:

1. **Adapters translate; adapters never decide.** An adapter may not read availability
   rules, check conflicts, or produce a `Decision`. Unmappable provider fields go into
   `metadata` untouched, rather than growing a column.
2. **Native intake is itself an adapter** (`intake/native.py`). There is exactly one
   downstream code path, so an external source cannot reach logic the native flow does not
   also exercise, and any bug in the shared path surfaces in the native tests immediately.
3. **One endpoint serves every source:** `POST /api/v1/intake/{source_slug}`. Registering a
   source is a config block plus one file under `intake/external/` — no change to `domain/`,
   `services/`, or `api/`. Needing to touch those is the signal that the boundary has been
   violated.
4. **Sources are disabled by default,** and CI runs the full suite with none configured, so
   a dependency on an external source cannot creep in unnoticed.

Verification uses HMAC-SHA256 over the raw request body with a per-source shared secret,
compared in constant time, with a timestamp window against replay. Idempotency is enforced
on `(source, idempotency_key)`; a replay returns the stored original response rather than
re-evaluating the rules — including when the original was a rejection, so a retry cannot
turn a stale rejection into an acceptance because the calendar has changed since.

**No provider-specific adapter ships in `0.1.0.`** The framework is proven with a synthetic
test source. Building a real adapter against a guessed payload is precisely how the generic
boundary would become vendor-shaped; the first one lands in `0.2.0`, against a genuine
payload sample.

## Consequences

- calon remains fully usable standalone, and that property is enforced by CI rather than by
  good intentions.
- Adding a provider is a small, contained, well-understood change.
- The scheduling core stays free of provider knowledge, and every source is subject to
  identical rules and audit behaviour.
- **Cost:** one translation layer for every source, including the native one, and canonical
  schemas that must occasionally be extended rather than bent. That indirection is the price
  of the boundary and is accepted deliberately.
- `metadata` will accumulate provider-specific data that calon does not interpret. This is
  intended — it is the pressure valve that keeps provider concepts out of the core.
