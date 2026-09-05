import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import math
import re
import json
import random
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db


MUSIC_DEFAULTS = {
    "enabled": False,
    "dj_role_id": None,
    "default_volume": 50,
    "announce_channel_id": None,
}


async def get_music_settings(guild_id: int):
    return await neon_db.load_cached_settings("music_settings", guild_id, MUSIC_DEFAULTS)


URL_REGEX = re.compile(r"https?://(?:www\.)?.+")


class MusicQueue:
    def __init__(self):
        self.queue = []
        self.current = None
        self.loop = False
        self.loop_all = False
        self.volume = 0.5

    def add(self, item: dict):
        self.queue.append(item)

    def next(self):
        if self.loop and self.current:
            return self.current
        if self.loop_all and self.current:
            self.queue.append(self.current)
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self):
        self.queue.clear()
        self.current = None

    def remove(self, index: int):
        if 0 <= index < len(self.queue):
            return self.queue.pop(index)
        return None

    def shuffle(self):
        random.shuffle(self.queue)

    def total_length(self):
        return sum(item.get("duration", 0) for item in self.queue)

    def __len__(self):
        return len(self.queue)


class MusicPlayer(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=None)
        self.cog = cog
        self.original_interaction = interaction

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Connected")).description("Not connected to a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if interaction.guild.voice_client.is_paused():
            interaction.guild.voice_client.resume()
            button.label = "⏸"
        elif interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.pause()
            button.label = "▶"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Connected")).description("Not connected to a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q = self.cog.queues.get(interaction.guild_id)
        if q:
            q.clear()
        interaction.guild.voice_client.stop()
        try:
            await interaction.guild.voice_client.disconnect()
        except Exception:
            pass
        embed = (
            EmbedBuilder()
            .title(emoji_title("music", "Stopped"))
            .description("Playback stopped and disconnected.")
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)
        self.stop()

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Connected")).description("Not connected to a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        interaction.guild.voice_client.stop()
        await self.cog.play_next(interaction.guild)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Skipped")).description("Skipped to next track.").color("brand").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self.cog.queues.get(interaction.guild_id)
        if q and len(q) > 0:
            q.shuffle()
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("music", "Shuffled")).description(f"Queue shuffled ({len(q)} tracks).").color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self.cog.queues.get(interaction.guild_id)
        if q:
            q.loop = not q.loop
            status = "enabled" if q.loop else "disabled"
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("music", "Loop")).description(f"Loop **{status}**.").color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )


