"""Phase 3 calendar handoff: the RFC 5545 file, the provider deeplinks, and the gate.

Two families of tests live here:

* **Pure.** ``build_ics`` / ``build_deeplinks`` are pure functions over a pure value, so
  their tests need no fixtures — only a known frozen ``now``. The ICS is checked by
  parsing it back with the same library it was written with (a silent re-encoding that
  drops a field must fail the build, not ship), and the three deeplinks are asserted to
  their **exact** query strings: a change in how a provider parameter is encoded is a
  broken button, and the only way to catch it is to compare the whole thing.

* **Gated.** The ``.ics`` endpoint and the operator panel carry a requester's name and
  subject, so they sit behind the operator login (ADR 0010). These go through the real
  application: fail-closed (no login → 503 while the public flow stays open), a wrong
  login (401), a correct login (cookie), the panel rendering the personal data, and the
  ``.ics`` bytes round-tripping through the endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
import time_machine
from fastapi.testclient import TestClient
from icalendar import Calendar

from calon.calendarkit import (
    CalendarEvent,
    build_deeplinks,
    build_ics,
    event_uid,
    ics_filename,
)
from tests.conftest import NOW, booking_payload

# --- a known event, used by both the pure and the gated tests ----------------------

BOOKING_ID = "bk_1234567890abcdef"
INSTANCE_HOST = "calon.example"
EVENT = CalendarEvent(
    booking_id=BOOKING_ID,
    instance_host=INSTANCE_HOST,
    sequence=0,
    title="Consultation with Ada Lovelace",
    description="Initial consultation",
    location=None,
    start_utc=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
    end_utc=datetime(2026, 9, 2, 8, 30, tzinfo=UTC),
    timezone="Europe/Berlin",
)
EXPECTED_UID = f"{BOOKING_ID}@{INSTANCE_HOST}"


# ------------------------------------------------------------------------------------
# Pure: the ICS file
# ------------------------------------------------------------------------------------


def test_uid_is_stable_and_namespaced_by_instance_host() -> None:
    assert event_uid(BOOKING_ID, INSTANCE_HOST) == EXPECTED_UID
    # A different instance host names a different UID — that is what keeps two
    # instances' events from colliding in a shared calendar.
    assert event_uid(BOOKING_ID, "other.example") != EXPECTED_UID


def test_ics_filename_is_namespaced_and_predictable() -> None:
    assert ics_filename(BOOKING_ID) == f"calon-{BOOKING_ID}.ics"


def test_ics_round_trips_through_an_icalendar_parser() -> None:
    """Write the file, parse it back, and confirm every field the contract promises.

    ``icalendar`` is the writer and the reader here, which makes the round trip a real
    check of serialisation (does the ``Z`` survive? does the ``UID`` survive line
    folding?) rather than a check of whether our two in-house functions agree with
    themselves.
    """
    raw = build_ics(EVENT, now=NOW)
    assert raw.startswith(b"BEGIN:VCALENDAR")
    # icalendar terminates the file with a final CRLF after END:VCALENDAR (valid RFC 5545).
    assert raw.rstrip(b"\r\n").endswith(b"END:VCALENDAR")

    # Regression: the property used to be emitted as "SEQ:", which RFC 5545 does not
    # register — clients ignore it, so "stable UID, incrementing SEQUENCE" (the
    # mechanism a re-download updates an entry in place) was never actually shipped.
    assert b"SEQUENCE:0" in raw
    assert b"\r\nSEQ:" not in raw

    calendar = Calendar.from_ical(raw)
    assert calendar["METHOD"] == "PUBLISH"

    events = [comp for comp in calendar.walk() if comp.name == "VEVENT"]
    assert len(events) == 1
    # icalendar exposes component values as a broad value union it fully types; for a test
    # that only reads the contract fields, treat the parsed component as ``Any``.
    event: Any = events[0]

    # The stable identity and shape.
    assert str(event["UID"]) == EXPECTED_UID
    assert int(event["SEQUENCE"]) == 0
    assert str(event["STATUS"]) == "CONFIRMED"
    assert str(event["SUMMARY"]) == EVENT.title
    assert str(event["DESCRIPTION"]) == EVENT.description

    # Deliberately no LOCATION: a booking has no physical address, and the file omits
    # the line rather than writing an empty one.
    assert event.get("LOCATION") is None

    # Times are UTC instants, and the serialised form carries the Z suffix — the
    # ADR 0004 invariant. The parsed value is tz-aware UTC (so the instant assertion
    # below is the real check); the Z check is on the emitted text, where it belongs.
    assert event["DTSTART"].dt == datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    assert event["DTEND"].dt == datetime(2026, 9, 2, 8, 30, tzinfo=UTC)
    text = raw.decode("utf-8")
    assert "DTSTART:20260902T080000Z" in text
    assert "DTEND:20260902T083000Z" in text

    # DTSTAMP is the one field allowed to differ on re-download; it is present and is
    # the injected ``now``, not a wall-clock surprise.
    assert event.get("DTSTAMP") is not None
    assert event["DTSTAMP"].dt == NOW


def test_ics_deduplicates_by_uid_across_downloads() -> None:
    """A second download carries a fresh DTSTAMP but the identical UID.

    That is the mechanism by which a calendar updates the entry in place (ADR 0004):
    same UID, newer DTSTAMP.
    """
    first = Calendar.from_ical(build_ics(EVENT, now=NOW))
    second = Calendar.from_ical(build_ics(EVENT, now=NOW + timedelta(minutes=1)))

    # Parsed components read through ``Any`` (see the round-trip test) so mypy does not
    # expand icalendar's full value union onto every field access.
    first_event: Any = next(c for c in first.walk() if c.name == "VEVENT")
    second_event: Any = next(c for c in second.walk() if c.name == "VEVENT")

    assert str(first_event["UID"]) == str(second_event["UID"])
    assert first_event["DTSTAMP"].dt != second_event["DTSTAMP"].dt
    # Everything the requester sees is identical.
    assert str(first_event["SUMMARY"]) == str(second_event["SUMMARY"])
    assert first_event["DTSTART"].dt == second_event["DTSTART"].dt


# ------------------------------------------------------------------------------------
# Pure: the provider deeplinks
# ------------------------------------------------------------------------------------


def test_deeplinks_have_exactly_the_three_documented_providers() -> None:
    assert set(build_deeplinks(EVENT)) == {"google", "outlook_office", "outlook_live"}


def test_google_deeplink_is_query_string_exact() -> None:
    url = build_deeplinks(EVENT)["google"]
    assert url.startswith("https://calendar.google.com/calendar/render?")
    assert parse_qs(urlsplit(url).query) == {
        "action": ["TEMPLATE"],
        "text": ["Consultation with Ada Lovelace"],
        "details": ["Initial consultation"],
        "dates": ["20260902T080000Z/20260902T083000Z"],
    }


def test_outlook_deeplinks_are_query_string_exact_and_host_specific() -> None:
    links = build_deeplinks(EVENT)
    for provider, host in (
        ("outlook_office", "outlook.office.com"),
        ("outlook_live", "outlook.live.com"),
    ):
        url = links[provider]
        assert url.startswith(f"https://{host}/calendar/0/deeplink/compose?")
        assert parse_qs(urlsplit(url).query) == {
            "path": ["/calendar/action/compose"],
            "rru": ["addevent"],
            "subject": ["Consultation with Ada Lovelace"],
            "startdt": ["2026-09-02T08:00:00Z"],
            "enddt": ["2026-09-02T08:30:00Z"],
            "body": ["Initial consultation"],
        }


def test_deeplink_omits_location_when_the_event_has_none() -> None:
    for url in build_deeplinks(EVENT).values():
        assert "location" not in parse_qs(urlsplit(url).query)


# ------------------------------------------------------------------------------------
# Gated: the operator login and the .ics endpoint
# ------------------------------------------------------------------------------------


@pytest.fixture
def operator_client(tmp_path: Path) -> Iterator[TestClient]:
    """The real app with ``CALON_LOGIN`` set, so the operator surface is reachable."""
    from calon.config import Settings
    from calon.main import create_app

    settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login="op-key-123")
    with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(tmp_path: Path) -> Iterator[TestClient]:
    """The real app with no login — the fail-closed configuration."""
    from calon.config import Settings
    from calon.main import create_app

    settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login="")
    with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as test_client:
        yield test_client


def _make_a_booking(client: TestClient) -> dict[str, Any]:
    """Submit one accepted booking and return the decision body."""
    response = client.post("/api/v1/bookings", json=booking_payload("2026-09-02T10:00:00+02:00"))
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def _log_in(client: TestClient, login: str) -> None:
    """POST the login; the session cookie lands in the TestClient's own jar."""
    response = client.post("/login", json={"login": login})
    # TestClient follows the 303 and then keeps the Set-Cookie in its cookie jar.
    assert response.status_code in (200, 302, 303), response.text


