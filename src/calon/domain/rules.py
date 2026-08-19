"""The ordered rule chain: a booking request in, a ``Decision`` out.

This module is the heart of calon and deliberately the dullest code in it — plain
functions over plain values, no I/O, no clock. ``now`` is injected by the caller, which is
what makes every awkward case (a DST transition, a request landing exactly on the window
edge, a buffer colliding with the next booking) a single function call with no fixtures.

Rules run in ``DecisionCode`` declaration order. The first three are *gating*: a request
that names an unknown resource or a negative duration is structurally unusable, and
evaluating the rest would report confident nonsense. Everything after that is collected,
so a requester who picked a Sunday at 3am is told both things at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from calon.domain.availability import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    FreeBusySpan,
    Resource,
    is_aware,
    is_valid_timezone,
    to_utc,
)
from calon.domain.decision import Decision, DecisionCode, Outcome, Violation

ACCEPTED_REASON = "The requested slot is available."


@dataclass(frozen=True, slots=True)
class BookingRequest:
    """What the scheduling core needs to know about a request, and nothing more.

    Notably absent: the requester's name, the subject, the notes, and ``metadata``. None of
    them can affect whether a slot is bookable, so the domain never sees them — which is
    the cheapest possible enforcement of "``metadata`` is never read by core logic".
    """

    resource_slug: str
    start: datetime
    timezone: str
    end: datetime | None = None


def evaluate(
    request: BookingRequest,
    *,
    resource: Resource,
    policy: AvailabilityPolicy,
    now: datetime,
    blackouts: Sequence[BlackoutPeriod] = (),
    existing: Sequence[BookedSpan] = (),
    free_busy: Sequence[FreeBusySpan] = (),
) -> Decision:
    """Judge one request against the policy, the blackouts, existing bookings, and
    provider-reported busy time.

    ``now`` must be timezone-aware; a naive one is a programming error in the caller, not
    a bad booking request, so it raises rather than becoming a rejection.

    ``free_busy`` is optional busy time from a connected calendar provider (ADR 0009).
    It is only relevant when at least one :class:`FreeBusySpan` is supplied; an
    empty sequence leaves the rule chain byte-for-byte identical to the pre-phase-9
    behaviour (CLAUDE.md §2 — a resource with no provider configured must behave
    exactly as today).

    The returned decision carries no suggestions. Attach them with
    ``Decision.with_suggestions`` — see ``calon.domain.slots.suggest_slots``.
    """
    now_utc = to_utc(now)

    gate = _gating_violation(request, resource, policy)
    if gate is not None:
        return _rejected(gate, (gate,), now_utc)

    start_utc = to_utc(request.start)
    end_utc = _resolve_end(request, policy)

    violations = tuple(
        violation
        for violation in (
            _check_min_notice(start_utc, now_utc, policy),
            _check_max_advance(start_utc, now_utc, policy),
            _check_weekday(start_utc, policy),
            _check_business_hours(start_utc, end_utc, policy),
            _check_blackouts(start_utc, end_utc, blackouts),
            _check_daily_limit(start_utc, policy, existing),
            _check_conflicts(start_utc, end_utc, policy, existing),
            _check_provider_conflicts(start_utc, end_utc, policy, free_busy),
        )
        if violation is not None
    )

    if violations:
        return _rejected(violations[0], violations, now_utc)

    return Decision(
        outcome=Outcome.ACCEPTED,
        code=DecisionCode.ACCEPTED,
        reason=ACCEPTED_REASON,
        evaluated_at=now_utc,
    )


def resolve_end(request: BookingRequest, policy: AvailabilityPolicy) -> datetime:
    """The request's end instant in UTC, applying the policy default when it is omitted.

    Exposed because the slot search and the persistence layer both need the same answer,
    and two implementations of "what does an open-ended request mean" would eventually
    disagree.
    """
    return _resolve_end(request, policy)


# --------------------------------------------------------------------------------------
# Gating rules — evaluation stops at the first of these
# --------------------------------------------------------------------------------------


def _gating_violation(
    request: BookingRequest, resource: Resource, policy: AvailabilityPolicy
) -> Violation | None:
    return (
        _check_input(request)
        or _check_resource(request, resource)
        or _check_duration(request, policy)
    )


def _check_input(request: BookingRequest) -> Violation | None:
    if not request.resource_slug:
        return Violation(DecisionCode.INVALID_INPUT, "No resource was named in the request.")
    if not is_aware(request.start):
        return Violation(DecisionCode.INVALID_INPUT, "The requested start time has no timezone.")
    if request.end is not None and not is_aware(request.end):
        return Violation(DecisionCode.INVALID_INPUT, "The requested end time has no timezone.")
    if not is_valid_timezone(request.timezone):
        return Violation(
            DecisionCode.INVALID_INPUT,
            f"{request.timezone!r} is not a recognised timezone name.",
        )
    return None


def _check_resource(request: BookingRequest, resource: Resource) -> Violation | None:
    if request.resource_slug != resource.slug:
        return Violation(
            DecisionCode.RESOURCE_UNKNOWN,
            f"There is no bookable resource called {request.resource_slug!r}.",
        )
    if not resource.is_active:
        return Violation(DecisionCode.RESOURCE_UNKNOWN, "That resource is not accepting bookings.")
    return None


def _check_duration(request: BookingRequest, policy: AvailabilityPolicy) -> Violation | None:
    start_utc = to_utc(request.start)
    end_utc = _resolve_end(request, policy)
    if end_utc <= start_utc:
        return Violation(DecisionCode.DURATION_NOT_ALLOWED, "A booking must end after it starts.")
    return None


def _resolve_end(request: BookingRequest, policy: AvailabilityPolicy) -> datetime:
    if request.end is None:
        return to_utc(request.start) + policy.default_duration
    return to_utc(request.end)


# --------------------------------------------------------------------------------------
# Collected rules — all of these run, and all failures are reported
# --------------------------------------------------------------------------------------


def _check_min_notice(
    start_utc: datetime, now_utc: datetime, policy: AvailabilityPolicy
) -> Violation | None:
    if start_utc - now_utc < policy.min_notice:
        return Violation(
            DecisionCode.BELOW_MIN_NOTICE,
            f"Bookings need at least {_describe(policy.min_notice_min)} notice.",
        )
    return None


def _check_max_advance(
    start_utc: datetime, now_utc: datetime, policy: AvailabilityPolicy
) -> Violation | None:
    if start_utc > now_utc + timedelta(days=policy.max_advance_days):
        return Violation(
            DecisionCode.BEYOND_MAX_ADVANCE,
            f"Bookings can only be made up to {policy.max_advance_days} days ahead.",
        )
    return None


def _check_weekday(start_utc: datetime, policy: AvailabilityPolicy) -> Violation | None:
    local_start = policy.local(start_utc)
    if local_start.weekday() not in policy.allowed_weekdays:
        return Violation(
            DecisionCode.WEEKDAY_NOT_ALLOWED,
            f"{local_start:%A}s are not available for booking.",
        )
    return None


def _check_business_hours(
    start_utc: datetime, end_utc: datetime, policy: AvailabilityPolicy
) -> Violation | None:
    """A booking must start *and* end inside the window; an overrun is rejected.

    The window is compared as instants on the local start date rather than as clock times,
    so a booking that runs past midnight fails instead of wrapping around into the morning.
    """
    local_start = policy.local(start_utc)
    local_end = policy.local(end_utc)
    opens, closes = policy.window_bounds(local_start.date())
    if local_start < opens or local_end > closes:
        return Violation(
            DecisionCode.OUTSIDE_BUSINESS_HOURS,
            f"Bookings run between {policy.window_start:%H:%M} and "
            f"{policy.window_end:%H:%M}, and must finish within that window.",
        )
    return None


def _check_blackouts(
    start_utc: datetime, end_utc: datetime, blackouts: Sequence[BlackoutPeriod]
) -> Violation | None:
    for blackout in blackouts:
        if blackout.covers(start_utc, end_utc):
            detail = f": {blackout.reason}" if blackout.reason else "."
            return Violation(DecisionCode.BLACKOUT_PERIOD, f"That time is unavailable{detail}")
    return None


def _check_daily_limit(
    start_utc: datetime, policy: AvailabilityPolicy, existing: Sequence[BookedSpan]
) -> Violation | None:
    limit = policy.max_bookings_per_day
    if limit is None:
        return None
    local_day = policy.local(start_utc).date()
    booked = sum(1 for span in existing if policy.local(span.start_utc).date() == local_day)
    if booked >= limit:
        return Violation(DecisionCode.DAILY_LIMIT_REACHED, "That day is fully booked.")
    return None


def _check_conflicts(
    start_utc: datetime,
    end_utc: datetime,
    policy: AvailabilityPolicy,
    existing: Sequence[BookedSpan],
) -> Violation | None:
    block_start, block_end = policy.buffered_span(start_utc, end_utc)
    for span in existing:
        if span.conflicts_with(block_start, block_end):
            return Violation(DecisionCode.SLOT_CONFLICT, "That slot overlaps an existing booking.")
    return None


def _check_provider_conflicts(
    start_utc: datetime,
    end_utc: datetime,
    policy: AvailabilityPolicy,
    free_busy: Sequence[FreeBusySpan],
) -> Violation | None:
    """Reject with ``PROVIDER_CONFLICT`` when the request overlaps provider-reported busy.

    The request's span (buffered, mirroring the own-booking conflict check) is tested
    against every :class:`FreeBusySpan` in ``free_busy``. An empty ``free_busy`` makes
    this rule a no-op, which is what keeps a resource with no provider configured
    byte-for-byte identical to its pre-phase-9 behaviour (ADR 0009 / CLAUDE.md §2).
    """
    block_start, block_end = policy.buffered_span(start_utc, end_utc)
    for span in free_busy:
        if span.covers(block_start, block_end):
            detail = f": {span.reason}" if span.reason else ""
            return Violation(
                DecisionCode.PROVIDER_CONFLICT,
                f"That time conflicts with the resource's already-scheduled time{detail}.",
            )
    return None


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _rejected(first: Violation, violations: tuple[Violation, ...], now_utc: datetime) -> Decision:
    return Decision(
        outcome=Outcome.REJECTED,
        code=first.code,
        reason=first.message,
        evaluated_at=now_utc,
        violations=violations,
    )


def _describe(minutes: int) -> str:
    """Render a minute count the way a person would say it."""
    if minutes < 60:
        return f"{minutes} minutes"
    hours, remainder = divmod(minutes, 60)
    unit = "hour" if hours == 1 else "hours"
    if remainder:
        return f"{hours} {unit} {remainder} minutes"
    return f"{hours} {unit}"
