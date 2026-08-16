"""The configuration file is the source of truth, and the tables follow it.

ADR 0008 says ``config/calon.toml`` wins at every startup, and that the rows are a
projection of it. These tests hold that claim to account from both ends: that editing the
file and restarting changes what calon will accept, and that restarting *without* editing
it changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from calon.db import Database
from calon.models import AvailabilityPolicyRow, BlackoutPeriodRow, Booking, ResourceRow
from tests.conftest import BootFn, booking_payload

WEDNESDAY_10_00 = "2026-09-02T10:00:00+02:00"
WEDNESDAY_18_00 = "2026-09-02T18:00:00+02:00"


def db_of(client: TestClient) -> Database:
    database: Database = client.app.state.db  # type: ignore[attr-defined]
    return database


def test_the_configuration_is_projected_onto_the_tables(boot: BootFn) -> None:
    body = """
    [resource]
    slug = "studio"
    name = "Recording studio"
    timezone = "Europe/Berlin"

    [availability]
    allowed_weekdays = [2]
    window_start = "12:00"
    window_end = "20:00"

    [[blackout]]
    date = "2026-09-09"
    reason = "Maintenance"
    """

    with boot(body) as client, db_of(client).read() as session:
        resource = session.scalars(select(ResourceRow)).one()
        policy = session.get(AvailabilityPolicyRow, resource.id)
        blackouts = session.scalars(select(BlackoutPeriodRow)).all()

    assert resource.slug == "studio"
    assert resource.name == "Recording studio"
    assert policy is not None
    assert policy.allowed_weekdays == "2"
    assert policy.window_start == "12:00"
    assert policy.window_end == "20:00"
    assert [blackout.reason for blackout in blackouts] == ["Maintenance"]


def test_the_rules_in_the_file_are_the_rules_that_are_applied(boot: BootFn) -> None:
    body = """
    [availability]
    window_start = "17:00"
    window_end = "21:00"
    """

    with boot(body) as client:
        # 10:00 is inside the default window but outside the configured one.
        rejected = client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_10_00))
        accepted = client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_18_00))

    assert rejected.json()["decision"]["code"] == "OUTSIDE_BUSINESS_HOURS"
    assert accepted.status_code == 201


def test_a_configured_blackout_closes_that_day(boot: BootFn) -> None:
    body = """
    [[blackout]]
    date = "2026-09-02"
    reason = "Company offsite"
    """

    with boot(body) as client:
        response = client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_10_00))

    decision = response.json()["decision"]
    assert decision["code"] == "BLACKOUT_PERIOD"
    assert "Company offsite" in decision["reason"]


def test_restarting_without_editing_the_file_changes_nothing(boot: BootFn) -> None:
    body = """
    [[blackout]]
    date = "2026-12-24"
    reason = "Christmas Eve"
    """

    with boot(body):
        pass
    with boot(body) as client, db_of(client).read() as session:
        resources = session.scalar(select(func.count()).select_from(ResourceRow))
        policies = session.scalar(select(func.count()).select_from(AvailabilityPolicyRow))
        blackouts = session.scalar(select(func.count()).select_from(BlackoutPeriodRow))

    assert (resources, policies, blackouts) == (1, 1, 1)


def test_editing_the_file_and_restarting_applies_the_new_rules(boot: BootFn) -> None:
    with boot('[availability]\nwindow_end = "17:00"\n') as client:
        assert (
            client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_18_00)).json()[
                "decision"
            ]["code"]
            == "OUTSIDE_BUSINESS_HOURS"
        )

    with boot('[availability]\nwindow_end = "21:00"\n') as client:
        assert (
            client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_18_00)).status_code
            == 201
        )


def test_tightening_the_rules_does_not_unmake_an_existing_booking(boot: BootFn) -> None:
    """A booking was accepted under the rules in force at the time. It stays accepted."""
    with boot('[availability]\nwindow_end = "21:00"\n') as client:
        booked = client.post("/api/v1/bookings", json=booking_payload(WEDNESDAY_18_00))
        assert booked.status_code == 201

    with (
        boot('[availability]\nwindow_end = "17:00"\n') as client,
        db_of(client).read() as session,
    ):
        bookings = session.scalars(select(Booking)).all()
        assert len(bookings) == 1
        assert bookings[0].start_utc == datetime(2026, 9, 2, 16, 0, tzinfo=UTC)


def test_a_resource_the_file_stops_mentioning_is_closed_rather_than_deleted(boot: BootFn) -> None:
    with boot('[resource]\nslug = "studio"\n') as client:
        assert (
            client.post(
                "/api/v1/bookings", json=booking_payload(WEDNESDAY_10_00, resource_slug="studio")
            ).status_code
            == 201
        )

    with boot('[resource]\nslug = "office"\n') as client:
        response = client.post(
            "/api/v1/bookings", json=booking_payload(WEDNESDAY_10_00, resource_slug="studio")
        )
        with db_of(client).read() as session:
            studio = session.scalars(select(ResourceRow).where(ResourceRow.slug == "studio")).one()
            bookings = session.scalars(select(Booking)).all()

    assert response.json()["decision"]["code"] == "RESOURCE_UNKNOWN"
    assert studio.is_active is False
    # Its history is still there.
    assert len(bookings) == 1
