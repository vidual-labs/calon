"""The calendar-provider contract and the registry that builds one per resource.

ADR 0009 defines the two-method contract that keeps "there is exactly one downstream
code path for a calendar sync" testable. This module is the seam: the domain layer
imports only :class:`FreeBusySpan` and never hears about a provider, and the wiring
layer (Batch 3 onwards) is the only place that learns the provider's name.

The package is called ``calendars`` on purpose: the stdlib ships ``calendar``, and a
module imported as ``calon.calendar`` would shadow it for anything in this codebase
that does ``import calendar`` (CLAUDE.md §5). Nothing we export is the stdlib
``calendar``; the name ``calendars`` leaves the stdlib module untouched.

The provider is the only piece that may touch the network. Everything below it —
the rule chain, the audit log, the SQLite writes — is provider-agnostic, and an
unreachable provider degrades to calon-only availability rather than refusing a
booking (ADR 0009, CLAUDE.md §2). That is why the interface raises
:class:`CalendarProviderError` on any failure and the callers catch exactly that
type and fall back to an empty free/busy answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from calon.config import CalendarProviderConfig

from calon.domain import FreeBusySpan

logger = logging.getLogger(__name__)

__all__ = [
    "CalendarEvent",
    "CalendarProvider",
    "CalendarProviderError",
    "CalendarProviderRegistry",
    "FakeCalendar",
    "FreeBusySpan",  # re-exported for convenience; defined in calon.domain
]


class CalendarProviderError(RuntimeError):
    """The provider's HTTP call failed, or the response could not be understood.

    Callers (Batch 3 wires this) catch this type and degrade to the empty free/busy
    answer: a request is judged on calon's own data rather than failing. The error's
    ``str`` is safe to log because it must not echo a token; the providers'
    constructors ensure that.

    ``status_code`` carries the response's HTTP status when the failure was an HTTP
    error response (``None`` for a transport failure or a non-JSON body). It exists
    so a caller that needs to branch on "was this specifically a 404" — the
    create-vs-update decision in ``upsert_event`` — has a structured value to check
    instead of substring-matching the digits out of the error's message, which can
    misfire when the request URL itself happens to contain the digits "404" (a UUID's
    hex, for instance).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A calon-originated event as it is written to the provider.

    Written once, keyed by ``uid`` so a re-run of the write (e.g. after a partial
    failure) is idempotent. The provider is responsible for translating this shape
    into its own API payload; the ``uid`` is the iCal ``UID`` the booking uses so a
    re-run with the same booking does not create a duplicate (ADR 0009 Consequences).
    """

    uid: str
    summary: str
    starts_at_utc: datetime
    ends_at_utc: datetime
    description: str = ""

    def __post_init__(self) -> None:
        if self.ends_at_utc <= self.starts_at_utc:
            raise ValueError("a CalendarEvent must end after it starts")


@runtime_checkable
class CalendarProvider(Protocol):
    """A provider adapts a resource's external calendar to two calls (ADR 0009).

    Every implementation must expose ``name`` (the provider identifier, e.g. "google"
    or "microsoft") and the two methods below. A :class:`CalendarProviderError` out of
    either method means *degrade to calon-only* at the call site, never a refused
    booking (CLAUDE.md §2).
    """

    name: str
    #: Whether :meth:`upsert_event` can actually write. ``False`` for a read-only
    #: source such as a published ICS feed (ADR 0017), which the write-back skips
    #: rather than auditing a failure for on every booking.
    writable: bool

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Return every busy span the provider reports overlapping ``[window_start, end)``.

        Returns an empty tuple when the provider has no free/busy data for the window
        — *not* an error. Any transport or parse failure raises
        :class:`CalendarProviderError` instead; the caller treats that as empty and
        degrades.
        """
        ...

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        """Create or update the provider's event keyed by ``event.uid``.

        A provider that supports a caller-chosen event id uses ``event.uid`` as the id;
        one that does not (Microsoft Graph) uses a create-then-patch flow keyed by it.
        Idempotent: a second call with the same ``uid`` does not create a duplicate.
        Raises :class:`CalendarProviderError` on any failure; the caller audits the
        rejection but does not re-raise.
        """
        ...


