"""Next-available slot search.

The search walks the ``slot_granularity_min`` grid forward from
``max(now + min_notice, requested_start)`` and returns the first few candidates that pass
the **complete** rule chain, stopping at the ``max_advance_days`` horizon.

Re-running the whole chain per candidate rather than a cheaper subset is deliberate: a
suggestion that turns out to sit inside a blackout, or on top of another booking's buffer,
is worse than no suggestion at all. The candidate generator only walks in-window times on
allowed weekdays, so the work stays small even at a 15-minute grid over a 60-day horizon.

This same search answers the availability query: "everything free between A and B" is this
walk with an explicit end bound instead of the policy horizon. Both callers share one
implementation because two answers to "what is free" would eventually disagree
(``docs/adr/0007-availability-is-an-advisory-read.md``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta

from calon.domain.availability import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    Resource,
    is_valid_timezone,
    to_utc,
    zone,
)
from calon.domain.decision import SlotSuggestion
from calon.domain.rules import BookingRequest, evaluate, resolve_end

#: How many alternatives a rejection carries. Three is enough to be useful and few enough
#: that a requester reads all of them.
MAX_SUGGESTIONS = 3


def suggest_slots(
    request: BookingRequest,
    *,
    resource: Resource,
    policy: AvailabilityPolicy,
    now: datetime,
    blackouts: Sequence[BlackoutPeriod] = (),
    existing: Sequence[BookedSpan] = (),
    limit: int = MAX_SUGGESTIONS,
    until: datetime | None = None,
) -> tuple[SlotSuggestion, ...]:
    """Find up to ``limit`` bookable slots at or after what was asked for.

    ``until`` bounds the search earlier than the policy's ``max_advance_days`` horizon,
    which is how the availability query asks for a specific window. It can only narrow the
    horizon, never extend it past what the policy allows. A slot must *finish* by
    ``until``, so a range query cannot return a slot that runs past the window asked about.

    Returns an empty tuple when the request itself is unusable (no timezone, no duration)
    or when nothing is free before the horizon. Suggestions are expressed in the
    requester's timezone.
    """
    if limit <= 0 or not is_valid_timezone(request.timezone):
        return ()

    start_utc = to_utc(request.start)
    duration = resolve_end(request, policy) - start_utc
    if duration <= timedelta(0):
        return ()

    now_utc = to_utc(now)
    origin = max(now_utc + policy.min_notice, start_utc)
    horizon = now_utc + timedelta(days=policy.max_advance_days)
    finish_by = to_utc(until) if until is not None else None
    if origin > horizon or (finish_by is not None and origin + duration > finish_by):
        return ()

    requester_tz = zone(request.timezone)
    found: list[SlotSuggestion] = []

    for candidate in _candidates(policy, duration, origin, horizon, finish_by):
        decision = evaluate(
            BookingRequest(
                resource_slug=request.resource_slug,
                start=candidate,
                timezone=request.timezone,
                end=candidate + duration,
            ),
            resource=resource,
            policy=policy,
            now=now_utc,
            blackouts=blackouts,
            existing=existing,
        )
        if decision.accepted:
            found.append(
                SlotSuggestion(
                    start=candidate.astimezone(requester_tz),
                    end=(candidate + duration).astimezone(requester_tz),
                    timezone=request.timezone,
                )
            )
            if len(found) == limit:
                break

    return tuple(found)


def _candidates(
    policy: AvailabilityPolicy,
    duration: timedelta,
    origin: datetime,
    horizon: datetime,
    finish_by: datetime | None,
) -> Iterator[datetime]:
    """Yield grid-aligned start instants inside the booking window, in order.

    The grid is anchored to each day's ``window_start`` and stepped in local wall-clock
    time, so a booking keeps its "quarter past the hour" alignment across a DST shift
    instead of drifting by an hour.
    """
    day = policy.local(origin).date()
    last_day = policy.local(min(horizon, finish_by) if finish_by else horizon).date()

    while day <= last_day:
        if day.weekday() in policy.allowed_weekdays:
            yield from _candidates_on(policy, day, duration, origin, horizon, finish_by)
        day += timedelta(days=1)


def _candidates_on(
    policy: AvailabilityPolicy,
    day: date,
    duration: timedelta,
    origin: datetime,
    horizon: datetime,
    finish_by: datetime | None,
) -> Iterator[datetime]:
    opens, closes = policy.window_bounds(day)
    if finish_by is not None:
        # A caller asking about a window gets slots that fit inside it, not slots that
        # start inside it and run past the end.
        closes = min(closes, finish_by)
    candidate = opens
    while candidate + duration <= closes:
        if candidate > horizon:
            return
        if candidate >= origin:
            yield candidate
        candidate += policy.granularity
