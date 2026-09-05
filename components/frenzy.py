import discord
from discord.ext import commands
from discord import app_commands
import json
import time
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title, EMBED_EMOJIS


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


async def get_frenzy_settings(guild_id: int):
    return await neon_db.load_cached_settings("frenzy_settings", guild_id, FRENZY_DEFAULTS)


async def save_frenzy_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("frenzy_settings", guild_id, settings)


async def get_active_frenzy(guild_id: int):
    pool = await neon_db.get_pool()
    if not pool:
        return None
    row = await pool.fetchrow(
        "SELECT multiplier, reason, started_by, started_at, expires_at FROM frenzy_active WHERE guild_id = ?",
        str(guild_id),
    )
    if not row:
        return None
    now = time.time()
    expires = row["expires_at"]
    if expires and now > float(expires):
        await pool.execute("DELETE FROM frenzy_active WHERE guild_id = ?", str(guild_id))
        return None
    return dict(row)


async def start_frenzy(guild_id: int, multiplier: float, duration_minutes: Optional[int], reason: str, started_by: int):
    pool = await neon_db.get_pool()
    if not pool:
        return False
    now = time.time()
    expires_at = now + (duration_minutes * 60) if duration_minutes else None
    await pool.execute(
        "INSERT INTO frenzy_active (guild_id, multiplier, reason, started_by, started_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET "
        "multiplier = ?, reason = ?, started_by = ?, started_at = ?, expires_at = ?",
        str(guild_id), multiplier, reason, str(started_by), now, expires_at,
        multiplier, reason, str(started_by), now, expires_at,
    )
    return True


async def stop_frenzy(guild_id: int):
    pool = await neon_db.get_pool()
    if not pool:
        return False
    await pool.execute("DELETE FROM frenzy_active WHERE guild_id = ?", str(guild_id))
    return True


async def get_frenzy_multiplier(guild_id: int) -> float:
    """Get the current frenzy multiplier for a guild. Used by leveling cog."""
    frenzy = await get_active_frenzy(guild_id)
    if frenzy:
        return float(frenzy["multiplier"])
    return 1.0


