"""The operator-facing web panel and the public booking form.

This is the human-facing UI. It does **not** replace the API — ``POST
/api/v1/bookings`` is still the machine-facing path — but it is where a person
opens a browser and books something.

Routes:
* ``GET  /book``      — the public booking form (no login required).
* ``POST /book``      — submit the form; renders success or rejection in place.
* ``GET  /login``     — the login form (the only other unauthenticated page).
* ``POST /login``     — verifies the entered login and sets a session cookie.
* ``POST /logout``    — ends the session and redirects to the login form.
* ``GET  /bookings``  — the operator dashboard.
* ``POST /calendars/{resource_slug}/oauth-client``        — store the OAuth app credentials
  an operator entered in the dashboard (ADR 0016).
* ``POST /calendars/{resource_slug}/oauth-client/forget``  — drop them again.
* ``POST /calendars/{resource_slug}/feed``                 — subscribe to a published ICS
  calendar URL (ADR 0017); ``.../feed/forget`` unsubscribes.
* ``GET  /calendars/{resource_slug}/connect``    — start the Google connect flow (ADR 0014).
* ``GET  /calendars/google/callback``            — Google's redirect target.
* ``POST /calendars/{resource_slug}/disconnect`` — drop a resource's stored credential.
* Gated by :func:`calon.api.deps.get_authorised_operator`, except ``/book`` and ``/login``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, unquote_plus
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from calon.api.deps import AuthorisedOperator, CalendarRegistryDep, DatabaseDep, SettingsDep
from calon.api.v1 import _calendar_writeback
from calon.calendarkit import build_deeplinks, event_for, event_uid, ics_filename
from calon.calendars import CalendarProviderError
from calon.clock import utcnow
from calon.config import OperatorConfig, Settings
from calon.intake import native
from calon.models import (
    Booking,
    BookingIntent,
    CalendarCredentialRow,
    CalendarFeedRow,
    CalendarOAuthClientRow,
)
from calon.schemas import BookingIntentIn, CalendarHandoff, CalendarLinksOut, RequesterIn
from calon.security import SESSION_COOKIE, derive_login_key, verify_oauth_state
from calon.services import booking_service, calendar_connect_service

__all__ = ["router"]

logger = logging.getLogger("calon")

router = APIRouter(tags=["web"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Public booking form  (phase 4)
# ---------------------------------------------------------------------------


def _book_ctx(
    request: Request,
    *,
    form: dict[str, str] | None = None,
    errors: list[str] | None = None,
    decision: Mapping[str, object] | None = None,
    success: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the template context for book.html."""
    return {
        **_form_context(request),
        "form": form,
        "errors": errors or [],
        "decision": decision,
        "success": success,
    }


def _form_context(request: Request) -> dict[str, Any]:
    """Build the template context shared by GET and POST /book."""
    config = request.app.state.config
    policy = config.policy
    return {
        "instance_name": config.instance_name,
        "resource_name": config.resource_name,
        "timezone": config.resource.timezone,
        "window_start": policy.window_start.strftime("%H:%M"),
        "window_end": policy.window_end.strftime("%H:%M"),
        "duration_label": str(policy.default_duration_min),
    }


def _build_handoff_for_form(
    booking: Booking, intent: BookingIntent, settings: Settings
) -> CalendarHandoff:
    """Build the handoff for the success page, using the same builder as api/v1/bookings."""
    event = event_for(booking, intent, instance_host=settings.instance_host)
    links = build_deeplinks(event)
    return CalendarHandoff(
        ics_url=f"{settings.base_url}/api/v1/bookings/{booking.id}/calendar.ics",
        ics_filename=ics_filename(booking.id),
        uid=event_uid(booking.id, settings.instance_host),
        sequence=booking.ics_sequence or 0,
        links=CalendarLinksOut(**links),
    )


@router.get("/book", name="book_form", response_class=HTMLResponse)
def book_form_get(request: Request) -> HTMLResponse:
    """Render the public booking form. No login required."""
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context=_book_ctx(request, form=None, errors=[]),
    )


