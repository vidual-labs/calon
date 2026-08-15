"""Decision codes and the structured accept/reject result.

``DecisionCode`` is public API. Once a code has shipped, its string is never renamed,
never repurposed, and never has its meaning changed — a new constraint gets a new code
(``CLAUDE.md`` §5). The declaration order below *is* the rule chain's evaluation order.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum


class DecisionCode(StrEnum):
    """Why a request was accepted, or the first rule it failed.

    Declared in evaluation order. ``ACCEPTED`` is last because it is not a failure.
    """

    INVALID_INPUT = "INVALID_INPUT"
    RESOURCE_UNKNOWN = "RESOURCE_UNKNOWN"
    DURATION_NOT_ALLOWED = "DURATION_NOT_ALLOWED"
    BELOW_MIN_NOTICE = "BELOW_MIN_NOTICE"
    BEYOND_MAX_ADVANCE = "BEYOND_MAX_ADVANCE"
    WEEKDAY_NOT_ALLOWED = "WEEKDAY_NOT_ALLOWED"
    OUTSIDE_BUSINESS_HOURS = "OUTSIDE_BUSINESS_HOURS"
    BLACKOUT_PERIOD = "BLACKOUT_PERIOD"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    SLOT_CONFLICT = "SLOT_CONFLICT"
    ACCEPTED = "ACCEPTED"


#: Codes that describe a structurally unusable request. Evaluation stops at the first of
#: these, because every later rule would be reasoning about nonsense — a negative duration
#: would report a spurious "ends outside business hours", for example.
GATING_CODES: frozenset[DecisionCode] = frozenset(
    {
        DecisionCode.INVALID_INPUT,
        DecisionCode.RESOURCE_UNKNOWN,
        DecisionCode.DURATION_NOT_ALLOWED,
    }
)


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Violation:
    """One rule the request failed. A rejected request may carry several."""

    code: DecisionCode
    message: str


@dataclass(frozen=True, slots=True)
class SlotSuggestion:
    """An alternative slot that passes the complete rule chain.

    ``start`` and ``end`` are expressed in ``timezone`` — the requester's — so the caller
    can render them without another conversion.
    """

    start: datetime
    end: datetime
    timezone: str


@dataclass(frozen=True, slots=True)
class Decision:
    """The result of evaluating one booking request.

    ``code`` is the *first* rule that failed, which keeps branching deterministic;
    ``violations`` carries *all* of them, so a requester who picked a Sunday at 3am is told
    both things at once instead of discovering them one at a time.

    Collections are tuples rather than lists: a decision is a value, and nothing downstream
    has any business mutating one after the fact.
    """

    outcome: Outcome
    code: DecisionCode
    reason: str
    evaluated_at: datetime
    violations: tuple[Violation, ...] = ()
    suggestions: tuple[SlotSuggestion, ...] = field(default=())

    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.ACCEPTED

    @property
    def is_searchable(self) -> bool:
        """Whether proposing alternative slots is meaningful for this decision.

        A request that named an unknown resource or a negative duration gives the slot
        search nothing to work with; there is no "next available" for a question that
        could not be asked.
        """
        return not self.accepted and self.code not in GATING_CODES

    def with_suggestions(self, suggestions: tuple[SlotSuggestion, ...]) -> Decision:
        """Return a copy carrying ``suggestions``.

        The rule chain does not search for alternatives itself — that keeps rule
        evaluation cheap and total. The caller attaches them.
        """
        return replace(self, suggestions=suggestions)
