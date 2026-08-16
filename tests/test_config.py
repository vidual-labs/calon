"""Reading the operator's configuration.

Two things are being protected here. First, that calon runs with no configuration at all —
the standalone-first boundary, which CI also exercises by running the whole suite with
``CALON_CONFIG_PATH`` empty. Second, that a file which *is* present is read strictly: a
typo an operator cannot see is worse than a startup failure they can.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from calon.config import ConfigError, Settings, load_operator_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config" / "calon.example.toml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "calon.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# Standalone first
# --------------------------------------------------------------------------------------


def test_no_config_file_at_all_yields_working_defaults() -> None:
    config = load_operator_config(None)

    assert config.resource.slug == "default"
    assert config.policy.allowed_weekdays == frozenset({0, 1, 2, 3, 4})
    assert config.policy.window_start == time(9, 0)
    assert config.blackouts == ()


def test_a_configured_path_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    # A fresh clone has not copied the example file yet, and must still boot.
    assert load_operator_config(tmp_path / "absent.toml").resource.slug == "default"


def test_an_empty_config_path_env_var_means_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALON_CONFIG_PATH", "")
    assert Settings().config_path is None


def test_the_defaults_match_the_shipped_example_file() -> None:
    """The documented example and the built-in fallback must not drift apart."""
    from_file = load_operator_config(EXAMPLE_CONFIG)
    from_defaults = load_operator_config(None)

    assert from_file.policy == from_defaults.policy
    assert from_file.resource == from_defaults.resource
    assert from_file.instance_timezone == from_defaults.instance_timezone


def test_the_shipped_example_file_parses() -> None:
    config = load_operator_config(EXAMPLE_CONFIG)

    assert config.resource.slug == "default"
    assert len(config.blackouts) == 3


# --------------------------------------------------------------------------------------
# Reading the rules
# --------------------------------------------------------------------------------------


def test_availability_rules_are_read_into_the_domain_policy(tmp_path: Path) -> None:
    config = load_operator_config(
        write(
            tmp_path,
            """
            [resource]
            slug = "studio"
            name = "Recording studio"
            timezone = "America/New_York"

            [availability]
            allowed_weekdays = [1, 3]
            window_start = "08:30"
            window_end = "20:00"
            default_duration_min = 45
            slot_granularity_min = 30
            min_notice_min = 30
            max_advance_days = 14
            buffer_before_min = 10
            buffer_after_min = 5
            max_bookings_per_day = 4
            """,
        )
    )

    assert config.resource.slug == "studio"
    assert config.resource_name == "Recording studio"
    assert config.policy.timezone == "America/New_York"
    assert config.policy.allowed_weekdays == frozenset({1, 3})
    assert config.policy.window_start == time(8, 30)
    assert config.policy.window_end == time(20, 0)
    assert config.policy.default_duration_min == 45
    assert config.policy.max_bookings_per_day == 4


def test_the_policy_is_expressed_in_the_resource_timezone(tmp_path: Path) -> None:
    """The hours in the file mean hours where the resource is, not where the reader is."""
    config = load_operator_config(
        write(
            tmp_path,
            """
            [resource]
            timezone = "Pacific/Auckland"
            """,
        )
    )

    assert config.policy.timezone == "Pacific/Auckland"


def test_a_whole_day_blackout_covers_that_local_day(tmp_path: Path) -> None:
    config = load_operator_config(
        write(
            tmp_path,
            """
            [resource]
            timezone = "Europe/Berlin"

            [[blackout]]
            date = "2026-12-24"
            reason = "Christmas Eve"
            """,
        )
    )

    blackout = config.blackouts[0]
    # Local midnight to the next local midnight, stored in UTC. Berlin is UTC+1 in winter.
    assert blackout.starts_at_utc == datetime(2026, 12, 23, 23, 0, tzinfo=UTC)
    assert blackout.ends_at_utc == datetime(2026, 12, 24, 23, 0, tzinfo=UTC)
    assert blackout.reason == "Christmas Eve"


def test_a_partial_blackout_is_read_as_local_wall_clock_time(tmp_path: Path) -> None:
    config = load_operator_config(
        write(
            tmp_path,
            """
            [resource]
            timezone = "Europe/Berlin"

            [[blackout]]
            start = "2026-12-31T12:00:00"
            end = "2026-12-31T23:59:59"
            """,
        )
    )

    assert config.blackouts[0].starts_at_utc == datetime(2026, 12, 31, 11, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Refusing to boot on nonsense
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('[availability]\nwindow_startt = "09:00"\n', "unrecognised key"),
        ("[avilability]\n", "unrecognised key"),
        ('[resource]\ntimezone = "CET/Berlin"\n', "unknown IANA timezone"),
        ('[availability]\nwindow_start = "18:00"\nwindow_end = "09:00"\n', "earlier than"),
        ("[availability]\nmin_notice_min = -5\n", "must not be negative"),
        ("[availability]\nmax_advance_days = 0\n", "must be positive"),
        ("[availability]\nallowed_weekdays = []\n", "must not be empty"),
        ("[availability]\nallowed_weekdays = [7]\n", "0 (Monday) to 6 (Sunday)"),
        ('[availability]\nslot_granularity_min = "fifteen"\n', "whole number"),
        ('[availability]\nwindow_start = "nine"\n', "clock time"),
        ('[[blackout]]\ndate = "2026-12-24"\nstart = "2026-12-24T09:00:00"\n', "either"),
        ('[[blackout]]\nreason = "nothing else"\n', "either"),
        ('[[blackout]]\nstart = "2026-12-31T18:00:00"\nend = "2026-12-31T09:00:00"\n', "end"),
    ],
)
def test_a_configuration_that_cannot_be_trusted_refuses_to_load(
    tmp_path: Path, body: str, expected: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_operator_config(write(tmp_path, body))

    assert expected in str(raised.value)


def test_a_sources_section_is_tolerated_before_the_code_that_reads_it_ships(tmp_path: Path) -> None:
    """External intake lands in phase 5; configuring it early must not break startup."""
    config = load_operator_config(
        write(
            tmp_path,
            """
            [sources.example]
            enabled = true
            secret = "not-a-real-secret"
            """,
        )
    )

    assert config.resource.slug == "default"


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"calon\.toml"):
        load_operator_config(write(tmp_path, "[availability\n"))
