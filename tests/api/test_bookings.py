"""Native intake, end to end: HTTP in, decision and rows out.

These go through the real application and a real SQLite file, because the things worth
checking here are exactly the ones a unit test cannot see — that the intent is recorded,
that the audit trail is written, that the block bounds are materialised, and that a
rejection is still a recorded outcome rather than a request that vanished.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.db import Database
from calon.models import AuditEvent, Booking, BookingIntent
from tests.conftest import NEW_YORK, booking_payload

# Wednesday 2 September 2026, the day after the frozen "now".
TOMORROW_10_00 = "2026-09-02T10:00:00+02:00"
TOMORROW_10_30 = "2026-09-02T10:30:00+02:00"
TOMORROW_10_45 = "2026-09-02T10:45:00+02:00"
SUNDAY_03_00 = "2026-09-06T03:00:00+02:00"


def test_a_valid_request_is_booked_and_returned_in_the_requesters_timezone(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))

    assert response.status_code == 201
    body = response.json()
    assert body["decision"]["outcome"] == "accepted"
    assert body["decision"]["code"] == "ACCEPTED"
    assert body["status"] == "accepted"

    booking = body["booking"]
    assert booking["status"] == "confirmed"
    assert booking["timezone"] == "Europe/Berlin"
    # The default duration is 30 minutes, and the answer comes back in local time.
    assert booking["start"] == "2026-09-02T10:00:00+02:00"
    assert booking["end"] == "2026-09-02T10:30:00+02:00"


def test_an_accepted_booking_materialises_its_buffered_span(
    client: TestClient, database: Database
) -> None:
    client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))

    with database.read() as session:
        booking = session.scalars(select(Booking)).one()

    assert booking.start_utc == datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    assert booking.end_utc == datetime(2026, 9, 2, 8, 30, tzinfo=UTC)
    # buffer_before is 0 and buffer_after is 15 minutes in the default policy.
    assert booking.block_start_utc == booking.start_utc
    assert booking.block_end_utc == datetime(2026, 9, 2, 8, 45, tzinfo=UTC)


def test_the_whole_decision_is_recorded_in_the_audit_log(
    client: TestClient, database: Database
) -> None:
    client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))

    with database.read() as session:
        events = session.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all()

    assert [event.event_type for event in events] == [
        "intent.received",
        "intent.accepted",
        "booking.created",
    ]
    assert {event.actor for event in events} == {"system"}
    assert all(event.intent_id for event in events)


def test_a_rejection_reports_every_broken_rule_and_offers_alternatives(client: TestClient) -> None:
    # A Sunday at 3am fails on the weekday and on the daily window at the same time.
    response = client.post("/api/v1/bookings", json=booking_payload(SUNDAY_03_00))

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["outcome"] == "rejected"
    assert decision["code"] == "WEEKDAY_NOT_ALLOWED"

    codes = [violation["code"] for violation in decision["violations"]]
    assert codes == ["WEEKDAY_NOT_ALLOWED", "OUTSIDE_BUSINESS_HOURS"]

    suggestions = decision["suggestions"]
    assert 1 <= len(suggestions) <= 3
    assert all(slot["timezone"] == "Europe/Berlin" for slot in suggestions)


def test_a_rejected_request_is_still_recorded(client: TestClient, database: Database) -> None:
    response = client.post("/api/v1/bookings", json=booking_payload(SUNDAY_03_00))
    intent_id = response.json()["intent_id"]

    with database.read() as session:
        intent = session.get(BookingIntent, intent_id)
        bookings = session.scalars(select(Booking)).all()

    assert intent is not None
    assert intent.status == "rejected"
    assert intent.decision_code == "WEEKDAY_NOT_ALLOWED"
    assert intent.decided_at_utc is not None
    assert bookings == []


def test_an_unknown_resource_is_rejected_without_guessing_an_alternative(
    client: TestClient, database: Database
) -> None:
    response = client.post(
        "/api/v1/bookings", json=booking_payload(TOMORROW_10_00, resource_slug="does-not-exist")
    )

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["code"] == "RESOURCE_UNKNOWN"
    # A gating rejection carries one violation and no suggestions: there is no "next
    # available" for a question that could not be asked.
    assert len(decision["violations"]) == 1
    assert decision["suggestions"] == []

    with database.read() as session:
        intent = session.scalars(select(BookingIntent)).one()
    assert intent.resource_id is None


def test_the_same_slot_cannot_be_booked_twice(client: TestClient) -> None:
    first = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))
    second = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["decision"]["code"] == "SLOT_CONFLICT"
    assert second.json()["booking"] is None


def test_the_trailing_buffer_protects_the_slot_immediately_after(client: TestClient) -> None:
    client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00))

    # 10:30 starts the moment the first booking ends, but inside its 15-minute buffer.
    butting_up = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_30))
    assert butting_up.json()["decision"]["code"] == "SLOT_CONFLICT"

    # 10:45 is the first start that clears it.
    clear = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_45))
    assert clear.status_code == 201


def test_a_request_in_another_timezone_is_answered_in_that_timezone(client: TestClient) -> None:
    # 04:00 in New York is 10:00 in Berlin, which is inside the window.
    response = client.post(
        "/api/v1/bookings",
        json=booking_payload("2026-09-02T04:00:00-04:00", timezone=NEW_YORK),
    )

    assert response.status_code == 201
    booking = response.json()["booking"]
    assert booking["timezone"] == NEW_YORK
    assert booking["start"] == "2026-09-02T04:00:00-04:00"


def test_a_start_time_without_an_offset_never_becomes_an_intent(
    client: TestClient, database: Database
) -> None:
    response = client.post("/api/v1/bookings", json=booking_payload("2026-09-02T10:00:00"))

    assert response.status_code == 422
    with database.read() as session:
        assert session.scalars(select(BookingIntent)).all() == []


def test_an_unrecognised_timezone_is_turned_away_at_the_edge(
    client: TestClient, database: Database
) -> None:
    response = client.post(
        "/api/v1/bookings", json=booking_payload(TOMORROW_10_00, timezone="Mars/Olympus_Mons")
    )

    assert response.status_code == 422
    with database.read() as session:
        assert session.scalars(select(BookingIntent)).all() == []


def test_unknown_fields_are_refused_rather_than_silently_dropped(client: TestClient) -> None:
    payload = booking_payload(TOMORROW_10_00)
    payload["party_size"] = 4  # calon has no concept of this, and will not pretend to.

    assert client.post("/api/v1/bookings", json=payload).status_code == 422


def test_metadata_is_carried_through_untouched(client: TestClient, database: Database) -> None:
    payload = booking_payload(
        TOMORROW_10_00, metadata={"campaign": "spring", "nested": {"anything": [1, 2]}}
    )
    response = client.post("/api/v1/bookings", json=payload)

    with database.read() as session:
        intent = session.get(BookingIntent, response.json()["intent_id"])

    assert intent is not None
    assert intent.metadata_json == {"campaign": "spring", "nested": {"anything": [1, 2]}}


def test_a_request_below_the_notice_period_is_rejected(client: TestClient) -> None:
    # "Now" is 08:00 in Berlin and the default notice is two hours.
    response = client.post("/api/v1/bookings", json=booking_payload("2026-09-01T09:00:00+02:00"))

    assert response.json()["decision"]["code"] == "BELOW_MIN_NOTICE"


def test_a_request_beyond_the_advance_horizon_is_rejected(client: TestClient) -> None:
    # The default horizon is 60 days.
    response = client.post("/api/v1/bookings", json=booking_payload("2026-12-02T10:00:00+01:00"))

    assert response.json()["decision"]["code"] == "BEYOND_MAX_ADVANCE"


def test_health_reports_the_running_version(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["version"]
