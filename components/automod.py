import discord
from discord.ext import commands
import re
import time
import datetime
import unicodedata

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import embed_from_dict, emoji_title, EMBED_EMOJIS
from components.moderation import log_mod_action, render_embed_data, render_template


AUTOMOD_DEFAULTS = {
    "enabled": False,
    "moderation_channel_id": None,
    "filter_mute_minutes": 60,
    "profanity_enabled": True,
    "profanity_action": "delete",
    "profanity_words": "",
    "spam_enabled": True,
    "spam_action": "delete",
    "spam_messages": 5,
    "spam_window": 5,
    "links_enabled": False,
    "links_action": "delete",
    "links_allowlist": "",
    "caps_enabled": False,
    "caps_action": "delete",
    "caps_percent": 70,
    "caps_min_chars": 6,
    "mentions_enabled": False,
    "mentions_action": "delete",
    "mentions_max": 5,
    "invites_enabled": False,
    "invites_action": "delete",
    "zalgo_enabled": False,
    "zalgo_action": "delete",
    "emoji_enabled": False,
    "emoji_action": "delete",
    "emoji_max": 10,
    "action_configs": {},
}

ACTIONS = ("delete", "delete_dm", "warn", "warn_dm", "mute", "mute_dm", "kick", "kick_dm", "ban", "ban_dm")

# Display name -> config key (matches website AUTOMOD_FILTERS)
FILTER_KEY = {
    "Profanity": "profanity",
    "Spam": "spam",
    "Links": "links",
    "Caps Lock": "caps",
    "Mention Spam": "mentions",
    "Invite Links": "invites",
    "Zalgo": "zalgo",
    "Emoji Spam": "emoji",
}

# Default profanity words (community standard). Server can override via profanity_words.
DEFAULT_PROFANITY = [
    "fuck", "shit", "bitch", "asshole", "bastard", "cunt", "dick", "pussy",
    "nigga", "nigger", "faggot", "retard", "whore", "slut", "porn",
]

URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
INVITE_RE = re.compile(r"(discord\.(gg|com/invite)/)[a-zA-Z0-9_-]+", re.IGNORECASE)
ZALGO_RE = re.compile(r"[\u0300-\u036f\u0489\u0616-\u061a\u06d6-\u06ed\u200d\u2060\u20d0-\u20ff\ufe00-\ufe0f]")
# Domains + extensions that should never be blocked by the link filter (media/gifs)
MEDIA_HOSTS = ("cdn.discordapp.com", "tenor.com", "cdn.tenor.com", "giphy.com", "media.giphy.com", "imgur.com", "i.imgur.com")
MEDIA_EXT = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".webm", ".mp4", ".mov")
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    r"\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF\U0001F1E6-\U0001F1FF"
    r"\u2b00-\u2bff\u2934-\u2935\u25aa-\u25fe]"
)


async def get_automod_settings(guild_id: int):
    return await neon_db.load_cached_settings("automod_settings", guild_id, AUTOMOD_DEFAULTS)


async def save_automod_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("automod_settings", guild_id, settings)


def _word_list(words_str: str, defaults):
    custom = [w.strip().lower() for w in (words_str or "").split(",") if w.strip()]
    if custom:
        return custom
    return defaults


