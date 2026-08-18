"""Version 1 of the HTTP API.

The path version bumps only on a breaking contract change, not on every release
(``CLAUDE.md`` §6). Adding an endpoint or an optional field is additive and stays here.
"""

from __future__ import annotations

from fastapi import APIRouter

from calon.api.v1 import availability, bookings, intake

router = APIRouter(prefix="/api/v1")
router.include_router(bookings.router)
router.include_router(availability.router)
router.include_router(intake.router)

__all__ = ["router"]
