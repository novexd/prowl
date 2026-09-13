"""
Direct HTTP bridge for the dashboard.

Lets the website send moderation quick-actions straight to the bot process,
bypassing the ~5s DB queue poll. Requests are authorized with a shared secret
token (BOT_HTTP_TOKEN) that lives only in the bot's and the website's server
environments - never in browser JS.

Endpoints:
  GET  /health            -> {"ok": true, "bot": ..., "guilds": N}
  POST /api/action        -> execute a moderation quick-action immediately
  GET  /api/stats/actions -> last 24h hourly dashboard-action counts (in-memory)
  POST /api/rank_preview  -> enqueue a rank card render, returns {job_id} fast
  GET  /api/rank_preview/{job_id} -> poll: {ready:false} or the PNG bytes
                           (website rank-editor preview; strictly validated)

Set BOT_HTTP_TOKEN in cli/.env and the website env. Port defaults to 24612
(BOT_HTTP_PORT). The website must be able to reach http://<host>:<port>.
"""

import os
import re
import time
import hmac
import asyncio
import logging
import uuid

import discord
from aiohttp import web

from semantic_search import semantic_search_service
from Ediscord.cache import settings_cache

logger = logging.getLogger(__name__)

# Semantic search defensive limits (shared with the service's MAX_QUERY_CHARS).
SEMANTIC_MAX_QUERY_CHARS = 500

_bot = None

# Actions that may be executed directly. Everything else keeps using the queue.
DIRECT_ACTIONS = ("mute", "unmute", "kick", "ban", "add_role", "remove_role", "nickname", "purge", "emergency_lock", "emergency_unlock", "verify_panel", "verify_panel_remove", "verify_user", "panel_send")

# In-memory per-hour dashboard action counters. The status page polls these via
# /api/stats/actions so it can render a "bot actions" graph. Not persisted on
# purpose - it's a live view since the last bot restart.
_ACTION_BUCKETS = {}
_ACTION_BUCKET_HOURS = 48


def set_bot(bot):
    global _bot
    _bot = bot


