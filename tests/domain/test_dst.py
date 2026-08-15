"""Daylight-saving transitions.

Europe/Berlin springs forward on 2026-03-29 (02:00 → 03:00) and falls back on
2026-10-25 (03:00 → 02:00). These are the cases where "just add an hour" quietly produces
a booking at the wrong moment, so they get their own file.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from calon.domain import (
    AvailabilityPolicy,
    BookingRequest,
    Decision,
    DecisionCode,
    evaluate,
    resolve_end,
    suggest_slots,
)
from tests.domain.builders import (
    BERLIN,
    RESOURCE,
    at,
    make_open_policy,
    make_policy,
    make_request,
)

SPRING_FORWARD = (2026, 3, 29)  # Sunday: 02:00 → 03:00, CET → CEST
FALL_BACK = (2026, 10, 25)  # Sunday: 03:00 → 02:00, CEST → CET


def decide_on(request: BookingRequest, policy: AvailabilityPolicy, now: datetime) -> Decision:
    return evaluate(request, resource=RESOURCE, policy=policy, now=now, existing=())


def test_the_same_wall_clock_maps_to_different_instants_either_side_of_a_transition():
    """09:00 in Berlin is 07:00 UTC in summer and 08:00 UTC in winter."""
    policy = make_policy(min_notice_min=0)
    now = at(2026, 10, 20, 8, 0)

    summer = decide_on(make_request(at(2026, 10, 23, 9, 0)), policy, now)  # Friday, CEST
    winter = decide_on(make_request(at(2026, 10, 26, 9, 0)), policy, now)  # Monday, CET

    assert summer.accepted and winter.accepted
    assert at(2026, 10, 23, 9, 0).astimezone(UTC).hour == 7
    assert at(2026, 10, 26, 9, 0).astimezone(UTC).hour == 8


def test_the_suggestion_grid_keeps_its_wall_clock_across_a_spring_forward():
    """Monday's slots still start at 09:00 local, not 08:00 or 10:00."""
    now = at(2026, 3, 27, 8, 0)  # Friday before the transition, CET
    saturday = make_request(at(2026, 3, 28, 10, 0), at(2026, 3, 28, 10, 30))

    found = suggest_slots(saturday, resource=RESOURCE, policy=make_policy(), now=now, existing=())

    monday = [s.start.astimezone(ZoneInfo(BERLIN)) for s in found]
    assert monday == [
        at(2026, 3, 30, 9, 0),
        at(2026, 3, 30, 9, 15),
        at(2026, 3, 30, 9, 30),
    ]
    # Monday is in summer time now, so 09:00 local really is 07:00 UTC.
    assert found[0].start.astimezone(UTC).hour == 7


def test_minimum_notice_is_measured_in_elapsed_time_not_wall_clock():
    """The wall clock says two and a half hours; only ninety minutes actually pass."""
    policy = make_open_policy(min_notice_min=120)
    now = at(*SPRING_FORWARD, 1, 30)  # 00:30 UTC, still CET

    too_soon = decide_on(make_request(at(*SPRING_FORWARD, 4, 0)), policy, now)
    assert too_soon.code is DecisionCode.BELOW_MIN_NOTICE

    far_enough = decide_on(make_request(at(*SPRING_FORWARD, 4, 30)), policy, now)
    assert far_enough.accepted


def test_a_local_time_that_does_not_exist_normalises_forward():
    """02:30 never happens on the spring-forward date; it resolves to 03:30 CEST."""
    policy = make_open_policy()
    requested = at(*SPRING_FORWARD, 2, 30)

    assert policy.local(requested) == at(*SPRING_FORWARD, 3, 30)

    # With the window closing at 03:00 the request is refused, because the instant it
    # actually names lands at 03:30 — not silently accepted at the time that was typed.
    narrow = make_open_policy(window_end=time(3, 0))
    assert (
        decide_on(make_request(requested), narrow, at(2026, 3, 28, 8, 0)).code
        is DecisionCode.OUTSIDE_BUSINESS_HOURS
    )
    assert decide_on(make_request(requested), policy, at(2026, 3, 28, 8, 0)).accepted


def test_the_default_duration_is_added_in_real_time():
    """A 30 minute booking is 30 real minutes, even when the clock jumps mid-booking."""
    policy = make_open_policy(default_duration_min=30)
    request = make_request(at(*SPRING_FORWARD, 1, 45))

    end = resolve_end(request, policy)

    assert end - at(*SPRING_FORWARD, 1, 45) == policy.default_duration
    # 01:45 CET plus half an hour is 03:15 CEST on the far side of the jump.
    assert end.astimezone(ZoneInfo(BERLIN)) == at(*SPRING_FORWARD, 3, 15)


def test_a_booking_on_the_fall_back_date_is_judged_on_instants():
    policy = make_open_policy()
    now = at(2026, 10, 24, 8, 0)

    decision = decide_on(make_request(at(*FALL_BACK, 10, 0), at(*FALL_BACK, 11, 0)), policy, now)

    assert decision.accepted
    assert at(*FALL_BACK, 10, 0).astimezone(UTC).hour == 9  # CET by then
