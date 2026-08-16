"""Value objects, time helpers, and operator-configuration validation."""

from datetime import UTC, datetime, time

import pytest

from calon.domain import AvailabilityPolicy, BlackoutPeriod, BookedSpan, Resource
from calon.domain.availability import is_aware, is_valid_timezone, overlaps, to_utc, zone
from tests.domain.builders import BERLIN, at, booked, make_policy


def test_valid_timezone_requires_an_iana_name():
    assert is_valid_timezone(BERLIN)
    assert is_valid_timezone("UTC")
    assert not is_valid_timezone("Mars/Olympus_Mons")
    assert not is_valid_timezone("")


def test_zone_raises_for_a_name_that_is_not_a_timezone():
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        zone("Not/AZone")


def test_naive_datetimes_are_rejected_rather_than_guessed_at():
    assert not is_aware(datetime(2026, 9, 15, 10, 0))
    with pytest.raises(ValueError, match="naive datetime"):
        to_utc(datetime(2026, 9, 15, 10, 0))


def test_to_utc_normalises_without_moving_the_instant():
    local = at(2026, 9, 15, 11, 0)
    assert to_utc(local) == local
    assert to_utc(local).tzinfo is UTC
    assert to_utc(local).hour == 9  # Berlin is UTC+2 in September


def test_touching_spans_do_not_overlap():
    """A booking may end at exactly the moment the next one begins."""
    first_end = at(2026, 9, 15, 10, 0)
    assert not overlaps(at(2026, 9, 15, 9, 0), first_end, first_end, at(2026, 9, 15, 11, 0))
    assert overlaps(
        at(2026, 9, 15, 9, 0),
        at(2026, 9, 15, 10, 1),
        first_end,
        at(2026, 9, 15, 11, 0),
    )


# --------------------------------------------------------------------------------------
# Operator configuration errors surface at construction, not as a rejected booking
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"allowed_weekdays": frozenset()}, "must not be empty"),
        ({"allowed_weekdays": frozenset({0, 7})}, "0 \\(Monday\\) to 6"),
        ({"window_start": time(17, 0), "window_end": time(9, 0)}, "earlier than"),
        ({"window_start": time(9, 0), "window_end": time(9, 0)}, "earlier than"),
        ({"default_duration_min": 0}, "default_duration_min must be positive"),
        ({"slot_granularity_min": 0}, "slot_granularity_min must be positive"),
        ({"min_notice_min": -1}, "must not be negative"),
        ({"max_advance_days": 0}, "max_advance_days must be positive"),
        ({"buffer_after_min": -5}, "buffers must not be negative"),
        ({"max_bookings_per_day": 0}, "must be positive when set"),
        ({"timezone": "Europe/Berln"}, "unknown IANA timezone"),
    ],
)
def test_policy_rejects_nonsense_configuration(overrides, message):
    with pytest.raises(ValueError, match=message):
        make_policy(**overrides)


def test_resource_rejects_an_empty_slug_or_a_bad_timezone():
    with pytest.raises(ValueError, match="slug must not be empty"):
        Resource(slug="", timezone=BERLIN)
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        Resource(slug="default", timezone="Europe/Berln")


def test_unlimited_is_the_default_daily_cap():
    assert make_policy().max_bookings_per_day is None


def test_policy_exposes_its_durations_as_timedeltas():
    policy = make_policy(default_duration_min=45, slot_granularity_min=15, min_notice_min=90)
    assert policy.default_duration.total_seconds() == 45 * 60
    assert policy.granularity.total_seconds() == 15 * 60
    assert policy.min_notice.total_seconds() == 90 * 60


# --------------------------------------------------------------------------------------
# Windows, buffers, blackouts
# --------------------------------------------------------------------------------------


def test_window_bounds_are_aware_instants_on_the_requested_local_day():
    policy = make_policy()
    opens, closes = policy.window_bounds(datetime(2026, 9, 15).date())

    assert opens == at(2026, 9, 15, 9, 0)
    assert closes == at(2026, 9, 15, 17, 0)
    assert opens.utcoffset() is not None


def test_buffered_span_widens_only_for_conflict_detection():
    policy = make_policy(buffer_before_min=10, buffer_after_min=15)
    start, end = at(2026, 9, 15, 10, 0), at(2026, 9, 15, 11, 0)

    block_start, block_end = policy.buffered_span(start, end)

    assert block_start == at(2026, 9, 15, 9, 50)
    assert block_end == at(2026, 9, 15, 11, 15)


def test_booked_span_materialises_its_block_from_the_policy():
    policy = make_policy(buffer_before_min=0, buffer_after_min=15)
    span = booked(at(2026, 9, 15, 10, 0), at(2026, 9, 15, 11, 0), policy)

    assert span.start_utc == to_utc(at(2026, 9, 15, 10, 0))
    assert span.block_end_utc == to_utc(at(2026, 9, 15, 11, 15))
    assert span.block_start_utc == span.start_utc


def test_booked_span_rejects_bounds_that_do_not_contain_the_booking():
    start, end = at(2026, 9, 15, 10, 0), at(2026, 9, 15, 11, 0)
    with pytest.raises(ValueError, match="must contain the booking"):
        BookedSpan(
            start_utc=start,
            end_utc=end,
            block_start_utc=at(2026, 9, 15, 10, 30),
            block_end_utc=end,
        )


def test_booked_span_rejects_a_backwards_booking():
    moment = at(2026, 9, 15, 10, 0)
    with pytest.raises(ValueError, match="must end after it starts"):
        BookedSpan(
            start_utc=moment,
            end_utc=moment,
            block_start_utc=moment,
            block_end_utc=moment,
        )


def test_blackout_requires_aware_bounds_and_positive_length():
    with pytest.raises(ValueError, match="must be timezone-aware"):
        BlackoutPeriod(
            starts_at_utc=datetime(2026, 12, 24),
            ends_at_utc=at(2026, 12, 25, 0, 0),
        )
    with pytest.raises(ValueError, match="must end after it starts"):
        BlackoutPeriod(
            starts_at_utc=at(2026, 12, 25, 0, 0),
            ends_at_utc=at(2026, 12, 24, 0, 0),
        )


def test_blackout_covers_is_half_open():
    period = BlackoutPeriod(
        starts_at_utc=at(2026, 12, 24, 0, 0), ends_at_utc=at(2026, 12, 25, 0, 0)
    )
    assert period.covers(at(2026, 12, 24, 23, 0), at(2026, 12, 24, 23, 30))
    # A booking that starts exactly when the blackout ends is fine.
    assert not period.covers(at(2026, 12, 25, 0, 0), at(2026, 12, 25, 1, 0))


def test_policy_is_hashable_so_it_can_be_cached_and_compared():
    assert make_policy() == make_policy()
    assert len({make_policy(), make_policy()}) == 1
    assert isinstance(make_policy(), AvailabilityPolicy)
