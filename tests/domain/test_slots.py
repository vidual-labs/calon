"""Next-available slot search."""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from calon.domain import (
    MAX_SUGGESTIONS,
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    BookingRequest,
    Decision,
    DecisionCode,
    SlotSuggestion,
    decide,
    suggest_slots,
)
from tests.domain.builders import (
    BERLIN,
    NEW_YORK,
    RESOURCE,
    at,
    blackout,
    booked,
    make_policy,
    make_request,
)

NOW = at(2026, 9, 15, 8, 0)  # Tuesday, 08:00 Berlin
TUESDAY = (2026, 9, 15)
WEDNESDAY = (2026, 9, 16)
SATURDAY = (2026, 9, 19)
MONDAY = (2026, 9, 21)


def suggestions_for(
    request: BookingRequest,
    policy: AvailabilityPolicy | None = None,
    blackouts: Sequence[BlackoutPeriod] = (),
    existing: Sequence[BookedSpan] = (),
    now: datetime = NOW,
    limit: int = MAX_SUGGESTIONS,
    until: datetime | None = None,
) -> tuple[SlotSuggestion, ...]:
    return suggest_slots(
        request,
        resource=RESOURCE,
        policy=policy or make_policy(),
        now=now,
        blackouts=blackouts,
        existing=existing,
        limit=limit,
        until=until,
    )


def local_starts(suggestions: Sequence[SlotSuggestion], timezone: str = BERLIN) -> list[datetime]:
    return [s.start.astimezone(ZoneInfo(timezone)) for s in suggestions]


def test_a_weekend_request_is_offered_the_next_working_morning():
    found = suggestions_for(make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 10, 30)))

    assert local_starts(found) == [
        at(*MONDAY, 9, 0),
        at(*MONDAY, 9, 15),
        at(*MONDAY, 9, 30),
    ]


def test_three_suggestions_are_offered_by_default():
    found = suggestions_for(make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 10, 30)))
    assert len(found) == MAX_SUGGESTIONS == 3


def test_suggestions_preserve_the_requested_duration():
    found = suggestions_for(make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 11, 0)))
    for suggestion in found:
        assert suggestion.end - suggestion.start == timedelta(hours=1)


def test_suggestions_are_expressed_in_the_requesters_timezone():
    found = suggestions_for(
        make_request(
            at(*SATURDAY, 10, 0, timezone=NEW_YORK),
            at(*SATURDAY, 10, 30, timezone=NEW_YORK),
            timezone=NEW_YORK,
        )
    )

    assert found[0].timezone == NEW_YORK
    assert found[0].start.tzinfo == ZoneInfo(NEW_YORK)
    # 09:00 in Berlin is 03:00 in New York.
    assert found[0].start.hour == 3
    assert found[0].start == at(*MONDAY, 9, 0)


def test_suggestions_step_the_configured_granularity_grid():
    policy = make_policy(slot_granularity_min=20)
    found = suggestions_for(make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 10, 30)), policy)

    assert local_starts(found) == [
        at(*MONDAY, 9, 0),
        at(*MONDAY, 9, 20),
        at(*MONDAY, 9, 40),
    ]


def test_suggestions_never_fall_inside_the_notice_period():
    """Asked for 09:00 with two hours' notice at 08:00, the first offer is 10:00."""
    found = suggestions_for(make_request(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30)))

    assert local_starts(found)[0] == at(*TUESDAY, 10, 0)


def test_suggestions_skip_over_an_existing_booking_and_its_buffer():
    policy = make_policy(buffer_after_min=15)
    existing = [booked(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0), policy)]

    found = suggestions_for(
        make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0)), policy, existing=existing
    )

    assert local_starts(found) == [
        at(*TUESDAY, 11, 15),
        at(*TUESDAY, 11, 30),
        at(*TUESDAY, 11, 45),
    ]


def test_suggestions_skip_over_a_blackout():
    period = blackout(at(*TUESDAY, 10, 0), at(*TUESDAY, 12, 0), "Team offsite")
    found = suggestions_for(
        make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30)), blackouts=[period]
    )

    assert local_starts(found)[0] == at(*TUESDAY, 12, 0)


def test_suggestions_never_overrun_the_window():
    policy = make_policy(slot_granularity_min=30)
    found = suggestions_for(make_request(at(*TUESDAY, 16, 45), at(*TUESDAY, 17, 45)), policy)

    for suggestion in found:
        assert suggestion.end.astimezone(ZoneInfo(BERLIN)).time() <= time(17, 0)


