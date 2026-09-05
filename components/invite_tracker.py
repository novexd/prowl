import discord
from discord.ext import commands
from discord import app_commands
import json
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


INVITE_DEFAULTS = {"enabled": False, "announce_channel_id": None, "ping_on_join": False}


async def get_invite_settings(guild_id: int):
    return await neon_db.load_cached_settings("invite_settings", guild_id, INVITE_DEFAULTS)


async def save_invite_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("invite_settings", guild_id, settings)


async def record_invite(guild_id: int, inviter_id: str, code: str):
    pool = await neon_db.get_pool()
    if not pool:
        return
    await pool.execute(
        "INSERT INTO invite_stats (guild_id, inviter_id, code, uses) VALUES (?, ?, ?, 1) "
        "ON CONFLICT (guild_id, inviter_id, code) DO UPDATE SET uses = invite_stats.uses + 1",
        str(guild_id), inviter_id, code,
    )


class InviteTracker(commands.Cog, name="InviteTracker"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites = {}

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                self.invites[guild.id] = await guild.invites()
            except Exception:
                self.invites[guild.id] = []

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if not guild.me.guild_permissions.manage_guild:
            return
        try:
            self.invites[guild.id] = await guild.invites()
        except Exception:
            self.invites[guild.id] = []

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.guild.me.guild_permissions.manage_guild:
            return
        before = self.invites.get(member.guild.id, [])
        try:
            after = await member.guild.invites()
        except Exception:
            return

        used = None
        for invite in before:
            found = discord.utils.get(after, code=invite.code)
            if found and found.uses > invite.uses:
                used = invite
                break
        self.invites[member.guild.id] = after

        settings = await get_invite_settings(member.guild.id)
        if not settings.get("enabled"):
            return

        channel_id = settings.get("announce_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not channel:
            return

        if used:
            inviter = used.inviter
            inviter_name = inviter.mention if inviter else "Unknown"
            if inviter:
                await record_invite(member.guild.id, str(inviter.id), used.code)
            embed = (
                EmbedBuilder()
                .title(emoji_title("invite_join", "Member Joined"))
                .description(f"{member.mention} was invited by {inviter_name}")
                .color("green")
                .row(
                    ('Invite Code', used.code),
                    ('Uses', str(used.uses)),
                    ('Account Age', discord.utils.format_dt(member.created_at, style='R'))
                )
                .thumbnail(member.display_avatar.url)
                .footer(f"User ID: {str(member.id)}")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        else:
            embed = (
                EmbedBuilder()
                .title(emoji_title("invite_join", "Member Joined"))
                .description(f"{member.mention} joined (no invite tracked)")
                .color("green")
                .field("Account Age", discord.utils.format_dt(member.created_at, style="R"))
                .thumbnail(member.display_avatar.url)
                .footer(f"User ID: {str(member.id)}")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        await channel.send(embed=embed)

    invite_group = app_commands.Group(name="invites", description="Invite tracking commands")

    @invite_group.command(name="toggle", description="Enable or disable invite tracking")
    async def toggle(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_invite_settings(interaction.guild_id)
        settings["enabled"] = not settings.get("enabled", False)
        await save_invite_settings(interaction.guild_id, settings)
        status = "enabled" if settings["enabled"] else "disabled"
        color = "green" if settings["enabled"] else "red"
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Invite Tracking")).description(f"Invite tracking **{status}**.").color(color).timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @invite_group.command(name="channel", description="Set channel for invite announcements")
    @app_commands.describe(channel="The announcement channel")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_invite_settings(interaction.guild_id)
        settings["announce_channel_id"] = str(channel.id)
        await save_invite_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Channel Set")).description(f"Invite announcements will be sent to {channel.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @invite_group.command(name="stats", description="Show invite leaderboard")
    async def stats(self, interaction: discord.Interaction):
        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Database unavailable.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        rows = await pool.fetch(
            "SELECT inviter_id, SUM(uses) as total_uses FROM invite_stats WHERE guild_id = ? GROUP BY inviter_id ORDER BY total_uses DESC LIMIT 10",
            str(interaction.guild_id),
        )
        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Invite Stats")).description("No invite data yet.").color("blue").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        lines = []
        for i, row in enumerate(rows, 1):
            user = interaction.guild.get_member(int(row["inviter_id"]))
            name = user.mention if user else f"User {row['inviter_id'][:8]}"
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"**{i}.**"
            lines.append(f"{medal} {name} - {row['total_uses']} invites")
        embed = (
            EmbedBuilder()
            .title(emoji_title("invite_stats", "Invite Leaderboard"))
            .description("\n".join(lines))
            .color("gold")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @invite_group.command(name="user", description="Show invite stats for a specific user")
    @app_commands.describe(user="The user to check")
    async def user_stats(self, interaction: discord.Interaction, user: discord.Member):
        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Database unavailable.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        rows = await pool.fetch(
            "SELECT code, uses FROM invite_stats WHERE guild_id = ? AND inviter_id = ? ORDER BY uses DESC",
            str(interaction.guild_id), str(user.id),
        )
        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Invite Stats")).description(f"{user.mention} has no recorded invites.").color("blue").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        total = sum(r["uses"] for r in rows)
        lines = [f"`{r['code']}` - {r['uses']} uses" for r in rows[:10]]
        embed = (
            EmbedBuilder()
            .title(emoji_title("invite_stats", f"{user.display_name}'s Invites"))
            .description("\n".join(lines))
            .color("blue")
            .row(
                ('Total Invites', str(total)),
                ('Unique Codes', str(len(rows)))
            )
            .footer(f"User ID: {str(user.id)}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))
