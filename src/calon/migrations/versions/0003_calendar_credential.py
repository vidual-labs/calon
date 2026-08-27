"""Add the calendar_credential table for the operator-initiated connect flow.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27

ADR 0014: a resource connected through the operator dashboard's "Connect with Google"
button has its refresh token stored here, one row per resource, keyed by the resource
slug (the same natural key `[calendars.<slug>]` already uses in the TOML). A resource
that only ever used the out-of-band/TOML path (ADR 0013) has no row here at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_credential",
        sa.Column("resource_slug", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("connected_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("calendar_credential")
