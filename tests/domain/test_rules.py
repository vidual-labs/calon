"""The ordered rule chain, including the boundaries that are easy to get wrong."""

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta

import pytest

from calon.domain import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    BookingRequest,
    Decision,
    DecisionCode,
    Outcome,
    Resource,
    evaluate,
    resolve_end,
)
from tests.domain.builders import (
    BERLIN,
    NEW_YORK,
    RESOURCE,
    at,
    blackout,
    booked,
    make_open_policy,
    make_policy,
    make_request,
)

# Tuesday, 08:00 in Berlin. With the default two hours' notice, the first bookable moment
# is 10:00 — comfortably inside the 09:00-17:00 window, so notice and hours can be tested
# without tripping over each other.
NOW = at(2026, 9, 15, 8, 0)
TUESDAY = (2026, 9, 15)
SUNDAY = (2026, 9, 20)


def decide_on(
    request: BookingRequest,
    policy: AvailabilityPolicy | None = None,
    blackouts: Sequence[BlackoutPeriod] = (),
    existing: Sequence[BookedSpan] = (),
    now: datetime = NOW,
    resource: Resource = RESOURCE,
) -> Decision:
    return evaluate(
        request,
        resource=resource,
        policy=policy or make_policy(),
        now=now,
        blackouts=blackouts,
        existing=existing,
    )


def codes(decision: Decision) -> list[DecisionCode]:
    return [violation.code for violation in decision.violations]


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


def test_a_reasonable_request_is_accepted():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0)))

    assert decision.accepted
    assert decision.outcome is Outcome.ACCEPTED
    assert decision.code is DecisionCode.ACCEPTED
    assert decision.violations == ()
    assert decision.suggestions == ()


def test_evaluated_at_is_recorded_in_utc():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0)))
    assert decision.evaluated_at == NOW
    assert decision.evaluated_at.tzinfo is UTC


def test_a_naive_now_is_a_caller_bug_not_a_rejection():
    with pytest.raises(ValueError, match="naive datetime"):
        decide_on(make_request(at(*TUESDAY, 10, 0)), now=datetime(2026, 9, 15, 8, 0))


# --------------------------------------------------------------------------------------
# Gating rules
# --------------------------------------------------------------------------------------


def test_a_naive_start_is_invalid_input():
    decision = decide_on(make_request(datetime(2026, 9, 15, 10, 0)))
    assert decision.code is DecisionCode.INVALID_INPUT


def test_a_naive_end_is_invalid_input():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), datetime(2026, 9, 15, 11, 0)))
    assert decision.code is DecisionCode.INVALID_INPUT


def test_an_unrecognised_requester_timezone_is_invalid_input():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), timezone="Mars/Olympus_Mons"))
    assert decision.code is DecisionCode.INVALID_INPUT
    assert "Mars/Olympus_Mons" in decision.reason


def test_a_missing_resource_slug_is_invalid_input():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), resource_slug=""))
    assert decision.code is DecisionCode.INVALID_INPUT


def test_an_unknown_resource_is_rejected():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), resource_slug="nope"))
    assert decision.code is DecisionCode.RESOURCE_UNKNOWN


def test_an_inactive_resource_is_rejected():
    from calon.domain import Resource

    decision = decide_on(
        make_request(at(*TUESDAY, 10, 0)),
        resource=Resource(slug="default", timezone=BERLIN, is_active=False),
    )
    assert decision.code is DecisionCode.RESOURCE_UNKNOWN


def test_a_zero_length_booking_is_rejected():
    moment = at(*TUESDAY, 10, 0)
    decision = decide_on(make_request(moment, moment))
    assert decision.code is DecisionCode.DURATION_NOT_ALLOWED


def test_a_negative_duration_is_rejected():
    decision = decide_on(make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 10, 0)))
    assert decision.code is DecisionCode.DURATION_NOT_ALLOWED


def test_gating_rules_stop_the_chain_before_it_reports_nonsense():
    """A backwards booking on a Sunday at 03:00 has exactly one useful thing to say."""
    decision = decide_on(make_request(at(*SUNDAY, 3, 0), at(*SUNDAY, 2, 0)))

    assert decision.code is DecisionCode.DURATION_NOT_ALLOWED
    assert codes(decision) == [DecisionCode.DURATION_NOT_ALLOWED]


def test_an_omitted_end_takes_the_policy_default_duration():
    policy = make_policy(default_duration_min=45)
    request = make_request(at(*TUESDAY, 10, 0))

    assert resolve_end(request, policy) == at(*TUESDAY, 10, 45)
    assert decide_on(request, policy).accepted


