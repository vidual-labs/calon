"""Cancelling a booking from the operator dashboard, end to end (ADR 0020).

``POST /admin/bookings/{id}/cancel`` marks the booking ``cancelled`` — which frees its slot,
because conflict detection only counts confirmed bookings — audits it, and then removes the
event from a connected calendar. The calendar removal runs after the cancellation commits and
can never undo it, the same degrade-not-fail contract as the write-back that created the
event (CLAUDE.md §2). Calendars here are :class:`calon.calendars.FakeCalendar`; no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.calendars import CalendarProviderRegistry, FakeCalendar
from calon.config import Settings
from calon.db import Database
from calon.main import create_app
from calon.models import AuditEvent, Booking
from calon.web import _load_intents
from tests.conftest import NOW, booking_payload

LOGIN = "op-key-123"

# NOW is Tuesday 1 Sep 2026 06:00 UTC; Wednesday 10:00 Berlin is a bookable slot.
WEDNESDAY_10_00 = "2026-09-02T10:00:00+02:00"
WEDNESDAY_10_30 = "2026-09-02T10:30:00+02:00"
# Sunday is outside the default weekdays, so a request for it is rejected.
SUNDAY_10_00 = "2026-09-06T10:00:00+02:00"


def _settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)


@pytest.fixture
def operator_client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in operator on an instance with no calendar configured."""
    with (
        time_machine.travel(NOW, tick=False),
        TestClient(create_app(_settings(tmp_path))) as test_client,
    ):
        response = test_client.post("/login", json={"login": LOGIN})
        assert response.status_code in (200, 302, 303), response.text
        yield test_client


def _database(client: TestClient) -> Database:
    db: Database = client.app.state.db  # type: ignore[attr-defined]
    return db


def _install_provider(client: TestClient, provider: FakeCalendar) -> None:
    client.app.state.calendar_registry = CalendarProviderRegistry(  # type: ignore[attr-defined]
        {"default": provider}
    )


def _book(client: TestClient, start: str = WEDNESDAY_10_00, end: str = WEDNESDAY_10_30) -> str:
    response = client.post("/api/v1/bookings", json=booking_payload(start, end))
    assert response.status_code == 201, response.text
    booking_id: str = response.json()["booking"]["id"]
    return booking_id


def _cancel(client: TestClient, booking_id: str) -> int:
    response = client.post(f"/admin/bookings/{booking_id}/cancel", follow_redirects=False)
    status_code: int = response.status_code
    return status_code


def _audit(client: TestClient, event_type: str) -> list[AuditEvent]:
    with _database(client).read() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.seq)
            ).all()
        )


class TestCancel:
    def test_cancelling_marks_the_booking_cancelled_and_audits_it(
        self, operator_client: TestClient
    ) -> None:
        booking_id = _book(operator_client)

        assert _cancel(operator_client, booking_id) == 303

        with _database(operator_client).read() as session:
            booking = session.get(Booking, booking_id)
            assert booking is not None
            assert booking.status == "cancelled"
            assert booking.cancelled_at_utc == NOW
        events = _audit(operator_client, "booking.cancelled")
        assert len(events) == 1
        assert events[0].actor == "operator"
        assert events[0].booking_id == booking_id

    def test_the_cancelled_slot_can_be_booked_again(self, operator_client: TestClient) -> None:
        booking_id = _book(operator_client)
        taken = operator_client.post(
            "/api/v1/bookings", json=booking_payload(WEDNESDAY_10_00, WEDNESDAY_10_30)
        )
        assert taken.json()["decision"]["code"] == "SLOT_CONFLICT"

        _cancel(operator_client, booking_id)

        _book(operator_client)

    def test_the_cancelled_slot_is_offered_again_by_availability(
        self, operator_client: TestClient
    ) -> None:
        def offered() -> bool:
            response = operator_client.get(
                "/api/v1/availability",
                params={
                    "resource_slug": "default",
                    "from": "2026-09-02T09:00:00+02:00",
                    "to": "2026-09-02T12:00:00+02:00",
                },
            )
            starts = [slot["start"] for slot in response.json()["slots"]]
            return WEDNESDAY_10_00 in starts

        booking_id = _book(operator_client)
        assert not offered()

        _cancel(operator_client, booking_id)

        assert offered()

    def test_cancelling_twice_is_harmless(self, operator_client: TestClient) -> None:
        booking_id = _book(operator_client)

        assert _cancel(operator_client, booking_id) == 303
        assert _cancel(operator_client, booking_id) == 303

        assert len(_audit(operator_client, "booking.cancelled")) == 1

    def test_an_unknown_booking_is_404(self, operator_client: TestClient) -> None:
        assert _cancel(operator_client, "no-such-booking") == 404

    def test_cancelling_requires_the_operator_login(self, tmp_path: Path) -> None:
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(_settings(tmp_path))) as anonymous,
        ):
            booking_id = _book(anonymous)

            assert _cancel(anonymous, booking_id) == 401

            with _database(anonymous).read() as session:
                booking = session.get(Booking, booking_id)
                assert booking is not None
                assert booking.status == "confirmed"


