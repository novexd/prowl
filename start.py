"""
Prowl - Entry Point
Run this script to start the bot.
"""

import os
import sys
import time
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env.local")
load_dotenv(Path(__file__).parent / ".env")

import discord
from discord.ext import commands
import psutil
import json
import datetime

from Ediscord import variables, logger, utils, __version__
from Ediscord import db as neon_db
from Ediscord import http_bridge


COGS_DIR = Path(__file__).parent / "components"

CUSTOM_STATUSES = [
    "im a bot bro",
    "looking at servers probably",
    "prowlbot.xyz",
    "/help | you'll need it",
    "#grrrrrrr mondays",
    "OBSERVING {guilds} servers",
    "hey can i have this?",
    "tracking {users} users",
]


class ProwlBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="",
            intents=variables.intents,
            activity=discord.Game("starting up..."),
            status=discord.Status.online,
        )
        self.version = __version__
        self.launch_time = 0

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # Skip prefix-command processing — Prowl uses slash commands only.
        # Cog on_message listeners still fire via dispatch.

    async def setup_hook(self):
        await self.load_cogs()
        synced = await self.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands.")
        self.loop.create_task(self._dashboard_writer())
        self.loop.create_task(self._initial_neon_push())
        self.loop.create_task(self._stats_syncer())
        self.loop.create_task(self._neon_syncer())
        self.loop.create_task(self._member_sync())
        self.loop.create_task(self._mod_settings_poller())
        self.loop.create_task(self._mod_action_processor())
        # Direct HTTP bridge so the dashboard can skip the ~5s DB queue poll.
        http_bridge.set_bot(self)
        self.loop.create_task(http_bridge.start_http_server())
        logger.info("Setup hook complete.")

    async def _initial_neon_push(self):
        await self.wait_until_ready()
        import os as _os
        if not (_os.environ.get("TURSO_DATABASE_URL") or _os.environ.get("DATABASE_URL")):
            logger.warning("TURSO_DATABASE_URL not set - bot won't push guild data. Set it in cli/.env")
            return
        try:
            await self._push_to_neon()
            logger.info("Initial DB push complete.")
        except Exception as e:
            logger.error(f"Initial DB push failed: {e}")

    async def load_cogs(self):
        for file in COGS_DIR.glob("*.py"):
            if file.name.startswith("_"):
                continue
            cog_name = f"components.{file.stem}"
            try:
                await self.load_extension(cog_name, package=str(COGS_DIR.parent))
                logger.info(f"Loaded cog: {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load {cog_name}: {e}")

    async def on_ready(self):
        logger.info(f"Prowl is online! Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Servers: {len(self.guilds)} | Users: {len(self.users)}")
        self.launch_time = time.time()
        utils.write_bot_data(self)
        self._status_index = 0
        await self._rotate_status()
        self.loop.create_task(self._status_rotator())

    async def _rotate_status(self):
        text = CUSTOM_STATUSES[self._status_index % len(CUSTOM_STATUSES)].format(guilds=len(self.guilds), users=len(self.users))
        activity = discord.CustomActivity(name=text)
        await self.change_presence(status=discord.Status.online, activity=activity)
        self._status_index += 1

    async def _status_rotator(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(300)
            try:
                await self._rotate_status()
            except Exception as e:
                logger.error(f"Status rotation failed: {e}")

    async def on_guild_remove(self, guild: discord.Guild):
        """Clean up a guild's data when the bot is removed/kicked."""
        logger.info(f"Left/Kicked from guild: {guild.name} ({guild.id}) - deleting data.")
        try:
            await neon_db.delete_guild_data(guild.id)
        except Exception as e:
            logger.error(f"Failed to delete data for {guild.id}: {e}")

    async def _dashboard_writer(self):
        """Periodically write bot data for the dashboard."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                utils.write_bot_data(self)
            except Exception as e:
                logger.error(f"Dashboard write failed: {e}")
            await asyncio.sleep(60)

    async def _stats_syncer(self):
        """Push lightweight bot stats every 60s to keep the dashboard fresh without burning CPU."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await neon_db.push_bot_stats(await self._build_stats())
            except Exception as e:
                logger.error(f"Stats sync failed: {e}")
            await asyncio.sleep(60)

    async def _neon_syncer(self):
        """Push full guild data (and stats) to the database every 60s."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._push_to_neon()
            except Exception as e:
                logger.error(f"DB sync failed: {e}")
            await asyncio.sleep(60)

    async def _member_sync(self):
        """Lightweight sync: update member names/roles/joins in guild_data every 60s as a temporary CPU cap."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    await self._sync_guild_members(guild)
            except Exception as e:
                logger.debug(f"Member sync failed: {e}")
            await asyncio.sleep(60)

    async def _sync_guild_members(self, guild):
        pool = await neon_db.get_pool()
        if pool is None:
            return
        members = [{
            "id": str(m.id), "name": m.name, "display_name": m.display_name,
            "avatar_url": str(m.display_avatar.url),
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "roles": [str(r.id) for r in m.roles[1:]],
            "is_raider": False,
        } for m in guild.members]
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE guild_data SET data = json_set(data, '$.members', json(?)), updated_at = ? WHERE guild_id = ?",
                    json.dumps(members), time.time(), str(guild.id),
                )
        except Exception as e:
            logger.debug(f"Sync members for {guild.id} failed: {e}")

    async def _mod_settings_poller(self):
        """Poll mod settings less aggressively to reduce DB CPU burn."""
        await self.wait_until_ready()
        self._mod_cache = {}
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    gid = str(guild.id)
                    new = await neon_db.fetch_mod_settings(gid)
                    old = self._mod_cache.get(gid)
                    if old is None:
                        if new.get("mod_roles"):
                            logger.info(f"[Settings] {guild.name}: current mod roles -> {new.get('mod_roles')}")
                    elif new.get("mod_roles") != old.get("mod_roles"):
                        logger.info(f"[Settings] {guild.name}: mod roles changed -> {new.get('mod_roles', [])}")
                    self._mod_cache[gid] = new
            except Exception as e:
                logger.error(f"Mod settings poller failed: {e}")
            await asyncio.sleep(120)

    async def _mod_action_processor(self):
        """Process queued actions via polling."""
        await self.wait_until_ready()
        from components.verification import get_verify_settings  # noqa: F811
        self._processing_actions = False
        self._last_stale_reap = 0
        while not self.is_closed():
            try:
                await self._reap_stale_actions()
                await self._process_pending()
            except Exception as e:
                logger.error(f"Mod action processor failed: {e}")
            await asyncio.sleep(3)

    async def _reap_stale_actions(self):
        """Mark actions stuck in 'executing' for >2min as 'failed'."""
        now = time.time()
        if now - self._last_stale_reap < 60:
            return
        self._last_stale_reap = now
        try:
            pool = await neon_db.get_pool()
            if pool is None:
                return
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, request_id, action, target_id, guild_id FROM mod_actions "
                    "WHERE status = 'executing' AND created_at < ? LIMIT 20",
                    now - 120,
                )
                for r in rows:
                    await neon_db.update_action_status(r["request_id"] or "", "failed", "bot timed out")
                    logger.warning(f"Reaped stale action {r['id']}: {r['action']} on {r['target_id']}")
        except Exception as e:
            logger.error(f"Stale action reap failed: {e}")

    async def _process_pending(self):
        if self._processing_actions:
            return
        self._processing_actions = True
        try:
            actions = await neon_db.fetch_pending_actions()
            if actions:
                logger.info(f"Mod action processor: {len(actions)} pending action(s).")
            for a in actions:
                act = a["action"]
                guild = self.get_guild(int(a["guild_id"]))
                if not guild:
                    await neon_db.complete_action(a["id"], "skipped")
                    logger.warning(f"Action {a['id']} skipped: guild not found.")
                    continue
                req_id = a.get("request_id") or ""
                ok, message = await self.execute_action(
                    a["guild_id"], act, a["target_id"], a.get("target_name") or "",
                    a.get("reason") or "", a.get("duration"), a.get("moderator") or "Dashboard",
                    request_id=req_id,
                )
                # complete_action is a fallback for legacy rows without request_id;
                # execute_action already updates status when request_id is present.
                if not req_id:
                    if ok:
                        await neon_db.complete_action(a["id"], "completed")
                    else:
                        await neon_db.complete_action(a["id"], "failed", message)
                if ok:
                    if "already processed" in message:
                        logger.info(f"Action {a['id']} ({act}): {message}")
                    else:
                        logger.info(f"Processed action {a['id']}: {act} -> {a['target_id']} in {a['guild_id']} ({message})")
                else:
                    logger.error(f"Action {a['id']} ({act} on {a['target_id']}) failed: {message}")
        except Exception as e:
            logger.error(f"Mod action processor failed: {e}")
        finally:
            self._processing_actions = False

    async def execute_action(self, guild_id, action, target_id, target_name="",
                             reason="No reason provided", duration=None, moderator="Dashboard",
                             request_id=""):
        """Execute a single dashboard/moderation action immediately.

        Returns (ok: bool, message: str). Shared by the DB action processor and
        the direct HTTP bridge so dashboard actions complete instantly instead
        of waiting on the queue poll.

        When request_id is provided, the action status is tracked in mod_actions
        (executing -> completed/failed) for lifecycle visibility."""
        guild = self.get_guild(int(guild_id))
        if not guild:
            if request_id:
                await neon_db.update_action_status(request_id, "failed", "guild not found")
            return False, "guild not found"
        # Idempotency: atomically claim the action (pending -> executing) so that
        # the direct bridge and the DB queue processor never run it twice.
        if request_id:
            if not await neon_db.claim_action(request_id):
                existing = await neon_db.get_action_by_request_id(request_id)
                if existing and existing.get("status") in ("completed", "failed"):
                    logger.info(f"Action {request_id} already {existing['status']}, skipping.")
                    return existing["status"] == "completed", existing.get("error") or "already processed"
                # Another worker is executing it concurrently - do not double-run.
                return True, "already processed by another worker"
        member = None
        try:
            member = await guild.fetch_member(int(target_id))
        except Exception:
            member = None
        act = action
        reason = reason or "No reason provided"
        # Count every action the bot processes (direct bridge + DB queue) so the
        # status page can show a live "bot actions" graph.
        http_bridge.record_action()
        skip_log = False
        try:
            if act in ("emergency_lock", "emergency_unlock"):
                from components.moderation import get_mod_settings, save_mod_settings, perform_lockdown
                lock = act == "emergency_lock"
                settings = await get_mod_settings(int(guild_id))
                ok, detail = await perform_lockdown(guild, lock, settings, save_mod_settings)
                if not ok:
                    raise Exception(detail)
                reason = detail
            elif act == "kick":
                if member:
                    await member.kick(reason=reason)
                else:
                    raise Exception("member not in guild")
            elif act == "ban":
                if member:
                    await member.ban(reason=reason)
                else:
                    await guild.ban(discord.Object(id=int(target_id)), reason=reason)
            elif act == "mute":
                if not member:
                    raise Exception("member not in guild")
                minutes = int(duration or 60)
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
                await neon_db.set_muted_user(guild_id, target_id, member.name, reason, until.timestamp())
            elif act == "unmute":
                if not member:
                    raise Exception("member not in guild")
                await member.timeout(None, reason=reason)
                await neon_db.remove_muted_user(guild_id, target_id)
            elif act == "purge":
                channel = guild.get_channel(int(target_id))
                if not isinstance(channel, discord.TextChannel):
                    raise Exception("channel not found")
                deleted = await channel.purge(limit=int(duration or 10))
                reason = f"Purged {len(deleted)} messages in #{channel.name}"
            elif act == "panel_send":
                from components.tickets import Tickets
                cog = self.get_cog("Tickets")
                if not cog:
                    raise Exception("Tickets cog not loaded")
                if not await cog._send_panel(guild, target_id):
                    raise Exception("panel send failed")
                reason = "Ticket panel sent"
            elif act == "verify_panel":
                from components.verification import Verification, get_verify_settings
                cog = self.get_cog("Verification")
                if not cog:
                    raise Exception("Verification cog not loaded")
                settings = await get_verify_settings(guild.id)
                logger.info(f"verify_panel: channel_id={settings.get('channel_id')}, type={settings.get('type')}, role={settings.get('verified_role_id')}")
                ok_panel = await cog._send_panel(guild, settings)
                logger.info(f"verify_panel: _send_panel returned {ok_panel}")
                if not ok_panel:
                    raise Exception("verify panel send failed")
                reason = "Verification panel deployed"
            elif act == "verify_panel_remove":
                from components.verification import Verification
                cog = self.get_cog("Verification")
                if not cog:
                    raise Exception("Verification cog not loaded")
                await cog._delete_panel(guild)
                reason = "Verification panel removed"
                skip_log = True
            elif act == "verify_user":
                target = guild.get_member(int(target_id))
                if not target:
                    raise Exception("member not in guild")
                settings = await get_verify_settings(guild.id)
                role_id = settings.get("verified_role_id")
                if not role_id:
                    raise Exception("verified role not configured")
                role = guild.get_role(int(role_id))
                if not role:
                    raise Exception("verified role not found")
                if role not in target.roles:
                    await target.add_roles(role, reason="Verified via captcha")
                reason = f"Verified {target.name}"
            elif act in ("add_role", "remove_role"):
                role = guild.get_role(int(target_name))
                if not role:
                    raise Exception("role not found")
                if not member:
                    raise Exception("member not in guild")
                if act == "add_role":
                    await member.add_roles(role, reason="Prowl dashboard")
                else:
                    await member.remove_roles(role, reason="Prowl dashboard")
                reason = f"{'Added' if act=='add_role' else 'Removed'} role {role.name}"
            elif act == "nickname":
                if not member:
                    raise Exception("member not in guild")
                await member.edit(nick=target_name, reason="Prowl dashboard")
                reason = f"Nickname set to {target_name}"
            elif act == "leave_guild":
                await guild.leave()
                reason = "Account deletion - leaving server"
                skip_log = True
            else:
                if request_id:
                    await neon_db.update_action_status(request_id, "failed", f"unknown action: {act}")
                return False, f"unknown action: {act}"
        except Exception as e:
            if request_id:
                await neon_db.update_action_status(request_id, "failed", str(e)[:500])
            return False, str(e)[:500]
        # Log to mod_log only on actual success (skip for guild-leave)
        if not skip_log:
            member_name = member.display_name if member else (target_name or str(target_id))
            log_user = member_name
            log_reason = reason
            if act == "purge":
                channel = guild.get_channel(int(target_id))
                log_user = f"#{channel.name}" if channel else target_id
            elif act in ("add_role", "remove_role"):
                role = guild.get_role(int(target_name))
                log_reason = f"{'Added' if act=='add_role' else 'Removed'} role {role.name if role else target_name} on @{member_name}"
            elif act == "nickname":
                log_reason = f"Changed nickname to '{target_name}'"
            elif act == "verify_panel":
                log_user = "Verification"
                log_reason = "Verification panel deployed"
            elif act == "verify_user":
                log_reason = "Verified via captcha"
            await neon_db.push_mod_event(guild_id, target_id, log_user, act, log_reason, moderator)
        # Push member data immediately so the dashboard reflects the change fast
        if act in ("add_role", "remove_role", "nickname"):
            self.loop.create_task(self._sync_guild_members(guild))
        if request_id:
            await neon_db.update_action_status(request_id, "completed")
        return True, reason

    async def _build_stats(self):
        """Build the lightweight bot_stats dict (used by the fast + full syncs)."""
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss // 1024 // 1024
        cpu = process.cpu_percent()

        total_users = len(self.users)
        active_users = sum(1 for m in self.get_all_members() if m.status != discord.Status.offline)
        total_commands = len(self.tree.get_commands())
        launch_time = getattr(self, "launch_time", None)
        uptime_seconds = int(time.time() - launch_time) if launch_time else 0
        uptime_str = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime_seconds))
        bot_status = "Running" if self.is_ready() else "Not Running"
        bot_version = str(getattr(self, "version", "unknown"))
        python_version = sys.version.replace("\n", " ")
        guilds = list(self.guilds)
        guild_ids = [str(g.id) for g in guilds]
        loaded_cogs = list(self.cogs.keys())
        all_commands = [cmd.name for cmd in self.tree.get_commands()]
        last_restart = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(launch_time)) if launch_time else "unknown"

        return {
            "total_users": total_users,
            "active_users": active_users,
            "total_commands": total_commands,
            "uptime": uptime_str,
            "bot_status": bot_status,
            "bot_version": bot_version,
            "python_version": python_version,
            "num_guilds": len(guilds),
            "num_shards": len(self.shards) if getattr(self, "shards", None) else 1,
            "gateway_ping_ms": int(getattr(self, "latency", 0) * 1000),
            "guild_ids": json.dumps(guild_ids),
            "num_channels": sum(len(g.channels) for g in guilds),
            "num_roles": sum(len(g.roles) for g in guilds),
            "num_emojis": sum(len(g.emojis) for g in guilds),
            "loaded_cogs": json.dumps(loaded_cogs),
            "all_commands": json.dumps(all_commands),
            "memory_usage_mb": mem,
            "cpu_usage_percent": cpu,
            "last_restart": last_restart,
            "music_status": "disabled",
        }

    async def _push_to_neon(self):
        """Build stats and push directly to the database."""
        bot_stats = await self._build_stats()

        guilds = list(self.guilds)
        guild_list = []
        for guild in guilds:
            icon_url = str(guild.icon.url) if guild.icon else None
            guild_list.append({
                "id": guild.id,
                "name": guild.name,
                "icon_url": icon_url,
                "member_count": guild.member_count,
                "online_count": sum(1 for m in guild.members if m.status != discord.Status.offline),
                "channel_count": len(guild.channels),
                "text_channels": len(guild.text_channels),
                "voice_channels": len(guild.voice_channels),
                "role_count": len(guild.roles),
                "emoji_count": len(guild.emojis),
                "created_at": guild.created_at.isoformat(),
                "owner_id": guild.owner_id,
                "bot_top_role_position": guild.me.top_role.position if guild.me else 0,
                "bot_permissions": (guild.me.guild_permissions.value if guild.me else 0),
                "members": [{
                    "id": str(m.id), "name": m.name, "display_name": m.display_name,
                    "avatar_url": str(m.display_avatar.url),
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                    "roles": [str(r.id) for r in m.roles[1:]],
                    "is_raider": False,
                } for m in guild.members],
                "channels": [{"id": str(c.id), "name": c.name, "type": c.type.value} for c in guild.channels],
                "roles": [{"id": str(r.id), "name": r.name, "color": r.color.value, "position": r.position, "managed": r.managed, "count": len(r.members), "permissions": r.permissions.value} for r in guild.roles],
            })

        await neon_db.push_bot_stats(bot_stats)
        await neon_db.push_guild_data(guild_list)
        logger.info("DB sync: data pushed successfully.")


def main():
    token = os.environ.get("TOKEN")
    if not token:
        logger.error("TOKEN not found in environment variables. Add it to cli/.env")
        sys.exit(1)

    bot = ProwlBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
