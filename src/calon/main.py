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

from fastapi import FastAPI

from calon import __version__
from calon.api.v1 import router as v1_router
from calon.clock import utcnow
from calon.config import OperatorConfig, Settings, load_operator_config
from calon.db import Database
from calon.migrate import upgrade_to_head
from calon.security import LoginStore
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

        app.state.db = database
        app.state.settings = resolved_settings
        app.state.config = resolved_config

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