class TestCalendarRemoval:
    def test_the_event_is_removed_from_a_connected_calendar(
        self, operator_client: TestClient
    ) -> None:
        provider = FakeCalendar()
        _install_provider(operator_client, provider)
        booking_id = _book(operator_client)
        assert len(provider.events("default")) == 1

        _cancel(operator_client, booking_id)

        assert provider.events("default") == {}
        assert len(_audit(operator_client, "booking.calendar_removed")) == 1
        with _database(operator_client).read() as session:
            intents = _load_intents(session)
        assert intents[0]["calendar_sync"] == "removed"

    def test_a_failed_removal_leaves_the_booking_cancelled(
        self, operator_client: TestClient
    ) -> None:
        provider = FakeCalendar()
        _install_provider(operator_client, provider)
        booking_id = _book(operator_client)
        provider.fail_remove = True

        assert _cancel(operator_client, booking_id) == 303

        with _database(operator_client).read() as session:
            booking = session.get(Booking, booking_id)
            assert booking is not None
            assert booking.status == "cancelled"
            intents = _load_intents(session)
        assert len(_audit(operator_client, "booking.calendar_remove_failed")) == 1
        assert intents[0]["calendar_sync"] == "remove_failed"
        assert intents[0]["calendar_sync_detail"] == "FakeCalendar is configured to fail remove"

    def test_cancelling_twice_removes_the_event_once(self, operator_client: TestClient) -> None:
        provider = FakeCalendar()
        _install_provider(operator_client, provider)
        booking_id = _book(operator_client)

        _cancel(operator_client, booking_id)
        _cancel(operator_client, booking_id)

        assert len(_audit(operator_client, "booking.calendar_removed")) == 1

    def test_with_no_calendar_nothing_is_removed_or_audited(
        self, operator_client: TestClient
    ) -> None:
        booking_id = _book(operator_client)

        _cancel(operator_client, booking_id)

        assert _audit(operator_client, "booking.calendar_removed") == []
        assert _audit(operator_client, "booking.calendar_remove_failed") == []


class TestDashboard:
    def test_a_confirmed_booking_offers_the_cancel_button(
        self, operator_client: TestClient
    ) -> None:
        booking_id = _book(operator_client)

        page = operator_client.get("/admin").text

        assert f'action="/admin/bookings/{booking_id}/cancel"' in page

    def test_a_cancelled_booking_shows_as_cancelled_without_the_button(
        self, operator_client: TestClient
    ) -> None:
        booking_id = _book(operator_client)
        _cancel(operator_client, booking_id)

        page = operator_client.get("/admin").text

        assert ">Cancelled<" in page
        assert f'action="/admin/bookings/{booking_id}/cancel"' not in page
        assert f"/api/v1/bookings/{booking_id}/calendar.ics" not in page

    def test_a_rejected_request_shows_as_rejected(self, operator_client: TestClient) -> None:
        # Regression: a rejected request has no booking row, and the dashboard used to
        # read its status from the booking alone, so it showed as "Queued".
        response = operator_client.post("/api/v1/bookings", json=booking_payload(SUNDAY_10_00))
        assert response.json()["decision"]["outcome"] == "rejected"

        page = operator_client.get("/admin").text

        assert ">Rejected<" in page
        assert ">Queued<" not in page
