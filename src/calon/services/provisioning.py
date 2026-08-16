"""Projecting ``config/calon.toml`` onto the database.

The TOML file is the source of truth for the operator's rules; the tables are a projection
of it, refreshed at every startup (ADR 0008). Anything the file no longer says stops being
true here — blackouts are replaced wholesale, and a resource the file has stopped
mentioning is deactivated rather than left quietly bookable.

Existing bookings are never touched. A booking was accepted under the rules in force when
it was made, and tightening the rules afterwards must not retroactively unmake it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from calon.config import OperatorConfig
from calon.models import AvailabilityPolicyRow, BlackoutPeriodRow, ResourceRow
from calon.services.repository import encode_weekdays, encode_window_time

__all__ = ["sync_operator_config"]


def sync_operator_config(session: Session, config: OperatorConfig, *, now: datetime) -> ResourceRow:
    """Make the database say what the configuration file says. Returns the resource row."""
    resource = _sync_resource(session, config, now=now)
    _sync_policy(session, config, resource_id=resource.id, now=now)
    _sync_blackouts(session, config, resource_id=resource.id)
    _deactivate_others(session, keep_slug=config.resource.slug)
    session.flush()
    return resource


def _sync_resource(session: Session, config: OperatorConfig, *, now: datetime) -> ResourceRow:
    row = session.scalar(select(ResourceRow).where(ResourceRow.slug == config.resource.slug))
    if row is None:
        row = ResourceRow(
            slug=config.resource.slug,
            name=config.resource_name,
            timezone=config.resource.timezone,
            is_active=config.resource.is_active,
            created_at_utc=now,
        )
        session.add(row)
        session.flush()
        return row

    row.name = config.resource_name
    row.timezone = config.resource.timezone
    row.is_active = config.resource.is_active
    return row


def _sync_policy(
    session: Session, config: OperatorConfig, *, resource_id: str, now: datetime
) -> None:
    policy = config.policy
    row = session.get(AvailabilityPolicyRow, resource_id)
    if row is None:
        row = AvailabilityPolicyRow(resource_id=resource_id, updated_at_utc=now)
        session.add(row)

    row.timezone = policy.timezone
    row.allowed_weekdays = encode_weekdays(policy.allowed_weekdays)
    row.window_start = encode_window_time(policy.window_start)
    row.window_end = encode_window_time(policy.window_end)
    row.default_duration_min = policy.default_duration_min
    row.slot_granularity_min = policy.slot_granularity_min
    row.min_notice_min = policy.min_notice_min
    row.max_advance_days = policy.max_advance_days
    row.buffer_before_min = policy.buffer_before_min
    row.buffer_after_min = policy.buffer_after_min
    row.max_bookings_per_day = policy.max_bookings_per_day
    row.updated_at_utc = now


def _sync_blackouts(session: Session, config: OperatorConfig, *, resource_id: str) -> None:
    """Replace the stored blackouts with the configured ones.

    Wholesale replacement rather than a diff: nothing references a blackout row, the list
    is short, and "what the file says, exactly" is easier to reason about than a merge.
    """
    session.execute(delete(BlackoutPeriodRow).where(BlackoutPeriodRow.resource_id == resource_id))
    for blackout in config.blackouts:
        session.add(
            BlackoutPeriodRow(
                resource_id=resource_id,
                starts_at_utc=blackout.starts_at_utc,
                ends_at_utc=blackout.ends_at_utc,
                reason=blackout.reason,
            )
        )


def _deactivate_others(session: Session, *, keep_slug: str) -> None:
    """Close any resource the configuration has stopped mentioning.

    It is deactivated rather than deleted: its bookings and its audit trail stay readable,
    but nothing new can be booked against it.
    """
    for row in session.scalars(select(ResourceRow).where(ResourceRow.slug != keep_slug)):
        row.is_active = False
