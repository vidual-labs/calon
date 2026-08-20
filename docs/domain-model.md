# Domain model

> Status: built as of phase 2 (native flow) and phase 5 (external intake, ADR 0005 / ADR
> 0012). The decision types, the rule chain, the Pydantic schemas and every table below
> exist. The fields `ics_uid`, `ics_sequence` (phase 3) and `idempotency_key`,
> `decision_json` (phase 5) are all now written: the first two on acceptance, the latter two
> on an external-intake submission. This document is the reference the implementation is built
> against, and must be kept current as schemas change (see `CLAUDE.md` §7).

Two conventions apply everywhere:

- **Every instant is stored in UTC**, in a column suffixed `_utc`, with the relevant IANA
  timezone string carried alongside. Naive datetimes are a bug.
- **Identifiers are sortable UUIDs** (UUIDv7-style) stored as strings.

## Canonical contracts

These Pydantic models in `src/calon/schemas.py` are calon's public contract. Changing one
is an API change, not a refactor.

### `BookingIntentIn`

What every source — native form, native API, or an external adapter — must produce. It is
the only input the scheduling core understands.

| Field | Type | Notes |
| --- | --- | --- |
| `resource_slug` | `str` | Which bookable resource |
| `start` | `datetime` | Timezone-aware. Required |
| `end` | `datetime \| None` | Omit to use the policy's `default_duration_min` |
| `timezone` | `str` | IANA name of the *requester's* timezone |
| `requester.name` | `str` | |
| `requester.email` | `str` | |
| `requester.phone` | `str \| None` | |
| `subject` | `str` | What the booking is for |
| `notes` | `str \| None` | Free text from the requester |
| `metadata` | `dict[str, Any]` | Opaque passthrough. **Never read by core logic** |
| `source_ref` | `str \| None` | The source's own identifier, used for idempotency |

`metadata` is the pressure valve: anything a provider sends that calon has no concept of
goes here untouched, rather than growing a column and a boundary violation.

The model rejects unknown top-level fields, a `start` or `end` without a UTC offset, and a
`timezone` that is not in the IANA database. All three are malformed rather than unbookable:
they are answered with `422` and never become an intent, which keeps the audit log a record
of real booking attempts rather than of typos. `DecisionCode.INVALID_INPUT` remains the
backstop for a caller that builds a `BookingRequest` directly.

### `BookingRequest`

The domain layer's own view of a request, in `src/calon/domain/rules.py`: `resource_slug`,
`start`, `timezone`, and an optional `end`. That is the whole of it.

The requester's name, the subject, the notes, and `metadata` are all absent, because none
of them can affect whether a slot is bookable. Keeping them out is the cheapest possible
enforcement of "`metadata` is never read by core logic" — the core cannot read what it was
never given. The service layer translates `BookingIntentIn` into a `BookingRequest` on the
way in, and persists the full intent separately.

### `Decision`

The structured accept/reject result.

| Field | Type | Notes |
| --- | --- | --- |
| `outcome` | `"accepted" \| "rejected"` | |
| `code` | `DecisionCode` | The **first** rule that failed |
| `reason` | `str` | Human-readable and safe to show a requester |
| `violations` | `list[Violation]` | **All** failures, not just the first |
| `evaluated_at` | `datetime` | |
| `suggestions` | `list[SlotSuggestion]` | Populated on rejection where possible |

Reporting the first failure as `code` keeps the outcome deterministic and easy to branch
on; reporting all of them in `violations` means a requester who picked a Sunday at 3am is
told both things at once instead of discovering them one at a time.

In the domain layer `violations` and `suggestions` are tuples rather than lists — a
decision is a value, and nothing downstream should be editing one after it has been
recorded. They serialize as JSON arrays either way.

`suggestions` is populated by the caller rather than by rule evaluation itself, via
`Decision.with_suggestions()`. This keeps `evaluate()` cheap and total: judging a request
never triggers a search. `calon.domain.decide()` composes the two for callers who want
both.

### `DecisionCode`

An ordered enum. The rule chain evaluates in exactly this order.