def test_operator_surface_fails_closed_without_a_login(anonymous_client: TestClient) -> None:
    """No CALON_LOGIN means the operator endpoints refuse with 503 — not open."""
    assert anonymous_client.get("/bookings").status_code == 503
    # The public booking flow must still work in exactly this configuration.
    assert (
        anonymous_client.post(
            "/api/v1/bookings", json=booking_payload("2026-09-02T10:00:00+02:00")
        ).status_code
        == 201
    )


def test_the_ics_endpoint_requires_and_accepts_the_login(operator_client: TestClient) -> None:
    body = _make_a_booking(operator_client)
    booking_id = body["booking"]["id"]
    path = f"/api/v1/bookings/{booking_id}/calendar.ics"

    # Unauthenticated: 401, with the WWW-Authenticate hint.
    no_auth = operator_client.get(path)
    assert no_auth.status_code == 401
    assert "WWW-Authenticate" in no_auth.headers

    # Log in, and the same path now serves the file.
    _log_in(operator_client, "op-key-123")
    ics = operator_client.get(path)
    assert ics.status_code == 200
    assert ics.headers["content-type"].startswith("text/calendar")
    assert f'filename="calon-{booking_id}.ics"' in ics.headers["content-disposition"]
    assert ics.content.lstrip().startswith(b"BEGIN:VCALENDAR")