# --------------------------------------------------------------------------------------
# Notice and advance window
# --------------------------------------------------------------------------------------


def test_a_request_inside_the_notice_period_is_rejected():
    decision = decide_on(make_request(at(*TUESDAY, 9, 45), at(*TUESDAY, 10, 15)))

    assert decision.code is DecisionCode.BELOW_MIN_NOTICE
    assert codes(decision) == [DecisionCode.BELOW_MIN_NOTICE]
    assert "2 hours" in decision.reason


def test_a_request_exactly_at_the_notice_boundary_is_accepted():
    decision = decide_on(make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30)))
    assert decision.accepted


def test_notice_is_measured_in_real_elapsed_time_not_wall_clock():
    policy = make_open_policy(min_notice_min=90)
    decision = decide_on(make_request(at(*TUESDAY, 9, 30)), policy, now=at(*TUESDAY, 8, 0))
    assert decision.accepted


def test_a_request_beyond_the_horizon_is_rejected():
    policy = make_open_policy(max_advance_days=7)
    decision = decide_on(make_request(NOW + timedelta(days=7, minutes=1)), policy)

    assert decision.code is DecisionCode.BEYOND_MAX_ADVANCE
    assert "7 days" in decision.reason


def test_a_request_exactly_on_the_horizon_is_accepted():
    policy = make_open_policy(max_advance_days=7)
    assert decide_on(make_request(NOW + timedelta(days=7)), policy).accepted


# --------------------------------------------------------------------------------------
# Weekday and business hours
# --------------------------------------------------------------------------------------


def test_a_disallowed_weekday_is_rejected():
    decision = decide_on(make_request(at(*SUNDAY, 10, 0), at(*SUNDAY, 10, 30)))

    assert decision.code is DecisionCode.WEEKDAY_NOT_ALLOWED
    assert "Sunday" in decision.reason


def test_a_booking_starting_exactly_when_the_window_opens_is_accepted():
    decision = decide_on(
        make_request(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30)),
        make_policy(min_notice_min=0),
    )
    assert decision.accepted


def test_a_booking_ending_exactly_when_the_window_closes_is_accepted():
    decision = decide_on(make_request(at(*TUESDAY, 16, 30), at(*TUESDAY, 17, 0)))
    assert decision.accepted


def test_a_booking_that_would_overrun_the_window_is_rejected_not_truncated():
    decision = decide_on(make_request(at(*TUESDAY, 16, 45), at(*TUESDAY, 17, 15)))

    assert decision.code is DecisionCode.OUTSIDE_BUSINESS_HOURS
    assert "17:00" in decision.reason


def test_a_booking_before_the_window_opens_is_rejected():
    decision = decide_on(
        make_request(at(*TUESDAY, 8, 45), at(*TUESDAY, 9, 15)),
        make_policy(min_notice_min=0),
    )
    assert decision.code is DecisionCode.OUTSIDE_BUSINESS_HOURS


def test_a_booking_running_past_midnight_does_not_wrap_into_the_next_morning():
    policy = make_open_policy()
    decision = decide_on(make_request(at(*TUESDAY, 22, 30), at(2026, 9, 16, 0, 0)), policy)
    assert decision.code is DecisionCode.OUTSIDE_BUSINESS_HOURS


def test_rules_are_judged_in_the_resource_timezone_not_the_requesters():
    """04:00 in New York is 10:00 in Berlin, and Berlin is where the rules live."""
    decision = decide_on(
        make_request(
            at(2026, 9, 15, 4, 0, timezone=NEW_YORK),
            at(2026, 9, 15, 5, 0, timezone=NEW_YORK),
            timezone=NEW_YORK,
        )
    )
    assert decision.accepted


# --------------------------------------------------------------------------------------
# Blackouts, daily limits, conflicts
# --------------------------------------------------------------------------------------


def test_a_booking_inside_a_blackout_is_rejected_with_its_reason():
    period = blackout(at(*TUESDAY, 10, 0), at(*TUESDAY, 12, 0), "Team offsite")
    decision = decide_on(
        make_request(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 0)), blackouts=[period]
    )

    assert decision.code is DecisionCode.BLACKOUT_PERIOD
    assert "Team offsite" in decision.reason


def test_a_booking_starting_exactly_when_a_blackout_ends_is_accepted():
    period = blackout(at(*TUESDAY, 9, 0), at(*TUESDAY, 10, 0))
    decision = decide_on(
        make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30)), blackouts=[period]
    )
    assert decision.accepted


