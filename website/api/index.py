import os
import re
import json
import time
import uuid
import asyncio
import math
import secrets
import logging
import urllib.parse
import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env.local")
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from api import session as rotating_session
import httpx

from api.db import get_pool, query, fetchrow, fetchval, execute, fetchrow_cached, _update_cache, get_guild_data, invalidate_guild_data

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/callback")
OAUTH_SCOPES = "identify guilds"
MANAGE_SERVER = 0x20

# ── Nerimity OAuth (nerimity.com) ──
# Authorize: GET https://nerimity.com/authorize?clientId=..&redirectUri=..&scopes=..
# Token:    POST https://nerimity.com/api/oauth2/token?grantType=..&clientId=..&clientSecret=..
NERIMITY_API = "https://nerimity.com/api"
NERIMITY_CLIENT_ID = os.environ.get("NERIMITY_CLIENT_ID", "")
NERIMITY_CLIENT_SECRET = os.environ.get("NERIMITY_CLIENT_SECRET", "")
NERIMITY_REDIRECT_URI = os.environ.get("NERIMITY_REDIRECT_URI", "")
NERIMITY_SCOPES = "USER_INFO USER_SERVERS"

ROOT = Path(__file__).parent.parent

_missing = []
if not CLIENT_ID:
    _missing.append("CLIENT_ID")
if not CLIENT_SECRET:
    _missing.append("CLIENT_SECRET")
if not REDIRECT_URI or REDIRECT_URI.startswith("http://localhost"):
    _missing.append("REDIRECT_URI (should be production URL)")
if not os.environ.get("DATABASE_URL"):
    _missing.append("DATABASE_URL")
if not os.environ.get("SECRET_KEY"):
    _missing.append("SECRET_KEY")
if _missing:
    logger.warning("Missing/invalid environment variables: %s", ", ".join(_missing))


def _cfg():
    """Return only safe, template-needed env vars."""
    return {"CLIENT_ID": os.environ.get("CLIENT_ID", "")}


# ── Direct bot HTTP bridge ──
# The dashboard used to route every moderation action through the DB queue,
# which the bot polls every ~3s -> 5s+ latency for bans/mutes/etc. When these
# are configured the API calls the bot's aiohttp bridge directly (token is
# server-side only, never exposed to the browser) and falls back to the queue.
BOT_SERVER_URL = os.environ.get("BOT_SERVER_URL", "")
BOT_HTTP_TOKEN = os.environ.get("BOT_HTTP_TOKEN", "")
DIRECT_ACTIONS = ("mute", "unmute", "kick", "ban", "purge", "emergency_lock", "emergency_unlock", "add_role", "remove_role", "nickname", "verify_panel", "verify_panel_remove", "verify_user", "panel_send")

# ── Remote semantic search (HidenCloud BGE microservice) ──
# When enabled, the dashboard search asks the bot server's /semantic-search
# endpoint and combines those rankings with the local keyword score. The auth
# token reuses BOT_HTTP_TOKEN (same secret the bridge already uses). Falls back
# to keyword-only if the microservice is unreachable or disabled.
SEMANTIC_SEARCH_ENABLED = os.environ.get("SEMANTIC_SEARCH_ENABLED", "false").lower() in (
    "1", "true", "yes", "on",
)
SEMANTIC_API_URL = (os.environ.get("SEMANTIC_API_URL", "") or BOT_SERVER_URL).rstrip("/")
SEMANTIC_API_KEY = os.environ.get("SEMANTIC_API_KEY", BOT_HTTP_TOKEN)
SEMANTIC_WEIGHT = float(os.environ.get("SEMANTIC_WEIGHT", "0.6"))
KEYWORD_WEIGHT = float(os.environ.get("KEYWORD_WEIGHT", "0.4"))
SEMANTIC_MIN_SCORE = float(os.environ.get("SEMANTIC_MIN_SCORE", "0.20"))


def _parse_guild_data(row):
    """Safely extract guild data dict from a DB row."""
    if not row:
        return None
    d = row["data"]
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except (json.JSONDecodeError, TypeError):
            return None
    return d if isinstance(d, dict) else None


# ---------------------------------------------------------------------------
#  App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await get_pool()
        await _ensure_incidents()
        await _ensure_user_columns()
    except Exception as e:
        logger.error("Failed to initialize database pool: %s", e)
    # Warm the sidebar-search embedding cache in the background (no-op if
    # OPENAI_API_KEY is unset - search then runs in keyword-only mode).
    try:
        asyncio.get_running_loop().create_task(_catalog_vectors())
    except Exception:
        pass
    yield

app = FastAPI(title="Prowl", version="1.0.0", lifespan=lifespan)

# CORS for api.prowlbot.xyz (cross-origin API calls from the main site)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://prowlbot.xyz",
        "https://www.prowlbot.xyz",
        "https://api.prowlbot.xyz",
        "https://status.prowlbot.xyz",
        "http://localhost:5000",
        "http://localhost:8000",
        "http://127.0.0.1:5000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Turnstile challenge gate ──
class TurnstileMiddleware:
    """Gate the entire site behind a Cloudflare Turnstile challenge.
    Visitors without a valid turnstile flag in their session are redirected to /challenge."""

    WHITELIST_PREFIXES = ("/static", "/challenge", "/api/", "/favicon")
    WHITELIST_PATHS = {"/", "/health", "/api/v1/health", "/api/v1/ping"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if path in self.WHITELIST_PATHS or any(path.startswith(p) for p in self.WHITELIST_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Check session for turnstile verification
        session = scope.get("session")
        if session and rotating_session.is_turnstile_verified(session):
            await self.app(scope, receive, send)
            return

        from starlette.responses import RedirectResponse
        query = scope.get("query_string", b"").decode("latin-1")
        next_param = f"?next={urllib.parse.quote(path + ('?' + query if query else ''))}" if path != "/" else ""
        resp = RedirectResponse(f"/challenge{next_param}", status_code=302)
        await resp(scope, receive, send)


app.add_middleware(TurnstileMiddleware)


class RotatingSessionMiddleware:
    """
    ASGI middleware that replaces Starlette's SessionMiddleware with
    server-side sessions and 30-second key rotation with 5-minute grace.
    """

    def __init__(self, app):
        self.app = app
        self._initial_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
        # Prime the key ring
        rotating_session._get_current_key()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.datastructures import MutableHeaders
        from starlette.requests import HTTPConnection

        conn = HTTPConnection(scope)
        cookie_val = conn.cookies.get("session")
        # Session data lives in the signed cookie itself - serverless-safe.
        session_data = rotating_session.unsign_session_data(cookie_val) if cookie_val else None
        if session_data is None:
            session_data = {}
        session_snapshot = json.dumps(session_data, sort_keys=True, default=str)

        scope["session"] = session_data

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Re-sign the cookie whenever the data changed, to keep it fresh.
                cur = json.dumps(scope.get("session") or {}, sort_keys=True, default=str)
                if cur != session_snapshot or not cookie_val:
                    signed = rotating_session.sign_session_data(scope.get("session") or {})
                    host = (conn.headers.get("host") or "").lower()
                    if "prowlbot.xyz" in host:
                        headers["Set-Cookie"] = (
                            f"session={signed}; Path=/; HttpOnly; SameSite=None; Secure; Domain=.prowlbot.xyz; Max-Age={86400}"
                        )
                    else:
                        headers["Set-Cookie"] = (
                            f"session={signed}; Path=/; HttpOnly; SameSite=lax; Max-Age={86400}"
                        )
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(RotatingSessionMiddleware)

# ── Security headers ──
# 'unsafe-inline'/'unsafe-eval' are required by the CDN-based JS, tailwind play
# CDN, and inline dashboard scripts. The CSP still restricts every other source
# to known domains (defense in depth against injected external resources).
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdn.tailwindcss.com https://unpkg.com https://cdnjs.cloudflare.com "
    "https://cdn.jsdelivr.net https://www.google.com https://www.gstatic.com "
    "https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https://cdn.discordapp.com https://img.itch.zone; "
    "connect-src 'self' https://api.prowlbot.xyz https://discord.com "
    "https://www.google.com https://www.gstatic.com https://challenges.cloudflare.com "
    "https://unpkg.com; "
    "frame-src https://www.google.com https://www.gstatic.com https://challenges.cloudflare.com; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = ""
        for k, v in scope.get("headers") or []:
            if k == b"host":
                host = v.decode("latin-1").lower()
                break

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["X-Frame-Options"] = "DENY"
                # Only on production hosts - HSTS on localhost would break dev
                if "prowlbot.xyz" in host:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeadersMiddleware)

# ── Request counting (status page graph) ──
_REQUEST_SKIP = ("/static/", "/favicon.ico", "/favicon.png")
_REQUEST_SKIP_EXACT = ("/api/v1/status/summary", "/api/v1/health", "/api/v1/ping")


class RequestCountMiddleware:
    """Best-effort hourly request counter for the public status page graph."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        host = ""
        for k, v in scope.get("headers") or []:
            if k == b"host":
                host = v.decode("latin-1").lower()
                break
        skip = path.startswith(_REQUEST_SKIP) or path in _REQUEST_SKIP_EXACT or "prowlbot.xyz" not in host
        await self.app(scope, receive, send)
        if skip:
            return
        # Awaited (not fire-and-forget): serverless functions die right after the
        # response, so the write must complete inside the request lifecycle.
        await self._count()

    async def _count(self):
        bucket = int(time.time() // 3600) * 3600
        result = await execute(
            "INSERT INTO request_stats (bucket_ts, count) VALUES (?, 1) "
            "ON CONFLICT (bucket_ts) DO UPDATE SET count = request_stats.count + 1",
            bucket,
        )
        if result is None:
            await execute(_REQUEST_TABLE_SQL)
            await execute(
                "INSERT INTO request_stats (bucket_ts, count) VALUES (?, 1) "
                "ON CONFLICT (bucket_ts) DO UPDATE SET count = request_stats.count + 1",
                bucket,
            )


_REQUEST_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS request_stats (
    bucket_ts REAL PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
"""


app.add_middleware(RequestCountMiddleware)

# ── Subdomain routing: enforce api.prowlbot.xyz = API only, prowlbot.xyz = pages ──
class SubdomainRouteMiddleware:
    """
    Enforce the subdomain split:
      - api.prowlbot.xyz  -> API-only (only /api/*, plus a small root descriptor)
      - status.prowlbot.xyz -> status page at any non-static path
      - prowlbot.xyz / www.prowlbot.xyz -> main website; /api/* redirects to api subdomain
    """

    def __init__(self, app):
        self.app = app

    def _host(self, scope):
        headers = dict(scope.get("headers") or [])
        # Prefer the real host; Vercel may forward via x-forwarded-host
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if not host and b"x-forwarded-host" in headers:
            host = headers.get(b"x-forwarded-host", b"").decode("latin-1").lower()
        if host and ":" in host:
            host = host.split(":")[0]
        return host

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = self._host(scope)
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("latin-1")

        is_api_host = host == "api.prowlbot.xyz"
        is_status_host = host == "status.prowlbot.xyz"
        is_main_host = host in ("prowlbot.xyz", "www.prowlbot.xyz")

        # api.prowlbot.xyz: API-only gateway
        if is_api_host:
            if path in ("/", ""):
                return await self._send_json(
                    scope, receive, send, 200,
                    {
                        "service": "prowl-api",
                        "version": "1.0.0",
                        "endpoints": {
                            "health": "https://api.prowlbot.xyz/api/v1/health",
                            "ping": "https://api.prowlbot.xyz/api/v1/ping",
                        },
                    },
                )
            # Browsers request the favicon when API endpoints 4xx - let it through
            if path in ("/favicon.ico", "/favicon.png"):
                await self.app(scope, receive, send)
                return
            if not path.startswith("/api/"):
                return await self._send_json(
                    scope, receive, send, 404,
                    {"error": "Not Found", "message": "Only /api/* routes are available on this subdomain."},
                )
            await self.app(scope, receive, send)
            return

        # status.prowlbot.xyz: serve the status page at the root
        if is_status_host:
            # API + static assets are shared with the main app; allow them through
            if path.startswith("/api/") or path.startswith("/static/") or path in ("/favicon.ico", "/favicon.png"):
                await self.app(scope, receive, send)
                return
            # Let the turnstile challenge through, otherwise TurnstileMiddleware
            # redirects to /challenge which we'd rewrite back to /status -> loop
            if path == "/challenge":
                await self.app(scope, receive, send)
                return
            # Rewrite everything else to the /status page handler
            scope["path"] = "/status"
            scope["raw_path"] = b"/status"
            await self.app(scope, receive, send)
            return

        # prowlbot.xyz: main website; API routes should live on the API subdomain
        if is_main_host and path.startswith("/api/") and not path.startswith("/api/v1/turnstile"):
            from starlette.responses import RedirectResponse
            new_path = f"https://api.prowlbot.xyz{path}"
            if query:
                new_path += "?" + query
            resp = RedirectResponse(new_path, status_code=307)
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)

    async def _send_json(self, scope, receive, send, status, obj):
        from starlette.responses import JSONResponse
        resp = JSONResponse(obj, status_code=status)
        await resp(scope, receive, send)


app.add_middleware(SubdomainRouteMiddleware)

templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ---------------------------------------------------------------------------
#  Error handlers
# ---------------------------------------------------------------------------

ERROR_PAGES = {
    400: ("Bad Request", "The request could not be understood."),
    403: ("Forbidden", "You don't have permission to access this."),
    404: ("Page Not Found", "The page you're looking for doesn't exist."),
    405: ("Method Not Allowed", "This method is not allowed here."),
    429: ("Too Many Requests", "Slow down there, partner."),
    500: ("Internal Server Error", "Something went wrong on our end."),
    502: ("Bad Gateway", "The upstream server is not responding."),
    503: ("Service Unavailable", "The service is temporarily unavailable."),
    504: ("Gateway Timeout", "The upstream server took too long to respond."),
}


@app.exception_handler(404)
async def not_found(request, exc):
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 404, "title": ERROR_PAGES[404][0], "message": ERROR_PAGES[404][1]},
        status_code=404,
    )


@app.exception_handler(500)
async def server_error(request, exc):
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 500, "title": ERROR_PAGES[500][0], "message": ERROR_PAGES[500][1]},
        status_code=500,
    )


@app.exception_handler(403)
async def forbidden(request, exc):
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 403, "title": ERROR_PAGES[403][0], "message": ERROR_PAGES[403][1]},
        status_code=403,
    )


@app.exception_handler(429)
async def too_many(request, exc):
    detail = getattr(exc, "detail", None)
    retry = 30
    headers = getattr(exc, "headers", None)
    if headers:
        ra = headers.get("Retry-After")
        if ra:
            try:
                retry = int(ra)
            except (ValueError, TypeError):
                pass
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 429, "title": "Rate Limited", "message": detail or ERROR_PAGES[429][1], "retry_in": retry},
        status_code=429,
    )


# ---------------------------------------------------------------------------
#  Favicon
# ---------------------------------------------------------------------------


@app.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(ROOT / "static" / "favicon.ico")


@app.get("/favicon.png")
async def favicon_png():
    return FileResponse(ROOT / "static" / "favicon.png")


@app.get("/robots.txt")
async def robots_txt():
    return FileResponse(ROOT / "static" / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    return FileResponse(ROOT / "static" / "sitemap.xml", media_type="application/xml")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

async def discord_get(path: str, token: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DISCORD_API}{path}", headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return r.json()
    return None


def get_user(request: Request):
    return request.session.get("user")


def get_token(request: Request):
    return request.session.get("token")


def get_selected_guild(request: Request):
    return request.session.get("selected_guild")


def _relative_time(ts: float) -> str:
    diff = time.time() - ts
    if diff < 60: return f"{int(diff)}s ago"
    if diff < 3600: return f"{int(diff // 60)}m ago"
    if diff < 86400: return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


async def get_bot_guild_ids():
    rows = await query("SELECT guild_id FROM guild_data")
    return {row["guild_id"] for row in rows}


CACHE_TTL = 120  # seconds


async def get_user_guilds_filtered(request: Request):
    token = get_token(request)
    if not token:
        return []

    now = time.time()
    cached = request.session.get("guild_cache")
    if cached and (now - cached.get("ts", 0)) < CACHE_TTL:
        return cached["guilds"]

    user_guilds = await discord_get("/users/@me/guilds", token)
    if not user_guilds:
        return []
    bot_ids = await get_bot_guild_ids()
    eligible = []
    for g in user_guilds:
        perms = int(g.get("permissions", 0))
        gid = str(g["id"])
        if (perms & MANAGE_SERVER) and gid in bot_ids:
            eligible.append(g)

    request.session["guild_cache"] = {"ts": now, "guilds": eligible}
    return eligible


# ---------------------------------------------------------------------------
#  Nerimity helpers
# ---------------------------------------------------------------------------

def _nerimity_redirect_uri(request: Request) -> str:
    """Nerimity caps the redirect URI at 20 characters, so we use the bare
    short relay domain `https://prowl.xo.je` (18 chars). That relay simply 302s
    the browser to the real callback on prowlbot.xyz, which does the token
    exchange. Nerimity appends `?code=...&state=...` itself at redirect time -
    that appended part is NOT counted against the 20-char limit. Override with
    NERIMITY_REDIRECT_URI if you registered a different <=20-char URI."""
    if NERIMITY_REDIRECT_URI:
        return NERIMITY_REDIRECT_URI
    return "https://prowl.xo.je"


async def nerimity_get(path: str, token: str):
    """GET a Nerimity OAuth endpoint. Returns parsed JSON or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{NERIMITY_API}{path}", headers={"Authorization": token})
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.error("Nerimity GET %s failed: %s", path, e)
    return None


async def _nerimity_token_request(request: Request, params: dict):
    """Exchange a code / refresh a token with Nerimity. Stores the new access +
    refresh tokens in the session. Returns the access token or None."""
    if not NERIMITY_CLIENT_ID or not NERIMITY_CLIENT_SECRET:
        return None
    query = {"clientId": NERIMITY_CLIENT_ID, "clientSecret": NERIMITY_CLIENT_SECRET, **params}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{NERIMITY_API}/oauth2/token", params=query)
            if r.status_code != 200:
                logger.error("Nerimity token exchange failed: %s %s", r.status_code, r.text[:200])
                return None
            data = r.json()
    except Exception as e:
        logger.error("Nerimity token request error: %s", e)
        return None
    access = data.get("accessToken")
    if not access:
        return None
    scope_session = request.scope.get("session")
    if scope_session is not None:
        scope_session["nerimity_token"] = access
        if data.get("refreshToken"):
            scope_session["nerimity_refresh"] = data["refreshToken"]
    return access


async def get_nerimity_servers(request: Request):
    """List the linked Nerimity account's servers via the OAuth API.
    Refreshes the access token once on failure. Returns [] when not linked."""
    token = request.session.get("nerimity_token")
    if not token:
        return []
    data = await nerimity_get("/oauth2/users/current/servers", token)
    if not isinstance(data, list):
        refresh = request.session.get("nerimity_refresh")
        if not refresh:
            return []
        token = await _nerimity_token_request(
            request, {"grantType": "refresh_token", "refreshToken": refresh}
        )
        if not token:
            return []
        data = await nerimity_get("/oauth2/users/current/servers", token)
    if not isinstance(data, list):
        return []
    return [
        {
            "id": str(s.get("id", "")),
            "name": s.get("name", ""),
            "icon": "",
            "platform": "nerimity",
            "disabled": True,  # Prowl can't manage Nerimity servers yet
        }
        for s in data
        if isinstance(s, dict) and s.get("id")
    ]


# ---------------------------------------------------------------------------
#  Routes - Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Nerimity redirects back to the bare site root (its 20-char redirect-URI
    # cap forbids a /callback/nerimity sub-page). Detect that callback via the
    # `code` query param plus the Nerimity `Referer` header (or a session flag
    # set when we started the flow, in case the browser strips the Referer),
    # then finish login.
    code = request.query_params.get("code")
    referer = request.headers.get("referer", "")
    if code and ("nerimity" in referer.lower() or request.session.get("nerimity_oauth_state")):
        state = request.query_params.get("state")
        return await _finish_nerimity(request, code, state)
    user = get_user(request)
    return templates.TemplateResponse(request, "index.html", {
        "config": _cfg(),
        "user": user,
    })


@app.get("/challenge", response_class=HTMLResponse)
async def challenge_page(request: Request):
    """Cloudflare Turnstile challenge page - gate before the rest of the site."""
    return templates.TemplateResponse(request, "challenge.html", {
        "site_key": os.environ.get("TURNSTILE_SITE_KEY", ""),
        "config": _cfg(),
    })


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if get_user(request):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse(request, "login.html", {"config": _cfg()})


@app.get("/invite", response_class=HTMLResponse)
async def invite(request: Request):
    if request.query_params.get("mode") == "instant":
        url = (
            f"https://discord.com/oauth2/authorize"
            f"?client_id={CLIENT_ID}"
            f"&scope=bot%20applications.commands"
            f"&permissions=8"
        )
        return RedirectResponse(url)
    return templates.TemplateResponse(request, "invite.html", {"config": _cfg()})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "tos.html", {"config": _cfg()})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"config": _cfg()})


@app.get("/changelog", response_class=HTMLResponse)
async def changelog(request: Request):
    return templates.TemplateResponse(request, "changelog.html", {"config": _cfg()})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return templates.TemplateResponse(request, "status.html", {"config": _cfg()})


@app.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "reminders.html", {
        "config": _cfg(),
        "user": user,
    })


_FEEDBACK_META = {
    "suggest": (
        "Suggest an idea", "Got a feature you wish Prowl had? Tell us - we read every suggestion.",
        "Your idea", "Describe your idea and why it would be useful…",
    ),
    "report": (
        "Report a bug", "Something not working as expected? Let us know what happened.",
        "What went wrong", "Describe the bug and the steps to reproduce it…",
    ),
    "feedback": (
        "Give feedback", "We'd love to hear your thoughts on Prowl.",
        "Your feedback", "Share what you love or what we could improve…",
    ),
}


@app.get("/suggest", response_class=HTMLResponse)
@app.get("/report-bug", response_class=HTMLResponse)
@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request):
    kind = request.url.path.split("/")[-1]
    if kind == "report-bug":
        kind = "report"
    title, intro, label, placeholder = _FEEDBACK_META.get(
        kind, _FEEDBACK_META["feedback"]
    )
    return templates.TemplateResponse(request, "feedback.html", {
        "config": _cfg(),
        "kind": kind,
        "title": title,
        "desc": title + " - Prowl.",
        "intro": intro,
        "label": label,
        "placeholder": placeholder,
    })


@app.post("/api/v1/feedback")
async def submit_feedback(request: Request):
    """Accept a suggestion / bug report / feedback submission.

    Forwards to a Discord webhook when FEEDBACK_WEBHOOK_URL is configured,
    otherwise just logs it. Never raises - always returns JSON.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
    kind = (data.get("kind") or "").strip().lower()
    if kind not in ("suggest", "report", "feedback"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    message = (data.get("message") or "").strip()
    if len(message) < 3:
        return JSONResponse({"ok": False, "error": "message too short"}, status_code=400)
    if len(message) > 4000:
        message = message[:4000]
    name = (data.get("name") or "").strip()[:120]
    email = (data.get("email") or "").strip()[:160]

    wh = os.environ.get("FEEDBACK_WEBHOOK_URL")
    if wh:
        try:
            label = {"suggest": "Idea", "report": "Bug", "feedback": "Feedback"}[kind]
            content = (f"**New {label}**" + (f" from {name}" if name else "") +
                       (f" ({email})" if email else "") + f"\n\n{message}")[:2000]
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(wh, json={"content": content})
        except Exception as e:
            logger.warning("Feedback webhook failed: %s", e)
    logger.info("Feedback received (%s) from %s", kind, name or "anonymous")
    return {"ok": True}


@app.get("/captcha/{provider}", response_class=HTMLResponse)
async def captcha_page(request: Request, provider: str):
    """Hosted captcha solve page: renders the widget and auto-verifies on solve."""
    if provider not in ("recaptcha",):
        return templates.TemplateResponse(request, "error.html",
            {"code": 404, "title": "Not Found", "message": "Unknown captcha provider."}, status_code=404)
    code = request.query_params.get("code", "")
    info = await _validate_captcha_code(code, provider)
    if not info:
        return templates.TemplateResponse(request, "error.html",
            {"code": 403, "title": "Link Expired", "message": "This verification link is invalid or has already been used. Click Verify again in Discord to get a fresh link."},
            status_code=403)
    site_key = os.environ.get(f"{provider.upper()}_SITE_KEY", "")
    return templates.TemplateResponse(request, "captcha.html", {
        "provider": provider,
        "site_key": site_key,
        "code": code,
        "recaptcha_version": os.environ.get("RECAPTCHA_VERSION", "v2"),
        "config": _cfg(),
    })


@app.get("/auth/discord")
async def auth_discord():
    if not CLIENT_ID:
        return HTMLResponse("OAuth not configured.", status_code=500)
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "prompt": "consent",
    })
    return RedirectResponse(f"https://discord.com/api/oauth2/authorize?{params}")


