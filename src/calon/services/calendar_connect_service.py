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

Every calendar secret this module writes to the database — the refresh token, a
dashboard-entered client secret, a feed address — is sealed with the instance's
:class:`~calon.security.secretbox.SecretBox` first (ADR 0019), and storing one without a
``CALON_SECRET_KEY`` is refused. Reading tolerates values written before encryption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from calon.calendars import CalendarProviderRegistry
from calon.calendars.google import GoogleCalendarProvider, build_authorize_url
from calon.calendars.ics_feed import IcsFeedProvider
from calon.calendars.oauth import OAuthCredentials, exchange_authorization_code
from calon.config import CalendarProviderConfig, OperatorConfig
from calon.models import CalendarCredentialRow, CalendarFeedRow, CalendarOAuthClientRow
from calon.security import new_oauth_state
from calon.security.secretbox import SecretBox, SecretUnreadableError

__all__ = [
    "CalendarNotConfiguredError",
    "ConnectResult",
    "complete_connect",
    "configured_calendars",
    "connected_refresh_tokens",
    "disconnect",
    "forget_feed",
    "forget_oauth_client",
    "reseal_secrets",
    "resolve_calendar_config",
    "save_feed",
    "save_oauth_client",
    "start_connect",
]

#: The only provider the dashboard connect flow supports today (ADR 0014). The storage is
#: provider-keyed so Microsoft 365 can join it later without a schema change.
DASHBOARD_PROVIDERS = frozenset({"google"})

logger = logging.getLogger(__name__)


class CalendarNotConfiguredError(ValueError):
    """No usable calendar configuration for this resource.

    Covers every "there is nothing to connect yet" case alike: no ``[calendars.<slug>]``
    entry and no dashboard-entered OAuth client, an entry for a provider the connect flow
    does not support (Microsoft), or an entry missing the ``client_id``/``client_secret``
    the OAuth exchange needs. The message says which, since an operator hitting this came
    from a UI button, not a config parser, and needs to know what to fix and where.
    """


def resolve_calendar_config(
    session: Session, config: OperatorConfig, resource_slug: str, box: SecretBox
) -> CalendarProviderConfig | None:
    """The calendar configuration in force for a resource, from either source (ADR 0016).

    ``config/calon.toml`` wins wherever it has an entry for the resource — a file the
    operator edited is never silently overridden by a row in a database. Only where the
    TOML is silent does the dashboard-entered OAuth client apply. ``None`` means the
    resource has no calendar at all, which is the standalone default (``CLAUDE.md`` §2).
    So does a dashboard-stored secret that cannot be decrypted (no key, or the wrong
    one): the resource degrades to calon-only availability rather than failing.
    """
    from_toml = config.calendars.get(resource_slug)
    if from_toml is not None:
        return from_toml
    try:
        client_row = session.get(CalendarOAuthClientRow, resource_slug)
        if client_row is not None:
            return _client_config(client_row, timezone=config.resource.timezone, box=box)
        feed_row = session.get(CalendarFeedRow, resource_slug)
        if feed_row is not None:
            return _feed_config(feed_row, timezone=config.resource.timezone, box=box)
    except SecretUnreadableError as exc:
        _log_unreadable(resource_slug, exc)
    return None


def _log_unreadable(resource_slug: str, exc: SecretUnreadableError) -> None:
    logger.warning(
        "calendar credentials for %r cannot be read (%s); the resource runs on calon's "
        "own availability until the key is fixed or the calendar is set up again",
        resource_slug,
        exc,
    )


def _client_config(
    row: CalendarOAuthClientRow, *, timezone: str, box: SecretBox
) -> CalendarProviderConfig:
    return CalendarProviderConfig(
        slug=row.resource_slug,
        provider=row.provider,
        calendar_id=row.calendar_id,
        enabled=True,
        client_id=row.client_id,
        client_secret=box.unseal(row.client_secret),
        timezone=timezone,
    )


def _feed_config(row: CalendarFeedRow, *, timezone: str, box: SecretBox) -> CalendarProviderConfig:
    return CalendarProviderConfig(
        slug=row.resource_slug,
        provider="ics",
        calendar_id="",
        enabled=True,
        feed_url=box.unseal(row.url),
        timezone=timezone,
    )


