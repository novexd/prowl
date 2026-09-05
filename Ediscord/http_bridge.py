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

Set BOT_HTTP_TOKEN in cli/.env and the website env. Port defaults to 24612
(BOT_HTTP_PORT). The website must be able to reach http://<host>:<port>.
"""

import os
import re
import time
import hmac
import asyncio
import logging

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
    port = int(os.environ.get("BOT_HTTP_PORT", "24612"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"HTTP bridge listening on 0.0.0.0:{port}.")
    # Eagerly load the model in the background (never blocks the bot loop).
    if semantic_search_service.enabled:
        asyncio.ensure_future(semantic_search_service.ensure_loaded())
