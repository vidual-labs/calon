"""Calendar secrets are stored encrypted, and need a key to be stored at all (ADR 0019).

These go through the real application and a real database file, because what is pinned
is what ends up *on disk*: a subscribed feed address and a dashboard-entered client secret
never appear there in plain text, an old plain-text value is encrypted at the next start,
a rotated key keeps everything readable, and a missing or wrong key degrades a resource
rather than stopping calon.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import time_machine
from fastapi.testclient import TestClient

from calon.clock import utcnow
from calon.config import Settings
from calon.db import Database
from calon.main import create_app
from calon.models import CalendarFeedRow, CalendarOAuthClientRow
from calon.security.secretbox import SEALED_PREFIX, SecretKeyError
from tests.conftest import NOW, booking_payload

LOGIN = "op-key-123"
KEY_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
#: Standard-alphabet base64, as ``openssl rand -base64 32`` prints it.
KEY_B = "+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/+/8="
FEED_URL = "https://calendar.example.com/secret-token/basic.ics"


@contextmanager
def _running(
    db_path: Path, *, key: str | None = None, previous: str | None = None
) -> Iterator[TestClient]:
    """Start calon on ``db_path`` with the given keys, logged in; leaving it is a stop."""
    settings = Settings(
        db_path=db_path,
        config_path=None,
        login=LOGIN,
        secret_key=key,
        secret_key_previous=previous,
        base_url="http://testserver",
    )
    with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as client:
        client.post("/login", json={"login": LOGIN})
        yield client


def _stored_feed_url(db_path: Path) -> str:
    database = Database.from_path(db_path)
    try:
        with database.read() as session:
            row = session.get(CalendarFeedRow, "default")
            assert row is not None
            return row.url
    finally:
        database.dispose()


def _subscribe(client: TestClient) -> str:
    response = client.post(
        "/calendars/default/feed", data={"feed_url": FEED_URL}, follow_redirects=False
    )
    return str(response.headers["location"])


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "calon.db"


def test_a_subscribed_feed_address_is_stored_encrypted(db_path: Path) -> None:
    with _running(db_path, key=KEY_A) as client:
        assert _subscribe(client).startswith("/admin?calendar_subscribed=default")
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]

    stored = _stored_feed_url(db_path)
    assert stored.startswith(SEALED_PREFIX)
    assert "secret-token" not in stored
    # The raw file is what a backup carries: the address must not be in it anywhere.
    assert b"secret-token" not in db_path.read_bytes()


def test_a_dashboard_entered_client_secret_is_stored_encrypted(db_path: Path) -> None:
    with _running(db_path, key=KEY_A) as client:
        client.post(
            "/calendars/default/oauth-client",
            data={
                "client_id": "cid",
                "client_secret": "GOCSPX-very-secret",
                "calendar_id": "you@example.com",
            },
        )
        database: Database = client.app.state.db  # type: ignore[attr-defined]
        with database.read() as session:
            row = session.get(CalendarOAuthClientRow, "default")
            assert row is not None
            assert row.client_secret.startswith(SEALED_PREFIX)
            assert "very-secret" not in row.client_secret


def test_without_a_key_the_dashboard_stores_nothing_and_says_why(db_path: Path) -> None:
    with _running(db_path) as client:
        html = client.get("/admin").text
        assert "CALON_SECRET_KEY" in html
        assert 'action="/calendars/default/feed"' not in html  # the form is not offered

        location = _subscribe(client)
        assert "calendar_error=" in location
        assert "CALON_SECRET_KEY" in client.get(location).text
        assert client.app.state.calendar_registry.provider_for("default") is None  # type: ignore[attr-defined]

        saved = client.post(
            "/calendars/default/oauth-client",
            data={"client_id": "cid", "client_secret": "s", "calendar_id": "you@example.com"},
            follow_redirects=False,
        )
        assert "calendar_error=" in saved.headers["location"]

        database: Database = client.app.state.db  # type: ignore[attr-defined]
        with database.read() as session:
            assert session.get(CalendarFeedRow, "default") is None
            assert session.get(CalendarOAuthClientRow, "default") is None

        # Booking itself never needs the key (CLAUDE.md §2).
        booked = client.post("/api/v1/bookings", json=booking_payload("2026-09-02T10:00:00+02:00"))
        assert booked.status_code == 201


def test_a_plain_text_value_from_before_encryption_is_encrypted_at_the_next_start(
    db_path: Path,
) -> None:
    with _running(db_path):
        pass
    database = Database.from_path(db_path)
    with database.write() as session:
        session.add(
            CalendarFeedRow(
                resource_slug="default",
                url=FEED_URL,
                created_at_utc=utcnow(),
                updated_at_utc=utcnow(),
            )
        )
    database.dispose()

    # Without a key the legacy value still works as it always did.
    with _running(db_path) as client:
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]
    assert _stored_feed_url(db_path) == FEED_URL

    with _running(db_path, key=KEY_A) as client:
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]
    assert _stored_feed_url(db_path).startswith(SEALED_PREFIX)


def test_rotating_the_key_re_encrypts_under_the_new_one(db_path: Path) -> None:
    with _running(db_path, key=KEY_A) as client:
        _subscribe(client)
    sealed_under_a = _stored_feed_url(db_path)

    with _running(db_path, key=KEY_B, previous=KEY_A) as client:
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]
    sealed_under_b = _stored_feed_url(db_path)
    assert sealed_under_b != sealed_under_a

    # The previous key can now be dropped.
    with _running(db_path, key=KEY_B) as client:
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]


def test_a_missing_key_degrades_the_resource_instead_of_stopping_calon(db_path: Path) -> None:
    with _running(db_path, key=KEY_A) as client:
        _subscribe(client)

    for key in (None, KEY_B):  # no key at all, and the wrong key
        with _running(db_path, key=key) as client:
            assert client.get("/healthz").status_code == 200
            assert client.app.state.calendar_registry.provider_for("default") is None  # type: ignore[attr-defined]
            assert "Credentials unreadable" in client.get("/admin").text

    # Nothing was destroyed: the right key brings the calendar back.
    with _running(db_path, key=KEY_A) as client:
        assert client.app.state.calendar_registry.provider_for("default") is not None  # type: ignore[attr-defined]


def test_a_malformed_key_stops_startup(db_path: Path) -> None:
    with pytest.raises(SecretKeyError, match="openssl rand -base64 32"):
        create_app(Settings(db_path=db_path, config_path=None, secret_key="too-short"))
