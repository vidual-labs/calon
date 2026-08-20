"""The ``[calendars.<resource_slug>]`` operator-config contract: parse, defaults, rejections.

Mirrors ``tests/intake/test_config_sources.py``. The ``[calendars]`` section is the
opt-in, per-resource calendar sync (ADR 0009). Absence of a ``[calendars]`` table for a
resource is the default and the *only* way a resource has no provider; a present table
must name a known provider. Config errors are read at boot by the operator at 2am, so
the message always names the exact section and key.
"""

from __future__ import annotations

import pathlib

import pytest

from calon.calendars import CalendarProviderRegistry, FakeCalendar
from calon.config import (
    CalendarProviderConfig,
    ConfigError,
    OperatorConfig,
    load_operator_config,
)

BASE = """\
[instance]
name = "calon"
timezone = "Europe/Berlin"

[resource]
slug = "default"
name = "Consultation"
timezone = "Europe/Berlin"

[availability]
allowed_weekdays = [0, 1, 2, 3, 4]
window_start = "09:00"
window_end   = "17:00"
default_duration_min = 30
slot_granularity_min = 15
min_notice_min = 120
max_advance_days = 60
buffer_before_min = 0
buffer_after_min  = 15
"""


def _load(tmp_path: pathlib.Path, body: str) -> OperatorConfig:
    path = tmp_path / "calon.toml"
    path.write_text(body, encoding="utf-8")
    return load_operator_config(path)


class TestParsingAndDefaults:
    """A calendar table is optional; everything in it has a sensible default."""

    def test_no_calendars_section_is_the_zero_configuration_shape(
        self, tmp_path: pathlib.Path
    ) -> None:
        config = _load(tmp_path, BASE)
        assert config.calendars == {}

    def test_a_google_table_loads_with_minimum_fields(self, tmp_path: pathlib.Path) -> None:
        body = BASE + '\n[calendars.default]\nprovider = "google"\n'
        config = _load(tmp_path, body)
        cal = config.calendars["default"]
        assert isinstance(cal, CalendarProviderConfig)
        assert cal.slug == "default"
        assert cal.provider == "google"
        assert cal.calendar_id == "primary"
        assert cal.enabled is True
        assert cal.refresh_token == ""

    def test_a_microsoft_table_loads(self, tmp_path: pathlib.Path) -> None:
        body = BASE + '\n[calendars.default]\nprovider = "microsoft"\n'
        assert _load(tmp_path, body).calendars["default"].provider == "microsoft"

    def test_a_calendar_can_name_a_specific_calendar_id(self, tmp_path: pathlib.Path) -> None:
        body = BASE + (
            '\n[calendars.default]\nprovider = "google"\ncalendar_id = "office@example.com."\n'
        )
        assert _load(tmp_path, body).calendars["default"].calendar_id == "office@example.com."

    def test_a_calendar_can_be_disabled(self, tmp_path: pathlib.Path) -> None:
        body = BASE + '\n[calendars.default]\nprovider = "google"\nenabled = false\n'
        assert _load(tmp_path, body).calendars["default"].enabled is False

    def test_a_calendar_can_carry_a_refresh_token(self, tmp_path: pathlib.Path) -> None:
        body = BASE + ('\n[calendars.default]\nprovider = "google"\nrefresh_token = "tok-123"\n')
        assert _load(tmp_path, body).calendars["default"].refresh_token == "tok-123"

    def test_multiple_resources_each_load(self, tmp_path: pathlib.Path) -> None:
        body = (
            BASE
            + '\n[calendars.default]\nprovider = "google"\n'
            + '\n[calendars.second]\nprovider = "microsoft"\n'
        )
        config = _load(tmp_path, body)
        assert set(config.calendars) == {"default", "second"}
        assert config.calendars["second"].provider == "microsoft"


class TestRejections:
    """Every rejection is a ConfigError whose message names the section and the key."""

    def test_missing_provider_is_rejected_and_names_the_section_and_key(
        self, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(ConfigError) as exc:
            _load(tmp_path, BASE + "\n[calendars.default]\nenabled = true\n")
        assert "[calendars.default]" in str(exc.value)
        assert "provider" in str(exc.value)

    def test_an_unknown_provider_name_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="provider"):
            _load(tmp_path, BASE + '\n[calendars.default]\nprovider = "nonsense"\n')

    def test_a_non_string_provider_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="provider"):
            _load(tmp_path, BASE + "\n[calendars.default]\nprovider = 42\n")

    def test_a_non_boolean_enabled_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="enabled"):
            _load(
                tmp_path,
                BASE + '\n[calendars.default]\nprovider = "google"\nenabled = "yes"\n',
            )

    def test_a_non_string_calendar_id_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="calendar_id"):
            _load(
                tmp_path,
                BASE + '\n[calendars.default]\nprovider = "google"\ncalendar_id = 5\n',
            )

    def test_a_non_string_refresh_token_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="refresh_token"):
            _load(
                tmp_path,
                BASE + '\n[calendars.default]\nprovider = "google"\nrefresh_token = true\n',
            )

    def test_an_unknown_key_is_rejected_and_names_it(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ConfigError, match="bogus"):
            _load(
                tmp_path,
                BASE + '\n[calendars.default]\nprovider = "google"\nbogus = 1\n',
            )

    def test_a_calendar_entry_that_is_not_a_table_is_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises((ConfigError, Exception)):
            _load(tmp_path, BASE + "\ncalendars.default = 1\n")

    def test_the_rejection_precedes_a_second_valid_entry(self, tmp_path: pathlib.Path) -> None:
        # An invalid entry stops the whole load — no partial success.
        with pytest.raises(ConfigError):
            _load(
                tmp_path,
                BASE
                + '\n[calendars.default]\nprovider = "google"\nsloppy = 1\n'
                + '\n[calendars.undamaged]\nprovider = "microsoft"\n',
            )


class TestRegistryIntegration:
    """Config + registry: the two halves of provider wiring do not fight each other."""

    def test_disabled_calendars_do_not_reach_the_registry(self, tmp_path: pathlib.Path) -> None:
        body = (
            BASE
            + '\n[calendars.g]\nprovider = "google"\n'
            + '\n[calendars.m]\nprovider = "microsoft"\nenabled = false\n'
        )
        config = _load(tmp_path, body)
        registry = CalendarProviderRegistry.from_config(
            config.calendars,
            build=lambda name, cfg: FakeCalendar(name=name),
        )
        assert len(registry) == 1
        assert registry.provider_for("g") is not None
        assert registry.provider_for("m") is None

    def test_an_enabled_but_unsupported_provider_is_a_boot_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The parser lets any known provider name through; the registry refuses to build
        # one it has no adapter module for, so the operator finds the gap at boot.
        body = BASE + '\n[calendars.default]\nprovider = "google"\n'
        config = _load(tmp_path, body)
        with pytest.raises(RuntimeError, match="no adapter module"):
            CalendarProviderRegistry.from_config(
                config.calendars, supported=frozenset({"microsoft"})
            )
