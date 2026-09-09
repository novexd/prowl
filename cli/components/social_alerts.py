import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import aiohttp
import asyncio
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


SOCIAL_DEFAULTS = {
    "enabled": True,
    "youtube_enabled": False,
    "youtube_channel_id": None,
    "youtube_ping_role": None,
    "youtube_announce_channel_id": None,
    "youtube_message": None,
    "twitch_enabled": False,
    "twitch_channel": None,
    "twitch_ping_role": None,
    "twitch_announce_channel_id": None,
    "twitch_message": None,
    "twitter_enabled": False,
    "twitter_handle": None,
    "twitter_ping_role": None,
    "twitter_announce_channel_id": None,
    "twitter_message": None,
    "reddit_enabled": False,
    "reddit_subreddit": None,
    "reddit_ping_role": None,
    "reddit_announce_channel_id": None,
    "reddit_message": None,
    "github_enabled": False,
    "github_repo": None,
    "github_ping_role": None,
    "github_announce_channel_id": None,
    "github_message": None,
    "tiktok_enabled": False,
    "tiktok_username": None,
    "tiktok_ping_role": None,
    "tiktok_announce_channel_id": None,
    "tiktok_message": None,
    # Defaults (fallbacks, overwritten by per-platform values)
    "default_announce_channel_id": None,
    "default_ping_role": None,
    # Extra alerts per platform: { "youtube": [{target, ping_role, message}], ... }
    "extra_alerts": {},
}


async def get_social_settings(guild_id: int):
    return await neon_db.load_cached_settings("social_settings", guild_id, SOCIAL_DEFAULTS)


async def save_social_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("social_settings", guild_id, settings)


