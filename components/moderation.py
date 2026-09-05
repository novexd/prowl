import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import datetime
import time
import json
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import embed_from_dict, emoji_title, basic_action_embed, BUTTON_EMOJIS
from Ediscord.utils import is_owner


MOD_DEFAULTS = {
    "dm_on_action": True, "require_reason": True, "silent_mod": False,
    "auto_thread": False, "track_stats": True,
    "cmd_ban": True, "cmd_kick": True, "cmd_tempban": True,
    "cmd_unban": True, "cmd_mute": True, "cmd_timeout": True, "cmd_unmute": True,
    "cmd_warn": True, "cmd_purge": True,
    "mod_roles": [], "emergency_lock": False,
    "mute_evasion": False,
    # ── Modlog ──
    "modlog_channel_id": None,
    # ── Ban ──
    "ban_dm": True, "ban_purge": True,
    "ban_message": "{username} has been banned.", "ban_message_enabled": True,
    "ban_message_mode": "basic", "ban_embed": {},
    # ── Temp ban ──
    "tempban_dm": True, "tempban_purge": True,
    "tempban_message": "{username} has been temporarily banned for {time}.",
    "tempban_message_enabled": True,
    "tempban_message_mode": "basic", "tempban_embed": {},
    "tempban_duration": 1440,  # minutes
    # ── Mute ──
    "mute_dm": True, "mute_duration": 60,
    "mute_message": "{username} has been muted for {time}.",
    "mute_message_enabled": True,
    "mute_message_mode": "basic", "mute_embed": {},
    # ── Kick ──
    "kick_dm": True,
    "kick_message": "{username} has been kicked.", "kick_message_enabled": True,
    "kick_message_mode": "basic", "kick_embed": {},
    # ── Warn ──
    "warn_dm": True,
    "warn_message": "{username} has been warned.", "warn_message_enabled": True,
    "warn_message_mode": "basic", "warn_embed": {},
}


def render_template(template: str, member: discord.Member, reason: str = "", msg_count: int = 0, time_str: str = "") -> str:
    """Replace template placeholders with member/context values."""
    if not template:
        return ""
    guild = member.guild
    joined = member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "unknown"
    return (template
            .replace("{username}", member.name)
            .replace("{name}", member.display_name)
            .replace("{avatar}", str(member.display_avatar.url))
            .replace("{server}", guild.name if guild else "")
            .replace("{servername}", guild.name if guild else "")
            .replace("{servermembercount}", str(guild.member_count if guild else 0))
            .replace("{datejoined}", joined)
            .replace("{messagessent}", str(msg_count))
            .replace("{reason}", reason)
            .replace("{time}", time_str))


def format_duration(minutes: int) -> str:
    """Convert minutes to a human-readable duration string like '2 hours 30 minutes'."""
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rem = divmod(minutes, 60)
    if rem == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''} {rem} minute{'s' if rem != 1 else ''}"


def custom_action_embed(action: str, settings: dict, member: discord.Member,
                        reason: str = "", msg_count: int = 0, time_str: str = "") -> Optional[discord.Embed]:
    """Return the dashboard-configured custom embed for an action, or None when
    the action isn't in custom mode or the embed is empty (caller falls back to basic)."""
    if settings.get(f"{action}_message_mode", "basic") != "custom":
        return None
    data = settings.get(f"{action}_embed") or {}
    if not isinstance(data, dict):
        return None
    if not (data.get("title") or data.get("description") or data.get("fields")
            or data.get("footer_text") or data.get("author_name")
            or data.get("thumbnail_url") or data.get("image_url")):
        return None
    return embed_from_dict(render_embed_data(data, member, reason, msg_count, time_str))


def render_embed_data(data: dict, member: discord.Member, reason: str = "", msg_count: int = 0, time_str: str = "") -> dict:
    """Render template variables inside an embed dict's text fields."""
    out = dict(data)
    for key in ("title", "description", "footer_text", "author_name", "url"):
        if out.get(key):
            out[key] = render_template(str(out[key]), member, reason, msg_count, time_str)
    return out


def _info_embed(title: str, description: str, color: str = "blue", ephemeral_view: bool = True) -> discord.Embed:
    eb = EmbedBuilder().title(title).description(description).color(color).timestamp(datetime.datetime.utcnow())
    return eb.build()


def _error_embed(description: str) -> discord.Embed:
    return EmbedBuilder().title(emoji_title("error", "Error")).description(description).color("red").timestamp(datetime.datetime.utcnow()).build()


def _confirmation_embed(description: str) -> discord.Embed:
    return EmbedBuilder().title(emoji_title("warning", "Confirm Action")).description(description).color("orange").timestamp(datetime.datetime.utcnow()).build()


