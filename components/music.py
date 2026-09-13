import discord
from discord.ext import commands
from discord import app_commands
import math
import os
import re
import json
import random
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db

try:
    import mafic
    LAVALINK_AVAILABLE = True
except ImportError:
    mafic = None
    LAVALINK_AVAILABLE = False


MUSIC_DEFAULTS = {
    "enabled": False,
    "dj_role_id": None,
    "default_volume": 50,
    "announce_channel_id": None,
}

# Max tracks taken from a single playlist add (playlists can be thousands long).
PLAYLIST_ADD_CAP = 100


def _lavalink_cfg() -> dict:
    """Node connection info from the environment (bot host side)."""
    try:
        port = int(os.environ.get("LAVALINK_PORT", "2333"))
    except (TypeError, ValueError):
        port = 2333
    return {
        "host": os.environ.get("LAVALINK_HOST", "").strip(),
        "port": port,
        "password": os.environ.get("LAVALINK_PASSWORD", ""),
        "secure": os.environ.get("LAVALINK_SECURE", "0").strip().lower() in ("1", "true", "yes"),
    }


async def get_music_settings(guild_id: int):
    return await neon_db.load_cached_settings("music_settings", guild_id, MUSIC_DEFAULTS)


async def save_music_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("music_settings", guild_id, settings)


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
        player = self.cog._get_player(interaction.guild)
        if player is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Not connected to a voice channel.").header(emoji_title("error", "Not Connected")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            if player.paused:
                await player.resume()
                button.label = "⏸"
            elif player.current is not None:
                await player.pause()
                button.label = "▶"
        except Exception:
            pass
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._get_player(interaction.guild)
        if player is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Not connected to a voice channel.").header(emoji_title("error", "Not Connected")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q = self.cog.queues.get(interaction.guild_id)
        if q:
            q.clear()
        try:
            await player.stop()
        except Exception:
            pass
        try:
            await player.disconnect()
        except Exception:
            pass
        embed = (
            EmbedBuilder()
            .description("Playback stopped and disconnected.").header(emoji_title("music", "Stopped"))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)
        self.stop()

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog._get_player(interaction.guild)
        if player is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Not connected to a voice channel.").header(emoji_title("error", "Not Connected")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            await player.stop()
        except Exception:
            pass
        await self.cog.play_next(interaction.guild)
        await interaction.response.send_message(
            embed=EmbedBuilder().description("Skipped to next track.").header(emoji_title("music", "Skipped")).color("brand").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self.cog.queues.get(interaction.guild_id)
        if q and len(q) > 0:
            q.shuffle()
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Queue shuffled ({len(q)} tracks).").header(emoji_title("music", "Shuffled")).color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = self.cog.queues.get(interaction.guild_id)
        if q:
            q.loop = not q.loop
            status = "enabled" if q.loop else "disabled"
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Loop **{status}**.").header(emoji_title("music", "Loop")).color("brand").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )


class Music(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues = {}
        self.voice_states = {}
        self.pool = None
        if LAVALINK_AVAILABLE:
            try:
                self.pool = mafic.NodePool(bot)
            except Exception as e:
                logger.warning(f"Lavalink pool init failed: {e}")
                self.pool = None
        try:
            self.bot.loop.create_task(self._connect_node())
        except Exception:
            pass

    async def _connect_node(self):
        """Connect the Lavalink node once the bot is ready (best-effort)."""
        if not LAVALINK_AVAILABLE or self.pool is None:
            return
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return
        cfg = _lavalink_cfg()
        if not cfg["host"]:
            logger.warning("Lavalink not configured (set LAVALINK_HOST/PORT/PASSWORD) - music playback disabled.")
            return
        try:
            await self.pool.create_node(
                host=cfg["host"], port=cfg["port"], label="MAIN",
                password=cfg["password"], secure=cfg["secure"],
            )
            logger.info(f"Lavalink node connected at {cfg['host']}:{cfg['port']}.")
        except Exception as e:
            logger.warning(f"Lavalink node connect failed ({cfg['host']}:{cfg['port']}): {e}")

    def _node_ready(self) -> bool:
        return bool(LAVALINK_AVAILABLE and self.pool is not None and self.pool.nodes)

    def _get_player(self, guild: discord.Guild):
        """The mafic player for a guild, or None when not on Lavalink voice."""
        vc = guild.voice_client
        if LAVALINK_AVAILABLE and isinstance(vc, mafic.Player):
            return vc
        return None

    @staticmethod
    def _should_advance(reason) -> bool:
        """Only natural ends / load failures advance the queue - user stops,
        skips (handled explicitly) and disconnects must not double-advance."""
        if not LAVALINK_AVAILABLE:
            return False
        try:
            return reason in (mafic.EndReason.FINISHED, mafic.EndReason.LOAD_FAILED)
        except Exception:
            return False

    @staticmethod
    def _build_queue_item(track, query: str, user) -> dict:
        """Display-ready queue entry keeping the playable mafic Track."""
        try:
            duration = int((track.length or 0) // 1000)
        except (TypeError, ValueError):
            duration = 0
        try:
            name = str(user.name)
        except Exception:
            name = "Unknown"
        return {
            "url": getattr(track, "uri", None) or query,
            "title": str(getattr(track, "title", None) or query)[:100],
            "author": str(getattr(track, "author", "") or ""),
            "duration": duration,
            "requester": name,
            "requester_id": str(getattr(user, "id", "")),
            "_track": track,
        }

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
        player = self._get_player(guild)
        track = item.get("_track")
        if player is None or track is None:
            return
        try:
            await player.play(track, volume=int(q.volume * 100))
        except Exception as e:
            logger.warning(f"Lavalink play failed in {guild.id}: {e}")
            await self.play_next(guild)

    @commands.Cog.listener("on_track_end")
    async def _on_track_end(self, event):
        """Advance the queue when a track ends naturally or fails to load."""
        if not LAVALINK_AVAILABLE:
            return
        try:
            reason = event.reason
            guild = event.player.guild
        except AttributeError:
            return
        if not self._should_advance(reason) or guild is None:
            return
        await self.play_next(guild)

    @commands.Cog.listener("on_track_exception")
    async def _on_track_exception(self, event):
        if not LAVALINK_AVAILABLE:
            return
        try:
            guild = event.player.guild
        except AttributeError:
            return
        if guild is None:
            return
        logger.warning(f"Lavalink track exception: {getattr(event, 'exception', '?')}")
        await self.play_next(guild)

    @commands.Cog.listener("on_track_stuck")
    async def _on_track_stuck(self, event):
        if not LAVALINK_AVAILABLE:
            return
        try:
            guild = event.player.guild
        except AttributeError:
            return
        if guild is None:
            return
        logger.warning("Lavalink track stuck, skipping.")
        await self.play_next(guild)

    def _node_unavailable_embed(self):
        return EmbedBuilder().description(
            "The music node is offline or not configured. Try again in a bit."
        ).header(emoji_title("error", "Music Unavailable")).color("error").timestamp(datetime.datetime.utcnow()).build()

    async def _reply_error(self, interaction: discord.Interaction, embed):
        """Ephemeral error reply that works before and after defer()."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    async def ensure_voice(self, interaction: discord.Interaction):
        """Return a connected mafic player for the invoker's channel, or None
        (with an error already sent)."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await self._reply_error(interaction,
                EmbedBuilder().description("You must be in a voice channel.").header(emoji_title("error", "Not in Voice")).color("error").timestamp(datetime.datetime.utcnow()).build())
            return None
        if not self._node_ready():
            await self._reply_error(interaction, self._node_unavailable_embed())
            return None
        channel = interaction.user.voice.channel
        player = self._get_player(interaction.guild)
        if player is not None and player.channel.id != channel.id:
            await self._reply_error(interaction,
                EmbedBuilder().description("I'm already in another voice channel.").header(emoji_title("error", "Already Connected")).color("error").timestamp(datetime.datetime.utcnow()).build())
            return None
        if player is None:
            try:
                player = await channel.connect(cls=mafic.Player)
            except Exception as e:
                await self._reply_error(interaction,
                    EmbedBuilder().description(f"Could not connect: {str(e)[:100]}").header(emoji_title("error", "Connection Failed")).color("error").timestamp(datetime.datetime.utcnow()).build())
                return None
        return player

    class MusicGroup(app_commands.Group):
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild:
                await interaction.response.send_message(
                    embed=EmbedBuilder()
                    .description("Music commands can only be used inside a server.").header(emoji_title("error", "Server Only")).color("error")
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
                    embed=EmbedBuilder()
                    .description("Music is disabled in this server. An admin can enable it from the dashboard.").header(emoji_title("error", "Music Disabled"))
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
                        embed=EmbedBuilder()
                        .description("You need the DJ role to use music commands.").header(emoji_title("error", "DJ Only")).color("error")
                        .timestamp(datetime.datetime.utcnow()).build(),
                        ephemeral=True,
                    )
                    return False
            return True

    music_group = MusicGroup(name="music", description="Music playback commands")

    @app_commands.command(name="music-toggle", description="Enable or disable music for this server (admin only)")
    @app_commands.describe(enabled="True to enable music, False to disable it")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_toggle(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Music commands can only be used inside a server.").header(emoji_title("error", "Server Only")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        settings = await get_music_settings(interaction.guild_id)
        settings["enabled"] = bool(enabled)
        await save_music_settings(interaction.guild_id, settings)
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"Music is now **{state}** in this server.").header(emoji_title("music", "Music Toggled")).color("brand").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )

    @music_group.command(name="play", description="Play a song from a URL or search query")
    @app_commands.describe(query="Song URL or search term")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        player = await self.ensure_voice(interaction)
        if player is None:
            return

        try:
            loaded = await player.fetch_tracks(query)
        except Exception as e:
            logger.warning(f"Lavalink search failed: {e}")
            loaded = None
        tracks = []
        playlist_name = None
        if isinstance(loaded, mafic.Playlist):
            playlist_name = loaded.name
            tracks = list(loaded.tracks)
        elif isinstance(loaded, list):
            tracks = loaded
        if not tracks:
            return await interaction.followup.send(
                embed=EmbedBuilder().description(f"No results for `{query[:100]}`.").header(emoji_title("error", "Nothing Found")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

        q = self.get_queue(interaction.guild_id)
        added = 0
        for track in tracks[:PLAYLIST_ADD_CAP]:
            q.add(self._build_queue_item(track, query, interaction.user))
            added += 1

        if player.current is None and not player.paused:
            await self.play_next(interaction.guild)
            first = q.current or {}
            embed = (
                EmbedBuilder()
                .description(f"{first.get('title', query)[:200]}" + (f"\n*{added - 1} more from **{playlist_name}** queued*" if playlist_name and added > 1 else ""))
                .header(emoji_title("music", "Now Playing"))
                .color("brand")
                .divider().row(
                    ('Requested by', interaction.user.mention),
                    ('Duration', f"{(first.get('duration') or 0) // 60}:{(first.get('duration') or 0) % 60:02d}" if first.get('duration') else "Live"),
                )
                .footer(f"User ID: {str(interaction.user.id)}")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        else:
            if playlist_name:
                desc = f"Queued **{added}** tracks from **{playlist_name}**"
            else:
                first = tracks[0]
                desc = f"{getattr(first, 'title', query)[:200]}"
            embed = (
                EmbedBuilder()
                .description(desc).header(emoji_title("music", "Added to Queue"))
                .color("brand")
                .divider().row(
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
        player = self._get_player(interaction.guild)
        if player is None or player.current is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Nothing is currently playing.").header(emoji_title("error", "Nothing Playing")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            await player.stop()
        except Exception:
            pass
        await self.play_next(interaction.guild)
        await interaction.response.send_message(
            embed=EmbedBuilder().description("Skipped to next track.").header(emoji_title("music", "Skipped")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="stop", description="Stop playback and clear the queue")
    async def stop_music(self, interaction: discord.Interaction):
        player = self._get_player(interaction.guild)
        if player is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Not connected to a voice channel.").header(emoji_title("error", "Not Connected")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q = self.queues.get(interaction.guild_id)
        if q:
            q.clear()
        try:
            await player.stop()
        except Exception:
            pass
        try:
            await player.disconnect()
        except Exception:
            pass
        await interaction.response.send_message(
            embed=EmbedBuilder().description("Playback stopped and disconnected.").header(emoji_title("music", "Stopped")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="queue", description="Show the current music queue")
    async def show_queue(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or (not q.queue and not q.current):
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("music", "Queue")).color("brand").timestamp(datetime.datetime.utcnow()).build(),
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
            .description("\n".join(lines)).header(emoji_title("music", "Music Queue"))
            .color("brand")
            .divider().row(
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
                embed=EmbedBuilder().description("Volume must be between 0 and 100.").header(emoji_title("error", "Invalid Volume")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        player = self._get_player(interaction.guild)
        if player is None:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Not connected to a voice channel.").header(emoji_title("error", "Not Connected")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            await player.set_volume(level)
        except Exception as e:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Could not set volume: {str(e)[:100]}").header(emoji_title("error", "Volume Failed")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q = self.queues.get(interaction.guild_id)
        if q:
            q.volume = level / 100
        vol_bar = "▓" * (level // 10) + "░" * (10 - level // 10)
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"{vol_bar} **{level}%**").header(emoji_title("music", "Volume")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="nowplaying", description="Show what's currently playing")
    async def nowplaying(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or not q.current:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Nothing is currently playing.").header(emoji_title("error", "Nothing Playing")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        embed = (
            EmbedBuilder()
            .description(q.current.get("title", "Unknown")).header(emoji_title("music", "Now Playing"))
            .color("brand")
            .divider().field("Requested by", q.current.get("requester", "Unknown"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @music_group.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        player = self._get_player(interaction.guild)
        if player is None or player.current is None or player.paused:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Nothing is currently playing.").header(emoji_title("error", "Nothing Playing")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            await player.pause()
        except Exception as e:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Could not pause: {str(e)[:100]}").header(emoji_title("error", "Pause Failed")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await interaction.response.send_message(
            embed=EmbedBuilder().description("Playback paused.").header(emoji_title("music", "Paused")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        player = self._get_player(interaction.guild)
        if player is None or not player.paused:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Playback is not paused.").header(emoji_title("error", "Not Paused")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        try:
            await player.resume()
        except Exception as e:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Could not resume: {str(e)[:100]}").header(emoji_title("error", "Resume Failed")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await interaction.response.send_message(
            embed=EmbedBuilder().description("Playback resumed.").header(emoji_title("music", "Resumed")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="loop", description="Toggle loop for the current track")
    async def loop(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q.loop = not q.loop
        status = "enabled" if q.loop else "disabled"
        color = "brand"
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"Loop **{status}**.").header(emoji_title("music", "Loop")).color(color).timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        q.shuffle()
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"Queue shuffled ({len(q)} tracks).").header(emoji_title("music", "Shuffled")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )

    @music_group.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def remove(self, interaction: discord.Interaction, position: int):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        removed = q.remove(position - 1)
        if removed:
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Removed: {removed.get('title', 'Unknown')}").header(emoji_title("music", "Removed")).color("brand").timestamp(datetime.datetime.utcnow()).build()
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().description(f"Position must be between 1 and {len(q)}.").header(emoji_title("error", "Invalid Position")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @music_group.command(name="clear", description="Clear the entire queue")
    async def clear(self, interaction: discord.Interaction):
        q = self.queues.get(interaction.guild_id)
        if not q or len(q) == 0:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("Queue is already empty.").header(emoji_title("error", "Empty Queue")).color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        count = len(q)
        q.clear()
        await interaction.response.send_message(
            embed=EmbedBuilder().description(f"Removed {count} tracks from the queue.").header(emoji_title("music", "Queue Cleared")).color("brand").timestamp(datetime.datetime.utcnow()).build()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
