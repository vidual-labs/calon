"""The operator-initiated "Connect with Google" flow (ADR 0014).

Three steps, one round trip through the operator's own browser:

1. :func:`start_connect` builds Google's consent-screen URL, signing a ``state`` value
   with the operator's own derived session key so the callback can trust it, without a
   server-side state store (``calon.security.new_oauth_state``).
2. The operator authorizes on Google's own site; Google redirects back with a ``code``.
3. :func:`complete_connect` exchanges the code for tokens, persists the refresh token
   (``calon.models.CalendarCredentialRow``), and installs the resulting provider into the
   running :class:`~calon.calendars.CalendarProviderRegistry` immediately — no restart.

Google only, for now — the connect flow's whole scope per ADR 0014. Microsoft 365 stays on
the out-of-band/TOML path (ADR 0013).

The OAuth application's own ``client_id``/``client_secret`` still come from
``config/calon.toml``: registering an OAuth app with the provider is a one-time
developer-console step that is specific to each self-hosted instance's redirect URI, and
this flow does not attempt to remove that step — only the refresh-token exchange that used
to be a manual copy-paste.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from calon.calendars import CalendarProviderRegistry
from calon.calendars.google import GoogleCalendarProvider, build_authorize_url
from calon.calendars.oauth import OAuthCredentials, exchange_authorization_code
from calon.config import CalendarProviderConfig, OperatorConfig
from calon.models import CalendarCredentialRow
from calon.security import new_oauth_state

__all__ = [
    "CalendarNotConfiguredError",
    "ConnectResult",
    "complete_connect",
    "disconnect",
    "start_connect",
]


class CalendarNotConfiguredError(ValueError):
    """No usable ``[calendars.<slug>]`` entry for this resource.

    Covers three cases alike, all of which mean "there is nothing to connect yet": no
    entry at all, an entry for a provider the connect flow does not support (Microsoft),
    or an entry missing the ``client_id``/``client_secret`` the OAuth exchange needs. The
    message says which, since an operator hitting this came from a UI button, not a config
    parser, and needs to know what to fix in ``config/calon.toml``.
    """


def _connectable_config(config: OperatorConfig, resource_slug: str) -> CalendarProviderConfig:
    cfg = config.calendars.get(resource_slug)
    if cfg is None:
        raise CalendarNotConfiguredError(
            f"no [calendars.{resource_slug}] entry in config/calon.toml; add one with "
            'provider = "google", client_id, and client_secret before connecting'
        )
    if cfg.provider != "google":
        raise CalendarNotConfiguredError(
            f"the connect flow supports Google only; [calendars.{resource_slug}] is "
            f"configured for provider = {cfg.provider!r} (use the out-of-band refresh_token "
            "setup for that provider instead)"
        )
    if not cfg.client_id or not cfg.client_secret:
        raise CalendarNotConfiguredError(
            f"[calendars.{resource_slug}] is missing client_id and/or client_secret; set "
            "both from the Google Cloud OAuth client before connecting"
        )
    return cfg


def start_connect(
    config: OperatorConfig,
    *,
    resource_slug: str,
    redirect_uri: str,
    signing_key: bytes,
) -> str:
    """The consent-screen URL to send the operator's browser to.

    Raises :class:`CalendarNotConfiguredError` if the resource has no Google entry ready in the
    TOML yet — the caller (the web route) turns that into a readable error for the
    operator rather than an OAuth redirect to nowhere.
    """
    cfg = _connectable_config(config, resource_slug)
    state = new_oauth_state(signing_key, resource_slug)
    return build_authorize_url(client_id=cfg.client_id, redirect_uri=redirect_uri, state=state)


@dataclass(frozen=True, slots=True)
class ConnectResult:
    resource_slug: str
    provider: str


def complete_connect(
    session: Session,
    calendar_registry: CalendarProviderRegistry,
    config: OperatorConfig,
    *,
    resource_slug: str,
    code: str,
    redirect_uri: str,
    now: datetime,
    client: httpx.Client | None = None,
) -> ConnectResult:
    """Exchange the authorization code, persist the credential, and go live immediately.

    Raises :class:`CalendarNotConfiguredError` (the resource's TOML entry vanished or changed
    provider between the redirect and the callback) or
    :class:`~calon.calendars.CalendarProviderError` (the token exchange itself failed) —
    the caller shows either as a readable error and leaves any prior connection untouched.
    """
    cfg = _connectable_config(config, resource_slug)
    credentials = OAuthCredentials(client_id=cfg.client_id, client_secret=cfg.client_secret)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        _access_token, _expires_in, refresh_token = exchange_authorization_code(
            http_client,
            token_url=GoogleCalendarProvider.token_url,
            credentials=credentials,
            code=code,
            redirect_uri=redirect_uri,
        )
    finally:
        if owns_client:
            http_client.close()

    row = session.get(CalendarCredentialRow, resource_slug)
    if row is None:
        session.add(
            CalendarCredentialRow(
                resource_slug=resource_slug,
                provider="google",
                refresh_token=refresh_token,
                connected_at_utc=now,
                updated_at_utc=now,
            )
        )
    else:
        row.provider = "google"
        row.refresh_token = refresh_token
        row.updated_at_utc = now

    calendar_registry.set_provider(
        resource_slug,
        GoogleCalendarProvider(
            resource_slug=resource_slug,
            calendar_id=cfg.calendar_id,
            refresh_token=refresh_token,
            credentials=credentials,
        ),
    )

    return ConnectResult(resource_slug=resource_slug, provider="google")


def disconnect(
    session: Session,
    calendar_registry: CalendarProviderRegistry,
    *,
    resource_slug: str,
) -> bool:
    """Remove a resource's stored credential and drop it from the live registry.

    Returns ``True`` if a credential existed, ``False`` if there was nothing to remove.
    The resource degrades straight back to calon-only availability (``CLAUDE.md`` §2) —
    exactly like an unreachable provider does, never a refused booking.
    """
    row = session.get(CalendarCredentialRow, resource_slug)
    if row is None:
        return False
    session.delete(row)
    calendar_registry.remove_provider(resource_slug)
    return True
