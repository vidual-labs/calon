"""The single entry point for creating a booking.

Every intake path — the native API, the booking form in phase 4, an external source's
webhook in phase 6 — converges here (``CLAUDE.md`` §4.2). There is no second path that
could drift, and no scheduling logic anywhere above this function.

What this function owns: the transaction, the translation between rows and domain values,
the audit trail, and writing the outcome down. What it does not own: the decision. That
comes from ``calon.domain.decide`` and is recorded as given.

Idempotency (ADR 0005): the caller may pass ``idempotency_key`` for external intake. The
key is attached to the intent row at insert time so a concurrent retry on the same key
resolves to the same row. The *route* handles the replay — it is the caller that decides
"this pair has already been seen, return the stored decision" — because re-reading from
the database is where the answer comes from, and the route is the only layer that knows
whether a second request is a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from calon.calendarkit import event_uid
from calon.calendars import CalendarProviderRegistry, FreeBusySpan
from calon.domain import AvailabilityPolicy, Decision, decide, to_utc
from calon.domain.rules import BookingRequest
from calon.ids import new_id
from calon.intake.native import NATIVE_SOURCE
from calon.models import Booking, BookingIntent
from calon.schemas import BookingIntentIn, DecisionOut
from calon.services import repository

__all__ = ["AcceptedBooking", "Submission", "submit_intent"]

#: How far either side of the interesting range to load bookings and blackouts. The daily
#: limit rule counts everything on a candidate's *local* day, which can reach a day beyond
#: the range in either direction once timezones are involved.
_MARGIN = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class AcceptedBooking:
    """The booking that was written, when the answer was yes.

    Carries the iCalendar identity minted inside the acceptance transaction (ADR 0004) so
    the handoff can be assembled after the transaction commits, out of band. The ``UID`` is
    deterministic — ``<intent-uuid>@<instance-host>`` — so a requester who re-issues the
    same booking request gets a calendar event that updates in place rather than
    duplicates.
    """

    id: str
    start_utc: datetime
    end_utc: datetime
    status: str
    ics_uid: str
    ics_sequence: int


@dataclass(frozen=True, slots=True)
class Submission:
    """What happened to one booking request.

    ``intent_id`` is always present. A rejection is a recorded outcome, not a request that
    never happened — which is the whole point of keeping rejected intents.
    """

    intent_id: str
    status: str
    decision: Decision
    booking: AcceptedBooking | None = None

    @property
    def accepted(self) -> bool:
        return self.booking is not None


def _decision_to_json(decision: Decision) -> dict[str, Any]:
    """The structured decision in wire form — exactly what ``DecisionOut`` would build.

    Serialized with ``mode="json"`` so datetimes become strings: the stored value must
    survive a round-trip through SQLite's JSON column unchanged. A replay returns this
    value as-is rather than re-building it, so what the source first got is what it gets
    again — including suggestions the requester would want to see (ADR 0005).
    """
    return DecisionOut.of(decision).model_dump(mode="json")


def submit_intent(
    session: Session,
    intent: BookingIntentIn,
    *,
    source: str,
    now: datetime,
    raw_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    instance_host: str = "localhost",
    calendar_registry: CalendarProviderRegistry | None = None,
) -> Submission:
    """Record a booking request, judge it, and write the outcome.

    ``session`` must already be inside the ``BEGIN IMMEDIATE`` transaction that
    ``Database.write()`` opens: rule evaluation and insertion have to be one atomic step,
    or two simultaneous requests for the same slot can both read "free".

    ``instance_host`` is the domain part of the iCalendar ``UID`` that gets minted on
    acceptance (ADR 0004). The route passes it through from ``Settings`` so a re-issue of
    the same booking request produces a calendar event with a stable, predictable identity.

    ``calendar_registry`` (ADR 0009), when the resource has an enabled calendar provider,
    narrows the judge to times the provider does not report as busy. The provider call is
    made *inside* :func:`_free_busy`, which is called from ``judge()`` below, so both the
    initial decision and the post-conflict re-judge see the same provider truth in the same
    write transaction. With no registered provider — the default for every instance that
    has not configured ``[calendars.*]`` — the decision is computed exactly as before
    (CLAUDE.md §2).

    ``idempotency_key`` (ADR 0005) is the external-intake replay key. It is stored on the
    intent row so a concurrent request with the same key and the same source resolves to
    the same row via the unique ``(source, idempotency_key)`` index. The *route* (not this
    function) handles the replay path — that is where the stored response is read back
    and returned. This function is happy to be called twice with the same key; it is the
    route's job to detect that and short-circuit to the stored outcome instead.
    """
    resource_row = repository.find_resource(session, intent.resource_slug)
    known_row = resource_row or repository.any_resource(session)
    if known_row is None:
        raise RuntimeError(
            "no bookable resource is configured; the projection from config/calon.toml "
            "has not run against this database"
        )

    resource = repository.to_domain_resource(known_row)
    policy = repository.load_policy(session, known_row.id)

    requested_start = to_utc(intent.start)
    requested_end = (
        to_utc(intent.end) if intent.end is not None else requested_start + policy.default_duration
    )

    intent_row = _record_intent(
        session,
        intent,
        source=source,
        now=now,
        resource_id=resource_row.id if resource_row is not None else None,
        requested_start=requested_start,
        requested_end=requested_end,
        raw_payload=raw_payload,
        idempotency_key=idempotency_key,
    )

    request = BookingRequest(
        resource_slug=intent.resource_slug,
        start=intent.start,
        timezone=intent.timezone,
        end=intent.end,
    )
    window = _search_window(policy, now=now, start=requested_start, end=requested_end)

    def free_busy() -> tuple[FreeBusySpan, ...]:
        if calendar_registry is None:
            return ()
        return calendar_registry.free_busy(intent.resource_slug, *window)

    def judge() -> Decision:
        return decide(
            request,
            resource=resource,
            policy=policy,
            now=now,
            blackouts=repository.load_blackouts(session, known_row.id, *window),
            existing=repository.load_booked_spans(session, known_row.id, *window),
            free_busy=free_busy(),
        )

    decision = judge()

    booking: AcceptedBooking | None = None
    if decision.accepted:
        block_start, block_end = policy.buffered_span(requested_start, requested_end)
        if repository.has_conflict(session, known_row.id, block_start, block_end):
            # Unreachable while the caller holds the write transaction, and checked anyway:
            # this is the guard that makes double-booking impossible rather than unlikely.
            # Re-judging rather than synthesising a rejection keeps every decision the
            # domain's to make.
            decision = judge()
        else:
            booking = _write_booking(
                session,
                intent_id=intent_row.id,
                resource_id=known_row.id,
                start=requested_start,
                end=requested_end,
                block_start=block_start,
                block_end=block_end,
                now=now,
                instance_host=instance_host,
            )

    _record_outcome(session, intent_row, decision, accepted=booking is not None, now=now)
    _audit_outcome(session, intent_row, decision, booking, source=source, now=now)

    return Submission(
        intent_id=intent_row.id,
        status=intent_row.status,
        decision=decision,
        booking=booking,
    )


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def _search_window(
    policy: AvailabilityPolicy, *, now: datetime, start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    """The span of bookings and blackouts the decision could possibly depend on.

    Wide enough to cover the whole suggestion horizon, because a rejection re-checks the
    complete rule chain against every candidate slot it proposes.
    """
    horizon = now + timedelta(days=policy.max_advance_days)
    return (min(now, start) - _MARGIN, max(horizon, end) + _MARGIN)


def _record_intent(
    session: Session,
    intent: BookingIntentIn,
    *,
    source: str,
    now: datetime,
    resource_id: str | None,
    requested_start: datetime,
    requested_end: datetime,
    raw_payload: dict[str, Any] | None,
    idempotency_key: str | None = None,
) -> BookingIntent:
    """Write down what was asked for, before judging it.

    The intent is recorded first so that a request is on the record even if deciding it
    raises. ``resource_id`` is null when the request named a resource that does not exist,
    which is exactly the case worth being able to find later.

    ``idempotency_key`` is stored on the row so the unique ``(source, idempotency_key)``
    index (migration 0001) can resolve a concurrent retry to this row. Native intake
    passes ``None`` — there is no retry semantics there to be idempotent about.
    """
    row = BookingIntent(
        resource_id=resource_id,
        source=source,
        source_ref=intent.source_ref,
        idempotency_key=idempotency_key,
        requested_start_utc=requested_start,
        requested_end_utc=requested_end,
        requester_timezone=intent.timezone,
        requester_name=intent.requester.name,
        requester_email=intent.requester.email,
        requester_phone=intent.requester.phone,
        subject=intent.subject,
        notes=intent.notes,
        metadata_json=intent.metadata,
        raw_payload_json=raw_payload,
        received_at_utc=now,
        status="pending",
    )
    session.add(row)
    session.flush()

    repository.append_audit(
        session,
        at=now,
        actor=_actor(source),
        event_type="intent.received",
        intent_id=row.id,
        payload={
            "source": source,
            "resource_slug": intent.resource_slug,
            "source_ref": intent.source_ref,
            "requested_start_utc": requested_start.isoformat(),
            "requested_end_utc": requested_end.isoformat(),
        },
    )
    return row


def _write_booking(
    session: Session,
    *,
    intent_id: str,
    resource_id: str,
    start: datetime,
    end: datetime,
    block_start: datetime,
    block_end: datetime,
    now: datetime,
    instance_host: str,
    ics_sequence: int = 0,
) -> AcceptedBooking:
    """Create the booking row and mint its iCalendar identity.

    The ``UID`` is minted here, inside the acceptance transaction, so a double-booking
    can never leave behind a dangling calendar identity, and the value is stable for the
    life of the booking (ADR 0004). It is only *issued* to the requester via the
    handoff endpoint, which is where the ``.ics`` file is rendered.

    The row's own id is generated up front (rather than left to the column default and
    read back after flush) so the ``UID`` can be minted from *this* booking's id —
    ``calendarkit.event_uid(booking_id, instance_host)`` — the same call every other
    reader of a booking's identity makes. Minting it from the booking's *intent* id
    instead, as an earlier version of this function did, produced a stored ``ics_uid``
    that disagreed with the UID every handoff and provider write-back actually used.
    """
    booking_id = new_id()
    ics_uid = event_uid(booking_id, instance_host)
    row = Booking(
        id=booking_id,
        intent_id=intent_id,
        resource_id=resource_id,
        start_utc=start,
        end_utc=end,
        block_start_utc=block_start,
        block_end_utc=block_end,
        status="confirmed",
        created_at_utc=now,
        ics_uid=ics_uid,
        ics_sequence=ics_sequence,
    )
    session.add(row)
    session.flush()
    return AcceptedBooking(
        id=row.id,
        start_utc=start,
        end_utc=end,
        status=row.status,
        ics_uid=ics_uid,
        ics_sequence=ics_sequence,
    )


def _record_outcome(
    session: Session,
    intent_row: BookingIntent,
    decision: Decision,
    *,
    accepted: bool,
    now: datetime,
) -> None:
    intent_row.status = "accepted" if accepted else "rejected"
    intent_row.decision_code = decision.code.value
    intent_row.decision_reason = decision.reason
    intent_row.decided_at_utc = now
    # The complete structured decision, for external-intake replays (ADR 0005). Native
    # intents carry it too — the cost is one JSON column write and the value is free to
    # a future native replay endpoint that would otherwise have to re-derive it.
    intent_row.decision_json = _decision_to_json(decision)
    session.flush()


def _audit_outcome(
    session: Session,
    intent_row: BookingIntent,
    decision: Decision,
    booking: AcceptedBooking | None,
    *,
    source: str,
    now: datetime,
) -> None:
    actor = _actor(source)
    repository.append_audit(
        session,
        at=now,
        actor=actor,
        event_type="intent.accepted" if booking else "intent.rejected",
        intent_id=intent_row.id,
        booking_id=booking.id if booking else None,
        payload={
            "code": decision.code.value,
            "reason": decision.reason,
            "violations": [violation.code.value for violation in decision.violations],
        },
    )

    if booking is not None:
        repository.append_audit(
            session,
            at=now,
            actor=actor,
            event_type="booking.created",
            intent_id=intent_row.id,
            booking_id=booking.id,
            payload={
                "start_utc": booking.start_utc.isoformat(),
                "end_utc": booking.end_utc.isoformat(),
            },
        )


def _actor(source: str) -> str:
    """``system`` for calon's own intake, ``source:<slug>`` for anything external."""
    return "system" if source == NATIVE_SOURCE else f"source:{source}"
