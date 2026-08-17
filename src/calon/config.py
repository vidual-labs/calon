"""Runtime settings and the operator's scheduling rules.

Two sources, with a deliberate split:

- **Environment** (``.env``, ``CALON_*``) carries runtime concerns: where the database
  lives, the public URL, the log level. Read into :class:`Settings`.
- **``config/calon.toml``** carries the scheduling rules: the resource, its availability
  policy, and its blackout periods. Read into :class:`OperatorConfig`.

The TOML file is the source of truth for the rules — the database rows are a projection of
it, refreshed at startup (see ``docs/adr/0008-operator-config-is-toml-authoritative.md``).

Both are optional. calon is standalone first (``CLAUDE.md`` §2): with no ``.env`` and no
``config/calon.toml`` at all it boots on the defaults below, which match the shipped
``config/calon.example.toml``.

Where the file *is* present, it is read strictly. An unrecognised key is an error rather
than a shrug, because the failure mode of ignoring it is an operator who believes they
closed Saturdays and finds out otherwise from a requester.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_cls
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from calon.domain import AvailabilityPolicy, BlackoutPeriod, Resource

__all__ = [
    "CalendarConfig",
    "ConfigError",
    "OperatorConfig",
    "Settings",
    "load_operator_config",
]


class ConfigError(ValueError):
    """The operator configuration file could not be understood.

    Raised at startup, never mid-request: a misconfigured instance should refuse to boot
    rather than turn a typo into a rejected booking.
    """


# --------------------------------------------------------------------------------------
# Runtime settings
# --------------------------------------------------------------------------------------


class Settings(BaseSettings):
    """Runtime settings, read from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="CALON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Path("./data/calon.db")
    base_url: str = "http://localhost:8000"
    instance_host: str = "localhost"
    config_path: Path | None = Path("./config/calon.toml")
    log_level: str = "INFO"
    docs_enabled: bool = True

    #: The operator's login. Set via ``CALON_LOGIN``. This is the single key that gates
    #: the operator web panel and every endpoint that returns personal data (notably the
    #: ``.ics`` handoff). It is **required** when any personal-data endpoint is reached;
    #: the instance still boots and the public booking flow still works without it, but
    #: login-gated routes return ``HTTPError`` (401/503) until it is set.
    login: str = ""
    #: Optional shared ``API key`` for programmatic access to the operator endpoints
    #: (``Authorization: Bearer <CALON_API_KEY>``). Set via ``CALON_API_KEY``. When unset,
    #: the operator endpoints fall back to the cookie login from :attr:`login` and the
    #: Bearer path is disabled. This is what an external system or a ``curl`` from the
    #: operator's laptop uses.
    api_key: str | None = None
    #: How long an operator web session lives before it requires re-login.
    session_ttl_hours: int = 12

    @field_validator("config_path", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value: Any) -> Any:
        """Treat ``CALON_CONFIG_PATH=""`` as "no operator config", not as the CWD.

        CI's standalone job sets exactly that to prove calon runs with nothing configured.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"


# --------------------------------------------------------------------------------------
# Operator configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """How an accepted booking is described in the calendar handoff (phase 3)."""

    event_title: str = "Consultation with {requester_name}"
    location: str = ""
    organizer_name: str = ""
    organizer_email: str = ""


@dataclass(frozen=True, slots=True)
class OperatorConfig:
    """Everything the operator decides, resolved into the domain's own value objects."""

    instance_name: str = "calon"
    instance_timezone: str = "Europe/Berlin"
    resource_name: str = "Consultation"
    resource: Resource = field(
        default_factory=lambda: Resource(slug="default", timezone="Europe/Berlin")
    )
    policy: AvailabilityPolicy = field(
        default_factory=lambda: AvailabilityPolicy(
            timezone="Europe/Berlin",
            allowed_weekdays=frozenset({0, 1, 2, 3, 4}),
            window_start=time(9, 0),
            window_end=time(17, 0),
            default_duration_min=30,
            slot_granularity_min=15,
            min_notice_min=120,
            max_advance_days=60,
            buffer_before_min=0,
            buffer_after_min=15,
            max_bookings_per_day=None,
        )
    )
    blackouts: tuple[BlackoutPeriod, ...] = ()
    calendar: CalendarConfig = field(default_factory=CalendarConfig)


