"""Initial schema: resources, policy, blackouts, intents, bookings, audit.

Revision ID: 0001
Revises:
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "availability_policy",
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("allowed_weekdays", sa.String(length=32), nullable=False),
        sa.Column("window_start", sa.String(length=5), nullable=False),
        sa.Column("window_end", sa.String(length=5), nullable=False),
        sa.Column("default_duration_min", sa.Integer(), nullable=False),
        sa.Column("slot_granularity_min", sa.Integer(), nullable=False),
        sa.Column("min_notice_min", sa.Integer(), nullable=False),
        sa.Column("max_advance_days", sa.Integer(), nullable=False),
        sa.Column("buffer_before_min", sa.Integer(), nullable=False),
        sa.Column("buffer_after_min", sa.Integer(), nullable=False),
        sa.Column("max_bookings_per_day", sa.Integer(), nullable=True),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["resource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("resource_id"),
    )

    op.create_table(
        "blackout_period",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("starts_at_utc", sa.DateTime(), nullable=False),
        sa.Column("ends_at_utc", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.CheckConstraint("ends_at_utc > starts_at_utc", name="ck_blackout_period_ordered"),
        sa.ForeignKeyConstraint(["resource_id"], ["resource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_blackout_period_resource_span",
        "blackout_period",
        ["resource_id", "starts_at_utc", "ends_at_utc"],
    )

    op.create_table(
        "booking_intent",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("requested_start_utc", sa.DateTime(), nullable=False),
        sa.Column("requested_end_utc", sa.DateTime(), nullable=False),
        sa.Column("requester_timezone", sa.String(length=64), nullable=False),
        sa.Column("requester_name", sa.String(length=200), nullable=False),
        sa.Column("requester_email", sa.String(length=320), nullable=False),
        sa.Column("requester_phone", sa.String(length=64), nullable=True),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("received_at_utc", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision_code", sa.String(length=32), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at_utc", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_booking_intent_status"
        ),
        sa.ForeignKeyConstraint(["resource_id"], ["resource.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_intent_received", "booking_intent", ["received_at_utc"])
    # Nothing writes an idempotency key until the external intake framework lands, but a
    # unique constraint added later against live data that already violates it is a far
    # worse migration than one added up front.
    op.create_index(
        "uq_booking_intent_source_idempotency",
        "booking_intent",
        ["source", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "booking",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("start_utc", sa.DateTime(), nullable=False),
        sa.Column("end_utc", sa.DateTime(), nullable=False),
        sa.Column("block_start_utc", sa.DateTime(), nullable=False),
        sa.Column("block_end_utc", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ics_uid", sa.String(length=320), nullable=True),
        sa.Column("ics_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("cancelled_at_utc", sa.DateTime(), nullable=True),
        sa.CheckConstraint("end_utc > start_utc", name="ck_booking_ordered"),
        sa.CheckConstraint("status IN ('confirmed', 'cancelled')", name="ck_booking_status"),
        sa.ForeignKeyConstraint(["intent_id"], ["booking_intent.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resource_id"], ["resource.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_id"),
    )
    op.create_index(
        "ix_booking_resource_block",
        "booking",
        ["resource_id", "status", "block_start_utc", "block_end_utc"],
    )

    op.create_table(
        "audit_event",
        # An integer sequence, because the events of one decision share a timestamp and
        # can share a millisecond: nothing else in the row can order them.
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("at_utc", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=True),
        sa.Column("booking_id", sa.String(length=36), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("id"),
    )
    op.create_index("ix_audit_event_at", "audit_event", ["at_utc"])


def downgrade() -> None:
    op.drop_index("ix_audit_event_at", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_booking_resource_block", table_name="booking")
    op.drop_table("booking")
    op.drop_index("uq_booking_intent_source_idempotency", table_name="booking_intent")
    op.drop_index("ix_booking_intent_received", table_name="booking_intent")
    op.drop_table("booking_intent")
    op.drop_index("ix_blackout_period_resource_span", table_name="blackout_period")
    op.drop_table("blackout_period")
    op.drop_table("availability_policy")
    op.drop_table("resource")
