"""The operator-initiated "Connect with Google" flow (ADR 0014), against a real database.

``start_connect`` and ``complete_connect``/``disconnect`` are exercised directly (not
through HTTP) so these tests pin the service layer's own behaviour: what
:class:`CalendarNotConfiguredError` is raised for, that the persisted credential and the
live registry always agree, and that a reconnect updates rather than duplicates. The web
route tests in ``tests/api/test_calendar_connect.py`` cover the HTTP-facing wrapping
around this (redirects, the signed ``state``, error rendering).

The token exchange itself is a scripted ``httpx.MockTransport`` client passed in — no
network, mirroring every other calendar test in this codebase.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.calendars import CalendarProviderRegistry
from calon.calendars.google import GoogleCalendarProvider
from calon.clock import utcnow
from calon.config import CalendarProviderConfig, OperatorConfig
from calon.db import Database
from calon.models import CalendarCredentialRow
from calon.services import calendar_connect_service

_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _config(**overrides: object) -> OperatorConfig:
    defaults: dict[str, object] = {
        "slug": "default",
        "provider": "google",
        "calendar_id": "you@example.com",
        "enabled": True,
        "client_id": "cid",
        "client_secret": "csecret",
    }
    defaults.update(overrides)
    return OperatorConfig(calendars={"default": CalendarProviderConfig(**defaults)})  # type: ignore[arg-type]


def _mock_client(refresh_token: str = "fresh-refresh-token") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "tok",
                "expires_in": 3600,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


#: ``start_connect`` reads the dashboard-entered OAuth client (ADR 0016) when the TOML has
#: no entry, so it needs a session even in the cases that never reach the database.
_REDIRECT_URI = "https://calon.example.com/calendars/google/callback"


class TestStartConnect:
    def test_builds_a_google_consent_url(self, client: TestClient, database: Database) -> None:
        with database.read() as session:
            url = calendar_connect_service.start_connect(
                session,
                _config(),
                resource_slug="default",
                redirect_uri=_REDIRECT_URI,
                signing_key=b"0" * 32,
            )
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=cid" in url

    def test_an_unconfigured_resource_raises(self, client: TestClient, database: Database) -> None:
        with (
            database.read() as session,
            pytest.raises(calendar_connect_service.CalendarNotConfiguredError, match="default"),
        ):
            calendar_connect_service.start_connect(
                session,
                OperatorConfig(),
                resource_slug="default",
                redirect_uri=_REDIRECT_URI,
                signing_key=b"0" * 32,
            )

    def test_a_microsoft_resource_raises(self, client: TestClient, database: Database) -> None:
        config = _config(provider="microsoft")
        with (
            database.read() as session,
            pytest.raises(calendar_connect_service.CalendarNotConfiguredError, match="Google only"),
        ):
            calendar_connect_service.start_connect(
                session,
                config,
                resource_slug="default",
                redirect_uri=_REDIRECT_URI,
                signing_key=b"0" * 32,
            )

    def test_missing_client_credentials_raises(
        self, client: TestClient, database: Database
    ) -> None:
        config = _config(client_id="", client_secret="")
        with (
            database.read() as session,
            pytest.raises(calendar_connect_service.CalendarNotConfiguredError, match="client_id"),
        ):
            calendar_connect_service.start_connect(
                session,
                config,
                resource_slug="default",
                redirect_uri=_REDIRECT_URI,
                signing_key=b"0" * 32,
            )


class TestCompleteConnect:
    def test_persists_the_credential_and_installs_the_provider(
        self, client: TestClient, database: Database
    ) -> None:
        registry = CalendarProviderRegistry()
        config = _config()

        with database.write() as session:
            result = calendar_connect_service.complete_connect(
                session,
                registry,
                config,
                resource_slug="default",
                code="auth-code",
                redirect_uri="https://calon.example.com/calendars/google/callback",
                now=utcnow(),
                client=_mock_client("token-1"),
            )

        assert result.resource_slug == "default"
        assert result.provider == "google"

        provider = registry.provider_for("default")
        assert isinstance(provider, GoogleCalendarProvider)
        assert provider._store.refresh_token == "token-1"

        with database.read() as session:
            row = session.get(CalendarCredentialRow, "default")
            assert row is not None
            assert row.provider == "google"
            assert row.refresh_token == "token-1"
            assert row.connected_at_utc == utcnow()

    def test_a_second_connect_updates_the_existing_row_rather_than_duplicating(
        self, client: TestClient, database: Database
    ) -> None:
        registry = CalendarProviderRegistry()
        config = _config()

        with database.write() as session:
            calendar_connect_service.complete_connect(
                session,
                registry,
                config,
                resource_slug="default",
                code="auth-code-1",
                redirect_uri="https://calon.example.com/calendars/google/callback",
                now=utcnow(),
                client=_mock_client("token-1"),
            )
        with database.write() as session:
            calendar_connect_service.complete_connect(
                session,
                registry,
                config,
                resource_slug="default",
                code="auth-code-2",
                redirect_uri="https://calon.example.com/calendars/google/callback",
                now=utcnow(),
                client=_mock_client("token-2"),
            )

        with database.read() as session:
            rows = session.scalars(select(CalendarCredentialRow)).all()
            assert len(rows) == 1
            assert rows[0].refresh_token == "token-2"

        provider = registry.provider_for("default")
        assert isinstance(provider, GoogleCalendarProvider)
        assert provider._store.refresh_token == "token-2"

    def test_an_unconfigured_resource_raises_and_writes_nothing(
        self, client: TestClient, database: Database
    ) -> None:
        registry = CalendarProviderRegistry()

        with (
            pytest.raises(calendar_connect_service.CalendarNotConfiguredError),
            database.write() as session,
        ):
            calendar_connect_service.complete_connect(
                session,
                registry,
                OperatorConfig(),
                resource_slug="default",
                code="auth-code",
                redirect_uri="https://calon.example.com/calendars/google/callback",
                now=utcnow(),
                client=_mock_client(),
            )

        with database.read() as session:
            assert session.get(CalendarCredentialRow, "default") is None
        assert registry.provider_for("default") is None


class TestDisconnect:
    def test_removes_the_credential_and_the_live_provider(
        self, client: TestClient, database: Database
    ) -> None:
        registry = CalendarProviderRegistry()
        config = _config()
        with database.write() as session:
            calendar_connect_service.complete_connect(
                session,
                registry,
                config,
                resource_slug="default",
                code="auth-code",
                redirect_uri="https://calon.example.com/calendars/google/callback",
                now=utcnow(),
                client=_mock_client(),
            )

        with database.write() as session:
            removed = calendar_connect_service.disconnect(
                session, registry, resource_slug="default"
            )

        assert removed is True
        assert registry.provider_for("default") is None
        with database.read() as session:
            assert session.get(CalendarCredentialRow, "default") is None

    def test_disconnecting_an_unconnected_resource_returns_false(
        self, client: TestClient, database: Database
    ) -> None:
        registry = CalendarProviderRegistry()
        with database.write() as session:
            removed = calendar_connect_service.disconnect(
                session, registry, resource_slug="default"
            )
        assert removed is False
