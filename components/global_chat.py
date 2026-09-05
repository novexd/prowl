import discord
from discord.ext import commands
from discord import app_commands
import datetime

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


class GlobalChat(commands.Cog, name="GlobalChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_linked_channel(self, guild_id: int):
        pool = await neon_db.get_pool()
        if not pool:
            return None
        row = await pool.fetchrow(
            "SELECT value FROM bot_stats WHERE key = ?",
            f"global_chat_channel_{guild_id}",
        )
        if not row or not row["value"] or row["value"] == "0":
            return None
        return str(row["value"])

    async def set_linked_channel(self, guild_id: int, channel_id: str):
        pool = await neon_db.get_pool()
        if not pool:
            return
        await pool.execute(
            "INSERT INTO bot_stats (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = ?",
            f"global_chat_channel_{guild_id}",
            channel_id, channel_id,
        )

    async def get_all_linked_channels(self):
        pool = await neon_db.get_pool()
        if not pool:
            return []
        rows = await pool.fetch(
            "SELECT key, value FROM bot_stats WHERE key LIKE 'global_chat_channel_%' AND value != '0' AND value != ''"
        )
        results = []
        for row in rows:
            guild_id = row["key"].rsplit("_", 1)[-1]
            channel_id = row["value"]
            results.append((guild_id, channel_id))
        return results

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        my_channel_id = await self.get_linked_channel(message.guild.id)
        if not my_channel_id:
            return
        if str(message.channel.id) != my_channel_id:
            return

        all_linked = await self.get_all_linked_channels()

        content = message.content[:1000] if message.content else "[attachment]"
        for guild_id, channel_id in all_linked:
            if str(message.guild.id) == guild_id and str(message.channel.id) == channel_id:
                continue
            target_guild = self.bot.get_guild(int(guild_id))
            if not target_guild:
                continue
            target_channel = target_guild.get_channel(int(channel_id))
            if not target_channel:
                continue
            webhooks = await target_channel.webhooks()
            webhook = discord.utils.get(webhooks, name="GlobalChat")
            if not webhook:
                try:
                    webhook = await target_channel.create_webhook(name="GlobalChat")
                except Exception as e:
                    logger.warning(f"Failed to create GlobalChat webhook: {e}")
                    continue
            try:
                await webhook.send(
                    content=content,
                    username=f"{message.author.display_name} ({message.guild.name})",
                    avatar_url=message.author.display_avatar.url,
                )
            except Exception as e:
                logger.warning(f"GlobalChat webhook send failed: {e}")
                continue

    gc_group = app_commands.Group(name="globalchat", description="Global chat commands")

    @gc_group.command(name="link", description="Link this channel to the global chat network")
    async def link(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.set_linked_channel(interaction.guild.id, str(interaction.channel_id))
        embed = (
            EmbedBuilder()
            .title(emoji_title("global_chat", "Global Chat Linked"))
            .description(f"This channel ({interaction.channel.mention}) is now linked to the global chat!")
            .color("blue")
            .field("Channel ID", str(interaction.channel_id))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @gc_group.command(name="unlink", description="Unlink this channel from the global chat")
    async def unlink(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.set_linked_channel(interaction.guild.id, "0")
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Global Chat Unlinked")).description("This channel has been unlinked from global chat.").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @gc_group.command(name="info", description="Check global chat status")
    async def info(self, interaction: discord.Interaction):
        hub_channel_id = await self.get_linked_channel(interaction.guild.id)
        if hub_channel_id:
            channel = self.bot.get_channel(int(hub_channel_id))
            embed = (
                EmbedBuilder()
                .title(emoji_title("global_chat", "Global Chat Status"))
                .description(f"Global chat is linked to {channel.mention if channel else f'<#{hub_channel_id}>'}")
                .color("blue")
                .field("Channel ID", str(hub_channel_id))
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        else:
            embed = (
                EmbedBuilder()
                .title(emoji_title("global_chat", "Global Chat Status"))
                .description("Global chat is not set up yet.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalChat(bot))
