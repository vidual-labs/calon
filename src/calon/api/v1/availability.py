"""The advisory availability read.

This publishes free/busy times and nothing else — never a requester, a subject, or any
booking content. It is a read: it holds nothing, and a caller that treats a slot it saw
here as theirs will still lose the race, correctly, at submit time (ADR 0007).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import AwareDatetime

from calon.api.deps import DatabaseDep
from calon.clock import utcnow
from calon.schemas import AvailabilityResponse, SlotOut
from calon.services import availability_service

router = APIRouter(tags=["availability"])


@router.get(
    "/availability",
    response_model=AvailabilityResponse,
    summary="List free slots in a window",
)
def get_availability(
    database: DatabaseDep,
    resource_slug: Annotated[str, Query(description="Which bookable resource.")],
    range_start: Annotated[
        AwareDatetime,
        Query(alias="from", description="Start of the window. Must carry a UTC offset."),
    ],
    range_end: Annotated[
        AwareDatetime,
        Query(
            alias="to",
            description=(
                "End of the window, at most "
                f"{availability_service.MAX_RANGE_DAYS} days after `from`. "
                "Slots must finish by this instant."
            ),
        ),
    ],
    timezone: Annotated[
        str | None,
        Query(description="IANA timezone to express slots in. Defaults to the resource's."),
    ] = None,
    duration_min: Annotated[
        int | None,
        Query(gt=0, description="Slot length in minutes. Defaults to the resource's."),
    ] = None,
) -> AvailabilityResponse:
    """Every slot that would be accepted right now, in the window asked about.

    Advisory only: these are not held, and nothing here reserves anything.
    """
    with database.read() as session:
        try:
            availability = availability_service.find_availability(
                session,
                resource_slug=resource_slug,
                range_start=range_start,
                range_end=range_end,
                timezone=timezone,
                duration_min=duration_min,
                now=utcnow(),
            )
        except availability_service.UnknownResourceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except availability_service.InvalidRangeError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return AvailabilityResponse(
        resource_slug=availability.resource_slug,
        timezone=availability.timezone,
        **{"from": availability.range_start, "to": availability.range_end},
        duration_min=availability.duration_min,
        evaluated_at=availability.evaluated_at,
        slots=[
            SlotOut(start=slot.start, end=slot.end, timezone=slot.timezone)
            for slot in availability.slots
        ],
    )
