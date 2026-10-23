"""LOCAL DEVELOPMENT ONLY: Google Calendar OAuth connection.

Add this router only inside `if settings.enable_dev_routes` and never publish it
without application authentication, tenant ownership, persistent encrypted token
storage, and proper per-tenant account linking.

OAuth state and tokens live in app.state, hence a single local process only.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

router = APIRouter(prefix="/api/v1/auth/google", tags=["development ONLY"])
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
)
COOKIE_NAME = "zovak_google_oauth_state"
STATE_LIFETIME_SECONDS = 600


class OAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/v1/auth/google/callback"


@dataclass(frozen=True)
class PendingAuthorization:
    code_verifier: str
    expires_at: float


def _config() -> OAuthSettings:
    settings = OAuthSettings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(503, "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env")
    parsed = urlsplit(settings.google_redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.path != "/api/v1/auth/google/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "For this dev-only route, use the local HTTP callback URI")
    return settings


def _dev_only(request: Request) -> None:
    # Dev router should only be included when enable_dev_routes is true,
    # and Uvicorn must be bound to 127.0.0.1, not an external interface.
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and (
        settings.app_env != "development" or not settings.enable_dev_routes
    ):
        raise HTTPException(404, "Not found")


def _pending(request: Request) -> dict[str, PendingAuthorization]:
    if not hasattr(request.app.state, "google_oauth_pending"):
        request.app.state.google_oauth_pending = {}
    return request.app.state.google_oauth_pending


@router.get("/start", include_in_schema=False)
async def start(request: Request):
    _dev_only(request)
    cfg = _config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = _pending(request)
    now = time.time()
    # Avoid unbounded growth from abandoned authorization attempts.
    for key, value in list(pending.items()):
        if value.expires_at < now:
            del pending[key]
    pending[state] = PendingAuthorization(verifier, now + STATE_LIFETIME_SECONDS)
    params = {
        "client_id": cfg.google_client_id,
        "redirect_uri": cfg.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        COOKIE_NAME, state, max_age=STATE_LIFETIME_SECONDS,
        path="/api/v1/auth/google/callback", httponly=True, samesite="lax", secure=False,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback", include_in_schema=False)
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None):
    _dev_only(request)
    cfg = _config()
    stored_cookie = request.cookies.get(COOKIE_NAME, "")
    pending = _pending(request)
    record = pending.pop(state, None) if state else None  # state is one-use
    if (
        not state or not stored_cookie or
        not secrets.compare_digest(state, stored_cookie) or
        record is None or record.expires_at <= time.time()
    ):
        raise HTTPException(400, "Invalid or expired OAuth state. Restart at /start")
    if error:
        response = PlainTextResponse("Google authorization not granted. Try /start again.", status_code=400)
    elif not code:
        response = PlainTextResponse("Google returned no authorization code.", status_code=400)
    else:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                upstream = await client.post(TOKEN_URL, data={
                    "code": code,
                    "client_id": cfg.google_client_id,
                    "client_secret": cfg.google_client_secret,
                    "redirect_uri": cfg.google_redirect_uri,
                    "code_verifier": record.code_verifier,
                    "grant_type": "authorization_code",
                })
                upstream.raise_for_status()
                credentials = upstream.json()
        except (httpx.HTTPError, ValueError):
            # Never return upstream details or tokens: they may expose sensitive data.
            response = PlainTextResponse("Google token exchange failed. Check your OAuth settings.", status_code=502)
        else:
            received_scopes = set(credentials.get("scope", "").split())
            if not credentials.get("access_token") or not set(SCOPES).issubset(received_scopes):
                response = PlainTextResponse("Google did not grant the required Calendar permissions.", status_code=403)
            else:
                # Single calendar account in memory. Nothing written to disk or logs.
                request.app.state.google_oauth_tokens = {
                    "access_token": credentials["access_token"],
                    "refresh_token": credentials.get("refresh_token"),
                    "expires_at": time.time() + int(credentials.get("expires_in", 3600)),
                    "scopes": tuple(received_scopes),
                }
                response = PlainTextResponse(
                    "Google Calendar connected for this local development session. "
                    "You can now visit /api/v1/auth/google/status. "
                    "Tokens will be lost when the server restarts."
                )
    response.delete_cookie(COOKIE_NAME, path="/api/v1/auth/google/callback")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/status")
async def status(request: Request) -> dict[str, bool]:
    _dev_only(request)
    token = getattr(request.app.state, "google_oauth_tokens", None)
    return {
        "connected": bool(token),
        "refresh_token_available": bool(token and token.get("refresh_token")),
    }

from datetime import datetime
from zovak.providers.calendar.google import GoogleCalendar


@router.get("/test-availability")
async def test_availability(
    request: Request,
    start: datetime,
    end: datetime,
):
    _dev_only(request)

    tokens = getattr(
        request.app.state, "google_oauth_tokens", None
    )

    if not tokens or tokens["expires_at"] <= time.time():
        raise HTTPException(
            401, "Connect Google Calendar again."
        )

    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise HTTPException(400, "Invalid time range.")

    try:
        available = await GoogleCalendar(
            tokens["access_token"]
        ).is_available(start, end)
    except (httpx.HTTPError, RuntimeError):
        raise HTTPException(
            502, "Google Calendar availability check failed."
        )

    return {"available": available}