class CalendarProviderRegistry:
    """The set of providers built from the operator config at boot.

    Mirrors :class:`calon.intake.external.SourceRegistry`: built once in the app's
    lifespan, stored on ``app.state``, and read by a FastAPI dependency. Only the
    per-resource providers that are *enabled* and *supported* land here — an operator
    who sets ``[calendars.<slug>]`` with a provider name the instance does not have a
    module for gets a boot error, not a silent no-op (CLAUDE.md §3).
    """

    def __init__(self, providers: dict[str, CalendarProvider] | None = None) -> None:
        self._providers: dict[str, CalendarProvider] = dict(providers or {})

    @classmethod
    def from_config(
        cls,
        configs: dict[str, CalendarProviderConfig],
        supported: frozenset[str] | None = None,
        build: Callable[[str, CalendarProviderConfig], CalendarProvider] | None = None,
        refresh_token_overrides: dict[str, str] | None = None,
    ) -> CalendarProviderRegistry:
        """Build the registry from the operator config.

        ``supported`` is the frozenset of provider names the instance has adapters for.
        A configured provider that is enabled but not in ``supported`` is a boot error
        so the operator does not discover a missing adapter at lunchtime. An
        ``enabled = false`` entry is skipped (config kept for reference, no provider
        built), mirroring the SourceRegistry behaviour.

        ``refresh_token_overrides`` is the boot-time read of the ``calendar_credential``
        table (ADR 0014): a resource connected through the operator dashboard has its
        refresh token there, and it takes precedence over the TOML's ``refresh_token``,
        which is only a bootstrap seed once a real connection exists.
        """
        supported = supported if supported is not None else _SUPPORTED_PROVIDER_NAMES
        overrides = refresh_token_overrides or {}
        providers: dict[str, CalendarProvider] = {}
        for slug, cfg in configs.items():
            if not cfg.enabled:
                continue
            if cfg.provider not in supported:
                raise RuntimeError(
                    f"[calendars.{slug}] enables provider {cfg.provider!r} but the "
                    "instance has no adapter module for it; set enabled = false or "
                    "remove the table"
                )
            override_token = overrides.get(slug)
            effective_cfg = replace(cfg, refresh_token=override_token) if override_token else cfg
            builder = build if build is not None else _build_provider
            providers[slug] = builder(cfg.provider, effective_cfg)
        return cls(providers)

    def provider_for(self, resource_slug: str) -> CalendarProvider | None:
        """The provider for this resource, or ``None`` if the resource has no sync."""
        return self._providers.get(resource_slug)

    def set_provider(self, resource_slug: str, provider: CalendarProvider) -> None:
        """Install or replace a resource's provider at runtime (the connect flow, ADR 0014).

        The registry is normally built once at boot and treated as read-only for the rest
        of the process (mirroring ``SourceRegistry``). A connection made through the
        operator dashboard must take effect on the very next availability check or
        write-back, without a restart — that immediacy is the point of the button — so
        this is the one mutator outside :meth:`from_config` that touches ``_providers``.
        """
        self._providers[resource_slug] = provider

    def remove_provider(self, resource_slug: str) -> None:
        """Drop a resource's provider at runtime (disconnect, ADR 0014). A no-op if absent."""
        self._providers.pop(resource_slug, None)

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        """Provider-reported busy spans for one resource, degrading to empty on failure.

        The single place that learns a provider is broken. A :class:`CalendarProviderError`
        out of :meth:`CalendarProvider.free_busy` is logged (never with a token in the
        message — providers ensure that) and turned into an empty tuple, so the decision
        falls back to calon's own data (ADR 0009, CLAUDE.md §2). Resources with no
        configured provider also return an empty tuple: nothing to ask.
        """
        provider = self._providers.get(resource_slug)
        if provider is None:
            return ()
        try:
            return provider.free_busy(resource_slug, window_start_utc, window_end_utc)
        except CalendarProviderError as exc:
            logger.warning(
                "calendar provider %r for %r failed; degrading to calon-only availability",
                provider.name,
                resource_slug,
                exc_info=exc,
            )
            return ()

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        """Write a booking event to the provider.

        Unlike :meth:`free_busy`, this method does **not** catch
        :class:`CalendarProviderError` itself — it propagates. This is the post-commit
        write-back: the booking has already been accepted inside the write transaction
        when this runs, so a provider failure must not, and does not, roll it back; the
        caller (``perform_write_back``) is the one that catches the error, degrades,
        and appends the audit record — it never lets this method's exception escape
        further. Resources with no configured provider are a silent no-op.
        """
        provider = self._providers.get(resource_slug)
        if provider is None:
            return
        if not self.writes_back(resource_slug):
            return
        provider.upsert_event(resource_slug, event)

    def writes_back(self, resource_slug: str) -> bool:
        """Whether this resource's provider can be written to at all (ADR 0017).

        A read-only source — a published ICS feed — reports free/busy but has nowhere to
        put an event. Callers use this to tell "nothing to write" apart from "the write
        failed", so a feed-connected resource does not audit a sync failure per booking.
        """
        provider = self._providers.get(resource_slug)
        return provider is not None and getattr(provider, "writable", True)

    def __len__(self) -> int:
        return len(self._providers)