async def send_modlog(guild, settings, embed):
    """Send a log embed to the configured modlog channel."""
    channel_id = settings.get("modlog_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send modlog to {channel_id} in {guild.id}: {e}")


async def safe_dm(member, embed: discord.Embed = None, content: str = None):
    """Send a DM to a member, swallowing common errors (closed DMs, blocked bot, etc.)."""
    try:
        if embed is not None:
            await member.send(embed=embed, content=content)
        else:
            await member.send(content=content)
    except (discord.Forbidden, discord.HTTPException):
        pass
    except Exception as e:
        logger.debug(f"DM to {getattr(member, 'id', '?')} failed: {e}")


async def perform_lockdown(guild, lock: bool, settings: dict, save_fn) -> tuple:
    """Actually lock/unlock a server: snapshot perms, deny @everyone, delete invites."""
    try:
        if lock:
            snapshot = {}
            for channel in guild.channels:
                if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel)):
                    ow = channel.overwrites_for(guild.default_role)
                    snapshot[str(channel.id)] = {"allow": ow.allow.value, "deny": ow.deny.value}
                    ow.send_messages = False
                    ow.create_instant_invite = False
                    if isinstance(channel, discord.VoiceChannel):
                        ow.connect = False
                        ow.speak = False
                    await channel.set_permissions(guild.default_role, overwrite=ow)
            # Delete all active invites
            try:
                invites = await guild.invites()
                for inv in invites:
                    try:
                        await inv.delete()
                    except Exception:
                        pass
            except Exception:
                pass
            settings["emergency_lock"] = True
            settings["emergency_snapshot"] = snapshot
            await save_fn(guild.id, settings)
            return True, f"Locked down {len(snapshot)} channels and removed invites."
        else:
            snapshot = settings.get("emergency_snapshot", {})
            for cid, data in snapshot.items():
                channel = guild.get_channel(int(cid))
                if channel:
                    ow = discord.PermissionOverwrite()
                    ow.allow = discord.Permissions(int(data.get("allow", 0)))
                    ow.deny = discord.Permissions(int(data.get("deny", 0)))
                    await channel.set_permissions(guild.default_role, overwrite=ow)
            settings["emergency_lock"] = False
            settings.pop("emergency_snapshot", None)
            await save_fn(guild.id, settings)
            return True, f"Restored permissions on {len(snapshot)} channels."
    except Exception as e:
        logger.error(f"Lockdown failed: {e}")
        return False, f"Failed: {e}"


async def get_mod_settings(guild_id: int):
    return await neon_db.load_cached_settings("mod_settings", guild_id, MOD_DEFAULTS)


async def save_mod_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("mod_settings", guild_id, settings)


async def log_mod_action(guild_id: int, user_id: str, user_name: str, action: str, reason: str = "", moderator: str = ""):
    await neon_db.push_mod_event(guild_id, user_id, user_name, action, reason, moderator)


