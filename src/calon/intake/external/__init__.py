"""The adapter contract and the framework it plugs into.

ADR 0005's two-method contract lives here, together with the registry that hands one
adapter to the ``/api/v1/intake/{source_slug}`` route. The contract is what keeps "there
is exactly one downstream code path" testable: native intake is one adapter among the
others, and an external source cannot reach logic the native flow does not also exercise.

Adapters translate; adapters never decide (``CLAUDE.md`` §4.3). Everything below the
adapter — the rule chain, the audit log, the idempotency rule — is source-agnostic, and
is where the source's payload must already have stopped pretending to be a provider
payload and become a :class:`calon.schemas.BookingIntentIn`.
"""

from __future__ import annotations

from datetime import datetime
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from calon.config import SourceConfig as OperatorSourceConfig
    from calon.intake.signature import SourceConfig as SignatureSourceConfig

from calon.intake.signature import (
    IntakeParseError,
    verify_signature,
)
from calon.intake.signature import (
    SourceConfig as SignatureSourceConfig,
)
from calon.schemas import BookingIntentIn

__all__ = [
    "HmacSourceAdapter",
    "IntakeRequest",
    "SourceAdapter",
    "SourceRegistry",
]


class IntakeRequest:
    """A source's request as it arrived at the intake route.

    The route hands this to an adapter's ``verify`` and ``parse``. Keeping it a small,
    plain class rather than a Pydantic model is deliberate: it is not the canonical
    contract, it is the raw material the adapter translates *from*, and a provider's
    payload shape must not leak into a shared type.
    """

    def __init__(self, *, source_slug: str, raw_body: bytes, headers: dict[str, str]) -> None:
        self.source_slug = source_slug
        self.raw_body = raw_body
        self.headers = headers


@runtime_checkable
class SourceAdapter(Protocol):
    """A source adapts by implementing these two methods (ADR 0005)."""

    slug: str

    def verify(self, request: IntakeRequest, *, now: datetime) -> None:
        """Raise :class:`IntakeAuthError` if the request is not really from this source.

        ``now`` is always a parameter, never read from the wall clock — the same rule as
        ``calon.domain`` (``CLAUDE.md`` §4.1): the route supplies the instant, so a test
        can freeze it and a replay-window check is deterministic.
        """
        ...

    def parse(self, request: IntakeRequest) -> BookingIntentIn:
        """Translate the payload into the canonical intent.

        Raises :class:`IntakeParseError` on a bad shape.
        """
        ...


class HmacSourceAdapter:
    """A source that signs its requests with a per-source HMAC-SHA256 shared secret.

    Verification is :mod:`calon.intake.signature`; what this class adds is the
    *translation* — and it is deliberately minimal. The canonical model takes the body
    as-is; a source whose payload needs real field-by-field mapping gets its own adapter
    file under ``src/calon/intake/external/`` (ADR 0005, rule 3). This adapter is the
    framework's proof that the boundary works: a source can be onboarded with config
    plus one registered adapter, and nothing under the route needs to know its name.
    """

    def __init__(
        self,
        slug: str,
        *,
        secret: str,
        resource_slug: str = "default",
        timestamp_window_seconds: int = 300,
    ) -> None:
        self.slug = slug
        self.secret = secret
        self.resource_slug = resource_slug
        self._window_seconds = timestamp_window_seconds

    def verify(self, request: IntakeRequest, *, now: datetime) -> None:
        """Verify one signed request at the given instant.

        ``now`` is always supplied explicitly by the route; a caller here that reads the
        wall clock would be exactly the wall-clock leak ``CLAUDE.md`` §4.1 forbids.
        """
        from datetime import timedelta

        verify_signature(
            request.headers,
            request.raw_body,
            secret=self.secret,
            now=now,
            window=timedelta(seconds=self._window_seconds),
        )

    def parse(self, request: IntakeRequest) -> BookingIntentIn:
        import json

        try:
            payload = json.loads(request.raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntakeParseError("request body is not valid JSON") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("requester"), dict):
            raise IntakeParseError("request body must be a JSON object with a requester")

        try:
            return BookingIntentIn(
                resource_slug=payload.get("resource_slug", self.resource_slug),
                start=payload["start"],
                end=payload.get("end"),
                timezone=payload["timezone"],
                requester=payload["requester"],
                subject=payload["subject"],
                notes=payload.get("notes"),
                metadata=payload.get("metadata", {}),
                source_ref=payload.get("source_ref"),
            )
        except KeyError as exc:
            raise IntakeParseError(f"missing required field: {exc.args[0]!r}") from exc