class Music(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues = {}
        self.voice_states = {}

    def get_queue(self, guild_id: int) -> MusicQueue:
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    async def play_next(self, guild: discord.Guild):
        q = self.queues.get(guild.id)
        if not q:
            return
        item = q.next()
        if not item:
            return
        q.current = item
        voice = guild.voice_client
        if not voice:
            return

        try:
            source = await discord.FFmpegOpusAudio.from_probe(
                item["url"],
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                options='-vn'
            )
        except Exception as e:
            logger.error(f"Failed to create audio source: {e}")
            await self.play_next(guild)
            return

        def after(error):
            if error:
                logger.error(f"Playback error: {error}")
            coro = self.play_next(guild)
            fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            try:
                fut.result()
            except Exception:
                pass

        voice.play(source, after=after)
        if voice.source:
            voice.source = discord.PCMVolumeTransformer(voice.source)
            voice.source.volume = q.volume

    async def ensure_voice(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not in Voice")).description("You must be in a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
            return False
        voice = interaction.guild.voice_client
        if voice and voice.channel.id != interaction.user.voice.channel.id:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Already Connected")).description("I'm already in another voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
            return False
        return True

    class MusicGroup(app_commands.Group):
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild:
                await interaction.response.send_message(
                    embed=EmbedBuilder().title(emoji_title("error", "Server Only"))
                    .description("Music commands can only be used inside a server.").color("error")
                    .timestamp(datetime.datetime.utcnow()).build(),
                    ephemeral=True,
                )
                return False
            try:
                settings = await get_music_settings(interaction.guild.id)
            except Exception:
                settings = MUSIC_DEFAULTS
            if not settings.get("enabled", False):
                await interaction.response.send_message(
                    embed=EmbedBuilder().title(emoji_title("error", "Music Disabled"))
                    .description("Music is disabled in this server. An admin can enable it from the dashboard.")
                    .color("error").timestamp(datetime.datetime.utcnow()).build(),
                    ephemeral=True,
                )
                return False
            dj_role_id = settings.get("dj_role_id")
            if dj_role_id:
                role = interaction.guild.get_role(int(dj_role_id))
                is_dj = bool(role and role in interaction.user.roles)
                is_admin = interaction.user.guild_permissions.manage_guild
                if not (is_dj or is_admin):
                    await interaction.response.send_message(
                        embed=EmbedBuilder().title(emoji_title("error", "DJ Only"))
                        .description("You need the DJ role to use music commands.").color("error")
                        .timestamp(datetime.datetime.utcnow()).build(),
                        ephemeral=True,
                    )
                    return False
            return True

    music_group = MusicGroup(name="music", description="Music playback commands")

    @music_group.command(name="play", description="Play a song from a URL or search query")
    @app_commands.describe(query="Song URL or search term")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        if not await self.ensure_voice(interaction):
            return

        voice = interaction.guild.voice_client
        if not voice:
            try:
                voice = await interaction.user.voice.channel.connect()
            except Exception as e:
                return await interaction.followup.send(
                    embed=EmbedBuilder().title(emoji_title("error", "Connection Failed")).description(f"Could not connect: {str(e)[:100]}").color("error").timestamp(datetime.datetime.utcnow()).build(),
                    ephemeral=True
                )

        q = self.get_queue(interaction.guild_id)
        item = {"url": query, "title": query[:100], "duration": 0, "requester": interaction.user.name, "requester_id": str(interaction.user.id)}

        if not voice.is_playing():
            q.add(item)
            await self.play_next(interaction.guild)
            embed = (
                EmbedBuilder()
                .title(emoji_title("music", "Now Playing"))
                .description(query[:200])
                .color("brand")
                .field("Requested by", interaction.user.mention)
                .footer(f"User ID: {str(interaction.user.id)}")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        else:
            q.add(item)
            embed = (
                EmbedBuilder()
                .title(emoji_title("music", "Added to Queue"))
                .description(query[:200])
                .color("brand")
                .row(
                    ('Position', str(len(q))),
                    ('Requested by', interaction.user.mention)
                )
                .footer(f"User ID: {str(interaction.user.id)}")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )

        view = MusicPlayer(self, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @music_group.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        voice = interaction.guild.voice_client
        if not voice or not voice.is_playing():
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Nothing Playing")).description("Nothing is currently playing.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        voice.stop()
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Skipped")).description("Skipped to next track.").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="stop", description="Stop playback and clear the queue")
    async def stop_music(self, interaction: discord.Interaction):
        voice = interaction.guild.voice_client
        if not voice:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Connected")).description("Not connected to a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q = self.queues.get(interaction.guild_id)
        if q:
            q.clear()
        voice.stop()
        try:
            await voice.disconnect()
        except Exception:
            pass
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Stopped")).description("Playback stopped and disconnected.").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="queue", description="Show the current music queue")
    async def show_queue(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or (not q.queue and not q.current):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("music", "Queue")).description("Queue is empty.").color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        lines = []
        if q.current:
            lines.append(f"**Now Playing:** {q.current.get('title', 'Unknown')}")
        lines.append("**Up Next:**")
        for i, item in enumerate(q.queue[:10], 1):
            duration = item.get("duration", 0)
            dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
            lines.append(f"`{i}.` {item.get('title', 'Unknown')} [{dur_str}]")
        if len(q) > 10:
            lines.append(f"... and {len(q) - 10} more")
        total_dur = q.total_length()
        total_str = f"{total_dur // 60}:{total_dur % 60:02d}" if total_dur else "Unknown"
        embed = (
            EmbedBuilder()
            .title(emoji_title("music", "Music Queue"))
            .description("\n".join(lines))
            .color("brand")
            .row(
                ('Total Tracks', str(len(q))),
                ('Total Duration', total_str),
                ('Loop', emoji_title('check', 'On') if q.loop else emoji_title('cross', 'Off'))
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @music_group.command(name="volume", description="Set the player volume")
    @app_commands.describe(level="Volume level (0-100)")
    async def volume(self, interaction: discord.Interaction, level: int):
        if level < 0 or level > 100:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Invalid Volume")).description("Volume must be between 0 and 100.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        voice = interaction.guild.voice_client
        if not voice:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Connected")).description("Not connected to a voice channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if voice.source:
            voice.source.volume = level / 100
        q = self.queues.get(interaction.guild_id)
        if q:
            q.volume = level / 100
        vol_bar = "▓" * (level // 10) + "░" * (10 - level // 10)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Volume")).description(f"{vol_bar} **{level}%**").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="nowplaying", description="Show what's currently playing")
    async def nowplaying(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or not q.current:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Nothing Playing")).description("Nothing is currently playing.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        embed = (
            EmbedBuilder()
            .title(emoji_title("music", "Now Playing"))
            .description(q.current.get("title", "Unknown"))
            .color("brand")
            .field("Requested by", q.current.get("requester", "Unknown"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @music_group.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        voice = interaction.guild.voice_client
        if not voice or not voice.is_playing():
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Nothing Playing")).description("Nothing is currently playing.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        voice.pause()
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Paused")).description("Playback paused.").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        voice = interaction.guild.voice_client
        if not voice or not voice.is_paused():
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Paused")).description("Playback is not paused.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        voice.resume()
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Resumed")).description("Playback resumed.").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="loop", description="Toggle loop for the current track")
    async def loop(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q.loop = not q.loop
        status = "enabled" if q.loop else "disabled"
        color = "brand"
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Loop")).description(f"Loop **{status}**.").color(color).timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q.shuffle()
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Shuffled")).description(f"Queue shuffled ({len(q)} tracks).").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def remove(self, interaction: discord.Interaction, position: int):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        removed = q.remove(position - 1)
        if removed:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("music", "Removed")).description(f"Removed: {removed.get('title', 'Unknown')}").color("brand").timestamp(datetime.datetime.utcnow()).build()
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Invalid Position")).description(f"Position must be between 1 and {len(q)}.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @music_group.command(name="clear", description="Clear the entire queue")
    async def clear(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Empty Queue")).description("Queue is already empty.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        count = len(q)
        q.clear()
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("music", "Queue Cleared")).description(f"Removed {count} tracks from the queue.").color("brand").timestamp(datetime.datetime.utcnow()).build()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
