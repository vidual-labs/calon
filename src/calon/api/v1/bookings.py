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

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session

from calon.api.deps import AuthorisedOperator, DatabaseDep, SettingsDep
from calon.calendarkit import CalendarEvent, build_deeplinks, build_ics, event_uid, ics_filename
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
) -> BookingResponse:
    """Record a booking request, apply the rules, and book it if it passes."""
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
        )

        handoff_context: _HandoffContext | None = None
        if submission.booking is not None:
            handoff_context = _fetch_handoff_context(session, booking_id=submission.booking.id)

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
        event = _event_for(row, intent, instance_host=settings.instance_host)

        body = build_ics(event, now=utcnow())
    filename = ics_filename(booking_id)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _event_for(booking: Booking, intent: BookingIntent, *, instance_host: str) -> CalendarEvent:
    """The ``CalendarEvent`` this booking + intent pair maps to.

    One place to build it, so ``_render`` (which needs the UID and links) and
    ``get_calendar_ics`` (which needs the file bytes) stay consistent. Bookings do not
    carry their own location, so ``location`` is always ``None`` here; the ``.ics`` file
    simply omits the ``LOCATION`` line, which is the correct behaviour for a booking with
    no physical address.
    """
    return CalendarEvent(
        booking_id=booking.id,
        instance_host=instance_host,
        sequence=booking.ics_sequence or 0,
        title=intent.subject,
        description=intent.notes or "",
        location=None,
        start_utc=booking.start_utc,
        end_utc=booking.end_utc,
        timezone=intent.requester_timezone,
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
        zone = ZoneInfo(timezone)
        booking = BookingOut(
            id=submission.booking.id,
            start=_local(submission.booking.start_utc, zone),
            end=_local(submission.booking.end_utc, zone),
            timezone=timezone,
            status=submission.booking.status,
        )
        if handoff_context is not None:
            booking.calendar = _build_handoff(
                handoff_context.booking, handoff_context.intent, settings=settings, now=now
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
    event = _event_for(booking, intent, instance_host=settings.instance_host)
    links = build_deeplinks(event)
    return CalendarHandoff(
        ics_url=f"{settings.base_url}/api/v1/bookings/{booking.id}/calendar.ics",
        ics_filename=ics_filename(booking.id),
        uid=event_uid(booking.id, settings.instance_host),
        sequence=booking.ics_sequence or 0,
        links=CalendarLinksOut(**links),
    )


def _local(moment: datetime, zone: ZoneInfo) -> datetime:
    """Times go back to the requester in the timezone they asked in, not in UTC."""
    return moment.astimezone(zone)
