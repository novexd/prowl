import discord
from discord.ext import commands
from discord import app_commands
import json
import datetime

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db


class Members(commands.Cog, name="Members"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def can_manage(self, interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.manage_roles or interaction.user.guild_permissions.administrator

    members_group = app_commands.Group(name="members", description="Member management commands")

    @members_group.command(name="list", description="List members with a specific role")
    @app_commands.describe(role="The role to filter by")
    async def list_members(self, interaction: discord.Interaction, role: discord.Role):
        if not self.can_manage(interaction):
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Roles permission.").header(emoji_title("error", "Permission Denied")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        members = [m for m in interaction.guild.members if role in m.roles]
        if not members:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description(f"No members with {role.mention}.").header(emoji_title("members", "No Members")).color("gray").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        chunks = [members[i:i+20] for i in range(0, len(members), 20)]
        embed = EmbedBuilder().description(f"Total: {len(members)}").header(emoji_title("members", f"Members with {role.name}")).color("gray").divider()
        for chunk in chunks[:5]:
            names = "\n".join(f"{m.mention} - {m.display_name}" for m in chunk)
            embed.field(role.name, names[:1000])
        embed.footer(f"Role ID: {role.id}").timestamp(datetime.datetime.utcnow())
        await interaction.response.send_message(embed=embed.build())

    @members_group.command(name="role", description="Add or remove a role from a member")
    @app_commands.describe(member="The member", role="The role")
    async def role(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if not self.can_manage(interaction):
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Roles permission.").header(emoji_title("error", "Permission Denied")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You cannot manage this role.").header(emoji_title("error", "Cannot Manage")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if role in member.roles:
            await member.remove_roles(role, reason=f"Removed by {interaction.user}")
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Removed {role.mention} from {member.mention}.").header(emoji_title("warn", "Role Removed")).color("warn").divider().field("Moderator", interaction.user.mention).timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await member.add_roles(role, reason=f"Added by {interaction.user}")
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Added {role.mention} to {member.mention}.").header(emoji_title("role", "Role Added")).color("brand").divider().field("Moderator", interaction.user.mention).timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @members_group.command(name="note", description="Add a note about a member (stored locally)")
    @app_commands.describe(member="The member", note="The note text")
    async def note(self, interaction: discord.Interaction, member: discord.Member, note: str):
        if not self.can_manage(interaction):
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Roles permission.").header(emoji_title("error", "Permission Denied")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        notes_file = f"data/notes_{interaction.guild_id}.json"
        try:
            with open(notes_file) as f:
                notes = json.load(f)
        except:
            notes = {}
        key = str(member.id)
        if key not in notes:
            notes[key] = []
        notes[key].append({"author": interaction.user.id, "note": note, "time": str(datetime.datetime.utcnow())})
        with open(notes_file, "w") as f:
            json.dump(notes, f, indent=2)
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"Note added for {member.mention}.").header(emoji_title("save", "Note Added")).color("gray").divider().field("Note", note[:1024]).timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @members_group.command(name="warnings", description="View a member's warning history")
    @app_commands.describe(member="The member")
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not self.can_manage(interaction):
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Roles permission.").header(emoji_title("error", "Permission Denied")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        pool = await neon_db.get_pool()
        if not pool:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Database unavailable.").header(emoji_title("error", "Error")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        rows = await pool.fetch(
            "SELECT action, reason, created_at FROM mod_log WHERE guild_id = ? AND user_id = ? AND action = 'warn' ORDER BY created_at DESC",
            str(interaction.guild_id), str(member.id),
        )
        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description(f"{member.mention} has no warnings.").header(emoji_title("warning", "No Warnings")).color("warn").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        embed = EmbedBuilder().description(f"Total: {len(rows)}").header(emoji_title("warn", f"Warnings for {member.display_name}")).color("warn").divider()
        for row in rows[:10]:
            reason = row["reason"] or "No reason"
            embed.field(reason[:200], discord.utils.format_dt(datetime.datetime.fromtimestamp(row["created_at"]), style="R"))
        embed.footer(f"User ID: {str(member.id)}").timestamp(datetime.datetime.utcnow())
        await interaction.response.send_message(embed=embed.build())


async def setup(bot: commands.Bot):
    await bot.add_cog(Members(bot))