def configured_calendars(
    session: Session, config: OperatorConfig, box: SecretBox
) -> dict[str, CalendarProviderConfig]:
    """Every resource with a calendar configured, from both sources, TOML winning.

    Used at boot to build the provider registry, so a resource connected through the
    dashboard keeps working across a restart. A resource whose stored secret cannot be
    decrypted is left out (and logged), exactly like one with no calendar.
    """
    timezone = config.resource.timezone
    resolved: dict[str, CalendarProviderConfig] = {}
    for feed in session.query(CalendarFeedRow).all():
        try:
            resolved[feed.resource_slug] = _feed_config(feed, timezone=timezone, box=box)
        except SecretUnreadableError as exc:
            _log_unreadable(feed.resource_slug, exc)
    for client in session.query(CalendarOAuthClientRow).all():
        try:
            resolved[client.resource_slug] = _client_config(client, timezone=timezone, box=box)
        except SecretUnreadableError as exc:
            _log_unreadable(client.resource_slug, exc)
    resolved.update(config.calendars)
    return resolved


def connected_refresh_tokens(session: Session, box: SecretBox) -> dict[str, str]:
    """The refresh token of every resource connected through the dashboard (ADR 0014).

    A token that cannot be decrypted is left out (and logged): that resource then has no
    grant, which is the standalone default, not a boot failure.
    """
    tokens: dict[str, str] = {}
    for row in session.query(CalendarCredentialRow).all():
        try:
            tokens[row.resource_slug] = box.unseal(row.refresh_token)
        except SecretUnreadableError as exc:
            _log_unreadable(row.resource_slug, exc)
    return tokens


def reseal_secrets(session: Session, box: SecretBox) -> int:
    """Encrypt every stored secret that is not yet sealed under the current key (ADR 0019).

    Run at startup. Turns plain-text values written before a key was set, and values
    sealed with ``CALON_SECRET_KEY_PREVIOUS``, into values sealed with
    ``CALON_SECRET_KEY``. A value no configured key can open is left untouched. Returns
    how many values were rewritten; ``0`` when there is no key.
    """
    rewritten = 0
    for row in session.query(CalendarCredentialRow).all():
        if box.needs_reseal(row.refresh_token):
            row.refresh_token = box.seal(box.unseal(row.refresh_token))
            rewritten += 1
    for client in session.query(CalendarOAuthClientRow).all():
        if box.needs_reseal(client.client_secret):
            client.client_secret = box.seal(box.unseal(client.client_secret))
            rewritten += 1
    for feed in session.query(CalendarFeedRow).all():
        if box.needs_reseal(feed.url):
            feed.url = box.seal(box.unseal(feed.url))
            rewritten += 1
    return rewritten


def _require_key(box: SecretBox) -> None:
    """Refuse up front, with the operator-facing reason, when nothing can be stored."""
    if not box.has_key:
        raise CalendarNotConfiguredError(
            "storing calendar credentials in calon requires CALON_SECRET_KEY; generate one "
            "with `openssl rand -base64 32`, add it to .env, and restart calon"
        )


