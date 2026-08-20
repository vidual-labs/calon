"""Answering "what is free between these two instants".

**Advisory only.** Nothing here holds, locks, or reserves anything, and the response
carries no token, expiry, or identifier that could be mistaken for a claim on a slot.
Anything returned is stale the moment it is computed; the authoritative answer is what
happens when a booking is actually submitted, inside the write transaction that re-checks
conflicts (ADR 0007).

The search itself is ``calon.domain.suggest_slots`` — the same code that proposes
alternatives on a rejection, with an explicit end bound instead of the policy horizon. One
implementation, because two answers to "what is free" would eventually disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from calon.calendars import CalendarProviderRegistry
from calon.domain import SlotSuggestion, is_valid_timezone, suggest_slots, to_utc
from calon.domain.rules import BookingRequest
from calon.services import repository

__all__ = [
    "MAX_RANGE_DAYS",
    "Availability",
    "InvalidRangeError",
    "UnknownResourceError",
    "find_availability",
]

#: The widest window one query may ask about. Each candidate slot costs a full rule-chain
#: evaluation, so an unbounded range is the one way this endpoint could become expensive
#: (ADR 0007). A month at a time is enough for any calendar view worth rendering.
MAX_RANGE_DAYS = 31

#: A ceiling on the number of slots returned, which :data:`MAX_RANGE_DAYS` already makes
#: unreachable for any sensible policy. It exists so that a pathological configuration —
#: a one-minute grid across a 24-hour window — cannot produce an unbounded response.
_MAX_SLOTS = 5_000

_MARGIN = timedelta(days=1)


class UnknownResourceError(LookupError):
    """No such bookable resource, or it is not accepting bookings."""


class InvalidRangeError(ValueError):
    """The window asked about is empty, backwards, or wider than :data:`MAX_RANGE_DAYS`."""


@dataclass(frozen=True, slots=True)
class Availability:
    """Free slots in a window, as of ``evaluated_at``."""

    resource_slug: str
    timezone: str
    range_start: datetime
    range_end: datetime
    duration_min: int
    evaluated_at: datetime
    slots: tuple[SlotSuggestion, ...]


def find_availability(
    session: Session,
    *,
    resource_slug: str,
    range_start: datetime,
    range_end: datetime,
    now: datetime,
    timezone: str | None = None,
    duration_min: int | None = None,
    calendar_registry: CalendarProviderRegistry | None = None,
) -> Availability:
    """Every slot of ``duration_min`` that is bookable inside the window.

    Slots must *finish* inside the window, and are expressed in ``timezone`` so the caller
    can render them without converting anything. Omit ``timezone`` to get them in the
    resource's own — which is what an operator-facing view wants, and what the booking form
    falls back to when the requester has not said where they are.

    ``calendar_registry`` (ADR 0009), when the resource has an enabled calendar provider,
    narrows the search to slots the provider does not report as busy. With no registered
    provider — the default for every instance that has not configured ``[calendars.*]`` —
    the search is byte-for-byte identical to the pre-phase-9 path (CLAUDE.md §2).
    """
    resource_row = repository.find_resource(session, resource_slug)
    if resource_row is None or not resource_row.is_active:
        raise UnknownResourceError(f"there is no bookable resource called {resource_slug!r}")

    policy = repository.load_policy(session, resource_row.id)
    resource = repository.to_domain_resource(resource_row)

    start_utc = to_utc(range_start)
    end_utc = to_utc(range_end)
    _validate_range(start_utc, end_utc)

    zone_name = timezone or policy.timezone
    if not is_valid_timezone(zone_name):
        raise InvalidRangeError(f"{zone_name!r} is not a recognised IANA timezone name")

    duration = duration_min if duration_min is not None else policy.default_duration_min
    if duration <= 0:
        raise InvalidRangeError("duration_min must be a positive number of minutes")

    window = (start_utc - _MARGIN, end_utc + _MARGIN)
    free_busy = (
        calendar_registry.free_busy(resource_slug, *window) if calendar_registry is not None else ()
    )
    slots = suggest_slots(
        BookingRequest(
            resource_slug=resource_slug,
            start=start_utc,
            timezone=zone_name,
            end=start_utc + timedelta(minutes=duration),
        ),
        resource=resource,
        policy=policy,
        now=now,
        blackouts=repository.load_blackouts(session, resource_row.id, *window),
        existing=repository.load_booked_spans(session, resource_row.id, *window),
        free_busy=free_busy,
        limit=_MAX_SLOTS,
        until=end_utc,
    )

    return Availability(
        resource_slug=resource_slug,
        timezone=zone_name,
        range_start=start_utc,
        range_end=end_utc,
        duration_min=duration,
        evaluated_at=now,
        slots=slots,
    )


def _validate_range(start_utc: datetime, end_utc: datetime) -> None:
    if end_utc <= start_utc:
        raise InvalidRangeError("`to` must be later than `from`")
    if end_utc - start_utc > timedelta(days=MAX_RANGE_DAYS):
        raise InvalidRangeError(f"the window asked about must be {MAX_RANGE_DAYS} days or less")