_INSTANCE_KEYS = frozenset({"name", "timezone"})
_RESOURCE_KEYS = frozenset({"slug", "name", "timezone"})
_AVAILABILITY_KEYS = frozenset(
    {
        "allowed_weekdays",
        "window_start",
        "window_end",
        "default_duration_min",
        "slot_granularity_min",
        "min_notice_min",
        "max_advance_days",
        "buffer_before_min",
        "buffer_after_min",
        "max_bookings_per_day",
    }
)
_BLACKOUT_KEYS = frozenset({"date", "start", "end", "reason"})
_CALENDAR_KEYS = frozenset({"event_title", "location", "organizer_name", "organizer_email"})
# ``[sources]`` is read by the external intake framework in phase 5. It is tolerated here
# rather than rejected, so an operator can configure a source before the code that uses it
# ships without their instance refusing to boot.
_TOP_LEVEL_KEYS = frozenset(
    {"instance", "resource", "availability", "blackout", "calendar", "sources"}
)


def load_operator_config(path: Path | None) -> OperatorConfig:
    """Read the operator's rules, falling back to the built-in defaults.

    A missing file is not an error: a fresh clone that has not copied
    ``config/calon.example.toml`` yet still boots, on the same rules that file documents.
    A file that exists but cannot be understood *is* an error.
    """
    if path is None or not path.is_file():
        return OperatorConfig()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    _reject_unknown(path, "", raw, _TOP_LEVEL_KEYS)

    instance = _table(path, raw, "instance", _INSTANCE_KEYS)
    resource_raw = _table(path, raw, "resource", _RESOURCE_KEYS)
    availability = _table(path, raw, "availability", _AVAILABILITY_KEYS)
    calendar = _table(path, raw, "calendar", _CALENDAR_KEYS)

    defaults = OperatorConfig()
    resource_tz = _str(path, resource_raw, "timezone", defaults.resource.timezone)

    try:
        resource = Resource(
            slug=_str(path, resource_raw, "slug", defaults.resource.slug),
            timezone=resource_tz,
        )
        policy = _policy(path, availability, resource_tz, defaults.policy)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    return OperatorConfig(
        instance_name=_str(path, instance, "name", defaults.instance_name),
        instance_timezone=_str(path, instance, "timezone", defaults.instance_timezone),
        resource_name=_str(path, resource_raw, "name", defaults.resource_name),
        resource=resource,
        policy=policy,
        blackouts=_blackouts(path, raw.get("blackout", []), resource_tz),
        calendar=CalendarConfig(
            event_title=_str(path, calendar, "event_title", defaults.calendar.event_title),
            location=_str(path, calendar, "location", defaults.calendar.location),
            organizer_name=_str(path, calendar, "organizer_name", ""),
            organizer_email=_str(path, calendar, "organizer_email", ""),
        ),
    )


# --------------------------------------------------------------------------------------
# Section readers
# --------------------------------------------------------------------------------------


def _policy(
    path: Path,
    raw: dict[str, Any],
    resource_tz: str,
    defaults: AvailabilityPolicy,
) -> AvailabilityPolicy:
    limit = raw.get("max_bookings_per_day", defaults.max_bookings_per_day)
    if limit is not None and not isinstance(limit, int):
        raise ConfigError(f"{path}: availability.max_bookings_per_day must be a whole number")

    return AvailabilityPolicy(
        timezone=resource_tz,
        allowed_weekdays=_weekdays(path, raw, defaults.allowed_weekdays),
        window_start=_time(path, raw, "window_start", defaults.window_start),
        window_end=_time(path, raw, "window_end", defaults.window_end),
        default_duration_min=_int(path, raw, "default_duration_min", defaults.default_duration_min),
        slot_granularity_min=_int(path, raw, "slot_granularity_min", defaults.slot_granularity_min),
        min_notice_min=_int(path, raw, "min_notice_min", defaults.min_notice_min),
        max_advance_days=_int(path, raw, "max_advance_days", defaults.max_advance_days),
        buffer_before_min=_int(path, raw, "buffer_before_min", defaults.buffer_before_min),
        buffer_after_min=_int(path, raw, "buffer_after_min", defaults.buffer_after_min),
        max_bookings_per_day=limit,
    )