class AutoMod(commands.Cog, name="AutoMod"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.spam_log = {}  # (guild_id, user_id) -> [timestamps]
        self.trigger_cooldown = {}  # (guild_id, user_id) -> last trigger ts

    async def _mod_channel(self, guild, settings):
        cid = settings.get("moderation_channel_id")
        if not cid:
            return None
        return guild.get_channel(int(cid))

    async def _post_action(self, guild, settings, message, filter_name, reason, action, custom_embed=None):
        channel = await self._mod_channel(guild, settings)
        if not channel:
            return
        if custom_embed is not None:
            try:
                await channel.send(embed=custom_embed)
            except Exception as e:
                logger.warning(f"AutoMod post failed in {guild.id}: {e}")
            return
        embed = (
            EmbedBuilder()
            .title(emoji_title("shield", f"AutoMod: {filter_name}"))
            .color("gray")
            .row(
                ('User', f'{message.author.mention} (`{message.author.id}`)'),
                ('Channel', message.channel.mention),
                ('Reason', reason),
                ('Action', action)
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"AutoMod post failed in {guild.id}: {e}")

    def _custom_embed(self, cfg, base, message, reason, filter_name=""):
        """Return a discord.Embed for a custom-message action, or None."""
        if not isinstance(cfg, dict):
            return None
        if cfg.get(base + "_mode") != "custom":
            return None
        data = cfg.get(base + "_embed") or {}
        if not isinstance(data, dict) or not (data.get("title") or data.get("description")):
            return None
        rendered = render_embed_data(data, message.author, reason, 0, "")
        if filter_name:
            for k, v in list(rendered.items()):
                if isinstance(v, str):
                    rendered[k] = v.replace("{filter}", filter_name)
        return embed_from_dict(rendered)

    def _dm_text(self, cfg, base, message, filter_name, reason):
        """Resolve the DM text for an action: custom message with variables, or default."""
        tmpl = cfg.get(base + "_message") if isinstance(cfg, dict) else None
        if tmpl:
            try:
                return render_template(str(tmpl), message.author, reason).replace("{filter}", filter_name)
            except Exception:
                return str(tmpl)
        return f"**AutoMod - {filter_name}** in {message.guild.name}:\n{reason}"

    def _default_dm_embed(self, filter_name, reason, guild):
        """Create a default styled embed for automod DMs."""
        return (
            EmbedBuilder()
            .title(f"{EMBED_EMOJIS.get('shield', '')} AutoMod - {filter_name}")
            .description(reason)
            .field("Server", guild.name, inline=True)
            .color("warn")
            .footer("AutoMod | Prowl")
            .build()
        )

    async def _apply_action(self, guild, settings, message, filter_name, reason, action):
        author = message.author
        member = guild.get_member(author.id)
        configs = settings.get("action_configs", {}) or {}
        cfg = configs.get(FILTER_KEY.get(filter_name, filter_name), {}) if isinstance(configs, dict) else {}
        send_dm = action.endswith("_dm")
        base = action[:-3] if send_dm else action
        # Always remove the offending message when we can
        try:
            await message.delete()
        except Exception:
            pass
        custom_embed = self._custom_embed(cfg, base, message, reason, filter_name)
        if send_dm:
            try:
                if custom_embed is not None:
                    await author.send(embed=custom_embed)
                else:
                    await author.send(embed=self._default_dm_embed(filter_name, reason, guild))
            except Exception:
                pass
        if base == "warn":
            custom = cfg.get("warn_message")
            if custom:
                reason = render_template(str(custom), author, reason).replace("{filter}", filter_name)
            await log_mod_action(guild.id, str(author.id), author.name, "warn", reason, "AutoMod")
        elif base == "mute":
            if member:
                minutes = int(cfg.get("mute_minutes") or settings.get("filter_mute_minutes", 60) or 60)
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                try:
                    await member.timeout(until, reason=reason)
                    await neon_db.set_muted_user(guild.id, str(author.id), author.name, reason, until.timestamp())
                except Exception as e:
                    logger.warning(f"AutoMod mute failed: {e}")
            await log_mod_action(guild.id, str(author.id), author.name, "mute", reason, "AutoMod")
        elif base == "kick":
            if member:
                try:
                    await member.kick(reason=cfg.get("kick_message") or reason)
                except Exception as e:
                    logger.warning(f"AutoMod kick failed: {e}")
            await log_mod_action(guild.id, str(author.id), author.name, "kick", cfg.get("kick_message") or reason, "AutoMod")
        elif base == "ban":
            try:
                days = int(cfg.get("ban_days") or 0)
                await guild.ban(discord.Object(id=author.id), reason=cfg.get("ban_message") or reason, delete_message_seconds=max(0, min(7, days)) * 86400)
            except Exception as e:
                logger.warning(f"AutoMod ban failed: {e}")
            await log_mod_action(guild.id, str(author.id), author.name, "ban", cfg.get("ban_message") or reason, "AutoMod")
        await self._post_action(guild, settings, message, filter_name, reason, action, custom_embed)

    def _triggered(self, guild_id, user_id, cooldown=10):
        key = (guild_id, user_id)
        now = time.time()
        if self.trigger_cooldown.get(key, 0) + cooldown > now:
            return True
        self.trigger_cooldown[key] = now
        return False

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.content:
            return
        settings = await get_automod_settings(message.guild.id)
        if not settings.get("enabled"):
            return
        if await self._is_mod(message.author, message.guild):
            return

        content = message.content
        hits = []

        # Profanity
        if settings.get("profanity_enabled", True):
            words = _word_list(settings.get("profanity_words"), DEFAULT_PROFANITY)
            low = content.lower()
            for w in words:
                if re.search(rf"\b{re.escape(w)}\b", low):
                    hits.append(("Profanity", f"Banned word: `{w}`"))
                    break

        # Spam
        if settings.get("spam_enabled", True) and not hits:
            skey = (message.guild.id, message.author.id)
            now = time.time()
            window = int(settings.get("spam_window", 5) or 5)
            threshold = int(settings.get("spam_messages", 5) or 5)
            self.spam_log.setdefault(skey, []).append(now)
            self.spam_log[skey] = [t for t in self.spam_log[skey] if now - t <= window]
            if len(self.spam_log[skey]) > threshold:
                hits.append(("Spam", f"{len(self.spam_log[skey])} messages in {window}s"))

        # Links
        if settings.get("links_enabled") and not hits:
            allowlist = [d.strip().lower() for d in (settings.get("links_allowlist") or "").split(",") if d.strip()]
            for m in URL_RE.finditer(content):
                url = m.group(0)
                lower = url.lower()
                if any(lower.startswith(("http://" + d, "https://" + d, "www." + d)) for d in allowlist):
                    continue
                # Never block media/GIF links
                if any(host in lower for host in MEDIA_HOSTS):
                    continue
                if lower.rstrip("/.,)").endswith(MEDIA_EXT):
                    continue
                hits.append(("Links", "Message contains a link"))
                break

        # Caps
        if settings.get("caps_enabled") and not hits:
            min_chars = int(settings.get("caps_min_chars", 6) or 6)
            pct = int(settings.get("caps_percent", 70) or 70)
            letters = [c for c in content if c.isalpha()]
            if len(letters) >= min_chars:
                upper = sum(1 for c in letters if c.isupper())
                if upper / len(letters) * 100 >= pct:
                    hits.append(("Caps Lock", f"{upper}/{len(letters)} uppercase letters"))

        # Mentions
        if settings.get("mentions_enabled") and not hits:
            max_mentions = int(settings.get("mentions_max", 5) or 5)
            count = len(message.mentions) + content.count("@everyone") + content.count("@here")
            if count > max_mentions:
                hits.append(("Mention Spam", f"{count} mentions (max {max_mentions})"))

        # Invites
        if settings.get("invites_enabled") and not hits:
            if INVITE_RE.search(content):
                hits.append(("Invite Links", "Message contains a Discord invite"))

        # Zalgo
        if settings.get("zalgo_enabled") and not hits:
            if ZALGO_RE.search(content):
                hits.append(("Zalgo", "Message contains glitch/combining characters"))

        # Emoji spam
        if settings.get("emoji_enabled") and not hits:
            max_emoji = int(settings.get("emoji_max", 10) or 10)
            count = len(EMOJI_RE.findall(content))
            if count > max_emoji:
                hits.append(("Emoji Spam", f"{count} emojis (max {max_emoji})"))

        if not hits:
            return
        filter_name, reason = hits[0]
        action = self._action_for(filter_name, settings)
        if self._triggered(message.guild.id, message.author.id):
            return
        await self._apply_action(message.guild, settings, message, filter_name, reason, action)

    def _action_for(self, filter_name, settings):
        keymap = {
            "Profanity": "profanity_action",
            "Spam": "spam_action",
            "Links": "links_action",
            "Caps Lock": "caps_action",
            "Mention Spam": "mentions_action",
            "Invite Links": "invites_action",
            "Zalgo": "zalgo_action",
            "Emoji Spam": "emoji_action",
        }
        action = settings.get(keymap.get(filter_name))
        return action if action in ACTIONS else "delete"

    async def _is_mod(self, member, guild):
        if member.guild_permissions.administrator:
            return True
        if member.guild_permissions.manage_messages:
            return True
        if member.guild_permissions.moderate_members:
            return True
        # Bot role check via mod_settings mod_roles
        try:
            from components.moderation import get_mod_settings
            ms = await get_mod_settings(guild.id)
            mod_roles = ms.get("mod_roles", [])
            if any(str(r.id) in mod_roles for r in member.roles):
                return True
        except Exception:
            pass
        return False


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
