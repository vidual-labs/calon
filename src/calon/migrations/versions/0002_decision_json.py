"""Store the original decision on the intent row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-18

The external intake framework (ADR 0005) must return the *stored* original answer on an
idempotent replay — never re-judge a retried request against the calendar as it is *now*,
because that is exactly how a stale rejection silently becomes today's acceptance. The
answer that was given first, in its complete structured form, is what a retry gets back,
and that answer lives on the intent row as ``decision_json``.

Native intents keep a structured ``decision_code`` and ``decision_reason`` as before;
``decision_json`` is ``NULL`` for them because a replay is an external-intake property
and the native form has no retry semantics to replay. The column is added, not
migrated from the two existing columns, because the two columns are the *operator's*
view of the decision (one-line, human-readable) and the JSON column is the *adapter's*
view (complete, and includes the suggestions the requester would want to see again).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "booking_intent",
        sa.Column("decision_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking_intent", "decision_json")
