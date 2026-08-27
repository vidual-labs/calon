"""The ASGI application.

Startup does three things, in order: bring the database schema up to date, read
``config/calon.toml``, and project it onto the tables. All three are safe to repeat, so a
restart is always a valid way to apply a configuration change — which is the only way to
apply one, there being no admin UI.

Configuration is read *before* anything is served. A misconfigured instance refuses to
start rather than accepting bookings under rules its operator did not write.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI

import calon.intake.external as intake_external
from calon import __version__
from calon.api.v1 import router as v1_router
from calon.calendars import CalendarProviderRegistry
from calon.clock import utcnow
from calon.config import OperatorConfig, Settings, load_operator_config
from calon.db import Database
from calon.intake.external import SourceRegistry
from calon.migrate import upgrade_to_head
from calon.models import CalendarCredentialRow
from calon.security import LoginStore
from calon.services import calendar_connect_service
from calon.services.provisioning import sync_operator_config
from calon.web import router as web_router

__all__ = ["app", "create_app"]

logger = logging.getLogger("calon")

DESCRIPTION = """
A lean, self-hostable booking intake tool.

Submit a booking request to `POST /api/v1/bookings`; it is judged against the operator's
scheduling rules and either booked or rejected with the reasons and up to three
alternatives.

`GET /api/v1/availability` lists what is free. It is **advisory**: slots are not held or
reserved, and the authoritative answer is what happens when a booking is submitted.
""".strip()


def create_app(settings: Settings | None = None, config: OperatorConfig | None = None) -> FastAPI:
    """Build the application.

    Both arguments exist for tests and for anyone embedding calon; in normal operation
    they are read from the environment and from ``config/calon.toml``.
    """
    resolved_settings = settings or Settings()
    resolved_config = config or load_operator_config(resolved_settings.config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.getLogger("calon").setLevel(resolved_settings.log_level.upper())

        # Before anything else: SQLite creates a missing database file, but not the
        # directory holding it, and migrations run before the first connection is opened.
        resolved_settings.db_path.parent.mkdir(parents=True, exist_ok=True)

        upgrade_to_head(resolved_settings.database_url)
        database = Database.from_path(resolved_settings.db_path)

        with database.write() as session:
            resource = sync_operator_config(session, resolved_config, now=utcnow())
            logger.info(
                "calon ready: resource %r in %s, %d blackout period(s)",
                resource.slug,
                resource.timezone,
                len(resolved_config.blackouts),
            )
            # Resources connected through the operator dashboard's "Connect with Google"
            # button (ADR 0014) have their refresh token here; it takes precedence over
            # the TOML's, which is only a bootstrap seed once a real connection exists.
            connected_refresh_tokens = {
                row.resource_slug: row.refresh_token
                for row in session.scalars(sa.select(CalendarCredentialRow))
            }
            # ADR 0016: a resource whose OAuth app credentials were entered in the
            # dashboard rather than the TOML is configured here too, so the connection
            # survives a restart. The TOML still wins where it has an entry. A client
            # with no credential yet is deliberately left out: a provider that can never
            # refresh is worse than no provider, which is simply the standalone default.
            calendar_configs = {
                slug: cfg
                for slug, cfg in calendar_connect_service.configured_calendars(
                    session, resolved_config
                ).items()
                if slug in resolved_config.calendars or slug in connected_refresh_tokens
            }

        app.state.db = database
        app.state.settings = resolved_settings
        app.state.config = resolved_config

        # External-intake sources are a startup-time decision (ADR 0005, rule 4):
        # the set of enabled sources is read once from ``[sources.<slug>]`` and no
        # per-request probing of that set is possible. A source that is not enabled
        # here receives 404, not 401 — that is what keeps an unauthenticated caller
        # from enumerating which slugs an instance has configured.
        registry = SourceRegistry.from_config(
            intake_external,
            source_configs=resolved_config.sources,
        )
        app.state.source_registry = registry
        logger.info(
            "calon sources: %d registered",
            len(registry),
        )

        # Calendar providers are a startup-time decision, like sources (ADR 0009): the
        # set of resources with an enabled ``[calendars.<slug>]`` is read once and no
        # per-request probing of it is possible. An instance with no calendars configured
        # gets an empty registry — every provider call degrades to calon-only
        # availability, which is exactly today's behaviour (CLAUDE.md §2).
        calendar_registry = CalendarProviderRegistry.from_config(
            calendar_configs, refresh_token_overrides=connected_refresh_tokens
        )
        app.state.calendar_registry = calendar_registry
        logger.info(
            "calon calendars: %d provider(s) registered",
            len(calendar_registry),
        )

        # The operator's login store. Built only when a login is configured; when it is
        # not, ``login_store`` is ``None`` and every login-gated route refuses with 503
        # while the public booking flow keeps working (ADR 0010).
        app.state.login_store = (
            LoginStore(
                resolved_settings.login,
                session_ttl_seconds=resolved_settings.session_ttl_hours * 60 * 60,
            )
            if resolved_settings.login
            else None
        )

        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title=resolved_config.instance_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved_settings.docs_enabled else None,
    )
    app.include_router(v1_router)
    app.include_router(web_router)

    @app.get("/healthz", tags=["operations"], summary="Liveness check")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
