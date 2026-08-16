"""Daylight saving, over HTTP.

``tests/domain/test_dst.py`` proves the rule chain and the slot grid survive a transition.
This proves the whole stack does — that nothing between the endpoint and the domain
re-derives a local time, and that the UTC instants written to the database are the ones the
requester's wall clock meant.

Europe/Berlin ends summer time at 03:00 on Sunday 25 October 2026, when clocks go back to
02:00. Weekends are opened in the configuration here, because the default policy is
weekdays only and the transition falls on a Sunday.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from calon.models import Booking
from tests.conftest import BootFn, booking_payload

OPEN_EVERY_DAY = """
[availability]
allowed_weekdays = [0, 1, 2, 3, 4, 5, 6]
window_start = "09:00"
window_end = "17:00"
"""

TRANSITION_DAY_09_00 = "2026-10-25T09:00:00+01:00"
TRANSITION_DAY_17_00 = "2026-10-25T17:00:00+01:00"


def test_the_grid_keeps_its_local_alignment_across_the_transition(boot: BootFn) -> None:
    with boot(OPEN_EVERY_DAY) as client:
        response = client.get(
            "/api/v1/availability",
            params={
                "resource_slug": "default",
                "from": TRANSITION_DAY_09_00,
                "to": TRANSITION_DAY_17_00,
            },
        )

    slots = response.json()["slots"]
    # The window is anchored to local wall-clock time, so the day still opens at 09:00 and
    # still yields 09:00 to 16:30 on a 15-minute grid — now at +01:00, summer time over.
    assert slots[0]["start"] == "2026-10-25T09:00:00+01:00"
    assert slots[-1]["start"] == "2026-10-25T16:30:00+01:00"
    assert len(slots) == 31


def test_a_booking_after_the_transition_is_stored_at_the_instant_the_requester_meant(
    boot: BootFn,
) -> None:
    with boot(OPEN_EVERY_DAY) as client:
        response = client.post(
            "/api/v1/bookings", json=booking_payload("2026-10-25T10:00:00+01:00")
        )
        assert response.status_code == 201

        database = client.app.state.db  # type: ignore[attr-defined]
        with database.read() as session:
            booking = session.scalars(select(Booking)).one()

    # 10:00 CET is 09:00 UTC. The same wall-clock time a week earlier would have been 08:00.
    assert booking.start_utc == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    assert response.json()["booking"]["start"] == "2026-10-25T10:00:00+01:00"


def test_the_day_before_the_transition_is_an_hour_offset_from_the_day_after(boot: BootFn) -> None:
    """The same local hour, either side of the change, is a different instant in UTC."""
    with boot(OPEN_EVERY_DAY) as client:
        before = client.post("/api/v1/bookings", json=booking_payload("2026-10-24T10:00:00+02:00"))
        after = client.post("/api/v1/bookings", json=booking_payload("2026-10-25T10:00:00+01:00"))
        assert (before.status_code, after.status_code) == (201, 201)

        database = client.app.state.db  # type: ignore[attr-defined]
        with database.read() as session:
            bookings = session.scalars(select(Booking).order_by(Booking.start_utc)).all()

    assert bookings[0].start_utc == datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    assert bookings[1].start_utc == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
