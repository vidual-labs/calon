"""Native booking intake.

One endpoint, and it does not decide anything: it adapts the payload, hands it to
``booking_service.submit_intent``, and renders whatever came back.

A rejection is a successful request that produced a "no" — the intent was recorded, the
rules were applied, and the answer includes both why and what to try instead. It is
``200 OK`` with ``outcome: rejected``, not a client error. ``201 Created`` is reserved for
the case where a booking row actually exists.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Response, status

from calon.api.deps import DatabaseDep
from calon.clock import utcnow
from calon.intake import native
from calon.schemas import BookingIntentIn, BookingOut, BookingResponse, DecisionOut
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
    payload: BookingIntentIn, response: Response, database: DatabaseDep
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
        )

    if submission.accepted:
        response.status_code = status.HTTP_201_CREATED

    return _render(submission, payload.timezone)


def _render(submission: booking_service.Submission, timezone: str) -> BookingResponse:
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

    return BookingResponse(
        intent_id=submission.intent_id,
        status=submission.status,
        decision=DecisionOut.of(submission.decision),
        booking=booking,
    )


def _local(moment: datetime, zone: ZoneInfo) -> datetime:
    """Times go back to the requester in the timezone they asked in, not in UTC."""
    return moment.astimezone(zone)
