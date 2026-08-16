"""Translation between stored rows and the domain's pure value objects.

Everything here is a one-way door: rows come in, values the rule chain understands come
out. No row ever reaches a rule, which is what keeps ``calon.domain`` free of the ORM.

The encodings are deliberately boring — weekdays as ``"0,1,2,3,4"``, window times as
``"09:00"`` — so the policy table is readable with ``sqlite3`` and a human eye, which is
the kind of thing that matters at 2am on a self-hosted box.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from calon.domain import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    Resource,
)
from calon.models import (
    AuditEvent,
    AvailabilityPolicyRow,
    BlackoutPeriodRow,
    Booking,
    ResourceRow,
)

__all__ = [
    "any_resource",
    "append_audit",
    "encode_weekdays",
    "encode_window_time",
    "find_resource",
    "has_conflict",
    "load_blackouts",
    "load_booked_spans",
    "load_policy",
    "to_domain_resource",
]


# --------------------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------------------


def encode_weekdays(weekdays: frozenset[int]) -> str:
    return ",".join(str(day) for day in sorted(weekdays))


def decode_weekdays(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.split(",") if part)


def encode_window_time(value: time) -> str:
    return value.strftime("%H:%M")


def decode_window_time(raw: str) -> time:
    return time.fromisoformat(raw)


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


def find_resource(session: Session, slug: str) -> ResourceRow | None:
    """The resource with this slug, or ``None`` if there is no such thing."""
    return session.scalar(select(ResourceRow).where(ResourceRow.slug == slug))


def any_resource(session: Session) -> ResourceRow | None:
    """Some configured resource, for judging a request that named an unknown one.

    The rule chain decides whether a slug is bookable, so it needs a real resource to
    compare against even when the request names one that does not exist. Handing it the
    instance's own resource keeps ``RESOURCE_UNKNOWN`` a domain decision rather than
    something the service layer invents on the side.
    """
    return session.scalars(select(ResourceRow).order_by(ResourceRow.slug).limit(1)).first()


def to_domain_resource(row: ResourceRow) -> Resource:
    return Resource(slug=row.slug, timezone=row.timezone, is_active=row.is_active)


def load_policy(session: Session, resource_id: str) -> AvailabilityPolicy:
    """The availability policy for a resource.

    Absence is a programming error, not a request outcome: every resource is written with
    its policy in the same transaction, so a resource without one means the projection
    from ``config/calon.toml`` did not run.
    """
    row = session.get(AvailabilityPolicyRow, resource_id)
    if row is None:
        raise LookupError(f"resource {resource_id!r} has no availability policy")

    return AvailabilityPolicy(
        timezone=row.timezone,
        allowed_weekdays=decode_weekdays(row.allowed_weekdays),
        window_start=decode_window_time(row.window_start),
        window_end=decode_window_time(row.window_end),
        default_duration_min=row.default_duration_min,
        slot_granularity_min=row.slot_granularity_min,
        min_notice_min=row.min_notice_min,
        max_advance_days=row.max_advance_days,
        buffer_before_min=row.buffer_before_min,
        buffer_after_min=row.buffer_after_min,
        max_bookings_per_day=row.max_bookings_per_day,
    )


def load_blackouts(
    session: Session, resource_id: str, window_start: datetime, window_end: datetime
) -> tuple[BlackoutPeriod, ...]:
    """Blackouts overlapping a window, half-open at both ends."""
    rows = session.scalars(
        select(BlackoutPeriodRow)
        .where(
            BlackoutPeriodRow.resource_id == resource_id,
            BlackoutPeriodRow.starts_at_utc < window_end,
            BlackoutPeriodRow.ends_at_utc > window_start,
        )
        .order_by(BlackoutPeriodRow.starts_at_utc)
    ).all()

    return tuple(
        BlackoutPeriod(
            starts_at_utc=row.starts_at_utc,
            ends_at_utc=row.ends_at_utc,
            reason=row.reason,
        )
        for row in rows
    )


def load_booked_spans(
    session: Session, resource_id: str, window_start: datetime, window_end: datetime
) -> tuple[BookedSpan, ...]:
    """Confirmed bookings overlapping a window, as the rule chain sees them.

    The stored block bounds are used as they are rather than recomputed from the current
    buffers: a booking was accepted under the buffers in force at the time, and changing
    them later must not retroactively make accepted bookings overlap.
    """
    rows = session.scalars(
        select(Booking)
        .where(
            Booking.resource_id == resource_id,
            Booking.status == "confirmed",
            Booking.block_start_utc < window_end,
            Booking.block_end_utc > window_start,
        )
        .order_by(Booking.start_utc)
    ).all()

    return tuple(
        BookedSpan(
            start_utc=row.start_utc,
            end_utc=row.end_utc,
            block_start_utc=row.block_start_utc,
            block_end_utc=row.block_end_utc,
        )
        for row in rows
    )


def has_conflict(
    session: Session, resource_id: str, block_start: datetime, block_end: datetime
) -> bool:
    """Whether any confirmed booking's buffered span overlaps this one.

    This is the check run immediately before insert, inside the same ``BEGIN IMMEDIATE``
    transaction that made the decision — the last line of defence against two simultaneous
    requests for the same slot.
    """
    return (
        session.scalar(
            select(Booking.id)
            .where(
                Booking.resource_id == resource_id,
                Booking.status == "confirmed",
                Booking.block_start_utc < block_end,
                Booking.block_end_utc > block_start,
            )
            .limit(1)
        )
        is not None
    )


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------


def append_audit(
    session: Session,
    *,
    at: datetime,
    actor: str,
    event_type: str,
    intent_id: str | None = None,
    booking_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event. The log is never updated and never deleted."""
    event = AuditEvent(
        at_utc=at,
        actor=actor,
        event_type=event_type,
        intent_id=intent_id,
        booking_id=booking_id,
        payload_json=payload or {},
    )
    session.add(event)
    return event
