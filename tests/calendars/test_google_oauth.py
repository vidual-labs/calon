"""OAuth refresh helper: the grant body and the parse are pinned against a mock transport.

Covers :func:`calon.calendars.oauth.refresh_access_token` (used by the Google and
Microsoft adapters) without any network: a scripted ``httpx.MockTransport`` plays the
token endpoint, and the test asserts the exact request body and that the returned values
are exactly what the endpoint produced — including the rotated refresh token.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from calon.calendars import CalendarProviderError
from calon.calendars.oauth import OAuthCredentials, refresh_access_token

_TOKEN_URL = "https://oauth2.googleapis.com/token"
CREDS = OAuthCredentials(client_id="cid", client_secret="csecret")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestRefreshTokenBody:
    def test_the_refresh_body_is_correct(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "tok-A",
                    "expires_in": 3599,
                    "refresh_token": "rotated-rot",
                    "token_type": "Bearer",
                },
            )

        access, expires, refresh = refresh_access_token(
            _client(handler), token_url=_TOKEN_URL, credentials=CREDS, refresh_token="seed-rot"
        )
        assert seen["path"].endswith("/token")
        # RFC 6749 §4.1.3: the token endpoint takes a form-encoded body, not JSON —
        # both Google's and Microsoft's endpoints reject a JSON grant with a 400.
        assert seen["content_type"].startswith("application/x-www-form-urlencoded")
        from urllib.parse import parse_qs

        sent = parse_qs(seen["body"])
        assert sent == {
            "grant_type": ["refresh_token"],
            "client_id": ["cid"],
            "client_secret": ["csecret"],
            # The seed refresh token is sent, not a rotated one — there isn't one yet.
            "refresh_token": ["seed-rot"],
        }
        assert access == "tok-A"
        assert expires == 3599
        assert refresh == "rotated-rot"

    def test_the_refresh_token_not_returned_keeps_the_original(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "tok-B", "expires_in": 60})

        access, _expires, refresh = refresh_access_token(
            _client(handler), token_url=_TOKEN_URL, credentials=CREDS, refresh_token="keep-me"
        )
        assert access == "tok-B"
        assert refresh == "keep-me"


class TestRefreshTokenParse:
    def test_a_400_grant_raises_the_provider_error_and_names_the_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        with pytest.raises(CalendarProviderError) as exc_info:
            refresh_access_token(
                _client(handler), token_url=_TOKEN_URL, credentials=CREDS, refresh_token="dead"
            )
        assert "400" in str(exc_info.value)

    def test_a_200_with_a_non_json_body_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>gate</html>")

        with pytest.raises(CalendarProviderError) as exc_info:
            refresh_access_token(
                _client(handler), token_url=_TOKEN_URL, credentials=CREDS, refresh_token="dead"
            )
        assert "non-JSON" in str(exc_info.value)

    def test_a_200_missing_access_token_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        with pytest.raises(CalendarProviderError) as exc_info:
            refresh_access_token(
                _client(handler), token_url=_TOKEN_URL, credentials=CREDS, refresh_token="dead"
            )
        assert "access_token" in str(exc_info.value)
