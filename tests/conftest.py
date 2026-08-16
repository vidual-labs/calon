"""Shared fixtures for the tests that need a running calon.

The domain tests need none of this — they are pure functions over pure values and live in
``tests/domain`` with no fixtures at all. Everything here exists for the layers that do
have I/O: a real SQLite file, real migrations, and the real application.

Time is frozen for every test in this file's scope. Scheduling behaviour is a function of
"now", so a test that does not control it is a test that fails on a Sunday.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import time_machine
from fastapi.testclient import TestClient

from calon.config import Settings, load_operator_config
from calon.db import Database
from calon.main import create_app

#: Starts the application against a given operator configuration file body.
BootFn = Callable[[str | None], TestClient]

#: Tuesday 1 September 2026, 08:00 in Europe/Berlin. A working day inside the default
#: booking window, chosen so that "later today" and "tomorrow" are both bookable.
NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)

BERLIN = "Europe/Berlin"
NEW_YORK = "America/New_York"


@pytest.fixture
def frozen_clock() -> Iterator[None]:
    with time_machine.travel(NOW, tick=False):
        yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Runtime settings pointed at a throwaway database and no operator config file.

    No config file means the built-in defaults, which are the ones
    ``config/calon.example.toml`` documents — the same standalone path CI exercises.
    """
    return Settings(
        db_path=tmp_path / "calon.db",
        config_path=None,
        docs_enabled=True,
    )


@pytest.fixture
def client(settings: Settings, frozen_clock: None) -> Iterator[TestClient]:
    """The real application, migrated and provisioned, over an in-process transport."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def boot(tmp_path: Path, frozen_clock: None) -> BootFn:
    """Start calon against a given operator config, on one database across restarts.

    Calling it twice is a restart: same database file, whatever configuration is passed
    the second time. That is the only way to apply a configuration change, so it is worth
    being able to test.
    """

    def _boot(config_body: str | None = None) -> TestClient:
        config_path = None
        if config_body is not None:
            config_path = tmp_path / "calon.toml"
            config_path.write_text(config_body, encoding="utf-8")

        settings = Settings(db_path=tmp_path / "calon.db", config_path=config_path)
        return TestClient(create_app(settings, load_operator_config(config_path)))

    return _boot


@pytest.fixture
def database(client: TestClient) -> Database:
    """The database the running application is using, for asserting on stored rows."""
    db: Database = client.app.state.db  # type: ignore[attr-defined]
    return db


def booking_payload(
    start: str,
    end: str | None = None,
    *,
    resource_slug: str = "default",
    timezone: str = BERLIN,
    **overrides: Any,
) -> dict[str, Any]:
    """A well-formed booking request, so each test only states what it is about."""
    payload: dict[str, Any] = {
        "resource_slug": resource_slug,
        "start": start,
        "timezone": timezone,
        "requester": {"name": "Ada Lovelace", "email": "ada@example.com"},
        "subject": "Initial consultation",
    }
    if end is not None:
        payload["end"] = end
    payload.update(overrides)
    return payload


__all__ = [
    "BERLIN",
    "NEW_YORK",
    "NOW",
    "BootFn",
    "booking_payload",
]
