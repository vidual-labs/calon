"""The operator dashboard's Google Calendar connect flow, end to end (ADR 0014, 0016).

Every route here is gated by the operator login exactly like the rest of the dashboard:
``GET /calendars/{slug}/connect`` (redirect to Google), ``GET
/calendars/google/callback`` (Google's redirect target), ``POST
/calendars/{slug}/disconnect``, and the two that store and drop the OAuth app credentials
an operator entered in the browser instead of in ``config/calon.toml`` (``POST
/calendars/{slug}/oauth-client`` and ``.../forget``, ADR 0016). The token exchange itself
is a monkeypatched ``httpx.Client`` (a scripted ``MockTransport``, same technique as every
other calendar test in this codebase) — no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import time_machine
from fastapi.testclient import TestClient

from calon.config import CalendarProviderConfig, OperatorConfig, Settings, SourceConfig
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
        assert response.headers["location"].startswith("/admin?calendar_error=")

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
        assert response.headers["location"] == "/admin?calendar_connected=default"

        dashboard = operator_client.get("/admin")
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
        assert "Not connected" in operator_client.get("/admin").text

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

        dashboard = operator_client.get("/admin")
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
    def test_no_calendars_configured_shows_the_setup_instructions(self, tmp_path: Path) -> None:
        """With nothing configured the panel explains the setup rather than disappearing.

        It stays a signpost: no provider is built, no connect action is offered, and the
        booking flow is untouched (CLAUDE.md §2) — the operator just gets a way to
        discover the feature that does not require reading the docs first.
        """
        settings = Settings(
            db_path=tmp_path / "calon.db",
            config_path=None,
            login=LOGIN,
            base_url="http://testserver",
        )
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as client,
        ):
            _log_in(client)
            html = client.get("/admin").text
            assert "Calendars" in html
            assert "Not configured" in html
            assert "/calendars/default/connect" not in html  # no action, only a signpost
            assert "[calendars.default]" in html
            assert "http://testserver/calendars/google/callback" in html

    def test_a_configured_but_unconnected_resource_shows_a_connect_button(
        self, operator_client: TestClient
    ) -> None:
        html = operator_client.get("/admin").text
        assert "Calendars" in html
        assert "Connect with Google" in html
        assert "Not connected" in html
        assert "Not configured" not in html


class TestDashboardOverviewPanel:
    """The functions overview: what the instance exposes, under which rules."""

    def test_it_lists_the_functions_and_the_rules_in_force(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as client,
        ):
            _log_in(client)
            html = client.get("/admin").text
            assert "Overview" in html
            assert "POST /api/v1/bookings" in html
            assert "GET /api/v1/availability" in html
            # The default policy, rendered from the config calon actually parsed.
            assert "Mon, Tue, Wed, Thu, Fri" in html
            assert "09:00-17:00" in html
            assert "no limit" in html  # max_bookings_per_day unset
            assert "none configured" in html  # no [sources.<slug>]

    def test_a_configured_source_is_listed_with_its_endpoint(self, tmp_path: Path) -> None:
        config = _google_config()
        config = OperatorConfig(
            calendars=config.calendars,
            sources={
                "openflow": SourceConfig(
                    slug="openflow",
                    secret="s" * 16,
                    fields={"form-1": {"start": "fld_start"}},
                )
            },
        )
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings, config)) as client,
        ):
            _log_in(client)
            html = client.get("/admin").text
            assert "POST /api/v1/openflow" in html


class TestDashboardOAuthClient:
    """Entering the OAuth app credentials in the dashboard instead of the TOML (ADR 0016)."""

    @pytest.fixture
    def standalone(self, tmp_path: Path) -> Iterator[TestClient]:
        """A logged-in operator on an instance with no ``[calendars]`` block at all."""
        settings = Settings(
            db_path=tmp_path / "calon.db",
            config_path=None,
            login=LOGIN,
            base_url="http://testserver",
        )
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as test_client,
        ):
            _log_in(test_client)
            yield test_client

    @staticmethod
    def _save(client: TestClient, **overrides: str) -> httpx.Response:
        data = {
            "client_id": "browser-cid",
            "client_secret": "browser-secret",
            "calendar_id": "you@example.com",
        }
        data.update(overrides)
        response: httpx.Response = client.post(
            "/calendars/default/oauth-client", data=data, follow_redirects=False
        )
        return response

    def test_saving_credentials_makes_the_resource_connectable(
        self, standalone: TestClient
    ) -> None:
        response = self._save(standalone)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin?calendar_saved=default")

        html = standalone.get("/admin").text
        assert "Connect with Google" in html
        assert "Not configured" not in html

        # …and the connect flow uses the credentials that were typed in.
        redirect = standalone.get("/calendars/default/connect", follow_redirects=False)
        assert redirect.status_code == 302
        params = parse_qs(urlsplit(redirect.headers["location"]).query)
        assert params["client_id"] == ["browser-cid"]
        assert params["redirect_uri"] == ["http://testserver/calendars/google/callback"]

    def test_the_client_secret_is_never_rendered_back(self, standalone: TestClient) -> None:
        self._save(standalone)
        assert "browser-secret" not in standalone.get("/admin").text

    def test_empty_credentials_are_refused(self, standalone: TestClient) -> None:
        response = self._save(standalone, client_id="", client_secret="")
        assert response.status_code == 303
        assert "calendar_error=" in response.headers["location"]
        assert "Not configured" in standalone.get("/admin").text

    def test_a_toml_configured_resource_refuses_the_form(self, operator_client: TestClient) -> None:
        """The file wins at resolution time, so storing a row that never applies is worse."""
        response = self._save(operator_client)
        assert response.status_code == 303
        assert "calendar_error=" in response.headers["location"]
        # The TOML's own client id is still the one the connect flow uses.
        redirect = operator_client.get("/calendars/default/connect", follow_redirects=False)
        params = parse_qs(urlsplit(redirect.headers["location"]).query)
        assert params["client_id"] == ["cid"]

    def test_the_full_round_trip_connects_with_the_entered_credentials(
        self, standalone: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save(standalone)
        monkeypatch.setattr(
            "calon.services.calendar_connect_service.httpx.Client",
            lambda *args, **kwargs: _mock_token_client(),
        )
        state = new_oauth_state(derive_login_key(LOGIN), "default")

        response = standalone.get(
            "/calendars/google/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?calendar_connected=default"

        registry = standalone.app.state.calendar_registry  # type: ignore[attr-defined]
        provider = registry.provider_for("default")
        assert provider is not None
        assert provider.calendar_id == "you@example.com"
        assert "Connected" in standalone.get("/admin").text

    def test_forget_removes_the_credentials_and_the_connection(
        self, standalone: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save(standalone)
        monkeypatch.setattr(
            "calon.services.calendar_connect_service.httpx.Client",
            lambda *args, **kwargs: _mock_token_client(),
        )
        state = new_oauth_state(derive_login_key(LOGIN), "default")
        standalone.get(
            "/calendars/google/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        registry = standalone.app.state.calendar_registry  # type: ignore[attr-defined]
        assert registry.provider_for("default") is not None

        response = standalone.post("/calendars/default/oauth-client/forget", follow_redirects=False)
        assert response.status_code == 303
        assert registry.provider_for("default") is None
        assert "Not configured" in standalone.get("/admin").text

    def test_the_connection_survives_a_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Boot rebuilds the provider from the stored client + credential (no TOML)."""
        settings = Settings(
            db_path=tmp_path / "calon.db",
            config_path=None,
            login=LOGIN,
            base_url="http://testserver",
        )
        monkeypatch.setattr(
            "calon.services.calendar_connect_service.httpx.Client",
            lambda *args, **kwargs: _mock_token_client(),
        )
        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as first:
            _log_in(first)
            self._save(first)
            first.get(
                "/calendars/google/callback",
                params={
                    "code": "auth-code",
                    "state": new_oauth_state(derive_login_key(LOGIN), "default"),
                },
                follow_redirects=False,
            )

        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as second:
            registry = second.app.state.calendar_registry  # type: ignore[attr-defined]
            assert registry.provider_for("default") is not None

    def test_a_saved_client_with_no_connection_builds_no_provider(self, tmp_path: Path) -> None:
        """Credentials alone are not a connection — standalone until the consent round trip."""
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as first:
            _log_in(first)
            self._save(first)

        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as second:
            registry = second.app.state.calendar_registry  # type: ignore[attr-defined]
            assert registry.provider_for("default") is None

    def test_requires_the_operator_login(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as anonymous,
        ):
            saved = anonymous.post(
                "/calendars/default/oauth-client",
                data={"client_id": "x", "client_secret": "y", "calendar_id": ""},
                follow_redirects=False,
            )
            forgotten = anonymous.post(
                "/calendars/default/oauth-client/forget", follow_redirects=False
            )
            assert saved.status_code == 401
            assert forgotten.status_code == 401