| # | Code | Meaning |
| --- | --- | --- |
| 1 | `INVALID_INPUT` | Malformed or nonsensical request |
| 2 | `RESOURCE_UNKNOWN` | No such resource, or it is inactive |
| 3 | `DURATION_NOT_ALLOWED` | Duration is zero, negative, or outside allowed bounds |
| 4 | `BELOW_MIN_NOTICE` | Starts sooner than `min_notice_min` |
| 5 | `BEYOND_MAX_ADVANCE` | Starts further out than `max_advance_days` |
| 6 | `WEEKDAY_NOT_ALLOWED` | Not one of `allowed_weekdays` |
| 7 | `OUTSIDE_BUSINESS_HOURS` | Starts or **ends** outside the daily window |
| 8 | `BLACKOUT_PERIOD` | Overlaps a blackout |
| 9 | `DAILY_LIMIT_REACHED` | `max_bookings_per_day` already met |
| 10 | `SLOT_CONFLICT` | Overlaps an existing booking's buffered span |
| 11 | `PROVIDER_CONFLICT` | Overlaps busy time on the resource's connected (provider) calendar |
| 12 | `ACCEPTED` | Passed every rule |

**Once shipped, these strings are public API.** They are never renamed, never repurposed,
and never have their meaning changed. A new constraint gets a new code.

The first three are **gating**: evaluation stops at the first of them, and the decision
carries that one violation alone. A request with a negative duration, or one naming a
resource that does not exist, is structurally unusable, and running the remaining rules
against it would report confident nonsense — a backwards booking would also be accused of
ending outside business hours. Codes 4 through 11 are all evaluated, and all their failures
are reported.

Gating rejections also carry no suggestions: there is no "next available" for a question
that could not be asked.

### `FreeBusySpan`

One busy interval reported by a connected calendar provider for a resource
(ADR 0009). A frozen value object with `starts_at_utc`, `ends_at_utc`, and an
optional `reason`.

The rule chain treats a provider-reported busy span **exactly like an own-booking
span** for conflict purposes — the only difference is the code it rejects with:
`PROVIDER_CONFLICT` rather than `SLOT_CONFLICT`, so a requester learns the clash
is with the resource's existing external calendar, not with another booking calon
made. The span is **not** buffered: buffers are a property of calon-authored
bookings, and a provider event is not a booking calon made.

### `SlotSuggestion`


`{start, end, timezone}`. The search walks the `slot_granularity_min` grid forward from
`max(now + min_notice, requested_start)` and returns the first **three** candidates that
pass the complete rule chain, stopping at the `max_advance_days` horizon.

`timezone` is the **requester's**, and `start` and `end` are expressed in it, so a
suggestion can be rendered without a further conversion. The grid is anchored to each day's
`window_start` and stepped in local wall-clock time, so slots keep their alignment across a
DST transition instead of drifting by an hour.

Each candidate is re-checked against the *complete* chain rather than a cheaper subset. A
suggestion that turns out to sit inside a blackout, or on top of another booking's buffer,
is worse than offering nothing.

## The HTTP contract

| Endpoint | Answers with |
| --- | --- |
| `POST /api/v1/bookings` | `BookingResponse` — `201` when a booking was created, `200` when the request was judged and rejected |
| `GET /api/v1/availability` | `AvailabilityResponse` |
| `GET /healthz` | `{"status", "version"}` |

A rejection is `200`, not a client error: the request was well-formed, the rules were
applied, and the answer is on the record. `4xx` is reserved for requests calon could not
judge at all — `422` for a malformed payload or an impossible window, `404` for a resource
that is not there.

### `BookingResponse`

`intent_id` · `status` · `decision` (a `Decision`) · `booking` (a `BookingOut`, or null)

`intent_id` is always present, because a rejection is a recorded outcome rather than a
request that never happened. `BookingOut` is `{id, start, end, timezone, status}`, with
`start` and `end` in the requester's timezone. Buffers never appear: they widen the span
used for conflict detection and are none of the requester's business.

### `AvailabilityResponse`

`resource_slug` · `timezone` · `from` · `to` · `duration_min` · `evaluated_at` · `slots`

Query parameters are `resource_slug`, `from`, `to`, and optionally `timezone` (defaults to
the resource's) and `duration_min` (defaults to the policy's). `from` and `to` must carry a
UTC offset, and the window may not exceed **31 days** — each candidate slot costs a full
rule-chain evaluation.

Slots must *finish* by `to`, so a range query never returns a slot that runs past the
window asked about.

**The response deliberately carries nothing that reads like a claim on a slot** — no token,
no expiry, no identifier. Availability is advisory (ADR 0007); a caller that treats a query
as a reservation will still lose the race, correctly, at submit time.

