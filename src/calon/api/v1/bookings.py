"""Native booking intake, and the calendar handoff behind it.

One intake endpoint, and it does not decide anything: it adapts the payload, hands it to
``booking_service.submit_intent``, and renders whatever came back.

A rejection is a successful request that produced a "no" — the intent was recorded, the
rules were applied, and the answer includes both why and what to try instead. It is
``200 OK`` with ``outcome: rejected``, not a client error. ``201 Created`` is reserved for
the case where a booking row actually exists.

On acceptance two things happen that did not before the calendar-handoff phase:

* The response carries ``booking.calendar`` — the ``.ics`` URL, the provider deeplinks,
  and the stable ``UID``. This is the baseline handoff (ADR 0004).
* ``GET /bookings/{id}/calendar.ics`` serves the RFC 5545 file itself. It is gate: it
  embeds the requester's name and subject, which are personal data, so it is never a
  public route (ADR 0010).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session

from calon.api.deps import AuthorisedOperator, CalendarRegistryDep, DatabaseDep, SettingsDep
from calon.calendarkit import build_deeplinks, build_ics, event_for, event_uid, ics_filename
from calon.clock import utcnow
from calon.config import Settings
from calon.intake import native
from calon.models import Booking, BookingIntent
from calon.schemas import (
    BookingIntentIn,
    BookingOut,
    BookingResponse,
    CalendarHandoff,
    CalendarLinksOut,
    DecisionOut,
)
from calon.services import booking_service

from . import _calendar_writeback

logger = logging.getLogger(__name__)


router = APIRouter(tags=["bookings"])


@router.post(
    "/bookings",
    response_model=BookingResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a booking request",
    responses={
        status.HTTP_200_OK: {"description": "The request was judged and rejected."},
        status.HTTP_201_CREATED: {"description": "The request was accepted and booked."},
    },
)
def submit_booking(
    payload: BookingIntentIn,
    response: Response,
    database: DatabaseDep,
    settings: SettingsDep,
    calendar_registry: CalendarRegistryDep,
) -> BookingResponse:
    """Record a booking request, apply the rules, and book it if it passes.

    On acceptance, after the write transaction commits, the route triggers a calendar
    provider write-back (ADR 0009). The write-back runs *outside* the transaction
    deliberately: the provider is a network hop, and a failing provider must never hold
    the DB lock or roll the booking back. On failure the write-back is audited as
    ``booking.calendar_sync_failed`` and logged with a stack trace; the response still
    reflects the 201 and the booking stays booked.
    """
    intent, raw_payload = native.adapt(payload)
    now = utcnow()

    with database.write() as session:
        submission = booking_service.submit_intent(
            session,
            intent,
            source=native.NATIVE_SOURCE,
            now=now,
            raw_payload=raw_payload,
            instance_host=settings.instance_host,
            calendar_registry=calendar_registry,
        )

        handoff_context: _HandoffContext | None = None
        if submission.booking is not None:
            handoff_context = _fetch_handoff_context(session, booking_id=submission.booking.id)

    # Post-commit write-back (ADR 0009). Outside the transaction on purpose: the
    # provider is a network hop, and a failing provider must not hold the DB lock.
    if submission.booking is not None and handoff_context is not None:
        synced = _calendar_writeback.perform_write_back(
            database,
            calendar_registry,
            booking=submission.booking,
            intent=handoff_context.intent,
            now=now,
        )

        # ``None`` means no provider is configured for this resource, so no write-back
        # was attempted and the decision is left untouched. Only a real sync outcome
        # (True = synced, False = degraded) updates the response flag.
        if synced is not None:
            submission = replace(
                submission,
                decision=submission.decision.with_calendar_synced(synced),
            )

    if submission.accepted:
        response.status_code = status.HTTP_201_CREATED

    return _render(
        submission,
        payload.timezone,
        settings=settings,
        handoff_context=handoff_context,
        now=now,
    )


@router.get(
    "/bookings/{booking_id}/calendar.ics",
    response_class=Response,
    summary="Serve the accepted booking as a calendar file",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No booking with that id, or it was rejected."},
    },
)
def get_calendar_ics(
    booking_id: str,
    database: DatabaseDep,
    settings: SettingsDep,
    _operator: AuthorisedOperator,
) -> Response:
    """The RFC 5545 file for one accepted booking.

    Login-gated: the file embeds the requester's name and subject. ``DTSTAMP`` is set from
    the moment of the request so a second download carries a fresh timestamp while the
    ``UID`` stays the same and calendars deduplicate (ADR 0004).
    """
    with database.read() as session:
        row = session.get(Booking, booking_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no booking with id {booking_id!r}")

        intent = session.get(BookingIntent, row.intent_id)
        if intent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no intent for booking {booking_id!r}")

        event = event_for(row, intent, instance_host=settings.instance_host)
        body = build_ics(event, now=utcnow())

    filename = ics_filename(booking_id)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _render(
    submission: booking_service.Submission,
    timezone: str,
    *,
    settings: Settings,
    handoff_context: _HandoffContext | None,
    now: datetime,
) -> BookingResponse:
    booking = None
    if submission.booking is not None:
        calendar = None
        if handoff_context is not None:
            calendar = _build_handoff(
                handoff_context.booking, handoff_context.intent, settings=settings, now=now
            )
        booking = BookingOut.of(
            id=submission.booking.id,
            start_utc=submission.booking.start_utc,
            end_utc=submission.booking.end_utc,
            status=submission.booking.status,
            timezone=timezone,
            calendar=calendar,
        )

    return BookingResponse(
        intent_id=submission.intent_id,
        status=submission.status,
        decision=DecisionOut.of(submission.decision),
        booking=booking,
    )


@dataclass(frozen=True, slots=True)
class _HandoffContext:
    """The booking + intent pair ``_render`` needs to build the handoff after commit."""

    booking: Booking
    intent: BookingIntent


def _fetch_handoff_context(session: Session, *, booking_id: str) -> _HandoffContext:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "handoff context missing for a booking that was just written",
        )

    intent = session.get(BookingIntent, booking.intent_id)
    if intent is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "handoff context missing for a booking that was just written",
        )

    return _HandoffContext(booking=booking, intent=intent)


def _build_handoff(
    booking: Booking, intent: BookingIntent, *, settings: Settings, now: datetime
) -> CalendarHandoff:
    """Build the handoff from a committed booking + its intent.

    ``base_url`` is where the requester reaches the ``.ics``; ``instance_host`` is the
    ``UID``'s domain. They are independent and can differ (``CALON_INSTANCE_HOST`` is
    long-lived state, ``base_url`` is the deployment's current address).
    """
    event = event_for(booking, intent, instance_host=settings.instance_host)
    links = build_deeplinks(event)
    return CalendarHandoff(
        ics_url=f"{settings.base_url}/api/v1/bookings/{booking.id}/calendar.ics",
        ics_filename=ics_filename(booking.id),
        uid=event_uid(booking.id, settings.instance_host),
        sequence=booking.ics_sequence or 0,
        links=CalendarLinksOut(**links),
    )
