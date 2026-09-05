import discord
from discord.ext import commands
from discord import app_commands
import datetime
import time
import asyncio
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import EMBED_EMOJIS, emoji_title


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


async def get_temp_settings(guild_id: int):
    return await neon_db.load_cached_settings("temp_channel_settings", guild_id, TEMP_CHANNEL_DEFAULTS)


async def save_temp_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("temp_channel_settings", guild_id, settings)


class TempChannels(commands.Cog, name="Temp Channels"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._empty_checks = {}  # channel_id -> asyncio.Task

    # ── JTC Voice ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        try:
            pool = await neon_db.get_pool()
            if not pool:
                return

            # User joined a channel
            if after.channel and not before.channel:
                settings = await get_temp_settings(member.guild.id)
                if not settings.get("enabled", True):
                    return
                if not settings.get("jtc_enabled"):
                    return

                hub_id = settings.get("jtc_hub_channel")
                if not hub_id or str(after.channel.id) != str(hub_id):
                    return

                # Create temp voice channel
                await self._create_temp_voice(member, settings)

            # User left a channel
            if before.channel and not after.channel:
                await self._check_empty(before.channel)

            # User moved channels
            if before.channel and after.channel and before.channel.id != after.channel.id:
                await self._check_empty(before.channel)

        except Exception as e:
            logger.error(f"JTC listener failed: {e}")

    async def _create_temp_voice(self, member: discord.Member, settings: dict):
        pool = await neon_db.get_pool()
        if not pool:
            return

        # Check if user already owns a temp channel
        existing = await pool.fetchrow(
            "SELECT channel_id FROM temp_channels WHERE guild_id = ? AND owner_id = ? AND channel_type = 'voice'",
            str(member.guild.id), str(member.id),
        )
        if existing:
            ch = member.guild.get_channel(int(existing["channel_id"]))
            if ch:
                return  # already has one

        category_id = settings.get("jtc_category")
        category = member.guild.get_channel(int(category_id)) if category_id else None
        if not category or not isinstance(category, discord.CategoryChannel):
            category = None

        naming = settings.get("jtc_naming", "{user}'s Channel")
        name = naming.replace("{user}", member.display_name)[:100]
        limit = settings.get("jtc_default_limit", 0)

        try:
            overwrites = {
                member.guild.default_role: discord.PermissionOverwrite(connect=True),
                member: discord.PermissionOverwrite(
                    connect=True,
                    manage_channels=True,
                    move_members=True,
                ),
            }
            ch = await member.guild.create_voice_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                user_limit=limit if limit > 0 else None,
                reason=f"JTC: {member.display_name}",
            )

            # Move user to the new channel
            await member.move_to(ch, reason="JTC: moved to your channel")

            # Track in DB
            now = datetime.datetime.utcnow().timestamp()
            await pool.execute(
                "INSERT INTO temp_channels (guild_id, channel_id, owner_id, channel_type, created_at) "
                "VALUES (?, ?, ?, 'voice', ?)",
                str(member.guild.id), str(ch.id), str(member.id), now,
            )

            # Start empty check
            self._start_empty_check(ch.id, ch.guild.id)

        except Exception as e:
            logger.warning(f"Failed to create JTC voice channel: {e}")

    async def _check_empty(self, channel: discord.VoiceChannel):
        if len(channel.members) == 0:
            pool = await neon_db.get_pool()
            if not pool:
                return
            row = await pool.fetchrow(
                "SELECT channel_id FROM temp_channels WHERE guild_id = ? AND channel_id = ? AND channel_type = 'voice'",
                str(channel.guild.id), str(channel.id),
            )
            if row:
                try:
                    await channel.delete(reason="JTC: channel empty")
                except Exception:
                    pass
                await pool.execute(
                    "DELETE FROM temp_channels WHERE guild_id = ? AND channel_id = ?",
                    str(channel.guild.id), str(channel.id),
                )

    def _start_empty_check(self, channel_id: int, guild_id: int):
        key = (guild_id, channel_id)
        if key in self._empty_checks and not self._empty_checks[key].done():
            return
        self._empty_checks[key] = self.bot.loop.create_task(self._empty_check_loop(channel_id, guild_id))

    async def _empty_check_loop(self, channel_id: int, guild_id: int):
        await asyncio.sleep(10)  # Initial delay
        while True:
            ch = self.bot.get_guild(guild_id) and self.bot.get_guild(guild_id).get_channel(channel_id)
            if not ch or not isinstance(ch, discord.VoiceChannel) or len(ch.members) > 0:
                break
            try:
                await ch.delete(reason="JTC: channel empty")
            except Exception:
                pass
            pool = await neon_db.get_pool()
            if pool:
                await pool.execute(
                    "DELETE FROM temp_channels WHERE guild_id = ? AND channel_id = ?",
                    str(guild_id), str(channel_id),
                )
            break

    # ── Temp Chat ─────────────────────────────────────────────────────

    @app_commands.command(name="tempchat", description="Create a temporary text channel")
    @app_commands.describe(
        duration="Duration in minutes (default: 60)",
        name="Channel name",
    )
    async def tempchat_cmd(
        self,
        interaction: discord.Interaction,
        duration: Optional[app_commands.Range[int, 1, 10080]] = None,
        name: Optional[str] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        settings = await get_temp_settings(interaction.guild_id)
        if not settings.get("enabled", True):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Disabled")).description("Temp channels are disabled. Ask an admin to enable it.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        if not settings.get("tempchat_enabled"):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Disabled")).description("Temp chat is disabled. Ask an admin to enable it.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message("Database unavailable.", ephemeral=True)

        # Check limit
        existing = await pool.fetch(
            "SELECT channel_id FROM temp_channels WHERE guild_id = ? AND owner_id = ? AND channel_type = 'text'",
            str(interaction.guild_id), str(interaction.user.id),
        )
        if len(existing) >= 3:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Limit Reached")).description("You can have up to 3 temp channels at once.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        minutes = duration or settings.get("tempchat_default_minutes", 60)
        channel_name = (name or f"temp-{interaction.user.name[:20].lower().replace(' ', '-')}")[:100]

        category_id = settings.get("tempchat_category")
        category = interaction.guild.get_channel(int(category_id)) if category_id else None
        if not category or not isinstance(category, discord.CategoryChannel):
            category = None

        await interaction.response.defer(ephemeral=True)

        try:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                ),
            }
            ch = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Temp chat: {interaction.user.display_name}",
            )

            now = datetime.datetime.utcnow().timestamp()
            expires_at = now + (minutes * 60)

            await pool.execute(
                "INSERT INTO temp_channels (guild_id, channel_id, owner_id, channel_type, created_at, expires_at) "
                "VALUES (?, ?, ?, 'text', ?, ?)",
                str(interaction.guild_id), str(ch.id), str(interaction.user.id), now, expires_at,
            )

            # Send welcome message
            embed = (
                EmbedBuilder()
                .title(emoji_title("chat", "Temporary Channel"))
                .description(f"This channel will auto-delete in **{minutes} minutes**.\nYou can manage it with the buttons below.")
                .color("blue")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            await ch.send(embed=embed)

            # Start expiry timer
            self.bot.loop.create_task(self._expire_tempchat(ch.id, interaction.guild_id, minutes * 60))

            await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("check", "Created")).description(f"Created {ch.mention} (expires in {minutes} min)").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        except Exception as e:
            logger.warning(f"Failed to create temp chat: {e}")
            await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description(f"Could not create channel: {str(e)[:100]}").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

    async def _expire_tempchat(self, channel_id: int, guild_id: int, delay: int):
        await asyncio.sleep(delay)
        pool = await neon_db.get_pool()
        if not pool:
            return

        row = await pool.fetchrow(
            "SELECT channel_id FROM temp_channels WHERE guild_id = ? AND channel_id = ? AND channel_type = 'text'",
            str(guild_id), str(channel_id),
        )
        if not row:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        ch = guild.get_channel(channel_id)
        if ch:
            try:
                await ch.delete(reason="Temp chat: expired")
            except Exception:
                pass

        await pool.execute(
            "DELETE FROM temp_channels WHERE guild_id = ? AND channel_id = ?",
            str(guild_id), str(channel_id),
        )

    # ── Owner Commands ────────────────────────────────────────────────

    @app_commands.command(name="tempchat_close", description="Close your temporary channel")
    async def tempchat_close(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message("Database unavailable.", ephemeral=True)

        row = await pool.fetchrow(
            "SELECT channel_id FROM temp_channels WHERE guild_id = ? AND owner_id = ? AND channel_type = 'text'",
            str(interaction.guild_id), str(interaction.user.id),
        )
        if not row:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Found")).description("You don't own any temp channels.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        ch = interaction.guild.get_channel(int(row["channel_id"]))
        if ch:
            try:
                await ch.delete(reason=f"Closed by {interaction.user}")
            except Exception:
                pass

        await pool.execute(
            "DELETE FROM temp_channels WHERE guild_id = ? AND channel_id = ?",
            str(interaction.guild_id), str(row["channel_id"]),
        )

        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("check", "Closed")).description("Channel deleted.").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )

    @app_commands.command(name="tempchat_list", description="List your temporary channels")
    async def tempchat_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message("Database unavailable.", ephemeral=True)

        rows = await pool.fetch(
            "SELECT channel_id, channel_type, created_at, expires_at FROM temp_channels WHERE guild_id = ? AND owner_id = ?",
            str(interaction.guild_id), str(interaction.user.id),
        )

        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "No Temp Channels")).description("You don't own any temp channels.").color("blue").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        lines = []
        for r in rows:
            ch = interaction.guild.get_channel(int(r["channel_id"]))
            ch_type = "Voice" if r["channel_type"] == "voice" else "Text"
            name = ch.name if ch else "deleted"
            if r["channel_type"] == "text" and r.get("expires_at"):
                remaining = int(float(r["expires_at"]) - time.time())
                if remaining > 0:
                    mins = remaining // 60
                    lines.append(f"**{ch_type}** #{name} — expires in {mins}m")
                else:
                    lines.append(f"**{ch_type}** #{name} — expiring soon")
            else:
                lines.append(f"**{ch_type}** #{name}")

        embed = (
            EmbedBuilder()
            .title(emoji_title("chat", "Your Temp Channels"))
            .description("\n".join(lines))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempChannels(bot))
