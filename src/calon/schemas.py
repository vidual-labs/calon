"""The canonical contracts.

These models are calon's public API. Changing one is an API change, not a refactor
(``CLAUDE.md`` §4.6): adding an optional field is additive, but removing a field, renaming
one, or changing its type is breaking and belongs in the changelog under **BREAKING:**.

:class:`BookingIntentIn` is the shape every source must produce — the native form, the
native API, and every external adapter alike. It is the only input the scheduling core
understands, which is what keeps "adapters translate, adapters never decide" enforceable:
an adapter's whole job is to land here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from calon.domain import Decision, DecisionCode, Outcome, is_valid_timezone

__all__ = [
    "AvailabilityResponse",
    "BookingIntentIn",
    "BookingOut",
    "BookingResponse",
    "CalendarHandoff",
    "CalendarLinksOut",
    "DecisionOut",
    "RequesterIn",
    "SlotOut",
    "ViolationOut",
]

Timezone = Annotated[str, Field(min_length=1, max_length=64, examples=["Europe/Berlin"])]


def _validate_timezone(value: str) -> str:
    """Reject an unknown timezone at the edge rather than as a scheduling decision.

    A request naming a timezone that does not exist is malformed, not unbookable. Turning
    it away here keeps it out of the audit log, which should record real booking attempts
    rather than typos.
    """
    if not is_valid_timezone(value):
        raise ValueError(f"{value!r} is not a recognised IANA timezone name")
    return value


# --------------------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------------------


class RequesterIn(BaseModel):
    """Who is asking. None of this reaches the scheduling core."""

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=64)

    @field_validator("email")
    @classmethod
    def _plausible_email(cls, value: str) -> str:
        """A deliberately shallow check.

        Full RFC 5322 validation means a dependency, and it would still not tell us the
        address exists. This catches the mistakes a person actually makes in a form field
        and leaves the rest to whether they ever receive anything.
        """
        local, _, domain = value.partition("@")
        if not local or not domain or "@" in domain or " " in value or "." not in domain:
            raise ValueError("must be an email address")
        return value


class BookingIntentIn(BaseModel):
    """What every source must produce. The one input the scheduling core understands."""

    model_config = ConfigDict(extra="forbid")

    resource_slug: str = Field(min_length=1, max_length=64, examples=["default"])
    start: AwareDatetime = Field(description="When the booking should start. Must carry an offset.")
    end: AwareDatetime | None = Field(
        default=None,
        description="Omit to use the resource's default duration.",
    )
    timezone: Timezone = Field(description="IANA timezone of the requester, for display.")
    requester: RequesterIn
    subject: str = Field(min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=5_000)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque passthrough from the source. Never read by scheduling logic.",
    )
    source_ref: str | None = Field(
        default=None,
        max_length=200,
        description="The source's own identifier for this request.",
    )

    _check_timezone = field_validator("timezone")(_validate_timezone)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


class ViolationOut(BaseModel):
    """One rule the request failed."""

    code: DecisionCode
    message: str


class SlotOut(BaseModel):
    """A bookable slot, expressed in the timezone the caller asked in."""

    start: datetime
    end: datetime
    timezone: str


class DecisionOut(BaseModel):
    """The structured accept/reject result.

    ``code`` is the first rule that failed, so a caller can branch on one value;
    ``violations`` carries all of them, so a requester is told everything at once.

    ``calendar_synced`` (ADR 0009): informational flag set ``True`` when the post-acceptance
    provider write-back succeeded, ``False`` when it degraded (provider failure or no
    provider configured). It never affects the booking's own status or the decision's code.
    """

    outcome: Outcome
    code: DecisionCode
    reason: str
    evaluated_at: datetime
    violations: list[ViolationOut] = Field(default_factory=list)
    suggestions: list[SlotOut] = Field(default_factory=list)
    calendar_synced: bool = False

    @classmethod
    def of(cls, decision: Decision) -> DecisionOut:
        return cls(
            outcome=decision.outcome,
            code=decision.code,
            reason=decision.reason,
            evaluated_at=decision.evaluated_at,
            violations=[
                ViolationOut(code=violation.code, message=violation.message)
                for violation in decision.violations
            ],
            suggestions=[
                SlotOut(start=slot.start, end=slot.end, timezone=slot.timezone)
                for slot in decision.suggestions
            ],
            calendar_synced=decision.calendar_synced,
        )


class BookingOut(BaseModel):
    """A booking that was written. Present only when the decision was to accept.

    ``start`` and ``end`` are in the requester's timezone. Buffers are not shown: the
    buffered span widens conflict detection and is none of the requester's business.
    """

    id: str
    start: datetime
    end: datetime
    timezone: str
    status: str
    # The handoff the event should be added to. Present once the ICS file and
    # provider deeplinks are built (phase 3); ``null`` in earlier builds so the field is
    # additive and never a breaking change once it exists.
    calendar: CalendarHandoff | None = None


class CalendarLinksOut(BaseModel):
    """Provider deeplinks for the accepted event.

    Three keys, always present: ``google``, ``outlook_office`` (work/school accounts),
    and ``outlook_live`` (personal Outlook.com). The query strings are exact and golden
    tested; see ``docs/calendar-handoff.md``.
    """

    google: str
    outlook_office: str
    outlook_live: str


class CalendarHandoff(BaseModel):
    """What the requester needs to put the accepted booking onto their calendar.

    Two layers (``docs/calendar-handoff.md``, ADR 0004): the ``ics_url`` is the
    baseline and reaches every calendar that has ever read RFC 5545; the ``links`` are a
    convenience, one click per provider, and are lossy (they carry no ``UID`` and cannot
    deduplicate). ``ics_filename`` is the ``Content-Disposition`` filename the handler
    sends with the file.
    """

    ics_url: str
    ics_filename: str
    uid: str
    sequence: int
    links: CalendarLinksOut


class BookingResponse(BaseModel):
    """The answer to a booking request, accepted or not.

    An intent is recorded either way, so ``intent_id`` is always present — a rejection is
    a recorded outcome rather than a request that never happened.
    """

    intent_id: str
    status: str
    decision: DecisionOut
    booking: BookingOut | None = None


class AvailabilityResponse(BaseModel):
    """Free slots in a window.

    **Advisory only.** These are free as of ``evaluated_at`` and are not held, locked, or
    reserved in any way; the authoritative answer is what happens when a booking is
    submitted. See ``docs/adr/0007-availability-is-an-advisory-read.md``.
    """

    model_config = ConfigDict(populate_by_name=True)

    resource_slug: str
    timezone: str
    range_start: datetime = Field(alias="from")
    range_end: datetime = Field(alias="to")
    duration_min: int
    evaluated_at: datetime
    slots: list[SlotOut] = Field(default_factory=list)
