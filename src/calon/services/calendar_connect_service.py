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

The OAuth application's own ``client_id``/``client_secret`` come from one of two places
(ADR 0016): a ``[calendars.<slug>]`` entry in ``config/calon.toml``, or — where the TOML
has no entry for the resource — a row the operator entered in the dashboard itself
(``calon.models.CalendarOAuthClientRow``). The TOML always wins where it is present.
Registering the OAuth app with the provider remains a one-time developer-console step that
no self-hosted instance can automate; what both paths remove is the manual refresh-token
copy-paste, and what the second removes on top of that is the need to edit a file on the
host at all.
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
from calon.models import CalendarCredentialRow, CalendarOAuthClientRow
from calon.security import new_oauth_state

__all__ = [
    "CalendarNotConfiguredError",
    "ConnectResult",
    "complete_connect",
    "configured_calendars",
    "disconnect",
    "forget_oauth_client",
    "resolve_calendar_config",
    "save_oauth_client",
    "start_connect",
]

#: The only provider the dashboard connect flow supports today (ADR 0014). The storage is
#: provider-keyed so Microsoft 365 can join it later without a schema change.
DASHBOARD_PROVIDERS = frozenset({"google"})


class CalendarNotConfiguredError(ValueError):
    """No usable calendar configuration for this resource.

    Covers every "there is nothing to connect yet" case alike: no ``[calendars.<slug>]``
    entry and no dashboard-entered OAuth client, an entry for a provider the connect flow
    does not support (Microsoft), or an entry missing the ``client_id``/``client_secret``
    the OAuth exchange needs. The message says which, since an operator hitting this came
    from a UI button, not a config parser, and needs to know what to fix and where.
    """


def resolve_calendar_config(
    session: Session, config: OperatorConfig, resource_slug: str
) -> CalendarProviderConfig | None:
    """The calendar configuration in force for a resource, from either source (ADR 0016).

    ``config/calon.toml`` wins wherever it has an entry for the resource — a file the
    operator edited is never silently overridden by a row in a database. Only where the
    TOML is silent does the dashboard-entered OAuth client apply. ``None`` means the
    resource has no calendar at all, which is the standalone default (``CLAUDE.md`` §2).
    """
    from_toml = config.calendars.get(resource_slug)
    if from_toml is not None:
        return from_toml
    row = session.get(CalendarOAuthClientRow, resource_slug)
    if row is None:
        return None
    return CalendarProviderConfig(
        slug=resource_slug,
        provider=row.provider,
        calendar_id=row.calendar_id,
        enabled=True,
        client_id=row.client_id,
        client_secret=row.client_secret,
    )


def configured_calendars(
    session: Session, config: OperatorConfig
) -> dict[str, CalendarProviderConfig]:
    """Every resource with a calendar configured, from both sources, TOML winning.

    Used at boot to build the provider registry, so a resource connected through the
    dashboard keeps working across a restart.
    """
    resolved: dict[str, CalendarProviderConfig] = {}
    for row in session.query(CalendarOAuthClientRow).all():
        resolved[row.resource_slug] = CalendarProviderConfig(
            slug=row.resource_slug,
            provider=row.provider,
            calendar_id=row.calendar_id,
            enabled=True,
            client_id=row.client_id,
            client_secret=row.client_secret,
        )
    resolved.update(config.calendars)
    return resolved


def _connectable_config(
    session: Session, config: OperatorConfig, resource_slug: str
) -> CalendarProviderConfig:
    cfg = resolve_calendar_config(session, config, resource_slug)
    if cfg is None:
        raise CalendarNotConfiguredError(
            f"{resource_slug} has no calendar configured yet; enter the Google OAuth "
            "client id and secret on the dashboard, or add a [calendars."
            f"{resource_slug}] entry to config/calon.toml"
        )
    if cfg.provider not in DASHBOARD_PROVIDERS:
        raise CalendarNotConfiguredError(
            f"the connect flow supports Google only; {resource_slug} is configured for "
            f"provider = {cfg.provider!r} (use the out-of-band refresh_token setup for "
            "that provider instead)"
        )
    if not cfg.client_id or not cfg.client_secret:
        raise CalendarNotConfiguredError(
            f"{resource_slug} is missing client_id and/or client_secret; set both from "
            "the Google Cloud OAuth client before connecting"
        )
    return cfg


def save_oauth_client(
    session: Session,
    *,
    resource_slug: str,
    client_id: str,
    client_secret: str,
    calendar_id: str,
    now: datetime,
    provider: str = "google",
) -> None:
    """Store the OAuth app credentials an operator entered in the dashboard (ADR 0016).

    Storing them does **not** connect anything: it only makes the resource connectable, so
    the next step is the same consent round trip a TOML-configured resource takes. A
    resource whose credentials are already in ``config/calon.toml`` cannot be configured
    this way — the caller checks that first, since the TOML would win anyway and a form
    that silently did nothing would be worse than a refusal.
    """
    if provider not in DASHBOARD_PROVIDERS:
        raise CalendarNotConfiguredError(
            f"the dashboard connect flow supports Google only; {provider!r} is set up "
            "out-of-band in config/calon.toml"
        )
    if not client_id or not client_secret:
        raise CalendarNotConfiguredError("both the client id and the client secret are required")

    row = session.get(CalendarOAuthClientRow, resource_slug)
    if row is None:
        session.add(
            CalendarOAuthClientRow(
                resource_slug=resource_slug,
                provider=provider,
                calendar_id=calendar_id or "primary",
                client_id=client_id,
                client_secret=client_secret,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        return
    row.provider = provider
    row.calendar_id = calendar_id or "primary"
    row.client_id = client_id
    row.client_secret = client_secret
    row.updated_at_utc = now


def forget_oauth_client(
    session: Session,
    calendar_registry: CalendarProviderRegistry,
    *,
    resource_slug: str,
) -> bool:
    """Drop the dashboard-entered OAuth client, and with it any connection built on it.

    The grant cannot outlive the app it was issued to, so this removes the credential and
    the live provider as well: the resource degrades straight back to calon-only
    availability (``CLAUDE.md`` §2). Returns ``True`` if a client row existed.
    """
    row = session.get(CalendarOAuthClientRow, resource_slug)
    if row is None:
        return False
    session.delete(row)
    disconnect(session, calendar_registry, resource_slug=resource_slug)
    return True


def start_connect(
    session: Session,
    config: OperatorConfig,
    *,
    resource_slug: str,
    redirect_uri: str,
    signing_key: bytes,
) -> str:
    """The consent-screen URL to send the operator's browser to.

    Raises :class:`CalendarNotConfiguredError` if the resource has no Google credentials
    ready, from either source — the caller (the web route) turns that into a readable
    error for the operator rather than an OAuth redirect to nowhere.
    """
    cfg = _connectable_config(session, config, resource_slug)
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

    Raises :class:`CalendarNotConfiguredError` (the resource's configuration vanished or
    changed provider between the redirect and the callback) or
    :class:`~calon.calendars.CalendarProviderError` (the token exchange itself failed) —
    the caller shows either as a readable error and leaves any prior connection untouched.
    """
    cfg = _connectable_config(session, config, resource_slug)
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
