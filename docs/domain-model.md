# Domain model

> Status: planned. This is the reference the implementation will be built against, and it
> must be kept current as schemas change (see `CLAUDE.md` §7).

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
| 11 | `ACCEPTED` | Passed every rule |

**Once shipped, these strings are public API.** They are never renamed, never repurposed,
and never have their meaning changed. A new constraint gets a new code.

### `SlotSuggestion`

`{start, end, timezone}`. The search walks the `slot_granularity_min` grid forward from
`max(now + min_notice, requested_start)` and returns the first **three** candidates that
pass the complete rule chain, stopping at the `max_advance_days` horizon.

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
`decided_at_utc`

`status` is `pending`, `accepted`, or `rejected`. Unique index on
`(source, idempotency_key)` where the key is not null.

That index ships with the first migration even though nothing uses it until the external
intake framework lands — adding a unique constraint later, against live data that may
already violate it, is a far worse migration than adding it up front.

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

`id` · `at_utc` · `actor` · `event_type` · `intent_id` · `booking_id` · `payload_json`

`actor` is `system`, `operator`, or `source:<slug>`.

Event types: `intent.received`, `intent.normalized`, `intent.rejected`, `intent.accepted`,
`booking.created`, `booking.cancelled`, `handoff.generated`, `intake.replayed`,
`intake.rejected_signature`.

## Concurrency

Rule evaluation and insertion happen inside a single `BEGIN IMMEDIATE` transaction, with a
conflict re-check immediately before the insert. SQLite serializes writers, so two
simultaneous requests for the same slot cannot both be accepted.
