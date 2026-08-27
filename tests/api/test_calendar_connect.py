"""The operator dashboard's Google Calendar connect flow, end to end (ADR 0014).

Three routes, all gated by the operator login exactly like the rest of the dashboard:
``GET /calendars/{slug}/connect`` (redirect to Google), ``GET
/calendars/google/callback`` (Google's redirect target), and ``POST
/calendars/{slug}/disconnect``. The token exchange itself is a monkeypatched
``httpx.Client`` (a scripted ``MockTransport``, same technique as every other calendar
test in this codebase) — no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient

from calon.config import CalendarProviderConfig, OperatorConfig, Settings
from calon.main import create_app
from calon.security import derive_login_key, new_oauth_state
from tests.conftest import NOW

LOGIN = "op-key-123"


def _google_config(**overrides: object) -> OperatorConfig:
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


def _log_in(client: TestClient) -> None:
    response = client.post("/login", json={"login": LOGIN})
    assert response.status_code in (200, 302, 303), response.text


@pytest.fixture
def operator_client(tmp_path: Path) -> Iterator[TestClient]:
    """A logged-in operator, with ``[calendars.default]`` set up for Google."""
    settings = Settings(
        db_path=tmp_path / "calon.db", config_path=None, login=LOGIN, base_url="http://testserver"
    )
    with (
        time_machine.travel(NOW, tick=False),
        TestClient(create_app(settings, _google_config())) as test_client,
    ):
        _log_in(test_client)
        yield test_client


#: Captured before any test monkeypatches ``httpx.Client`` (see below) — the monkeypatch
#: replaces the *module attribute* ``httpx.Client``, which is the same object every
#: importer sees, so this helper must not call the (by-then-patched) name itself.
_RealClient = httpx.Client


def _mock_token_client(refresh_token: str = "connected-token") -> httpx.Client:
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

    return _RealClient(transport=httpx.MockTransport(handler))


class TestCalendarConnectRoute:
    def test_redirects_to_google_with_a_signed_state(self, operator_client: TestClient) -> None:
        response = operator_client.get("/calendars/default/connect", follow_redirects=False)
        assert response.status_code == 302
        location = response.headers["location"]
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        params = parse_qs(parts.query)
        assert params["client_id"] == ["cid"]
        assert params["redirect_uri"] == ["http://testserver/calendars/google/callback"]
        assert "state" in params

    def test_an_unconfigured_resource_redirects_to_the_dashboard_with_an_error(
        self, operator_client: TestClient
    ) -> None:
        response = operator_client.get("/calendars/does-not-exist/connect", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/bookings?calendar_error=")

    def test_requires_the_operator_login(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings, _google_config())) as anonymous,
        ):
            response = anonymous.get("/calendars/default/connect", follow_redirects=False)
            assert response.status_code == 401


class TestCalendarConnectCallback:
    def test_the_full_round_trip_persists_and_installs_the_provider(
        self, operator_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signing_key = derive_login_key(LOGIN)
        state = new_oauth_state(signing_key, "default")
        monkeypatch.setattr(
            "calon.services.calendar_connect_service.httpx.Client",
            lambda *args, **kwargs: _mock_token_client(),
        )

        response = operator_client.get(
            "/calendars/google/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/bookings?calendar_connected=default"

        dashboard = operator_client.get("/bookings")
        assert "Connected" in dashboard.text
        assert "connected-token" not in dashboard.text  # the token itself is never rendered

        registry = operator_client.app.state.calendar_registry  # type: ignore[attr-defined]
        assert registry.provider_for("default") is not None

    def test_a_provider_error_redirects_with_the_message(self, operator_client: TestClient) -> None:
        response = operator_client.get(
            "/calendars/google/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "access_denied" in response.headers["location"]

    def test_a_missing_code_or_state_redirects_with_an_error(
        self, operator_client: TestClient
    ) -> None:
        response = operator_client.get("/calendars/google/callback", follow_redirects=False)
        assert response.status_code == 303
        assert "calendar_error=" in response.headers["location"]

    def test_an_invalid_state_redirects_with_an_error_and_writes_nothing(
        self, operator_client: TestClient
    ) -> None:
        response = operator_client.get(
            "/calendars/google/callback",
            params={"code": "auth-code", "state": "not-a-real-state"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "calendar_error=" in response.headers["location"]
        # No credential is persisted — the dashboard still shows "Not connected", not the
        # boot-built (seed-refresh-token-less) provider the config alone already installs.
        assert "Not connected" in operator_client.get("/bookings").text

    def test_callback_requires_the_operator_login(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings, _google_config())) as anonymous,
        ):
            response = anonymous.get(
                "/calendars/google/callback",
                params={"code": "x", "state": "y"},
                follow_redirects=False,
            )
            assert response.status_code == 401


class TestCalendarDisconnect:
    def test_disconnect_removes_the_credential_and_the_live_provider(
        self, operator_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signing_key = derive_login_key(LOGIN)
        state = new_oauth_state(signing_key, "default")
        monkeypatch.setattr(
            "calon.services.calendar_connect_service.httpx.Client",
            lambda *args, **kwargs: _mock_token_client(),
        )
        operator_client.get(
            "/calendars/google/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        registry = operator_client.app.state.calendar_registry  # type: ignore[attr-defined]
        assert registry.provider_for("default") is not None

        response = operator_client.post("/calendars/default/disconnect", follow_redirects=False)
        assert response.status_code == 303
        assert registry.provider_for("default") is None

        dashboard = operator_client.get("/bookings")
        assert "Not connected" in dashboard.text

    def test_disconnect_requires_the_operator_login(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings, _google_config())) as anonymous,
        ):
            response = anonymous.post("/calendars/default/disconnect", follow_redirects=False)
            assert response.status_code == 401


class TestDashboardCalendarsPanel:
    def test_no_calendars_configured_shows_no_panel(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as client,
        ):
            _log_in(client)
            html = client.get("/bookings").text
            assert "Calendars" not in html

    def test_a_configured_but_unconnected_resource_shows_a_connect_button(
        self, operator_client: TestClient
    ) -> None:
        html = operator_client.get("/bookings").text
        assert "Calendars" in html
        assert "Connect with Google" in html
        assert "Not connected" in html