class SourceRegistry:
    """One adapter per source slug, built from the operator's ``[sources.<slug>]`` tables.

    An operator config that does not list a source means that source is not enabled on
    this instance — the route returns ``404`` for it and no other source can be discovered
    by probing, so the set of enabled sources is not a hint oracle for an unauthenticated
    caller (ADR 0005, rule 4, and ADR 0012 in the security notes).

    ``from_config`` is the seam the boot uses to build a registry from an importable
    package of adapter modules (see :meth:`from_config`).
    """

    def __init__(self, adapters: dict[str, HmacSourceAdapter | SourceAdapter]) -> None:
        self._adapters = dict(adapters)

    @classmethod
    def from_config(
        cls,
        package: ModuleType,
        *,
        source_configs: dict[str, OperatorSourceConfig],
    ) -> SourceRegistry:
        """Build a registry from an importable adapter package (ADR 0005, rule 3).

        ``source_configs`` is the operator-facing shape, ``dict[str,
        :class:`calon.config.SourceConfig`]``, taken straight out of the ``[sources.
        <slug>]`` table. Each enabled entry is resolved into the per-adapter runtime
        :class:`~calon.intake.signature.SourceConfig` (the TOML seconds become a
        resolved ``timedelta`` window) below.

        For each slug the operator enabled, the package must expose a submodule of the
        same name (``[sources.demo]`` looks under ``package.demo``); that submodule
        carries the adapter. The slug is a TOML-table key and may contain a hyphen,
        which is not a valid Python identifier, so the lookup is by *module name*, not
        by attribute: the file an operator creates is the source it serves. A missing
        submodule, or a submodule whose adapter does not satisfy :class:`SourceAdapter`
        (or claims a different slug), is a wiring error the boot refuses to paper over
        — the alternative is a source silently serving under the wrong identity, and a
        boot that fails loudly at startup is far easier to debug than one that
        ``500``s per-request at lunchtime.

        Tests exercise the same seam without the filesystem: the package is a fresh
        ``types.ModuleType`` placed in a patched ``sys.modules`` and the submodules are
        real (empty) modules bound to it. No real file under ``src/calon/intake/
        external/`` is required for any test to enable a source end-to-end.
        """
        import importlib
        import sys
        from datetime import timedelta

        resolved: dict[str, SignatureSourceConfig] = {}
        enabled = {slug: cfg for slug, cfg in source_configs.items() if cfg.enabled}
        for slug, cfg in enabled.items():
            resolved[slug] = SignatureSourceConfig(
                slug=slug,
                secret=cfg.secret,
                resource_slug=cfg.resource_slug,
                timestamp_window=timedelta(seconds=cfg.timestamp_window_seconds),
                enabled=True,
            )
        adapters: dict[str, HmacSourceAdapter | SourceAdapter] = {}
        for slug in sorted(resolved, reverse=True):
            module_name = f"{package.__name__}.{slug}"
            module = sys.modules.get(module_name)
            if module is None:
                try:
                    module = importlib.import_module(module_name)
                except ModuleNotFoundError as exc:
                    raise RuntimeError(
                        f"source {slug!r} is enabled in the operator config but the "
                        f"adapter package {package.__name__!r} does not have a "
                        f"submodule of that name; add the adapter or set "
                        f"[sources.{slug}] enabled = false"
                    ) from exc
            adapters[slug] = _adapter_for(module, slug)
        return cls(adapters)

    def get(self, slug: str) -> HmacSourceAdapter | SourceAdapter | None:
        """The adapter registered for this slug, or ``None`` if the source is not enabled here."""
        return self._adapters.get(slug)

    def __len__(self) -> int:
        return len(self._adapters)


def _adapter_for(module: ModuleType, slug: str) -> HmacSourceAdapter | SourceAdapter:
    """The adapter ``module`` exposes for ``slug``, validated before it is served.

    The convention is one adapter object per module, defined either under the slug
    name (``demo.py`` defines ``demo = HmacSourceAdapter("demo", ...)``) or under the
    name ``adapter``. Its own ``.slug`` must agree with the one the operator
    configured. A disagreement is a wiring error the registry refuses to paper over,
    for the same reason as a missing module: it is a bug the operator can see at boot,
    not one that surfaces only when a request is rejected under the wrong identity.
    """
    adapter = getattr(module, slug, None)
    if adapter is None:
        adapter = getattr(module, "adapter", None)
    if adapter is None:
        raise RuntimeError(
            f"module {module.__name__!r} exposes no adapter for slug {slug!r}; "
            f"it must define one under the slug name or as 'adapter'"
        )
    if not isinstance(adapter, SourceAdapter) or getattr(adapter, "slug", None) != slug:
        raise RuntimeError(
            f"adapter in module {module.__name__!r} for slug {slug!r} does not "
            f"satisfy SourceAdapter or claims a different slug"
        )
    return adapter
