import discord
from discord.ext import commands
from discord import app_commands
import datetime

from Ediscord import EmbedBuilder
from Ediscord.builders import emoji_title


class IdLookup(commands.Cog, name="ID"):
    """Look up IDs for members, roles, channels, emojis, and more."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="id", description="Get the ID of a member, role, channel, or emoji")
    @app_commands.describe(
        member="A server member",
        role="A server role",
        channel="A server channel",
        emoji="A custom emoji",
    )
    async def get_id(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None,
        role: discord.Role = None,
        channel: discord.abc.GuildChannel = None,
        emoji: str = None,
    ):
        if not any([member, role, channel, emoji]):
            return await interaction.response.send_message(
                embed=EmbedBuilder()
                .title(emoji_title("error", "Nothing Provided"))
                .description("Pass at least one target: a member, role, channel, or emoji.")
                .color("error")
                .timestamp(datetime.datetime.utcnow())
                .build(),
                ephemeral=True,
            )

        fields = []
        if member:
            fields.append(("Member", f"`{member.id}`", True))
            fields.append(("Tag", f"{member.name}#{member.discriminator}", True))
            fields.append(("Mention", member.mention, True))
        if role:
            fields.append(("Role", f"`{role.id}`", True))
            fields.append(("Mention", role.mention, True))
            fields.append(("Position", str(role.position), True))
        if channel:
            fields.append(("Channel", f"`{channel.id}`", True))
            fields.append(("Mention", channel.mention, True))
            fields.append(("Type", str(channel.type).title(), True))
        if emoji:
            parsed = await self._parse_emoji(interaction.guild, emoji)
            if parsed:
                fields.append(("Emoji", f"`{parsed['id']}`", True))
                fields.append(("Name", parsed["name"], True))
                fields.append(("Animated", "Yes" if parsed["animated"] else "No", True))
            else:
                fields.append(("Emoji", f"Could not parse `{emoji}`", True))

        embed = (
            EmbedBuilder()
                .title(emoji_title("tag", "ID Lookup"))
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
        )
        for name, value, inline in fields:
            embed.field(name, value, inline=inline)
        embed.footer(f"Requested by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed.build(), ephemeral=True)

    async def _parse_emoji(self, guild, raw: str):
        """Try to resolve a raw emoji string like <a:name:id> or <name:id> or just an id."""
        import re
        m = re.match(r"<a?:(\w+):(\d+)>", raw)
        if m:
            return {"name": m.group(1), "id": m.group(2), "animated": raw.startswith("<a:")}
        if raw.isdigit():
            try:
                emoji_obj = await guild.fetch_emoji(int(raw))
                return {"name": emoji_obj.name, "id": str(emoji_obj.id), "animated": emoji_obj.animated}
            except (discord.NotFound, discord.HTTPException):
                return None
        return None

    @app_commands.command(name="roleid", description="Get the ID of a role by name (autocomplete)")
    @app_commands.describe(role="The role to look up")
    async def role_id(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("tag", "Role ID"))
            .description(f"**{role.name}**\n`{role.id}`")
            .color(role.color if role.color != discord.Color.default() else "gray")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @app_commands.command(name="channelid", description="Get the ID of a channel")
    @app_commands.describe(channel="The channel to look up")
    async def channel_id(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel = None):
        target = channel or interaction.channel
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("tag", "Channel ID"))
            .description(f"**{target.name}**\n`{target.id}`")
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @app_commands.command(name="serverid", description="Get this server's ID")
    async def server_id(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("tag", "Server ID"))
            .description(f"**{interaction.guild.name}**\n`{interaction.guild.id}`")
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(IdLookup(bot))
