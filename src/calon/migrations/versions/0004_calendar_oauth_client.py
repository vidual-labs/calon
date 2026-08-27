"""Add the calendar_oauth_client table for dashboard-entered OAuth app credentials.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27

ADR 0016: an operator who cannot conveniently edit ``config/calon.toml`` on the host (a
container image, a managed deployment) can enter the Google OAuth client's id and secret
in the operator dashboard instead. One row per resource, keyed by the resource slug — the
same natural key ``[calendars.<slug>]`` uses in the TOML, which still wins wherever it is
present. A resource configured only through the TOML has no row here at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_oauth_client",
        sa.Column("resource_slug", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("calendar_id", sa.String(255), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_secret", sa.Text(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("calendar_oauth_client")
