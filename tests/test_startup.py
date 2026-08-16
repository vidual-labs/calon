"""Starting calon on a machine where nothing has been set up yet.

The first run is the one nobody tests and everybody does. It has to work with no database,
no directory to put one in, and no configuration file — that is the standalone-first
promise in ``CLAUDE.md`` §2, taken literally.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import time_machine
from fastapi.testclient import TestClient

from calon.config import ConfigError, Settings
from calon.main import create_app
from tests.conftest import NOW


@pytest.fixture
def frozen_clock() -> Iterator[None]:
    with time_machine.travel(NOW, tick=False):
        yield


def test_the_first_run_creates_the_database_and_the_directory_holding_it(
    tmp_path: Path, frozen_clock: None
) -> None:
    """SQLite creates a missing file, but never a missing directory.

    The default ``CALON_DB_PATH`` is ``./data/calon.db`` and a fresh clone has no ``data/``,
    so this is the very first thing a new instance does.
    """
    db_path = tmp_path / "data" / "calon.db"
    assert not db_path.parent.exists()

    with TestClient(create_app(Settings(db_path=db_path, config_path=None))) as client:
        assert client.get("/healthz").json()["status"] == "ok"

    assert db_path.is_file()


def test_it_starts_with_no_configuration_file_at_all(tmp_path: Path, frozen_clock: None) -> None:
    settings = Settings(db_path=tmp_path / "calon.db", config_path=None)

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/availability",
            params={
                "resource_slug": "default",
                "from": "2026-09-02T09:00:00+02:00",
                "to": "2026-09-02T17:00:00+02:00",
            },
        )

    # The built-in defaults are a working configuration, not an empty one.
    assert response.status_code == 200
    assert response.json()["slots"]


def test_a_configuration_it_cannot_understand_stops_startup(
    tmp_path: Path, frozen_clock: None
) -> None:
    """An instance that cannot read its rules must not serve bookings under guesses."""
    config_path = tmp_path / "calon.toml"
    config_path.write_text('[availability]\nwindow_startt = "09:00"\n', encoding="utf-8")

    settings = Settings(db_path=tmp_path / "calon.db", config_path=config_path)

    with pytest.raises(ConfigError, match="unrecognised key"):
        create_app(settings)


def test_the_openapi_document_can_be_switched_off(tmp_path: Path, frozen_clock: None) -> None:
    settings = Settings(db_path=tmp_path / "calon.db", config_path=None, docs_enabled=False)

    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        # The service itself still answers.
        assert client.get("/healthz").status_code == 200