def _github_redirect_uri(request: Request) -> str:
    """Callback URL for the current request. Uses GITHUB_REDIRECT_URI when set,
    otherwise derives it from the request host so it's always correct in prod."""
    env = os.environ.get("GITHUB_REDIRECT_URI", "")
    if env:
        return env
    host = request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto", "")
    if not proto:
        proto = "https" if "prowlbot.xyz" in host else "http"
    return f"{proto}://{host}/callback/github"


@app.get("/login/nerimity")
async def login_nerimity(request: Request):
    """Start Nerimity OAuth. When already signed in to Prowl this links the
    Nerimity account to the current Prowl account (handled in the callback);
    otherwise it can sign in to an account that already has it linked.
    The redirect URI is the bare site root because Nerimity caps it at 20
    characters (see _nerimity_redirect_uri)."""
    if not NERIMITY_CLIENT_ID:
        return HTMLResponse(
            "<html><body style='background:#0a0a0a;color:#e9edf5;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;'>"
            "<div style='text-align:center;'><h2>Nerimity sign-in is coming soon</h2>"
            "<p style='color:#888;'>We're building the Nerimity backend - check back later!</p>"
            "<p><a href='/' style='color:#a78bfa;'>← Back to Prowl</a></p></div></body></html>",
            status_code=200,
        )
    # CSRF protection: bind this authorization attempt to the session and have
    # Nerimity echo it back on the redirect. If it ever fails to echo, the
    # callback still proceeds (state is only enforced when both sides send one).
    state = secrets.token_urlsafe(16)
    request.session["nerimity_oauth_state"] = state
    params = urllib.parse.urlencode({
        "clientId": NERIMITY_CLIENT_ID,
        "redirectUri": _nerimity_redirect_uri(request),
        "scopes": NERIMITY_SCOPES,
        "state": state,
    })
    return RedirectResponse(
        f"https://nerimity.com/authorize?{params}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def _finish_nerimity(request: Request, code: str = None, state: str = None):
    """Complete Nerimity OAuth. Called from the root route (because Nerimity's
    20-char redirect-URI cap forbids a /callback/nerimity sub-page) and from the
    legacy /callback/nerimity route. A logged-in dashboard user gets their
    Nerimity account linked; otherwise the Nerimity account signs in (only if
    it's already connected). Verifies the CSRF `state` when both sides send one.
    """
    sess_state = request.session.get("nerimity_oauth_state")
    request.session.pop("nerimity_oauth_state", None)
    if state and sess_state and state != sess_state:
        return RedirectResponse("/login")
    if not code:
        return RedirectResponse("/login")
    if not NERIMITY_CLIENT_ID or not NERIMITY_CLIENT_SECRET:
        return RedirectResponse("/login")

    access = await _nerimity_token_request(
        request,
        {
            "grantType": "authorization_code",
            "redirectUri": _nerimity_redirect_uri(request),
            "code": code,
        },
    )
    if not access:
        return RedirectResponse("/login")

    profile = await nerimity_get("/oauth2/users/current", access)
    nuser = profile.get("user") if isinstance(profile, dict) else None
    if not isinstance(nuser, dict) or not nuser.get("id"):
        return RedirectResponse("/login")

    nid = str(nuser["id"])
    nname = nuser.get("username", "") or ""
    user = get_user(request)
    if user and user.get("id"):
        try:
            await execute(
                "UPDATE users SET nerimity_id = ?, nerimity_username = ? WHERE id = ?",
                nid, nname, str(user.get("id")),
            )
        except Exception as e:
            logger.error("Nerimity account link failed: %s", e)
        return RedirectResponse("/servers")

    # Sign-in via Nerimity: only works if the account already has this
    # connection linked.
    row = await fetchrow(
        "SELECT id, username, global_name, avatar, email FROM users WHERE nerimity_id = ?",
        nid,
    )
    if row:
        request.session["user"] = dict(row)
        try:
            await execute("UPDATE users SET last_login = ? WHERE id = ?", time.time(), str(row["id"]))
        except Exception as e:
            logger.error("Nerimity login last_login update failed: %s", e)
        return RedirectResponse("/servers")
    return RedirectResponse("/login?error=no_account")


@app.get("/callback/nerimity")
async def callback_nerimity(request: Request, code: str = None, state: str = None):
    """Legacy sub-page callback. Unused now that the redirect URI is the site
    root (Nerimity's 20-char cap), but kept for backwards compatibility and for
    when NERIMITY_REDIRECT_URI is explicitly set to this path."""
    return await _finish_nerimity(request, code, state)


@app.get("/login/github")
async def login_github(request: Request):
    """Start GitHub OAuth. Adds a fresh state param (unique authorize URL, so
    GitHub can't serve a stale 304) and marks the redirect no-store so neither
    the browser nor Vercel's edge caches the OAuth hop."""
    github_id = os.environ.get("GITHUB_CLIENT_ID", "")
    if not github_id:
        return HTMLResponse("GitHub OAuth not configured yet.", status_code=503)
    redirect_uri = _github_redirect_uri(request)
    state = secrets.token_urlsafe(16)
    request.session["github_oauth_state"] = state
    params = urllib.parse.urlencode({
        "client_id": github_id,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "allow_signup": "true",
        "state": state,
    })
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?{params}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/callback")
async def callback(request: Request, code: str = None):
    if not code:
        return RedirectResponse("/dashboard")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
    if r.status_code != 200:
        return RedirectResponse("/dashboard")

    token_json = r.json()
    access_token = token_json.get("access_token")
    if not access_token:
        return RedirectResponse("/dashboard")

    request.session["token"] = access_token
    user = await discord_get("/users/@me", access_token)
    if user:
        request.session["user"] = user
        await _upsert_user(user)

    return RedirectResponse("/dashboard")


@app.get("/callback/github")
async def callback_github(request: Request, code: str = None, state: str = None):
    """Complete GitHub OAuth. A logged-in dashboard user gets their GitHub
    account linked to their Prowl account. Otherwise the GitHub account is
    used to sign in, which only works if it's already connected (has a
    matching github_id in the users table)."""
    if not code:
        return RedirectResponse("/login")
    expected_state = request.session.get("github_oauth_state")
    request.session.pop("github_oauth_state", None)
    if not expected_state or not state or state != expected_state:
        return RedirectResponse("/login")
    github_id = os.environ.get("GITHUB_CLIENT_ID", "")
    github_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
    redirect_uri = _github_redirect_uri(request)
    if not github_id or not github_secret:
        return RedirectResponse("/login")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": github_id,
                    "client_secret": github_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            if r.status_code != 200:
                return RedirectResponse("/login")
            token_json = r.json()
            access_token = token_json.get("access_token")
            if not access_token:
                return RedirectResponse("/login")
            ui = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            if ui.status_code != 200:
                return RedirectResponse("/login")
            profile = ui.json()
    except Exception as e:
        logger.error("GitHub OAuth failed: %s", e)
        return RedirectResponse("/login")

    gid = str(profile.get("id", "") or "")
    gname = profile.get("login", "") or ""
    gemail = profile.get("email", "") or ""
    user = get_user(request)
    if user and user.get("id"):
        try:
            await execute(
                "UPDATE users SET github_id = ?, github_username = ?, github_email = ? WHERE id = ?",
                gid, gname, gemail, str(user.get("id")),
            )
        except Exception as e:
            logger.error("GitHub account link failed: %s", e)
        return RedirectResponse("/guild/profile")

    # Sign-in via GitHub: only works if the account already has this GitHub
    # connection linked.
    row = await fetchrow(
        "SELECT id, username, global_name, avatar, email FROM users WHERE github_id = ?",
        gid,
    )
    if row:
        request.session["user"] = dict(row)
        try:
            await execute("UPDATE users SET last_login = ? WHERE id = ?", time.time(), str(row["id"]))
        except Exception as e:
            logger.error("GitHub login last_login update failed: %s", e)
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login?error=no_account")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/dashboard")
async def dashboard_redirect(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login")

    guilds = await get_user_guilds_filtered(request)
    if not guilds:
        return templates.TemplateResponse(request, "servers.html", {
            "user": user, "guilds": [], "config": _cfg(),
        })

    selected = get_selected_guild(request)
    if not selected or str(selected.get("id")) not in {str(g["id"]) for g in guilds}:
        return templates.TemplateResponse(request, "servers.html", {
            "user": user, "guilds": guilds, "config": _cfg(),
        })

    return RedirectResponse(f"/guild/{selected['id']}/overview")


@app.get("/servers")
async def servers_page(request: Request):
    """Always shows the server picker, never auto-redirects."""
    user = get_user(request)
    if not user:
        return RedirectResponse("/login")

    discord_guilds = await get_user_guilds_filtered(request)
    nerimity_guilds = await get_nerimity_servers(request)
    # Platform badges only appear when both accounts are linked to Prowl
    show_platform = bool(nerimity_guilds)
    guilds = [dict(g, platform="discord") for g in discord_guilds] + nerimity_guilds
    return templates.TemplateResponse(request, "servers.html", {
        "user": user, "guilds": guilds, "show_platform": show_platform, "config": _cfg(),
    })


@app.post("/select_guild")
async def select_guild(
    request: Request,
    guild_id: str = Form(...),
    guild_name: str = Form(""),
    guild_icon: str = Form(""),
):
    request.session["selected_guild"] = {"id": guild_id, "name": guild_name, "icon": guild_icon}
    return RedirectResponse(f"/guild/{guild_id}/overview", status_code=303)


@app.get("/guild/profile")
async def guild_profile(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/dashboard")
    # No guild context; the sidebar uses the last-viewed guild (saved client-side)
    return templates.TemplateResponse(request, "dashboard/profile.html", {
        "user": user,
        "guild": None,
        "guild_id": "",
        "active_panel": "profile",
        "bot_data": {},
        "config": _cfg(),
    }, headers={"Cache-Control": "no-store"})


@app.get("/guild/{guild_id}/{panel}")
@app.get("/guild/{guild_id}/")
async def dashboard(request: Request, guild_id: str, panel: str = "overview"):
    user = get_user(request)
    if not user:
        return RedirectResponse("/dashboard")

    guilds = await get_user_guilds_filtered(request)
    guild_ids = {str(g["id"]) for g in guilds}
    if guild_id not in guild_ids:
        return RedirectResponse("/dashboard")

    guild_info = next((g for g in guilds if str(g["id"]) == guild_id), None)

    valid_panels = [
        "overview", "welcomer", "ai", "moderation", "members", "logs", "automod",
        "music", "leveling", "verification", "automation",
        "social_alerts", "invite_tracker", "tickets", "global_chat",
        "autoresponder", "settings", "raid_protection", "profile",
        "aliases", "bot_profile", "reminders", "afk", "giveaways",
        "birthday", "activity_roles", "badges", "temp_channels", "frenzy", "more",
        "rank-editor",
    ]
    if panel not in valid_panels:
        panel = "overview"

    ctx = {
        "user": user,
        "guild": guild_info,
        "guild_id": guild_id,
        "active_panel": panel,
        "bot_data": {},
        "config": _cfg(),
    }

    return templates.TemplateResponse(request, f"dashboard/{panel}.html", ctx,
                                      headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
#  Routes - API v1
# ---------------------------------------------------------------------------

# Simple per-user rate limiter to mitigate API scraping
_RATE_LIMIT = {}
_RATE_WINDOW = 60
_RATE_MAX = 60  # requests per minute per user


def _check_rate_limit(user_id: str):
    now = time.time()
    entry = _RATE_LIMIT.get(user_id)
    if not entry or now - entry["start"] >= _RATE_WINDOW:
        _RATE_LIMIT[user_id] = {"start": now, "count": 1}
        return
    entry["count"] += 1
    if entry["count"] > _RATE_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in a moment.")
    if len(_RATE_LIMIT) > 10000:
        _RATE_LIMIT.clear()


async def require_auth(request: Request):
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _check_rate_limit(str(user.get("id", "anon")))
    return user


async def require_guild_access(request: Request, guild_id: str):
    user = await require_auth(request)
    guilds = await get_user_guilds_filtered(request)
    if not any(str(g["id"]) == guild_id for g in guilds):
        raise HTTPException(status_code=403, detail="No access to this guild")
    return user


async def is_guild_moderator(request: Request, guild_id: str) -> bool:
    """True if the logged-in user can manage messages in this guild.

    Mirrors the bot's slash-command gate (manage_messages). Checks guild owner,
    configured mod_roles, or a role granting administrator / manage_messages.
    """
    user = request.session.get("user") or {}
    uid = str(user.get("id") or "")
    if not uid:
        return False
    try:
        d = await get_guild_data(guild_id)
    except Exception:
        d = None
    if not d:
        return False
    if str(d.get("owner_id")) == uid:
        return True
    member_roles = None
    for m in (d.get("members") or []):
        mid = str(m.get("user", {}).get("id") or m.get("id") or "")
        if mid == uid:
            member_roles = [str(r) for r in (m.get("roles") or [])]
            break
    if member_roles is None:
        return False
    try:
        mod_settings = await _get_mod_settings(guild_id)
        mod_role_ids = set(str(r) for r in (mod_settings.get("mod_roles") or []))
        if mod_role_ids and set(member_roles) & mod_role_ids:
            return True
    except Exception:
        pass
    perms = 0
    for r in (d.get("roles") or []):
        if str(r.get("id")) in member_roles:
            perms |= int(r.get("permissions", 0) or 0)
    if perms & (1 << 3) or perms & (1 << 13):
        return True
    return False


async def require_mod(request: Request, guild_id: str):
    """FastAPI dependency: member must be a guild moderator (manage_messages+).

    Uses the same check as the bot's slash-command gate. Prevents any dashboard
    viewer from performing server-mutating/mod actions.
    """
    await require_guild_access(request, guild_id)
    if not await is_guild_moderator(request, guild_id):
        raise HTTPException(status_code=403, detail="Moderators only.")


# ---------------------------------------------------------------------------
#  Accounts
# ---------------------------------------------------------------------------

async def _upsert_user(user: dict):
    """Record (or update) a user account on login."""
    if not user or not user.get("id"):
        return
    try:
        await execute(
            "INSERT INTO users (id, username, global_name, avatar, email, created_at, last_login) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET username = excluded.username, "
            "global_name = excluded.global_name, avatar = excluded.avatar, "
            "email = excluded.email, last_login = excluded.last_login",
            str(user["id"]), user.get("username", ""), user.get("global_name") or user.get("username", ""),
            user.get("avatar"), user.get("email", ""), time.time(), time.time(),
        )
    except Exception as e:
        logger.error("Failed to upsert user account: %s", e)


@app.get("/api/v1/account")
async def account_info(request: Request):
    user = await require_auth(request)
    FIELDS = "id, username, global_name, created_at, last_login, github_id, github_username, github_email, nerimity_id, nerimity_username"
    row = await fetchrow(f"SELECT {FIELDS} FROM users WHERE id = ?", str(user.get("id")))
    if not row:
        # Account not recorded yet (e.g. logged in before this feature) - record it now
        await _upsert_user(user)
        row = await fetchrow(f"SELECT {FIELDS} FROM users WHERE id = ?", str(user.get("id")))
    return {"account": dict(row) if row else None}


@app.post("/api/v1/account/github/unlink")
async def account_github_unlink(request: Request):
    user = await require_auth(request)
    try:
        await execute("UPDATE users SET github_id = '', github_username = '', github_email = '' WHERE id = ?", str(user.get("id")))
    except Exception as e:
        logger.error("github unlink failed: %s", e)
    return {"ok": True}


@app.post("/api/v1/account/nerimity/unlink")
async def account_nerimity_unlink(request: Request):
    user = await require_auth(request)
    try:
        await execute("UPDATE users SET nerimity_id = '', nerimity_username = '' WHERE id = ?", str(user.get("id")))
    except Exception as e:
        logger.error("nerimity unlink failed: %s", e)
    request.session.pop("nerimity_token", None)
    request.session.pop("nerimity_refresh", None)
    return {"ok": True}


@app.post("/api/v1/account/delete")
async def account_delete(request: Request):
    user = await require_auth(request)
    uid = str(user.get("id"))
    username = user.get("username", "Unknown")
    # Find every server this user owns
    owned = []
    try:
        rows = await query("SELECT guild_id, data FROM guild_data")
        for row in rows:
            d = _parse_guild_data(row)
            if d and str(d.get("owner_id")) == uid:
                owned.append(str(row["guild_id"]))
    except Exception as e:
        logger.error("account delete: failed to list owned guilds: %s", e)
    # Wipe moderation state for those servers, then queue Prowl to leave each one
    for gid in owned:
        try:
            await execute("DELETE FROM muted_users WHERE guild_id = ?", gid)
            await execute("DELETE FROM mod_log WHERE guild_id = ?", gid)
            await execute("DELETE FROM mod_actions WHERE guild_id = ?", gid)
            await _queue_action(gid, "leave_guild", uid, username, "Account deleted", None, "System")
        except Exception as e:
            logger.error("account delete: cleanup failed for %s: %s", gid, e)
    await execute("DELETE FROM users WHERE id = ?", uid)
    request.session.clear()
    return {"ok": True, "cleaned_guilds": len(owned)}


@app.get("/api/v1/health")
async def api_health():
    return {"status": "ok", "service": "prowl-api"}


@app.get("/api/v1/ping")
async def api_ping():
    return {"ping": "pong"}


@app.post("/api/v1/turnstile/verify")
async def turnstile_verify(request: Request):
    """Validate a Cloudflare Turnstile token and set a signed cookie."""
    TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
    if not TURNSTILE_SECRET:
        return HTMLResponse("Turnstile not configured.", status_code=500)

    body = await request.json()
    token = body.get("token", "")
    if not token:
        return JSONResponse({"ok": False, "error": "Missing token."}, status_code=400)

    remoteip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": remoteip},
            )
            result = resp.json()
    except Exception as e:
        logger.error("Turnstile verify error: %s", e)
        return JSONResponse({"ok": False, "error": "Verification service error."}, status_code=502)

    if not result.get("success"):
        return JSONResponse({"ok": False, "error": "Verification failed.", "codes": result.get("error-codes", [])}, status_code=400)

    # Set turnstile flag in session (RotatingSessionMiddleware will persist it in the signed cookie)
    scope_session = request.scope.get("session")
    if scope_session is not None:
        scope_session["_turnstile_ts"] = int(time.time())

    return {"ok": True}


@app.get("/api/v1/db-test")
async def api_db_test(request: Request):
    await require_auth(request)
    try:
        row = await fetchval("SELECT 1")
        return {"db": "connected", "result": row}
    except Exception as e:
        return {"db": "error", "detail": str(e)}


@app.get("/api/v1/@me")
async def api_me(request: Request):
    user = await require_auth(request)
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "discriminator": user.get("discriminator"),
        "avatar": user.get("avatar"),
    }


@app.get("/api/v1/status")
async def api_status(request: Request):
    await require_auth(request)
    rows = await query("SELECT key, value FROM bot_stats")
    data = {row["key"]: row["value"] for row in rows}

    def safe_int(key, default=0):
        try:
            return int(data.get(key, default))
        except (ValueError, TypeError):
            return default

    return {
        "status": "online" if data.get("bot_status") == "Running" else "offline",
        "guilds": safe_int("num_guilds"),
        "users": safe_int("total_users"),
        "active_users": safe_int("active_users"),
        "uptime": data.get("uptime", "N/A"),
        "memory_mb": data.get("memory_usage_mb", "N/A"),
        "cpu_percent": data.get("cpu_usage_percent", "N/A"),
        "version": data.get("bot_version", "unknown"),
        "python_version": data.get("python_version", "unknown"),
        "channels": safe_int("num_channels"),
        "roles": safe_int("num_roles"),
        "emojis": safe_int("num_emojis"),
    }


# ---------------------------------------------------------------------------
#  Public status summary (no auth) - powers the status.prowlbot.xyz page
# ---------------------------------------------------------------------------

_INCIDENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    severity TEXT NOT NULL DEFAULT 'minor',
    starts_at REAL NOT NULL,
    resolves_at REAL
);
"""


async def _ensure_incidents():
    try:
        await execute(_INCIDENTS_TABLE_SQL)
    except Exception as e:
        logger.error("incidents table failed: %s", e)


async def _ensure_user_columns():
    """Self-heal the users table. The nerimity_id / nerimity_username columns
    were added to the schema after many prod DBs were created, and
    setup_schema.py is not run on deploy - so link/unlink silently no-ops until
    the columns exist. Add them idempotently at startup. Turso rejects
    ADD COLUMN IF NOT EXISTS, so we use the plain form and tolerate an
    already-existing column."""
    for col in ("nerimity_id", "nerimity_username"):
        try:
            await execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" not in msg and "already exists" not in msg:
                logger.error("users.%s ensure failed: %s", col, e)


async def _fetch_bot_action_stats():
    """Pull the bot server's hourly dashboard-action counts for the status page.

    Returns a list of {"t": ts, "count": n} hourly buckets (last 24h), or []
    when the bridge is unreachable/not configured - never raises."""
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(
                BOT_SERVER_URL.rstrip("/") + "/api/stats/actions",
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, dict):
                return []
            acts = data.get("actions", [])
            return [a for a in acts if isinstance(a, dict) and "t" in a and "count" in a] if isinstance(acts, list) else []
    except Exception:
        return []


@app.get("/api/v1/status/summary")
async def status_summary(request: Request):
    t0_all = time.perf_counter()
    rows = await query("SELECT key, value, updated_at FROM bot_stats")
    data = {row["key"]: row["value"] for row in rows}
    last_upd = {row["key"]: row["updated_at"] for row in rows}

    def safe_int(key, default=0):
        try:
            return int(data.get(key, default))
        except (ValueError, TypeError):
            return default

    def safe_str(key, default=""):
        return data.get(key, default)

    # Heartbeat staleness: the bot pushes stats every ~2 min. If nothing has
    # been written recently the process is down, regardless of the stored value.
    STALE_BOT_SECONDS = 240
    last_sync = 0
    for v in last_upd.values():
        try:
            last_sync = max(last_sync, int(v))
        except (TypeError, ValueError):
            pass
    bot_stale = bool(last_sync) and (time.time() - last_sync) > STALE_BOT_SECONDS

    def ago(ts):
        if not ts:
            return "never"
        s = int(max(0, time.time() - ts))
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        return f"{s // 3600}h ago"

    # Live DB check + measured latency
    db_ok = False
    db_ms = None
    try:
        t0 = time.perf_counter()
        await fetchval("SELECT 1")
        db_ok = True
        db_ms = int((time.perf_counter() - t0) * 1000)
    except Exception:
        pass

    bot_up = data.get("bot_status") == "Running" and not bot_stale
    try:
        gateway_ping = int(float(data.get("gateway_ping_ms", 0)))
        gateway_ping = gateway_ping if gateway_ping > 0 else None
    except (TypeError, ValueError):
        gateway_ping = None

    # Live Discord API check (public gateway endpoint)
    discord_ok = False
    discord_ms = None
    try:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{DISCORD_API}/gateway")
        discord_ok = r.status_code == 200
        discord_ms = int((time.perf_counter() - t0) * 1000)
    except Exception:
        pass

    music_raw = data.get("music_status", "disabled")
    if bot_stale and music_raw in ("operational", "disabled", "unknown"):
        music_status = "down"
    else:
        music_status = music_raw if music_raw in ("operational", "degraded", "down", "disabled") else "disabled"
    shard_count = safe_int("num_shards", 1) or 1

    services = [
        {"id": "gateway", "name": "Bot Gateway", "status": "operational" if bot_up else "down",
         "detail": f"Uptime {safe_str('uptime', 'N/A')}" if bot_up else f"No heartbeat · last seen {ago(last_sync)}",
         "latency_ms": gateway_ping},
        {"id": "web", "name": "Web Dashboard & API", "status": "operational",
         "detail": f"v{safe_str('bot_version', '?')} · Python {safe_str('python_version', '?')}", "latency_ms": None},
        {"id": "db", "name": "Database", "status": "operational" if db_ok else "down",
         "detail": "Turso (libSQL)", "latency_ms": db_ms},
        {"id": "discord", "name": "Discord API", "status": "operational" if discord_ok else "down",
         "detail": f"{shard_count} shard{'s' if shard_count != 1 else ''}", "latency_ms": discord_ms},
        {"id": "music", "name": "Music", "status": music_status,
         "detail": "Audio playback", "latency_ms": None},
    ]

    shards = []
    try:
        raw = data.get("shards")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                shards = [dict(s) for s in parsed]
    except Exception:
        pass

    incidents = []
    try:
        rows_i = await query(
            "SELECT title, body, status, severity, starts_at, resolves_at "
            "FROM incidents ORDER BY starts_at DESC"
        )
        incidents = [
            {
                "title": r["title"], "body": r["body"], "status": r["status"],
                "severity": r["severity"], "starts_at": r["starts_at"],
                "resolves_at": r["resolves_at"],
            }
            for r in rows_i
        ]
    except Exception:
        pass

    requests = []
    try:
        since = int(time.time() // 3600) * 3600 - 23 * 3600
        rows_r = await query(
            "SELECT bucket_ts, count FROM request_stats WHERE bucket_ts >= ? ORDER BY bucket_ts ASC",
            since,
        )
        by_bucket = {int(r["bucket_ts"]): int(r["count"]) for r in rows_r}
        for i in range(24):
            b = since + i * 3600
            requests.append({"t": b, "count": by_bucket.get(b, 0)})
    except Exception:
        pass

    actions = await _fetch_bot_action_stats()

    web_ms = int((time.perf_counter() - t0_all) * 1000)
    for s in services:
        if s["id"] == "web":
            s["latency_ms"] = web_ms

    return {
        "stats": {
            "status": "online" if bot_up else "offline",
            "guilds": safe_int("num_guilds"),
            "users": safe_int("total_users"),
            "active_users": safe_int("active_users"),
            "commands": safe_int("total_commands"),
            "uptime": safe_str("uptime", "N/A"),
            "memory_mb": safe_str("memory_usage_mb", "N/A"),
            "cpu_percent": safe_str("cpu_usage_percent", "N/A"),
            "version": safe_str("bot_version", "unknown"),
            "python_version": safe_str("python_version", "unknown"),
            "shards": safe_int("num_shards"),
            "last_restart": safe_str("last_restart", "unknown"),
            "last_sync": last_sync,
            "stale": bot_stale,
            "downtime": (int(time.time() - last_sync) if bot_stale else None),
        },
        "services": services,
        "shards": shards,
        "incidents": incidents,
        "requests": requests,
        "actions": actions,
    }


@app.get("/api/v1/guilds")
async def api_guilds(request: Request):
    await require_auth(request)
    rows = await query("SELECT data FROM guild_data ORDER BY updated_at DESC")
    guilds = []
    for row in rows:
        g = _parse_guild_data(row)
        if g is None:
            continue
        guilds.append({
            "id": g.get("id"),
            "name": g.get("name"),
            "icon_url": g.get("icon_url"),
            "member_count": g.get("member_count", 0),
            "online_count": g.get("online_count", 0),
        })
    return {"guilds": guilds}


@app.get("/api/v1/guild/{guild_id}")
async def api_guild(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    g = await get_guild_data(guild_id)
    if g is not None:
        return g
    return JSONResponse({"error": "Guild not found"}, status_code=404)


@app.get("/api/v1/commands")
async def api_commands(request: Request):
    await require_auth(request)
    row = await fetchrow("SELECT value FROM bot_stats WHERE key = 'all_commands'")
    commands = []
    try:
        commands = json.loads(row["value"]) if row else []
    except (json.JSONDecodeError, TypeError):
        pass

    row2 = await fetchrow("SELECT value FROM bot_stats WHERE key = 'loaded_cogs'")
    cogs = []
    try:
        cogs = json.loads(row2["value"]) if row2 else []
    except (json.JSONDecodeError, TypeError):
        pass

    return {"commands": commands, "cogs": cogs}


# ---------------------------------------------------------------------------
#  Moderation API v1
# ---------------------------------------------------------------------------

MOD_SETTINGS_DEFAULTS = {
    "dm_on_action": True, "require_reason": True, "silent_mod": False,
    "auto_thread": False, "track_stats": True,
    "cmd_ban": True, "cmd_kick": True, "cmd_mute": True, "cmd_timeout": True, "cmd_warn": True,
    # ── Modlog ──
    "modlog_channel_id": None,
    # ── Ban ──
    "ban_dm": True, "ban_purge": True, "ban_message": "{username} has been banned.", "ban_message_enabled": True,
    "ban_message_mode": "basic", "ban_embed": {},
    # ── Temp ban ──
    "tempban_dm": True, "tempban_purge": True,
    "tempban_message": "{username} has been temporarily banned.", "tempban_message_enabled": True,
    "tempban_message_mode": "basic", "tempban_embed": {},
    "tempban_duration": 1440,
    # ── Mute ──
    "mute_dm": True, "mute_duration": 60, "mute_message": "{username} has been muted.", "mute_message_enabled": True,
    "mute_message_mode": "basic", "mute_embed": {},
    # ── Kick ──
    "kick_dm": True, "kick_message": "{username} has been kicked.", "kick_message_enabled": True,
    "kick_message_mode": "basic", "kick_embed": {},
    # ── Warn ──
    "warn_dm": True, "warn_message": "{username} has been warned.", "warn_message_enabled": True,
    "warn_message_mode": "basic", "warn_embed": {},
}


async def _get_mod_settings(guild_id: str):
    return await fetchrow_cached("mod_settings", "SELECT settings FROM mod_settings WHERE guild_id = ?", guild_id, MOD_SETTINGS_DEFAULTS)


def _valid_snowflake(value) -> bool:
    """True if value is None or a numeric string/int (Discord ID)."""
    if value is None:
        return True
    s = str(value).strip()
    return s.isdigit() and len(s) <= 20


def _sanitize_extra_alerts(value):
    """Validate extra_alerts: dict of {platform: [{target, ping_role, message}]}, max 5/platform."""
    if not isinstance(value, dict):
        return None, "extra_alerts must be an object"
    clean = {}
    for platform, items in value.items():
        if platform not in ("youtube", "twitch", "twitter"):
            return None, f"invalid platform '{platform}'"
        if not isinstance(items, list):
            return None, f"'{platform}' alerts must be a list"
        if len(items) > 5:
            return None, f"'{platform}' exceeds max of 5 alerts"
        clean_items = []
        for it in items:
            if not isinstance(it, dict):
                return None, "each alert must be an object"
            target = str(it.get("target") or "").strip()
            if not target:
                return None, "alert target is required"
            ping_role = it.get("ping_role")
            if ping_role is not None and not _valid_snowflake(ping_role):
                return None, "invalid ping role id"
            message = it.get("message")
            if message is not None and not isinstance(message, str):
                return None, "message must be a string"
            clean_items.append({
                "target": target[:200],
                "ping_role": str(ping_role) if ping_role else None,
                "message": message[:500] if message else None,
            })
        clean[platform] = clean_items
    return clean, None


def _sanitize_setting(key: str, value, defaults: dict):
    """Coerce/validate a single settings key. Returns (clean_value, error)."""
    if key not in defaults:
        return value, f"unknown key '{key}'"
    default = defaults[key]

    # Discord ID fields (channel/role selectors) must be a snowflake or null
    id_key = (key.endswith("_ping_role") or key.endswith("_announce_channel_id")
              or key.endswith("_channel")
              or key in ("modlog_channel_id", "default_announce_channel_id", "default_ping_role",
                         "channel_id", "auto_role_id", "verified_role_id", "support_role_id",
                         "category_id", "panel_channel_id", "log_channel_id", "panel_message_id"))
    if id_key:
        if value is None or value == "":
            return None, None
        if not _valid_snowflake(value):
            return None, f"'{key}' must be a valid Discord ID or null"
        return str(value), None

    # Lists (e.g. auto_role_ids) and dicts (embed data) pass through as-is
    if isinstance(default, list):
        if not isinstance(value, list):
            return None, f"'{key}' must be a list"
        return value, None
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return None, f"'{key}' must be an object"
        return value, None

    # Booleans
    if isinstance(default, bool):
        return bool(value), None
    # None-able free-text (handles, channel ids, messages)
    if default is None:
        if value is None or value == "":
            return None, None
        return str(value)[:500], None
    # Integers (durations)
    if isinstance(default, int):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None, f"'{key}' must be an integer"
        return n, None
    # Floats (rates / multipliers)
    if isinstance(default, float):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None, f"'{key}' must be a number"
        if f < 0:
            return None, f"'{key}' must be positive"
        return f, None
    return str(value), None


async def _notify_bot_cache_invalidate(table, guild_id):
    """Best-effort fire-and-forget: tell the bot to drop its cached settings.

    The Turso write in ``_save_settings`` has already succeeded before this is
    called, so a failure here must never roll back that write. A missed
    invalidation self-heals via the bot's cache TTL, so this is purely an
    optimization to push fresh settings to the bot faster than the TTL.
    """
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return
    url = BOT_SERVER_URL.rstrip("/") + "/cache/invalidate"

    async def _post():
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(
                    url,
                    json={"table": table, "guild_id": str(guild_id)},
                    headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
                )
        except Exception as e:
            logger.debug(f"bot cache invalidate failed for {table}/{guild_id}: {e}")

    try:
        asyncio.create_task(_post())
    except Exception:
        pass


async def _save_settings(table, guild_id, key, value, defaults):
    """Validate + persist a settings key with server-side checks."""
    clean, err = _sanitize_setting(key, value, defaults)
    if err:
        return err
    current = dict(defaults)
    row = await fetchrow(f"SELECT settings FROM {table} WHERE guild_id = ?", str(guild_id))
    if row:
        stored = row["settings"]
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except (json.JSONDecodeError, TypeError):
                stored = {}
        if isinstance(stored, dict):
            current.update(stored)
    current[key] = clean
    result = await execute(
        f"INSERT INTO {table} (guild_id, settings, updated_at) VALUES (?, ?, ?) "
        f"ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(current), time.time(),
        json.dumps(current), time.time(),
    )
    if result is None:
        return "database write failed"
    _update_cache(table, guild_id, current)
    _notify_bot_cache_invalidate(table, guild_id)
    return None


@app.get("/api/v1/mod/{guild_id}/settings")
async def mod_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_mod_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/mod/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def mod_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    # Custom action embeds are dicts - validate via _sanitize_panel_embed
    if key.endswith("_embed"):
        clean, err = _sanitize_panel_embed(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("mod_settings", str(guild_id), key, clean, MOD_SETTINGS_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    err = await _save_settings("mod_settings", str(guild_id), key, value, MOD_SETTINGS_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/mod/{guild_id}/feed")
async def mod_feed(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    scope = request.query_params.get("scope", "all")
    MOD_ONLY = ("kick", "ban", "unban", "tempban", "mute", "unmute", "warn", "purge", "lockdown")
    sql = "SELECT user_name, action, reason, moderator, created_at FROM mod_log WHERE guild_id = ?"
    params = [str(guild_id)]
    if scope == "mod":
        sql += f" AND action IN ({','.join(['?']*len(MOD_ONLY))})"
        params.extend(MOD_ONLY)
    sql += " ORDER BY created_at DESC LIMIT 20"
    rows = await query(sql, *params)
    if rows:
        # Build channel-name lookup for purge events that stored raw IDs
        ch_map = {}
        parsed = await get_guild_data(guild_id)
        if parsed and isinstance(parsed.get("channels"), list):
            for c in parsed["channels"]:
                ch_map[str(c.get("id"))] = c.get("name", "")
        ACTION_LABELS = {
            "ban": "Banned", "tempban": "Temp-Banned", "unban": "Unbanned",
            "kick": "Kicked", "mute": "Muted", "unmute": "Unmuted", "warn": "Warned",
            "purge": "Purged messages", "lockdown": "Lockdown",
            "verify_panel": "Deployed verification panel",
            "verify_user": "Verified member", "add_role": "Added role",
            "remove_role": "Removed role", "nickname": "Changed nickname",
            "emergency_lock": "Locked down server", "emergency_unlock": "Unlocked server",
            "panel_send": "Deployed ticket panel",
        }
        return {"events": [{
            "user": (f"#{ch_map.get(r['user_name'], r['user_name'])}" if r["action"] == "purge" and r["user_name"].isdigit() else (r["user_name"] if r["user_name"] and r["user_name"] != "0" else r.get("moderator") or "Dashboard")),
            "action": ACTION_LABELS.get(r["action"], r["action"]),
            "reason": r.get("reason", ""),
            "moderator": r.get("moderator") or "",
            "time": _relative_time(r["created_at"]),
            "color": {"ban": "red", "kick": "red", "tempban": "red", "mute": "blue", "unmute": "green", "warn": "yellow", "unban": "green", "purge": "blue", "lockdown": "gray", "verify_panel": "gray", "verify_user": "green", "add_role": "blue", "remove_role": "blue", "nickname": "gray"}.get(r["action"], "gray"),
        } for r in rows]}
    return {"events": []}


@app.post("/api/v1/mod/{guild_id}/log", dependencies=[Depends(require_mod)])
async def mod_log_push(guild_id: str, request: Request):
    """Endpoint for the bot to push moderation events."""
    body = await request.json()
    await execute(
        "INSERT INTO mod_log (guild_id, user_id, user_name, action, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        str(guild_id), body.get("user_id", ""), body.get("user_name", ""),
        body.get("action", ""), body.get("reason", ""), time.time(),
    )
    return {"ok": True}


async def push_mod_event(guild_id, user_id, user_name, action, reason=""):
    """Insert a moderation event into mod_log."""
    await execute(
        "INSERT INTO mod_log (guild_id, user_id, user_name, action, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        str(guild_id), str(user_id), user_name, action, reason, time.time(),
    )


_MOD_ACTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mod_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_name TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    moderator TEXT DEFAULT '',
    error TEXT DEFAULT '',
    duration INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    processed_at REAL,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_mod_actions_pending ON mod_actions (status, created_at);
"""


