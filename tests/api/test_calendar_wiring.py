"""Batch 3 wiring: the whole calendar-sync path, end to end.

These tests exercise the real application over a real SQLite file with a
:class:`calon.calendars.FakeCalendar` swapped into the running app's provider registry —
no HTTP mocks, no network. ``FakeCalendar`` is a deterministic in-memory
:class:`CalendarProvider` (seeded busy spans, recorded upserts, an optional forced
failure), so each test can drive exactly one provider behaviour at a time.

Reaching the registry per request: the ``get_calendar_registry`` dependency reads
``request.app.state.calendar_registry`` at request time, so replacing that attribute on
the live ``TestClient`` app before a request is enough to make the route, the availability
and booking services, and the post-commit write-back all use the fake.

The three areas covered:

* **availability with a provider** — a provider busy span hides the matching slots; a
  failing provider degrades to calon-only availability without an error.
* **bookings with a provider** — an accepted booking is written back to the provider and
  audited ``booking.calendar_synced``; ``calendar_synced`` in the decision is ``True``;
  a failing write-back degrades (the booking stays accepted, ``calendar_synced`` is
  ``False``, and the failure is audited ``booking.calendar_sync_failed``); a resource with
  *no* provider is a silent no-op (no write-back, no audit, ``calendar_synced`` absent /
  ``False``).
* **standalone regression** — the default app (no calendars configured) books and lists
  availability exactly as before; nothing in the wiring changes a provider-less instance's
  behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.calendars import CalendarProviderRegistry, FakeCalendar
from calon.db import Database
from calon.models import AuditEvent, Booking
from tests.conftest import booking_payload

__all__: list[str] = []

# A window fully inside the default booking window. NOW is Tuesday 1 Sep 2026 06:00 UTC;
# 10:00 local (08:00 UTC) is a working-day slot on the 15-minute grid.
TOMORROW_10_00 = "2026-09-02T10:00:00+02:00"
TOMORROW_10_30 = "2026-09-02T10:30:00+02:00"
WEDNESDAY_10_00 = "2026-09-02T10:00:00+02:00"
WEDNESDAY_10_30 = "2026-09-02T10:30:00+02:00"
WEDNESDAY_11_00 = "2026-09-02T11:00:00+02:00"


def _install_provider(
    client: TestClient, provider: FakeCalendar, *, resource_slug: str = "default"
) -> None:
    """Swap the live app's provider registry for one holding ``provider``.

    The dependency reads ``app.state.calendar_registry`` at request time, so replacing the
    whole registry (rather than mutating the boot-built one) keeps the change scoped and
    the boot-built registry untouched for other assertions.
    """
    client.app.state.calendar_registry = CalendarProviderRegistry({resource_slug: provider})


def _audit_types(client: TestClient, database: Database) -> list[str]:
    with database.read() as session:
        return [
            event.event_type
            for event in session.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all()
        ]


# --------------------------------------------------------------------------------------
# Availability with a provider
# --------------------------------------------------------------------------------------


def test_availability_hides_slots_the_provider_reports_busy(
    client: TestClient,
) -> None:
    provider = FakeCalendar()
    # A busy span exactly over 10:00-10:30 local (08:00-08:30 UTC).
    provider.seed_busy(
        "default",
        datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 2, 8, 30, tzinfo=UTC),
    )
    _install_provider(client, provider)

    slots = client.get(
        "/api/v1/availability",
        params={"resource_slug": "default", "from": WEDNESDAY_10_00, "to": WEDNESDAY_11_00},
    ).json()["slots"]
    starts = [slot["start"] for slot in slots]

    # 10:00 local overlaps the busy span 08:00-08:30 UTC, so that slot is hidden.
    assert WEDNESDAY_10_00 not in starts
    # The window still returns other, bookable slots.
    assert starts


def test_availability_degrades_to_calon_only_when_the_provider_fails(
    client: TestClient,
) -> None:
    provider = FakeCalendar()
    provider.fail_free_busy = True
    _install_provider(client, provider)

    # A failing provider must not break the read; availability still succeeds.
    response = client.get(
        "/api/v1/availability",
        params={"resource_slug": "default", "from": WEDNESDAY_10_00, "to": WEDNESDAY_11_00},
    )

    assert response.status_code == 200
    # Degrade to calon-only: slots are still offered (no provider data to hide them).
    assert response.json()["slots"]


# --------------------------------------------------------------------------------------
# Bookings with a provider
# --------------------------------------------------------------------------------------


def test_an_accepted_booking_is_written_back_and_audited(
    client: TestClient, database: Database
) -> None:
    provider = FakeCalendar()
    _install_provider(client, provider)

    response = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00, TOMORROW_10_30))

    assert response.status_code == 201
    # The response reports the sync succeeded.
    assert response.json()["decision"]["calendar_synced"] is True

    with database.read() as session:
        booking = session.scalars(select(Booking)).one()

    # The event landed in the provider, keyed by the booking's iCal UID.
    synced = provider.event("default", booking.ics_uid)
    assert synced is not None
    assert synced.uid == booking.ics_uid
    assert synced.starts_at_utc == booking.start_utc

    # The write-back is audited, and only after the booking was committed.
    types = _audit_types(client, database)
    assert "booking.created" in types
    assert "booking.calendar_synced" in types
    assert types.index("booking.calendar_synced") > types.index("booking.created")


def test_a_failing_write_back_degrades_without_rolling_back_the_booking(
    client: TestClient, database: Database
) -> None:
    provider = FakeCalendar()
    provider.fail_upsert = True
    _install_provider(client, provider)

    response = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00, TOMORROW_10_30))

    # Degrade-not-fail: the booking is still confirmed even though the write-back failed.
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    assert body["decision"]["outcome"] == "accepted"
    # The decision records that the sync degraded (provider was asked, it failed).
    assert body["decision"]["calendar_synced"] is False

    # The failure is audited as a sync failure, not dropped.
    types = _audit_types(client, database)
    assert "booking.calendar_sync_failed" in types
    # The event never reached the provider.
    assert provider.events("default") == {}


def test_a_resource_with_no_provider_is_a_silent_no_op(
    client: TestClient, database: Database
) -> None:
    # No provider is installed on the live app (the boot-built registry is empty by
    # default), so the write-back must be a silent no-op.
    assert client.app.state.calendar_registry.provider_for("default") is None

    response = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00, TOMORROW_10_30))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    # Nothing was synced, so the flag is not True and no sync audit exists.
    assert body["decision"]["calendar_synced"] is False
    assert "booking.calendar_synced" not in _audit_types(client, database)
    assert "booking.calendar_sync_failed" not in _audit_types(client, database)


# --------------------------------------------------------------------------------
# Standalone regression
# --------------------------------------------------------------------------------


def test_standalone_booking_behaviour_is_unchanged_by_the_wiring(
    client: TestClient, database: Database
) -> None:
    # The default app has no calendars configured (an empty registry), exactly like an
    # unconfigured standalone instance. A booking behaves as before Phase 9.
    response = client.post("/api/v1/bookings", json=booking_payload(TOMORROW_10_00, TOMORROW_10_30))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    assert body["decision"]["outcome"] == "accepted"
    assert body["decision"]["code"] == "ACCEPTED"

    # The audit trail is exactly the three pre-Phase-9 events, in order.
    assert _audit_types(client, database) == [
        "intent.received",
        "intent.accepted",
        "booking.created",
    ]


def test_standalone_availability_is_unchanged_by_the_wiring(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/availability",
        params={"resource_slug": "default", "from": WEDNESDAY_10_00, "to": WEDNESDAY_11_00},
    )

    assert response.status_code == 200
    # A working-day window still lists slots on the grid, calendar-independent.
    assert response.json()["slots"]
