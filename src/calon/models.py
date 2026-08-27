"""The SQLite schema, as SQLAlchemy 2.0 declarative models.

The tables are documented in ``docs/domain-model.md``; that document is the reference and
this file follows it. Two conventions are load-bearing:

- **Every instant is UTC**, in a column suffixed ``_utc``, stored through
  :class:`UtcDateTime` so a naive value cannot get in and a value read back is never
  silently naive. SQLite has no timezone-aware type of its own, so without that decorator
  round-tripping a datetime quietly loses its offset.
- **Identifiers are UUIDv7 strings**, so primary-key order is creation order.

Nothing here belongs to the domain layer. These models are the persistence edge: they are
translated into the pure value objects in ``calon.domain`` on the way in, and never passed
into a rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from calon.ids import new_id

__all__ = [
    "AuditEvent",
    "AvailabilityPolicyRow",
    "Base",
    "BlackoutPeriodRow",
    "Booking",
    "BookingIntent",
    "CalendarCredentialRow",
    "ResourceRow",
    "UtcDateTime",
]


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that refuses naive values and always reads back as UTC.

    ``CLAUDE.md`` §4 says naive datetimes are a bug. SQLite stores no offset, so this is
    where that rule is actually enforced — at the one boundary that would otherwise erase
    it.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime; every stored instant must carry a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class ResourceRow(Base):
    """The bookable thing — a person, a room, a service.

    Named ``ResourceRow`` rather than ``Resource`` so it cannot be confused with the pure
    ``calon.domain.Resource`` the rules actually reason about.
    """

    __tablename__ = "resource"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class AvailabilityPolicyRow(Base):
    """One row per resource: the operator's scheduling rules, projected from TOML.

    ``config/calon.toml`` is the source of truth; this table is refreshed from it at
    startup. See ``docs/adr/0008-operator-config-is-toml-authoritative.md``.
    """

    __tablename__ = "availability_policy"

    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Comma-separated weekday numbers, 0 (Monday) to 6 (Sunday) — e.g. ``"0,1,2,3,4"``.
    allowed_weekdays: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Local clock times in ``timezone``, as ``"HH:MM"``.
    window_start: Mapped[str] = mapped_column(String(5), nullable=False)
    window_end: Mapped[str] = mapped_column(String(5), nullable=False)
    default_duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_granularity_min: Mapped[int] = mapped_column(Integer, nullable=False)
    min_notice_min: Mapped[int] = mapped_column(Integer, nullable=False)
    max_advance_days: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_before_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_after_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_bookings_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class BlackoutPeriodRow(Base):
    """Time that is closed regardless of every other rule."""

    __tablename__ = "blackout_period"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resource.id", ondelete="CASCADE"), nullable=False
    )
    starts_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    __table_args__ = (
        CheckConstraint("ends_at_utc > starts_at_utc", name="ck_blackout_period_ordered"),
        Index("ix_blackout_period_resource_span", "resource_id", "starts_at_utc", "ends_at_utc"),
    )


class BookingIntent(Base):
    """The canonical record of what was asked for — including what was refused.

    Immutable in spirit: only the decision fields are filled in after insert, and only
    once. Rejected intents are kept deliberately; they are exactly the rows worth reading
    later when an operator asks why nobody could book a Friday.
    """

    __tablename__ = "booking_intent"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resource_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resource.id", ondelete="SET NULL"), nullable=True
    )
    #: Which adapter produced this intent — ``native``, or ``<source-slug>`` later.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    requested_start_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    requested_end_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    requester_timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    requester_name: Mapped[str] = mapped_column(String(200), nullable=False)
    requester_email: Mapped[str] = mapped_column(String(320), nullable=False)
    requester_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Opaque passthrough from the source. Never read by core logic.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: The original :class:`Decision`, serialized. Read only to replay a response on
    #: idempotent retry (ADR 0005) — the stored answer is what the source first got.
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    received_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decision_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_booking_intent_status"
        ),
        # Ships with the first migration although nothing writes an idempotency key until
        # the external intake framework lands: adding a unique constraint later, against
        # live data that may already violate it, is a far worse migration.
        Index(
            "uq_booking_intent_source_idempotency",
            "source",
            "idempotency_key",
            unique=True,
            sqlite_where=idempotency_key.isnot(None),
        ),
        Index("ix_booking_intent_received", "received_at_utc"),
    )


class Booking(Base):
    """Written only on acceptance.

    ``block_start_utc`` and ``block_end_utc`` are the booking's span widened by the buffers
    that were in force when it was accepted. They are materialised rather than derived so
    that conflict detection is one indexed range query, and so that changing the buffers
    later cannot retroactively make already-accepted bookings overlap.
    """

    __tablename__ = "booking"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    intent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("booking_intent.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    resource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resource.id", ondelete="RESTRICT"), nullable=False
    )
    start_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    end_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    block_start_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    block_end_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
    #: The iCalendar UID issued for this booking. Filled in with the handoff, in phase 3.
    ics_uid: Mapped[str | None] = mapped_column(String(320), nullable=True)
    ics_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    cancelled_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("end_utc > start_utc", name="ck_booking_ordered"),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled')",
            name="ck_booking_status",
        ),
        # The conflict query filters on resource and status, then ranges over the block
        # bounds. This is the index that makes it one lookup instead of a table scan.
        Index(
            "ix_booking_resource_block",
            "resource_id",
            "status",
            "block_start_utc",
            "block_end_utc",
        ),
    )


class CalendarCredentialRow(Base):
    """A resource's OAuth refresh token, obtained through the operator connect flow.

    ADR 0014: one row per resource that has been connected via the "Connect with Google"
    button on the operator dashboard, keyed by the resource slug — the same natural key
    ``[calendars.<slug>]`` uses in ``config/calon.toml``. A resource that only uses the
    out-of-band/TOML path (ADR 0013) has no row here; ``CalendarProviderRegistry`` prefers
    this table's token over the TOML's when both are present, since this one reflects the
    provider's own token rotation.

    No column-level encryption (see ADR 0014, Decision): the refresh token is a secret at
    the same trust level as ``client_secret`` already stored in plaintext in
    ``config/calon.toml``, and as the requester PII already stored in plaintext in
    ``booking_intent`` — calon has one trust boundary, the operator's own host.
    """

    __tablename__ = "calendar_credential"

    resource_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The provider this credential is for. ``"google"`` today (ADR 0014 scopes the
    #: connect flow to Google only); Microsoft 365 stays on the out-of-band path.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    connected_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class AuditEvent(Base):
    """Append-only. Never updated, never deleted.

    This is the answer to "why did calon do that": every intake, every decision, and every
    booking written leaves a row here, in the order it happened.

    Alone among calon's tables this one carries an integer ``seq`` as well as a UUID. The
    events of a single decision are written inside one transaction and share one timestamp
    by design — ``now`` is injected once and used throughout — so neither ``at_utc`` nor a
    UUIDv7 minted in the same millisecond can order them. ``seq`` is what makes the log
    readable in the order things actually happened; ``id`` remains its stable identifier.
    """

    __tablename__ = "audit_event"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=new_id)
    at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: ``system``, ``operator``, or ``source:<slug>``.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    booking_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_audit_event_at", "at_utc"),)
