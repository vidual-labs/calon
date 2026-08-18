"""Shared route dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from calon.config import Settings
from calon.db import Database
from calon.intake.external import SourceRegistry
from calon.security import SESSION_COOKIE, LoginStore

__all__ = [
    "AuthorisedOperator",
    "DatabaseDep",
    "RegistryDep",
    "SettingsDep",
    "get_authorised_operator",
    "get_database",
    "get_settings",
    "get_source_registry",
]


def get_settings(request: Request) -> Settings:
    """The application's resolved runtime settings, chosen once at startup."""
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    """The application's database, opened once at startup."""
    database: Database = request.app.state.db
    return database


def get_authorised_operator(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginStore:
    """Gate a route behind the operator's login.

    Two ways in, tried in order:

    1. **``Authorization: Bearer <key>``** — when ``CALON_API_KEY`` is set, a request
       carrying the matching key is admitted without a cookie. This is the path for
       ``curl`` and for external systems.
    2. **Session cookie** — a token in the ``calon_session`` cookie that this process
       issued at login. The default path for a human using the operator panel.

    Neither is available: if ``CALON_LOGIN`` was never set, the instance is explicitly
    *unsecured* for its operator surface and the route refuses with ``503`` rather than
    opening the door. That "fail closed" is deliberate: an operator who has forgotten to
    set a login should see their operator endpoints go dark, not a wide-open one.
    """
    _login_store: LoginStore | None = request.app.state.login_store

    # Fail closed: with no login configured there is nothing to verify against, and the
    # operator surface must go dark rather than open. Checked *before* the cookie path,
    # because there is no store to ask a question of when ``login_store`` is ``None``.
    if _login_store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "this operator endpoint requires CALON_LOGIN to be configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Bearer key (optional): ``curl`` and external systems, no browser required.
    if settings.api_key:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
            if hmac.compare_digest(supplied, settings.api_key):
                return _login_store
        # Fall through to the cookie path if the Bearer key was absent or wrong.

    cookie = request.cookies.get(SESSION_COOKIE)
    if _login_store.valid_session(cookie):
        return _login_store

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="operator login required",
        headers={"WWW-Authenticate": 'Bearer, Cookie="calon_session"'},
    )


def get_source_registry(request: Request) -> SourceRegistry:
    """The startup-built intake source registry (ADR 0005).

    The registry is built once in ``create_app``'s lifespan from the operator
    config's ``[sources.<slug>]`` tables and lives on ``app.state``. Exposing it
    as a dependency — rather than touching ``app.state`` inside the route body —
    keeps the route testable: a test override replaces the registry wholesale,
    which is exactly the seam the ADR's boundary contract is about.
    """
    registry = request.app.state.source_registry
    if registry is None:
        # create_app's lifespan builds it before the app accepts requests; a None here
        # means the app was constructed without running the lifespan (a test shortcut).
        raise RuntimeError("source registry not initialised; run the app lifespan first")
    assert isinstance(registry, SourceRegistry)
    return registry


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RegistryDep = Annotated[SourceRegistry, Depends(get_source_registry)]
AuthorisedOperator = Annotated[LoginStore, Depends(get_authorised_operator)]
