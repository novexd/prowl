"""Guild scheduled (recurring) messages, configured from the More dashboard page.

Settings live in bot_stats (same pattern as sticky messages):
  more_scheduled_enabled_{guild_id}  -> "1"/"0"
  more_scheduled_messages_{guild_id} -> JSON list of entries:
    {channel_id, type: "basic"|"custom", message?, embed?, interval_minutes, next_run?}

A 1-minute loop sends due entries and persists the next run time back so
restarts don't reset the schedule.
"""
import json
import time

import discord
from discord.ext import commands, tasks

from Ediscord import logger
from Ediscord import db as neon_db
from Ediscord.builders import embed_from_dict

SCHED_MIN_MINUTES = 5
SCHED_MAX_MINUTES = 60 * 24 * 30  # 30 days
SCHED_MAX_PER_GUILD = 10


def _sched_key(guild_id: int, suffix: str) -> str:
    return f"more_scheduled_{suffix}_{guild_id}"


async def _get_stat(key: str):
    pool = await neon_db.get_pool()
    if not pool:
        return None
    row = await pool.fetchrow("SELECT value FROM bot_stats WHERE key = ?", key)
    return row["value"] if row else None


async def _set_stat(key: str, value: str):
    pool = await neon_db.get_pool()
    if not pool:
        return
    await pool.execute(
        "INSERT INTO bot_stats (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = ?, updated_at = ?",
        key, value, time.time(), value, time.time(),
    )


def clamp_interval(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 60
    return max(SCHED_MIN_MINUTES, min(SCHED_MAX_MINUTES, minutes))


async def get_scheduled_settings(guild_id: int):
    """Return (enabled, entries) for a guild."""
    enabled_raw = await _get_stat(_sched_key(guild_id, "enabled"))
    enabled = enabled_raw == "1"
    raw = await _get_stat(_sched_key(guild_id, "messages"))
    entries = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                entries = parsed
        except (json.JSONDecodeError, TypeError):
            entries = []
    return enabled, entries


async def save_scheduled_entries(guild_id: int, entries: list):
    await _set_stat(_sched_key(guild_id, "messages"), json.dumps(entries))


class ScheduledMessages(commands.Cog):
    """Send recurring guild messages on a per-channel interval."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tick.start()

    def cog_unload(self):
        self._tick.cancel()

    @tasks.loop(minutes=1)
    async def _tick(self):
        now = time.time()
        for guild in self.bot.guilds:
            try:
                enabled, entries = await get_scheduled_settings(guild.id)
            except Exception as e:
                logger.warning(f"Scheduled tick read failed for {guild.id}: {e}")
                continue
            if not enabled or not entries:
                continue
            changed = False
            for entry in entries[:SCHED_MAX_PER_GUILD]:
                if not isinstance(entry, dict):
                    continue
                interval = clamp_interval(entry.get("interval_minutes"))
                try:
                    nxt = float(entry.get("next_run") or 0)
                except (TypeError, ValueError):
                    nxt = 0
                if nxt <= now:
                    await self._send(guild, entry)
                    entry["next_run"] = now + interval * 60
                    changed = True
            if changed:
                try:
                    await save_scheduled_entries(guild.id, entries)
                except Exception as e:
                    logger.warning(f"Scheduled persist failed for {guild.id}: {e}")

    @_tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _send(self, guild: discord.Guild, entry: dict):
        try:
            channel_id = int(entry.get("channel_id") or 0)
        except (TypeError, ValueError):
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        embed = None
        content = None
        try:
            if entry.get("type") == "custom" and isinstance(entry.get("embed"), dict):
                embed = embed_from_dict(entry["embed"])
                if entry.get("message"):
                    content = self._apply_vars(str(entry["message"])[:2000], guild, channel)
            else:
                msg = str(entry.get("message") or "")
                content = self._apply_vars(msg, guild, channel) if msg else "(no scheduled message configured)"
            await channel.send(content=content, embed=embed)
        except (discord.Forbidden, discord.NotFound):
            pass
        except Exception as e:
            logger.warning(f"Scheduled send failed in {guild.id}: {e}")

    def _apply_vars(self, text: str, guild: discord.Guild, channel) -> str:
        try:
            member_count = str(guild.member_count or 0)
        except Exception:
            member_count = "0"
        return (
            text
            .replace("{server}", guild.name)
            .replace("{channel}", getattr(channel, "name", "channel"))
            .replace("{membercount}", member_count)
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduledMessages(bot))
