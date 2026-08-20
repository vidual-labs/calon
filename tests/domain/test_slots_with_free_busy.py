"""Slot search must not propose slots the provider says are busy (ADR 0009).

``suggest_slots`` threads ``free_busy`` into every candidate's rule-chain evaluation, so a
rejected request is only offered alternatives that are not already taken in the resource's
external calendar. The search is a function of the provider's answer for the window it
covers; an empty ``free_busy`` must leave the search byte-for-byte identical to its
pre-phase-9 behaviour (CLAUDE.md §2).
"""

from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from calon.domain import (
    MAX_SUGGESTIONS,
    AvailabilityPolicy,
    BookedSpan,
    BookingRequest,
    FreeBusySpan,
    SlotSuggestion,
    decide,
    suggest_slots,
)
from tests.domain.builders import (
    BERLIN,
    RESOURCE,
    at,
    free_busy,
    make_policy,
    make_request,
)

NOW = at(2026, 9, 15, 8, 0)  # Tuesday, 08:00 Berlin
TUESDAY = (2026, 9, 15)
WEDNESDAY = (2026, 9, 16)
MONDAY = (2026, 9, 21)


def search(
    request: BookingRequest,
    free_busy: Sequence[FreeBusySpan] = (),
    policy: AvailabilityPolicy | None = None,
    existing: Sequence[BookedSpan] = (),
    limit: int = MAX_SUGGESTIONS,
) -> tuple[SlotSuggestion, ...]:
    return suggest_slots(
        request,
        resource=RESOURCE,
        policy=policy or make_policy(),
        now=NOW,
        existing=existing,
        free_busy=free_busy,
        limit=limit,
    )


def local_starts(suggestions: Sequence[SlotSuggestion], timezone: str = BERLIN) -> list[datetime]:
    return [s.start.astimezone(ZoneInfo(timezone)) for s in suggestions]


def test_a_provider_busy_slot_is_not_suggested_in_its_place():
    """Block the first morning grid positions with provider busy; the first free one is offered."""
    # Default granule 15 min; window 09:00-17:00; notice 2h so origin is 10:00.
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))
    # Busy 10:00-10:45 hides 10:00, 10:15, 10:30.
    busy = [free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 45))]
    found = search(request, free_busy=busy, limit=3)

    # The first suggestion is the first grid point not inside 10:00-10:45 (and not inside
    # the request's 15-min after-buffer of the busy block).
    assert local_starts(found)[0] == at(*TUESDAY, 10, 45)


def test_a_provider_busy_block_is_skipped_over_entirely():
    """A busy block spanning 09:00-12:00 must hide every candidate within it."""
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))
    # Busy 10:00-11:30; with the 15-min after-buffer the first clean grid is 11:45.
    busy = [free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 11, 30))]
    found = search(request, free_busy=busy, limit=100)

    assert all(s.start.astimezone(ZoneInfo(BERLIN)) >= at(*TUESDAY, 11, 30) for s in found)
    # Specifically, nothing is suggested inside the busy block's raw bounds.
    assert not any(
        at(*TUESDAY, 10, 0) < s.start.astimezone(ZoneInfo(BERLIN)) < at(*TUESDAY, 11, 30)
        for s in found
    )


def test_a_request_fully_busy_by_the_provider_gets_a_later_suggestion():
    """When the asked slot is busy by the provider, alternatives are proposed past the busy."""
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))
    busy = [free_busy(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 45))]
    decision = decide(
        request,
        resource=RESOURCE,
        policy=make_policy(),
        now=NOW,
        free_busy=busy,
    )

    assert not decision.accepted
    assert decision.code.value == "PROVIDER_CONFLICT"
    assert decision.suggestions, "a rejected request must be offered alternatives"
    # The first alternative is the first bookable grid point after the busy block's end.
    assert local_starts(decision.suggestions)[0] == at(*TUESDAY, 10, 45)


def test_an_empty_free_busy_leaves_suggestions_unchanged():
    """Regression for CLAUDE.md §2: with no provider spans the search behaves as before."""
    request = make_request(at(*TUESDAY, 10, 0), at(*TUESDAY, 10, 30))
    baseline = search(request)
    with_empty = search(request, free_busy=())

    assert baseline
    assert len(baseline) == len(with_empty) == 3
    # The explicit empty sequence must be indistinguishable from the default.
    assert local_starts(baseline) == local_starts(with_empty)
    assert baseline[0] == with_empty[0]
    # And the suggestions themselves are unchanged objects.
    assert list(baseline) == list(with_empty)


def test_a_provider_busy_does_not_propose_a_later_day_slot_that_is_its_own():
    """Even with a wide provider busy covering several days, slots outside it are still offered."""
    request = make_request(at(*WEDNESDAY, 10, 0), at(*WEDNESDAY, 10, 30))
    # Busy on Wednesday 10:00-12:00 only; Thursday candidates must still be offered.
    busy = [free_busy(at(*WEDNESDAY, 10, 0), at(*WEDNESDAY, 12, 0))]
    found = search(request, free_busy=busy, limit=100)

    assert found
    # The first offered slot is the first free grid point on Wednesday after 12:00 (plus buffer).
    assert local_starts(found)[0].date() == date(*WEDNESDAY)
    assert local_starts(found)[0].hour >= 12
