import discord
from discord.ext import commands
from discord import app_commands
import json
import re
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db


def format_response(template: str, message: discord.Message, trigger: str) -> str:
    """Replace {vars} in an autoresponder response with real values."""
    replacements = {
        "{user}": message.author.mention,
        "{mention}": message.author.mention,
        "{name}": message.author.display_name,
        "{author}": message.author.name,
        "{message}": message.content or "",
        "{trigger}": trigger,
        "{channel}": message.channel.mention,
        "{servername}": message.guild.name if message.guild else "",
        "{server}": message.guild.name if message.guild else "",
        "{membercount}": str(message.guild.member_count or 0) if message.guild else "",
    }
    out = template
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out


class Autoresponder(commands.Cog, name="Autoresponder"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers = {}
        self.cooldowns = {}

    async def load_triggers(self, guild_id: int):
        pool = await neon_db.get_pool()
        if not pool:
            return []
        rows = await pool.fetch(
            "SELECT id, trigger, response, match_type, channel_id, cooldown FROM autoresponder WHERE guild_id = ? ORDER BY created_at ASC",
            str(guild_id),
        )
        return [dict(r) for r in rows]

    async def save_trigger(self, guild_id: int, trigger: str, response: str, match_type: str, channel_id: str = None, cooldown: int = 0):
        pool = await neon_db.get_pool()
        if not pool:
            return
        await pool.execute(
            "INSERT INTO autoresponder (guild_id, trigger, response, match_type, channel_id, cooldown) VALUES (?, ?, ?, ?, ?, ?)",
            str(guild_id), trigger, response, match_type, channel_id, cooldown,
        )

    async def remove_trigger(self, guild_id: int, trigger: str):
        pool = await neon_db.get_pool()
        if not pool:
            return
        await pool.execute(
            "DELETE FROM autoresponder WHERE guild_id = ? AND trigger = ?", str(guild_id), trigger,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        triggers = await self.load_triggers(message.guild.id)
        for t in triggers:
            channel_id = t.get("channel_id")
            if channel_id and str(message.channel.id) != str(channel_id):
                continue

            cooldown = t.get("cooldown", 0)
            if cooldown > 0:
                key = (message.guild.id, t["trigger"])
                last = self.cooldowns.get(key, 0)
                now = datetime.datetime.utcnow().timestamp()
                if now - last < cooldown:
                    continue
                self.cooldowns[key] = now

            matched = False
            content = message.content or ""
            if t["match_type"] == "exact" and content.lower() == t["trigger"].lower():
                matched = True
            elif t["match_type"] == "contains" and t["trigger"].lower() in content.lower():
                matched = True
            elif t["match_type"] == "starts_with" and content.lower().startswith(t["trigger"].lower()):
                matched = True
            elif t["match_type"] == "ends_with" and content.lower().endswith(t["trigger"].lower()):
                matched = True
            elif t["match_type"] == "regex":
                try:
                    if re.search(t["trigger"], content, re.IGNORECASE):
                        matched = True
                except re.error:
                    pass

            if matched:
                try:
                    response = format_response(t["response"], message, t["trigger"])
                    await message.channel.send(response)
                except Exception as e:
                    logger.warning(f"Autoresponder failed to send: {e}")

    autoresponder_group = app_commands.Group(name="autoresponder", description="Auto-response commands")

    @autoresponder_group.command(name="add", description="Add an auto-response trigger")
    @app_commands.describe(
        trigger="The word or phrase to trigger on",
        response="The bot's response",
        match_type="How to match",
        channel="Restrict to a specific channel (optional)",
        cooldown="Cooldown in seconds (0 for none)"
    )
    @app_commands.choices(match_type=[
        app_commands.Choice(name="Exact match", value="exact"),
        app_commands.Choice(name="Contains", value="contains"),
        app_commands.Choice(name="Starts with", value="starts_with"),
        app_commands.Choice(name="Ends with", value="ends_with"),
        app_commands.Choice(name="Regex", value="regex")
    ])
    async def add(self, interaction: discord.Interaction, trigger: str, response: str, match_type: str = "contains", channel: discord.TextChannel = None, cooldown: int = 0):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if len(response) > 2000:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Too Long")).description("Response too long (max 2000 characters).").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.save_trigger(interaction.guild_id, trigger, response, match_type, str(channel.id) if channel else None, cooldown)
        embed = (
            EmbedBuilder()
            .title(emoji_title("success", "Auto-Response Added"))
            .color("success")
            .row(
                ('Trigger', f'`{trigger}`'),
                ('Response', response[:1024]),
                ('Match Type', match_type.title()),
                ('Channel', channel.mention if channel else 'All channels'),
                ('Cooldown', f'{cooldown}s' if cooldown else 'None')
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autoresponder_group.command(name="remove", description="Remove an auto-response trigger")
    @app_commands.describe(trigger="The trigger to remove")
    async def remove(self, interaction: discord.Interaction, trigger: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.remove_trigger(interaction.guild_id, trigger)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Auto-Response Removed")).description(f"Removed trigger: `{trigger}`").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @autoresponder_group.command(name="list", description="List all auto-responses")
    async def list_triggers(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        triggers = await self.load_triggers(interaction.guild_id)
        if not triggers:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("settings", "Auto-Responses")).description("No auto-responses configured.").color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        lines = []
        for t in triggers[:25]:
            channel = interaction.guild.get_channel(int(t["channel_id"])) if t.get("channel_id") else None
            channel_str = channel.mention if channel else "All"
            lines.append(f"`{t['trigger']}` → {t['response'][:50]} ({t['match_type']}) | {channel_str}")
        embed = (
            EmbedBuilder()
            .title(emoji_title("settings", "Auto-Responses"))
            .description("\n".join(lines))
            .color("brand")
            .footer(f"Total: {len(triggers)} triggers")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Autoresponder(bot))