class SocialAlerts(commands.Cog, name="SocialAlerts"):
    DEFAULT_MESSAGES = {
        "youtube": "New video from {channel}!",
        "twitch": "🔴 {channel} is now live!",
        "twitter": "New post from @{channel}!",
        "reddit": "New post in r/{channel}!",
        "github": "New release from {channel}!",
        "tiktok": "New TikTok from @{channel}!",
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_videos = {}
        self.check_youtube.start()
        self.check_twitch.start()
        self.check_twitter.start()
        self.check_reddit.start()
        self.check_github.start()
        self.check_tiktok.start()

    def cog_unload(self):
        self.check_youtube.cancel()
        self.check_twitch.cancel()
        self.check_twitter.cancel()
        self.check_reddit.cancel()
        self.check_github.cancel()
        self.check_tiktok.cancel()

    def _resolve_channel(self, guild: discord.Guild, channel_id, fallback_id=None) -> Optional[discord.TextChannel]:
        for cid in (channel_id, fallback_id):
            if not cid:
                continue
            channel = guild.get_channel(int(cid))
            if channel and isinstance(channel, discord.TextChannel):
                return channel
        return None

    def _resolve_role(self, guild: discord.Guild, role_id, fallback_id=None) -> Optional[discord.Role]:
        for rid in (role_id, fallback_id):
            if not rid:
                continue
            role = guild.get_role(int(rid))
            if role:
                return role
        return None

    async def _send_alert(self, guild: discord.Guild, settings: dict, platform: str, channel_name: str,
                          custom_msg=None, ping_role_id=None, announce_channel_id=None, url=None,
                          extra_fields=None):
        """Post an alert with the fallback chain: custom msg → default, role → default role, channel → default channel."""
        channel = self._resolve_channel(guild, announce_channel_id, settings.get("default_announce_channel_id"))
        if not channel:
            return
        role = self._resolve_role(guild, ping_role_id, settings.get("default_ping_role"))
        mode = settings.get(f"{platform}_message_mode", "basic")
        if mode == "custom" and settings.get(f"{platform}_embed"):
            embed_data = dict(settings[f"{platform}_embed"])
            for k in ("title", "description", "footer_text"):
                if k in embed_data and isinstance(embed_data[k], str):
                    embed_data[k] = embed_data[k].replace("{channel}", channel_name).replace("{name}", channel_name).replace("{url}", url or "")
            color = embed_data.get("color")
            if isinstance(color, str):
                color = int(color.lstrip("#"), 16) if color.startswith("#") else int(color)
            embed = discord.Embed(
                title=embed_data.get("title", ""),
                description=embed_data.get("description", ""),
                url=embed_data.get("url"),
                color=color or discord.Color.blurple(),
            )
            if embed_data.get("author_name"):
                embed.set_author(name=embed_data["author_name"], icon_url=embed_data.get("author_icon"))
            if embed_data.get("footer_text"):
                embed.set_footer(text=embed_data["footer_text"], icon_url=embed_data.get("footer_icon"))
            if embed_data.get("image_url"):
                embed.set_image(url=embed_data["image_url"])
            if embed_data.get("thumbnail_url"):
                embed.set_thumbnail(url=embed_data["thumbnail_url"])
            if extra_fields:
                for fname, fval in extra_fields:
                    embed.add_field(name=fname, value=str(fval), inline=True)
            if role:
                content = role.mention
            else:
                content = None
            try:
                await channel.send(content=content, embed=embed)
                logger.info(f"Posted {platform} embed alert for {channel_name} in {guild.id}")
            except Exception as e:
                logger.error(f"Social embed alert send failed for {guild.id}: {e}")
            return
        text = (custom_msg or self.DEFAULT_MESSAGES.get(platform, ""))
        text = text.replace("{channel}", channel_name).replace("{name}", channel_name).replace("{url}", url or "")
        if role:
            text = f"{role.mention} {text}"
        if url and "{url}" not in (custom_msg or ""):
            text = f"{text} {url}"
        try:
            await channel.send(text)
            logger.info(f"Posted {platform} alert for {channel_name} in {guild.id}")
        except Exception as e:
            logger.error(f"Social alert send failed for {guild.id}: {e}")

    async def _check_youtube_channel(self, guild, settings, cid, channel_name, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        import xml.etree.ElementTree as ET
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.text()
        except Exception:
            return
        try:
            root = ET.fromstring(data)
        except Exception:
            return
        entry = root.find("entry")
        if entry is None:
            return
        vid = entry.findtext("{http://www.youtube.com/xml/schemas/2015}videoId")
        title = entry.findtext("title") or channel_name
        if not vid:
            return
        key = f"{guild.id}:{cid}"
        if self._last_videos.get(key) == vid:
            return
        self._last_videos[key] = vid
        video_url = f"https://www.youtube.com/watch?v={vid}"
        await self._send_alert(guild, settings, "youtube", title, custom_msg, ping_role_id, announce_channel_id, video_url)

    @tasks.loop(minutes=10)
    async def check_youtube(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("youtube_enabled"):
                    continue
                cid = settings.get("youtube_channel_id")
                if cid:
                    await self._check_youtube_channel(guild, settings, cid, cid,
                                                      settings.get("youtube_message"), settings.get("youtube_ping_role"),
                                                      settings.get("youtube_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("youtube", []):
                    if ea.get("target"):
                        await self._check_youtube_channel(guild, settings, ea["target"], ea["target"],
                                                          ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"YouTube check failed for {guild.id}: {e}")

    @check_youtube.before_loop
    async def before_check(self):
        await asyncio.sleep(30)

    # ── Twitch (uses Helix API; no-op without TWITCH_CLIENT_ID/SECRET) ──
    @tasks.loop(minutes=5)
    async def check_twitch(self):
        await self.bot.wait_until_ready()
        from Ediscord.variables import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
        if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
            return
        token = await self._twitch_token()
        if not token:
            return
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("twitch_enabled"):
                    continue
                channel = settings.get("twitch_channel")
                if channel:
                    await self._check_twitch_channel(guild, settings, channel, token,
                                                     settings.get("twitch_message"), settings.get("twitch_ping_role"),
                                                     settings.get("twitch_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("twitch", []):
                    if ea.get("target"):
                        await self._check_twitch_channel(guild, settings, ea["target"], token,
                                                         ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"Twitch check failed for {guild.id}: {e}")

    async def _twitch_token(self):
        from Ediscord.variables import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post("https://id.twitch.tv/oauth2/token",
                                  params={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET,
                                          "grant_type": "client_credentials"},
                                  headers={"Client-ID": TWITCH_CLIENT_ID}) as resp:
                    data = await resp.json()
                    return data.get("access_token")
        except Exception:
            return None

    async def _check_twitch_channel(self, guild, settings, channel, token, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        from Ediscord.variables import TWITCH_CLIENT_ID
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.twitch.tv/helix/streams",
                                 params={"user_login": channel},
                                 headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}) as resp:
                    data = await resp.json()
            if data.get("data"):
                stream = data["data"][0]
                title = stream.get("title") or channel
                stream_id = stream.get("id") or channel
                url = f"https://www.twitch.tv/{channel}"
                if self._last_videos.get(f"{guild.id}:twitch:{channel}") == stream_id:
                    return
                self._last_videos[f"{guild.id}:twitch:{channel}"] = stream_id
                await self._send_alert(guild, settings, "twitch", title, custom_msg, ping_role_id, announce_channel_id, url)
        except Exception as e:
            logger.debug(f"Twitch channel check failed: {e}")

    # ── Twitter/X (uses Nitter RSS bridge - free, may be unreliable) ──
    @tasks.loop(minutes=10)
    async def check_twitter(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("twitter_enabled"):
                    continue
                handle = settings.get("twitter_handle")
                if handle:
                    await self._check_twitter_handle(guild, settings, handle,
                                                     settings.get("twitter_message"), settings.get("twitter_ping_role"),
                                                     settings.get("twitter_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("twitter", []):
                    if ea.get("target"):
                        await self._check_twitter_handle(guild, settings, ea["target"],
                                                         ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"Twitter check failed for {guild.id}: {e}")

    async def _check_twitter_handle(self, guild, settings, handle, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        import xml.etree.ElementTree as ET
        from Ediscord.variables import NITTER_INSTANCE
        instance = NITTER_INSTANCE.rstrip("/")
        url = f"{instance}/{handle}/rss"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=15, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status != 200:
                        logger.debug(f"Nitter RSS {instance} returned {resp.status} for @{handle}")
                        return
                    data = await resp.text()
        except Exception as e:
            logger.debug(f"Nitter fetch failed for @{handle}: {e}")
            return
        try:
            root = ET.fromstring(data)
        except Exception:
            return
        item = root.find(".//item")
        if item is None:
            return
        link = item.findtext("link") or ""
        title = item.findtext("title") or f"@{handle}"
        status_id = link.rstrip("/").split("/")[-1]
        key = f"{guild.id}:twitter:{handle}"
        if self._last_videos.get(key) == status_id:
            return
        self._last_videos[key] = status_id
        tweet_url = f"https://twitter.com/{handle}/status/{status_id}"
        await self._send_alert(guild, settings, "twitter", f"@{handle}", custom_msg, ping_role_id, announce_channel_id, tweet_url)

    # ── Reddit (RSS feed) ──
    @tasks.loop(minutes=10)
    async def check_reddit(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("reddit_enabled"):
                    continue
                sub = settings.get("reddit_subreddit")
                if sub:
                    await self._check_reddit_sub(guild, settings, sub,
                                                 settings.get("reddit_message"), settings.get("reddit_ping_role"),
                                                 settings.get("reddit_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("reddit", []):
                    if ea.get("target"):
                        await self._check_reddit_sub(guild, settings, ea["target"],
                                                     ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"Reddit check failed for {guild.id}: {e}")

    async def _check_reddit_sub(self, guild, settings, subreddit, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        import xml.etree.ElementTree as ET
        sub = subreddit.lstrip("r/")
        url = f"https://www.reddit.com/r/{sub}/new/.rss?limit=1"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=15, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.text()
        except Exception:
            return
        try:
            root = ET.fromstring(data)
        except Exception:
            return
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return
        post_id = entry.findtext("atom:id", namespaces=ns) or ""
        title = entry.findtext("atom:title", namespaces=ns) or f"r/{sub}"
        link_el = entry.find("atom:link", ns)
        post_url = link_el.get("href", "") if link_el is not None else ""
        key = f"{guild.id}:reddit:{sub}"
        if self._last_videos.get(key) == post_id:
            return
        self._last_videos[key] = post_id
        await self._send_alert(guild, settings, "reddit", f"r/{sub}", custom_msg, ping_role_id, announce_channel_id, post_url)

    # ── GitHub (RSS feed for releases) ──
    @tasks.loop(minutes=15)
    async def check_github(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("github_enabled"):
                    continue
                repo = settings.get("github_repo")
                if repo:
                    await self._check_github_repo(guild, settings, repo,
                                                  settings.get("github_message"), settings.get("github_ping_role"),
                                                  settings.get("github_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("github", []):
                    if ea.get("target"):
                        await self._check_github_repo(guild, settings, ea["target"],
                                                      ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"GitHub check failed for {guild.id}: {e}")

    async def _check_github_repo(self, guild, settings, repo, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        import xml.etree.ElementTree as ET
        repo = repo.lstrip("/")
        url = f"https://github.com/{repo}/releases.atom"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=15, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.text()
        except Exception:
            return
        try:
            root = ET.fromstring(data)
        except Exception:
            return
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return
        release_id = entry.findtext("atom:id", namespaces=ns) or ""
        title = entry.findtext("atom:title", namespaces=ns) or repo
        link_el = entry.find("atom:link", ns)
        release_url = link_el.get("href", "") if link_el is not None else ""
        key = f"{guild.id}:github:{repo}"
        if self._last_videos.get(key) == release_id:
            return
        self._last_videos[key] = release_id
        extra_fields = []
        if settings.get("github_field_stars") or settings.get("github_field_forks") or settings.get("github_field_watchers") or settings.get("github_field_issues") or settings.get("github_field_license"):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"https://api.github.com/repos/{repo}", timeout=10, headers={"User-Agent": "ProwlBot/1.0", "Accept": "application/vnd.github.v3+json"}) as resp:
                        if resp.status == 200:
                            rdata = await resp.json()
                            if settings.get("github_field_stars"):
                                extra_fields.append(("Stars", rdata.get("stargazers_count", 0)))
                            if settings.get("github_field_forks"):
                                extra_fields.append(("Forks", rdata.get("forks_count", 0)))
                            if settings.get("github_field_watchers"):
                                extra_fields.append(("Watchers", rdata.get("subscribers_count", 0)))
                            if settings.get("github_field_issues"):
                                extra_fields.append(("Open Issues", rdata.get("open_issues_count", 0)))
                            if settings.get("github_field_license"):
                                lic = rdata.get("license")
                                if lic:
                                    extra_fields.append(("License", lic.get("spdx_id", "Unknown")))
            except Exception:
                pass
        await self._send_alert(guild, settings, "github", repo, custom_msg, ping_role_id, announce_channel_id, release_url, extra_fields=extra_fields or None)

    # ── TikTok (via RSSHub) ──
    @tasks.loop(minutes=10)
    async def check_tiktok(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                settings = await get_social_settings(guild.id)
                if not settings.get("tiktok_enabled"):
                    continue
                username = settings.get("tiktok_username")
                if username:
                    await self._check_tiktok_user(guild, settings, username,
                                                  settings.get("tiktok_message"), settings.get("tiktok_ping_role"),
                                                  settings.get("tiktok_announce_channel_id"))
                for ea in settings.get("extra_alerts", {}).get("tiktok", []):
                    if ea.get("target"):
                        await self._check_tiktok_user(guild, settings, ea["target"],
                                                      ea.get("message"), ea.get("ping_role"), ea.get("announce_channel_id"))
            except Exception as e:
                logger.debug(f"TikTok check failed for {guild.id}: {e}")

    async def _check_tiktok_user(self, guild, settings, username, custom_msg=None, ping_role_id=None, announce_channel_id=None):
        import xml.etree.ElementTree as ET
        username = username.lstrip("@")
        url = f"https://rsshub.app/tiktok/user/{username}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=15, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.text()
        except Exception:
            return
        try:
            root = ET.fromstring(data)
        except Exception:
            return
        item = root.find(".//item")
        if item is None:
            return
        link = item.findtext("link") or ""
        title = item.findtext("title") or f"@{username}"
        video_id = link.rstrip("/").split("/")[-1]
        key = f"{guild.id}:tiktok:{username}"
        if self._last_videos.get(key) == video_id:
            return
        self._last_videos[key] = video_id
        video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"
        await self._send_alert(guild, settings, "tiktok", f"@{username}", custom_msg, ping_role_id, announce_channel_id, video_url)

    social_group = app_commands.Group(name="social", description="Social media alert settings")

    @social_group.command(name="youtube", description="Set YouTube channel for upload alerts")
    @app_commands.describe(youtube_channel_id="Your YouTube channel ID", ping_role="Role to ping on upload", announce_channel="Channel for YouTube announcements")
    async def set_youtube(self, interaction: discord.Interaction, youtube_channel_id: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["youtube_enabled"] = True
        settings["youtube_channel_id"] = youtube_channel_id
        settings["youtube_ping_role"] = str(ping_role.id) if ping_role else None
        settings["youtube_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "YouTube Alerts Set Up"))
            .color("warn")
            .row(
                ('Channel ID', f'`{youtube_channel_id}`'),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="twitch", description="Set Twitch channel for stream alerts")
    @app_commands.describe(twitch_channel="Your Twitch channel name", ping_role="Role to ping on stream", announce_channel="Channel for Twitch announcements")
    async def set_twitch(self, interaction: discord.Interaction, twitch_channel: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["twitch_enabled"] = True
        settings["twitch_channel"] = twitch_channel
        settings["twitch_ping_role"] = str(ping_role.id) if ping_role else None
        settings["twitch_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "Twitch Alerts Set Up"))
            .color("warn")
            .row(
                ('Channel', f'`{twitch_channel}`'),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="twitter", description="Set Twitter/X handle for post alerts")
    @app_commands.describe(handle="Twitter/X handle (without @)", ping_role="Role to ping on post", announce_channel="Channel for Twitter/X announcements")
    async def set_twitter(self, interaction: discord.Interaction, handle: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["twitter_enabled"] = True
        settings["twitter_handle"] = handle.lstrip("@")
        settings["twitter_ping_role"] = str(ping_role.id) if ping_role else None
        settings["twitter_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "Twitter/X Alerts Set Up"))
            .color("warn")
            .row(
                ('Handle', f"@{handle.lstrip('@')}"),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="reddit", description="Set subreddit for post alerts")
    @app_commands.describe(subreddit="Subreddit name (e.g. 'python' or 'r/python')", ping_role="Role to ping on post", announce_channel="Channel for Reddit announcements")
    async def set_reddit(self, interaction: discord.Interaction, subreddit: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["reddit_enabled"] = True
        settings["reddit_subreddit"] = subreddit.lstrip("r/")
        settings["reddit_ping_role"] = str(ping_role.id) if ping_role else None
        settings["reddit_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "Reddit Alerts Set Up"))
            .color("orange")
            .row(
                ('Subreddit', f"r/{subreddit.lstrip('r/')}"),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="github", description="Set GitHub repo for release alerts")
    @app_commands.describe(repo="GitHub repo (e.g. 'novexd/prowl')", ping_role="Role to ping on release", announce_channel="Channel for GitHub announcements")
    async def set_github(self, interaction: discord.Interaction, repo: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["github_enabled"] = True
        settings["github_repo"] = repo.lstrip("/")
        settings["github_ping_role"] = str(ping_role.id) if ping_role else None
        settings["github_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "GitHub Alerts Set Up"))
            .color("gray")
            .row(
                ('Repo', f"`{repo.lstrip('/')}`"),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="tiktok", description="Set TikTok user for video alerts")
    @app_commands.describe(username="TikTok username (without @)", ping_role="Role to ping on video", announce_channel="Channel for TikTok announcements")
    async def set_tiktok(self, interaction: discord.Interaction, username: str, ping_role: Optional[discord.Role] = None, announce_channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        settings["tiktok_enabled"] = True
        settings["tiktok_username"] = username.lstrip("@")
        settings["tiktok_ping_role"] = str(ping_role.id) if ping_role else None
        settings["tiktok_announce_channel_id"] = str(announce_channel.id) if announce_channel else str(interaction.channel_id)
        await save_social_settings(interaction.guild_id, settings)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bell", "TikTok Alerts Set Up"))
            .color("red")
            .row(
                ('Username', f"@{username.lstrip('@')}"),
                ('Ping Role', ping_role.mention if ping_role else 'None'),
                ('Announce Channel', announce_channel.mention if announce_channel else interaction.channel.mention)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="config", description="View social alert settings")
    async def config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        yt_role = interaction.guild.get_role(int(settings["youtube_ping_role"])) if settings.get("youtube_ping_role") else None
        tw_role = interaction.guild.get_role(int(settings["twitch_ping_role"])) if settings.get("twitch_ping_role") else None
        x_role = interaction.guild.get_role(int(settings["twitter_ping_role"])) if settings.get("twitter_ping_role") else None
        rd_role = interaction.guild.get_role(int(settings["reddit_ping_role"])) if settings.get("reddit_ping_role") else None
        gh_role = interaction.guild.get_role(int(settings["github_ping_role"])) if settings.get("github_ping_role") else None
        tt_role = interaction.guild.get_role(int(settings["tiktok_ping_role"])) if settings.get("tiktok_ping_role") else None
        yt_channel = self._resolve_channel(interaction.guild, settings.get("youtube_announce_channel_id"))
        tw_channel = self._resolve_channel(interaction.guild, settings.get("twitch_announce_channel_id"))
        x_channel = self._resolve_channel(interaction.guild, settings.get("twitter_announce_channel_id"))
        rd_channel = self._resolve_channel(interaction.guild, settings.get("reddit_announce_channel_id"))
        gh_channel = self._resolve_channel(interaction.guild, settings.get("github_announce_channel_id"))
        tt_channel = self._resolve_channel(interaction.guild, settings.get("tiktok_announce_channel_id"))
        embed = (
            EmbedBuilder()
            .title(emoji_title("settings", "Social Alert Settings"))
            .color("brand")
            .row(
                ('YouTube', f"Channel: `{settings.get('youtube_channel_id') or 'Not set'}`\nPing: {(yt_role.mention if yt_role else 'None')}\nAnnounces: {(yt_channel.mention if yt_channel else 'Not set')}"),
                ('Twitch', f"Channel: `{settings.get('twitch_channel') or 'Not set'}`\nPing: {(tw_role.mention if tw_role else 'None')}\nAnnounces: {(tw_channel.mention if tw_channel else 'Not set')}"),
                ('Twitter/X', f"Handle: `@{settings.get('twitter_handle') or 'Not set'}`\nPing: {(x_role.mention if x_role else 'None')}\nAnnounces: {(x_channel.mention if x_channel else 'Not set')}")
            )
            .row(
                ('Reddit', f"Sub: `r/{settings.get('reddit_subreddit') or 'Not set'}`\nPing: {(rd_role.mention if rd_role else 'None')}\nAnnounces: {(rd_channel.mention if rd_channel else 'Not set')}"),
                ('GitHub', f"Repo: `{settings.get('github_repo') or 'Not set'}`\nPing: {(gh_role.mention if gh_role else 'None')}\nAnnounces: {(gh_channel.mention if gh_channel else 'Not set')}"),
                ('TikTok', f"User: `@{settings.get('tiktok_username') or 'Not set'}`\nPing: {(tt_role.mention if tt_role else 'None')}\nAnnounces: {(tt_channel.mention if tt_channel else 'Not set')}")
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @social_group.command(name="remove", description="Remove social alert settings")
    @app_commands.describe(platform="Which platform to remove")
    @app_commands.choices(platform=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="Twitter/X", value="twitter"),
        app_commands.Choice(name="Reddit", value="reddit"),
        app_commands.Choice(name="GitHub", value="github"),
        app_commands.Choice(name="TikTok", value="tiktok"),
        app_commands.Choice(name="All", value="all")
    ])
    async def remove(self, interaction: discord.Interaction, platform: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_social_settings(interaction.guild_id)
        if platform in ("youtube", "all"):
            settings["youtube_enabled"] = False
            settings["youtube_channel_id"] = None
            settings["youtube_ping_role"] = None
            settings["youtube_announce_channel_id"] = None
        if platform in ("twitch", "all"):
            settings["twitch_enabled"] = False
            settings["twitch_channel"] = None
            settings["twitch_ping_role"] = None
            settings["twitch_announce_channel_id"] = None
        if platform in ("twitter", "all"):
            settings["twitter_enabled"] = False
            settings["twitter_handle"] = None
            settings["twitter_ping_role"] = None
            settings["twitter_announce_channel_id"] = None
        if platform in ("reddit", "all"):
            settings["reddit_enabled"] = False
            settings["reddit_subreddit"] = None
            settings["reddit_ping_role"] = None
            settings["reddit_announce_channel_id"] = None
        if platform in ("github", "all"):
            settings["github_enabled"] = False
            settings["github_repo"] = None
            settings["github_ping_role"] = None
            settings["github_announce_channel_id"] = None
        if platform in ("tiktok", "all"):
            settings["tiktok_enabled"] = False
            settings["tiktok_username"] = None
            settings["tiktok_ping_role"] = None
            settings["tiktok_announce_channel_id"] = None
        await save_social_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("bell_off", "Social Alerts Removed")).description(f"Removed alerts for **{platform}**.").color("gray").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SocialAlerts(bot))
