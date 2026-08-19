"""The pure scheduling core.

Nothing in this package imports SQLAlchemy, FastAPI, or the filesystem, and nothing reads
the wall clock — ``now`` is always a parameter. That is what makes the scheduling logic
unit-testable with no fixtures and no database, and it is the one architectural rule in
calon that is not negotiable (``CLAUDE.md`` §4).

The public surface is ``decide``: one request in, one ``Decision`` out, alternatives
attached when the answer was no.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from calon.domain.availability import (
    AvailabilityPolicy,
    BlackoutPeriod,
    BookedSpan,
    FreeBusySpan,
    Resource,
    is_valid_timezone,
    to_utc,
)
from calon.domain.decision import (
    Decision,
    DecisionCode,
    Outcome,
    SlotSuggestion,
    Violation,
)
from calon.domain.rules import BookingRequest, evaluate, resolve_end
from calon.domain.slots import MAX_SUGGESTIONS, suggest_slots

__all__ = [
    "MAX_SUGGESTIONS",
    "AvailabilityPolicy",
    "BlackoutPeriod",
    "BookedSpan",
    "BookingRequest",
    "Decision",
    "DecisionCode",
    "FreeBusySpan",
    "Outcome",
    "Resource",
    "SlotSuggestion",
    "Violation",
    "decide",
    "evaluate",
    "is_valid_timezone",
    "resolve_end",
    "suggest_slots",
    "to_utc",
]


def decide(
    request: BookingRequest,
    *,
    resource: Resource,
    policy: AvailabilityPolicy,
    now: datetime,
    blackouts: Sequence[BlackoutPeriod] = (),
    existing: Sequence[BookedSpan] = (),
    free_busy: Sequence[FreeBusySpan] = (),
    limit: int = MAX_SUGGESTIONS,
) -> Decision:
    """Evaluate a request and, on a rejection worth answering, propose alternatives.

    Suggestions are skipped when the request was structurally unusable — there is no "next
    available" for a question that could not be asked.

    ``free_busy`` (ADR 0009) is provider-reported busy time; it is threaded through to
    both the decision and the slot search so a rejected request is only proposed
    alternatives that are not already taken in the resource's external calendar, and the
    suggestions themselves are honest. An empty ``free_busy`` leaves behaviour identical
    to the pre-phase-9 path (CLAUDE.md §2).
    """
    decision = evaluate(
        request,
        resource=resource,
        policy=policy,
        now=now,
        blackouts=blackouts,
        existing=existing,
        free_busy=free_busy,
    )
    if not decision.is_searchable:
        return decision
    return decision.with_suggestions(
        suggest_slots(
            request,
            resource=resource,
            policy=policy,
            now=now,
            blackouts=blackouts,
            existing=existing,
            free_busy=free_busy,
            limit=limit,
        )
    )