@router.post("/book", name="book_form_submit", response_class=HTMLResponse)
async def book_form_post(
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    calendar_registry: CalendarRegistryDep,
) -> HTMLResponse:
    """Handle a submitted booking form.

    Builds a ``BookingIntentIn``, calls ``submit_intent`` exactly as the API does
    (``source="native"``), and renders the result in place — success with handoff
    links on acceptance, or the form re-displayed with the rejection reasons and
    all user-entered values preserved. Also mirrors the API route's post-commit
    calendar write-back (ADR 0009): a resource with a connected provider gets its
    free/busy checked and the accepted booking written back exactly as it would
    through ``POST /api/v1/bookings`` — the form is not a second, divergent path.
    """
    form = await request.form()
    form_data: dict[str, str] = {}
    errors: list[str] = []

    for field_name in ("name", "email", "phone", "date", "time", "subject", "notes"):
        value = form.get(field_name, "")
        form_data[field_name] = value.strip() if isinstance(value, str) else ""

    # --- validate required fields at the form layer ---
    if not form_data["name"]:
        errors.append("Your name is required.")
    if not form_data["email"]:
        errors.append("An email address is required.")
    if not form_data["date"]:
        errors.append("A date is required.")
    if not form_data["time"]:
        errors.append("A time is required.")
    if not form_data["subject"]:
        errors.append("A subject is required.")

    if errors:
        return templates.TemplateResponse(
            request=request,
            name="book.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            context=_book_ctx(request, form=form_data, errors=errors),
        )

    # --- build an aware datetime in the resource's timezone ---
    resource_tz = request.app.state.config.resource.timezone
    zone = ZoneInfo(resource_tz)
    try:
        date_part = form_data["date"]  # "2026-09-02"
        time_part = form_data["time"]  # "10:00"
        local_dt = datetime.fromisoformat(f"{date_part}T{time_part}").replace(tzinfo=zone)
    except ValueError:
        errors.append("The date or time you entered could not be understood.")
        return templates.TemplateResponse(
            request=request,
            name="book.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            context=_book_ctx(request, form=form_data, errors=errors),
        )

    # --- build the canonical intent ---
    try:
        intent_in = BookingIntentIn(
            resource_slug=request.app.state.config.resource.slug,
            start=local_dt,
            timezone=resource_tz,
            requester=RequesterIn(
                name=form_data["name"],
                email=form_data["email"],
                phone=form_data["phone"] or None,
            ),
            subject=form_data["subject"],
            notes=form_data["notes"] or None,
        )
    except Exception as exc:
        errors.append(f"The request could not be processed: {exc}")
        return templates.TemplateResponse(
            request=request,
            name="book.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            context=_book_ctx(request, form=form_data, errors=errors),
        )

    # --- submit through the single downstream path ---
    now = utcnow()
    raw_payload = intent_in.model_dump(mode="json")

    with database.write() as session:
        submission = booking_service.submit_intent(
            session,
            intent_in,
            source=native.NATIVE_SOURCE,
            now=now,
            raw_payload=raw_payload,
            instance_host=settings.instance_host,
            calendar_registry=calendar_registry,
        )
        success_ctx = None
        intent_row = None
        if submission.booking is not None:
            booking_row = session.get(Booking, submission.booking.id)
            intent_row = session.get(BookingIntent, submission.intent_id)
            if booking_row and intent_row:
                handoff = _build_handoff_for_form(booking_row, intent_row, settings)
                success_ctx = {
                    "start": submission.booking.start_utc.astimezone(zone).strftime(
                        "%a %d %b %Y %H:%M"
                    ),
                    "end": submission.booking.end_utc.astimezone(zone).strftime("%H:%M"),
                    "timezone": resource_tz,
                    "booking_id": submission.booking.id,
                    "handoff": handoff,
                }

    # Post-commit write-back (ADR 0009), outside the transaction: the provider is a
    # network hop and a failing provider must not hold the DB lock or roll the
    # booking back. A resource with no configured provider is a silent no-op.
    if submission.booking is not None and intent_row is not None:
        _calendar_writeback.perform_write_back(
            database,
            calendar_registry,
            booking=submission.booking,
            intent=intent_row,
            now=now,
        )

    # --- render ---
    if submission.accepted:
        context = _book_ctx(request, success=success_ctx)
    else:
        # Build a lightweight decision dict for the template (mirrors DecisionOut).
        violation_msgs = [{"message": v.message} for v in submission.decision.violations]
        sug_items = submission.decision.suggestions
        suggestions = [
            {
                "start": s.start.astimezone(ZoneInfo(s.timezone)),
                "end": s.end.astimezone(ZoneInfo(s.timezone)),
                "timezone": s.timezone,
            }
            for s in sug_items
        ]
        decision = {
            "outcome": "rejected",
            "code": submission.decision.code.value,
            "reason": submission.decision.reason,
            "violations": violation_msgs,
            "suggestions": suggestions,
        }
        context = _book_ctx(request, form=form_data, decision=decision)

    return templates.TemplateResponse(request=request, name="book.html", context=context)