## Tables

### `resource`

The bookable thing — a person, a room, a service.

`id` · `slug` · `name` · `timezone` · `is_active` · `created_at_utc`

The MVP seeds exactly one row, but the foreign key exists everywhere, so supporting several
later is a data change rather than a refactor. **This is not multi-tenancy:** there are no
accounts and no isolation boundary between resources.

### `availability_policy`

One row per resource.

`resource_id` · `timezone` · `allowed_weekdays` · `window_start` · `window_end` ·
`default_duration_min` · `slot_granularity_min` · `min_notice_min` · `max_advance_days` ·
`buffer_before_min` · `buffer_after_min` · `max_bookings_per_day` (nullable) ·
`updated_at_utc`

`window_start` and `window_end` are local `HH:MM` in the resource's timezone. A booking must
start *and end* inside the window; a request that would overrun it is rejected rather than
truncated.

### `blackout_period`

`id` · `resource_id` · `starts_at_utc` · `ends_at_utc` · `reason`

Whole-day blackouts are stored as local-midnight-to-midnight converted to UTC. One shape
for every blackout means no special-casing in the rule that checks them.

### `booking_intent`

The canonical record of what was asked for. Immutable once written — including rejected
requests, which are exactly the ones you want to look at later.

`id` · `resource_id` · `source` · `source_ref` · `idempotency_key` ·
`requested_start_utc` · `requested_end_utc` · `requester_timezone` · `requester_name` ·
`requester_email` · `requester_phone` · `subject` · `notes` · `metadata_json` ·
`raw_payload_json` · `received_at_utc` · `status` · `decision_code` · `decision_reason` ·
`decision_json` · `decided_at_utc`

`status` is `pending`, `accepted`, or `rejected`. Unique index on
`(source, idempotency_key)` where the key is not null.

That index ships with the first migration even though nothing uses it until the external
intake framework lands — adding a unique constraint later, against live data that may
already violate it, is a far worse migration than adding it up front.

`decision_json` (added by migration 0002) stores the complete structured decision —
`code`, `reason`, and `suggestions` — at the instant it is produced, for external-intake
submissions. On an idempotent replay the route returns **this** value (re-validated into the
public `DecisionOut` shape) rather than re-evaluating the rules, so a retry cannot change a
stored outcome. Native submissions always write `NULL` here, because the native form has no
replay semantics; their decision stays in the human-readable `decision_code` /
`decision_reason` columns. See [ADR 0012](adr/0012-external-intake-final.md).

### `booking`

Written only on acceptance.

`id` · `intent_id` (unique) · `resource_id` · `start_utc` · `end_utc` · `block_start_utc` ·
`block_end_utc` · `status` · `ics_uid` · `ics_sequence` · `created_at_utc` ·
`cancelled_at_utc`

`block_start_utc` and `block_end_utc` are `start − buffer_before` and `end + buffer_after`.
They are **materialized** so conflict detection is one indexed range query:

```sql
SELECT 1 FROM booking
WHERE resource_id = :rid
  AND status = 'confirmed'
  AND block_start_utc < :new_block_end
  AND block_end_utc   > :new_block_start;
```

Buffers never appear in the calendar event itself. They only widen the span used for
conflict detection, so back-to-back bookings cannot be squeezed together.

### `audit_event`

Append-only. Never updated, never deleted.

`seq` · `id` · `at_utc` · `actor` · `event_type` · `intent_id` · `booking_id` ·
`payload_json`

Alone among calon's tables this one carries an integer `seq` as well as a UUID, and it is
the primary key. The events of a single decision are written inside one transaction and
share one timestamp by design — `now` is injected once and used throughout — so neither
`at_utc` nor a UUIDv7 minted in the same millisecond can order them. `seq` is what makes
the log readable in the order things actually happened; `id` remains its stable identifier.

`actor` is `system`, `operator`, or `source:<slug>`.

Event types: `intent.received`, `intent.normalized`, `intent.rejected`, `intent.accepted`,
`booking.created`, `booking.cancelled`, `handoff.generated`, `intake.replayed`,
`intake.rejected_signature`.

## Concurrency

Rule evaluation and insertion happen inside a single `BEGIN IMMEDIATE` transaction, with a
conflict re-check immediately before the insert. SQLite serializes writers, so two
simultaneous requests for the same slot cannot both be accepted.
