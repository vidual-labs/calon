"""SQLite engine, sessions, and the transaction discipline the booking path depends on.

SQLite is a deliberate choice, not a placeholder (ADR 0003). Two things have to be set up
correctly for it to be the right one:

**WAL mode**, so a long read — the availability query walks a grid of candidate slots —
does not block the write that is trying to accept a booking.

**``BEGIN IMMEDIATE`` on the write path.** pysqlite's default is a deferred transaction,
which takes its write lock only at the first write. That is too late here: two requests for
the same slot would both read "free", both decide to accept, and the second would fail with
"database is locked" *after* having already made its decision. Taking the write lock up
front makes SQLite serialise the two, so the second one reads the first one's booking and
correctly rejects.

Reads deliberately do not do this — they run as ordinary deferred transactions, so
answering "what is free" never blocks anyone from booking.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

__all__ = ["Database", "create_db_engine"]

#: Execution option that upgrades a transaction from ``BEGIN`` to ``BEGIN IMMEDIATE``.
_BEGIN_OPTION = "calon_begin"

#: How long SQLite waits for a lock before giving up, in milliseconds. Requests queue
#: behind each other rather than failing, which at this scale is what an operator wants.
_BUSY_TIMEOUT_MS = 5_000


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """Build the engine, with the pragmas and the BEGIN handling calon relies on."""
    engine = create_engine(url, echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
        # Hand control of transaction boundaries to SQLAlchemy's "begin" event below;
        # otherwise pysqlite emits its own BEGIN and we cannot make it IMMEDIATE.
        dbapi_connection.isolation_level = None

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        mode = connection.get_execution_options().get(_BEGIN_OPTION, "DEFERRED")
        connection.exec_driver_sql("BEGIN IMMEDIATE" if mode == "IMMEDIATE" else "BEGIN")

    return engine


class Database:
    """Engine plus the two kinds of session calon uses.

    Both share one connection pool; they differ only in how their transaction begins.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._read = sessionmaker(bind=engine, expire_on_commit=False)
        self._write = sessionmaker(
            bind=engine.execution_options(**{_BEGIN_OPTION: "IMMEDIATE"}),
            expire_on_commit=False,
        )

    @classmethod
    def from_path(cls, db_path: Path, *, echo: bool = False) -> Database:
        """Open (and create the directory for) a database file."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(create_db_engine(f"sqlite+pysqlite:///{db_path}", echo=echo))

    @contextmanager
    def read(self) -> Iterator[Session]:
        """A read-only session. Never takes a write lock."""
        with self._read() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        """A session whose transaction is ``BEGIN IMMEDIATE``, committed on clean exit.

        This is the transaction that spans rule evaluation and insertion, so that
        deciding and booking cannot be interleaved with another request's decision.
        """
        with self._write() as session, session.begin():
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
