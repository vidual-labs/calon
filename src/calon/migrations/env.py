"""Alembic environment.

Migrations live inside the installed package rather than beside it, so that a container
that has only the wheel can still upgrade its own database at startup.

The database URL comes from :class:`calon.config.Settings` — the same ``CALON_DB_PATH`` the
application reads — unless a caller has already set one on the Alembic config, which is how
``calon.migrate.upgrade_to_head`` and the tests point it somewhere else.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from calon.config import Settings
from calon.models import Base

config = context.config
target_metadata = Base.metadata

if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", Settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the table
            # instead, which is what makes later schema changes reviewable rather than
            # impossible.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