def _connectable_config(
    session: Session, config: OperatorConfig, resource_slug: str, box: SecretBox
) -> CalendarProviderConfig:
    _require_key(box)  # the connect flow ends by storing a refresh token
    cfg = resolve_calendar_config(session, config, resource_slug, box)
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
    box: SecretBox,
    provider: str = "google",
) -> None:
    """Store the OAuth app credentials an operator entered in the dashboard (ADR 0016).

    Storing them does **not** connect anything: it only makes the resource connectable, so
    the next step is the same consent round trip a TOML-configured resource takes. A
    resource whose credentials are already in ``config/calon.toml`` cannot be configured
    this way — the caller checks that first, since the TOML would win anyway and a form
    that silently did nothing would be worse than a refusal.

    The client secret is stored sealed; without a ``CALON_SECRET_KEY`` nothing is stored.
    """
    _require_key(box)
    if provider not in DASHBOARD_PROVIDERS:
        raise CalendarNotConfiguredError(
            f"the dashboard connect flow supports Google only; {provider!r} is set up "
            "out-of-band in config/calon.toml"
        )
    if not client_id or not client_secret:
        raise CalendarNotConfiguredError("both the client id and the client secret are required")
    if not calendar_id:
        # "primary" used to be accepted as a stand-in for a blank field, but it is a
        # Google API alias for "whoever is authenticated" — not an identity. A dashboard
        # showing "primary" next to a connected resource gives an operator no way to
        # tell *which* Google account is behind it, especially with more than one
        # resource connected. Requiring the real address up front is the only way to
        # make that visible without asking Google for it (a new OAuth scope, and
        # another reconnect for anyone already connected).
        raise CalendarNotConfiguredError(
            "the calendar id is required — use the connected account's email address, "
            "not the Google API's own 'primary' alias, so the dashboard can show which "
            "account a resource is connected to"
        )
    if session.get(CalendarFeedRow, resource_slug) is not None:
        raise CalendarNotConfiguredError(
            f"{resource_slug} already subscribes to a calendar feed; remove that first if "
            "you want to connect the calendar with OAuth instead"
        )

    row = session.get(CalendarOAuthClientRow, resource_slug)
    if row is None:
        session.add(
            CalendarOAuthClientRow(
                resource_slug=resource_slug,
                provider=provider,
                calendar_id=calendar_id,
                client_id=client_id,
                client_secret=box.seal(client_secret),
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        return
    row.provider = provider
    row.calendar_id = calendar_id
    row.client_id = client_id
    row.client_secret = box.seal(client_secret)
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


def save_feed(
    session: Session,
    calendar_registry: CalendarProviderRegistry,
    *,
    resource_slug: str,
    url: str,
    timezone: str,
    now: datetime,
    box: SecretBox,
) -> None:
    """Subscribe a resource to a published ICS calendar URL (ADR 0017).

    Unlike an OAuth client, a feed is usable the moment it is stored — the URL *is* the
    credential — so the provider goes live here rather than after a consent round trip.
    A resource already set up for OAuth is refused: one calendar per resource, and the
    operator decides which by removing the other. The address is stored sealed.
    """
    _require_key(box)
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise CalendarNotConfiguredError(
            "a calendar feed address must start with http:// or https:// — copy the "
            "secret iCal address from the calendar's own settings"
        )
    if session.get(CalendarOAuthClientRow, resource_slug) is not None:
        raise CalendarNotConfiguredError(
            f"{resource_slug} is already set up with an OAuth client; forget those "
            "credentials first if you want to subscribe to a feed instead"
        )

    row = session.get(CalendarFeedRow, resource_slug)
    if row is None:
        session.add(
            CalendarFeedRow(
                resource_slug=resource_slug,
                url=box.seal(url),
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
    else:
        row.url = box.seal(url)
        row.updated_at_utc = now

    calendar_registry.set_provider(
        resource_slug,
        IcsFeedProvider(resource_slug=resource_slug, feed_url=url, timezone=timezone),
    )


def forget_feed(
    session: Session,
    calendar_registry: CalendarProviderRegistry,
    *,
    resource_slug: str,
) -> bool:
    """Unsubscribe a resource from its calendar feed, degrading it to calon-only."""
    row = session.get(CalendarFeedRow, resource_slug)
    if row is None:
        return False
    session.delete(row)
    calendar_registry.remove_provider(resource_slug)
    return True


def start_connect(
    session: Session,
    config: OperatorConfig,
    *,
    resource_slug: str,
    redirect_uri: str,
    signing_key: bytes,
    box: SecretBox,
) -> str:
    """The consent-screen URL to send the operator's browser to.

    Raises :class:`CalendarNotConfiguredError` if the resource has no Google credentials
    ready, from either source — the caller (the web route) turns that into a readable
    error for the operator rather than an OAuth redirect to nowhere.
    """
    cfg = _connectable_config(session, config, resource_slug, box)
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
    box: SecretBox,
    client: httpx.Client | None = None,
) -> ConnectResult:
    """Exchange the authorization code, persist the credential, and go live immediately.

    Raises :class:`CalendarNotConfiguredError` (the resource's configuration vanished or
    changed provider between the redirect and the callback) or
    :class:`~calon.calendars.CalendarProviderError` (the token exchange itself failed) —
    the caller shows either as a readable error and leaves any prior connection untouched.
    """
    cfg = _connectable_config(session, config, resource_slug, box)
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
                refresh_token=box.seal(refresh_token),
                connected_at_utc=now,
                updated_at_utc=now,
            )
        )
    else:
        row.provider = "google"
        row.refresh_token = box.seal(refresh_token)
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
