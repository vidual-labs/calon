"""Running migrations.

Migrations run automatically at startup, as ``docs/self-hosting.md`` promises: an operator
who upgrades the container should not also have to remember a command. At this scale the
upgrade takes milliseconds and there is exactly one writer, so there is nothing to
coordinate.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

__all__ = ["alembic_config", "upgrade_to_head"]

_SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    """An Alembic config pointed at the packaged migrations and a specific database."""
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str) -> None:
    """Bring ``database_url`` up to the latest revision, creating it if it is new."""
    command.upgrade(alembic_config(database_url), "head")
