"""Provider OAuth helpers shared by the Google and Microsoft Calendar adapters.

The provider is the only piece of Calon that touches the network, and the only
credential it ever stores is an OAuth *refresh token*. The operator performs the
one-time authorization-code exchange **out of band** (Calon never runs a browser or an
authorization-code loop; see the self-hosting doc) and pastes the resulting refresh token
into the TOML. Everything else — exchanging the refresh token for a short-lived access
token, and refreshing it when the access token lapses — happens here, in-process, per
provider instance.

Refresh discipline (ADR 0009 / 0013, CLAUDE.md §2): the provider sends work with a fresh
access token; on the first ``401`` it refreshes once and retries; a *second* failure
means the grant itself is dead and it raises :class:`CalendarProviderError`, which the
caller turns into Calon-only availability — never a refused booking.

No secret is ever echoed into a log message or an exception string (CLAUDE.md §8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import Client

from json import JSONDecodeError

from calon.calendars import CalendarProviderError

__all__ = [
    "OAuthCredentials",
    "TokenStore",
    "calendar_error",
    "refresh_access_token",
]


def calendar_error(where: str, detail: str = "") -> CalendarProviderError:
    """A labelled provider error. The message names the *cause*, never a credential.

    ``where`` is the provider/operation (``"google free/busy"``) and ``detail`` a short
    reason, both safe to log.
    """
    message = where if not detail else f"{where}: {detail}"
    return CalendarProviderError(message)


@dataclass(frozen=True, slots=True)
class OAuthCredentials:
    """A provider's client credentials (the app-level id/secret the operator supplies).

    Distinct from the per-resource refresh token; held ``frozen`` so it cannot mutate in
    flight. For Google this is the OAuth client id/secret for the ``offline`` grant; for
    Microsoft the same shape against the v2.0 token endpoint.
    """

    client_id: str = ""
    client_secret: str = ""


@dataclass(frozen=False, slots=True)
class TokenStore:
    """A short-lived access token plus its expiry, plus the refresh credential.

    Mutable on purpose: the provider refreshes it in place. Every field is either a
    secret (the tokens) or a public value (expiry). ``expires_at`` is a unix timestamp
    so the provider can decide staleness without a timezone.
    """

    access_token: str = ""
    expires_at: float = 0.0
    refresh_token: str = ""

    def is_fresh(self, now: float | None = None) -> bool:
        """Whether the access token is usable without a refresh.

        A 60-second margin keeps a nearly-expired token from being sent and rejected
        mid-call, the common driver of the refresh-and-retry this store hides.
        """
        now = time.time() if now is None else now
        return bool(self.access_token) and now < self.expires_at - 60.0

    def adopt(self, access_token: str, expires_in: int, refresh_token: str) -> None:
        """Adopt a refresh-grant result (``expires_in`` in seconds)."""
        self.access_token = access_token
        self.expires_at = time.time() + expires_in
        self.refresh_token = refresh_token


def refresh_access_token(
    client: Client,
    *,
    token_url: str,
    credentials: OAuthCredentials,
    refresh_token: str,
) -> tuple[str, int, str]:
    """Post a refresh-token grant; return ``(access_token, expires_in, refresh_token)``.

    One POST to ``token_url`` with ``grant_type=refresh_token``. A non-200 answer, a
    non-JSON body, or a missing ``access_token``/``expires_in`` all raise
    :class:`CalendarProviderError` (via :func:`calendar_error`) — the caller treats that
    as a dead grant and degrades (CLAUDE.md §2). The returned refresh token is the one
    the grant echoed back: providers rotate refresh tokens, so the next call must use
    *this* one, not the original (the provider persists the refreshed store, which is why
    the TOML's seed value is authoritative only before the first real refresh).
    """
    response = client.post(
        token_url,
        json={
            "grant_type": "refresh_token",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": refresh_token,
        },
    )
    if response.status_code != 200:
        raise calendar_error("oauth refresh", f"token endpoint returned {response.status_code}")
    try:
        body = response.json()
    except (JSONDecodeError, ValueError) as exc:
        raise calendar_error("oauth refresh", "token endpoint returned a non-JSON body") from exc
    access_token = body.get("access_token")
    expires_in = body.get("expires_in", 0)
    if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, int):
        raise calendar_error("oauth refresh", "response lacked access_token or expires_in")
    granted = body.get("refresh_token")
    if not (isinstance(granted, str) and granted):
        granted = refresh_token
    return access_token, expires_in, granted
