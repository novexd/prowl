import discord
from discord.ext import commands
from discord import app_commands
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import EMBED_EMOJIS, emoji_title


class ActivityRoles(commands.Cog, name="Activity Roles"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Commands ──────────────────────────────────────────────────────

    activity_group = app_commands.Group(name="activityrole", description="Activity-based role assignments")

    @activity_group.command(name="add", description="Auto-assign a role when a member plays a game")
    @app_commands.describe(activity="Game/activity name (e.g. Valorant)", role="Role to assign")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def activityrole_add(
        self,
        interaction: discord.Interaction,
        activity: str,
        role: discord.Role,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Database unavailable.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        if role.position >= interaction.guild.me.top_role.position:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Role Too High")).description("I can't assign a role equal to or higher than my top role.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        now = datetime.datetime.utcnow().timestamp()
        await pool.execute(
            "INSERT INTO activity_role_rules (guild_id, activity, role_id, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT (guild_id, activity) DO UPDATE SET role_id=?, enabled=1",
            str(interaction.guild_id),
            activity.lower().strip(),
            str(role.id),
            now,
            str(role.id),
        )

        embed = (
            EmbedBuilder()
            .title(emoji_title("check", "Activity Role Added"))
            .description(f"Playing **{activity}** will now assign {role.mention}.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activity_group.command(name="remove", description="Remove an activity role rule")
    @app_commands.describe(activity="Game/activity name to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def activityrole_remove(self, interaction: discord.Interaction, activity: str):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Database unavailable.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        result = await pool.execute(
            "DELETE FROM activity_role_rules WHERE guild_id = ? AND activity = ?",
            str(interaction.guild_id),
            activity.lower().strip(),
        )

        embed = (
            EmbedBuilder()
            .title(emoji_title("check", "Removed"))
            .description(f"Activity role for **{activity}** removed.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @activity_group.command(name="list", description="List all activity role rules")
    async def activityrole_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Database unavailable.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        rows = await pool.fetch(
            "SELECT activity, role_id, enabled FROM activity_role_rules WHERE guild_id = ? ORDER BY created_at ASC",
            str(interaction.guild_id),
        )

        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "No Activity Roles")).description("No activity roles configured. Use `/activityrole add` to create one.").color("blue").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        lines = []
        for r in rows:
            role = interaction.guild.get_role(int(r["role_id"]))
            status = "ON" if int(r["enabled"]) else "OFF"
            role_name = role.mention if role else f"Unknown ({r['role_id']})"
            lines.append(f"**{r['activity']}** → {role_name} [{status}]")

        embed = (
            EmbedBuilder()
            .title(emoji_title("game", f"Activity Roles ({len(rows)})"))
            .description("\n".join(lines))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if before.bot or before.guild is None:
            return

        try:
            pool = await neon_db.get_pool()
            if not pool:
                return

            rows = await pool.fetch(
                "SELECT activity, role_id FROM activity_role_rules WHERE guild_id = ? AND enabled = 1",
                str(before.guild.id),
            )
            if not rows:
                return

            rules = {r["activity"].lower(): r["role_id"] for r in rows}

            old_activities = {a.name.lower() for a in before.activities if a.type == discord.ActivityType.playing}
            new_activities = {a.name.lower() for a in after.activities if a.type == discord.ActivityType.playing}

            gained = new_activities - old_activities
            lost = old_activities - new_activities

            for act in gained:
                if act in rules:
                    role = before.guild.get_role(int(rules[act]))
                    if role and role < before.guild.me.top_role and role not in after.roles:
                        try:
                            await after.add_roles(role, reason="Activity role: playing " + act)
                        except Exception as e:
                            logger.warning(f"Failed to add activity role {role.id} to {after.id}: {e}")

            for act in lost:
                if act in rules:
                    role = before.guild.get_role(int(rules[act]))
                    if role and role in after.roles:
                        try:
                            await after.remove_roles(role, reason="Stopped playing " + act)
                        except Exception as e:
                            logger.warning(f"Failed to remove activity role {role.id} from {after.id}: {e}")

        except Exception as e:
            logger.error(f"Activity roles listener failed: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityRoles(bot))