def test_a_day_that_is_fully_booked_is_skipped_entirely():
    policy = make_policy(max_bookings_per_day=1)
    existing = [booked(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30), policy)]

    found = suggestions_for(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=existing
    )

    assert local_starts(found)[0] == at(2026, 9, 16, 9, 0)


def test_nothing_is_offered_when_no_allowed_day_falls_before_the_horizon():
    policy = make_policy(allowed_weekdays=frozenset({0}), max_advance_days=1)
    found = suggestions_for(make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30)), policy)
    assert found == ()


def test_the_limit_is_respected():
    request = make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 10, 30))

    assert len(suggestions_for(request, limit=1)) == 1
    assert suggestions_for(request, limit=0) == ()


def test_an_unusable_request_yields_nothing_to_search_for():
    backwards = make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 10, 0))
    assert suggestions_for(backwards) == ()

    unknown_zone = make_request(at(*TUESDAY, 10, 0), timezone="Mars/Olympus_Mons")
    assert suggestions_for(unknown_zone) == ()


# --------------------------------------------------------------------------------------
# decide() — the composition the service layer will call
# --------------------------------------------------------------------------------------


def _decide(
    request: BookingRequest,
    policy: AvailabilityPolicy | None = None,
    existing: Sequence[BookedSpan] = (),
    blackouts: Sequence[BlackoutPeriod] = (),
) -> Decision:
    return decide(
        request,
        resource=RESOURCE,
        policy=policy or make_policy(),
        now=NOW,
        blackouts=blackouts,
        existing=existing,
    )


def test_an_accepted_decision_carries_no_suggestions():
    decision = _decide(make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0)))

    assert decision.accepted
    assert decision.suggestions == ()


def test_a_policy_rejection_carries_alternatives():
    decision = _decide(make_request(at(*SATURDAY, 10, 0), at(*SATURDAY, 10, 30)))

    assert decision.code is DecisionCode.WEEKDAY_NOT_ALLOWED
    assert local_starts(decision.suggestions)[0] == at(*MONDAY, 9, 0)


def test_a_structurally_unusable_request_is_not_offered_alternatives():
    """There is no 'next available' for a resource that does not exist."""
    decision = _decide(make_request(at(*TUESDAY, 10, 0), resource_slug="nope"))

    assert decision.code is DecisionCode.RESOURCE_UNKNOWN
    assert decision.suggestions == ()


# --------------------------------------------------------------------------------------
# `until` — the bound that lets the availability query share this search (ADR 0007)
# --------------------------------------------------------------------------------------


def test_until_bounds_the_search_to_the_window_asked_about():
    found = suggestions_for(
        make_request(at(*WEDNESDAY, 9, 0), at(*WEDNESDAY, 9, 30)),
        limit=100,
        until=at(*WEDNESDAY, 10, 0),
    )

    # 09:45 would end at 10:15, past the window, so it is not offered.
    assert local_starts(found) == [
        at(*WEDNESDAY, 9, 0),
        at(*WEDNESDAY, 9, 15),
        at(*WEDNESDAY, 9, 30),
    ]


def test_a_slot_must_finish_by_until_rather_than_merely_start_before_it():
    found = suggestions_for(
        make_request(at(*WEDNESDAY, 9, 0), at(*WEDNESDAY, 10, 0)),
        limit=100,
        until=at(*WEDNESDAY, 11, 0),
    )

    assert all(slot.end <= at(*WEDNESDAY, 11, 0) for slot in found)
    assert local_starts(found)[-1] == at(*WEDNESDAY, 10, 0)


def test_until_can_narrow_the_horizon_but_never_extend_it():
    """The policy's advance window still wins; a caller cannot ask past it."""
    found = suggestions_for(
        make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30)),
        policy=make_policy(max_advance_days=1),
        limit=100,
        until=at(*SATURDAY, 17, 0),
    )

    assert found
    assert {start.date() for start in local_starts(found)} == {date(*TUESDAY)}


def test_a_window_too_short_for_the_duration_offers_nothing():
    found = suggestions_for(
        make_request(at(*WEDNESDAY, 9, 0), at(*WEDNESDAY, 10, 0)),
        limit=100,
        until=at(*WEDNESDAY, 9, 30),
    )

    assert found == ()


def test_a_window_that_has_already_passed_offers_nothing():
    found = suggestions_for(
        make_request(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30)),
        limit=100,
        until=at(*TUESDAY, 9, 30),
    )

    assert found == ()
