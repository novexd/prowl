import discord
from discord.ext import commands
import asyncio
import datetime

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db
from components.moderation import get_mod_settings, save_mod_settings, perform_lockdown


RAID_DEFAULTS = {
    "enabled": False,
    "mode": "switches",
    "join_threshold": 5,
    "join_window": 10,
    "join_action": "kick",
    "account_age_min": 0,
    "account_age_action": "kick",
    "default_avatar_enabled": False,
    "default_avatar_action": "kick",
    "score_threshold": 3,
    "score_action": "kick",
    "score_window": 10,
    "score_default_avatar": 2,
    "score_new_account_min": 10,
    "score_new_account": 2,
    "score_join_burst": 1,
    "auto_recovery": True,
    "recovery_minutes": 30,
    "moderation_channel_id": None,
}

RAID_ACTIONS = ("kick", "ban", "lockdown", "verify")


async def get_raid_settings(guild_id: int):
    return await neon_db.load_cached_settings("raid_settings", guild_id, RAID_DEFAULTS)


class RaidProtection(commands.Cog, name="RaidProtection"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent_joins = {}   # guild_id -> [(ts, member_id)]
        self.raid_active = {}    # guild_id -> bool

    async def _mod_channel(self, guild, settings):
        cid = settings.get("moderation_channel_id")
        if not cid:
            return None
        return guild.get_channel(int(cid))

    async def _log(self, guild, settings, title, description, color="orange"):
        ch = await self._mod_channel(guild, settings)
        if not ch:
            return
        try:
            await ch.send(embed=EmbedBuilder().title(title).description(description).color(color).timestamp(datetime.datetime.utcnow()).build())
        except Exception as e:
            logger.warning(f"Raid log failed in {guild.id}: {e}")

    async def _apply_action(self, guild, settings, member, reason, action):
        if action == "kick":
            try:
                await member.kick(reason=reason)
                return True
            except Exception as e:
                logger.warning(f"Raid kick failed: {e}")
        elif action == "ban":
            try:
                await member.ban(reason=reason)
                return True
            except Exception as e:
                logger.warning(f"Raid ban failed: {e}")
        elif action == "lockdown":
            mods = await get_mod_settings(guild.id)
            ok, detail = await perform_lockdown(guild, True, mods, save_mod_settings)
            return ok
        elif action == "verify":
            try:
                from components.verification import get_verify_settings, save_verify_settings
                vs = await get_verify_settings(guild.id)
                if vs.get("channel_id") and vs.get("verified_role_id"):
                    vs["enabled"] = True
                    await save_verify_settings(guild.id, vs)
                    cog = self.bot.get_cog("Verification")
                    if cog and await cog._send_panel(guild, vs):
                        return True
            except Exception as e:
                logger.warning(f"Raid verify failed: {e}")
            mods = await get_mod_settings(guild.id)
            ok, detail = await perform_lockdown(guild, True, mods, save_mod_settings)
            return ok
        return False

    async def _score_check(self, guild, settings, member, now):
        """Score mode: sum points from each fulfilled criterion; act when the
        threshold is reached."""
        gid = guild.id
        score = 0
        parts = []

        # Default avatar (raiders often use one)
        pts = int(settings.get("score_default_avatar", 0) or 0)
        if pts > 0 and member.avatar is None:
            score += pts
            parts.append(f"default avatar +{pts}")

        # Newly-created account
        min_age = int(settings.get("score_new_account_min", 0) or 0)
        if min_age > 0:
            pts = int(settings.get("score_new_account", 0) or 0)
            age_min = (now - member.created_at.replace(tzinfo=None).timestamp()) / 60
            if age_min < min_age and pts > 0:
                score += pts
                parts.append(f"account {int(age_min)}min old +{pts}")

        # Join burst (part of a rapid wave of joins)
        pts = int(settings.get("score_join_burst", 0) or 0)
        window = int(settings.get("score_window", 10) or 10)
        threshold = int(settings.get("join_threshold", 5) or 5)
        self.recent_joins.setdefault(gid, []).append((now, member.id))
        self.recent_joins[gid] = [(t, mid) for t, mid in self.recent_joins[gid] if now - t < window]
        if pts > 0 and len(self.recent_joins[gid]) >= threshold:
            score += pts
            parts.append(f"join burst +{pts}")

        min_score = int(settings.get("score_threshold", 3) or 3)
        if score >= min_score:
            action = settings.get("score_action", "kick")
            reason = f"Raid score {score}/{min_score} ({', '.join(parts) or 'no criteria matched'})"
            await self._apply_action(guild, settings, member, reason, action)
            await self._log(guild, settings, emoji_title("raid_detected", "Raid Protection"), f"{member.mention} (`{member}`) blocked - {reason}", "error")
            if settings.get("auto_recovery") and action == "lockdown":
                minutes = int(settings.get("recovery_minutes", 30) or 30)
                asyncio.create_task(self._recover(guild.id, minutes))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot or not member.guild:
            return
        guild = member.guild
        settings = await get_raid_settings(guild.id)
        if not settings.get("enabled"):
            return
        now = datetime.datetime.utcnow().timestamp()
        gid = guild.id

        if settings.get("mode") == "score":
            await self._score_check(guild, settings, member, now)
            return

        # ── Switches mode ──
        # Default avatar recognition
        if settings.get("default_avatar_enabled") and member.avatar is None:
            reason = "Raid protection: using a default avatar"
            await self._apply_action(guild, settings, member, reason, settings.get("default_avatar_action", "kick"))
            await self._log(guild, settings, emoji_title("anti_raid", "Raid Protection"), f"{member.mention} (`{member}`) blocked - {reason}", "info")
            return

        # Account age filter
        min_age_min = int(settings.get("account_age_min", 0) or 0)
        if min_age_min > 0:
            age_min = (now - member.created_at.replace(tzinfo=None).timestamp()) / 60
            if age_min < min_age_min:
                reason = f"Raid protection: account {int(age_min)}min old (min {min_age_min})"
                await self._apply_action(guild, settings, member, reason, settings.get("account_age_action", "kick"))
                await self._log(guild, settings, emoji_title("anti_raid", "Raid Protection"), f"{member.mention} (`{member}`) blocked - {reason}", "info")
                return

        # Join burst detection
        threshold = int(settings.get("join_threshold", 5) or 5)
        window = int(settings.get("join_window", 10) or 10)
        action = settings.get("join_action", "kick")
        self.recent_joins.setdefault(gid, []).append((now, member.id))
        self.recent_joins[gid] = [(t, mid) for t, mid in self.recent_joins[gid] if now - t < window]
        if len(self.recent_joins[gid]) >= threshold and not self.raid_active.get(gid):
            self.raid_active[gid] = True
            reason = f"Raid detected: {len(self.recent_joins[gid])} joins in {window}s"
            # Kick/ban every member in the window; lockdown/verify act once
            if action in ("kick", "ban"):
                for t, mid in self.recent_joins[gid]:
                    m = guild.get_member(mid)
                    if m:
                        await self._apply_action(guild, settings, m, reason, action)
            else:
                await self._apply_action(guild, settings, member, reason, action)
            await self._log(guild, settings, emoji_title("raid_detected", "Raid Detected"), f"{reason}\nAction: **{action}**", "error")
            # Auto-recovery for lockdown
            if settings.get("auto_recovery") and action == "lockdown":
                minutes = int(settings.get("recovery_minutes", 30) or 30)
                asyncio.create_task(self._recover(guild.id, minutes))

    async def _recover(self, guild_id, minutes):
        await asyncio.sleep(minutes * 60)
        self.raid_active.pop(guild_id, None)
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return
        settings = await get_raid_settings(guild.id)
        if not settings.get("auto_recovery"):
            return
        mods = await get_mod_settings(guild.id)
        try:
            await perform_lockdown(guild, False, mods, save_mod_settings)
        except Exception as e:
            logger.warning(f"Raid recovery failed: {e}")
        await self._log(guild, settings, emoji_title("success", "Raid Recovery"), "Server unlocked after the raid.", "success")


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidProtection(bot))
