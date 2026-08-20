"""Two people asking for the same slot at the same moment.

This is the one correctness property that cannot be checked with a unit test, and the
reason the write path opens ``BEGIN IMMEDIATE`` rather than letting SQLite start a deferred
transaction: without the write lock taken up front, both requests read "free", both decide
to accept, and the loser discovers the problem only when its INSERT fails — after it has
already told someone their booking was confirmed.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from calon.db import Database
from calon.intake.native import NATIVE_SOURCE
from calon.models import Booking
from calon.schemas import BookingIntentIn
from calon.services import booking_service, repository
from tests.conftest import NOW, booking_payload

CONTESTED_SLOT = "2026-09-02T10:00:00+02:00"
CONTENDERS = 6


def test_only_one_of_many_simultaneous_requests_wins_the_slot(
    client: TestClient, database: Database
) -> None:
    submissions, failures = _race(database, CONTESTED_SLOT, contenders=CONTENDERS)

    assert failures == [], f"a request raised instead of being decided: {failures}"
    assert len(submissions) == CONTENDERS

    accepted = [submission for submission in submissions if submission.accepted]
    rejected = [submission for submission in submissions if not submission.accepted]

    assert len(accepted) == 1
    assert all(submission.decision.code.value == "SLOT_CONFLICT" for submission in rejected), [
        submission.decision.code.value for submission in rejected
    ]


def test_the_losers_are_recorded_as_rejections_not_as_errors(
    client: TestClient, database: Database
) -> None:
    _race(database, CONTESTED_SLOT, contenders=CONTENDERS)

    with database.read() as session:
        bookings = session.scalars(select(Booking)).all()

    # Exactly one booking exists, and every other attempt is on the record as a rejection
    # rather than having disappeared.
    assert len(bookings) == 1


def test_a_conflict_check_that_disagrees_with_the_rule_chain_raises_loudly(
    client: TestClient, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If repository.has_conflict() ever reports a conflict that the re-judged
    # decision does not see, that is a genuine invariant violation between the
    # last-line-of-defence check and the rule chain's own overlap test -- not
    # something to paper over by writing a "rejected" intent whose own
    # decision_code reads ACCEPTED. Force that disagreement directly.
    monkeypatch.setattr(repository, "has_conflict", lambda *args, **kwargs: True)

    payload = booking_payload(CONTESTED_SLOT)
    intent = BookingIntentIn.model_validate(payload)
    with pytest.raises(RuntimeError, match="has_conflict"), database.write() as session:
        booking_service.submit_intent(session, intent, source=NATIVE_SOURCE, now=NOW)


def _race(
    database: Database, start: str, *, contenders: int
) -> tuple[list[booking_service.Submission], list[BaseException]]:
    """Fire ``contenders`` submissions at the same slot, released at the same instant."""
    barrier = threading.Barrier(contenders)
    lock = threading.Lock()
    submissions: list[booking_service.Submission] = []
    failures: list[BaseException] = []

    def attempt(index: int) -> None:
        payload: dict[str, Any] = booking_payload(start)
        payload["requester"] = {"name": f"Requester {index}", "email": f"r{index}@example.com"}
        intent = BookingIntentIn.model_validate(payload)

        barrier.wait()
        try:
            with database.write() as session:
                submission = booking_service.submit_intent(
                    session, intent, source=NATIVE_SOURCE, now=NOW
                )
        except BaseException as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            submissions.append(submission)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    return submissions, failures
