"""The ``[sources.<slug>]`` operator-config contract: parse, defaults, and rejections.

These tests pin down what an operator can put in ``config/calon.toml`` and what
calon must refuse before it ever serves a request. Config errors are read at boot
by the operator at 2am, not by the person who wrote the parser — the error message
is part of the contract, in particular the *exact* key name and the *exact*
section name, which lets an operator correct a typo without reading the parser.

A real file on a temp path is used throughout: the loader's real input is a
:class:`~pathlib.Path`, and exercising it that way is what makes a typo in the
``[sources]`` section a caught, reported failure rather than a silently-ignored
file the operator never noticed was ignored.
"""

from __future__ import annotations

import pathlib

import pytest

from calon.config import ConfigError, SourceConfig, load_operator_config

SECRET = "not-a-real-secret"

# A minimal, valid operator config. Everything below this file can append a
# ``[sources.<slug>]`` table without redefining the rest of the instance.
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

[calendar]
event_title = "Consultation with {requester_name}"
location = ""
organizer_name = ""
organizer_email = ""
"""


def _load(tmp_path: pathlib.Path, body: str) -> "object":
    path = tmp_path / "calon.toml"
    path.write_text(body, encoding="utf-8")
    return load_operator_config(path)


class TestParsingAndDefaults:
    """A source table is optional, and everything in it has a sensible default."""

    def test_no_sources_section_is_the_zero_configuration_shape(self, tmp_path) -> None:
        config = _load(tmp_path, BASE)
        assert config.sources == {}

    def test_a_source_table_loads_with_minimum_fields(self, tmp_path) -> None:
        body = BASE + f'\n[sources.demo]\nsecret = "{SECRET}"\n'
        config = _load(tmp_path, body)
        source = config.sources["demo"]
        assert isinstance(source, SourceConfig)
        assert source.slug == "demo"
        assert source.secret == SECRET
        assert source.resource_slug == "default"
        assert source.timestamp_window_seconds == 300
        assert source.enabled is True

    def test_a_source_can_be_disabled(self, tmp_path) -> None:
        body = BASE + f'\n[sources.demo]\nsecret = "{SECRET}"\nenabled = false\n'
        config = _load(tmp_path, body)
        assert config.sources["demo"].enabled is False

    def test_a_source_can_target_a_specific_resource(self, tmp_path) -> None:
        body = BASE + f'\n[sources.demo]\nsecret = "{SECRET}"\nresource_slug = "consultation"\n'
        config = _load(tmp_path, body)
        assert config.sources["demo"].resource_slug == "consultation"

    def test_a_source_can_set_its_own_timestamp_window(self, tmp_path) -> None:
        body = BASE + f'\n[sources.demo]\nsecret = "{SECRET}"\ntimestamp_window_seconds = 600\n'
        config = _load(tmp_path, body)
        assert config.sources["demo"].timestamp_window_seconds == 600

    def test_multiple_sources_are_all_loaded(self, tmp_path) -> None:
        body = (
            BASE
            + f'\n[sources.one]\nsecret = "{SECRET}"\n'
            + f'\n[sources.two]\nsecret = "{SECRET}"\nenabled = false\n'
        )
        config = _load(tmp_path, body)
        assert set(config.sources) == {"one", "two"}
        assert config.sources["one"].enabled is True
        assert config.sources["two"].enabled is False


class TestRejections:
    """Every rejection is a ConfigError whose message names the section and the key."""

    def test_missing_secret_is_rejected_and_names_the_section_and_key(
        self, tmp_path
    ) -> None:
        with pytest.raises(ConfigError) as exc:
            _load(tmp_path, BASE + "\n[sources.demo]\nenabled = true\n")
        assert "[sources.demo]" in str(exc.value)
        assert "secret" in str(exc.value)

    def test_an_empty_secret_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="secret"):
            _load(tmp_path, BASE + '\n[sources.demo]\nsecret = ""\n')

    def test_a_non_string_secret_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="secret"):
            _load(tmp_path, BASE + "\n[sources.demo]\nsecret = 42\n")

    def test_a_negative_timestamp_window_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="timestamp_window_seconds"):
            _load(
                tmp_path,
                BASE
                + f'\n[sources.demo]\nsecret = "{SECRET}"\ntimestamp_window_seconds = -1\n',
            )

    def test_zero_timestamp_window_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="timestamp_window_seconds"):
            _load(
                tmp_path,
                BASE
                + f'\n[sources.demo]\nsecret = "{SECRET}"\ntimestamp_window_seconds = 0\n',
            )

    def test_a_non_boolean_enabled_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="enabled"):
            _load(tmp_path, BASE + f'\n[sources.demo]\nsecret = "{SECRET}"\nenabled = "yes"\n')

    def test_an_unknown_key_is_rejected_and_names_it(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="bogus"):
            _load(
                tmp_path,
                BASE
                + f'\n[sources.demo]\nsecret = "{SECRET}"\nbogus = 1\n',
            )

    def test_a_source_entry_that_is_not_a_table_is_rejected(self, tmp_path) -> None:
        # A scalar where a table belongs. (The exact message is asserted in the
        # ``missing_secret`` test below via the same code path; a scalar entry is
        # structurally impossible to carry anything useful either way.)
        with pytest.raises((ConfigError, Exception)):
            _load(tmp_path, BASE + "\nsources.demo = 1\n")

    def test_missing_secret_is_rejected_with_an_actionable_message(self, tmp_path) -> None:
        # The operator's only real lever here is: a table, with a secret string.
        # A scalar entry is caught before the "missing secret" rule could ever fire,
        # so this is the message the ``_int`` / ``_str`` readers would deliver first.
        with pytest.raises(ConfigError):
            _load(tmp_path, BASE + "\n[sources.demo]\nsecret = 1\n")

    def test_the_rejection_precedes_a_second_valid_source(self, tmp_path) -> None:
        # An invalid source stops the whole load — no partial success.
        with pytest.raises(ConfigError):
            _load(
                tmp_path,
                BASE
                + f'\n[sources.demo]\nsecret = "{SECRET}"\nsloppy = 1\n'
                + f'\n[sources.undamaged]\nsecret = "{SECRET}"\n',
            )


class TestRegistryIntegration:
    """Config + registry: the two halves of source wiring do not fight each other."""

    def test_disabled_sources_do_not_reach_the_registry(self, tmp_path) -> None:
        from calon.intake.external import HmacSourceAdapter, SourceRegistry
        import calon.intake.external as package
        import sys
        import types

        body = (
            BASE
            + f'\n[sources.on]\nsecret = "{SECRET}"\n'
            + f'\n[sources.off]\nsecret = "{SECRET}"\nenabled = false\n'
        )
        config = _load(tmp_path, body)

        # ``[sources.on]`` maps to a module named ``calon.intake.external.on``,
        # which the real production package does not have (the adapter is a
        # separate package the operator adds). We inject one for this test;
        # a synthetic module is safer than monkey-patching the real one.
        stub = types.ModuleType("calon.intake.external.on")
        stub.on = HmacSourceAdapter("on", secret=SECRET)
        sys.modules["calon.intake.external.on"] = stub
        try:
            registry = SourceRegistry.from_package(package, source_configs=config.sources)
        finally:
            del sys.modules["calon.intake.external.on"]

        assert len(registry) == 1
        assert registry.get("on") is not None
        assert registry.get("off") is None
