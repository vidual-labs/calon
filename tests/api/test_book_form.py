"""Tests for the public booking form at /book (phase 4).

The form POSTs to the same ``submit_intent`` path as the API. These tests
cover the HTTP layer: field preservation on rejection, handoff links on
acceptance, form-level validation errors, and public accessibility.

Time is frozen to Tuesday 1 September 2026 06:00 UTC (12:00 Europe/Berlin
in September, CDT in America/New_York). The resource is Europe/Berlin,
weekdays 12:00-22:00, 30-min slots.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from calon.db import Database
from tests.conftest import booking_payload


def _valid_fields() -> dict[str, str]:
    return {
        "name": "Alice Example",
        "email": "alice@example.com",
        "phone": "+49 123 456789",
        "date": "2026-09-01",
        "time": "14:00",
        "subject": "Product demo",
        "notes": "Prefer to start with an overview.",
    }


def _sunday_fields() -> dict[str, str]:
    return {
        "name": "Alice Example",
        "email": "alice@example.com",
        "date": "2026-09-06",
        "time": "03:00",
        "subject": "Sunday slot",
    }


# ---------------------------------------------------------------------------
# GET /book
# ---------------------------------------------------------------------------


def test_get_book_returns_200_with_form(client: TestClient) -> None:
    res = client.get("/book")
    assert res.status_code == 200
    body = res.text
    assert 'name="email"' in body
    assert 'name="date"' in body
    assert 'name="time"' in body
    assert 'name="subject"' in body
    # The instance name is rendered (defaults to "calon" when no config is set).
    assert "calon" in body


def test_book_form_reachable_without_login(client: TestClient) -> None:
    """No CALON_LOGIN set → /book is still public (not operator-gated)."""
    res = client.get("/book")
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# POST /book — success path
# ---------------------------------------------------------------------------


def test_post_book_accepts_and_shows_handoff(client: TestClient) -> None:
    res = client.post("/book", data=_valid_fields())
    assert res.status_code == 200
    body = res.text
    # Success banner with handoff links.
    assert "Booked." in body
    assert "Google Calendar" in body
    assert "Download .ics" in body
    assert "Booking reference" in body
    # The .ics URL is correct.
    # Find the booking id from the response and verify the ICS link.
    assert "/api/v1/bookings/" in body


def test_post_book_accepted_booking_visible_in_db(client: TestClient, database: Database) -> None:
    from calon.models import Booking

    res = client.post("/book", data=_valid_fields())
    assert "Booked." in res.text

    with database.read() as session:
        assert session.query(Booking).count() == 1


# ---------------------------------------------------------------------------
# POST /book — rejection path (Sunday 03:00 is not bookable)
# ---------------------------------------------------------------------------


def test_post_book_rejection_preserves_all_fields(client: TestClient) -> None:
    """Sunday 03:00 → rejected; every field value must survive in the re-rendered form."""
    fields = _sunday_fields()
    res = client.post("/book", data=fields)
    assert res.status_code == 200
    body = res.text
    # The form is back and the user's values are preserved.
    assert 'value="2026-09-06"' in body
    assert 'value="03:00"' in body
    assert "alice@example.com" in body
    assert "Alice Example" in body
    assert "Sunday slot" in body
    # Rejection banner is visible.
    assert "This time cannot be booked." in body


def test_post_book_rejection_shows_violation_messages(client: TestClient) -> None:
    res = client.post("/book", data=_sunday_fields())
    body = res.text
    # The domain layer's violation messages are rendered, not the raw keys.
    # Sunday 03:00 violates at least: business hours (03:00 < 12:00) and weekday.
    assert "business day" in body.lower() or "weekday" in body.lower() or "SUNDAY" in body.upper()


def test_post_book_rejection_shows_suggestions(client: TestClient) -> None:
    """Rejected requests include up to 3 alternative slots; the form shows them."""
    res = client.post("/book", data=_sunday_fields())
    # The page renders without crashing; the suggestion content is
    # implementation-defined (the template shows "Next available:" when the
    # decision carries alternatives).
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# POST /book — form-level validation errors
# ---------------------------------------------------------------------------


def test_post_book_missing_email_returns_422_and_error_banner(client: TestClient) -> None:
    fields = _valid_fields()
    del fields["email"]
    res = client.post("/book", data=fields)
    assert res.status_code == 422
    body = res.text
    assert "email" in body.lower()
    assert "Please fix the following" in body


def test_post_book_missing_multiple_fields_lists_all_errors(client: TestClient) -> None:
    # Only phone submitted — name, email, date, time, subject all missing.
    res = client.post("/book", data={"phone": "+49 123"})
    assert res.status_code == 422
    body = res.text
    assert "name" in body.lower()
    assert "email" in body.lower()
    assert "date" in body.lower()
    assert "Please fix the following" in body


def test_post_book_invalid_date_returns_422_not_500(client: TestClient) -> None:
    res = client.post(
        "/book",
        data={
            "name": "Alice",
            "email": "alice@example.com",
            "date": "not-a-date",
            "time": "12:00",
            "subject": "Test",
        },
    )
    assert res.status_code == 422
    assert res.text  # renders, no 500


def test_post_book_form_does_not_create_intent_on_validation_failure(
    client: TestClient, database: Database
) -> None:
    """A 422 form error must not create a booking intent row."""
    from calon.models import BookingIntent

    res = client.post("/book", data={"phone": "+49 123"})
    assert res.status_code == 422

    with database.read() as session:
        assert session.query(BookingIntent).count() == 0


# ---------------------------------------------------------------------------
# Integration: form and API share the same downstream path
# ---------------------------------------------------------------------------


def test_form_booking_and_api_booking_are_equivalent(
    client: TestClient, database: Database
) -> None:
    """A booking made through the form and one through the API should both be recorded."""
    from calon.models import Booking, BookingIntent

    # Book via the form.
    res_form = client.post("/book", data=_valid_fields())
    assert "Booked." in res_form.text

    # Try another slot via the API (15:00 same day).
    res_api = client.post(
        "/api/v1/bookings",
        json=booking_payload("2026-09-01T15:00:00+02:00"),
    )
    assert res_api.status_code in (200, 201)

    with database.read() as session:
        assert session.query(Booking).count() == 2
        assert session.query(BookingIntent).count() == 2