# ---------------------------------------------------------------------------
# Operator login
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
def logout(request: Request) -> Response:
    """End the current session and redirect to the login form."""
    store = request.app.state.login_store
    if store is not None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            store.end_session(cookie)
    # Build the redirect, then delete the cookie on *that* response — an injected
    # ``response`` parameter here would be a different object from the one FastAPI
    # actually sends, so the deletion would be silently dropped (the same mistake
    # ``login_submit`` above avoids by attaching its cookie to its own redirect).
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


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
    """The operator's home: what this instance does, its calendars, and every booking.

    Gated by the operator's login (the ``AuthorisedOperator`` dependency). A request
    without a valid session or API key gets a ``401``.

    Three panels, in the order an operator needs them: an **overview** of every function
    the instance exposes and the rules currently in force (read from
    ``config/calon.toml`` at startup), the **calendars** panel with each resource's
    connect status (ADR 0014), and the **bookings** list. The calendars panel is shown
    even when nothing is configured — it then explains how to set a calendar up, rather
    than leaving the operator with no way to discover the feature. That is a
    discoverability affordance only: a resource with no ``[calendars.<slug>]`` entry
    still has no provider, and the booking flow is untouched (CLAUDE.md §2).
    """
    config: OperatorConfig = request.app.state.config
    with database.read() as session:
        intents = _load_intents(session, limit=50)
        calendars = _load_calendar_status(session, config)

    return templates.TemplateResponse(
        request=request,
        name="bookings.html",
        context={
            "intents": intents,
            "instance_url": settings.base_url,
            "overview": _overview(config, settings, calendars),
            "calendars": calendars,
            "google_callback_url": _google_callback_url(settings),
            "calendar_connected": request.query_params.get("calendar_connected"),
            "calendar_saved": request.query_params.get("calendar_saved"),
            "calendar_subscribed": request.query_params.get("calendar_subscribed"),
            "calendar_error": request.query_params.get("calendar_error"),
        },
    )


# ---------------------------------------------------------------------------
# Calendar connect flow (ADR 0014)
# ---------------------------------------------------------------------------


def _google_callback_url(settings: Settings) -> str:
    return f"{settings.base_url}/calendars/google/callback"