class TestDashboardCalendarFeed:
    """Subscribing to a published ICS feed — the no-developer-console path (ADR 0017)."""

    FEED_URL = "https://calendar.example.com/secret/basic.ics"
    #: One busy hour, inside the operator's default window on a Wednesday.
    FEED_BODY = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:busy@example.com\r\n"
        "DTSTART:20260902T080000Z\r\nDTEND:20260902T090000Z\r\n"
        "SUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    @pytest.fixture
    def standalone(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = Settings(
            db_path=tmp_path / "calon.db",
            config_path=None,
            login=LOGIN,
            base_url="http://testserver",
        )
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as test_client,
        ):
            _log_in(test_client)
            yield test_client

    @classmethod
    def _mock_feed_client(cls) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=cls.FEED_BODY.encode("utf-8"))

        return _RealClient(transport=httpx.MockTransport(handler))

    @classmethod
    def _subscribe(cls, client: TestClient, url: str | None = None) -> httpx.Response:
        response: httpx.Response = client.post(
            "/calendars/default/feed",
            data={"feed_url": url if url is not None else cls.FEED_URL},
            follow_redirects=False,
        )
        return response

    def test_subscribing_goes_live_immediately(self, standalone: TestClient) -> None:
        """No consent round trip: for a feed, the URL is the credential."""
        response = self._subscribe(standalone)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin?calendar_subscribed=default")

        registry = standalone.app.state.calendar_registry  # type: ignore[attr-defined]
        assert registry.provider_for("default") is not None

        html = standalone.get("/admin").text
        assert "Subscribed" in html
        assert "free/busy only" in html
        assert "Not configured" not in html

    def test_the_feed_url_is_never_rendered_back(self, standalone: TestClient) -> None:
        self._subscribe(standalone)
        assert "secret" not in standalone.get("/admin").text

    def test_busy_time_from_the_feed_blocks_a_booking(
        self, standalone: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: a commitment in the operator's own calendar is respected."""
        monkeypatch.setattr(
            "calon.calendars.ics_feed.httpx.Client",
            lambda *args, **kwargs: self._mock_feed_client(),
        )
        self._subscribe(standalone)

        response = standalone.post(
            "/api/v1/bookings",
            json={
                "resource_slug": "default",
                "start": "2026-09-02T10:00:00+02:00",  # 08:00Z — exactly the busy hour
                "timezone": "Europe/Berlin",
                "requester": {"name": "Ada", "email": "ada@example.com"},
                "subject": "Consultation",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"]["outcome"] == "rejected"
        assert body["decision"]["code"] == "PROVIDER_CONFLICT"

    def test_a_free_hour_is_still_bookable(
        self, standalone: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "calon.calendars.ics_feed.httpx.Client",
            lambda *args, **kwargs: self._mock_feed_client(),
        )
        self._subscribe(standalone)

        response = standalone.post(
            "/api/v1/bookings",
            json={
                "resource_slug": "default",
                "start": "2026-09-02T14:00:00+02:00",
                "timezone": "Europe/Berlin",
                "requester": {"name": "Ada", "email": "ada@example.com"},
                "subject": "Consultation",
            },
        )
        assert response.status_code == 201, response.text
        # Nothing was written back: a feed is read-only, so this stays "not synced"
        # rather than being reported as a failed sync (ADR 0017).
        assert response.json()["decision"]["calendar_synced"] is False

    def test_unsubscribing_degrades_to_calon_only(self, standalone: TestClient) -> None:
        self._subscribe(standalone)
        response = standalone.post("/calendars/default/feed/forget", follow_redirects=False)
        assert response.status_code == 303

        registry = standalone.app.state.calendar_registry  # type: ignore[attr-defined]
        assert registry.provider_for("default") is None
        assert "Not configured" in standalone.get("/admin").text

    def test_a_url_that_is_not_http_is_refused(self, standalone: TestClient) -> None:
        response = self._subscribe(standalone, url="file:///etc/passwd")
        assert response.status_code == 303
        assert "calendar_error=" in response.headers["location"]
        assert "Not configured" in standalone.get("/admin").text

    def test_a_feed_and_an_oauth_client_are_mutually_exclusive(
        self, standalone: TestClient
    ) -> None:
        """One calendar per resource: the operator picks which by removing the other."""
        self._subscribe(standalone)
        saved = standalone.post(
            "/calendars/default/oauth-client",
            data={"client_id": "cid", "client_secret": "sec", "calendar_id": ""},
            follow_redirects=False,
        )
        assert "calendar_error=" in saved.headers["location"]

        standalone.post("/calendars/default/feed/forget", follow_redirects=False)
        saved_again = standalone.post(
            "/calendars/default/oauth-client",
            data={"client_id": "cid", "client_secret": "sec", "calendar_id": ""},
            follow_redirects=False,
        )
        assert saved_again.headers["location"].startswith("/admin?calendar_saved=")
        # …and now the feed form is the one refused.
        assert "calendar_error=" in self._subscribe(standalone).headers["location"]

    def test_a_toml_configured_resource_refuses_the_feed_form(
        self, operator_client: TestClient
    ) -> None:
        response = self._subscribe(operator_client)
        assert "calendar_error=" in response.headers["location"]

    def test_the_subscription_survives_a_restart(self, tmp_path: Path) -> None:
        settings = Settings(
            db_path=tmp_path / "calon.db",
            config_path=None,
            login=LOGIN,
            base_url="http://testserver",
        )
        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as first:
            _log_in(first)
            self._subscribe(first)

        with time_machine.travel(NOW, tick=False), TestClient(create_app(settings)) as second:
            registry = second.app.state.calendar_registry  # type: ignore[attr-defined]
            provider = registry.provider_for("default")
            assert provider is not None
            assert provider.name == "ics"

    def test_requires_the_operator_login(self, tmp_path: Path) -> None:
        settings = Settings(db_path=tmp_path / "calon.db", config_path=None, login=LOGIN)
        with (
            time_machine.travel(NOW, tick=False),
            TestClient(create_app(settings)) as anonymous,
        ):
            subscribed = anonymous.post(
                "/calendars/default/feed",
                data={"feed_url": self.FEED_URL},
                follow_redirects=False,
            )
            forgotten = anonymous.post("/calendars/default/feed/forget", follow_redirects=False)
            assert subscribed.status_code == 401
            assert forgotten.status_code == 401
