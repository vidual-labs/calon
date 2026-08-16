"""Decision codes are public API. These tests exist to make changing one loud."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from calon.domain import Decision, DecisionCode, Outcome, SlotSuggestion, Violation
from calon.domain.decision import GATING_CODES

# The documented chain, in evaluation order. Adding a code appends to this list; renaming
# or repurposing one is a breaking API change and this test is where it gets caught.
EXPECTED_ORDER = [
    "INVALID_INPUT",
    "RESOURCE_UNKNOWN",
    "DURATION_NOT_ALLOWED",
    "BELOW_MIN_NOTICE",
    "BEYOND_MAX_ADVANCE",
    "WEEKDAY_NOT_ALLOWED",
    "OUTSIDE_BUSINESS_HOURS",
    "BLACKOUT_PERIOD",
    "DAILY_LIMIT_REACHED",
    "SLOT_CONFLICT",
    "ACCEPTED",
]

NOW = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)


def test_codes_match_the_documented_chain_and_order():
    assert [code.name for code in DecisionCode] == EXPECTED_ORDER


def test_code_values_are_their_names():
    """The wire value is the name; nothing downstream should have to map between them."""
    for code in DecisionCode:
        assert code.value == code.name


def test_gating_codes_are_the_first_three():
    assert {code.name for code in GATING_CODES} == set(EXPECTED_ORDER[:3])


def _decision(code: DecisionCode, outcome: Outcome = Outcome.REJECTED) -> Decision:
    return Decision(outcome=outcome, code=code, reason="because", evaluated_at=NOW)


def test_accepted_decisions_are_not_searchable():
    assert not _decision(DecisionCode.ACCEPTED, Outcome.ACCEPTED).is_searchable


def test_gating_rejections_are_not_searchable():
    """There is no 'next available' for a request that named an unknown resource."""
    for code in GATING_CODES:
        assert not _decision(code).is_searchable


def test_policy_rejections_are_searchable():
    assert _decision(DecisionCode.SLOT_CONFLICT).is_searchable
    assert _decision(DecisionCode.WEEKDAY_NOT_ALLOWED).is_searchable


def test_with_suggestions_returns_a_copy_and_leaves_the_original_alone():
    original = _decision(DecisionCode.SLOT_CONFLICT)
    suggestion = SlotSuggestion(start=NOW, end=NOW, timezone="UTC")

    updated = original.with_suggestions((suggestion,))

    assert original.suggestions == ()
    assert updated.suggestions == (suggestion,)
    assert updated.code is original.code


def test_decisions_are_immutable_values():
    """A decision that has been recorded cannot be edited after the fact."""
    decision = _decision(DecisionCode.SLOT_CONFLICT)
    assert decision.violations == ()

    # Set through a variable attribute name: the point is the runtime guarantee, and
    # mypy already refuses the direct assignment.
    attribute = "reason"
    with pytest.raises(FrozenInstanceError):
        setattr(decision, attribute, "something else")


def test_violation_carries_its_code():
    violation = Violation(DecisionCode.BLACKOUT_PERIOD, "closed")
    assert violation.code is DecisionCode.BLACKOUT_PERIOD