@router.get("/calendars/{resource_slug}/connect", name="calendar_connect")
def calendar_connect(
    resource_slug: str,
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Redirect the operator's browser to Google's consent screen (ADR 0014)."""
    config: OperatorConfig = request.app.state.config
    try:
        with database.read() as session:
            url = calendar_connect_service.start_connect(
                session,
                config,
                resource_slug=resource_slug,
                redirect_uri=_google_callback_url(settings),
                signing_key=derive_login_key(settings.login),
            )
    except calendar_connect_service.CalendarNotConfiguredError as exc:
        return RedirectResponse(
            f"/bookings?calendar_error={quote_plus(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/calendars/google/callback", name="calendar_connect_callback")
def calendar_connect_callback(
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    calendar_registry: CalendarRegistryDep,
    _operator: AuthorisedOperator,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """Handle Google's redirect back: exchange the code, persist, go live (ADR 0014).

    Gated by the operator's login exactly like every other route here — Google redirects
    the operator's own browser back to this URL, which still carries their session
    cookie, so this is not an unauthenticated webhook.
    """
    if error:
        return RedirectResponse(
            f"/bookings?calendar_error={quote_plus(error)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not code or not state:
        return RedirectResponse(
            "/bookings?calendar_error=the+connect+request+was+missing+code+or+state",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    resource_slug = verify_oauth_state(derive_login_key(settings.login), state)
    if resource_slug is None:
        return RedirectResponse(
            "/bookings?calendar_error=the+connect+link+expired%3B+please+try+again",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    config: OperatorConfig = request.app.state.config
    try:
        with database.write() as session:
            calendar_connect_service.complete_connect(
                session,
                calendar_registry,
                config,
                resource_slug=resource_slug,
                code=code,
                redirect_uri=_google_callback_url(settings),
                now=utcnow(),
            )
    except (calendar_connect_service.CalendarNotConfiguredError, CalendarProviderError) as exc:
        return RedirectResponse(
            f"/bookings?calendar_error={quote_plus(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(
        f"/bookings?calendar_connected={quote_plus(resource_slug)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/calendars/{resource_slug}/disconnect", name="calendar_disconnect")
def calendar_disconnect(
    resource_slug: str,
    database: DatabaseDep,
    calendar_registry: CalendarRegistryDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Remove a resource's stored credential and degrade it to calon-only (ADR 0014)."""
    with database.write() as session:
        calendar_connect_service.disconnect(session, calendar_registry, resource_slug=resource_slug)
    return RedirectResponse("/bookings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/calendars/{resource_slug}/oauth-client", name="calendar_oauth_client_save")
async def calendar_oauth_client_save(
    resource_slug: str,
    request: Request,
    database: DatabaseDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Store the Google OAuth app credentials the operator typed into the dashboard.

    ADR 0016. This is the step that used to require editing ``config/calon.toml`` on the
    host and restarting; storing the pair here makes the resource connectable immediately,
    and the consent round trip that follows is the unchanged ADR 0014 flow.

    A resource that already has a ``[calendars.<slug>]`` entry is refused rather than
    written: the file wins at resolution time anyway, so accepting the form would store a
    credential that never takes effect.
    """
    config: OperatorConfig = request.app.state.config
    if resource_slug in config.calendars:
        return _dashboard_error(
            f"{resource_slug} is configured in config/calon.toml; edit it there instead"
        )

    form = await request.form()

    def _field(name: str) -> str:
        value = form.get(name, "")
        return value.strip() if isinstance(value, str) else ""

    try:
        with database.write() as session:
            calendar_connect_service.save_oauth_client(
                session,
                resource_slug=resource_slug,
                client_id=_field("client_id"),
                client_secret=_field("client_secret"),
                calendar_id=_field("calendar_id"),
                now=utcnow(),
            )
    except calendar_connect_service.CalendarNotConfiguredError as exc:
        return _dashboard_error(str(exc))

    return RedirectResponse(
        f"/bookings?calendar_saved={quote_plus(resource_slug)}#calendars",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/calendars/{resource_slug}/oauth-client/forget", name="calendar_oauth_client_forget")
def calendar_oauth_client_forget(
    resource_slug: str,
    database: DatabaseDep,
    calendar_registry: CalendarRegistryDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Remove dashboard-entered OAuth credentials, and any connection built on them."""
    with database.write() as session:
        calendar_connect_service.forget_oauth_client(
            session, calendar_registry, resource_slug=resource_slug
        )
    return RedirectResponse("/bookings#calendars", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/calendars/{resource_slug}/feed", name="calendar_feed_save")
async def calendar_feed_save(
    resource_slug: str,
    request: Request,
    database: DatabaseDep,
    calendar_registry: CalendarRegistryDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Subscribe a resource to a published ICS calendar URL (ADR 0017).

    The no-OAuth path: the operator publishes their calendar from its own settings and
    pastes the secret address here. It goes live immediately — a feed needs no consent
    round trip, because the URL is the credential — and is read-only, so bookings still
    reach the calendar through the ``.ics`` handoff rather than a write-back.
    """
    config: OperatorConfig = request.app.state.config
    if resource_slug in config.calendars:
        return _dashboard_error(
            f"{resource_slug} is configured in config/calon.toml; edit it there instead"
        )

    form = await request.form()
    url = form.get("feed_url", "")
    try:
        with database.write() as session:
            calendar_connect_service.save_feed(
                session,
                calendar_registry,
                resource_slug=resource_slug,
                url=url if isinstance(url, str) else "",
                timezone=config.resource.timezone,
                now=utcnow(),
            )
    except calendar_connect_service.CalendarNotConfiguredError as exc:
        return _dashboard_error(str(exc))
    except ValueError as exc:  # a URL the provider itself refuses to accept
        return _dashboard_error(str(exc))

    return RedirectResponse(
        f"/bookings?calendar_subscribed={quote_plus(resource_slug)}#calendars",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/calendars/{resource_slug}/feed/forget", name="calendar_feed_forget")
def calendar_feed_forget(
    resource_slug: str,
    database: DatabaseDep,
    calendar_registry: CalendarRegistryDep,
    _operator: AuthorisedOperator,
) -> Response:
    """Unsubscribe a resource from its calendar feed."""
    with database.write() as session:
        calendar_connect_service.forget_feed(
            session, calendar_registry, resource_slug=resource_slug
        )
    return RedirectResponse("/bookings#calendars", status_code=status.HTTP_303_SEE_OTHER)


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


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _overview(
    config: OperatorConfig, settings: Settings, calendars: list[dict[str, object]]
) -> dict[str, object]:
    """What this instance does, and under which rules — the operator's orientation panel.

    Everything here is read straight from the already-loaded config; the panel is a view
    of ``config/calon.toml`` as calon actually parsed it, which is what makes it useful
    when a rule does not behave the way the file was meant to read.
    """
    policy = config.policy
    subscribed = sum(1 for row in calendars if row["read_only"])
    connected = sum(1 for row in calendars if row["connected"] and not row["read_only"])
    configured = sum(1 for row in calendars if row["configured"])
    return {
        "instance_name": config.instance_name,
        "resource_name": config.resource_name,
        "resource_slug": config.resource.slug,
        "timezone": config.resource.timezone,
        "weekdays": ", ".join(_WEEKDAY_NAMES[day] for day in sorted(policy.allowed_weekdays)),
        "window": f"{policy.window_start:%H:%M}-{policy.window_end:%H:%M}",
        "default_duration_min": policy.default_duration_min,
        "slot_granularity_min": policy.slot_granularity_min,
        "min_notice_min": policy.min_notice_min,
        "max_advance_days": policy.max_advance_days,
        "buffer_before_min": policy.buffer_before_min,
        "buffer_after_min": policy.buffer_after_min,
        "max_bookings_per_day": policy.max_bookings_per_day,
        "blackout_count": len(config.blackouts),
        "event_title": config.calendar.event_title,
        "sources": [
            {
                "slug": slug,
                "enabled": source.enabled,
                "resource_slug": source.resource_slug,
                "endpoint": f"/api/v1/{slug}",
            }
            for slug, source in sorted(config.sources.items())
        ],
        "calendars_configured": configured,
        "calendars_connected": connected,
        "calendars_subscribed": subscribed,
        "docs_url": "/docs" if settings.docs_enabled else None,
        "api_key_configured": bool(settings.api_key),
    }


def _dashboard_error(message: str) -> RedirectResponse:
    """Back to the dashboard with the message in the error banner."""
    return RedirectResponse(
        f"/bookings?calendar_error={quote_plus(message)}#calendars",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _load_calendar_status(session: Session, config: OperatorConfig) -> list[dict[str, object]]:
    """One row per resource, whether or not it has a ``[calendars.<slug>]`` entry.

    A resource with no entry gets a ``configured = False`` row so the dashboard can show
    the operator how to set one up, instead of the feature being invisible until the
    config file already has it. That row carries no provider and no action — it is a
    signpost, not a second way to configure calon (``config/calon.toml`` stays
    authoritative, ADR 0008).

    ``connectable`` distinguishes Google (has a connect button) from Microsoft (out-of-band
    only, per ADR 0014's scope) so the template can render the right action without
    guessing from the provider name itself.
    """
    clients = {row.resource_slug: row for row in session.query(CalendarOAuthClientRow).all()}
    feeds = {row.resource_slug: row for row in session.query(CalendarFeedRow).all()}
    rows: list[dict[str, object]] = []
    for slug in sorted({config.resource.slug} | set(config.calendars) | set(clients) | set(feeds)):
        cfg = calendar_connect_service.resolve_calendar_config(session, config, slug)
        if cfg is None:
            rows.append(
                {
                    "resource_slug": slug,
                    "provider": None,
                    "configured": False,
                    "source": None,
                    "calendar_id": None,
                    "connectable": False,
                    "connected": False,
                    "connected_at": None,
                    "read_only": False,
                }
            )
            continue
        credential = session.get(CalendarCredentialRow, slug)
        # A feed carries no OAuth grant: subscribing *is* being connected, which is why
        # it reports connected without a credential row (ADR 0017).
        is_feed = cfg.provider == "ics"
        rows.append(
            {
                "resource_slug": slug,
                "provider": cfg.provider,
                "configured": True,
                # Which source the credentials came from, so the row can offer the right
                # action: only a dashboard-entered client can be edited or forgotten from
                # the dashboard (ADR 0016 — the TOML is never written by calon).
                "source": (
                    "config" if slug in config.calendars else ("feed" if is_feed else "dashboard")
                ),
                "calendar_id": cfg.calendar_id,
                "connectable": cfg.provider == "google",
                "connected": is_feed or credential is not None,
                "connected_at": (
                    credential.connected_at_utc.isoformat() if credential is not None else None
                ),
                "read_only": is_feed,
            }
        )
    return rows


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
                # ``received_at_utc`` already comes back tz-aware (UtcDateTime
                # reattaches UTC on read), so ``isoformat()`` already carries the
                # offset; appending "Z" on top produced a malformed
                # "+00:00Z" suffix.
                "received_at": (
                    intent.received_at_utc.isoformat() if intent.received_at_utc else None
                ),
                "requester_name": intent.requester_name,
                "subject": intent.subject,
                "status": booking.status if booking else None,
                "booking_id": booking.id if booking else None,
                "start": booking.start_utc.isoformat() if booking and booking.start_utc else None,
                "ics_url": f"/api/v1/bookings/{booking.id}/calendar.ics" if booking else None,
            }
        )
    return result
