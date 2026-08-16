"""The advisory availability read.

The property that matters most here is agreement: what this endpoint says is free must be
what the booking endpoint will actually accept, because both run the same rule chain. The
tests are written to catch the two ways that could quietly stop being true — a slot listed
that a booking would reject, and a slot hidden that a booking would take.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import NEW_YORK, booking_payload

WEDNESDAY_09_00 = "2026-09-02T09:00:00+02:00"
WEDNESDAY_11_00 = "2026-09-02T11:00:00+02:00"
WEDNESDAY_17_00 = "2026-09-02T17:00:00+02:00"
SATURDAY_09_00 = "2026-09-05T09:00:00+02:00"
SATURDAY_17_00 = "2026-09-05T17:00:00+02:00"


def availability(client: TestClient, **params: Any) -> Any:
    return client.get("/api/v1/availability", params={"resource_slug": "default", **params})


def starts(response: Any) -> list[str]:
    return [slot["start"] for slot in response.json()["slots"]]


def test_a_working_day_lists_every_slot_on_the_grid(client: TestClient) -> None:
    response = availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00})

    assert response.status_code == 200
    body = response.json()
    assert body["duration_min"] == 30
    assert body["timezone"] == "Europe/Berlin"

    # 09:00 to 16:30 on a 15-minute grid, with every slot finishing by 17:00.
    assert starts(response)[0] == "2026-09-02T09:00:00+02:00"
    assert starts(response)[-1] == "2026-09-02T16:30:00+02:00"
    assert len(body["slots"]) == 31


def test_the_response_says_when_it_was_computed_and_nothing_that_looks_like_a_hold(
    client: TestClient,
) -> None:
    body = availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00}).json()

    assert body["evaluated_at"]
    # Availability is advisory: nothing in the response may read as a claim on a slot.
    assert set(body) == {
        "resource_slug",
        "timezone",
        "from",
        "to",
        "duration_min",
        "evaluated_at",
        "slots",
    }
    assert all(set(slot) == {"start", "end", "timezone"} for slot in body["slots"])


def test_a_booking_takes_its_slot_and_its_buffer_out_of_availability(client: TestClient) -> None:
    client.post("/api/v1/bookings", json=booking_payload("2026-09-02T10:00:00+02:00"))

    listed = starts(availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00}))

    # The booking runs 10:00 to 10:30 and carries a 15-minute trailing buffer, so every
    # candidate whose own span would touch 10:00 to 10:45 is gone.
    for hidden in ("09:30", "09:45", "10:00", "10:15", "10:30"):
        assert f"2026-09-02T{hidden}:00+02:00" not in listed
    assert "2026-09-02T09:15:00+02:00" in listed
    assert "2026-09-02T10:45:00+02:00" in listed


def test_what_is_listed_as_free_is_what_booking_will_accept(client: TestClient) -> None:
    """Book the first free slot, re-ask, book the next — repeatedly.

    Availability is a snapshot, so the list goes stale as soon as anything is booked. What
    must hold is that the answer is *true when given*: the endpoint and the rule chain
    cannot drift apart while they share one implementation.
    """
    for _ in range(4):
        listed = starts(availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00}))
        assert listed, "ran out of slots before the round finished"

        response = client.post("/api/v1/bookings", json=booking_payload(listed[0]))
        assert response.status_code == 201, f"{listed[0]} was listed as free but was rejected"

        # And it is gone from the next answer.
        assert listed[0] not in starts(
            availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00})
        )


def test_slots_must_finish_inside_the_window(client: TestClient) -> None:
    response = availability(client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_11_00})

    assert starts(response)[-1] == "2026-09-02T10:30:00+02:00"
    assert all(slot["end"] <= "2026-09-02T11:00:00+02:00" for slot in response.json()["slots"])


def test_a_longer_duration_leaves_fewer_slots(client: TestClient) -> None:
    response = availability(
        client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00, "duration_min": 60}
    )

    assert response.json()["duration_min"] == 60
    assert starts(response)[-1] == "2026-09-02T16:00:00+02:00"


def test_slots_come_back_in_the_timezone_asked_for(client: TestClient) -> None:
    response = availability(
        client, **{"from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00, "timezone": NEW_YORK}
    )

    body = response.json()
    assert body["timezone"] == NEW_YORK
    assert body["slots"][0]["start"] == "2026-09-02T03:00:00-04:00"


def test_the_notice_period_hides_slots_that_are_already_too_soon(client: TestClient) -> None:
    # "Now" is 08:00 in Berlin and the default notice is two hours.
    response = availability(
        client,
        **{"from": "2026-09-01T09:00:00+02:00", "to": "2026-09-01T17:00:00+02:00"},
    )

    assert starts(response)[0] == "2026-09-01T10:00:00+02:00"


def test_a_closed_day_has_nothing_free(client: TestClient) -> None:
    response = availability(client, **{"from": SATURDAY_09_00, "to": SATURDAY_17_00})

    assert response.status_code == 200
    assert response.json()["slots"] == []


def test_a_window_wider_than_a_month_is_refused(client: TestClient) -> None:
    response = availability(client, **{"from": WEDNESDAY_09_00, "to": "2026-10-15T09:00:00+02:00"})

    assert response.status_code == 422
    assert "31 days" in response.json()["detail"]


def test_a_backwards_window_is_refused(client: TestClient) -> None:
    response = availability(client, **{"from": WEDNESDAY_17_00, "to": WEDNESDAY_09_00})

    assert response.status_code == 422


def test_bounds_without_an_offset_are_refused(client: TestClient) -> None:
    response = availability(client, **{"from": "2026-09-02T09:00:00", "to": "2026-09-02T17:00:00"})

    assert response.status_code == 422


def test_an_unknown_resource_is_not_found(client: TestClient) -> None:
    response = client.get(
        "/api/v1/availability",
        params={"resource_slug": "nope", "from": WEDNESDAY_09_00, "to": WEDNESDAY_17_00},
    )

    assert response.status_code == 404