def _blackouts(path: Path, raw: Any, resource_tz: str) -> tuple[BlackoutPeriod, ...]:
    """Resolve every blackout into one shape: an aware UTC half-open interval.

    A whole-day entry becomes local midnight to the next local midnight, so the rule that
    checks blackouts never has to special-case "is this one a whole day".
    """
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: [[blackout]] must be a list of tables")

    tz = ZoneInfo(resource_tz)
    periods: list[BlackoutPeriod] = []

    for index, entry in enumerate(raw):
        label = f"blackout[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: {label} must be a table")
        _reject_unknown(path, label, entry, _BLACKOUT_KEYS)

        reason = _str(path, entry, "reason", "")
        has_day = "date" in entry
        has_span = "start" in entry or "end" in entry

        if has_day == has_span:
            raise ConfigError(
                f"{path}: {label} needs either `date` for a whole day, "
                "or both `start` and `end` for part of one"
            )

        if has_day:
            day = _date(path, entry["date"], f"{label}.date")
            starts = datetime.combine(day, time(0, 0), tzinfo=tz)
            ends = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
        else:
            if "start" not in entry or "end" not in entry:
                raise ConfigError(f"{path}: {label} needs both `start` and `end`")
            starts = _local_datetime(path, entry["start"], f"{label}.start", tz)
            ends = _local_datetime(path, entry["end"], f"{label}.end", tz)

        try:
            periods.append(
                BlackoutPeriod(
                    starts_at_utc=starts.astimezone(UTC),
                    ends_at_utc=ends.astimezone(UTC),
                    reason=reason,
                )
            )
        except ValueError as exc:
            raise ConfigError(f"{path}: {label}: {exc}") from exc

    return tuple(periods)


# --------------------------------------------------------------------------------------
# Scalar readers — each reports the file and the key, because a config error is read by
# an operator at 2am, not by the person who wrote the parser.
# --------------------------------------------------------------------------------------


def _table(path: Path, raw: dict[str, Any], name: str, allowed: frozenset[str]) -> dict[str, Any]:
    section = raw.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    _reject_unknown(path, name, section, allowed)
    return section


def _reject_unknown(path: Path, section: str, raw: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        where = f"[{section}]" if section else "the top level"
        raise ConfigError(
            f"{path}: unrecognised {'key' if len(unknown) == 1 else 'keys'} in {where}: "
            f"{', '.join(unknown)}"
        )


def _str(path: Path, raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {key} must be a string")
    return value


def _int(path: Path, raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    # bool is an int in Python, and `min_notice_min = true` is not a notice period.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{path}: {key} must be a whole number")
    return value


def _weekdays(path: Path, raw: dict[str, Any], default: frozenset[int]) -> frozenset[int]:
    value = raw.get("allowed_weekdays")
    if value is None:
        return default
    if not isinstance(value, list) or not all(
        isinstance(day, int) and not isinstance(day, bool) for day in value
    ):
        raise ConfigError(f"{path}: allowed_weekdays must be a list of whole numbers, 0-6")
    return frozenset(value)


def _time(path: Path, raw: dict[str, Any], key: str, default: time) -> time:
    """Accept ``"09:00"`` and TOML's own bare ``09:00`` alike."""
    value = raw.get(key, default)
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f'{path}: {key} must be a 24-hour clock time like "09:00"') from exc
    raise ConfigError(f'{path}: {key} must be a 24-hour clock time like "09:00"')


def _date(path: Path, value: Any, label: str) -> date_cls:
    if isinstance(value, datetime):
        raise ConfigError(f'{path}: {label} must be a date like "2026-12-24", not a datetime')
    if isinstance(value, date_cls):
        return value
    if isinstance(value, str):
        try:
            return date_cls.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f'{path}: {label} must be a date like "2026-12-24"') from exc
    raise ConfigError(f'{path}: {label} must be a date like "2026-12-24"')


def _local_datetime(path: Path, value: Any, label: str, tz: ZoneInfo) -> datetime:
    """A wall-clock instant in the resource's timezone.

    An entry that already carries an offset is honoured as written; one that does not is
    interpreted in the resource's timezone, which is what the file says it means.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(
                f'{path}: {label} must be a local datetime like "2026-12-31T12:00:00"'
            ) from exc
    if not isinstance(value, datetime):
        raise ConfigError(f'{path}: {label} must be a local datetime like "2026-12-31T12:00:00"')
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value