def is_mod():
    async def predicate(interaction: discord.Interaction):
        settings = await get_mod_settings(interaction.guild_id)
        mod_roles = settings.get("mod_roles", [])
        user_roles = [str(r.id) for r in interaction.user.roles]
        if any(rid in mod_roles for rid in user_roles):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        if interaction.user.guild_permissions.moderate_members:
            return True
        logger.info(
            f"[Mod] {interaction.user.name} denied /{interaction.command.name if interaction.command else '?'} "
            f"in {interaction.guild.name} (mod_roles={mod_roles}, user roles={user_roles})"
        )
        embed = _error_embed("You don't have permission to use this command.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass
        return False
    return app_commands.check(predicate)


async def check_emergency_lock(interaction: discord.Interaction) -> bool:
    settings = await get_mod_settings(interaction.guild_id)
    if settings.get("emergency_lock"):
        embed = _error_embed("Server is in emergency lockdown. Mod commands are disabled.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass
        return False
    return True


class ConfirmationView(discord.ui.View):
    def __init__(self, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.value = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji=BUTTON_EMOJIS["check"])
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=BUTTON_EMOJIS["cross"])
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        self.stop()


class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.msg_counts = {}
        self.message_accum = {}
        self.member_counts = {}
        self.hour_started = int(time.time())
        self.flush_history.start()
        self.flush_messages.start()

    async def send_confirm(self, interaction, settings, action: str, title: str, color: str,
                           member: discord.Member, reason: str, time_str: str = ""):
        """Post the action confirmation: custom embed if configured, else the basic embed."""
        if settings.get("silent_mod") or not settings.get(f"{action}_message_enabled", True):
            try:
                await interaction.followup.send(f"{title} - done.", ephemeral=True)
            except Exception:
                pass
            return
        msg_count = self.get_msg_count(interaction.guild_id, member.id)
        try:
            embed = custom_action_embed(action, settings, member, reason, msg_count, time_str)
            if embed is not None:
                await interaction.channel.send(embed=embed)
            else:
                msg = render_template(settings.get(f"{action}_message", ""), member, reason, msg_count, time_str)
                if not msg:
                    msg = f"{member.mention} has been {action}ed."
                await interaction.channel.send(embed=basic_action_embed(action, msg, color))
        except Exception as e:
            logger.error(f"send_confirm failed for {action}: {e}")

    def cog_unload(self):
        self.flush_history.cancel()
        self.flush_messages.cancel()

    async def _ensure_tables(self):
        """Create the mod_actions / history tables if they don't exist."""
        pool = await neon_db.get_pool()
        if not pool:
            logger.warning("No DB pool - cannot ensure tables. Check DATABASE_URL in cli/.env")
            return False
        try:
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS mod_actions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    target_id   TEXT NOT NULL,
                    target_name TEXT DEFAULT '',
                    reason      TEXT DEFAULT '',
                    duration    INTEGER,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    created_at  REAL NOT NULL
                )
            """)
            await pool.execute("CREATE INDEX IF NOT EXISTS idx_mod_actions_pending ON mod_actions (status, created_at)")
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS member_history (
                    guild_id TEXT NOT NULL, timestamp REAL NOT NULL,
                    member_count INTEGER NOT NULL, PRIMARY KEY (guild_id, timestamp)
                )
            """)
            await pool.execute("""
                CREATE TABLE IF NOT EXISTS message_history (
                    guild_id TEXT NOT NULL, timestamp REAL NOT NULL,
                    message_count INTEGER NOT NULL, PRIMARY KEY (guild_id, timestamp)
                )
            """)
            logger.info("Ensured moderation tables exist.")
            return True
        except Exception as e:
            logger.error(f"Failed to ensure tables: {e}")
            return False

    async def cog_load(self):
        logger.info("[Moderation] cog loaded - starting action poller.")
        self.bot.loop.create_task(self._on_ready_init())

    async def _on_ready_init(self):
        await self.bot.wait_until_ready()
        try:
            ok = await self._ensure_tables()
            logger.info(f"[Moderation] init: tables {'ensured' if ok else 'FAILED - check DATABASE_URL in cli/.env'}")
            now = time.time()
            for guild in self.bot.guilds:
                self.member_counts[guild.id] = {"ts": now, "count": guild.member_count}
            await self._flush_history()
        except Exception as e:
            logger.error(f"Moderation init error: {e}")

    @tasks.loop(hours=1)
    async def flush_history(self):
        await self.bot.wait_until_ready()
        await self._flush_history()

    @tasks.loop(minutes=10)
    async def flush_messages(self):
        """Flush message counts more often so the message graph fills quickly."""
        await self.bot.wait_until_ready()
        pool = await neon_db.get_pool()
        if not pool or not self.message_accum:
            return
        try:
            now = time.time()
            for guild_id, msg_count in self.message_accum.items():
                if msg_count > 0:
                    await pool.execute(
                        "INSERT INTO message_history (guild_id, timestamp, message_count) VALUES (?, ?, ?) "
                        "ON CONFLICT (guild_id, timestamp) DO UPDATE SET message_count = ?",
                        str(guild_id), now, msg_count, msg_count,
                    )
            self.message_accum = {}
        except Exception as e:
            logger.error(f"flush_messages failed: {e}")

    async def _flush_history(self):
        pool = await neon_db.get_pool()
        if not pool:
            return
        now = time.time()
        try:
            for guild_id, mc in self.member_counts.items():
                await pool.execute(
                    "INSERT INTO member_history (guild_id, timestamp, member_count) VALUES (?, ?, ?) "
                    "ON CONFLICT (guild_id, timestamp) DO UPDATE SET member_count = ?",
                    str(guild_id), mc["ts"], mc["count"], mc["count"],
                )
            for guild_id, msg_count in self.message_accum.items():
                if msg_count > 0:
                    await pool.execute(
                        "INSERT INTO message_history (guild_id, timestamp, message_count) VALUES (?, ?, ?) "
                        "ON CONFLICT (guild_id, timestamp) DO UPDATE SET message_count = ?",
                        str(guild_id), now, msg_count, msg_count,
                    )
            self.message_accum = {}
            self.member_counts = {g.id: {"ts": now, "count": g.member_count} for g in self.bot.guilds}
            self.hour_started = now
            logger.info("History flush complete.")
        except Exception as e:
            logger.error(f"History flush failed: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self.member_counts[member.guild.id] = {"ts": time.time(), "count": member.guild.member_count}

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        self.member_counts[member.guild.id] = {"ts": time.time(), "count": member.guild.member_count}

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        settings = await get_mod_settings(after.guild.id)
        if not settings.get("mute_evasion"):
            return
        if before.is_timed_out() and not after.is_timed_out():
            if before.timed_out_until and before.timed_out_until > discord.utils.utcnow():
                try:
                    await after.timeout(before.timed_out_until, reason="Mute evasion detected")
                    logger.info(f"Re-applied mute to {after.name} in {after.guild.name} (mute evasion)")
                except Exception as e:
                    logger.warning(f"Failed to re-apply mute: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        key = (str(message.guild.id), str(message.author.id))
        self.msg_counts[key] = self.msg_counts.get(key, 0) + 1
        self.message_accum[message.guild.id] = self.message_accum.get(message.guild.id, 0) + 1

    def get_msg_count(self, guild_id, user_id) -> int:
        return self.msg_counts.get((str(guild_id), str(user_id)), 0)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            embed = _error_embed("This command can only be used in a server.")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass
            return False
        return True

    def _user_dm_embed(self, title: str, description: str, color: str = "red") -> discord.Embed:
        return EmbedBuilder().title(title).description(description).color(color).timestamp(datetime.datetime.utcnow()).footer("Server Moderation").build()

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="The member to kick", reason="Reason for the kick (optional)")
    @is_mod()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = None):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_kick", True):
            return await interaction.response.send_message(embed=_error_embed("Kick command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.id == interaction.user.id:
            return await interaction.response.send_message(embed=_error_embed("You cannot kick yourself."), ephemeral=True)
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=_error_embed("I cannot kick this member - their role is higher than or equal to mine."), ephemeral=True)
        if not reason:
            reason = "No reason provided"

        view = ConfirmationView()
        embed = _confirmation_embed(f"Are you sure you want to kick {member.mention}?\n**Reason:** {reason}")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if view.value is True:
            if settings.get("dm_on_action", True) and settings.get("kick_dm", True):
                dm_embed = self._user_dm_embed(
                    emoji_title("kick", "You have been kicked"),
                    f"You were kicked from **{interaction.guild.name}**.\n**Reason:** {reason}",
                    "red",
                )
                await safe_dm(member, embed=dm_embed)
            await member.kick(reason=reason)

            await self.send_confirm(interaction, settings, "kick", "👢 Member Kicked", "red", member, reason)

            log_embed = (
                EmbedBuilder().title(emoji_title("kick", "Member Kicked"))
                .description(f"{member.mention} (`{member.id}`)")
                .color("red")
                .row(
                    ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                    ('Reason', reason)
                )
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            await send_modlog(interaction.guild, settings, log_embed)
            await log_mod_action(interaction.guild_id, str(member.id), member.name, "kick", reason, interaction.user.name)
        else:
            await interaction.followup.send(embed=EmbedBuilder().title(emoji_title("info", "Cancelled")).description("Kick cancelled.").color("grey").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(member="The member to ban", reason="Reason for the ban (optional)", delete_days="Days of messages to delete (0-7)")
    @is_mod()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = None, delete_days: int = None):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_ban", True):
            return await interaction.response.send_message(embed=_error_embed("Ban command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.id == interaction.user.id:
            return await interaction.response.send_message(embed=_error_embed("You cannot ban yourself."), ephemeral=True)
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=_error_embed("I cannot ban this member - their role is higher than or equal to mine."), ephemeral=True)
        if not reason:
            reason = "No reason provided"
        if delete_days is None:
            delete_days = 1 if settings.get("ban_purge", True) else 0
        if delete_days < 0 or delete_days > 7:
            return await interaction.response.send_message(embed=_error_embed("Delete days must be between 0 and 7."), ephemeral=True)

        view = ConfirmationView()
        embed = _confirmation_embed(f"Are you sure you want to ban {member.mention}?\n**Reason:** {reason}\n**Delete days:** {delete_days}")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if view.value is True:
            if settings.get("dm_on_action", True) and settings.get("ban_dm", True):
                dm_embed = self._user_dm_embed(
                    emoji_title("ban", "You have been banned"),
                    f"You were banned from **{interaction.guild.name}**.\n**Reason:** {reason}",
                    "red",
                )
                await safe_dm(member, embed=dm_embed)
            await member.ban(reason=reason, delete_message_seconds=delete_days * 86400)

            await self.send_confirm(interaction, settings, "ban", emoji_title("ban", "Member Banned"), "red", member, reason)

            log_embed = (
                EmbedBuilder().title(emoji_title("ban", "Member Banned"))
                .description(f"{member.mention} (`{member.id}`)")
                .color("red")
                .row(
                    ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                    ('Reason', reason),
                    ('Delete Days', str(delete_days))
                )
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            await send_modlog(interaction.guild, settings, log_embed)
            await log_mod_action(interaction.guild_id, str(member.id), member.name, "ban", reason, interaction.user.name)
        else:
            await interaction.followup.send(embed=EmbedBuilder().title(emoji_title("info", "Cancelled")).description("Ban cancelled.").color("grey").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

    @app_commands.command(name="tempban", description="Temporarily ban a member (auto-unbans after duration)")
    @app_commands.describe(member="The member to temporarily ban", duration="Duration in minutes", reason="Reason for the temp ban (optional)")
    @is_mod()
    async def tempban(self, interaction: discord.Interaction, member: discord.Member, duration: int = None, reason: str = None):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_tempban", True):
            return await interaction.response.send_message(embed=_error_embed("Tempban command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.id == interaction.user.id:
            return await interaction.response.send_message(embed=_error_embed("You cannot temp-ban yourself."), ephemeral=True)
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=_error_embed("I cannot temp-ban this member - their role is higher than or equal to mine."), ephemeral=True)
        if duration is None:
            duration = int(settings.get("tempban_duration", 1440))
        if duration <= 0:
            return await interaction.response.send_message(embed=_error_embed("Duration must be positive."), ephemeral=True)
        if not reason:
            reason = "No reason provided"

        delete_days = 1 if settings.get("tempban_purge", True) else 0

        view = ConfirmationView()
        embed = _confirmation_embed(f"Are you sure you want to temp-ban {member.mention} for **{format_duration(duration)}**?\n**Reason:** {reason}")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if view.value is True:
            if settings.get("dm_on_action", True) and settings.get("tempban_dm", True):
                dm_embed = self._user_dm_embed(
                    emoji_title("tempban", "You have been temporarily banned"),
                    f"You were temporarily banned from **{interaction.guild.name}** for **{format_duration(duration)}**.\n**Reason:** {reason}",
                    "red",
                )
                await safe_dm(member, embed=dm_embed)
            await member.ban(reason=f"Temp ban ({duration}m): {reason}", delete_message_seconds=delete_days * 86400)

            await self.send_confirm(interaction, settings, "tempban", "⏳ Member Temp-Banned", "red", member, reason, format_duration(duration))

            log_embed = (
                EmbedBuilder().title(emoji_title("tempban", "Member Temp-Banned"))
                .description(f"{member.mention} (`{member.id}`)")
                .color("red")
                .row(
                    ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                    ('Duration', f'{duration} minutes ({format_duration(duration)})'),
                    ('Reason', reason)
                )
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            await send_modlog(interaction.guild, settings, log_embed)
            await log_mod_action(interaction.guild_id, str(member.id), member.name, "tempban", f"{duration}min - {reason}", interaction.user.name)

            self.bot.loop.create_task(self._auto_unban(interaction.guild_id, member.id, duration, reason))
        else:
            await interaction.followup.send(embed=EmbedBuilder().title(emoji_title("info", "Cancelled")).description("Temp ban cancelled.").color("grey").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

    async def _auto_unban(self, guild_id, user_id, duration_minutes, original_reason: str = ""):
        await asyncio.sleep(duration_minutes * 60)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.warning(f"Auto-unban skipped: guild {guild_id} not found.")
            return
        try:
            user = await self.bot.fetch_user(user_id)
            await guild.unban(user, reason="Temp ban expired")
            logger.info(f"Auto-unbanned {user_id} in {guild_id} (temp ban expired)")
            await log_mod_action(
                guild_id, str(user_id), user.name if user else str(user_id),
                "unban", f"Temp ban expired ({duration_minutes}min) - {original_reason}", "Auto-unban"
            )
        except discord.NotFound:
            logger.info(f"Auto-unban: user {user_id} no longer banned in {guild_id}.")
        except Exception as e:
            logger.error(f"Auto-unban failed for {user_id} in {guild_id}: {e}")

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.describe(user_id="The user ID to unban", reason="Reason for the unban")
    @is_mod()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_unban", True):
            return await interaction.response.send_message(embed=_error_embed("Unban command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return

        try:
            target_id = int(user_id)
        except ValueError:
            return await interaction.response.send_message(embed=_error_embed("Invalid user ID - must be a number."), ephemeral=True)

        view = ConfirmationView()
        embed = _confirmation_embed(f"Are you sure you want to unban user ID `{user_id}`?\n**Reason:** {reason}")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if view.value is not True:
            return await interaction.followup.send(embed=EmbedBuilder().title(emoji_title("info", "Cancelled")).description("Unban cancelled.").color("grey").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

        try:
            user = await self.bot.fetch_user(target_id)
            await interaction.guild.unban(user, reason=reason)
        except discord.NotFound:
            return await interaction.followup.send(embed=_error_embed("User not found or not banned."), ephemeral=True)
        except discord.HTTPException as e:
            logger.error(f"Unban failed for {user_id} in {interaction.guild_id}: {e}")
            return await interaction.followup.send(embed=_error_embed(f"Failed to unban: {e}"), ephemeral=True)
        except Exception as e:
            logger.error(f"Unban unexpected error for {user_id} in {interaction.guild_id}: {e}")
            return await interaction.followup.send(embed=_error_embed("An unexpected error occurred while unbanning."), ephemeral=True)

        embed = (
            EmbedBuilder().title(emoji_title("unban", "User Unbanned"))
            .description(f"{user.mention} has been unbanned.")
            .color("green")
            .row(
                ('Reason', reason),
                ('Moderator', interaction.user.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        # Sent as a standalone channel message (not a follow-up reply to the
        # hidden ephemeral response) so it stays public without the
        # "This message was deleted" ghost for other users.
        await interaction.channel.send(embed=embed)

        log_embed = (
            EmbedBuilder().title(emoji_title("unban", "User Unbanned"))
            .description(f"{user.mention} (`{user.id}`)")
            .color("green")
            .row(
                ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                ('Reason', reason)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)
        await log_mod_action(interaction.guild_id, str(user.id), user.name, "unban", reason, interaction.user.name)

    @app_commands.command(name="mute", description="Mute a member")
    @app_commands.describe(member="The member to mute", duration="Duration in minutes (optional)", reason="Reason for the mute (optional)")
    @is_mod()
    async def mute(self, interaction: discord.Interaction, member: discord.Member, duration: int = None, reason: str = None):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_mute", settings.get("cmd_timeout", True)):
            return await interaction.response.send_message(embed=_error_embed("Mute command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.id == interaction.user.id:
            return await interaction.response.send_message(embed=_error_embed("You cannot mute yourself."), ephemeral=True)
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=_error_embed("I cannot mute this member - their role is higher than or equal to mine."), ephemeral=True)
        if duration is None:
            duration = int(settings.get("mute_duration", 60))
        if duration <= 0:
            return await interaction.response.send_message(embed=_error_embed("Duration must be positive."), ephemeral=True)
        if duration > 40320:
            return await interaction.response.send_message(embed=_error_embed("Duration cannot exceed 28 days (40320 minutes)."), ephemeral=True)
        if not reason:
            reason = "No reason provided"

        until = discord.utils.utcnow() + datetime.timedelta(minutes=duration)
        try:
            await member.timeout(until, reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message(embed=_error_embed("I don't have permission to mute this member."), ephemeral=True)
        except discord.HTTPException as e:
            logger.error(f"Mute failed for {member.id} in {interaction.guild_id}: {e}")
            return await interaction.response.send_message(embed=_error_embed(f"Failed to mute: {e}"), ephemeral=True)

        if not settings.get("silent_mod") and settings.get("mute_message_enabled", True):
            msg_count = self.get_msg_count(interaction.guild_id, member.id)
            embed = custom_action_embed("mute", settings, member, reason, msg_count, format_duration(duration))
            if embed is not None:
                await interaction.response.send_message(embed=embed)
            else:
                msg = render_template(settings.get("mute_message", ""), member, reason, msg_count, format_duration(duration))
                if not msg:
                    msg = f"{member.mention} has been timed out."
                await interaction.response.send_message(embed=basic_action_embed("mute", msg, "orange"))
        else:
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("success", "Member Muted")).description("Done.").color("orange").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

        if settings.get("dm_on_action", True) and settings.get("mute_dm", True):
            dm_embed = self._user_dm_embed(
                emoji_title("mute", "You have been muted"),
                f"You were muted in **{interaction.guild.name}** for **{format_duration(duration)}**.\n**Reason:** {reason}",
                "orange",
            )
            await safe_dm(member, embed=dm_embed)

        log_embed = (
            EmbedBuilder().title(emoji_title("mute", "Member Muted"))
            .description(f"{member.mention} (`{member.id}`)")
            .color("orange")
            .row(
                ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                ('Duration', f'{duration} minutes ({format_duration(duration)})'),
                ('Reason', reason)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)
        await log_mod_action(interaction.guild_id, str(member.id), member.name, "mute", f"{duration}min - {reason}", interaction.user.name)
        await neon_db.set_muted_user(
            interaction.guild_id, member.id, member.name, reason, until.timestamp()
        )

    @app_commands.command(name="unmute", description="Remove a mute from a member")
    @app_commands.describe(member="The member to unmute", reason="Reason for the unmute")
    @is_mod()
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_unmute", True):
            return await interaction.response.send_message(embed=_error_embed("Unmute command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.top_role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=_error_embed("I cannot unmute this member - their role is higher than or equal to mine."), ephemeral=True)

        if not member.is_timed_out():
            return await interaction.response.send_message(embed=_error_embed("This member is not currently muted."), ephemeral=True)

        try:
            await member.timeout(None, reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message(embed=_error_embed("I don't have permission to remove this member's mute."), ephemeral=True)
        except discord.HTTPException as e:
            logger.error(f"Unmute failed for {member.id} in {interaction.guild_id}: {e}")
            return await interaction.response.send_message(embed=_error_embed(f"Failed to unmute: {e}"), ephemeral=True)

        if not settings.get("silent_mod"):
            embed = (
                EmbedBuilder().title(emoji_title("unmute", "Member Unmuted"))
                .description(f"{member.mention}'s mute has been removed.")
                .color("green")
                .row(
                    ('Reason', reason),
                    ('Moderator', interaction.user.mention)
                )
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("success", "Member Unmuted")).description("Done.").color("green").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

        if settings.get("dm_on_action", True) and settings.get("mute_dm", True):
            dm_embed = self._user_dm_embed(
                emoji_title("unmute", "You have been unmuted"),
                f"Your mute in **{interaction.guild.name}** has been removed.\n**Reason:** {reason}",
                "green",
            )
            await safe_dm(member, embed=dm_embed)

        log_embed = (
            EmbedBuilder().title(emoji_title("unmute", "Member Unmuted"))
            .description(f"{member.mention} (`{member.id}`)")
            .color("green")
            .row(
                ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                ('Reason', reason)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)
        await log_mod_action(interaction.guild_id, str(member.id), member.name, "unmute", reason, interaction.user.name)
        await neon_db.remove_muted_user(interaction.guild_id, member.id)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(member="The member to warn", reason="Warning reason (optional)")
    @is_mod()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = None):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_warn", True):
            return await interaction.response.send_message(embed=_error_embed("Warn command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if member.id == interaction.user.id:
            return await interaction.response.send_message(embed=_error_embed("You cannot warn yourself."), ephemeral=True)
        if not reason:
            reason = "No reason provided"

        if not settings.get("silent_mod") and settings.get("warn_message_enabled", True):
            msg_count = self.get_msg_count(interaction.guild_id, member.id)
            embed = custom_action_embed("warn", settings, member, reason, msg_count)
            if embed is not None:
                await interaction.response.send_message(embed=embed)
            else:
                msg = render_template(settings.get("warn_message", ""), member, reason, msg_count)
                if not msg:
                    msg = f"{member.mention} has been warned."
                await interaction.response.send_message(embed=basic_action_embed("warn", msg, "yellow"))
        else:
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("success", "Member Warned")).description("Done.").color("yellow").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

        if settings.get("dm_on_action", True) and settings.get("warn_dm", True):
            dm_embed = self._user_dm_embed(
                emoji_title("warn", "You have been warned"),
                f"You were warned in **{interaction.guild.name}**.\n**Reason:** {reason}",
                "yellow",
            )
            await safe_dm(member, embed=dm_embed)

        log_embed = (
            EmbedBuilder().title(emoji_title("warn", "Member Warned"))
            .description(f"{member.mention} (`{member.id}`)")
            .color("yellow")
            .row(
                ('Moderator', f'{interaction.user.mention} (`{interaction.user.id}`)'),
                ('Reason', reason)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)
        await log_mod_action(interaction.guild_id, str(member.id), member.name, "warn", reason, interaction.user.name)

    @app_commands.command(name="purge", description="Bulk delete messages in a channel")
    @app_commands.describe(count="Number of messages to delete (1-100)")
    @is_mod()
    async def purge(self, interaction: discord.Interaction, count: int = 10):
        settings = await get_mod_settings(interaction.guild_id)
        if not settings.get("cmd_purge", True):
            return await interaction.response.send_message(embed=_error_embed("Purge command is disabled."), ephemeral=True)
        if not await check_emergency_lock(interaction):
            return
        if count < 1 or count > 100:
            return await interaction.response.send_message(embed=_error_embed("Count must be between 1 and 100."), ephemeral=True)

        try:
            deleted = await interaction.channel.purge(limit=count)
        except discord.Forbidden:
            return await interaction.response.send_message(embed=_error_embed("I don't have permission to delete messages in this channel."), ephemeral=True)
        except discord.HTTPException as e:
            logger.error(f"Purge failed in {interaction.channel.id}: {e}")
            return await interaction.response.send_message(embed=_error_embed(f"Failed to purge: {e}"), ephemeral=True)

        embed = (
            EmbedBuilder().title(emoji_title("purge", "Messages Purged"))
            .description(f"Deleted {len(deleted)} messages.")
            .color("blue")
            .row(
                ('Channel', interaction.channel.mention),
                ('Moderator', interaction.user.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, delete_after=5)

        log_embed = (
            EmbedBuilder().title(emoji_title("purge", "Messages Purged"))
            .description(f"Deleted **{len(deleted)}** messages in {interaction.channel.mention} (`{str(interaction.channel.id)}`)")
            .color("blue")
            .row(
                ('Moderator', f'{interaction.user.mention} (`{str(interaction.user.id)}`)'),
                ('Requested', str(count))
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)
        await log_mod_action(
            interaction.guild_id,
            str(interaction.user.id),
            interaction.user.name,
            "purge",
            f"{len(deleted)} messages in #{interaction.channel.name}",
            interaction.user.name,
        )

    @app_commands.command(name="muteevasion", description="Toggle mute evasion detection")
    @app_commands.describe(enabled="Enable or disable")
    @is_mod()
    async def muteevasion(self, interaction: discord.Interaction, enabled: bool):
        settings = await get_mod_settings(interaction.guild_id)
        settings["mute_evasion"] = enabled
        await save_mod_settings(interaction.guild_id, settings)
        status = "enabled" if enabled else "disabled"
        color = "green" if enabled else "red"
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("shield", "Mute Evasion Updated")).description(f"Mute evasion detection **{status}**.").color(color).timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @app_commands.command(name="settings", description="View current moderation settings")
    @is_mod()
    async def view_settings(self, interaction: discord.Interaction):
        settings = await get_mod_settings(interaction.guild_id)
        def b(v): return "Enabled" if v else "Disabled"
        modlog_id = settings.get("modlog_channel_id")
        modlog_channel = interaction.guild.get_channel(int(modlog_id)) if modlog_id else None
        mod_roles = settings.get("mod_roles", []) or []
        embed = (
            EmbedBuilder()
            .title(emoji_title("info", "Moderation Settings"))
            .color("blue")
            .field("General", "\u200b", inline=False)
            .row(
                ('DM on Action', b(settings.get('dm_on_action'))),
                ('Require Reason', b(settings.get('require_reason'))),
                ('Silent Mod', b(settings.get('silent_mod'))),
                ('Track Stats', b(settings.get('track_stats'))),
                ('Emergency Lock', 'LOCKED' if settings.get('emergency_lock') else 'Normal'),
                ('Modlog Channel', modlog_channel.mention if modlog_channel else 'Not set'),
                ('Mod Roles', ', '.join((f'<@&{r}>' for r in mod_roles)) if mod_roles else 'None')
            )
            .field("Commands", "\u200b", inline=False)
            .row(
                ('/ban', b(settings.get('cmd_ban'))),
                ('/tempban', b(settings.get('cmd_tempban'))),
                ('/unban', b(settings.get('cmd_unban'))),
                ('/kick', b(settings.get('cmd_kick'))),
                ('/mute', b(settings.get('cmd_mute', settings.get('cmd_timeout', True)))),
                ('/unmute', b(settings.get('cmd_unmute'))),
                ('/warn', b(settings.get('cmd_warn'))),
                ('/purge', b(settings.get('cmd_purge')))
            )
            .field("DM Settings", "\u200b", inline=False)
            .row(
                ('Ban DM', b(settings.get('ban_dm'))),
                ('Tempban DM', b(settings.get('tempban_dm'))),
                ('Kick DM', b(settings.get('kick_dm'))),
                ('Mute DM', b(settings.get('mute_dm'))),
                ('Warn DM', b(settings.get('warn_dm'))),
                ('Mute Evasion', b(settings.get('mute_evasion')))
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="lockdown", description="Toggle emergency server lockdown")
    @is_mod()
    async def lockdown(self, interaction: discord.Interaction):
        settings = await get_mod_settings(interaction.guild_id)
        current = settings.get("emergency_lock", False)
        await interaction.response.defer(ephemeral=True)
        success, detail = await perform_lockdown(interaction.guild, not current, settings, save_mod_settings)
        new_state = not current
        status = "LOCKED DOWN" if new_state else "normal"
        color = "red" if new_state else "green"
        title = emoji_title("lock" if new_state else "unlock", f"Emergency Lockdown {'Enabled' if new_state else 'Lifted'}")
        if not success:
            title = emoji_title("warning", "Lockdown Failed")
            color = "grey"
        embed = (
            EmbedBuilder().title(title)
            .description(detail or f"Server is now in **{status}** mode.")
            .color(color)
            .field("Moderator", interaction.user.mention)
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        # Standalone channel message (not a follow-up reply to the hidden
        # ephemeral response) so it stays public without the
        # "This message was deleted" ghost for other users.
        await interaction.channel.send(embed=embed)
        await log_mod_action(
            interaction.guild_id,
            str(interaction.user.id),
            interaction.user.name,
            "lockdown",
            f"Set to {status} - {detail or ''}",
            interaction.user.name,
        )
        log_embed = (
            EmbedBuilder().title(title)
            .description(detail or f"Server is now in **{status}** mode.")
            .color(color)
            .field("Moderator", f"{interaction.user.mention} (`{interaction.user.id}`)")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await send_modlog(interaction.guild, settings, log_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))