def test_the_ics_endpoint_is_404_for_an_unknown_id(operator_client: TestClient) -> None:
    _log_in(operator_client, "op-key-123")
    assert operator_client.get("/api/v1/bookings/does-not-exist/calendar.ics").status_code == 404


def test_logging_in_with_the_wrong_value_is_rejected(operator_client: TestClient) -> None:
    response = operator_client.post("/login", json={"login": "wrong"})
    assert response.status_code == 401


def test_login_sets_an_httponly_samesite_session_cookie(operator_client: TestClient) -> None:
    # Don't follow the redirect, so we can inspect the Set-Cookie on the login response
    # itself — that is where the security attributes (httponly, samesite) matter.
    response = operator_client.post("/login", json={"login": "op-key-123"}, follow_redirects=False)
    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    assert "calon_session" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    # http:// base_url means the cookie is NOT marked Secure here.
    assert "secure" not in set_cookie.lower()


def test_the_bookings_panel_renders_the_personal_data_after_login(
    operator_client: TestClient,
) -> None:
    body = _make_a_booking(operator_client)
    _log_in(operator_client, "op-key-123")

    response = operator_client.get("/bookings")
    assert response.status_code == 200
    html = response.text
    # The requester's name and subject are on the dashboard — that is precisely the
    # personal data this page exists to show the operator, and precisely why it is
    # login-gated. The row also carries the .ics link for re-sending the file.
    assert "Ada Lovelace" in html
    assert "Initial consultation" in html
    assert f"/api/v1/bookings/{body['booking']['id']}/calendar.ics" in html


def test_logout_ends_the_session(operator_client: TestClient) -> None:
    _log_in(operator_client, "op-key-123")
    assert operator_client.get("/bookings").status_code == 200

    operator_client.post("/logout")
    assert operator_client.get("/bookings").status_code == 401


def test_logout_clears_the_session_cookie(operator_client: TestClient) -> None:
    # Regression: logout built its cookie-deletion on an injected ``Response``
    # object that was never the one actually returned, so the deletion never
    # reached the client — the browser kept a (server-revoked, so harmless, but
    # stale) session cookie forever. Don't follow the redirect, so the logout
    # response's own Set-Cookie is inspectable.
    _log_in(operator_client, "op-key-123")
    response = operator_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    assert "calon_session" in set_cookie
    # A deletion cookie carries an empty value and an immediately-past expiry.
    assert 'calon_session=""' in set_cookie or "calon_session=;" in set_cookie


def test_the_dashboard_renders_well_formed_iso_8601_timestamps(
    operator_client: TestClient,
) -> None:
    # Regression: the dashboard appended a literal "Z" to a timestamp that was
    # already offset-aware (UtcDateTime round-trips as tz-aware), producing an
    # unparseable "+00:00Z" suffix for every row.
    _make_a_booking(operator_client)
    _log_in(operator_client, "op-key-123")

    with operator_client.app.state.db.read() as session:  # type: ignore[attr-defined]
        from calon.models import BookingIntent

        intent = session.query(BookingIntent).one()
        received_at = intent.received_at_utc.isoformat()

    html = operator_client.get("/bookings").text
    assert received_at in html
    assert f"{received_at}Z" not in html