class Frenzy(commands.Cog, name="Frenzy"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._member_join_tracking = {}  # guild_id -> list of join timestamps
        self._message_tracking = {}  # guild_id -> list of message timestamps
        self._voice_tracking = {}  # guild_id -> set of user_ids in voice
        self._check_frenzy_expiry.start()

    def cog_unload(self):
        self._check_frenzy_expiry.cancel()

    @commands.Cog.loop(minutes=1)
    async def _check_frenzy_expiry(self):
        """Check for expired frenzies every minute."""
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return
            now = time.time()
            rows = await pool.fetch(
                "SELECT guild_id FROM frenzy_active WHERE expires_at IS NOT NULL AND expires_at < ?",
                str(now),
            )
            for row in rows:
                guild_id = int(row["guild_id"])
                await stop_frenzy(guild_id)
                guild = self.bot.get_guild(guild_id)
                if guild:
                    settings = await get_frenzy_settings(guild_id)
                    if settings.get("announce_end", True):
                        channel_id = settings.get("announce_channel_id")
                        channel = guild.get_channel(int(channel_id)) if channel_id else guild.system_channel
                        if channel:
                            try:
                                embed = (
                                    EmbedBuilder()
                                    .title(emoji_title("bolt", "Frenzy Mode Ended"))
                                    .description("The XP frenzy has ended. Back to normal!")
                                    .color("info")
                                    .timestamp(datetime.datetime.utcnow())
                                    .build()
                                )
                                await channel.send(embed=embed)
                            except Exception:
                                pass
        except Exception as e:
            logger.warning(f"Frenzy expiry check failed: {e}")

    @_check_frenzy_expiry.before_loop
    async def before_frenzy_expiry(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild_id = member.guild.id
        settings = await get_frenzy_settings(guild_id)
        if not settings.get("enabled", True):
            return
        triggers = settings.get("auto_triggers", {})
        join_trigger = triggers.get("member_join", {})
        if not join_trigger.get("enabled", False):
            return
        now = time.time()
        if guild_id not in self._member_join_tracking:
            self._member_join_tracking[guild_id] = []
        self._member_join_tracking[guild_id].append(now)
        window = join_trigger.get("minutes", 10) * 60
        self._member_join_tracking[guild_id] = [
            t for t in self._member_join_tracking[guild_id] if now - t < window
        ]
        if len(self._member_join_tracking[guild_id]) >= join_trigger.get("count", 5):
            existing = await get_active_frenzy(guild_id)
            if not existing:
                await self._activate_frenzy(
                    guild_id, member.guild,
                    join_trigger.get("multiplier", 2.0),
                    join_trigger.get("duration_minutes", 30),
                    f"Member join spike ({len(self._member_join_tracking[guild_id])} joins in {join_trigger.get('minutes', 10)} min)",
                    None,
                )
                self._member_join_tracking[guild_id] = []

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        guild_id = message.guild.id
        settings = await get_frenzy_settings(guild_id)
        if not settings.get("enabled", True):
            return
        triggers = settings.get("auto_triggers", {})
        msg_trigger = triggers.get("message_spike", {})
        if not msg_trigger.get("enabled", False):
            return
        now = time.time()
        if guild_id not in self._message_tracking:
            self._message_tracking[guild_id] = []
        self._message_tracking[guild_id].append(now)
        window = msg_trigger.get("minutes", 5) * 60
        self._message_tracking[guild_id] = [
            t for t in self._message_tracking[guild_id] if now - t < window
        ]
        if len(self._message_tracking[guild_id]) >= msg_trigger.get("count", 100):
            existing = await get_active_frenzy(guild_id)
            if not existing:
                await self._activate_frenzy(
                    guild_id, message.guild,
                    msg_trigger.get("multiplier", 1.5),
                    msg_trigger.get("duration_minutes", 15),
                    f"Message spike ({len(self._message_tracking[guild_id])} messages in {msg_trigger.get('minutes', 5)} min)",
                    None,
                )
                self._message_tracking[guild_id] = []

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        guild_id = member.guild.id
        settings = await get_frenzy_settings(guild_id)
        if not settings.get("enabled", True):
            return
        triggers = settings.get("auto_triggers", {})
        voice_trigger = triggers.get("voice_activity", {})
        if not voice_trigger.get("enabled", False):
            return
        if guild_id not in self._voice_tracking:
            self._voice_tracking[guild_id] = set()
        if after.channel:
            self._voice_tracking[guild_id].add(member.id)
        if before.channel:
            self._voice_tracking[guild_id].discard(member.id)
        if len(self._voice_tracking[guild_id]) >= voice_trigger.get("count", 10):
            existing = await get_active_frenzy(guild_id)
            if not existing:
                await self._activate_frenzy(
                    guild_id, member.guild,
                    voice_trigger.get("multiplier", 2.0),
                    voice_trigger.get("duration_minutes", 60),
                    f"Voice activity ({len(self._voice_tracking[guild_id])} users in voice)",
                    None,
                )
                self._voice_tracking[guild_id] = set()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since == after.premium_since:
            return
        if after.premium_since and not before.premium_since:
            guild_id = after.guild.id
            settings = await get_frenzy_settings(guild_id)
            if not settings.get("enabled", True):
                return
            triggers = settings.get("auto_triggers", {})
            boost_trigger = triggers.get("boost", {})
            if not boost_trigger.get("enabled", False):
                return
            existing = await get_active_frenzy(guild_id)
            if not existing:
                await self._activate_frenzy(
                    guild_id, after.guild,
                    boost_trigger.get("multiplier", 3.0),
                    boost_trigger.get("duration_minutes", 60),
                    f"Server boost by {after.display_name}",
                    after.id,
                )

    async def _activate_frenzy(self, guild_id: int, guild: discord.Guild, multiplier: float, duration_minutes: int, reason: str, started_by: Optional[int]):
        settings = await get_frenzy_settings(guild_id)
        max_mult = settings.get("max_multiplier", 10.0)
        multiplier = min(multiplier, max_mult)
        max_dur = settings.get("max_duration_minutes", 1440)
        if duration_minutes and max_dur:
            duration_minutes = min(duration_minutes, max_dur)
        await start_frenzy(guild_id, multiplier, duration_minutes, reason, started_by)
        if settings.get("announce_start", True):
            channel_id = settings.get("announce_channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else guild.system_channel
            if channel:
                try:
                    dur_text = f"for {duration_minutes} minutes" if duration_minutes else "until stopped"
                    embed = (
                        EmbedBuilder()
                        .title(emoji_title("bolt", "Frenzy Mode Activated!"))
                        .description(f"**{multiplier}x XP** {dur_text}!\n\n**Reason:** {reason}")
                        .color("brand")
                        .timestamp(datetime.datetime.utcnow())
                        .build()
                    )
                    await channel.send(embed=embed)
                except Exception:
                    pass

    # ── Commands ──────────────────────────────────────────────────────

    @app_commands.command(name="frenzy", description="Frenzy mode - multiply XP gains!")
    @app_commands.describe(
        action="Start, stop, or check frenzy status",
        multiplier="XP multiplier (e.g. 2, 3, 5)",
        duration="Duration in minutes (leave empty for unlimited)",
        reason="Reason for starting frenzy"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Start", value="start"),
        app_commands.Choice(name="Stop", value="stop"),
        app_commands.Choice(name="Status", value="status"),
    ])
    async def frenzy_cmd(self, ctx: commands.Context, action: str = "status", multiplier: Optional[float] = None, duration: Optional[int] = None, reason: Optional[str] = None):
        if not ctx.guild:
            return await ctx.send("Server only.")

        settings = await get_frenzy_settings(ctx.guild.id)
        if not settings.get("enabled", True):
            return await ctx.send(
                embed=EmbedBuilder().title(emoji_title("error", "Frenzy Disabled")).description("Frenzy mode is disabled in this server.").color("error").timestamp(datetime.datetime.utcnow()).build()
            )

        if action == "status":
            frenzy = await get_active_frenzy(ctx.guild.id)
            if frenzy:
                mult = float(frenzy["multiplier"])
                rsn = frenzy["reason"] or "No reason"
                expires = frenzy["expires_at"]
                if expires:
                    remaining = max(0, int(float(expires) - time.time()))
                    if remaining > 3600:
                        dur_text = f"{remaining // 3600}h {(remaining % 3600) // 60}m"
                    elif remaining > 60:
                        dur_text = f"{remaining // 60}m {remaining % 60}s"
                    else:
                        dur_text = f"{remaining}s"
                else:
                    dur_text = "Until stopped"
                embed = (
                    EmbedBuilder()
                    .title(emoji_title("bolt", "Frenzy Active"))
                    .description(f"**{mult}x XP** is currently active!")
                    .color("brand")
                    .row(
                        ("Reason", rsn[:100]),
                        ("Remaining", dur_text),
                        ("Started by", f"<@{frenzy['started_by']}>" if frenzy.get("started_by") else "Auto"),
                    )
                    .timestamp(datetime.datetime.utcnow())
                    .build()
                )
            else:
                embed = (
                    EmbedBuilder()
                    .title(emoji_title("bolt", "No Frenzy Active"))
                    .description("No frenzy mode is currently active. Use `/frenzy start` to activate!")
                    .color("info")
                    .timestamp(datetime.datetime.utcnow())
                    .build()
                )
            return await ctx.send(embed=embed)

        if action == "stop":
            if not ctx.author.guild_permissions.manage_guild:
                return await ctx.send(
                    embed=EmbedBuilder().title(emoji_title("error", "No Permission")).description("You need **Manage Server** permission.").color("error").timestamp(datetime.datetime.utcnow()).build()
                )
            frenzy = await get_active_frenzy(ctx.guild.id)
            if not frenzy:
                return await ctx.send("No frenzy is currently active.")
            await stop_frenzy(ctx.guild.id)
            return await ctx.send(
                embed=EmbedBuilder().title(emoji_title("success", "Frenzy Stopped")).description("XP frenzy has been stopped.").color("success").timestamp(datetime.datetime.utcnow()).build()
            )

        if action == "start":
            if not ctx.author.guild_permissions.manage_guild:
                return await ctx.send(
                    embed=EmbedBuilder().title(emoji_title("error", "No Permission")).description("You need **Manage Server** permission.").color("error").timestamp(datetime.datetime.utcnow()).build()
                )
            existing = await get_active_frenzy(ctx.guild.id)
            if existing:
                return await ctx.send("Frenzy is already active! Use `/frenzy stop` first.")
            mult = multiplier or settings.get("default_multiplier", 2.0)
            max_mult = settings.get("max_multiplier", 10.0)
            if mult < 1.0:
                return await ctx.send("Multiplier must be at least 1.0.")
            if mult > max_mult:
                return await ctx.send(f"Maximum multiplier is {max_mult}x.")
            dur = duration
            max_dur = settings.get("max_duration_minutes", 1440)
            if dur and max_dur and dur > max_dur:
                return await ctx.send(f"Maximum duration is {max_dur} minutes.")
            if dur and dur < 1:
                return await ctx.send("Duration must be at least 1 minute.")
            rsn = reason or f"Started by {ctx.author.display_name}"
            await self._activate_frenzy(ctx.guild.id, ctx.guild, mult, dur, rsn, ctx.author.id)
            return await ctx.send(
                embed=EmbedBuilder().title(emoji_title("success", "Frenzy Started")).description(f"**{mult}x XP** is now active!").color("success").timestamp(datetime.datetime.utcnow()).build()
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Frenzy(bot))