#: The provider names the instance supports by default; :meth:`CalendarProviderRegistry.
#: from_config` rejects anything else so the operator finds a missing adapter at boot.
_SUPPORTED_PROVIDER_NAMES = frozenset({"google", "microsoft", "ics"})


def _build_provider(provider: str, cfg: CalendarProviderConfig) -> CalendarProvider:
    """Build a concrete provider for one resource from the operator config.

    Imported here (not at module import) so the package imports cleanly even when the
    provider adapter modules are not present — the same lazy-import discipline the
    intake registry uses (``SourceRegistry.from_config`` imports its modules only at
    boot, so the package is importable without every adapter).
    """
    if provider == "ics":
        from calon.calendars import ics_feed as ics_module

        return ics_module.IcsFeedProvider(
            resource_slug=cfg.slug,
            feed_url=cfg.feed_url,
            timezone=cfg.timezone,
        )

    from calon.calendars.oauth import OAuthCredentials

    credentials = OAuthCredentials(client_id=cfg.client_id, client_secret=cfg.client_secret)
    if provider == "google":
        from calon.calendars import google as google_module

        return google_module.GoogleCalendarProvider(
            resource_slug=cfg.slug,
            calendar_id=cfg.calendar_id,
            refresh_token=cfg.refresh_token,
            credentials=credentials,
        )
    if provider == "microsoft":
        from calon.calendars import microsoft as microsoft_module

        return microsoft_module.MicrosoftGraphProvider(
            resource_slug=cfg.slug,
            calendar_id=cfg.calendar_id,
            refresh_token=cfg.refresh_token,
            credentials=credentials,
        )
    raise RuntimeError(f"provider {provider!r} is not supported")


class FakeCalendar:
    """An in-memory provider for tests and the standalone demo.

    Deterministic and network-free: seeded busy spans are returned verbatim when they
    overlap a window, and upserts are stored keyed by :attr:`CalendarEvent.uid`. The
    same object can be asked to *fail* (raising :class:`CalendarProviderError`) to
    exercise the degrade-to-calon-only path without any HTTP mock.
    """

    name = "fake"
    writable = True

    def __init__(self, *, name: str | None = None) -> None:
        if name is not None:
            self.name = name
        self._busy: dict[str, list[tuple[datetime, datetime, str]]] = {}
        self._events: dict[str, dict[str, CalendarEvent]] = {}
        self.fail_free_busy = False
        self.fail_upsert = False

    def seed_busy(
        self,
        resource_slug: str,
        start_utc: datetime,
        end_utc: datetime,
        reason: str = "",
    ) -> None:
        """Add a busy span to a resource's provider view."""
        self._busy.setdefault(resource_slug, []).append((start_utc, end_utc, reason))

    def set_events(self, resource_slug: str, events: dict[str, CalendarEvent]) -> None:
        """Replace the entire event store for one resource (test setup)."""
        self._events[resource_slug] = dict(events)

    def free_busy(
        self,
        resource_slug: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> tuple[FreeBusySpan, ...]:
        if self.fail_free_busy:
            raise CalendarProviderError("FakeCalendar is configured to fail free/busy")
        spans: list[FreeBusySpan] = []
        for start, end, reason in self._busy.get(resource_slug, ()):
            if start < window_end_utc and end > window_start_utc:
                spans.append(FreeBusySpan(starts_at_utc=start, ends_at_utc=end, reason=reason))
        return tuple(sorted(spans, key=lambda s: s.starts_at_utc))

    def upsert_event(self, resource_slug: str, event: CalendarEvent) -> None:
        if self.fail_upsert:
            raise CalendarProviderError("FakeCalendar is configured to fail upsert")
        self._events.setdefault(resource_slug, {})[event.uid] = event

    def event(self, resource_slug: str, uid: str) -> CalendarEvent | None:
        """Fetch one stored event by uid (test assertion helper)."""
        return self._events.get(resource_slug, {}).get(uid)

    def events(self, resource_slug: str) -> dict[str, CalendarEvent]:
        """The stored events for one resource (test assertion helper)."""
        return dict(self._events.get(resource_slug, {}))

    def __len__(self) -> int:
        return sum(len(v) for v in self._events.values())
