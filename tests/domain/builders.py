"""Plain builders for domain tests.

Deliberately not pytest fixtures. The domain layer is pure, so its tests need no setup,
no database, and no dependency injection — and keeping the helpers as ordinary functions
keeps that property visible. If one of these ever needs a fixture, the purity rule has
been broken somewhere.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from calon.domain import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    BookingRequest,
    FreeBusySpan,
    Resource,
)

BERLIN = "Europe/Berlin"
NEW_YORK = "America/New_York"
WORKDAYS = frozenset({0, 1, 2, 3, 4})
EVERY_DAY = frozenset(range(7))

RESOURCE = Resource(slug="default", timezone=BERLIN)


def make_policy(
    timezone: str = BERLIN,
    allowed_weekdays: frozenset[int] = WORKDAYS,
    window_start: time = time(9, 0),
    window_end: time = time(17, 0),
    default_duration_min: int = 30,
    slot_granularity_min: int = 15,
    min_notice_min: int = 120,
    max_advance_days: int = 60,
    buffer_before_min: int = 0,
    buffer_after_min: int = 15,
    max_bookings_per_day: int | None = None,
) -> AvailabilityPolicy:
    """The example configuration from ``config/calon.example.toml``, unless overridden."""
    return AvailabilityPolicy(
        timezone=timezone,
        allowed_weekdays=allowed_weekdays,
        window_start=window_start,
        window_end=window_end,
        default_duration_min=default_duration_min,
        slot_granularity_min=slot_granularity_min,
        min_notice_min=min_notice_min,
        max_advance_days=max_advance_days,
        buffer_before_min=buffer_before_min,
        buffer_after_min=buffer_after_min,
        max_bookings_per_day=max_bookings_per_day,
    )


def make_open_policy(
    min_notice_min: int = 0,
    max_advance_days: int = 60,
    window_start: time = time(0, 0),
    window_end: time = time(23, 0),
    default_duration_min: int = 30,
) -> AvailabilityPolicy:
    """A policy that constrains almost nothing, so one rule can be tested on its own."""
    return make_policy(
        allowed_weekdays=EVERY_DAY,
        window_start=window_start,
        window_end=window_end,
        default_duration_min=default_duration_min,
        min_notice_min=min_notice_min,
        max_advance_days=max_advance_days,
        buffer_after_min=0,
    )


def at(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    timezone: str = BERLIN,
) -> datetime:
    """An aware local instant in ``timezone``."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(timezone))


def make_request(
    start: datetime,
    end: datetime | None = None,
    resource_slug: str = "default",
    timezone: str = BERLIN,
) -> BookingRequest:
    return BookingRequest(resource_slug=resource_slug, start=start, timezone=timezone, end=end)


def booked(start: datetime, end: datetime, policy: AvailabilityPolicy | None = None) -> BookedSpan:
    """An existing booking, with its block bounds materialised from ``policy``."""
    return BookedSpan.of(start, end, policy or make_policy())


def free_busy(start: datetime, end: datetime, reason: str = "") -> FreeBusySpan:
    """A provider-reported busy interval (ADR 0009)."""
    return FreeBusySpan(starts_at_utc=start, ends_at_utc=end, reason=reason)


def blackout(start: datetime, end: datetime, reason: str = "") -> BlackoutPeriod:
    return BlackoutPeriod(starts_at_utc=start, ends_at_utc=end, reason=reason)