def test_the_daily_limit_counts_only_bookings_on_the_same_local_day():
    policy = make_policy(max_bookings_per_day=2)
    same_day = [
        booked(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30), policy),
        booked(at(*TUESDAY, 9, 45), at(*TUESDAY, 10, 15), policy),
    ]
    other_day = [booked(at(2026, 9, 16, 9, 0), at(2026, 9, 16, 9, 30), policy)]

    reached = decide_on(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=same_day
    )
    assert reached.code is DecisionCode.DAILY_LIMIT_REACHED

    elsewhere = decide_on(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=other_day
    )
    assert elsewhere.accepted


def test_no_daily_limit_is_configured_by_default():
    policy = make_policy()
    existing = [
        booked(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30), policy),
        booked(at(*TUESDAY, 9, 45), at(*TUESDAY, 10, 15), policy),
    ]
    decision = decide_on(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=existing
    )
    assert decision.accepted


def test_an_overlapping_booking_is_a_conflict():
    policy = make_policy()
    existing = [booked(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0), policy)]
    decision = decide_on(
        make_request(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30)), policy, existing=existing
    )
    assert decision.code is DecisionCode.SLOT_CONFLICT


def test_the_trailing_buffer_stops_a_booking_being_squeezed_in_behind_another():
    """Existing 10:00-11:00 with a 15 minute trailing buffer blocks an 11:00 start."""
    policy = make_policy(buffer_after_min=15)
    existing = [booked(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0), policy)]

    squeezed = decide_on(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=existing
    )
    assert squeezed.code is DecisionCode.SLOT_CONFLICT

    clear = decide_on(
        make_request(at(*TUESDAY, 11, 15), at(*TUESDAY, 11, 45)), policy, existing=existing
    )
    assert clear.accepted


def test_the_leading_buffer_protects_the_time_before_a_booking():
    policy = make_policy(buffer_before_min=30, buffer_after_min=0)
    existing = [booked(at(*TUESDAY, 11, 0), at(*TUESDAY, 12, 0), policy)]

    decision = decide_on(
        make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 45)), policy, existing=existing
    )
    assert decision.code is DecisionCode.SLOT_CONFLICT


def test_back_to_back_bookings_are_allowed_when_no_buffer_is_configured():
    policy = make_policy(buffer_before_min=0, buffer_after_min=0)
    existing = [booked(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0), policy)]

    decision = decide_on(
        make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30)), policy, existing=existing
    )
    assert decision.accepted


def test_two_identical_requests_are_judged_identically():
    """The domain is a pure function; serialising the winner is the caller's job.

    This is the half of the simultaneous-request problem that lives here. The other half
    — that only one of them may be written — belongs to the transaction in phase 2.
    """
    policy = make_policy()
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))

    first = decide_on(request, policy)
    second = decide_on(request, policy)

    assert first.accepted and second.accepted
    assert first == second


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def test_every_failure_is_reported_but_the_code_is_the_first_one():
    decision = decide_on(make_request(at(*SUNDAY, 3, 0), at(*SUNDAY, 3, 30)))

    assert codes(decision) == [
        DecisionCode.WEEKDAY_NOT_ALLOWED,
        DecisionCode.OUTSIDE_BUSINESS_HOURS,
    ]
    assert decision.code is DecisionCode.WEEKDAY_NOT_ALLOWED
    assert decision.reason == decision.violations[0].message


def test_violations_are_reported_in_chain_order():
    """A request that fails notice, weekday, and hours reports them in that order."""
    decision = decide_on(
        make_request(at(2026, 9, 20, 3, 0), at(2026, 9, 20, 3, 30)),
        make_policy(min_notice_min=60 * 24 * 30),
    )

    assert codes(decision) == [
        DecisionCode.BELOW_MIN_NOTICE,
        DecisionCode.WEEKDAY_NOT_ALLOWED,
        DecisionCode.OUTSIDE_BUSINESS_HOURS,
    ]


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(30, "30 minutes"), (60, "1 hour"), (120, "2 hours"), (90, "1 hour 30 minutes")],
)
def test_notice_is_described_the_way_a_person_would_say_it(minutes, expected):
    decision = decide_on(
        make_request(at(*TUESDAY, 9, 0), at(*TUESDAY, 9, 30)),
        make_policy(min_notice_min=minutes, window_start=time(0, 0)),
        now=at(*TUESDAY, 8, 55),
    )
    assert decision.code is DecisionCode.BELOW_MIN_NOTICE
    assert expected in decision.reason
