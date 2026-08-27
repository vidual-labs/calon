"""Add the calendar_feed table for subscribed ICS calendar URLs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27

ADR 0017: an operator who cannot register an OAuth app can instead paste the secret ICS
address their calendar publishes, and calon reads free/busy from it. One row per
resource, keyed by the resource slug — the same natural key the TOML and the OAuth-client
table already use. Read-only: nothing is ever written back to a feed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_feed",
        sa.Column("resource_slug", sa.String(64), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("calendar_feed")
