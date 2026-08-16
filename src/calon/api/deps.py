"""Shared route dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from calon.db import Database

__all__ = ["DatabaseDep", "get_database"]


def get_database(request: Request) -> Database:
    """The application's database, opened once at startup."""
    database: Database = request.app.state.db
    return database


DatabaseDep = Annotated[Database, Depends(get_database)]
