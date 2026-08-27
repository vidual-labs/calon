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
from json import JSONDecodeError
from typing import Any

import httpx

from calon.calendars import CalendarProviderError

__all__ = [
    "OAuthCredentials",
    "ProviderTransport",
    "TokenStore",
    "calendar_error",
    "exchange_authorization_code",
    "refresh_access_token",
]


def calendar_error(
    where: str, detail: str = "", *, status_code: int | None = None
) -> CalendarProviderError:
    """A labelled provider error. The message names the *cause*, never a credential.

    ``where`` is the provider/operation (``"google free/busy"``) and ``detail`` a short
    reason, both safe to log. ``status_code`` carries the response's HTTP status when
    the failure was an HTTP error response, so a caller can branch on it structurally
    (see :class:`CalendarProviderError`) rather than parsing the message.
    """
    message = where if not detail else f"{where}: {detail}"
    return CalendarProviderError(message, status_code=status_code)


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
    client: httpx.Client,
    *,
    token_url: str,
    credentials: OAuthCredentials,
    refresh_token: str,
) -> tuple[str, int, str]:
    """Post a refresh-token grant; return ``(access_token, expires_in, refresh_token)``.

    One POST to ``token_url`` with ``grant_type=refresh_token``, as
    ``application/x-www-form-urlencoded`` — RFC 6749 §4.1.3 requires it for the token
    endpoint, and both Google's and Microsoft's reject a JSON body with a ``400``. A
    non-200 answer, a non-JSON body, or a missing ``access_token``/``expires_in`` all
    raise :class:`CalendarProviderError` (via :func:`calendar_error`) — the caller
    treats that as a dead grant and degrades (CLAUDE.md §2). The returned refresh token
    is the one the grant echoed back: providers rotate refresh tokens, so the next call
    must use *this* one, not the original (the provider persists the refreshed store,
    which is why the TOML's seed value is authoritative only before the first real
    refresh).
    """
    response = client.post(
        token_url,
        data={
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


def exchange_authorization_code(
    client: httpx.Client,
    *,
    token_url: str,
    credentials: OAuthCredentials,
    code: str,
    redirect_uri: str,
) -> tuple[str, int, str]:
    """Exchange an authorization code for tokens (RFC 6749 §4.1.3); same shape as a refresh.

    Used only by the operator-initiated connect flow (ADR 0014) — a normal request/refresh
    cycle never calls this. Returns ``(access_token, expires_in, refresh_token)`` exactly
    like :func:`refresh_access_token`, so a caller adopts the result into a
    :class:`TokenStore` the same way either function is used. Google issues a refresh token
    only on the *first* consent for a given client (or whenever ``prompt=consent`` forces
    one, which the connect flow's authorize URL always sets) — a response with no
    ``refresh_token`` raises :class:`CalendarProviderError`, because a connect with nothing
    to persist has not actually connected anything.
    """
    response = client.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
        },
    )
    if response.status_code != 200:
        raise calendar_error("oauth connect", f"token endpoint returned {response.status_code}")
    try:
        body = response.json()
    except (JSONDecodeError, ValueError) as exc:
        raise calendar_error("oauth connect", "token endpoint returned a non-JSON body") from exc
    access_token = body.get("access_token")
    expires_in = body.get("expires_in", 0)
    if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, int):
        raise calendar_error("oauth connect", "response lacked access_token or expires_in")
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise calendar_error(
            "oauth connect",
            "response lacked a refresh_token; reconnect to force a fresh consent",
        )
    return access_token, expires_in, refresh_token


class ProviderTransport:
    """Shared HTTP transport for a calendar provider: client lifecycle plus the
    401→refresh→retry-once discipline (ADR 0009 / 0013).

    Google's and Microsoft's providers differ only in their endpoints and payload
    shapes; the transport underneath — owning or borrowing an ``httpx.Client``, the
    refresh cycle, and "retry exactly once on a 401" — is identical, which is what
    ADR 0013 means by "the transport... are shared". A subclass sets
    :attr:`provider_name` (for error labelling) and :attr:`token_url` (its OAuth
    token endpoint) as class attributes, calls ``super().__init__(...)`` with the
    constructor arguments every provider needs, and calls :meth:`_request` for every
    API call.
    """

    #: The provider identifier used to label :class:`CalendarProviderError` messages
    #: (e.g. ``"google"``, ``"microsoft"``). Set by the subclass.
    provider_name: str
    #: The provider's OAuth token endpoint. Set by the subclass.
    token_url: str

    def __init__(
        self,
        *,
        refresh_token: str = "",
        credentials: OAuthCredentials | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._credentials = credentials or OAuthCredentials()
        self._store = TokenStore(refresh_token=refresh_token)
        self._client = client
        self._owns_client = client is None

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
            self._owns_client = True
        return self._client

    def close(self) -> None:
        """Close the provider's own HTTP client (a no-op if one was injected)."""
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _refresh(self) -> None:
        """Refresh the access token once, or raise if the grant cannot be refreshed."""
        access, expires_in, refresh = refresh_access_token(
            self._client_instance(),
            token_url=self.token_url,
            credentials=self._credentials,
            refresh_token=self._store.refresh_token,
        )
        self._store.adopt(access, expires_in, refresh)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one HTTP request, refreshing the token once on a ``401`` and retrying.

        The first failure after a refresh re-raises the :class:`CalendarProviderError` as
        a dead grant; the caller (free_busy / upsert_event) lets it propagate, and the
        registry degrades. A transport error is wrapped the same way.
        """
        if not self._store.is_fresh():
            self._refresh()
        headers = {"Authorization": f"Bearer {self._store.access_token}"}
        client = self._client_instance()
        try:
            response = client.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise calendar_error(
                self.provider_name, f"transport error ({type(exc).__name__})"
            ) from exc
        if response.status_code == 401:
            # One and only refresh-and-retry: a dead grant must not loop forever.
            self._refresh()
            headers = {"Authorization": f"Bearer {self._store.access_token}"}
            try:
                response = client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.HTTPError as exc:
                raise calendar_error(
                    self.provider_name, f"transport error after refresh ({type(exc).__name__})"
                ) from exc
            if response.status_code == 401:
                raise calendar_error(
                    self.provider_name, "refreshed token still rejected (401); grant is dead"
                )
        if response.status_code >= 400:
            raise calendar_error(
                self.provider_name,
                f"{method} {url} returned {response.status_code}",
                status_code=response.status_code,
            )
        return response