def record_action():
    """Count one dashboard action executed by the bot (in-memory, hourly)."""
    bucket = int(time.time() // 3600) * 3600
    _ACTION_BUCKETS[bucket] = _ACTION_BUCKETS.get(bucket, 0) + 1
    cutoff = bucket - _ACTION_BUCKET_HOURS * 3600
    for k in [k for k in _ACTION_BUCKETS if k < cutoff]:
        del _ACTION_BUCKETS[k]


def action_stats():
    """Last 24 hourly buckets, zero-filled: [{"t": ts, "count": n}, ...]."""
    start = int(time.time() // 3600) * 3600 - 23 * 3600
    return [
        {"t": start + i * 3600, "count": _ACTION_BUCKETS.get(start + i * 3600, 0)}
        for i in range(24)
    ]


def _get_token():
    return os.environ.get("BOT_HTTP_TOKEN", "")


async def _check_auth(request) -> bool:
    token = _get_token()
    if not token:
        return False
    supplied = request.headers.get("X-Prowl-Token", "")
    return hmac.compare_digest(token, supplied)


async def handle_health(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return web.json_response({
        "ok": True,
        "bot": _bot.user.name if _bot and _bot.user else None,
        "guilds": len(_bot.guilds) if _bot else 0,
        "ready": bool(_bot and _bot.is_ready()),
    })


async def handle_action(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if _bot is None or not _bot.is_ready():
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    action = body.get("action")
    guild_id = str(body.get("guild_id", ""))
    user_id = body.get("user_id")
    if action not in DIRECT_ACTIONS or not guild_id or not user_id:
        return web.json_response({"ok": False, "error": "invalid action, guild_id or user_id"}, status=400)

    guild = _bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
    if guild is None:
        return web.json_response({"ok": False, "error": "bot not in guild"}, status=404)

    ok, message = await _bot.execute_action(
        guild_id,
        action,
        user_id,
        target_name=str(body.get("target") or body.get("user_name") or ""),
        reason=str(body.get("reason") or "No reason provided"),
        duration=body.get("duration"),
        moderator=str(body.get("moderator") or "Dashboard"),
        request_id=str(body.get("request_id") or ""),
    )
    return web.json_response({"ok": ok, "message": message}, status=200 if ok else 400)


async def handle_action_stats(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return web.json_response({"actions": action_stats()})


# ── Rank card live preview ──────────────────────────────────────────────
# Renders with the real image_builder so website previews are pixel-identical
# to final cards. Pure render: needs no gateway connection, only the process.
PREVIEW_ELEMENTS = ("panel", "avatar", "name", "xp_value", "rank", "xp_bar", "xp_ratio")
PREVIEW_AVATAR_MAX_BYTES = 8 * 1024 * 1024
PREVIEW_POS_BOUNDS = {
    "x": (0, 900), "y": (0, 300), "size": (16, 512),
    "width": (50, 900), "height": (4, 100),
}


class _PreviewAvatar:
    """discord.Asset stand-in backed by a Discord CDN URL."""

    def __init__(self, url: str):
        self._url = url

    async def read(self) -> bytes:
        return await _fetch_preview_avatar(self._url)


# Preview avatar bytes cache: the editor re-renders the same avatar URL on
# every change, so repeat previews skip the Discord CDN round-trip (~0.5s).
_PREVIEW_AVATAR_CACHE = {}
_PREVIEW_AVATAR_TTL = 600
_PREVIEW_AVATAR_MAX = 32


async def _fetch_preview_avatar(url: str) -> bytes:
    import aiohttp
    import time
    now = time.monotonic()
    hit = _PREVIEW_AVATAR_CACHE.get(url)
    if hit is not None and now - hit[0] < _PREVIEW_AVATAR_TTL:
        return hit[1]
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
            if resp.status != 200:
                raise ValueError(f"avatar fetch failed: {resp.status}")
            data = await resp.read()
    if len(data) > PREVIEW_AVATAR_MAX_BYTES:
        raise ValueError("avatar too large")
    _PREVIEW_AVATAR_CACHE[url] = (now, data)
    while len(_PREVIEW_AVATAR_CACHE) > _PREVIEW_AVATAR_MAX:
        _PREVIEW_AVATAR_CACHE.pop(next(iter(_PREVIEW_AVATAR_CACHE)), None)
    return data


class _PreviewUser:
    """Minimal duck-typed stand-in for discord.Member/User (preview only)."""

    def __init__(self, user_id: int, display_name: str, avatar_url: str):
        self.id = user_id
        self.display_name = display_name
        self.display_avatar = _PreviewAvatar(avatar_url)


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _sanitize_preview_config(raw_cfg, manifest_ids: set) -> dict:
    """Strictly validate an editor-supplied rank_card config.

    Backgrounds are restricted to manifest ids / "random" / null or legacy
    local filenames (containment-checked at open time), plus solid/gradient
    dicts. Raw http(s) URLs are rejected here (SSRF) even though the renderer
    itself can fetch them. Colors accept RGBA lists (alpha kept) or gradient
    dicts; elements accept a primary/accent color source.
    """
    from components.image_builder import (
        _open_background_name, _sanitize_gradient, _sanitize_rgba_list,
        WHITE, LEGACY_SOLID_BG, RANKS_DEFAULT_CONFIG,
    )
    if not isinstance(raw_cfg, dict):
        raise ValueError("config must be an object")
    bg = raw_cfg.get("background", "random")
    if bg is None or bg == "random":
        clean_bg = bg
    elif isinstance(bg, dict):
        mode = bg.get("mode")
        if mode == "solid":
            clean_bg = {"mode": "solid", "color": _sanitize_rgba_list(bg.get("color"), LEGACY_SOLID_BG)}
        elif mode == "gradient":
            g = _sanitize_gradient(bg)
            if g is None:
                raise ValueError("invalid background gradient")
            clean_bg = {"mode": "gradient", "direction": g["direction"],
                        "colors": [list(c) for c in g["colors"]]}
        else:
            raise ValueError("invalid background")
    elif isinstance(bg, str) and (bg in manifest_ids or _open_background_name(bg) is not None):
        clean_bg = bg
    else:
        raise ValueError("invalid background")

    def _clean_slot(v, what, dflt=(255, 255, 255, 255)):
        if isinstance(v, dict):
            g = _sanitize_gradient(v)
            if g is None:
                raise ValueError(f"invalid {what} gradient")
            return {"direction": g["direction"], "colors": [list(c) for c in g["colors"]]}
        return _sanitize_rgba_list(v, dflt)

    clean = {
        "background": clean_bg,
        "primary_color": _clean_slot(raw_cfg.get("primary_color"), "primary"),
        "accent_color": _clean_slot(raw_cfg.get("accent_color"), "accent"),
        "panel_color": _clean_slot(raw_cfg.get("panel_color"), "panel", (10, 10, 16, 170)),
        "elements": {},
    }
    raw_elements = raw_cfg.get("elements") or {}
    if not isinstance(raw_elements, dict):
        raw_elements = {}
    for name in RANKS_DEFAULT_CONFIG["elements"]:
        raw_el = raw_elements.get(name) or {}
        if not isinstance(raw_el, dict):
            raw_el = {}
        el = {"enabled": bool(raw_el.get("enabled", True))}
        if raw_el.get("color") in ("primary", "accent"):
            el["color"] = raw_el["color"]
        for field, (lo, hi) in PREVIEW_POS_BOUNDS.items():
            if field in raw_el and raw_el[field] is not None:
                el[field] = _clamp_int(raw_el[field], lo, hi, None)
                if el[field] is None:
                    del el[field]
        clean["elements"][name] = el
    return clean


# Preview jobs: dispatch returns instantly; the render runs detached so slow
# first-renders (cold background downloads) can never trap the caller in a
# serverless timeout. Results are PNG bytes held briefly in memory.
_PREVIEW_JOBS = {}
_PREVIEW_JOB_TTL = 300
_PREVIEW_JOB_MAX = 50


def _preview_job_sweep():
    now = time.monotonic()
    for jid in [k for k, j in _PREVIEW_JOBS.items() if now - j["at"] > _PREVIEW_JOB_TTL]:
        _PREVIEW_JOBS.pop(jid, None)
    while len(_PREVIEW_JOBS) > _PREVIEW_JOB_MAX:
        _PREVIEW_JOBS.pop(next(iter(_PREVIEW_JOBS)), None)


async def _run_preview_job(job_id: str, clean: dict, pv: dict):
    job = _PREVIEW_JOBS.get(job_id)
    if job is None:
        return
    try:
        from components.image_builder import create_rank_card
        try:
            uid = int(pv.get("user_id") or 0)
        except (TypeError, ValueError):
            uid = 0
        name = str(pv.get("display_name") or "User")[:32]
        avatar_url = str(pv.get("avatar_url") or "")
        if not avatar_url.startswith("https://cdn.discordapp.com/"):
            avatar_url = ""
        user = _PreviewUser(uid, name, avatar_url)
        buf = await create_rank_card(
            user,
            _clamp_int(pv.get("level"), 1, 9999, 12),
            _clamp_int(pv.get("xp"), 0, 10 ** 12, 2470),
            _clamp_int(pv.get("xp_needed"), 0, 10 ** 12, 150),
            _clamp_int(pv.get("rank"), 1, 10 ** 9, 3),
            _clamp_int(pv.get("total_members"), 1, 10 ** 9, 284),
            guild_name="Preview",
            config=clean,
            png_optimize=False,
        )
        job["png"] = buf.getvalue()
        job["ready"] = True
    except Exception as e:
        logger.warning(f"Rank preview render failed: {e}")
        job["error"] = "render failed"
        job["ready"] = True


async def handle_rank_preview(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    try:
        from components.image_builder import _load_manifest_entries
        entries = await _load_manifest_entries()
        clean = _sanitize_preview_config(body.get("config"), {e["id"] for e in entries})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e) or "invalid config"}, status=400)
    except Exception as e:
        logger.warning(f"Rank preview validation failed: {e}")
        return web.json_response({"ok": False, "error": "invalid config"}, status=400)
    pv = body.get("preview") or {}
    if not isinstance(pv, dict):
        pv = {}
    _preview_job_sweep()
    job_id = uuid.uuid4().hex
    _PREVIEW_JOBS[job_id] = {"at": time.monotonic(), "ready": False, "png": None, "error": None}
    asyncio.create_task(_run_preview_job(job_id, clean, pv))
    return web.json_response({"ok": True, "job_id": job_id})


async def handle_rank_preview_result(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    job_id = request.match_info.get("job_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        return web.json_response({"ok": False, "error": "invalid job_id"}, status=400)
    _preview_job_sweep()
    job = _PREVIEW_JOBS.get(job_id)
    if job is None:
        return web.json_response({"ok": False, "error": "unknown or expired job"}, status=404)
    if not job["ready"]:
        return web.json_response({"ok": True, "ready": False})
    if job["error"]:
        return web.json_response({"ok": False, "error": job["error"]}, status=500)
    return web.Response(body=job["png"], content_type="image/png")


# ── Per-guild bot profile (nickname / avatar / banner) ──

MAX_IMAGE_DATA_CHARS = 14_000_000  # base64 data URI length cap (~10MB binary)

# Appended under the bio server-side whenever a description is saved.
BIO_SUFFIX = "powered by prowl"
_BIO_SUFFIX_RE = re.compile(r"\s*" + re.escape(BIO_SUFFIX) + r"\s*$")


def _apply_bio_suffix(bio: str) -> str:
    """Append the branded footer under the user's bio, within Discord's 350 cap."""
    bio = (bio or "").rstrip()
    if not bio:
        return ""
    room = 350 - len(BIO_SUFFIX) - 2
    return bio[:room].rstrip() + "\n\n" + BIO_SUFFIX


def _strip_bio_suffix(bio):
    """Inverse of _apply_bio_suffix so the editor never sees (or re-appends) it."""
    return _BIO_SUFFIX_RE.sub("", bio).strip() if bio else None


def _profile_payload(me) -> dict:
    """Current per-guild profile of the bot in a guild."""
    user = _bot.user
    return {
        "ok": True,
        "nick": me.nick,
        "name": user.name if user else None,
        "global_avatar_url": str(user.display_avatar.replace(size=256)) if user else None,
        "avatar_url": str(me.guild_avatar) if me.guild_avatar else None,
        "banner_url": str(me.guild_banner) if me.guild_banner else None,
        "bio": _strip_bio_suffix(getattr(me, "bio", None)),
    }


async def handle_profile_get(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if _bot is None or not _bot.is_ready():
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)
    guild_id = request.query.get("guild_id", "")
    guild = _bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
    if guild is None:
        return web.json_response({"ok": False, "error": "bot not in guild"}, status=404)
    return web.json_response(_profile_payload(guild.me))


async def handle_profile_post(request):
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if _bot is None or not _bot.is_ready():
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    guild_id = str(body.get("guild_id", ""))
    guild = _bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
    if guild is None:
        return web.json_response({"ok": False, "error": "bot not in guild"}, status=404)

    payload = {}
    if "nick" in body:
        nick = str(body.get("nick") or "").strip()
        if len(nick) > 32:
            return web.json_response({"ok": False, "error": "Nickname must be 32 characters or fewer."}, status=400)
        payload["nick"] = nick or None
    if "bio" in body:
        bio = str(body.get("bio") or "").strip()
        if len(bio) > 350:
            return web.json_response({"ok": False, "error": "Bio must be 350 characters or fewer."}, status=400)
        payload["bio"] = _apply_bio_suffix(bio) or None
    for key in ("avatar", "banner"):
        if body.get(f"reset_{key}"):
            payload[key] = None
            continue
        data = body.get(key)
        if data:
            data = str(data)
            if not data.startswith("data:image/"):
                return web.json_response({"ok": False, "error": f"Invalid {key} image data."}, status=400)
            if len(data) > MAX_IMAGE_DATA_CHARS:
                return web.json_response({"ok": False, "error": f"{key.capitalize()} must be under 10MB."}, status=400)
            payload[key] = data

    if not payload:
        return web.json_response({"ok": False, "error": "Nothing to update."}, status=400)

    try:
        me = await guild.me.edit(**payload, reason="Dashboard: bot profile update")
    except discord.Forbidden:
        return web.json_response({"ok": False, "error": "Discord denied the change (missing permission?)."}, status=403)
    except discord.HTTPException as e:
        logger.warning(f"Bot profile update failed in {guild_id}: {e}")
        return web.json_response({"ok": False, "error": f"Discord rejected the update ({e.status})."}, status=400)
    return web.json_response(_profile_payload(me))


async def handle_semantic_search(request):
    """POST /semantic-search - rank dashboard pages for a natural-language query.

    Auth reuse: same BOT_HTTP_TOKEN (X-Prowl-Token) as the rest of the bridge.
    Defensive validation only - never leak internal traces to the client.
    """
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if not semantic_search_service.enabled:
        return web.json_response(
            {"ok": False, "error": "semantic search disabled", "results": []}, status=503
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    query = (body.get("query") or "").strip()
    if not query:
        return web.json_response({"ok": False, "error": "missing query"}, status=400)
    if len(query) > SEMANTIC_MAX_QUERY_CHARS:
        return web.json_response({"ok": False, "error": "query too long"}, status=413)

    try:
        results = await semantic_search_service.search(query)
    except Exception as e:
        logger.error("Semantic search request failed: %s", e)
        return web.json_response(
            {"ok": False, "error": "search failed", "results": []}, status=500
        )
    return web.json_response({"ok": True, "results": results})


async def handle_cache_invalidate(request):
    """POST /cache/invalidate - drop cached settings for a guild/table.

    Triggered by the Vercel dashboard after it writes to Turso. Authorization
    reuses the same shared secret (X-Prowl-Token) as the rest of the bridge.

    Body options:
      {"all": true}                       -> clear the entire cache
      {"guild_id": "123"}                 -> clear all tables for a guild
      {"table": "mod_settings", "guild_id": "123"} -> clear one table/guild
    """
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    if body.get("all"):
        await settings_cache.invalidate_all()
        return web.json_response({"ok": True, "invalidated": "all"})

    guild_id = str(body.get("guild_id", ""))
    if not guild_id:
        return web.json_response({"ok": False, "error": "guild_id required"}, status=400)

    table = body.get("table")
    if table:
        await settings_cache.invalidate((table, guild_id))
        return web.json_response({"ok": True, "invalidated": [table, guild_id]})

    await settings_cache.invalidate_prefix(guild_id)
    return web.json_response({"ok": True, "invalidated": "guild:" + guild_id})


async def handle_gc_deploy_panel(request):
    """Deploy the Global Chat control panel embed to the management channel."""
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if _bot is None or not _bot.is_ready():
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    guild_id = body.get("guild_id")
    if not guild_id:
        return web.json_response({"ok": False, "error": "guild_id required"}, status=400)
    try:
        from components.global_chat import _refresh_panel
        await _refresh_panel(_bot, int(guild_id))
        return web.json_response({"ok": True, "message": "Panel deployed"})
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)


async def handle_gc_remove_panel(request):
    """Remove the Global Chat control panel from the management channel."""
    if not await _check_auth(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if _bot is None or not _bot.is_ready():
        return web.json_response({"ok": False, "error": "bot not ready"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    guild_id = body.get("guild_id")
    if not guild_id:
        return web.json_response({"ok": False, "error": "guild_id required"}, status=400)
    try:
        from components.global_chat import _remove_panel
        await _remove_panel(_bot, int(guild_id))
        return web.json_response({"ok": True, "message": "Panel removed"})
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)


async def start_http_server():
    """Start the aiohttp bridge. No-op (with a warning) if BOT_HTTP_TOKEN unset."""
    token = _get_token()
    if not token:
        logger.warning("BOT_HTTP_TOKEN not set - direct dashboard HTTP bridge disabled.")
        return
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/action", handle_action)
    app.router.add_get("/api/stats/actions", handle_action_stats)
    app.router.add_get("/api/profile", handle_profile_get)
    app.router.add_post("/api/profile", handle_profile_post)
    app.router.add_post("/cache/invalidate", handle_cache_invalidate)
    app.router.add_post("/semantic-search", handle_semantic_search)
    app.router.add_post("/api/gc/deploy_panel", handle_gc_deploy_panel)
    app.router.add_post("/api/gc/remove_panel", handle_gc_remove_panel)
    app.router.add_post("/api/rank_preview", handle_rank_preview)
    app.router.add_get("/api/rank_preview/{job_id}", handle_rank_preview_result)
    port = int(os.environ.get("BOT_HTTP_PORT", "24612"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"HTTP bridge listening on 0.0.0.0:{port}.")
    # Eagerly load the model in the background (never blocks the bot loop).
    if semantic_search_service.enabled:
        asyncio.ensure_future(semantic_search_service.ensure_loaded())
