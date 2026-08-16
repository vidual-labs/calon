# External intake

> Status: planned (phase 5). The one piece that exists is the native adapter
> (`intake/native.py`) and the single downstream path it feeds — which is the part this
> document is really about.

## What this is for

calon is **standalone first**. It is fully usable with nothing in this document configured,
and CI enforces that by running the entire test suite with no sources enabled.

External intake exists so that a system which already collects leads — OpenFlow is one
example among many — can submit booking requests to calon over HTTP. It is strictly
additive.

## The adapter contract

```python
class SourceAdapter(Protocol):
    slug: str

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        """Raise if the request is not authentic."""

    def parse(self, payload: dict) -> BookingIntentIn:
        """Map a provider payload onto calon's canonical booking intent."""
```

Two methods, and a hard rule: **adapters translate, adapters never decide.**

An adapter may not read availability rules, may not check for conflicts, and may not
produce a `Decision`. Anything in the payload that calon has no concept of goes into
`metadata` untouched, rather than growing a column. If you find yourself wanting scheduling
logic inside an adapter, the rule belongs in the domain layer where every source benefits
from it.

## One path, not two

Native intake is itself implemented as an adapter (`intake/native.py`), and already is
today. A native request arrives in the canonical shape and so has almost nothing to
translate — which is exactly the point. The adapter exists so that there is no shortcut past
the adapter layer for scheduling logic to appear in later.

```
native form ─┐
             ├─► SourceAdapter.parse ─► BookingIntentIn ─► booking_service.submit_intent()
OpenFlow ────┤                                                       │
other src ───┘                            rules → Decision → Booking → CalendarHandoff → audit
```

This is deliberate. If external intake had its own path, that path would drift — it would
miss a rule, skip an audit event, or handle a timezone differently, and nobody would notice
until a booking went wrong. Because native intake runs the same code, any bug in the shared
path shows up in the native tests immediately, and the scheduling core has no knowledge of
any provider.

## The endpoint

```
POST /api/v1/intake/{source_slug}
```

One endpoint serves every registered source. The flow is: look up the adapter by slug →
`verify()` → `parse()` → `booking_service.submit_intent()`.

### Authentication

HMAC-SHA256 over the raw request body, using a per-source shared secret, compared in
constant time, with a timestamp window to blunt replay attacks.

```
X-Calon-Timestamp: 1788000000
X-Calon-Signature: sha256=<hex digest of "<timestamp>.<raw body>">
```

Requests with a bad signature, a missing header, or a timestamp outside the window are
rejected with `401` and logged as `intake.rejected_signature`. The signature is computed
over the **raw bytes**, before JSON parsing — re-serializing first would produce a
different digest.

### Idempotency

Networks retry. A retried webhook must not create a second booking.

calon takes the idempotency key from the `Idempotency-Key` header, falling back to the
adapter-supplied `source_ref`, and enforces uniqueness on `(source, idempotency_key)`.

A replay **returns the stored original response** with `200` and `Idempotent-Replay: true`,
and logs `intake.replayed`. It does not re-evaluate the rules, and it does not create a
second booking — including when the original request was rejected, so a retry cannot turn
yesterday's rejection into today's acceptance because the calendar has since changed.

## Registering a source

Configuration, not core changes. In `config/calon.toml`:

```toml
[sources.openflow]
enabled = true
secret = "generate-with-openssl-rand-hex-32"
resource_slug = "default"
```

Adding a brand-new provider means one new file under `src/calon/intake/external/` and one
config block. Nothing in `domain/` or `services/` changes — if a new source requires
touching those, the adapter boundary has been violated.

Sources are **disabled by default**. A source with no config block does not exist as far as
calon is concerned, and its endpoint returns `404`.

## About OpenFlow specifically

OpenFlow is an example of an external source, not the centre of this architecture and not a
special case in the code. It gets the same two-method adapter as anything else.

No provider-specific adapter ships in `0.1.0`. Writing one against a guessed payload shape
is how a "generic" boundary quietly becomes shaped around a single vendor. The framework is
proven in `0.1.0` using a synthetic test source; the first real adapter lands in `0.2.0`,
once there is a genuine payload sample to build against.

## The other direction: reading availability

Everything above is *inbound* — a source pushing a booking request in. A form builder like
OpenFlow that wants to ask "which times are actually free" before it ever submits anything
goes the other way, and needs no adapter and no config at all: `GET /api/v1/availability`
(`docs/domain-model.md#the-http-contract`) is already a public, unauthenticated, read-only
endpoint precisely so any caller can read it. It costs nothing to expose — see
`docs/self-hosting.md`'s note that it discloses free/busy times only, which a public booking
form already makes inferable.

```
GET /api/v1/availability?resource_slug=default&from=2026-09-01T00:00:00%2B02:00&to=2026-09-14T00:00:00%2B02:00
```

`resource_slug` is the "which calendar" a caller like OpenFlow configures per form field.
The response carries no CORS headers, so a browser-side caller cannot reach it directly
cross-origin; the consuming application (e.g. OpenFlow's own backend) should fetch it
server-side and hand the slots to its own frontend. That also keeps calon reachable only from
where its operator intends, rather than from the public internet at large — see the reverse
proxy note in `docs/self-hosting.md` if you want to restrict it further.

Nothing in this response reserves anything (ADR 0007): a caller that shows a slot as free is
still subject to losing it to another requester between reading availability and submitting
a booking, exactly like the native booking form is.

## Writing an adapter

1. Create `src/calon/intake/external/<provider>.py`.
2. Implement `slug`, `verify()`, and `parse()`. Reuse the default HMAC verifier unless the
   provider signs differently.
3. Add a payload fixture under `tests/fixtures/` and a test asserting that it maps onto the
   expected `BookingIntentIn` — including the awkward fields: missing end times, unusual
   timezone spellings, and anything that should land in `metadata`.
4. Document the provider's payload shape and any quirks here.
5. Add the config block to `config/calon.example.toml`, commented out.

You should not need to modify anything in `domain/`, `services/`, or `api/`. If you do,
stop and open an issue — that is a signal about the boundary, not about your adapter.
