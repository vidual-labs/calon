"""Provider-reported busy time as a rejection reason (ADR 0009).

``FreeBusySpan`` is the pure value the providers hand to the rule chain. The chain treats
it like an own-booking span for conflict purposes but rejects with a *distinct* code,
``PROVIDER_CONFLICT``, so a requester learns the clash is with the resource's existing
external calendar rather than with another booking calon made (ADR 0009, Consequences).

An empty ``free_busy`` must leave the rule chain identical to its pre-phase-9 behaviour
(CLAUDE.md §2) — that is asserted explicitly here.
"""

from collections.abc import Sequence
from datetime import timedelta

import pytest

from calon.domain import (
    AvailabilityPolicy,
    BookedSpan,
    BookingRequest,
    Decision,
    DecisionCode,
    FreeBusySpan,
    evaluate,
)
from tests.domain.builders import (
    RESOURCE,
    at,
    booked,
    free_busy,
    make_policy,
    make_request,
)

# Tuesday, 08:00 in Berlin; with the default two hours' notice 10:00 is inside the window.
NOW = at(2026, 9, 15, 8, 0)
TUESDAY = (2026, 9, 15)


def judge(
    request: BookingRequest,
    free_busy: Sequence[FreeBusySpan] = (),
    policy: AvailabilityPolicy | None = None,
    existing: Sequence[BookedSpan] = (),
) -> Decision:
    return evaluate(
        request,
        resource=RESOURCE,
        policy=policy or make_policy(),
        now=NOW,
        existing=existing,
        free_busy=free_busy,
    )


# --------------------------------------------------------------------------------------
# The value object
# --------------------------------------------------------------------------------------


def test_a_free_busy_span_must_end_after_it_starts():
    with pytest.raises(ValueError, match="end after it starts"):
        free_busy(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 0))
    with pytest.raises(ValueError, match="end after it starts"):
        free_busy(at(*TUESDAY, 11, 0), at(*TUESDAY, 10, 0))


def test_a_free_busy_span_must_be_timezone_aware():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    naive_start = datetime(2026, 9, 15, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        FreeBusySpan(starts_at_utc=naive_start, ends_at_utc=naive_start + timedelta(hours=1))
    aware_end = datetime(2026, 9, 15, 11, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    with pytest.raises(ValueError, match="timezone-aware"):
        FreeBusySpan(starts_at_utc=naive_start, ends_at_utc=aware_end)


def test_covers_reports_overlap_on_half_open_bounds():
    span = free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    assert span.covers(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))  # inside
    assert span.covers(at(*TUESDAY, 9, 30), at(*TUESDAY, 10, 30))  # starts inside
    assert span.covers(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30))  # ends inside
    # Half-open at the right edge: [11:00, ..) does not overlap [10:00, 11:00).
    assert not span.covers(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30))
    assert not span.covers(at(*TUESDAY, 12, 0), at(*TUESDAY, 12, 30))


# --------------------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------------------


def test_a_request_that_overlaps_provider_busy_is_rejected_with_provider_conflict():
    request = make_request(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 0))
    decision = judge(request, free_busy=[free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 45))])

    assert not decision.accepted
    assert [v.code for v in decision.violations] == [DecisionCode.PROVIDER_CONFLICT]


def test_a_request_that_does_not_overlap_provider_busy_is_accepted():
    # 11:00-11:30 is free; the busy block ends at 11:00 and half-open bounds touch, not overlap.
    request = make_request(at(*TUESDAY, 11, 0), at(*TUESDAY, 11, 30))
    decision = judge(request, free_busy=[free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))])

    assert decision.accepted


def test_provider_busy_respects_the_request_s_own_buffer():
    """The request's own buffer widens the overlap window, mirroring own-booking conflicts."""
    policy = make_policy(buffer_after_min=15)
    # 10:00-10:30 request with a 15-minute after-buffer reaches 10:45; busy to 10:40
    # collides only with the buffer, so it is still a provider conflict.
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))
    decision = judge(
        request, free_busy=[free_busy(at(*TUESDAY, 10, 30), at(*TUESDAY, 10, 40))], policy=policy
    )

    assert not decision.accepted
    assert [v.code for v in decision.violations] == [DecisionCode.PROVIDER_CONFLICT]


def test_the_buffered_overlap_is_not_double_counted_for_the_provider_span():
    """A provider span is raw busy; only the *request* is widened, so a span ending inside
    the buffer but not overlapping the raw request is rejected, while the same span that
    also did not overlap the raw window would not. Kept narrow to the one boundary case."""
    policy = make_policy(buffer_after_min=30)
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    # Raw window 10:00-11:00, buffered to 11:30. Busy 11:20-11:30 only overlaps the buffer.
    decision = judge(
        request, free_busy=[free_busy(at(*TUESDAY, 11, 20), at(*TUESDAY, 11, 30))], policy=policy
    )
    assert not decision.accepted
    assert [v.code for v in decision.violations] == [DecisionCode.PROVIDER_CONFLICT]


def test_provider_conflict_is_distinct_from_slot_conflict():
    """The two conflict codes are separate so the requester hears the right reason.

    The ``test_decision.py:EXPECTED_ORDER`` list already proves the two names are distinct
    in the ``DecisionCode`` enum; here we prove the *behavioural* distinction — an own
    booking and a provider busy span each drive their own code.
    """
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    own = booked(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30), make_policy())
    provider = free_busy(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30))

    assert judge(request, existing=[own]).code is DecisionCode.SLOT_CONFLICT
    assert judge(request, free_busy=[provider]).code is DecisionCode.PROVIDER_CONFLICT


def test_provider_confiction_reason_is_exposed():
    """The busy span's reason is surfaced in the rejection reason."""
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    decision = judge(
        request, free_busy=[free_busy(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30), "Staff offsite")]
    )

    assert decision.code is DecisionCode.PROVIDER_CONFLICT
    assert "Staff offsite" in decision.reason


# --------------------------------------------------------------------------------------
# Regression: an empty free_busy leaves the chain unchanged (CLAUDE.md §2)
# --------------------------------------------------------------------------------------


def test_an_empty_free_busy_is_behaviourally_a_noop():
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    baseline = evaluate(
        request, resource=RESOURCE, policy=make_policy(), now=NOW, existing=(), blackouts=()
    )
    explicit_empty = judge(request)  # free_busy defaults to () here via evaluate keyword
    assert baseline.code is explicit_empty.code
    assert decision_codes(baseline) == decision_codes(explicit_empty)
    assert baseline.accepted


def test_explicit_empty_sequence_matches_the_default():
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    default = evaluate(request, resource=RESOURCE, policy=make_policy(), now=NOW)
    with_empty = judge(request, free_busy=())
    assert default.code is with_empty.code
    assert default.accepted is with_empty.accepted


def test_provider_conflict_is_added_last_in_order():
    """Own-booking conflicts are checked before provider ones, so an own booking still wins."""
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 0))
    own = booked(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30), make_policy())
    provider = free_busy(at(*TUESDAY, 10, 30), at(*TUESDAY, 11, 30))
    # Both set -> the first violation (own, SLOT_CONFLICT) is what is reported as the primary.
    assert (
        evaluate(
            request,
            resource=RESOURCE,
            policy=make_policy(),
            now=NOW,
            existing=[own],
            free_busy=[provider],
        ).code
        is DecisionCode.SLOT_CONFLICT
    )


def decision_codes(decision: Decision) -> list[DecisionCode]:
    return [v.code for v in decision.violations]
