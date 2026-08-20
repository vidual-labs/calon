"""Google Calendar provider (ADR 0009), the real client.

Adapts one resource's Google Calendar to the two-method :class:`CalendarProvider`
contract over the public Google Calendar JSON API (``https://www.googleapis.com/
calendar/v3/``) using ``httpx`` (already a transitive dependency — no new runtime
dependency is introduced; ADR 0013).

Authentication is a self-contained refresh cycle. The constructor takes the resource's
seed *refresh token* (from the TOML) plus the operator's app-level :class:`OAuthCredentials`;
the provider keeps a :class:`TokenStore`, refreshes on demand, and refreshes **at most
once per call** on a ``401`` (the common "expired access token" case). A second ``401``
means the grant is dead, so the call raises :class:`CalendarProviderError` and the caller
degrades to Calon-only data (CLAUDE.md §2, ADR 0009 / 0013).

Free/busy: ``POST freeBusy`` for the resource's calendar id, scoped to the requested UTC
window. Upsert: ``PATCH .../calendars/{id}/events/{uid}`` (or ``POST`` when the 404 tells
us the event is not there yet), where ``{uid}`` is the booking's iCal UID so a re-run of
the write-back is idempotent (the provider is created with a caller-chosen event id,
which Google allows). No secret is ever echoed into a log line or an exception string
(CLAUDE.md §8).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx

from calon.calendars import (
    CalendarEvent,
    CalendarProviderError,
    FreeBusySpan,
)
from calon.calendars.oauth import (
    OAuthCredentials,
    TokenStore,
    calendar_error,
    refresh_access_token,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarProvider:
    """Google Calendar adapter implementing :class:`CalendarProvider` (ADR 0009).

    See the module docstring for the refresh-and-retry discipline and the idempotent
    upsert design. A ``client`` may be injected for tests (typically wrapping
    ``httpx.MockTransport``); when none is given the provider owns and closes its own.
    """

    name = "google"

    def __init__(
        self,
        *,
        resource_slug: str,
        calendar_id: str,
        refresh_token: str = "",
        credentials: OAuthCredentials | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.resource_slug = resource_slug
        self.calendar_id = calendar_id
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
        try:
            access, expires_in, refresh = refresh_access_token(
                self._client_instance(),
                token_url=_TOKEN_URL,
                credentials=self._credentials,
                refresh_token=self._store.refresh_token,
            )
        except CalendarProviderError:
            raise
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
            raise calendar_error("google", f"transport error ({type(exc).__name__})") from exc
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
                    "google", f"transport error after refresh ({type(exc).__name__})"
                ) from exc
            if response.status_code == 401:
                raise calendar_error(
                    "google", "refreshed token still rejected (401); grant is dead"
                )
        if response.status_code >= 400:
            raise calendar_error(
                "google",
                f"{method} {url} returned {response.status_code}",
                status_code=response.status_code,
            )
        return response

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Fetch provider busy spans overlapping the window (ADR 0009).

        ``POST freeBusy`` scoped to this resource's calendar. A provider that reports no
        busy time returns an empty tuple; an unreachable / dead provider raises
        :class:`CalendarProviderError` (the caller degrades to Calon-only data).
        """
        body = {
            # The freeBusy API takes a list of calendar *objects*, each keyed by "id" —
            # a list of bare id strings is rejected with a 400.
            "items": [{"id": self.calendar_id}],
            "timeMin": window_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": window_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        response = self._request("POST", f"{_API_BASE}/freeBusy", json_body=body)
        data = response.json() if response.content else {}
        event_set = (data.get("calendars") or {}).get(self.calendar_id, {}).get("busy", [])
        spans: list[FreeBusySpan] = []
        for entry in event_set:
            start = entry.get("start")
            end = entry.get("end")
            if not start or not end:
                continue
            spans.append(
                FreeBusySpan(
                    starts_at_utc=_parse_rfc3339(start),
                    ends_at_utc=_parse_rfc3339(end),
                    reason="provider report",
                )
            )
        return tuple(spans)

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        """Create or update the event keyed by ``event.uid`` (idempotent, ADR 0009).

        Tries a ``PATCH`` to the caller-chosen id first; a 404 means the event does not
        exist yet, so a ``POST`` creates it with that same id. Either way the booking
        lands as exactly one event keyed by its iCal UID.

        Google restricts a caller-chosen event id to base32hex characters (lowercase
        ``a``-``v`` and ``0``-``9``) — ``event.uid`` is ``<booking-id>@<instance-host>``,
        which carries hyphens and an ``@`` that Google's API rejects outright. The event
        is instead keyed by :func:`_google_event_id`, a deterministic hash of the UID
        that *is* a valid id: the same booking always maps to the same Google event, so
        the upsert stays idempotent even though the two ids differ. ``event.uid`` is
        additionally set as the event's ``iCalUID`` field, which is what Google itself
        (and anything reading the event outside calon) uses to recognise it.
        """
        google_id = _google_event_id(event.uid)
        payload = {
            "iCalUID": event.uid,
            "summary": event.summary,
            "start": {"dateTime": _rfc3339(event.starts_at_utc)},
            "end": {"dateTime": _rfc3339(event.ends_at_utc)},
        }
        if event.description:
            payload["description"] = event.description
        url = f"{_API_BASE}/calendars/{self.calendar_id}/events/{google_id}"
        try:
            self._request("PATCH", url, json_body=payload)
        except CalendarProviderError as exc:
            # A fresh 404 (not-yet-created) is expected: create it instead. Re-raise
            # anything else so the caller audits the genuine failure.
            if exc.status_code != 404:
                raise
            base = f"{_API_BASE}/calendars/{self.calendar_id}/events"
            # The id is only settable at creation time; the PATCH path addresses the
            # existing resource by URL and must not send "id" in the payload.
            self._request("POST", base, json_body={**payload, "id": google_id})


def _rfc3339(moment: datetime) -> str:
    """Render an aware datetime as RFC 3339 with a ``Z`` suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _google_event_id(uid: str) -> str:
    """A Google-legal event id, deterministic in ``uid``.

    Google requires a caller-chosen event id to be base32hex (lowercase ``a``-``v``
    and ``0``-``9``, 5-1024 characters) — calon's own UID (``<booking-id>
    @<instance-host>``) is not one, so it cannot be used directly. A SHA-1 hex digest
    of the UID is: entirely lowercase hex digits, a subset of the allowed alphabet;
    a fixed 40 characters, well inside the length limit; and stable, so the same
    booking always maps to the same Google event and a re-run of the write-back
    finds it again instead of creating a duplicate.
    """
    return hashlib.sha1(uid.encode("utf-8"), usedforsecurity=False).hexdigest()


def _parse_rfc3339(text: str) -> datetime:
    """Parse an RFC 3339 timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
