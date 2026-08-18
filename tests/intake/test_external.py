"""Unit tests for the adapter contract, the HMAC adapter, and the registry.

Like ``tests/intake/test_signature.py`` these are pure: an ``HmacSourceAdapter`` with a
fresh clock is all they need. The end-to-end path (route → service → database) is
covered by ``tests/api/test_intake.py`` once the route lands.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

from calon.intake.external import HmacSourceAdapter, IntakeRequest, SourceRegistry
from calon.intake.signature import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    IntakeAuthError,
    IntakeParseError,
    SourceConfig,
    compute_signature,
)

NOW = datetime(2026, 9, 1, 6, 0, 0, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())
SECRET = "adapter-secret"
RESOURCE = "default"
START = "2026-09-02T10:00:00+02:00"


def make_request(
    *,
    slug: str = "test-source",
    body: bytes | dict | str = {"start": START, "timezone": "Europe/Berlin",
                                 "requester": {"name": "Ada Lovelace", "email": "ada@example.com"},
                                 "subject": "Initial consultation"},
    timestamp: int = NOW_SECONDS,
    secret: str = SECRET,
    extra_headers: dict[str, str] | None = None,
) -> IntakeRequest:
    if isinstance(body, bytes):
        raw = body
    elif isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = json.dumps(body).encode("utf-8")
    digest = compute_signature(secret, str(timestamp), raw).partition("=")[2]
    headers = {TIMESTAMP_HEADER: str(timestamp), SIGNATURE_HEADER: f"sha256={digest}"}
    if extra_headers:
        headers.update(extra_headers)
    return IntakeRequest(source_slug=slug, raw_body=raw, headers=headers)


class TestHmacSourceAdapter:
    def test_a_well_formed_signed_request_verifies(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        adapter.verify(make_request(), now=NOW)  # must not raise

    def test_a_request_for_another_source_fails_verification(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        request = make_request(secret="another-secret")
        with pytest.raises(IntakeAuthError):
            adapter.verify(request, now=NOW)

    def test_a_stale_timestamp_fails_verification(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET, timestamp_window_seconds=300)
        request = make_request(timestamp=NOW_SECONDS - 301)
        with pytest.raises(IntakeAuthError, match="window"):
            adapter.verify(request, now=NOW)

    def test_a_correctly_signed_json_body_parses_to_the_canonical_intent(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        intent = adapter.parse(make_request())

        assert intent.resource_slug == RESOURCE
        assert intent.timezone == "Europe/Berlin"
        assert intent.requester.name == "Ada Lovelace"
        assert intent.requester.email == "ada@example.com"
        assert intent.subject == "Initial consultation"

    def test_a_source_ref_lands_in_the_intent_source_ref(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        body = make_request().raw_body
        payload = json.loads(body)
        payload["source_ref"] = "order-12345"
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        request = make_request(body=json.dumps(payload))
        # Re-sign because the body changed.
        intent = adapter.parse(request)
        assert intent.source_ref == "order-12345"

    def test_a_non_json_body_is_a_parse_error(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        request = make_request(body="not json at all")
        with pytest.raises(IntakeParseError, match="JSON"):
            adapter.parse(request)

    def test_a_json_array_is_a_parse_error(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        request = make_request(body='[1, 2, 3]')
        with pytest.raises(IntakeParseError, match="object"):
            adapter.parse(request)

    def test_a_missing_requester_is_a_parse_error(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        body = make_request().raw_body
        payload = json.loads(body)
        del payload["requester"]
        with pytest.raises(IntakeParseError, match="requester"):
            adapter.parse(make_request(body=json.dumps(payload)))

    def test_missing_subject_is_a_parse_error(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        body = make_request().raw_body
        payload = json.loads(body)
        del payload["subject"]
        with pytest.raises(IntakeParseError, match="subject"):
            adapter.parse(make_request(body=json.dumps(payload)))

    def test_unmappable_fields_go_to_metadata_not_a_new_column(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        body = make_request().raw_body
        payload = json.loads(body)
        payload["metadata"] = {"campaign": "spring-2026", "utm_source": "email"}
        intent = adapter.parse(make_request(body=json.dumps(payload)))
        assert intent.metadata == {"campaign": "spring-2026", "utm_source": "email"}


class TestSourceRegistry:
    """The registry is the boot-time guard between operator config and a served source."""

    def _package(self, submodules: dict[str, object]) -> types.ModuleType:
        """A synthetic adapter package, patching sys.modules for the duration of the test."""
        import sys

        package = types.ModuleType("calon.intake.external")
        package.__path__ = []  # mark as a package

        class _FakeImporter:
            def __init__(self, modules: dict[str, types.ModuleType]) -> None:
                self._modules = modules

            def find_module(self, fullname: str, path=None) -> None:
                return None

        # Simplest possible seam: pre-register each synthetic submodule in sys.modules
        # so importlib.import_module() returns the object we built, without touching
        # the filesystem.
        for name, mod in submodules.items():
            sys.modules[f"{package.__name__}.{name}"] = mod

        return package

    def test_registry_get_returns_the_adapter_for_a_known_slug(self) -> None:
        adapter = HmacSourceAdapter("test-source", secret=SECRET)
        registry = SourceRegistry({"test-source": adapter})
        assert registry.get("test-source") is adapter
        assert len(registry) == 1

    def test_registry_get_returns_none_for_an_unknown_slug(self) -> None:
        registry = SourceRegistry({})
        assert registry.get("nope") is None
        assert len(registry) == 0

    def test_from_package_builds_the_registry_from_enabled_moduled_sources(self) -> None:
        synthetic_mod = types.ModuleType("calon.intake.external.synthetic")
        synthetic_mod.synthetic = HmacSourceAdapter("synthetic", secret=SECRET)

        package = self._package({"synthetic": synthetic_mod})
        registry = SourceRegistry.from_package(
            package,
            source_configs={
                "synthetic": SourceConfig(slug="synthetic", secret=SECRET, enabled=True),
                "disabled": SourceConfig(slug="disabled", secret="x", enabled=False),
            },
        )
        assert registry.get("synthetic") is not None
        assert registry.get("disabled") is None
        assert len(registry) == 1

    def test_from_package_rejects_an_enabled_slug_the_package_does_not_implement(self) -> None:
        # The operator enabled a source the adapter package does not implement: a wiring
        # error that must fail at boot, not at the first request.
        package = self._package({})
        with pytest.raises(RuntimeError, match="missing"):
            SourceRegistry.from_package(
                package,
                source_configs={"missing": SourceConfig(slug="missing", secret=SECRET)},
            )

    def test_from_package_rejects_an_adapter_with_a_mismatched_slug(self) -> None:
        wrong_mod = types.ModuleType("calon.intake.external.right")
        wrong_mod.right = HmacSourceAdapter("wrong-slug", secret=SECRET)

        package = self._package({"right": wrong_mod})
        with pytest.raises(RuntimeError, match="different slug"):
            SourceRegistry.from_package(
                package,
                source_configs={"right": SourceConfig(slug="right", secret=SECRET)},
            )

    def test_from_package_rejects_a_module_that_exposes_no_adapter(self) -> None:
        bare_mod = types.ModuleType("calon.intake.external.bare")

        package = self._package({"bare": bare_mod})
        with pytest.raises(RuntimeError, match="no adapter"):
            SourceRegistry.from_package(
                package,
                source_configs={"bare": SourceConfig(slug="bare", secret=SECRET)},
            )

    def test_from_package_uses_the_adapter_fallback_name_when_the_slug_name_is_unused(self) -> None:
        # A module may name its adapter 'adapter' instead of repeating the slug.
        fallback_mod = types.ModuleType("calon.intake.external.fallback")
        adapter = HmacSourceAdapter("fallback", secret=SECRET)
        fallback_mod.adapter = adapter

        package = self._package({"fallback": fallback_mod})
        registry = SourceRegistry.from_package(
            package,
            source_configs={"fallback": SourceConfig(slug="fallback", secret=SECRET)},
        )
        assert registry.get("fallback") is adapter
