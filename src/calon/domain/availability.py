"""The pure value objects the rule chain reasons about, and the time helpers it needs.

Nothing here touches a database, a framework, or the wall clock. Every instant is
timezone-aware and normalised to UTC at the boundary; naive datetimes are a bug.

Operator misconfiguration (a nonsense window, an unknown timezone) raises ``ValueError``
at construction rather than becoming a rejected booking. A requester should never be told
their Tuesday afternoon is invalid because someone typo'd a config file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS: frozenset[int] = frozenset(range(7))


def is_valid_timezone(name: str) -> bool:
    """Whether ``name`` resolves in the IANA database (``Europe/Berlin``, ``UTC``).

    Legacy names such as ``CET`` do resolve, so this is a "can we convert with it" check
    rather than a house-style one. Preferring region/city names is operator guidance in
    ``config/calon.example.toml``, not something the core enforces.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, raising ``ValueError`` if it is not one."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


def is_aware(moment: datetime) -> bool:
    """Whether ``moment`` carries a usable UTC offset."""
    return moment.tzinfo is not None and moment.utcoffset() is not None


def to_utc(moment: datetime) -> datetime:
    """Normalise an aware datetime to UTC, raising ``ValueError`` if it is naive."""
    if not is_aware(moment):
        raise ValueError("naive datetime; every instant must carry a timezone")
    return moment.astimezone(UTC)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open overlap test: touching at an edge is not an overlap.

    This is what lets a booking end at exactly the moment the next one begins.
    """
    return a_start < b_end and a_end > b_start


@dataclass(frozen=True, slots=True)
class Resource:
    """The bookable thing — a person, a room, a service."""

    slug: str
    timezone: str
    is_active: bool = True

    def __post_init__(self) -> None:
        if not self.slug:
            raise ValueError("resource slug must not be empty")
        zone(self.timezone)


@dataclass(frozen=True, slots=True)
class AvailabilityPolicy:
    """The operator's scheduling rules for one resource.

    Every field is interpreted in ``timezone`` — the *resource's* timezone — regardless of
    where the requester is. ``window_start`` and ``window_end`` are local clock times.
    """

    timezone: str
    allowed_weekdays: frozenset[int]
    window_start: time
    window_end: time
    default_duration_min: int
    slot_granularity_min: int
    min_notice_min: int
    max_advance_days: int
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    max_bookings_per_day: int | None = None

    def __post_init__(self) -> None:
        zone(self.timezone)
        if not self.allowed_weekdays:
            raise ValueError("allowed_weekdays must not be empty")
        if not self.allowed_weekdays <= WEEKDAYS:
            raise ValueError("allowed_weekdays must be integers 0 (Monday) to 6 (Sunday)")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        if self.default_duration_min <= 0:
            raise ValueError("default_duration_min must be positive")
        if self.slot_granularity_min <= 0:
            raise ValueError("slot_granularity_min must be positive")
        if self.min_notice_min < 0:
            raise ValueError("min_notice_min must not be negative")
        if self.max_advance_days <= 0:
            raise ValueError("max_advance_days must be positive")
        if self.buffer_before_min < 0 or self.buffer_after_min < 0:
            raise ValueError("buffers must not be negative")
        if self.max_bookings_per_day is not None and self.max_bookings_per_day <= 0:
            raise ValueError("max_bookings_per_day must be positive when set")

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.timezone)

    @property
    def default_duration(self) -> timedelta:
        return timedelta(minutes=self.default_duration_min)

    @property
    def granularity(self) -> timedelta:
        return timedelta(minutes=self.slot_granularity_min)

    @property
    def min_notice(self) -> timedelta:
        return timedelta(minutes=self.min_notice_min)

    def local(self, moment: datetime) -> datetime:
        """Express an instant in the resource's timezone."""
        return to_utc(moment).astimezone(self.tz)

    def window_bounds(self, local_day: date) -> tuple[datetime, datetime]:
        """The open and close instants of the booking window on ``local_day``.

        Returned as aware datetimes rather than clock times so that a booking running past
        midnight, or across a DST transition, compares correctly instead of wrapping.
        """
        tz = self.tz
        return (
            datetime.combine(local_day, self.window_start, tzinfo=tz),
            datetime.combine(local_day, self.window_end, tzinfo=tz),
        )

    def buffered_span(self, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        """Widen a booking's span by the configured buffers, for conflict detection.

        Buffers never appear in the calendar event itself — they exist so back-to-back
        bookings cannot be squeezed together.
        """
        return (
            to_utc(start) - timedelta(minutes=self.buffer_before_min),
            to_utc(end) + timedelta(minutes=self.buffer_after_min),
        )


@dataclass(frozen=True, slots=True)
class BlackoutPeriod:
    """Time that is closed regardless of every other rule.

    Whole-day blackouts are stored as local-midnight-to-midnight converted to UTC, so the
    rule that checks them has exactly one shape to handle.
    """

    starts_at_utc: datetime
    ends_at_utc: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if not is_aware(self.starts_at_utc) or not is_aware(self.ends_at_utc):
            raise ValueError("blackout bounds must be timezone-aware")
        if self.ends_at_utc <= self.starts_at_utc:
            raise ValueError("blackout must end after it starts")

    def covers(self, start: datetime, end: datetime) -> bool:
        return overlaps(start, end, self.starts_at_utc, self.ends_at_utc)


@dataclass(frozen=True, slots=True)
class BookedSpan:
    """An existing confirmed booking, as the rule chain sees it.

    The block bounds are materialised by the caller from the buffers in force when the
    booking was accepted, which is why they are passed in rather than recomputed here.
    """

    start_utc: datetime
    end_utc: datetime
    block_start_utc: datetime
    block_end_utc: datetime

    def __post_init__(self) -> None:
        moments = (self.start_utc, self.end_utc, self.block_start_utc, self.block_end_utc)
        if not all(is_aware(moment) for moment in moments):
            raise ValueError("booked span bounds must be timezone-aware")
        if self.end_utc <= self.start_utc:
            raise ValueError("booking must end after it starts")
        if self.block_start_utc > self.start_utc or self.block_end_utc < self.end_utc:
            raise ValueError("block bounds must contain the booking itself")

    @classmethod
    def of(cls, start: datetime, end: datetime, policy: AvailabilityPolicy) -> BookedSpan:
        """Build a span, materialising the block bounds from ``policy``'s buffers."""
        block_start, block_end = policy.buffered_span(start, end)
        return cls(
            start_utc=to_utc(start),
            end_utc=to_utc(end),
            block_start_utc=block_start,
            block_end_utc=block_end,
        )

    def conflicts_with(self, block_start: datetime, block_end: datetime) -> bool:
        return overlaps(block_start, block_end, self.block_start_utc, self.block_end_utc)