async def _ensure_mod_actions_table():
    await execute(_MOD_ACTIONS_TABLE_SQL)
    # Migration: add request_id if missing
    try:
        await execute("ALTER TABLE mod_actions ADD COLUMN request_id TEXT")
    except Exception:
        pass
    # Add unique index on request_id (can't use UNIQUE in ALTER TABLE ADD COLUMN)
    try:
        await execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mod_actions_request_id ON mod_actions (request_id)")
    except Exception:
        pass


_CAPTCHA_CODES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS captcha_codes (
    code TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    guild_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""


async def _validate_captcha_code(code: str, provider: str):
    """Check a code is valid (exists, right provider, unused, unexpired) WITHOUT consuming it.
    Returns the guild_id/user_id or None."""
    if not code:
        return None
    try:
        try:
            await execute(_CAPTCHA_CODES_TABLE_SQL)
        except Exception:
            pass
        row = await fetchrow(
            "SELECT used, expires_at, guild_id, user_id FROM captcha_codes WHERE code = ? AND provider = ?",
            code, provider,
        )
        if not row or row["used"] or time.time() > row["expires_at"]:
            return None
        return {"guild_id": row["guild_id"], "user_id": row["user_id"]}
    except Exception:
        return None


async def _consume_captcha_code(code: str, provider: str):
    """Validate AND mark a code used. Returns guild_id/user_id dict or None."""
    info = await _validate_captcha_code(code, provider)
    if not info:
        return None
    try:
        await execute("UPDATE captcha_codes SET used = 1 WHERE code = ?", code)
    except Exception:
        return None
    return info


async def _call_bot_direct(guild_id, action, user_id, user_name="", reason="", duration=None, moderator="", target=None, request_id=""):
    """Send a moderation quick-action straight to the bot's HTTP bridge.

    The bot owns the action's terminal status (executing -> completed/failed),
    keyed by request_id. We therefore never write a status here - doing so would
    risk marking an action 'failed' on a transport timeout while the bot is still
    executing, or racing the bot's own write. Returns (ok, message); the caller
    treats !ok as 'queued' and lets the bot/queue own the outcome.

    The auth token never leaves the server."""
    if action not in DIRECT_ACTIONS or not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return False, "bridge not configured"
    try:
        payload = {
            "action": action,
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "user_name": user_name,
            "reason": reason,
            "duration": duration,
            "moderator": moderator,
        }
        if target:
            payload["target"] = target
        if request_id:
            payload["request_id"] = request_id
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                BOT_SERVER_URL.rstrip("/") + "/api/action",
                json=payload,
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
            if r.status_code == 200:
                return True, (r.json().get("message") or "ok")
            # Bot responded but explicitly rejected - surface its real message.
            return False, (r.json().get("message") or f"bot responded {r.status_code}")
    except Exception as e:
        # Transport error (timeout / bot unreachable). Do NOT write a terminal
        # status: if the bot received the request it will write its own; if it
        # didn't, the row stays 'pending' and the bot's queue will retry it.
        return False, str(e)


async def _queue_action(guild_id, action, target_id, target_name="", reason="", duration=None, moderator=""):
    """Persist an action to mod_actions and return its request_id."""
    request_id = uuid.uuid4().hex
    try:
        duration_int = int(duration) if duration is not None and duration != "" else None
    except (ValueError, TypeError):
        duration_int = None
    try:
        await execute(
            "INSERT INTO mod_actions (guild_id, action, target_id, target_name, reason, moderator, duration, status, created_at, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            str(guild_id), action, str(target_id), target_name, reason, moderator,
            duration_int, time.time(), request_id,
        )
    except Exception:
        await _ensure_mod_actions_table()
        await execute(
            "INSERT INTO mod_actions (guild_id, action, target_id, target_name, reason, moderator, duration, status, created_at, request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            str(guild_id), action, str(target_id), target_name, reason, moderator,
            duration_int, time.time(), request_id,
        )
    return request_id


@app.get("/api/v1/mod/{guild_id}/debug")
async def mod_debug(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d is not None:
        return {"has_data": True, "has_members": "members" in d, "has_channels": "channels" in d, "has_roles": "roles" in d, "keys": list(d.keys()), "member_count": len(d.get("members", [])), "channel_count": len(d.get("channels", [])), "role_count": len(d.get("roles", []))}
    return {"has_data": False}


@app.get("/api/v1/mod/{guild_id}/members")
async def mod_members(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)

    def avatar_url(m):
        av = m.get("avatar_url") or m.get("avatar")
        if not av:
            return None
        if str(av).startswith("http"):
            return str(av)
        return f"https://cdn.discordapp.com/avatars/{m.get('id')}/{av}.png?size=64"

    if d and "members" in d:
        # Filter out the bot (bot user ID is in config); coerce IDs to strings
        return {"members": [{
            "id": str(m.get("id")), "name": m.get("name", ""), "display_name": m.get("display_name", ""),
            "avatar_url": avatar_url(m), "joined_at": m.get("joined_at"), "roles": m.get("roles") or [],
        } for m in d["members"] if str(m.get("id", "")) != _cfg().get("CLIENT_ID")]}
    return {"members": [
        {"id":"1001","name":"Alice","avatar_url":"https://cdn.discordapp.com/embed/avatars/0.png"},
        {"id":"1002","name":"Bob","avatar_url":"https://cdn.discordapp.com/embed/avatars/1.png"},
        {"id":"1003","name":"Charlie","avatar_url":"https://cdn.discordapp.com/embed/avatars/2.png"},
        {"id":"1004","name":"Diana","avatar_url":"https://cdn.discordapp.com/embed/avatars/3.png"},
        {"id":"1005","name":"Eve","avatar_url":"https://cdn.discordapp.com/embed/avatars/4.png"},
    ]}


@app.get("/api/v1/mod/{guild_id}/channels")
async def mod_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        # Coerce IDs to strings (JS-safe) - categories reported separately
        return {
            "channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0],
            "categories": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 4],
        }
    return {"channels": [
        {"id":"2001","name":"general","type":0},
        {"id":"2002","name":"chat","type":0},
        {"id":"2003","name":"spam","type":0},
    ], "categories": []}


@app.get("/api/v1/mod/{guild_id}/muted")
async def mod_muted(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    now = time.time()
    try:
        rows = await query(
            "SELECT user_id, user_name, reason, end_ts FROM muted_users WHERE guild_id = ? AND end_ts > ? ORDER BY end_ts ASC",
            str(guild_id), now,
        )
    except Exception:
        await execute(
            "CREATE TABLE IF NOT EXISTS muted_users (guild_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT DEFAULT '', reason TEXT DEFAULT '', end_ts REAL NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
        )
        rows = await query(
            "SELECT user_id, user_name, reason, end_ts FROM muted_users WHERE guild_id = ? AND end_ts > ? ORDER BY end_ts ASC",
            str(guild_id), now,
        )

    # Avatar lookup from guild_data members
    avatar_map = {}
    parsed = await get_guild_data(guild_id)
    if parsed and isinstance(parsed.get("members"), list):
        for m in parsed["members"]:
            av = m.get("avatar_url") or m.get("avatar")
            if av:
                if not str(av).startswith("http"):
                    av = f"https://cdn.discordapp.com/avatars/{m.get('id')}/{av}.png?size=64"
                avatar_map[str(m.get("id"))] = av

    muted = []
    for r in rows:
        uid = str(r["user_id"])
        avatar = avatar_map.get(uid)
        muted.append({
            "id": uid,
            "name": r["user_name"],
            "reason": r.get("reason", ""),
            "end_ts": r.get("end_ts") or 0,
            "avatar": avatar,
            "avatar_url": avatar,
        })
    return {"muted": muted}


@app.get("/api/v1/mod/{guild_id}/roles")
async def mod_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    roles = d.get("roles", []) if d else []
    settings = await _get_mod_settings(guild_id)
    mod_role_ids = settings.get("mod_roles", [])

    # Get bot's highest role position from guild data
    bot_role_pos = None
    if d and "bot_top_role_position" in d:
        bot_role_pos = d["bot_top_role_position"]

    exclude = ["level", "verified", "member", "bot"]
    result = []
    for r in roles:
        name = (r.get("name") or "").lower()
        # Skip @everyone (role ID matches guild ID)
        if str(r.get("id", "")) == str(guild_id):
            continue
        # Skip roles with excluded words and bot-managed roles
        if any(kw in name.split() for kw in exclude):
            continue
        tags = r.get("tags")
        if isinstance(tags, dict) and tags.get("bot_id"):
            continue
        if r.get("managed", False):
            continue
        # Auto-enable for roles with administrator permission (bit 3 = 8)
        perms = r.get("permissions", 0) or 0
        has_admin = bool(perms & 8)
        # Mark roles above Prowl's highest role as disabled (can't assign)
        is_above = bot_role_pos is not None and r.get("position", 0) > bot_role_pos
        r["id"] = str(r.get("id"))
        r["disabled"] = is_above
        r["is_mod"] = has_admin or (str(r.get("id")) in mod_role_ids)
        r["count"] = r.get("count") or r.get("member_count") or 0
        result.append(r)
    if not result:
        result = [
            {"id":"4001","name":"Admin","count":3,"is_mod":True,"disabled":True},
            {"id":"4002","name":"Moderator","count":8,"is_mod":True},
            {"id":"4005","name":"VIP","count":15,"is_mod":False},
        ]
    return {"roles": result}


@app.post("/api/v1/mod/{guild_id}/roles", dependencies=[Depends(require_mod)])
async def mod_roles_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    role_id = body.get("role_id")
    is_mod = body.get("is_mod", False)
    if not role_id:
        return JSONResponse({"error": "missing role_id"}, status_code=400)
    current = await _get_mod_settings(guild_id)
    mod_roles = current.get("mod_roles", [])
    if is_mod and role_id not in mod_roles:
        mod_roles.append(role_id)
    elif not is_mod and role_id in mod_roles:
        mod_roles.remove(role_id)
    current["mod_roles"] = mod_roles
    await execute(
        "INSERT INTO mod_settings (guild_id, settings, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(current), time.time(),
        json.dumps(current), time.time(),
    )
    return {"ok": True}


@app.post("/api/v1/mod/{guild_id}/roles/batch", dependencies=[Depends(require_mod)])
async def mod_roles_batch(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    role_ids = body.get("role_ids", [])
    current = await _get_mod_settings(guild_id)
    current["mod_roles"] = role_ids
    await execute(
        "INSERT INTO mod_settings (guild_id, settings, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(current), time.time(),
        json.dumps(current), time.time(),
    )
    logger.info(f"Mod roles saved for guild {guild_id}: {role_ids}")
    return {"ok": True, "mod_roles": role_ids}


@app.post("/api/v1/mod/{guild_id}/emergency", dependencies=[Depends(require_mod)])
async def mod_emergency(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    locked = body.get("locked", False)
    # Read current settings and merge to preserve other keys
    current = await _get_mod_settings(guild_id)
    current["emergency_lock"] = locked
    await execute(
        "INSERT INTO mod_settings (guild_id, settings, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(current), time.time(),
        json.dumps(current), time.time(),
    )
    # Queue the actual lockdown/restore for the bot to execute
    session_user = request.session.get("user") or {}
    moderator = session_user.get("username", "Unknown")
    action = "emergency_lock" if locked else "emergency_unlock"
    request_id = await _queue_action(guild_id, action, "", "", "Emergency lockdown" if locked else "Emergency unlock", None, moderator)
    ok, message = await _call_bot_direct(
        guild_id, action, "0", "", "Emergency lockdown" if locked else "Emergency unlock", None, moderator,
        request_id=request_id,
    )
    if ok:
        return {"ok": True, "direct": True, "request_id": request_id}
    return {"ok": True, "queued": True, "direct": False, "fallback": message, "request_id": request_id}


@app.post("/api/v1/mod/{guild_id}/purge", dependencies=[Depends(require_mod)])
async def mod_purge(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    channel_id = body.get("channel_id")
    count = int(body.get("count", 10))
    if not channel_id:
        return JSONResponse({"error": "missing channel_id"}, status_code=400)
    if count < 1 or count > 100:
        return JSONResponse({"error": "count must be 1-100"}, status_code=400)
    session_user = request.session.get("user") or {}
    moderator = session_user.get("username", "Unknown")
    request_id = await _queue_action(guild_id, "purge", channel_id, "", f"Purge {count} messages", count, moderator)
    ok, message = await _call_bot_direct(
        guild_id, "purge", channel_id, "", f"Purge {count} messages", count, moderator,
        request_id=request_id,
    )
    if ok:
        return {"ok": True, "direct": True, "request_id": request_id, "purged": count}
    return {"ok": True, "queued": True, "direct": False, "fallback": message, "request_id": request_id, "purged": count}


@app.get("/api/v1/mod/{guild_id}/stats/members")
async def mod_member_stats(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        """WITH recent AS (
             SELECT timestamp, member_count FROM member_history
             WHERE guild_id = ? ORDER BY timestamp DESC LIMIT 168
           ) SELECT timestamp, member_count FROM recent ORDER BY timestamp ASC""",
        str(guild_id),
    )
    return {"points": [{"t": r["timestamp"], "v": r["member_count"]} for r in rows]}


@app.get("/api/v1/mod/{guild_id}/stats/messages")
async def mod_message_stats(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        """WITH recent AS (
             SELECT timestamp, message_count FROM message_history
             WHERE guild_id = ? ORDER BY timestamp DESC LIMIT 168
           ) SELECT timestamp, message_count FROM recent ORDER BY timestamp ASC""",
        str(guild_id),
    )
    return {"points": [{"t": r["timestamp"], "v": r["message_count"]} for r in rows]}


_STATS_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS guild_stats_history (
    guild_id        TEXT NOT NULL,
    day             TEXT NOT NULL,
    member_count    INTEGER NOT NULL DEFAULT 0,
    channel_count   INTEGER NOT NULL DEFAULT 0,
    role_count      INTEGER NOT NULL DEFAULT 0,
    category_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day)
);
"""


@app.get("/api/v1/mod/{guild_id}/stats/daily")
async def mod_stats_daily(guild_id: str, request: Request):
    """Daily snapshot of the 4 server stats + day-over-day % change."""
    await require_guild_access(request, guild_id)
    from datetime import date, timedelta
    try:
        await execute(_STATS_HISTORY_SQL)
    except Exception:
        pass
    d = await get_guild_data(guild_id)
    d = d or {}
    members = int(d.get("member_count", 0) or 0)
    channels = sum(1 for c in (d.get("channels") or []) if c.get("type", 0) == 0)
    categories = sum(1 for c in (d.get("channels") or []) if c.get("type", 0) == 4)
    roles = len(d.get("roles", []) or [])
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        await execute(
            "INSERT INTO guild_stats_history (guild_id, day, member_count, channel_count, role_count, category_count) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, day) DO UPDATE SET "
            "member_count = excluded.member_count, channel_count = excluded.channel_count, "
            "role_count = excluded.role_count, category_count = excluded.category_count",
            str(guild_id), today, members, channels, roles, categories,
        )
    except Exception:
        pass
    prev = None
    try:
        yrow = await fetchrow(
            "SELECT member_count, channel_count, role_count, category_count "
            "FROM guild_stats_history WHERE guild_id = ? AND day = ?",
            str(guild_id), yesterday,
        )
        if yrow:
            prev = {
                "member_count": int(yrow["member_count"] or 0),
                "channel_count": int(yrow["channel_count"] or 0),
                "role_count": int(yrow["role_count"] or 0),
                "category_count": int(yrow["category_count"] or 0),
            }
    except Exception:
        pass

    def pct(cur, p):
        if p is None or p <= 0:
            return None
        return round((cur - p) / p * 100, 1)

    return {
        "today": {"members": members, "channels": channels, "roles": roles, "categories": categories},
        "yesterday": prev,
        "pct": {
            "members": pct(members, prev["member_count"] if prev else None),
            "channels": pct(channels, prev["channel_count"] if prev else None),
            "roles": pct(roles, prev["role_count"] if prev else None),
            "categories": pct(categories, prev["category_count"] if prev else None),
        },
    }


@app.get("/api/v1/mod/{guild_id}/health")
async def server_health(guild_id: str, request: Request):
    """Server Health Score based on activity trend, mod response time, member retention, message quality."""
    await require_guild_access(request, guild_id)
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    today = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).date().isoformat()
    two_weeks_ago = (now - timedelta(days=14)).date().isoformat()

    # 1. Activity Trend: compare this week's avg messages vs last week's
    activity_score = 50
    try:
        rows = await query(
            """SELECT strftime('%Y-%m-%d', timestamp / 86400, 'unixepoch') as day,
                      SUM(message_count) as daily_messages
               FROM message_history
               WHERE guild_id = ? AND timestamp >= ? AND timestamp < ?
               GROUP BY day""",
            str(guild_id),
            int(datetime.strptime(week_ago, "%Y-%m-%d").timestamp()),
            int(now.timestamp()),
        )
        this_week_rows = await query(
            """SELECT strftime('%Y-%m-%d', timestamp / 86400, 'unixepoch') as day,
                      SUM(message_count) as daily_messages
               FROM message_history
               WHERE guild_id = ? AND timestamp >= ? AND timestamp < ?
               GROUP BY day""",
            str(guild_id),
            int(datetime.strptime(today, "%Y-%m-%d").timestamp()),
            int(now.timestamp()),
        )
        if this_week_rows and rows:
            cur_avg = sum(r["daily_messages"] for r in this_week_rows) / len(this_week_rows)
            prev_avg = sum(r["daily_messages"] for r in rows) / len(rows)
            if prev_avg > 0:
                ratio = cur_avg / prev_avg
                if ratio >= 0.9: activity_score = 100
                elif ratio >= 0.7: activity_score = round(70 + (ratio - 0.7) / 0.2 * 30)
                elif ratio >= 0.4: activity_score = round(40 + (ratio - 0.4) / 0.3 * 30)
                else: activity_score = round(10 + ratio / 0.4 * 30)
    except Exception:
        pass

    # 2. Mod Response Time: avg time between consecutive mod_log entries (lower = better)
    mod_score = 50
    try:
        rows = await query(
            """SELECT created_at FROM mod_log
               WHERE guild_id = ? AND created_at >= ? AND created_at <= ?
               ORDER BY created_at DESC LIMIT 50""",
            str(guild_id),
            int((now - timedelta(days=7)).timestamp()),
            int(now.timestamp()),
        )
        if len(rows) >= 2:
            timestamps = sorted([r["created_at"] for r in rows])
            gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
            avg_gap = sum(gaps) / len(gaps) / 3600  # hours
            if avg_gap <= 1: mod_score = 100
            elif avg_gap <= 4: mod_score = round(80 + (4 - avg_gap) / 3 * 20)
            elif avg_gap <= 24: mod_score = round(50 + (24 - avg_gap) / 20 * 30)
            elif avg_gap <= 72: mod_score = round(30 + (72 - avg_gap) / 48 * 20)
            else: mod_score = round(10 + max(0, (72 - avg_gap) / 72 * 10))
        elif len(rows) == 1:
            mod_score = 50
    except Exception:
        pass

    # 3. Member Retention: compare current member count vs 7 days ago
    retention_score = 50
    try:
        cur_rows = await fetchrow(
            "SELECT member_count FROM guild_stats_history WHERE guild_id = ? AND day = ?",
            str(guild_id), today,
        )
        prev_rows = await fetchrow(
            "SELECT member_count FROM guild_stats_history WHERE guild_id = ? AND day = ?",
            str(guild_id), week_ago,
        )
        if cur_rows and prev_rows:
            cur = int(cur_rows["member_count"] or 0)
            prev = int(prev_rows["member_count"] or 0)
            if prev > 0:
                ratio = cur / prev
                if ratio >= 0.95: retention_score = 100
                elif ratio >= 0.85: retention_score = round(80 + (ratio - 0.85) / 0.10 * 20)
                elif ratio >= 0.70: retention_score = round(55 + (ratio - 0.70) / 0.15 * 25)
                elif ratio >= 0.50: retention_score = round(30 + (ratio - 0.50) / 0.20 * 25)
                else: retention_score = round(10 + ratio / 0.50 * 20)
    except Exception:
        pass

    # 4. Message Quality: unique active users ratio
    quality_score = 50
    try:
        rows = await query(
            """SELECT COUNT(DISTINCT user_id) as unique_users FROM leveling_data
               WHERE guild_id = ? AND messages > 0
               LIMIT 1""",
            str(guild_id),
        )
        total_rows = await fetchrow(
            "SELECT member_count FROM guild_stats_history WHERE guild_id = ? AND day = ?",
            str(guild_id), today,
        )
        total = int(total_rows["member_count"] or 0) if total_rows else 0
        if rows and total > 0:
            unique = int(rows[0]["unique_users"] or 0)
            ratio = unique / total
            if ratio >= 0.5: quality_score = 100
            elif ratio >= 0.3: quality_score = round(70 + (ratio - 0.3) / 0.2 * 30)
            elif ratio >= 0.15: quality_score = round(40 + (ratio - 0.15) / 0.15 * 30)
            elif ratio >= 0.05: quality_score = round(20 + (ratio - 0.05) / 0.10 * 20)
            else: quality_score = round(5 + ratio / 0.05 * 15)
    except Exception:
        pass

    total_score = round((activity_score + mod_score + retention_score + quality_score) / 4)
    if total_score >= 80: color = "#22C55E"
    elif total_score >= 60: color = "#F59E0B"
    elif total_score >= 40: color = "#F59E0B"
    else: color = "#EF4444"

    return {
        "score": total_score, "color": color,
        "factors": {
            "activity": activity_score,
            "mod_response": mod_score,
            "retention": retention_score,
            "message_quality": quality_score,
        },
    }


@app.get("/api/v1/mod/{guild_id}/actions")
async def mod_actions_list(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT id, action, target_id, target_name, reason, moderator, duration, status, error, created_at, processed_at, request_id "
        "FROM mod_actions WHERE guild_id = ? ORDER BY created_at DESC LIMIT 30",
        str(guild_id),
    )
    return {"actions": [dict(r) for r in rows]}


@app.get("/api/v1/mod/{guild_id}/action/{request_id}")
async def mod_action_status(guild_id: str, request_id: str, request: Request):
    """Query the status of a single action by request_id."""
    await require_guild_access(request, guild_id)
    row = await fetchrow(
        "SELECT id, action, target_id, target_name, reason, moderator, duration, status, error, created_at, processed_at, request_id "
        "FROM mod_actions WHERE request_id = ? AND guild_id = ?",
        request_id, str(guild_id),
    )
    if not row:
        return JSONResponse({"error": "Action not found"}, status_code=404)
    return dict(row)


@app.post("/api/v1/mod/{guild_id}/action", dependencies=[Depends(require_mod)])
async def mod_action(guild_id: str, request: Request):
    """Execute a moderation action. Persists to DB first for lifecycle tracking,
    then tries the bot's direct HTTP bridge (instant). Falls back to the DB
    queue poll if the bridge is down."""
    await require_guild_access(request, guild_id)
    body = await request.json()
    action = body.get("action")
    user_id = body.get("user_id")
    reason = body.get("reason", "")
    duration = body.get("duration")
    user_name = body.get("user_name", "")
    if action not in ("mute", "unmute", "kick", "ban", "add_role", "remove_role", "nickname"):
        return JSONResponse({"error": "Invalid action"}, status_code=400)
    session_user = request.session.get("user") or {}
    moderator = session_user.get("username", "Unknown")
    # For role/nickname actions, target_name carries the role ID or new nickname
    target_name = body.get("target") if action in ("add_role", "remove_role", "nickname") else user_name

    # Always persist first so we have a request_id for lifecycle tracking
    request_id = await _queue_action(guild_id, action, user_id, target_name, reason, duration, moderator)

    # Try the direct bridge (instant execution)
    ok, message = await _call_bot_direct(
        guild_id, action, user_id, user_name, reason, duration, moderator, body.get("target"),
        request_id=request_id,
    )
    if ok:
        return {"ok": True, "direct": True, "request_id": request_id}

    # Bridge unavailable - action is already in the DB queue, bot will pick it up
    return {"ok": True, "queued": True, "direct": False, "fallback": message, "request_id": request_id}


# ---------------------------------------------------------------------------
#  Personal Reminders & To-Do (per-user)
# ---------------------------------------------------------------------------

_REL_RE = re.compile(
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)",
    re.I,
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_clock(text):
    """Parse '9', '9am', '9:30', '9:30pm' into (hour, minute) or None."""
    text = (text or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    h = int(m.group(1)); mi = int(m.group(2) or 0); ap = (m.group(3) or "")
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return (h, mi)


def _parse_when(text, now=None):
    """Return (epoch_seconds, error_or_None). Mirrors the bot's parser."""
    now = now or datetime.datetime.now()
    s = (text or "").strip().lower()
    if _REL_RE.search(s):
        total = 0; matched = False
        for num, unit in _REL_RE.findall(s):
            u = unit.lower()[0]
            if u not in "smhd":
                continue
            total += int(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
            matched = True
        if matched and total > 0:
            return (now + datetime.timedelta(seconds=total)).timestamp(), None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return t.timestamp(), None
    if s.startswith("tomorrow"):
        rest = s[len("tomorrow"):].strip()
        base = now + datetime.timedelta(days=1)
        if rest:
            c = _parse_clock(rest)
            if not c:
                return None, "I couldn't parse the time after 'tomorrow'."
            base = base.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
        else:
            base = base.replace(hour=9, minute=0, second=0, microsecond=0)
        return base.timestamp(), None
    for i, wd in enumerate(_WEEKDAYS):
        if s.startswith(wd[:3]) or s.startswith(wd):
            rest = s[len(wd):].strip()
            days = (i - now.weekday()) % 7
            if days == 0:
                days = 7
            base = now + datetime.timedelta(days=days)
            if rest:
                c = _parse_clock(rest)
                if c:
                    base = base.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
            else:
                base = base.replace(hour=9, minute=0, second=0, microsecond=0)
            return base.timestamp(), None
    return None, "I couldn't understand that time. Try something like '30m', '2h', 'tomorrow 9am', or 'fri 18:00'."


_reminders_ensured = False


async def _ensure_reminders_tables():
    global _reminders_ensured
    if _reminders_ensured:
        return
    try:
        await execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, guild_id TEXT, "
            "channel_id TEXT, message TEXT DEFAULT '', remind_at REAL NOT NULL, "
            "created_at REAL, done INTEGER DEFAULT 0)"
        )
        await execute(
            "CREATE TABLE IF NOT EXISTS todos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, task TEXT NOT NULL, "
            "created_at REAL, done INTEGER DEFAULT 0, done_at REAL)"
        )
        _reminders_ensured = True
    except Exception as e:
        logger.error("ensure_reminders_tables failed: %s", e)


def _fmt_when(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")


@app.get("/api/v1/reminders/{guild_id}/reminders")
async def get_reminders(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    uid = str(user["id"])
    await _ensure_reminders_tables()
    rem = await query(
        "SELECT id, message, remind_at FROM reminders WHERE user_id = ? AND done = 0 ORDER BY remind_at ASC",
        uid,
    )
    todos = await query(
        "SELECT id, task, done FROM todos WHERE user_id = ? ORDER BY done ASC, id ASC",
        uid,
    )
    return {
        "reminders": [
            {"id": r["id"], "message": r["message"], "when": _fmt_when(r["remind_at"])} for r in rem
        ],
        "todos": [{"id": t["id"], "task": t["task"], "done": bool(t["done"])} for t in todos],
    }


@app.post("/api/v1/reminders/{guild_id}/reminders")
async def create_reminder(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    when = (body.get("when") or "").strip()
    what = (body.get("what") or "").strip()
    if not what:
        return JSONResponse({"error": "Add a message for your reminder."}, status_code=400)
    ts, err = _parse_when(when)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if ts <= time.time():
        return JSONResponse({"error": "That time is in the past."}, status_code=400)
    await _ensure_reminders_tables()
    await execute(
        "INSERT INTO reminders (user_id, guild_id, channel_id, message, remind_at, created_at, done) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        str(user["id"]), str(guild_id), None, what[:900], ts, time.time(),
    )
    return {"ok": True}


@app.delete("/api/v1/reminders/{guild_id}/reminders/{rid}")
async def delete_reminder(request: Request, guild_id: str, rid: str):
    user = await require_guild_access(request, guild_id)
    try:
        rid_i = int(rid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid reminder id"}, status_code=404)
    await execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", rid_i, str(user["id"]))
    return {"ok": True}


@app.post("/api/v1/reminders/{guild_id}/todos")
async def create_todo(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"error": "Add a task first."}, status_code=400)
    await _ensure_reminders_tables()
    await execute(
        "INSERT INTO todos (user_id, task, created_at, done) VALUES (?, ?, ?, 0)",
        str(user["id"]), task[:900], time.time(),
    )
    return {"ok": True}


@app.post("/api/v1/reminders/{guild_id}/todos/{tid}/done")
async def done_todo(request: Request, guild_id: str, tid: str):
    user = await require_guild_access(request, guild_id)
    try:
        tid_i = int(tid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid todo id"}, status_code=404)
    await execute(
        "UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND user_id = ?",
        time.time(), tid_i, str(user["id"]),
    )
    return {"ok": True}


@app.delete("/api/v1/reminders/{guild_id}/todos")
async def clear_todos(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    done_only = (request.query_params.get("done_only") == "1")
    await _ensure_reminders_tables()
    if done_only:
        await execute("DELETE FROM todos WHERE user_id = ? AND done = 1", str(user["id"]))
    else:
        await execute("DELETE FROM todos WHERE user_id = ?", str(user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------------------
#  User Reminders & To-Do (standalone, no guild required)
# ---------------------------------------------------------------------------

@app.get("/api/v1/user/reminders")
async def user_get_reminders(request: Request):
    user = await require_auth(request)
    uid = str(user["id"])
    await _ensure_reminders_tables()
    rem = await query(
        "SELECT id, message, remind_at FROM reminders WHERE user_id = ? AND done = 0 ORDER BY remind_at ASC",
        uid,
    )
    todos = await query(
        "SELECT id, task, done FROM todos WHERE user_id = ? ORDER BY done ASC, id ASC",
        uid,
    )
    return {
        "reminders": [
            {"id": r["id"], "message": r["message"], "when": _fmt_when(r["remind_at"])} for r in rem
        ],
        "todos": [{"id": t["id"], "task": t["task"], "done": bool(t["done"])} for t in todos],
    }


@app.post("/api/v1/user/reminders")
async def user_create_reminder(request: Request):
    user = await require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    when = (body.get("when") or "").strip()
    what = (body.get("what") or "").strip()
    if not what:
        return JSONResponse({"error": "Add a message for your reminder."}, status_code=400)
    ts, err = _parse_when(when)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if ts <= time.time():
        return JSONResponse({"error": "That time is in the past."}, status_code=400)
    await _ensure_reminders_tables()
    await execute(
        "INSERT INTO reminders (user_id, guild_id, channel_id, message, remind_at, created_at, done) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        str(user["id"]), None, None, what[:900], ts, time.time(),
    )
    return {"ok": True}


@app.delete("/api/v1/user/reminders/{rid}")
async def user_delete_reminder(request: Request, rid: str):
    user = await require_auth(request)
    try:
        rid_i = int(rid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid reminder id"}, status_code=404)
    await execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", rid_i, str(user["id"]))
    return {"ok": True}


@app.post("/api/v1/user/todos")
async def user_create_todo(request: Request):
    user = await require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"error": "Add a task first."}, status_code=400)
    await _ensure_reminders_tables()
    await execute(
        "INSERT INTO todos (user_id, task, created_at, done) VALUES (?, ?, ?, 0)",
        str(user["id"]), task[:900], time.time(),
    )
    return {"ok": True}


@app.post("/api/v1/user/todos/{tid}/done")
async def user_done_todo(request: Request, tid: str):
    user = await require_auth(request)
    try:
        tid_i = int(tid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid todo id"}, status_code=404)
    await execute(
        "UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND user_id = ?",
        time.time(), tid_i, str(user["id"]),
    )
    return {"ok": True}


@app.delete("/api/v1/user/todos")
async def user_clear_todos(request: Request):
    user = await require_auth(request)
    done_only = (request.query_params.get("done_only") == "1")
    await _ensure_reminders_tables()
    if done_only:
        await execute("DELETE FROM todos WHERE user_id = ? AND done = 1", str(user["id"]))
    else:
        await execute("DELETE FROM todos WHERE user_id = ?", str(user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Personal AFK status (per-user, per-guild)
# ---------------------------------------------------------------------------

AFK_DEFAULTS = {
    "enabled": True,
    "afk_message_type": "basic",
    "afk_emoji": "\U0001F634",
    "afk_message": "{mention} is currently AFK: {reason}",
    "afk_embed": {},
}
_afk_ensured = False


async def _ensure_afk_tables():
    global _afk_ensured
    if _afk_ensured:
        return
    try:
        await execute(
            "CREATE TABLE IF NOT EXISTS afk_status ("
            "guild_id TEXT NOT NULL, user_id TEXT NOT NULL, reason TEXT DEFAULT '', "
            "nickname TEXT DEFAULT '', since REAL NOT NULL, PRIMARY KEY (guild_id, user_id))"
        )
        await execute(
            "CREATE TABLE IF NOT EXISTS afk_settings ("
            "guild_id TEXT PRIMARY KEY, settings TEXT NOT NULL DEFAULT '{}', updated_at REAL)"
        )
        _afk_ensured = True
    except Exception as e:
        logger.error("ensure_afk_tables failed: %s", e)


async def _get_afk_settings(guild_id: str):
    try:
        row = await fetchrow("SELECT settings FROM afk_settings WHERE guild_id = ?", str(guild_id))
    except Exception:
        row = None
    if not row:
        return dict(AFK_DEFAULTS)
    try:
        return {**AFK_DEFAULTS, **json.loads(row["settings"])}
    except Exception:
        return dict(AFK_DEFAULTS)


@app.get("/api/v1/afk/{guild_id}")
async def get_afk_status(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    uid = str(user["id"])
    await _ensure_afk_tables()
    settings = await _get_afk_settings(guild_id)
    row = await fetchrow(
        "SELECT reason, nickname, since FROM afk_status WHERE guild_id = ? AND user_id = ?",
        str(guild_id), uid,
    )
    me = None
    if row:
        me = {"afk": True, "reason": row["reason"] or "", "since": row["since"]}
    is_mod = await is_guild_moderator(request, guild_id)
    return {"enabled": settings.get("enabled", True), "me": me, "settings": settings, "is_mod": is_mod}


@app.post("/api/v1/afk/{guild_id}")
async def set_afk_status(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    reason = (body.get("reason") or "").strip()
    await _ensure_afk_tables()
    if not reason:
        await execute("DELETE FROM afk_status WHERE guild_id = ? AND user_id = ?", str(guild_id), str(user["id"]))
    else:
        name = (user.get("global_name") or user.get("username") or "")[:80]
        await execute(
            "INSERT INTO afk_status (guild_id, user_id, reason, nickname, since) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET reason = ?, nickname = ?, since = ?",
            str(guild_id), str(user["id"]), reason[:500], name, time.time(),
            reason[:500], name, time.time(),
        )
    return {"ok": True}


@app.delete("/api/v1/afk/{guild_id}")
async def clear_afk_status(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    await _ensure_afk_tables()
    await execute("DELETE FROM afk_status WHERE guild_id = ? AND user_id = ?", str(guild_id), str(user["id"]))
    return {"ok": True}


@app.post("/api/v1/afk/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def update_afk_settings(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    settings = await _get_afk_settings(guild_id)
    if "key" in body:
        settings[body["key"]] = body.get("value")
    else:
        for k, v in body.items():
            if k == "enabled" and not isinstance(v, bool):
                continue
            settings[k] = v
    await _ensure_afk_tables()
    await execute(
        "INSERT INTO afk_settings (guild_id, settings, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(settings), time.time(), json.dumps(settings), time.time(),
    )
    return {"ok": True, "settings": settings}


# ---------------------------------------------------------------------------
#  Giveaways (shared DB; bot loop posts / ends / rerolls)
# ---------------------------------------------------------------------------

_gw_ensured = False


async def _ensure_giveaway_tables():
    global _gw_ensured
    if _gw_ensured:
        return
    try:
        await execute(
            "CREATE TABLE IF NOT EXISTS giveaways ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
            "message_id TEXT DEFAULT '', host_id TEXT DEFAULT '', prize TEXT NOT NULL, "
            "description TEXT DEFAULT '', thumbnail TEXT DEFAULT '', winners_count INTEGER DEFAULT 1, "
            "required_role_id TEXT DEFAULT '', end_ts REAL NOT NULL, start_ts REAL NOT NULL, "
            "status TEXT DEFAULT 'pending', winners TEXT DEFAULT '', reroll_pending INTEGER DEFAULT 0, "
            "created_at REAL)"
        )
        await execute(
            "CREATE TABLE IF NOT EXISTS giveaway_entries ("
            "giveaway_id INTEGER NOT NULL, user_id TEXT NOT NULL, joined_at REAL, "
            "PRIMARY KEY (giveaway_id, user_id))"
        )
        _gw_ensured = True
    except Exception as e:
        logger.error("ensure_giveaway_tables failed: %s", e)


def _parse_giveaway_end(text: str):
    text = (text or "").strip()
    if text.isdigit():
        return (datetime.datetime.now() + datetime.timedelta(minutes=int(text))).timestamp(), None
    return _parse_when(text)


@app.get("/api/v1/giveaways/{guild_id}")
async def list_giveaways_endpoint(request: Request, guild_id: str):
    await require_guild_access(request, guild_id)
    await _ensure_giveaway_tables()
    is_mod = await is_guild_moderator(request, guild_id)
    rows = await query(
        "SELECT * FROM giveaways WHERE guild_id = ? ORDER BY created_at DESC LIMIT 50", str(guild_id)
    )
    counts = {}
    try:
        cnt = await query(
            "SELECT giveaway_id, COUNT(*) AS c FROM giveaway_entries "
            "WHERE giveaway_id IN (SELECT id FROM giveaways WHERE guild_id = ?) GROUP BY giveaway_id",
            str(guild_id),
        )
        for r in cnt:
            counts[r["giveaway_id"]] = r["c"]
    except Exception:
        pass
    # Resolve winner names from cached guild data (best-effort).
    name_map = {}
    try:
        gd = await get_guild_data(guild_id)
        for m in (gd.get("members") or []):
            uid = str(m.get("user", {}).get("id") or m.get("id") or "")
            nm = (
                m.get("nick")
                or m.get("user", {}).get("global_name")
                or m.get("user", {}).get("username")
                or uid
            )
            if uid:
                name_map[uid] = nm
    except Exception:
        pass
    out = []
    for r in rows:
        winners = [w for w in (r["winners"] or "").split(",") if w]
        embed = r.get("embed") or ""
        if isinstance(embed, str) and embed:
            try:
                embed = json.loads(embed)
            except Exception:
                embed = {}
        if not isinstance(embed, dict):
            embed = {}
        out.append({
            "id": r["id"],
            "prize": r["prize"],
            "description": r["description"] or "",
            "winners_count": r["winners_count"],
            "required_role_id": r["required_role_id"] or "",
            "required_xp": r.get("required_xp") or 0,
            "required_level": r.get("required_level") or 0,
            "required_msgs": r.get("required_msgs") or 0,
            "message_type": r.get("message_type") or "",
            "emoji": r.get("emoji") or "",
            "message": r.get("message") or "",
            "embed": embed,
            "end_ts": r["end_ts"],
            "start_ts": r["start_ts"],
            "status": r["status"],
            "entries": counts.get(r["id"], 0),
            "winners": [{"id": w, "name": name_map.get(w, w)} for w in winners],
            "host_id": r["host_id"] or "",
            "channel_id": r["channel_id"],
        })
    return {"giveaways": out, "is_mod": is_mod}


@app.post("/api/v1/giveaways/{guild_id}")
async def create_giveaway_endpoint(request: Request, guild_id: str):
    user = await require_guild_access(request, guild_id)
    if not await is_guild_moderator(request, guild_id):
        return JSONResponse({"error": "Only moderators can create giveaways."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    prize = (body.get("prize") or "").strip()
    if not prize:
        return JSONResponse({"error": "Prize is required."}, status_code=400)
    duration = (body.get("duration") or "").strip()
    ts, err = _parse_giveaway_end(duration)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    if ts <= time.time():
        return JSONResponse({"error": "That end time is in the past."}, status_code=400)
    try:
        winners = int(body.get("winners", 1))
    except (TypeError, ValueError):
        winners = 1
    if winners < 1:
        winners = 1
    if winners > 20:
        winners = 20
    channel = (body.get("channel_id") or "").strip()
    if not channel:
        return JSONResponse({"error": "Choose a channel."}, status_code=400)
    desc = (body.get("description") or "").strip()
    thumbnail = (body.get("thumbnail") or "").strip()
    role = (body.get("required_role_id") or "").strip()
    try:
        required_xp = int(body.get("required_xp") or 0)
    except (TypeError, ValueError):
        required_xp = 0
    try:
        required_level = int(body.get("required_level") or 0)
    except (TypeError, ValueError):
        required_level = 0
    try:
        required_msgs = int(body.get("required_msgs") or 0)
    except (TypeError, ValueError):
        required_msgs = 0
    if required_xp < 0:
        required_xp = 0
    if required_level < 0:
        required_level = 0
    if required_msgs < 0:
        required_msgs = 0
    message_type = (body.get("message_type") or "").strip()
    if message_type not in ("basic", "embed"):
        message_type = ""
    emoji = (body.get("emoji") or "").strip()
    message = (body.get("message") or "").strip()
    embed = body.get("embed") or {}
    if not isinstance(embed, dict):
        embed = {}
    await _ensure_giveaway_tables()
    await execute(
        "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description, thumbnail, "
        "winners_count, required_role_id, end_ts, start_ts, status, created_at, "
        "required_xp, required_level, required_msgs, message_type, message, emoji, embed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        str(guild_id), str(channel), str(user["id"]), prize[:300], desc[:1000],
        (thumbnail or "")[:500], winners, str(role) if role else "", ts, time.time(), time.time(),
        required_xp, required_level, required_msgs, message_type[:20],
        message[:1000], emoji[:16], json.dumps(embed),
    )
    row = await fetchrow("SELECT id FROM giveaways WHERE guild_id = ? ORDER BY id DESC LIMIT 1", str(guild_id))
    return {"ok": True, "id": row["id"] if row else None}


@app.post("/api/v1/giveaways/{guild_id}/{gid}/end")
async def end_giveaway_endpoint(request: Request, guild_id: str, gid: str):
    await require_guild_access(request, guild_id)
    if not await is_guild_moderator(request, guild_id):
        return JSONResponse({"error": "Only moderators can end giveaways."}, status_code=403)
    try:
        gid_i = int(gid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid giveaway id"}, status_code=404)
    await _ensure_giveaway_tables()
    await execute(
        "UPDATE giveaways SET end_ts = ? WHERE id = ? AND guild_id = ? AND status = 'active'",
        time.time(), gid_i, str(guild_id),
    )
    return {"ok": True}


@app.post("/api/v1/giveaways/{guild_id}/{gid}/reroll")
async def reroll_giveaway_endpoint(request: Request, guild_id: str, gid: str):
    await require_guild_access(request, guild_id)
    if not await is_guild_moderator(request, guild_id):
        return JSONResponse({"error": "Only moderators can reroll giveaways."}, status_code=403)
    try:
        gid_i = int(gid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid giveaway id"}, status_code=404)
    await _ensure_giveaway_tables()
    await execute(
        "UPDATE giveaways SET reroll_pending = reroll_pending + 1 WHERE id = ? AND guild_id = ? AND status = 'ended'",
        gid_i, str(guild_id),
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Leveling API v1
# ---------------------------------------------------------------------------

LEVELING_DEFAULTS = {
    "enabled": True,
    "announce_channel_id": None,
    "xp_rate": 1.0,
    "xp_cooldown": 60,
    "random_xp": True,
    "xp_per_message_min": 15,
    "xp_per_message_max": 25,
    "role_xp_multipliers": {},
    "level_roles": {},
    "level_up_message": "{user} reached **level {level}**!",
    "level_up_message_mode": "basic", "level_up_embed": {},
    "rank_card": {},
}


async def _get_leveling_settings(guild_id: str):
    return await fetchrow_cached("leveling_settings", "SELECT settings FROM leveling_settings WHERE guild_id = ?", guild_id, LEVELING_DEFAULTS)


def _sanitize_role_multipliers(value):
    """Validate role_xp_multipliers: dict of {role_id(snowflake): float>=0}."""
    if not isinstance(value, dict):
        return None, "role_xp_multipliers must be an object"
    if len(value) > 50:
        return None, "too many role multipliers (max 50)"
    clean = {}
    for role_id, mult in value.items():
        if not _valid_snowflake(role_id):
            return None, f"role '{role_id}' is not a valid Discord ID"
        try:
            f = float(mult)
        except (TypeError, ValueError):
            return None, f"multiplier for role '{role_id}' must be a number"
        if f < 0:
            return None, f"multiplier for role '{role_id}' must be 0 or higher"
        clean[str(role_id)] = round(f, 2)
    return clean, None


def _sanitize_level_roles(value):
    """Validate level_roles: dict of {level(str/int): role_id(snowflake)}."""
    if not isinstance(value, dict):
        return None, "level_roles must be an object"
    if len(value) > 50:
        return None, "too many level roles (max 50)"
    clean = {}
    for level, role_id in value.items():
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            return None, f"level '{level}' must be an integer"
        if lvl < 1:
            return None, "levels must be 1 or higher"
        if not _valid_snowflake(role_id):
            return None, f"role for level {lvl} must be a valid Discord ID"
        clean[str(lvl)] = str(role_id)
    return clean, None


@app.get("/api/v1/leveling/{guild_id}/settings")
async def leveling_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_leveling_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/leveling/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def leveling_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "level_roles":
        clean, err = _sanitize_level_roles(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("leveling_settings", str(guild_id), key, clean, LEVELING_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    if key == "role_xp_multipliers":
        clean, err = _sanitize_role_multipliers(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("leveling_settings", str(guild_id), key, clean, LEVELING_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    if key == "level_up_embed":
        clean, err = _sanitize_panel_embed(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("leveling_settings", str(guild_id), key, clean, LEVELING_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    err = await _save_settings("leveling_settings", str(guild_id), key, value, LEVELING_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


_BACKGROUND_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"}


def _background_dir():
    """Resolve the bot's shared background image directory relative to this file.

    Returns None if the directory isn't present (e.g. on edge runtimes that don't
    ship the bot's static asset tree).
    """
    repo_root = Path(__file__).resolve().parents[1]
    candidate = repo_root / "cli" / "data" / "static" / "img"
    if candidate.is_dir():
        return candidate
    return None


@app.get("/api/v1/leveling/{guild_id}/backgrounds")
async def leveling_backgrounds(guild_id: str, request: Request):
    await require_auth(request)
    bg_dir = _background_dir()
    if bg_dir is None:
        return {"backgrounds": []}
    items = sorted(
        p.name for p in bg_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _BACKGROUND_EXTS
    )
    return {"backgrounds": items}


@app.get("/api/v1/leveling/{guild_id}/backgrounds/{filename:path}")
async def leveling_background_file(guild_id: str, filename: str, request: Request):
    await require_auth(request)
    bg_dir = _background_dir()
    if bg_dir is None:
        raise HTTPException(status_code=404, detail="backgrounds unavailable")
    # Prevent traversal outside the background directory.
    safe = (bg_dir / filename).resolve()
    try:
        safe.relative_to(bg_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not safe.is_file() or safe.suffix.lower() not in _BACKGROUND_EXTS:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(safe)


@app.get("/api/v1/leveling/{guild_id}/role-preview")
async def leveling_role_preview(guild_id: str, request: Request):
    """Return a sample avatar URL + display name for the editor preview."""
    await require_guild_access(request, guild_id)
    user = get_user(request)
    avatar = None
    name = "User"
    if user and isinstance(user, dict):
        avatar = user.get("avatar_url") or user.get("avatar")
        name = user.get("display_name") or user.get("name") or "User"
    if not avatar:
        avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
    return {"avatar_url": avatar, "display_name": name}
async def leveling_leaderboard(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT user_id, xp FROM leveling_data WHERE guild_id = ? ORDER BY xp DESC LIMIT 50",
        str(guild_id),
    )
    if not rows:
        return {"members": []}
    # Join with cached member data for names/avatars
    d = await get_guild_data(guild_id)
    by_id = {}
    if d and "members" in d:
        for m in d["members"]:
            av = m.get("avatar_url") or m.get("avatar")
            if av and not str(av).startswith("http"):
                av = f"https://cdn.discordapp.com/avatars/{m.get('id')}/{av}.png?size=64"
            by_id[str(m.get("id"))] = {
                "name": m.get("name", ""), "display_name": m.get("display_name", ""),
                "avatar_url": av or None,
            }

    def xp_for_level(lvl: int) -> int:
        return 100 * lvl + 50 * (lvl - 1)

    def level_from_xp(xp: int) -> int:
        lvl = 1
        while xp_for_level(lvl + 1) <= xp:
            lvl += 1
        return lvl

    members = []
    for r in rows:
        uid = str(r["user_id"])
        meta = by_id.get(uid, {})
        xp = int(r["xp"] or 0)
        level = level_from_xp(xp)
        members.append({
            "id": uid, "name": meta.get("name") or uid, "display_name": meta.get("display_name", ""),
            "avatar_url": meta.get("avatar_url"),
            "xp": xp, "level": level,
            "xp_next": xp_for_level(level + 1) - xp_for_level(level),
        })
    return {"members": members}


@app.get("/api/v1/leveling/{guild_id}/channels")
async def leveling_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": [{"id": "2001", "name": "general"}]}


@app.get("/api/v1/leveling/{guild_id}/roles")
async def leveling_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    roles = d.get("roles", []) if d else []
    result = []
    for r in roles:
        if str(r.get("id", "")) == str(guild_id):
            continue
        tags = r.get("tags")
        if isinstance(tags, dict) and tags.get("bot_id"):
            continue
        if r.get("managed", False):
            continue
        result.append({"id": str(r.get("id")), "name": r.get("name", ""), "color": r.get("color", 0), "position": r.get("position", 0), "count": r.get("count") or r.get("member_count") or 0})
    result.sort(key=lambda x: x["position"], reverse=True)
    if not result:
        result = [
            {"id": "4005", "name": "VIP", "color": 0, "position": 1, "count": 15},
            {"id": "4006", "name": "Member", "color": 0, "position": 0, "count": 120},
        ]
    return {"roles": result}


# ---------------------------------------------------------------------------
#  Logging API v1
# ---------------------------------------------------------------------------

LOGGING_DEFAULTS = {
    "message_delete_channel": None,
    "message_edit_channel": None,
    "member_join_channel": None,
    "member_leave_channel": None,
    "member_ban_channel": None,
    "member_unban_channel": None,
    "nickname_channel": None,
    "member_roles_channel": None,
    "member_mute_channel": None,
    "channel_create_channel": None,
    "channel_delete_channel": None,
    "channel_update_channel": None,
    "role_create_channel": None,
    "role_delete_channel": None,
    "role_update_channel": None,
    "server_update_channel": None,
    "emoji_update_channel": None,
    "invite_create_channel": None,
    "voice_channel": None,
}


async def _get_logging_settings(guild_id: str):
    return await fetchrow_cached("logging_settings", "SELECT settings FROM logging_settings WHERE guild_id = ?", guild_id, LOGGING_DEFAULTS)


@app.get("/api/v1/logging/{guild_id}/settings")
async def logging_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_logging_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/logging/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def logging_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    err = await _save_settings("logging_settings", str(guild_id), key, value, LOGGING_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/logging/{guild_id}/channels")
async def logging_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": [{"id": "2001", "name": "general"}]}


# ---------------------------------------------------------------------------
#  AutoMod API v1
# ---------------------------------------------------------------------------

AUTOMOD_DEFAULTS = {
    "enabled": False,
    "moderation_channel_id": None,
    "filter_mute_minutes": 60,
    "profanity_enabled": True,
    "profanity_action": "delete",
    "profanity_words": "",
    "spam_enabled": True,
    "spam_action": "delete",
    "spam_messages": 5,
    "spam_window": 5,
    "links_enabled": False,
    "links_action": "delete",
    "links_allowlist": "",
    "caps_enabled": False,
    "caps_action": "delete",
    "caps_percent": 70,
    "caps_min_chars": 6,
    "mentions_enabled": False,
    "mentions_action": "delete",
    "mentions_max": 5,
    "invites_enabled": False,
    "invites_action": "delete",
    "zalgo_enabled": False,
    "zalgo_action": "delete",
    "emoji_enabled": False,
    "emoji_action": "delete",
    "emoji_max": 10,
    "action_configs": {},
}

AUTOMOD_ACTIONS = ("delete", "delete_dm", "warn", "warn_dm", "mute", "mute_dm", "kick", "kick_dm", "ban", "ban_dm")
AUTOMOD_FILTERS = ("profanity", "spam", "links", "caps", "mentions", "invites", "zalgo", "emoji")


def _sanitize_action_configs(value):
    """Validate action_configs: per-filter {<base>_message, <base>_mode, <base>_embed, mute_minutes, ban_days}."""
    if not isinstance(value, dict):
        return None, "action_configs must be an object"
    clean = {}
    for fk, cfg in value.items():
        if fk not in AUTOMOD_FILTERS:
            continue
        if not isinstance(cfg, dict):
            continue
        c = {}
        for k, v in cfg.items():
            if k in ("delete_message", "warn_message", "mute_message", "kick_message", "ban_message") and isinstance(v, str) and v.strip():
                c[k] = v[:500]
            elif k in ("delete_mode", "warn_mode", "mute_mode", "kick_mode", "ban_mode") and v in ("basic", "custom"):
                c[k] = v
            elif k in ("delete_embed", "warn_embed", "mute_embed", "kick_embed", "ban_embed"):
                clean_embed, err = _sanitize_panel_embed(v)
                if not err:
                    c[k] = clean_embed
            elif k == "mute_minutes":
                try:
                    c[k] = max(1, int(v))
                except (TypeError, ValueError):
                    pass
            elif k == "ban_days":
                try:
                    c[k] = max(0, min(7, int(v)))
                except (TypeError, ValueError):
                    pass
        clean[fk] = c
    return clean, None


async def _get_automod_settings(guild_id: str):
    return await fetchrow_cached("automod_settings", "SELECT settings FROM automod_settings WHERE guild_id = ?", guild_id, AUTOMOD_DEFAULTS)


@app.get("/api/v1/automod/{guild_id}/settings")
async def automod_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_automod_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/automod/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def automod_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key.endswith("_action") and value not in AUTOMOD_ACTIONS:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    if key == "action_configs":
        clean, err = _sanitize_action_configs(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("automod_settings", str(guild_id), key, clean, AUTOMOD_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    err = await _save_settings("automod_settings", str(guild_id), key, value, AUTOMOD_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/automod/{guild_id}/channels")
async def automod_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": [{"id": "2001", "name": "general"}]}


# ---------------------------------------------------------------------------
#  Command Aliases API v1
# ---------------------------------------------------------------------------

ALIAS_DEFAULTS = {
    "enabled": False,
    "aliases": {},
}

ALIAS_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ALIAS_MAX = 50

# Keep in sync with the slash commands registered in cli/components/*.py
ALIAS_COMMAND_CATALOG = {
    "General": ["ping", "info", "say", "serverinfo", "userinfo", "avatar", "roleinfo", "channelinfo"],
    "Moderation": ["kick", "ban", "tempban", "unban", "mute", "unmute", "warn", "purge", "muteevasion", "settings", "lockdown"],
    "Leveling": ["level rank", "level leaderboard", "level toggle", "level setxp", "level reset", "level config", "level setrole"],
    "Music": ["music play", "music skip", "music stop", "music queue", "music volume", "music nowplaying",
              "music pause", "music resume", "music loop", "music shuffle", "music remove", "music clear"],
    "AI": ["ai chat", "ai clear", "ai imagine", "ai config", "ai model", "ai prompt"],
    "Members & Invites": ["members list", "members info", "members role", "members note", "members warnings",
                          "invites toggle", "invites channel", "invites stats", "invites user"],
}
ALIAS_VALID_TARGETS = {p for paths in ALIAS_COMMAND_CATALOG.values() for p in paths}


def _sanitize_aliases(value):
    """Validate aliases: {name: {target: command path}}."""
    if not isinstance(value, dict):
        return None, "aliases must be an object"
    clean = {}
    for name, entry in value.items():
        name = str(name).strip().lower()
        if not ALIAS_NAME_RE.match(name):
            return None, f"Invalid alias name: {name or '(empty)'}"
        if isinstance(entry, dict):
            target = str(entry.get("target", "")).strip()
        elif isinstance(entry, str):
            target = entry.strip()
        else:
            target = ""
        if target not in ALIAS_VALID_TARGETS:
            return None, f"Unknown command for /{name}: {target}"
        clean[name] = {"target": target}
    if len(clean) > ALIAS_MAX:
        return None, f"Too many aliases (max {ALIAS_MAX})"
    return clean, None


@app.get("/api/v1/aliases/catalog")
async def aliases_catalog(request: Request):
    await require_auth(request)
    return {"catalog": ALIAS_COMMAND_CATALOG}


@app.get("/api/v1/aliases/{guild_id}/settings")
async def alias_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    settings = await fetchrow_cached(
        "alias_settings",
        "SELECT settings FROM alias_settings WHERE guild_id = ?", guild_id, ALIAS_DEFAULTS,
    )
    return {"settings": settings}


@app.post("/api/v1/aliases/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def alias_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "aliases":
        clean, err = _sanitize_aliases(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("alias_settings", str(guild_id), key, clean, ALIAS_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    err = await _save_settings("alias_settings", str(guild_id), key, value, ALIAS_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Bot Profile API v1 (per-guild bot nickname/avatar/banner via HTTP bridge)
# ---------------------------------------------------------------------------

BOT_PROFILE_MAX_IMAGE_CHARS = 14_000_000  # base64 data URI cap (~10MB binary)


def _sanitize_bot_profile_payload(body: dict):
    """Validate a bot-profile update. Returns (payload, error)."""
    payload = {}
    if "nick" in body:
        nick = str(body.get("nick") or "").strip()
        if len(nick) > 32:
            return None, "Nickname must be 32 characters or fewer."
        payload["nick"] = nick
    if "bio" in body:
        bio = str(body.get("bio") or "").strip()
        if len(bio) > 350:
            return None, "Bio must be 350 characters or fewer."
        payload["bio"] = bio
    for key in ("avatar", "banner"):
        if body.get(f"reset_{key}"):
            payload[f"reset_{key}"] = True
            continue
        data = body.get(key)
        if data:
            data = str(data)
            if not data.startswith("data:image/"):
                return None, f"Invalid {key} image."
            if len(data) > BOT_PROFILE_MAX_IMAGE_CHARS:
                return None, f"{key.capitalize()} must be under 10MB."
            payload[key] = data
    if not payload:
        return None, "Nothing to update."
    return payload, None


@app.get("/api/v1/botprofile/{guild_id}")
async def bot_profile_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return JSONResponse({"error": "Bot bridge is not configured."}, status_code=503)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                BOT_SERVER_URL.rstrip("/") + "/api/profile",
                params={"guild_id": str(guild_id)},
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
    except Exception:
        return JSONResponse({"error": "Bot server unreachable."}, status_code=502)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = ""
        return JSONResponse({"error": detail or f"bot responded {r.status_code}"}, status_code=502)
    return r.json()


@app.post("/api/v1/botprofile/{guild_id}", dependencies=[Depends(require_mod)])
async def bot_profile_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return JSONResponse({"error": "Bot bridge is not configured."}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    payload, err = _sanitize_bot_profile_payload(body if isinstance(body, dict) else {})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    payload["guild_id"] = str(guild_id)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                BOT_SERVER_URL.rstrip("/") + "/api/profile",
                json=payload,
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
    except Exception:
        return JSONResponse({"error": "Bot server unreachable."}, status_code=502)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = ""
        return JSONResponse({"error": detail or f"bot responded {r.status_code}"}, status_code=502)
    return r.json()


# ---------------------------------------------------------------------------
#  Dashboard sidebar search (semantic via OpenAI embeddings, keyword fallback)
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBED_MODEL = "text-embedding-3-small"
SEARCH_RESULT_MIN = 0.18  # drop anything scoring below this

# Everything reachable from the sidebar. Descriptions are written so the
# embedding model can map natural queries ("stop people spamming") onto them.
# "blocks" are section titles inside each page - matching one deep-links the
# user straight to that box via /guild/<id>/<panel>#hl=<block>.
SEARCH_CATALOG = [
    {"panel": "overview", "icon": "layout-dashboard", "title": "Overview",
     "description": "Server statistics, recent activity feed and the quick setup checklist.",
     "keywords": "home dashboard stats overview activity setup",
     "blocks": ["Server Stats", "Feature Status", "Recent Activity", "Quick Links"]},
    {"panel": "ai", "icon": "sparkles", "title": "AI",
     "description": "AI chatbot, image generation, custom system prompt and model selection.",
     "keywords": "ai chat bot openai gpt image generate prompt model chatbot",
     "blocks": ["Behavior & Personality", "Generation Controls", "Model", "API Keys"]},
    {"panel": "moderation", "icon": "shield", "title": "Moderation",
     "description": "Ban, kick, temp-ban, mute, timeout, warn, purge messages, modlog, emergency lockdown, mute evasion and action DMs.",
     "keywords": "ban kick mute timeout warn purge modlog lockdown punish moderator",
     "blocks": ["Actions", "Custom Embed"]},
    {"panel": "members", "icon": "users", "title": "Users",
     "description": "Member list, role management, notes and warnings per user.",
     "keywords": "members users roles notes warnings list people",
     "blocks": ["Add Role", "Change Nickname", "Actions"]},
    {"panel": "welcomer", "icon": "door-open", "title": "Welcomer",
     "description": "Welcome messages, goodbye messages, auto role, auto nickname and welcome DMs for new members.",
     "keywords": "welcome goodbye greeting join leave auto role nickname dm greeter",
     "blocks": ["Welcome Channel", "Goodbye Channel", "Welcome Message", "Goodbye Message",
                "Welcome Image Card", "Goodbye Image Card", "Welcome DM", "Auto Roles", "Placeholders"]},
    {"panel": "verification", "icon": "shield-check", "title": "Verification",
     "description": "Verify button panel, captcha (reCAPTCHA / Turnstile), reaction verification and the verified role.",
     "keywords": "verify verification captcha recaptcha turnstile reaction verified role anti alt"},
    {"panel": "leveling", "icon": "trending-up", "title": "Leveling",
     "description": "XP system, rank cards, leaderboard, level roles and level-up announcements.",
     "keywords": "xp levels leveling rank leaderboard rewards voice text activity",
     "blocks": ["XP Settings", "Role XP Rates", "Level Roles", "Level-Up Message",
                "Level-Up Announcements", "Leaderboard"]},
    {"panel": "automation", "icon": "workflow", "title": "Automation",
     "description": "Visual automation graph connecting triggers to actions.",
     "keywords": "automation workflow triggers actions graph events"},
    {"panel": "autoresponder", "icon": "reply", "title": "Autoresponder",
     "description": "Automatic responses whenever a message matches a trigger word or phrase.",
     "keywords": "autoresponder auto response trigger words replies commands",
     "blocks": ["Triggers", "Add Trigger"]},
    {"panel": "global_chat", "icon": "globe", "title": "Global Chat",
     "description": "Link this server's channel with other servers into one shared global chat.",
     "keywords": "global chat link cross server network shared messaging"},
    {"panel": "aliases", "icon": "replace", "title": "Command Aliases",
     "description": "Custom alternative names for slash commands in this server.",
     "keywords": "alias aliases command rename shortcut custom names slash",
     "blocks": ["Aliases"]},
    {"panel": "social_alerts", "icon": "bell", "title": "Social Alerts",
     "description": "Notifications for YouTube uploads, Twitch streams going live and X/Twitter posts.",
     "keywords": "youtube twitch twitter x social alerts notifications posts uploads live stream"},
    {"panel": "tickets", "icon": "ticket", "title": "Tickets",
     "description": "Support ticket panels, ticket categories, claiming, closing and staff access.",
     "keywords": "tickets support help panel claim close category staff"},
    {"panel": "music", "icon": "music", "title": "Music",
     "description": "Play songs, queue management, skip, loop, shuffle and volume control.",
     "keywords": "music play song queue skip loop shuffle volume youtube spotify player",
     "blocks": ["Music Commands", "DJ Permissions", "Default Settings"]},
    {"panel": "logs", "icon": "scroll-text", "title": "Logs",
     "description": "Message edits/deletes, member joins/leaves, voice activity, channel and role changes logging.",
     "keywords": "logs logging audit message deleted edited joins leaves voice channels moderation trail",
     "blocks": ["Event Logs"]},
    {"panel": "automod", "icon": "bot", "title": "AutoMod",
     "description": "Anti-spam, invite filter, link filter, emoji spam, mention spam and banned words with automatic punishments.",
     "keywords": "automod auto filter spam links invites emoji mentions bad words swear censorship"},
    {"panel": "raid_protection", "icon": "shield-alert", "title": "Raid Protection",
     "description": "Detect join raids, block alt accounts, account-age gates and panic mode lockdown.",
     "keywords": "raid protection raids alts alt detection panic lockdown wave attack security",
     "blocks": ["Score Threshold", "Join Burst Detection", "Account Age Filter",
                "Default Avatar Recognition", "Moderation Channel", "Auto Recovery"]},
    {"panel": "bot_profile", "icon": "user-cog", "title": "Bot Profile",
     "description": "Per-server bot nickname, avatar, banner and bio - how Prowl looks in this server.",
     "keywords": "bot profile nickname avatar banner bio appearance name photo identity",
     "blocks": ["Preview"]},
    {"panel": "settings", "icon": "settings", "title": "Settings",
     "description": "General bot configuration for this server.",
     "keywords": "settings configuration options general preferences config",
     "blocks": ["Server Overview", "Bot Invite", "Danger Zone", "API Keys"]},
    {"panel": "afk", "icon": "moon", "title": "AFK",
     "description": "AFK status messages, auto-AFK after inactivity and custom away responses.",
     "keywords": "afk away idle inactive status absent offline"},
    {"panel": "giveaways", "icon": "gift", "title": "Giveaways",
     "description": "Create and manage giveaways with entry requirements, duration and winner count.",
     "keywords": "giveaway contest prize raffle win entry winner pick random draw"},
    {"panel": "birthday", "icon": "cake", "title": "Birthday",
     "description": "Birthday tracking, automatic role rewards and birthday announcements.",
     "keywords": "birthday birth date celebrate age cake role announcement auto"},
    {"panel": "activity_roles", "icon": "gamepad-2", "title": "Activity Roles",
     "description": "Automatic roles based on member activity like message count, voice time or reactions.",
     "keywords": "activity role automatic assign tracking message voice time react threshold"},
    {"panel": "badges", "icon": "trophy", "title": "Badges",
     "description": "Award custom badges to members for achievements, milestones or special recognition.",
     "keywords": "badges award achievement milestone trophy recognition special honor"},
    {"panel": "temp_channels", "icon": "audio-lines", "title": "Temp Channels",
     "description": "Temporary voice and text channels that auto-create and auto-delete.",
     "keywords": "temp temporary voice channel create delete auto jtc join to create"},
    {"panel": "invite_tracker", "icon": "link", "title": "Invite Tracker",
     "description": "Track who invited whom, invite leaderboards and invite rewards.",
     "keywords": "invite tracker invite code link leaderboard reward who invited join"},
    {"panel": "profile", "icon": "user", "title": "Profile",
     "description": "User profile customization, bio, banner and badge display.",
     "keywords": "profile user bio banner avatar customize display badge self"},
    {"panel": "reminders", "icon": "alarm-clock", "title": "Reminders",
     "description": "Set and manage recurring or one-time reminders for the server.",
     "keywords": "reminders reminder notify alert schedule repeat timer recurring"},
]

_search_state = {"vecs": None, "failed_at": 0.0}


def _search_doc(item):
    return f"{item['title']}: {item['description']} ({item['keywords']})"


def _search_tokens(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _keyword_score(q, item):
    """Cheap lexical score so exact wording always beats embeddings."""
    ql = q.lower().strip()
    title = item["title"].lower()
    doc = f"{title} {item['description'].lower()} {item['keywords'].lower()}"
    if ql == title:
        return 1.0
    if ql in title:
        return 0.95
    if ql in doc:
        return 0.8
    qt = _search_tokens(ql)
    dt = _search_tokens(doc)
    if not qt:
        return 0.0
    hits = sum(1 for t in qt if t in dt)
    prefix = any(any(w.startswith(t) for w in dt) for t in qt)
    return min(0.7, (hits / len(qt)) * 0.55 + (0.15 if prefix else 0.0))


def _block_match(q, item):
    """Return the page block (section) this query points at, if any."""
    ql = q.lower().strip()
    if len(ql) < 4:
        return None
    best = None
    for b in item.get("blocks", ()):
        bl = b.lower()
        if ql == bl:
            return b
        if (ql in bl or bl in ql) and (best is None or len(bl) < len(best.lower())):
            best = b
    return best


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


async def _openai_embed(texts):
    """Embed texts with OpenAI. Raises on failure; caller decides fallback."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        if r.status_code != 200:
            raise RuntimeError(f"embeddings api {r.status_code}")
        data = r.json()["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]


async def _catalog_vectors():
    """Lazily embed the catalog once per process; retry 5 min after a failure."""
    if _search_state["vecs"] is not None:
        return _search_state["vecs"]
    if time.time() - _search_state["failed_at"] < 300:
        return None
    try:
        vecs = await _openai_embed([_search_doc(i) for i in SEARCH_CATALOG])
        _search_state["vecs"] = vecs
        return vecs
    except Exception as e:
        logger.warning("Search: embedding catalog unavailable (%s) - keyword-only mode", e)
        _search_state["failed_at"] = time.time()
        return None


# Reused client so repeated semantic calls keep the TCP connection alive
# (avoids a fresh handshake to the bot server on every keystroke).
_semantic_client = None

async def _semantic_http():
    global _semantic_client
    if _semantic_client is None or _semantic_client.is_closed:
        _semantic_client = httpx.AsyncClient(timeout=4.0)
    return _semantic_client

async def _bot_semantic_scores(q: str):
    """Ask the HidenCloud BGE microservice for panel->score rankings.

    Returns a dict {panel: score} or None on any failure. Failure is silent:
    the caller drops back to keyword-only search (never raises).
    """
    if not SEMANTIC_API_URL or not SEMANTIC_API_KEY:
        return None
    try:
        client = await _semantic_http()
        r = await client.post(
            SEMANTIC_API_URL + "/semantic-search",
            json={"query": q},
            headers={"X-Prowl-Token": SEMANTIC_API_KEY},
            timeout=4.0,
        )
        if r.status_code != 200:
            logger.warning("Semantic API returned %s - keyword fallback", r.status_code)
            return None
        data = r.json()
        if not data.get("ok"):
            return None
        return {
            res["route"]: res["score"]
            for res in data.get("results", [])
            if res.get("route")
        }
    except Exception as e:
        logger.warning("Semantic API call failed (%s) - keyword fallback", e)
        return None


def _build_search_items(q, gid, score_map):
    """Build, sort and limit search result items from a {panel: (score, block)} map."""
    items = []
    for item in SEARCH_CATALOG:
        res = score_map.get(item["panel"])
        if not res:
            continue
        score, block = res
        if score < SEARCH_RESULT_MIN:
            continue
        href = f"/guild/{gid}/{item['panel']}" if gid else "/servers"
        if block and gid:
            href += "#hl=" + urllib.parse.quote(block)
        items.append({
            "panel": item["panel"],
            "icon": item["icon"],
            "title": item["title"],
            "description": item["description"],
            "href": href,
            "block": block,
            "_score": round(score, 4),
        })
    items.sort(key=lambda r: r["_score"], reverse=True)

    # Smart filtering: if the top result is significantly better than the rest,
    # only return it. Otherwise return up to 4 results.
    MAX_RESULTS = 4
    SCORE_GAP_THRESHOLD = 0.15  # if top score is this much higher than #2, drop #2+

    if len(items) > 1:
        top_score = items[0]["_score"]
        second_score = items[1]["_score"]
        if (top_score - second_score) >= SCORE_GAP_THRESHOLD:
            filtered = [items[0]]
        else:
            filtered = items[:MAX_RESULTS]
    else:
        filtered = items[:MAX_RESULTS]

    for it in filtered:
        it.pop("_score", None)
    return filtered


@app.get("/api/v1/dashboard/search")
async def dashboard_search(request: Request, q: str = "", guild_id: str = "", phase: str = ""):
    await require_auth(request)
    q = (q or "").strip()
    if len(q) < 2:
        return {"items": [], "mode": "none"}

    gid = guild_id if guild_id.isdigit() else ""

    if phase != "semantic":
        # Keyword-first pass (local + fast).
        kw_map = {}
        best_kw = 0.0
        has_block = False
        for item in SEARCH_CATALOG:
            block = _block_match(q, item)
            kw = _keyword_score(q, item)
            score = max(kw, 0.9) if block else kw
            if block:
                has_block = True
            best_kw = max(best_kw, score)
            kw_map[item["panel"]] = (score, block)
        kw_items = _build_search_items(q, gid, kw_map)
        semantic_available = bool(SEMANTIC_SEARCH_ENABLED and SEMANTIC_API_URL and SEMANTIC_API_KEY)
        # A *strong* keyword hit (clear title/section match) is returned
        # instantly. A weak single-token overlap should still defer to the
        # semantic service so natural-language queries get proper AI ranking.
        # If no semantic service is configured we keep the weak keyword match.
        strong = kw_items and (has_block or best_kw >= 0.5)
        if strong or not semantic_available:
            return {"items": kw_items, "mode": "keyword", "semantic": False}
        # Weak keyword hit but semantic is available: let the client request
        # the semantic phase. Include the weak items as a fallback.
        return {"items": kw_items, "mode": "keyword", "semantic": False, "need_semantic": True}

    # phase == "semantic": run the (slower) semantic ranking as a fallback.
    sem_map = {}
    mode = "keyword"
    if SEMANTIC_SEARCH_ENABLED:
        sem_scores = await _bot_semantic_scores(q) or {}
        mode = "semantic" if sem_scores else "keyword"
        for item in SEARCH_CATALOG:
            block = _block_match(q, item)
            sem = sem_scores.get(item["panel"], 0.0)
            score = max(sem, 0.9) if block else sem
            sem_map[item["panel"]] = (score, block)
    else:
        # Legacy OpenAI embedding path (kept as a fallback when BGE is off).
        cat_vecs = await _catalog_vectors()
        q_vec = None
        if cat_vecs:
            try:
                q_vec = (await _openai_embed([q]))[0]
            except Exception:
                q_vec = None
        if cat_vecs and q_vec:
            mode = "semantic"
            for i, item in enumerate(SEARCH_CATALOG):
                block = _block_match(q, item)
                sem = _cosine(q_vec, cat_vecs[i])
                score = max(sem, 0.9) if block else sem
                sem_map[item["panel"]] = (score, block)
    sem_items = _build_search_items(q, gid, sem_map)
    return {"items": sem_items, "mode": mode, "semantic": True}


# ---------------------------------------------------------------------------
#  Raid Protection API v1
# ---------------------------------------------------------------------------

RAID_DEFAULTS = {
    "enabled": False,
    "mode": "switches",
    # switches mode
    "join_threshold": 5,
    "join_window": 10,
    "join_action": "kick",
    "account_age_min": 0,
    "account_age_action": "kick",
    "default_avatar_enabled": False,
    "default_avatar_action": "kick",
    # score mode
    "score_threshold": 3,
    "score_action": "kick",
    "score_window": 10,
    "score_default_avatar": 2,
    "score_new_account_min": 10,
    "score_new_account": 2,
    "score_join_burst": 1,
    # shared
    "auto_recovery": True,
    "recovery_minutes": 30,
    "moderation_channel_id": None,
}

RAID_ACTIONS = ("kick", "ban", "lockdown", "verify")


async def _get_raid_settings(guild_id: str):
    return await fetchrow_cached("raid_settings", "SELECT settings FROM raid_settings WHERE guild_id = ?", guild_id, RAID_DEFAULTS)


@app.get("/api/v1/raid/{guild_id}/settings")
async def raid_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_raid_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/raid/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def raid_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "mode" and value not in ("switches", "score"):
        return JSONResponse({"error": "invalid mode"}, status_code=400)
    if key.endswith("_action") and value not in RAID_ACTIONS:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    err = await _save_settings("raid_settings", str(guild_id), key, value, RAID_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/raid/{guild_id}/channels")
async def raid_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": [{"id": "2001", "name": "general"}]}


# ---------------------------------------------------------------------------
#  Welcomer API v1
# ---------------------------------------------------------------------------

WELCOME_DEFAULTS = {
    "enabled": False,
    "channel_id": None,
    "goodbye_channel_id": None,
    "welcome_message": "Welcome {member} to {server}!",
    "welcome_mode": "default",
    "welcome_embed_data": {},
    "welcome_image_config": None,
    "goodbye_message": "{member} has left {server}.",
    "goodbye_mode": "default",
    "goodbye_embed_data": {},
    "goodbye_image_config": None,
    "welcome_dm": False,
    "welcome_dm_message": "Welcome to **{server}**! Make sure to read the rules.",
    "auto_role_ids": [],
    "bot_auto_role": None,
    "auto_nickname": None,
}


async def _get_welcome_settings(guild_id: str):
    return await fetchrow_cached("welcome_settings", "SELECT settings FROM welcome_settings WHERE guild_id = ?", guild_id, WELCOME_DEFAULTS)


@app.get("/api/v1/welcomer/{guild_id}/settings")
async def welcomer_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_welcome_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/welcomer/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def welcomer_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "auto_role_ids":
        if not isinstance(value, list):
            return JSONResponse({"error": "auto_role_ids must be a list"}, status_code=400)
        clean = []
        for rid in value:
            if not _valid_snowflake(rid):
                return JSONResponse({"error": f"role '{rid}' is not a valid Discord ID"}, status_code=400)
            clean.append(str(rid))
        value = clean
    elif key in ("welcome_embed_data", "goodbye_embed_data"):
        clean, err = _sanitize_panel_embed(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        value = clean
    elif key in ("welcome_image_config", "goodbye_image_config"):
        if value is not None and not isinstance(value, dict):
            return JSONResponse({"error": f"'{key}' must be an object or null"}, status_code=400)
        if value is not None:
            tl = value.get("text_layers")
            if tl is not None:
                if not isinstance(tl, list) or len(tl) > 5:
                    return JSONResponse({"error": "text_layers must be a list with at most 5 entries"}, status_code=400)
                for i, layer in enumerate(tl):
                    if not isinstance(layer, dict):
                        return JSONResponse({"error": f"text_layers[{i}] must be an object"}, status_code=400)
                    for field in ("content", "color"):
                        if field in layer and not isinstance(layer[field], str):
                            return JSONResponse({"error": f"text_layers[{i}].{field} must be a string"}, status_code=400)
                    if "font_size" in layer:
                        try:
                            layer["font_size"] = max(8, min(72, int(layer["font_size"])))
                        except (TypeError, ValueError):
                            return JSONResponse({"error": f"text_layers[{i}].font_size must be a number"}, status_code=400)
                    if "y" in layer:
                        try:
                            layer["y"] = max(0, min(2000, int(layer["y"])))
                        except (TypeError, ValueError):
                            return JSONResponse({"error": f"text_layers[{i}].y must be a number"}, status_code=400)
                    if "x" in layer:
                        try:
                            layer["x"] = max(0, min(4000, int(layer["x"])))
                        except (TypeError, ValueError):
                            return JSONResponse({"error": f"text_layers[{i}].x must be a number"}, status_code=400)
            bg_type = value.get("bg_type", "gradient")
            if bg_type not in ("solid", "gradient", "image"):
                return JSONResponse({"error": "bg_type must be 'solid', 'gradient', or 'image'"}, status_code=400)
            if "bg_opacity" in value:
                try:
                    value["bg_opacity"] = max(0, min(100, int(value["bg_opacity"])))
                except (TypeError, ValueError):
                    return JSONResponse({"error": "bg_opacity must be a number (0-100)"}, status_code=400)
            bs = value.get("avatar_border_style", "solid")
            if bs not in ("solid", "dashed", "dotted", "none"):
                return JSONResponse({"error": "avatar_border_style must be 'solid', 'dashed', 'dotted', or 'none'"}, status_code=400)
            for s_field in ("solid_color", "avatar_border"):
                if s_field in value and not isinstance(value[s_field], str):
                    return JSONResponse({"error": f"{s_field} must be a string"}, status_code=400)
            grad = value.get("gradient")
            if grad is not None:
                if not isinstance(grad, dict):
                    return JSONResponse({"error": "gradient must be an object"}, status_code=400)
            for num_key in ("width", "height", "avatar_size", "avatar_y", "avatar_border_width"):
                if num_key in value:
                    try:
                        value[num_key] = max(0, min(4000, int(value[num_key])))
                    except (TypeError, ValueError):
                        return JSONResponse({"error": f"{num_key} must be a number"}, status_code=400)
    err = await _save_settings("welcome_settings", str(guild_id), key, value, WELCOME_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/welcomer/{guild_id}/channels")
async def welcomer_channels(guild_id: str, request: Request):
    """Text channels for the welcome channel dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/welcomer/{guild_id}/roles")
async def welcomer_roles(guild_id: str, request: Request):
    """All roles for the auto-role dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Birthday API v1
# ---------------------------------------------------------------------------

BIRTHDAY_DEFAULTS = {
    "enabled": True,
    "channel_id": None,
    "role_id": None,
    "message": "Happy birthday {member}! 🎂 Wishing you an amazing day!",
    "message_type": "basic",
    "embed_data": None,
    "allow_year": True,
}


async def _get_birthday_settings(guild_id: str):
    return await fetchrow_cached("birthday_settings", "SELECT settings FROM birthday_settings WHERE guild_id = ?", guild_id, BIRTHDAY_DEFAULTS)


@app.get("/api/v1/birthday/{guild_id}/settings")
async def birthday_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_birthday_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/birthday/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def birthday_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "channel_id" and value is not None:
        if not _valid_snowflake(value):
            return JSONResponse({"error": "invalid channel ID"}, status_code=400)
        value = str(value)
    elif key == "role_id" and value is not None:
        if not _valid_snowflake(value):
            return JSONResponse({"error": "invalid role ID"}, status_code=400)
        value = str(value)
    elif key == "message":
        if not isinstance(value, str) or not value.strip():
            return JSONResponse({"error": "message must be a non-empty string"}, status_code=400)
        value = value[:500]
    elif key == "message_type":
        if value not in ("basic", "custom"):
            return JSONResponse({"error": "message_type must be 'basic' or 'custom'"}, status_code=400)
    elif key == "embed_data":
        if value is not None and not isinstance(value, dict):
            return JSONResponse({"error": "embed_data must be an object or null"}, status_code=400)
    err = await _save_settings("birthday_settings", str(guild_id), key, value, BIRTHDAY_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/birthday/{guild_id}/birthdays")
async def birthday_list(guild_id: str, request: Request):
    """List all birthdays for a guild."""
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT user_id, month, day, year FROM birthdays WHERE guild_id = ? ORDER BY month, day",
        str(guild_id),
    )
    return {"birthdays": [dict(r) for r in rows]}


@app.get("/api/v1/birthday/{guild_id}/channels")
async def birthday_channels(guild_id: str, request: Request):
    """Text channels for the birthday channel dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/birthday/{guild_id}/roles")
async def birthday_roles(guild_id: str, request: Request):
    """All roles for the birthday role dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Activity Roles API v1
# ---------------------------------------------------------------------------

ACTIVITY_ROLES_DEFAULTS = {
    "enabled": True,
}


@app.get("/api/v1/activity_roles/{guild_id}/settings")
async def activity_roles_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    row = await fetchrow(
        "SELECT settings FROM guild_settings WHERE guild_id = ?",
        str(guild_id),
    )
    if row and row.get("settings"):
        try:
            all_settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
            ar_settings = all_settings.get("activity_roles", ACTIVITY_ROLES_DEFAULTS)
        except (json.JSONDecodeError, TypeError):
            ar_settings = ACTIVITY_ROLES_DEFAULTS
    else:
        ar_settings = ACTIVITY_ROLES_DEFAULTS
    return {"settings": ar_settings, "is_mod": True}


@app.post("/api/v1/activity_roles/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def activity_roles_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    # Get current settings
    row = await fetchrow(
        "SELECT settings FROM guild_settings WHERE guild_id = ?",
        str(guild_id),
    )
    if row and row.get("settings"):
        try:
            all_settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
        except (json.JSONDecodeError, TypeError):
            all_settings = {}
    else:
        all_settings = {}
    ar_settings = all_settings.get("activity_roles", ACTIVITY_ROLES_DEFAULTS)
    ar_settings[key] = value
    all_settings["activity_roles"] = ar_settings
    await execute(
        "INSERT INTO guild_settings (guild_id, settings, updated_at) VALUES (?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET settings = ?, updated_at = ?",
        str(guild_id), json.dumps(all_settings), time.time(), json.dumps(all_settings), time.time(),
    )
    return {"ok": True}


@app.get("/api/v1/activity_roles/{guild_id}/rules")
async def activity_roles_list(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT activity, role_id, enabled FROM activity_role_rules WHERE guild_id = ? ORDER BY created_at ASC",
        str(guild_id),
    )
    return {"rules": [dict(r) for r in rows]}


@app.post("/api/v1/activity_roles/{guild_id}/rules", dependencies=[Depends(require_mod)])
async def activity_roles_add(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    activity = (body.get("activity") or "").strip().lower()
    role_id = body.get("role_id")
    if not activity:
        return JSONResponse({"error": "activity name required"}, status_code=400)
    if not _valid_snowflake(role_id):
        return JSONResponse({"error": "invalid role ID"}, status_code=400)
    now = time.time()
    await execute(
        "INSERT INTO activity_role_rules (guild_id, activity, role_id, enabled, created_at) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT (guild_id, activity) DO UPDATE SET role_id=?, enabled=1",
        str(guild_id), activity, str(role_id), now, str(role_id),
    )
    return {"ok": True}


@app.delete("/api/v1/activity_roles/{guild_id}/rules/{activity}", dependencies=[Depends(require_mod)])
async def activity_roles_remove(guild_id: str, activity: str, request: Request):
    await require_guild_access(request, guild_id)
    await execute(
        "DELETE FROM activity_role_rules WHERE guild_id = ? AND activity = ?",
        str(guild_id), activity,
    )
    return {"ok": True}


@app.get("/api/v1/activity_roles/{guild_id}/channels")
async def activity_roles_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/activity_roles/{guild_id}/roles")
async def activity_roles_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Badges API v1
# ---------------------------------------------------------------------------

BADGE_DEFINITIONS = [
    {"id": "msg_100",    "name": "Chatterbox",      "desc": "Sent 100 messages",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660505827151943.png" class="badge-emoji" alt="message" />', "category": "messages", "threshold": 100},
    {"id": "msg_500",    "name": "Conversationalist","desc": "Sent 500 messages",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660505827151943.png" class="badge-emoji" alt="message" />', "category": "messages", "threshold": 500},
    {"id": "msg_1k",     "name": "Big Talker",      "desc": "Sent 1,000 messages",        "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660505827151943.png" class="badge-emoji" alt="message" />', "category": "messages", "threshold": 1000},
    {"id": "msg_5k",     "name": "Non-Stop",        "desc": "Sent 5,000 messages",        "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660467931881623.png" class="badge-emoji" alt="fire" />', "category": "messages", "threshold": 5000},
    {"id": "msg_10k",    "name": "Legend",           "desc": "Sent 10,000 messages",       "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660544356294747.png" class="badge-emoji" alt="star" />', "category": "messages", "threshold": 10000},
    {"id": "msg_25k",    "name": "Living Chat",     "desc": "Sent 25,000 messages",       "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660473174753360.png" class="badge-emoji" alt="gem" />', "category": "messages", "threshold": 25000},
    {"id": "msg_50k",    "name": "Server Soul",     "desc": "Sent 50,000 messages",       "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660454052921364.png" class="badge-emoji" alt="crown" />', "category": "messages", "threshold": 50000},
    {"id": "msg_100k",   "name": "Myth",            "desc": "Sent 100,000 messages",      "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660528229056562.png" class="badge-emoji" alt="reward" />', "category": "messages", "threshold": 100000},
    {"id": "vc_1h",      "name": "Lurker",          "desc": "1 hour in voice",            "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 60},
    {"id": "vc_10h",     "name": "Regular",         "desc": "10 hours in voice",          "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 600},
    {"id": "vc_50h",     "name": "Voice Veteran",   "desc": "50 hours in voice",          "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 3000},
    {"id": "vc_100h",    "name": "Echo",            "desc": "100 hours in voice",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 6000},
    {"id": "vc_500h",    "name": "Voice Lord",      "desc": "500 hours in voice",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 30000},
    {"id": "vc_1000h",   "name": "Voice Legend",    "desc": "1,000 hours in voice",       "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660512789958656.png" class="badge-emoji" alt="music" />', "category": "voice", "threshold": 60000},
    {"id": "tenure_1m",  "name": "Newcomer",        "desc": "Member for 1 month",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660433786052618.png" class="badge-emoji" alt="calendar" />', "category": "tenure", "threshold": 30},
    {"id": "tenure_6m",  "name": "Regular",         "desc": "Member for 6 months",        "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660433786052618.png" class="badge-emoji" alt="calendar" />', "category": "tenure", "threshold": 180},
    {"id": "tenure_1y",  "name": "Veteran",         "desc": "Member for 1 year",          "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660528229056562.png" class="badge-emoji" alt="reward" />', "category": "tenure", "threshold": 365},
    {"id": "tenure_2y",  "name": "Old Guard",       "desc": "Member for 2 years",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660528229056562.png" class="badge-emoji" alt="reward" />', "category": "tenure", "threshold": 730},
    {"id": "first_member","name": "First Member",   "desc": "First member of the server", "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660544356294747.png" class="badge-emoji" alt="star" />', "category": "special", "threshold": 0},
    {"id": "booster",    "name": "Server Booster",  "desc": "Currently boosting",         "emoji": '<img src="https://cdn.discordapp.com/emojis/1538660529508323328.png" class="badge-emoji" alt="rocket" />', "category": "special", "threshold": 0},
]


@app.get("/api/v1/badges/{guild_id}/definitions")
async def badge_definitions(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    return {"badges": BADGE_DEFINITIONS}


@app.get("/api/v1/badges/{guild_id}/user/{user_id}")
async def badge_user(guild_id: str, user_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT badge_id, awarded_at FROM user_badges WHERE guild_id = ? AND user_id = ? ORDER BY awarded_at ASC",
        str(guild_id), user_id,
    )
    act_row = await fetchrow(
        "SELECT vc_minutes FROM user_activity WHERE guild_id = ? AND user_id = ?",
        str(guild_id), user_id,
    )
    lvl_row = await fetchrow(
        "SELECT messages FROM leveling_data WHERE guild_id = ? AND user_id = ?",
        str(guild_id), user_id,
    )
    vc_minutes = int(act_row["vc_minutes"]) if act_row and act_row.get("vc_minutes") else 0
    messages = int(lvl_row["messages"]) if lvl_row and lvl_row.get("messages") else 0
    return {
        "badges": [dict(r) for r in rows],
        "stats": {"vc_minutes": vc_minutes, "messages": messages},
    }


@app.get("/api/v1/badges/{guild_id}/leaderboard")
async def badge_leaderboard(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT user_id, COUNT(*) as count FROM user_badges WHERE guild_id = ? GROUP BY user_id ORDER BY count DESC LIMIT 20",
        str(guild_id),
    )
    return {"leaderboard": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
#  Temp Channels API v1
# ---------------------------------------------------------------------------

TEMP_CHANNEL_DEFAULTS = {
    "enabled": True,
    "jtc_enabled": False,
    "jtc_hub_channel": None,
    "jtc_category": None,
    "jtc_naming": "{user}'s Channel",
    "jtc_default_limit": 0,
    "tempchat_enabled": True,
    "tempchat_default_minutes": 60,
    "tempchat_category": None,
}


async def _get_temp_settings(guild_id: str):
    return await fetchrow_cached("temp_channel_settings", "SELECT settings FROM temp_channel_settings WHERE guild_id = ?", guild_id, TEMP_CHANNEL_DEFAULTS)


@app.get("/api/v1/temp_channels/{guild_id}/settings")
async def temp_channels_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_temp_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/temp_channels/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def temp_channels_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key in ("jtc_hub_channel", "jtc_category", "tempchat_category") and value is not None:
        if not _valid_snowflake(value):
            return JSONResponse({"error": f"invalid {key}"}, status_code=400)
        value = str(value)
    elif key == "jtc_naming":
        if not isinstance(value, str) or not value.strip():
            return JSONResponse({"error": "naming pattern required"}, status_code=400)
        value = value[:100]
    elif key == "tempchat_default_minutes":
        try:
            value = max(1, min(10080, int(value)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid duration"}, status_code=400)
    err = await _save_settings("temp_channel_settings", str(guild_id), key, value, TEMP_CHANNEL_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/temp_channels/{guild_id}/active")
async def temp_channels_active(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT channel_id, owner_id, channel_type, created_at, expires_at FROM temp_channels WHERE guild_id = ? ORDER BY created_at DESC",
        str(guild_id),
    )
    return {"channels": [dict(r) for r in rows]}


@app.get("/api/v1/temp_channels/{guild_id}/channels")
async def temp_channels_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", ""), "type": c.get("type", 0)} for c in d["channels"]]}
    return {"channels": []}


@app.get("/api/v1/temp_channels/{guild_id}/categories")
async def temp_channels_categories(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"categories": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 4]}
    return {"categories": []}


@app.get("/api/v1/temp_channels/{guild_id}/roles")
async def temp_channels_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Frenzy Mode API v1
# ---------------------------------------------------------------------------

FRENZY_DEFAULTS = {
    "enabled": True,
    "default_multiplier": 2.0,
    "max_multiplier": 10.0,
    "max_duration_minutes": 1440,
    "announce_channel_id": None,
    "announce_start": True,
    "announce_end": True,
    "auto_triggers": {
        "member_join": {"enabled": False, "count": 5, "minutes": 10, "multiplier": 2.0, "duration_minutes": 30},
        "message_spike": {"enabled": False, "count": 100, "minutes": 5, "multiplier": 1.5, "duration_minutes": 15},
        "voice_activity": {"enabled": False, "count": 10, "multiplier": 2.0, "duration_minutes": 60},
        "boost": {"enabled": False, "multiplier": 3.0, "duration_minutes": 60},
        "level_milestone": {"enabled": False, "level": 50, "multiplier": 2.0, "duration_minutes": 30},
    },
}


async def _get_frenzy_settings(guild_id: str):
    return await fetchrow_cached("frenzy_settings", "SELECT settings FROM frenzy_settings WHERE guild_id = ?", guild_id, FRENZY_DEFAULTS)


@app.get("/api/v1/frenzy/{guild_id}/settings")
async def frenzy_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_frenzy_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/frenzy/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def frenzy_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key in ("default_multiplier", "max_multiplier"):
        try:
            value = max(1.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return JSONResponse({"error": f"invalid {key}"}, status_code=400)
    elif key == "max_duration_minutes":
        try:
            value = max(0, min(10080, int(value)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid duration"}, status_code=400)
    elif key == "announce_channel_id" and value is not None:
        if not _valid_snowflake(value):
            return JSONResponse({"error": "invalid channel"}, status_code=400)
        value = str(value)
    elif key == "auto_triggers":
        if not isinstance(value, dict):
            return JSONResponse({"error": "invalid auto_triggers"}, status_code=400)
    err = await _save_settings("frenzy_settings", str(guild_id), key, value, FRENZY_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/frenzy/{guild_id}/active")
async def frenzy_active(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    row = await fetchrow(
        "SELECT multiplier, reason, started_by, started_at, expires_at FROM frenzy_active WHERE guild_id = ?",
        str(guild_id),
    )
    if not row:
        return {"active": None}
    now = time.time()
    expires = row["expires_at"]
    if expires and now > float(expires):
        await execute("DELETE FROM frenzy_active WHERE guild_id = ?", str(guild_id))
        return {"active": None}
    return {"active": dict(row)}


@app.post("/api/v1/frenzy/{guild_id}/active", dependencies=[Depends(require_mod)])
async def frenzy_start(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    multiplier = float(body.get("multiplier", 2.0))
    duration_minutes = body.get("duration_minutes")
    reason = body.get("reason", "Started from dashboard")
    started_by = body.get("started_by", "dashboard")
    if duration_minutes is not None:
        duration_minutes = int(duration_minutes)
    settings = await _get_frenzy_settings(guild_id)
    max_mult = settings.get("max_multiplier", 10.0)
    if multiplier < 1.0 or multiplier > max_mult:
        return JSONResponse({"error": f"Multiplier must be between 1 and {max_mult}"}, status_code=400)
    max_dur = settings.get("max_duration_minutes", 1440)
    if duration_minutes and max_dur and duration_minutes > max_dur:
        return JSONResponse({"error": f"Maximum duration is {max_dur} minutes"}, status_code=400)
    now = time.time()
    expires_at = now + (duration_minutes * 60) if duration_minutes else None
    await execute(
        "INSERT INTO frenzy_active (guild_id, multiplier, reason, started_by, started_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET "
        "multiplier = ?, reason = ?, started_by = ?, started_at = ?, expires_at = ?",
        str(guild_id), multiplier, reason, str(started_by), now, expires_at,
        multiplier, reason, str(started_by), now, expires_at,
    )
    return {"active": {"multiplier": multiplier, "reason": reason, "started_by": str(started_by), "started_at": now, "expires_at": expires_at}}


@app.delete("/api/v1/frenzy/{guild_id}/active", dependencies=[Depends(require_mod)])
async def frenzy_stop(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    await execute("DELETE FROM frenzy_active WHERE guild_id = ?", str(guild_id))
    return {"ok": True}


@app.get("/api/v1/frenzy/{guild_id}/channels")
async def frenzy_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", ""), "type": c.get("type", 0)} for c in d["channels"]]}
    return {"channels": []}


@app.get("/api/v1/frenzy/{guild_id}/roles")
async def frenzy_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Autoresponder API v1
# ---------------------------------------------------------------------------

@app.get("/api/v1/autoresponder/{guild_id}/triggers")
async def autoresponder_triggers(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT id, trigger, response, match_type, channel_id, cooldown FROM autoresponder WHERE guild_id = ? ORDER BY created_at ASC",
        str(guild_id),
    )
    if not rows:
        return {"triggers": []}
    return {"triggers": [dict(r) for r in rows]}


@app.get("/api/v1/autoresponder/{guild_id}/channels")
async def autoresponder_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": [{"id": "2001", "name": "general"}]}


@app.post("/api/v1/autoresponder/{guild_id}/triggers", dependencies=[Depends(require_mod)])
async def autoresponder_trigger_add(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    trigger = (body.get("trigger") or "").strip()
    response = (body.get("response") or "").strip()
    match_type = body.get("match_type") or "contains"
    channel_id = body.get("channel_id") or None
    cooldown = body.get("cooldown") or 0
    if not trigger or not response:
        return JSONResponse({"error": "trigger and response are required"}, status_code=400)
    if len(trigger) > 300:
        return JSONResponse({"error": "trigger is too long (max 300 chars)"}, status_code=400)
    if len(response) > 2000:
        return JSONResponse({"error": "response is too long (max 2000 chars)"}, status_code=400)
    if match_type not in ("contains", "exact", "starts_with", "ends_with", "regex"):
        return JSONResponse({"error": "invalid match_type"}, status_code=400)
    if channel_id is not None and not _valid_snowflake(channel_id):
        return JSONResponse({"error": "channel must be a valid Discord ID or null"}, status_code=400)
    try:
        cooldown = max(0, int(cooldown))
    except (TypeError, ValueError):
        return JSONResponse({"error": "cooldown must be an integer (seconds)"}, status_code=400)
    existing = await query(
        "SELECT id FROM autoresponder WHERE guild_id = ? AND lower(trigger) = lower(?) AND match_type = ?",
        str(guild_id), trigger, match_type,
    )
    if existing:
        return JSONResponse({"error": "A trigger with this text and match type already exists."}, status_code=400)
    r = await query(
        "INSERT INTO autoresponder (guild_id, trigger, response, match_type, channel_id, cooldown) VALUES (?, ?, ?, ?, ?, ?) RETURNING id, trigger, response, match_type, channel_id, cooldown",
        str(guild_id), trigger, response, match_type, channel_id, cooldown,
    )
    return {"ok": True, "trigger": dict(r[0]) if r else None}


@app.delete("/api/v1/autoresponder/{guild_id}/triggers/{trigger_id}", dependencies=[Depends(require_mod)])
async def autoresponder_trigger_remove(guild_id: str, trigger_id: str, request: Request):
    await require_guild_access(request, guild_id)
    try:
        tid = int(trigger_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid trigger id"}, status_code=400)
    await execute("DELETE FROM autoresponder WHERE guild_id = ? AND id = ?", str(guild_id), tid)
    return {"ok": True}


# ---------------------------------------------------------------------------
#  Social Alerts API v1
# ---------------------------------------------------------------------------

SOCIAL_SETTINGS_DEFAULTS = {
    "enabled": True,
    "youtube_enabled": False, "youtube_channel_id": None, "youtube_ping_role": None, "youtube_announce_channel_id": None, "youtube_message": None,
    "twitch_enabled": False, "twitch_channel": None, "twitch_ping_role": None, "twitch_announce_channel_id": None, "twitch_message": None,
    "twitter_enabled": False, "twitter_handle": None, "twitter_ping_role": None, "twitter_announce_channel_id": None, "twitter_message": None,
    "default_announce_channel_id": None, "default_ping_role": None,
    "extra_alerts": {},
}


async def _get_social_settings(guild_id: str):
    return await fetchrow_cached("social_settings", "SELECT settings FROM social_settings WHERE guild_id = ?", guild_id, SOCIAL_SETTINGS_DEFAULTS)


@app.get("/api/v1/social/{guild_id}/settings")
async def social_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_social_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/social/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def social_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)

    # Special validation for extra_alerts
    if key == "extra_alerts":
        clean, err = _sanitize_extra_alerts(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("social_settings", str(guild_id), "extra_alerts", clean, SOCIAL_SETTINGS_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}

    err = await _save_settings("social_settings", str(guild_id), key, value, SOCIAL_SETTINGS_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/social/{guild_id}/roles")
async def social_roles(guild_id: str, request: Request):
    """All roles for ping-role dropdowns."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


@app.get("/api/v1/social/{guild_id}/channels")
async def social_channels(guild_id: str, request: Request):
    """Text channels for the announce-channel dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


# ---------------------------------------------------------------------------
#  Tickets API v1
# ---------------------------------------------------------------------------

TICKET_SETTINGS_DEFAULTS = {
    "enabled": False,
    "category_id": None,
    "support_role_id": None,
    "log_channel_id": None,
    "welcome_message": "Support will be with you shortly. Please describe your issue.",
    "ticket_limit": 3,
    "auto_close_hours": 0,
    "panel_channel_id": None,
    "panel_embed": {},
    "questions": [],
}


def _sanitize_panel_embed(value):
    """Validate panel_embed: dict with bounded string/field limits."""
    if not isinstance(value, dict):
        return None, "panel_embed must be an object"
    clean = {}
    for key in ("title", "description", "url", "author_name", "author_url", "author_icon",
                "image_url", "thumbnail_url", "footer_text", "footer_icon"):
        v = value.get(key)
        if v is not None:
            if not isinstance(v, str):
                return None, f"'{key}' must be a string"
            clean[key] = v[:1024]
    color = value.get("color")
    if color is not None:
        try:
            int(str(color).lstrip("#"), 16)
        except (ValueError, TypeError):
            return None, "invalid color"
        clean["color"] = str(color)
    fields = value.get("fields")
    if fields is not None:
        if not isinstance(fields, list) or len(fields) > 25:
            return None, "fields must be a list of max 25"
        clean_fields = []
        for f in fields:
            if not isinstance(f, dict):
                return None, "each field must be an object"
            name = str(f.get("name") or "")[:256]
            if not name:
                return None, "field name is required"
            clean_fields.append({
                "name": name,
                "value": str(f.get("value") or "\u200b")[:1024],
                "inline": bool(f.get("inline")),
            })
        clean["fields"] = clean_fields
    return clean, None


def _sanitize_questions(value):
    """Validate questions: list of {label, placeholder, required}, max 5 (Discord modal limit)."""
    if not isinstance(value, list):
        return None, "questions must be a list"
    if len(value) > 5:
        return None, "questions exceeds max of 5 (Discord modal limit)"
    clean = []
    for q in value:
        if not isinstance(q, dict):
            return None, "each question must be an object"
        label = str(q.get("label") or "").strip()
        if not label:
            return None, "question label is required"
        clean.append({
            "label": label[:45],
            "placeholder": (str(q.get("placeholder") or "").strip())[:100] or None,
            "required": bool(q.get("required", True)),
        })
    return clean, None


async def _get_ticket_settings(guild_id: str):
    return await fetchrow_cached("ticket_settings", "SELECT settings FROM ticket_settings WHERE guild_id = ?", guild_id, TICKET_SETTINGS_DEFAULTS)


@app.get("/api/v1/tickets/{guild_id}/settings")
async def ticket_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_ticket_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/tickets/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def ticket_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "panel_embed":
        clean, err = _sanitize_panel_embed(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("ticket_settings", str(guild_id), "panel_embed", clean, TICKET_SETTINGS_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    if key == "questions":
        clean, err = _sanitize_questions(value)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        err = await _save_settings("ticket_settings", str(guild_id), "questions", clean, TICKET_SETTINGS_DEFAULTS)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        return {"ok": True}
    err = await _save_settings("ticket_settings", str(guild_id), key, value, TICKET_SETTINGS_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.post("/api/v1/tickets/{guild_id}/send_panel", dependencies=[Depends(require_mod)])
async def ticket_send_panel(guild_id: str, request: Request):
    """Queue the bot to send the ticket panel embed to a channel."""
    await require_guild_access(request, guild_id)
    body = await request.json()
    channel_id = body.get("channel_id")
    if not channel_id:
        return JSONResponse({"error": "missing channel_id"}, status_code=400)
    session_user = request.session.get("user") or {}
    moderator = session_user.get("username", "Unknown")
    request_id = await _queue_action(guild_id, "panel_send", channel_id, "panel", "Ticket panel", None, moderator)
    ok, message = await _call_bot_direct(
        guild_id, "panel_send", channel_id, "panel", "Ticket panel", None, moderator,
        request_id=request_id,
    )
    if ok:
        return {"ok": True, "direct": True, "request_id": request_id}
    return {"ok": True, "queued": True, "direct": False, "fallback": message, "request_id": request_id}


@app.get("/api/v1/tickets/{guild_id}/categories")
async def ticket_categories(guild_id: str, request: Request):
    """Categories for the ticket category dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"categories": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 4]}
    return {"categories": []}


@app.get("/api/v1/tickets/{guild_id}/channels")
async def ticket_channels(guild_id: str, request: Request):
    """Text channels for the log-channel dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/tickets/{guild_id}/roles")
async def ticket_roles(guild_id: str, request: Request):
    """All roles for the support-role dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


# ---------------------------------------------------------------------------
#  Verification API v1
# ---------------------------------------------------------------------------

VERIFY_SETTINGS_DEFAULTS = {
    "enabled": False, "channel_id": None, "verified_role_id": None,
    "log_channel_id": None, "type": "button", "captcha": False,
    "message": "Click the button below to verify yourself.",
    "reaction_emoji": "✅",
    # Bot-owner defaults from .env - server owners don't need their own keys.
    "recaptcha_site_key": os.environ.get("RECAPTCHA_SITE_KEY", ""),
    "recaptcha_secret": "",
    "panel_embed": {}, "panel_message_id": None,
}


async def _get_verify_settings(guild_id: str):
    return await fetchrow_cached("verify_settings", "SELECT settings FROM verify_settings WHERE guild_id = ?", guild_id, VERIFY_SETTINGS_DEFAULTS)


@app.get("/api/v1/verify/{guild_id}/settings")
async def verify_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_verify_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/verify/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def verify_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key"); value = body.get("value")
    if not key: return JSONResponse({"error": "missing key"}, 400)
    err = await _save_settings("verify_settings", str(guild_id), key, value, VERIFY_SETTINGS_DEFAULTS)
    if err: return JSONResponse({"error": err}, 400)
    # Disabling verification should also remove the deployed panel
    if key == "enabled" and not value:
        session_user = request.session.get("user") or {}
        moderator = session_user.get("username", "Unknown")
        request_id = await _queue_action(str(guild_id), "verify_panel_remove", "0", "", "Verification disabled - panel removed", None, moderator)
        ok, message = await _call_bot_direct(
            guild_id, "verify_panel_remove", "0", "", "Verification disabled - panel removed", None, moderator,
            request_id=request_id,
        )
        if not ok:
            return {"ok": True, "queued": True, "fallback": message}
    return {"ok": True}


@app.post("/api/v1/verify/{guild_id}/deploy", dependencies=[Depends(require_mod)])
async def verify_deploy(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    session_user = request.session.get("user") or {}
    moderator = session_user.get("username", "Unknown")
    request_id = await _queue_action(guild_id, "verify_panel", "0", "", "Deploy verification panel", None, moderator)
    ok, message = await _call_bot_direct(
        guild_id, "verify_panel", "0", "", "Deploy verification panel", None, moderator,
        request_id=request_id,
    )
    if ok:
        return {"ok": True, "direct": True, "request_id": request_id}
    return {"ok": True, "queued": True, "direct": False, "fallback": message, "request_id": request_id}


@app.get("/api/v1/verify/{guild_id}/channels")
async def verify_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/verify/{guild_id}/roles")
async def verify_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name")} for r in d["roles"]]}
    return {"roles": []}


@app.get("/verify/{guild_id}", response_class=HTMLResponse)
async def verify_page(guild_id: str, request: Request):
    """Public verification page that renders the reCAPTCHA / Turnstile widget."""
    settings = await _get_verify_settings(guild_id)
    provider = settings.get("type", "")
    if not settings.get("enabled") or provider != "recaptcha":
        return HTMLResponse("This server doesn't use link-based verification.", status_code=400)
    site_key = settings.get(f"{provider}_site_key", "")
    user_id = request.query_params.get("u", "")
    return templates.TemplateResponse(request, "verify.html", {
        "guild_id": guild_id,
        "provider": provider,
        "site_key": site_key,
        "user_id": user_id,
    })


@app.post("/api/v1/verify/{guild_id}/complete")
async def verify_complete(guild_id: str, request: Request):
    """Verify a captcha token and queue the bot to assign the verified role."""
    body = await request.json()
    token = body.get("token", "")
    user_id = body.get("user_id", "")
    provider = body.get("provider", "")
    if not token or not user_id or provider != "recaptcha":
        return JSONResponse({"error": "invalid request"}, status_code=400)
    settings = await _get_verify_settings(guild_id)
    if not settings.get("enabled") or settings.get("type") != provider:
        return JSONResponse({"error": "verification not configured for this method"}, status_code=400)
    secret = os.environ.get(f"{provider.upper()}_SECRET", "") or settings.get(f"{provider}_secret", "")
    if not secret:
        return JSONResponse({"error": "captcha secret not configured"}, status_code=400)

    verify_url = "https://www.google.com/recaptcha/api/siteverify"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(verify_url, data={"secret": secret, "response": token})
            data = r.json()
    except Exception as e:
        return JSONResponse({"error": f"verify failed: {e}"}, status_code=500)

    if not data.get("success"):
        return JSONResponse({"error": "captcha verification failed"}, status_code=400)

    await _queue_action(guild_id, "verify_user", user_id, "", "Verified via " + provider, None)
    return {"ok": True, "queued": True}


@app.post("/api/v1/captcha/complete")
async def captcha_complete(request: Request):
    """Auto-verify a user after they solve the captcha on the web page (no token copying)."""
    raw = await request.body()
    print(f"[CAPTCHA] hit /complete, raw body bytes={len(raw)}: {raw[:200]!r}")
    body = await request.json()
    token = body.get("token", "")
    code = body.get("code", "")
    provider = body.get("provider", "")
    if not token or not code or provider != "recaptcha":
        print(f"[CAPTCHA] invalid request: token_len={len(token)} code={code!r} provider={provider!r}")
        return JSONResponse({"error": "invalid request"}, status_code=400)
    info = await _consume_captcha_code(code, provider)
    if not info or not info.get("guild_id") or not info.get("user_id"):
        print(f"[CAPTCHA] code invalid/expired/used: code={code!r}")
        return JSONResponse({"error": "verification link expired or already used"}, status_code=403)

    settings = await _get_verify_settings(info["guild_id"])
    secret = os.environ.get(f"{provider.upper()}_SECRET", "") or settings.get(f"{provider}_secret", "")
    if not secret:
        print(f"[CAPTCHA] missing {provider.upper()}_SECRET for guild {info['guild_id']}")
        return JSONResponse({"error": "captcha secret not configured"}, status_code=400)

    verify_url = "https://www.google.com/recaptcha/api/siteverify"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(verify_url, data={"secret": secret, "response": token})
            data = r.json()
    except Exception as e:
        return JSONResponse({"error": f"verify failed: {e}"}, status_code=500)

    if not data.get("success"):
        codes = ", ".join(data.get("error-codes", []))
        print(f"[CAPTCHA] siteverify failed: {codes}")
        return JSONResponse({"error": f"captcha verification failed ({codes})"}, status_code=400)

    await _queue_action(info["guild_id"], "verify_user", info["user_id"], "", "Verified via " + provider, None)
    return {"ok": True, "queued": True}


@app.get("/api/v1/members/{guild_id}/roles")
async def members_roles(guild_id: str, request: Request):
    """All roles (unfiltered) with positions + managed flag for member management."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", ""), "position": r.get("position", 0), "managed": bool(r.get("managed", False))} for r in d["roles"]]}
    return {"roles": []}


@app.get("/api/v1/members/{guild_id}/bot")
async def members_bot_info(guild_id: str, request: Request):
    """Bot hierarchy/permission info for member management checks."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d:
        return {
            "bot_top_role_position": d.get("bot_top_role_position", 0),
            "bot_permissions": int(d.get("bot_permissions", 0) or 0),
            "can_manage_nicknames": bool(int(d.get("bot_permissions", 0) or 0) & (1 << 27)),
            "can_manage_roles": bool(int(d.get("bot_permissions", 0) or 0) & (1 << 28)),
        }
    return {"bot_top_role_position": 0, "bot_permissions": 0, "can_manage_nicknames": False, "can_manage_roles": False}


# ---------------------------------------------------------------------------
#  Global Chat API v1
# ---------------------------------------------------------------------------

GC_DEFAULTS = {"enabled": False, "channel_id": None, "management_channel_id": None}


async def _get_gc_settings(guild_id: str):
    d = dict(GC_DEFAULTS)
    for suffix, out_key in (("enabled", "enabled"), ("channel", "channel_id"), ("gc_management_channel", "management_channel_id")):
        row = await fetchrow("SELECT value FROM bot_stats WHERE key = ?", f"global_chat_{suffix}_{guild_id}")
        if row:
            val = row["value"]
            if out_key == "enabled":
                d[out_key] = (val.lower() == "true")
            else:
                d[out_key] = str(val) if val else None
    return d


@app.get("/api/v1/global_chat/{guild_id}/settings")
async def gc_settings(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_gc_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/global_chat/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def gc_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key"); value = body.get("value")
    if not key: return JSONResponse({"error": "missing key"}, 400)
    if key not in ("enabled", "channel_id", "management_channel_id"):
        return JSONResponse({"error": f"unknown key '{key}'"}, 400)
    if key == "management_channel_id":
        db_key = f"global_chat_gc_management_channel_{guild_id}"
    elif key == "enabled":
        db_key = f"global_chat_enabled_{guild_id}"
    else:
        db_key = f"global_chat_channel_{guild_id}"
    db_val = str(value) if value is not None else ""
    await execute(
        "INSERT INTO bot_stats (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        db_key, db_val, time.time(),
    )
    return {"ok": True}


@app.get("/api/v1/global_chat/{guild_id}/channels")
async def gc_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.post("/api/v1/global_chat/{guild_id}/deploy_panel", dependencies=[Depends(require_mod)])
async def gc_deploy_panel(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return JSONResponse({"error": "bot bridge not configured"}, 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                BOT_SERVER_URL.rstrip("/") + "/api/gc/deploy_panel",
                json={"guild_id": str(guild_id)},
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
            data = r.json() if r.status_code == 200 else {"error": f"bot responded {r.status_code}"}
            return JSONResponse(data, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, 502)


@app.post("/api/v1/global_chat/{guild_id}/remove_panel", dependencies=[Depends(require_mod)])
async def gc_remove_panel(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    if not BOT_SERVER_URL or not BOT_HTTP_TOKEN:
        return JSONResponse({"error": "bot bridge not configured"}, 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                BOT_SERVER_URL.rstrip("/") + "/api/gc/remove_panel",
                json={"guild_id": str(guild_id)},
                headers={"X-Prowl-Token": BOT_HTTP_TOKEN},
            )
            data = r.json() if r.status_code == 200 else {"error": f"bot responded {r.status_code}"}
            return JSONResponse(data, status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, 502)


# ---------------------------------------------------------------------------
#  Server Settings API v1
# ---------------------------------------------------------------------------

SERVER_DEFAULTS = {
    "language": "en",
    "timezone": "UTC",
}

ALL_FEATURE_TABLES = (
    "guild_settings", "mod_settings", "mod_log", "muted_users",
    "leveling_settings", "leveling_data", "automod_settings", "ai_settings",
    "raid_settings", "autoresponder", "social_settings", "welcome_settings",
    "verify_settings", "ticket_settings", "ticket_logs", "logging_settings",
    "invite_settings", "automation_settings",
)


async def _get_server_settings(guild_id: str):
    row = await fetchrow("SELECT settings FROM guild_settings WHERE guild_id = ?", str(guild_id))
    if row:
        settings = row["settings"]
        if isinstance(settings, str):
            try: settings = json.loads(settings)
            except: return dict(SERVER_DEFAULTS)
        if isinstance(settings, dict):
            return {**SERVER_DEFAULTS, **settings}
    return dict(SERVER_DEFAULTS)


@app.get("/api/v1/server/{guild_id}/settings")
async def server_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_server_settings(guild_id), "is_mod": is_mod}


@app.get("/api/v1/server/{guild_id}/info")
async def server_info(guild_id: str, request: Request):
    """Quick summary of the server + feature activation states (read-only)."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    features = {}
    for table in ALL_FEATURE_TABLES:
        if table in ("mod_log", "mod_actions", "ticket_logs", "captcha_codes", "guild_stats_history", "member_history", "message_history"):
            continue
        try:
            raw = await fetchval("SELECT COUNT(*) FROM " + table + " WHERE guild_id = ?", str(guild_id))
            cnt = int(raw) if raw is not None else 0
            features[table] = cnt > 0
        except Exception:
            features[table] = False
    return {
        "server": d,
        "features": features,
    }


@app.post("/api/v1/server/{guild_id}/reset", dependencies=[Depends(require_mod)])
async def server_reset(guild_id: str, request: Request):
    """Wipe all Prowl data for this guild."""
    await require_guild_access(request, guild_id)
    deleted = 0
    for table in ALL_FEATURE_TABLES:
        try:
            await execute("DELETE FROM " + table + " WHERE guild_id = ?", str(guild_id))
            deleted += 1
        except Exception:
            pass
    return {"ok": True, "tables_cleared": deleted}


_ALL_REMOVE_TABLES = ALL_FEATURE_TABLES + (
    "guild_data", "automation_graph", "guild_stats_history",
    "automation_runs", "automation_logs", "captcha_codes",
)


@app.post("/api/v1/server/{guild_id}/remove", dependencies=[Depends(require_mod)])
async def server_remove(guild_id: str, request: Request):
    """Completely remove this server from Prowl: wipe every row + make the bot leave."""
    user = await require_guild_access(request, guild_id)
    deleted = 0
    for table in _ALL_REMOVE_TABLES:
        try:
            await execute("DELETE FROM " + table + " WHERE guild_id = ?", str(guild_id))
            deleted += 1
        except Exception:
            pass
    # Queue Prowl to leave the server (same path as account deletion)
    try:
        await _queue_action(str(guild_id), "leave_guild", str(user.get("id")), user.get("username", "Unknown"), "Server removed from Prowl", None, "System")
    except Exception:
        pass
    return {"ok": True, "tables_cleared": deleted}


# ---------------------------------------------------------------------------
#  API Keys (global - stored in DB, bot reads them, no Vercel needed)
# ---------------------------------------------------------------------------

_API_KEY_NAMES = ("openai", "groq", "openrouter")


def _mask_key(v: str) -> str:
    if not v:
        return ""
    return v[:4] + "\u2026" + v[-4:] if len(v) > 8 else "\u2026"


@app.get("/api/v1/admin/api-keys")
async def get_api_keys(request: Request):
    await require_auth(request)
    rows = await query("SELECT key_name, value FROM api_keys")
    data = {r["key_name"]: _mask_key(r["value"]) for r in rows}
    for k in _API_KEY_NAMES:
        if k not in data:
            data[k] = ""
    return {"keys": data}


@app.post("/api/v1/admin/api-keys")
async def set_api_key(request: Request):
    await require_auth(request)
    body = await request.json()
    key_name = body.get("key_name", "")
    value = (body.get("value") or "").strip()
    if key_name not in _API_KEY_NAMES:
        return JSONResponse({"error": "invalid key_name"}, status_code=400)
    if not value:
        await execute("DELETE FROM api_keys WHERE key_name = ?", key_name)
    else:
        await execute(
            "INSERT INTO api_keys (key_name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key_name) DO UPDATE SET value = ?, updated_at = ?",
            key_name, value, time.time(), value, time.time(),
        )
    return {"ok": True, "key_name": key_name, "masked": _mask_key(value) if value else ""}


# ---------------------------------------------------------------------------
#  Music API v1
# ---------------------------------------------------------------------------

MUSIC_DEFAULTS = {
    "enabled": False,
    "dj_role_id": None,
    "default_volume": 50,
    "announce_channel_id": None,
}


async def _get_music_settings(guild_id: str):
    return await fetchrow_cached("music_settings", "SELECT settings FROM music_settings WHERE guild_id = ?", guild_id, MUSIC_DEFAULTS)


@app.get("/api/v1/music/{guild_id}/settings")
async def music_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_music_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/music/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def music_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    err = await _save_settings("music_settings", str(guild_id), key, value, MUSIC_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/music/{guild_id}/roles")
async def music_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


@app.get("/api/v1/music/{guild_id}/channels")
async def music_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


# ---------------------------------------------------------------------------
#  AI API v1
# ---------------------------------------------------------------------------

AI_DEFAULTS = {
    "enabled": True,
    "model": "gpt-3.5-turbo",
    "system_prompt": "You are a helpful Discord bot named Prowl. Be concise and friendly.",
    "max_tokens": 500,
    "temperature": 0.7,
    "api_keys": {},
}


async def _get_ai_settings(guild_id: str):
    return await fetchrow_cached("ai_settings", "SELECT settings FROM ai_settings WHERE guild_id = ?", guild_id, AI_DEFAULTS)


@app.get("/api/v1/ai/{guild_id}/settings")
async def ai_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    s = await _get_ai_settings(guild_id)
    # Mask API keys so they're never exposed in plaintext
    if s.get("api_keys") and isinstance(s["api_keys"], dict):
        masked = {}
        for k, v in s["api_keys"].items():
            masked[k] = _mask_key(v) if v else ""
        s["api_keys"] = masked
    return {"settings": s}


@app.post("/api/v1/ai/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def ai_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "api_keys":
        # Server-side API keys dict - merge with existing, mask on save
        if not isinstance(value, dict):
            return JSONResponse({"error": "api_keys must be an object"}, status_code=400)
        current = await _get_ai_settings(guild_id)
        existing = current.get("api_keys", {})
        if not isinstance(existing, dict):
            existing = {}
        for k, v in value.items():
            if k not in ("openai", "groq", "openrouter"):
                continue
            v_str = (v or "").strip()
            if v_str:
                existing[k] = v_str
            else:
                existing.pop(k, None)
        value = existing
    err = await _save_settings("ai_settings", str(guild_id), key, value, AI_DEFAULTS)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return {"ok": True}


@app.get("/api/v1/ai/{guild_id}/channels")
async def ai_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


# ---------------------------------------------------------------------------
#  Automation API v1
# ---------------------------------------------------------------------------

_AUTOMATION_RULES_TABLE = """
CREATE TABLE IF NOT EXISTS automation_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    trigger_type TEXT NOT NULL DEFAULT 'member_join',
    trigger_cfg TEXT NOT NULL DEFAULT '{}',
    action_type TEXT NOT NULL DEFAULT 'send_message',
    action_cfg TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_rules_guild ON automation_rules (guild_id);
"""

_AUTOMATION_OVERRIDES_TABLE = """
CREATE TABLE IF NOT EXISTS automation_overrides (
    guild_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (guild_id, feature)
);
"""

VALID_TRIGGERS = ("member_join", "member_leave", "message_contains", "message_starts", "role_added", "role_removed")
VALID_ACTIONS = ("send_message", "send_dm", "add_role", "remove_role", "kick", "ban", "mute")


async def _ensure_automation_tables():
    try:
        await execute(_AUTOMATION_RULES_TABLE)
    except Exception:
        pass
    try:
        await execute(_AUTOMATION_OVERRIDES_TABLE)
    except Exception:
        pass


@app.get("/api/v1/automation/{guild_id}/rules")
async def automation_rules_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT id, name, trigger_type, trigger_cfg, action_type, action_cfg FROM automation_rules "
        "WHERE guild_id = ? ORDER BY created_at ASC",
        str(guild_id),
    )
    rules = []
    for r in rows:
        rules.append({
            "id": r["id"],
            "name": r["name"],
            "trigger_type": r["trigger_type"],
            "trigger_cfg": r["trigger_cfg"] if isinstance(r["trigger_cfg"], dict) else json.loads(r["trigger_cfg"] or "{}"),
            "action_type": r["action_type"],
            "action_cfg": r["action_cfg"] if isinstance(r["action_cfg"], dict) else json.loads(r["action_cfg"] or "{}"),
        })
    return {"rules": rules}


@app.post("/api/v1/automation/{guild_id}/rules", dependencies=[Depends(require_mod)])
async def automation_rule_add(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    await _ensure_automation_tables()
    body = await request.json()
    name = str(body.get("name", "") or "").strip()[:100]
    trigger_type = body.get("trigger_type", "")
    action_type = body.get("action_type", "")
    trigger_cfg = body.get("trigger_cfg", {})
    action_cfg = body.get("action_cfg", {})
    if not name or trigger_type not in VALID_TRIGGERS or action_type not in VALID_ACTIONS:
        return JSONResponse({"error": "invalid name, trigger_type or action_type"}, status_code=400)
    row = await query(
        "INSERT INTO automation_rules (guild_id, name, trigger_type, trigger_cfg, action_type, action_cfg) "
        "VALUES (?,?,?,?,?,?) RETURNING id",
        str(guild_id), name, trigger_type, json.dumps(trigger_cfg), action_type, json.dumps(action_cfg),
    )
    return {"ok": True, "id": row[0]["id"] if row else None}


@app.put("/api/v1/automation/{guild_id}/rules/{rule_id}", dependencies=[Depends(require_mod)])
async def automation_rule_edit(guild_id: str, rule_id: str, request: Request):
    await require_guild_access(request, guild_id)
    await _ensure_automation_tables()
    body = await request.json()
    name = str(body.get("name", "") or "").strip()[:100]
    trigger_type = body.get("trigger_type", "")
    action_type = body.get("action_type", "")
    trigger_cfg = body.get("trigger_cfg", {})
    action_cfg = body.get("action_cfg", {})
    if not name or trigger_type not in VALID_TRIGGERS or action_type not in VALID_ACTIONS:
        return JSONResponse({"error": "invalid name, trigger_type or action_type"}, status_code=400)
    try:
        rid = int(rule_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid id"}, status_code=400)
    await execute(
        "UPDATE automation_rules SET name=?, trigger_type=?, trigger_cfg=?, action_type=?, action_cfg=? "
        "WHERE id=? AND guild_id=?",
        name, trigger_type, json.dumps(trigger_cfg), action_type, json.dumps(action_cfg), rid, str(guild_id),
    )
    return {"ok": True}


@app.delete("/api/v1/automation/{guild_id}/rules/{rule_id}", dependencies=[Depends(require_mod)])
async def automation_rule_delete(guild_id: str, rule_id: str, request: Request):
    await require_guild_access(request, guild_id)
    try:
        rid = int(rule_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid id"}, status_code=400)
    await execute("DELETE FROM automation_rules WHERE id=? AND guild_id=?", rid, str(guild_id))
    return {"ok": True}


@app.get("/api/v1/automation/{guild_id}/overrides")
async def automation_overrides_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    rows = await query(
        "SELECT feature, enabled FROM automation_overrides WHERE guild_id = ?",
        str(guild_id),
    )
    return {"overrides": {r["feature"]: r["enabled"] for r in rows}}


@app.post("/api/v1/automation/{guild_id}/overrides", dependencies=[Depends(require_mod)])
async def automation_overrides_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    await _ensure_automation_tables()
    body = await request.json()
    key = body.get("key", "")
    value = bool(body.get("value"))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    await execute(
        "INSERT INTO automation_overrides (guild_id, feature, enabled, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, feature) DO UPDATE SET enabled=?, updated_at=?",
        str(guild_id), key, value, time.time(), value, time.time(),
    )
    return {"ok": True}


@app.get("/api/v1/automation/{guild_id}/channels")
async def automation_channels(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


@app.get("/api/v1/automation/{guild_id}/roles")
async def automation_roles(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "roles" in d:
        return {"roles": [{"id": str(r.get("id")), "name": r.get("name", "")} for r in d["roles"]]}
    return {"roles": []}


_AUTO_SAVE_LIMIT = {}
_AUTO_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS automation_runs (
    guild_id    TEXT NOT NULL,
    bucket_ts   REAL NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, bucket_ts)
);
"""
_AUTO_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS automation_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_logs_guild ON automation_logs (guild_id, id DESC);
"""


@app.get("/api/v1/automation/{guild_id}/graph")
async def automation_graph_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    row = await fetchrow("SELECT nodes, connections FROM automation_graph WHERE guild_id = ?", str(guild_id))
    if row:
        return {"nodes": row["nodes"] if isinstance(row["nodes"], list) else json.loads(row["nodes"] or "[]"),
                "connections": row["connections"] if isinstance(row["connections"], list) else json.loads(row["connections"] or "[]")}
    return {"nodes": [], "connections": []}


@app.post("/api/v1/automation/{guild_id}/graph", dependencies=[Depends(require_mod)])
async def automation_graph_save(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    # Rate-limit saves (5/min per guild) so the editor can't be spammed
    now = time.time()
    last = _AUTO_SAVE_LIMIT.get(str(guild_id), 0)
    if now - last < 12:
        return JSONResponse({"error": "Saving too quickly - try again in a moment."}, status_code=429)
    _AUTO_SAVE_LIMIT[str(guild_id)] = now
    body = await request.json()
    nodes = body.get("nodes", [])
    connections = body.get("connections", [])
    await execute(
        "INSERT INTO automation_graph (guild_id, nodes, connections, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT (guild_id) DO UPDATE SET nodes=?, connections=?, updated_at=?",
        str(guild_id), json.dumps(nodes), json.dumps(connections), time.time(),
        json.dumps(nodes), json.dumps(connections), time.time(),
    )
    return {"ok": True}


@app.get("/api/v1/automation/{guild_id}/usage")
async def automation_usage(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    try:
        await execute(_AUTO_USAGE_SQL)
    except Exception:
        pass
    since = int(time.time() // 3600) * 3600 - 23 * 3600
    rows = await query("SELECT bucket_ts, count FROM automation_runs WHERE guild_id = ? AND bucket_ts >= ? ORDER BY bucket_ts ASC", str(guild_id), since)
    by = {int(r["bucket_ts"]): int(r["count"]) for r in rows}
    points = []
    for i in range(24):
        b = since + i * 3600
        points.append({"t": b, "count": by.get(b, 0)})
    return {"points": points}


@app.get("/api/v1/automation/{guild_id}/logs")
async def automation_logs(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    try:
        await execute(_AUTO_LOGS_SQL)
    except Exception:
        pass
    rows = await query("SELECT id, message, created_at FROM automation_logs WHERE guild_id = ? ORDER BY id DESC LIMIT 30", str(guild_id))
    return {"logs": [{"id": r["id"], "message": r["message"], "time": _relative_time(r["created_at"])} for r in rows]}


@app.get("/api/v1/more/{guild_id}/channels")
async def more_channels(guild_id: str, request: Request):
    """Text channels for the sticky message channel dropdown."""
    await require_guild_access(request, guild_id)
    d = await get_guild_data(guild_id)
    if d and "channels" in d:
        return {"channels": [{"id": str(c.get("id")), "name": c.get("name", "")} for c in d["channels"] if c.get("type", 0) == 0]}
    return {"channels": []}


async def _get_more_settings(guild_id: str):
    """Load sticky-message settings from bot_stats."""
    out = {"sticky_enabled": False, "sticky_messages": []}
    row = await fetchrow("SELECT value FROM bot_stats WHERE key = ?", f"more_sticky_enabled_{guild_id}")
    if row and row["value"]:
        out["sticky_enabled"] = row["value"] == "1"
    row2 = await fetchrow("SELECT value FROM bot_stats WHERE key = ?", f"more_sticky_messages_{guild_id}")
    if row2 and row2["value"]:
        try:
            out["sticky_messages"] = json.loads(row2["value"])
        except Exception:
            out["sticky_messages"] = []
    return out


@app.get("/api/v1/more/{guild_id}/settings")
async def more_settings_get(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    is_mod = await is_guild_moderator(request, guild_id)
    return {"settings": await _get_more_settings(guild_id), "is_mod": is_mod}


@app.post("/api/v1/more/{guild_id}/settings", dependencies=[Depends(require_mod)])
async def more_settings_set(guild_id: str, request: Request):
    await require_guild_access(request, guild_id)
    body = await request.json()
    key = body.get("key")
    value = body.get("value")
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if key == "sticky_enabled":
        value = "1" if value else "0"
    elif key == "sticky_messages":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = []
        if not isinstance(value, list):
            return JSONResponse({"error": "sticky_messages must be a list"}, status_code=400)
        value = json.dumps(value)
    else:
        return JSONResponse({"error": f"unknown key: {key}"}, status_code=400)
    stat_key = f"more_{key}_{guild_id}"
    await execute(
        "INSERT INTO bot_stats (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        stat_key, str(value), time.time(),
    )
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="127.0.0.1", port=8000, reload=True)
