"""The operator-facing web panel.

This is the human UI for the operator. It does **not** replace the API — ``POST
/api/v1/bookings`` is still open — but it is the only place where a human can see the
bookings list and download the ``.ics`` files.

Routes:
* ``GET  /login``     — the login form (the only unauthenticated page).
* ``POST /login``     — verifies the entered login and sets a session cookie.
* ``POST /logout``    — ends the session and redirects to the login form.
* ``GET  /bookings``  — the dashboard. Gated by :func:`calon.api.deps.get_authorised_operator`.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote_plus

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from calon.api.deps import AuthorisedOperator, DatabaseDep, SettingsDep
from calon.models import Booking, BookingIntent
from calon.security import SESSION_COOKIE

__all__ = ["router"]

router = APIRouter(tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.get("/login", name="login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    """The login form. If the user is already logged in, redirect to the dashboard."""
    store = request.app.state.login_store
    if store is not None and store.valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@router.post("/login", name="login_submit")
async def login_submit(request: Request, settings: SettingsDep) -> Response:
    """Verify the entered login and set a session cookie, or return an error.

    The login is the operator's key — the same value as ``CALON_LOGIN``. It is not a
    per-user account; it is the single credential that gates the operator surface.
    """
    store = request.app.state.login_store
    if store is None:
        return HTMLResponse(
            content=_error_html("CALON_LOGIN is not configured on this instance."),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    login_input = await _extract_login(request)
    if store.verify(login_input):
        token = store.create_session()
        # Build the redirect, then attach the session cookie to *that* response. Injecting
        # a separate ``response`` here would be a different object from the one FastAPI
        # actually sends, so the cookie would be silently dropped.
        response = RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=settings.base_url.startswith("https://"),
        )
        return response
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "The login you entered was not recognised. Please try again."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout", name="logout")
def logout(request: Request, response: Response) -> Response:
    """End the current session and redirect to the login form."""
    store = request.app.state.login_store
    if store is not None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            store.end_session(cookie)
    response.delete_cookie(SESSION_COOKIE)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/bookings", name="dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    _operator: AuthorisedOperator,
) -> HTMLResponse:
    """List every booking intent (accepted or rejected), newest first.

    Gated by the operator's login (the ``AuthorisedOperator`` dependency). A request
    without a valid session or API key gets a ``401``.
    """
    with database.read() as session:
        intents = _load_intents(session, limit=50)

    return templates.TemplateResponse(
        request=request,
        name="bookings.html",
        context={"intents": intents, "instance_url": settings.base_url},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _extract_login(request: Request) -> str:
    """Pull the login value from the body, supporting both JSON and form-encoded.

    ``await request.body()`` — in an async route the body is a coroutine, and a caller
    that forgot to await it gets a coroutine object, not the bytes.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        import json

        data = await request.body()
        payload = json.loads(data.decode("utf-8")) if data else {}
        return str(payload.get("login", ""))
    body = (await request.body()).decode("utf-8", errors="replace")
    for part in body.split("&"):
        if part.startswith("login="):
            return unquote_plus(part[6:])
    return ""


def _error_html(message: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        "<title>Login unavailable</title></head><body>"
        f"<h1>Login unavailable</h1><p>{message}</p>"
        '<a href="/login">Back to login</a>'
        "</body></html>"
    )


def _load_intents(session: Session, *, limit: int = 50) -> list[dict[str, object]]:
    """Load the latest booking intents with their associated bookings, if any."""
    rows = (
        session.execute(
            select(BookingIntent).order_by(BookingIntent.received_at_utc.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    bookings_by_intent: dict[str, Booking] = {}
    if rows:
        b_rows = (
            session.execute(select(Booking).where(Booking.intent_id.in_([r.id for r in rows])))
            .scalars()
            .all()
        )
        for b in b_rows:
            bookings_by_intent[b.intent_id] = b

    result: list[dict[str, object]] = []
    for intent in rows:
        booking = bookings_by_intent.get(intent.id)
        result.append(
            {
                "id": intent.id,
                "received_at": (intent.received_at_utc.isoformat() + "Z")
                if hasattr(intent, "received_at_utc") and intent.received_at_utc
                else None,
                "requester_name": intent.requester_name,
                "subject": intent.subject,
                "status": booking.status if booking else None,
                "booking_id": booking.id if booking else None,
                "start": (booking.start_utc.isoformat() + "Z")
                if booking and booking.start_utc
                else None,
                "ics_url": f"/api/v1/bookings/{booking.id}/calendar.ics" if booking else None,
            }
        )
    return result
