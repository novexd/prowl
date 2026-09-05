import discord
from discord.ext import commands
import datetime
import time
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import EMBED_EMOJIS, emoji_title


BADGE_EMOJI = {
    "msg_100": "message", "msg_500": "message", "msg_1k": "message", "msg_5k": "fire",
    "msg_10k": "star", "msg_25k": "gem", "msg_50k": "crown", "msg_100k": "reward",
    "vc_1h": "music", "vc_10h": "music", "vc_50h": "music", "vc_100h": "music",
    "vc_500h": "music", "vc_1000h": "music",
    "tenure_1m": "calendar", "tenure_6m": "calendar", "tenure_1y": "reward",
    "tenure_2y": "reward", "first_member": "star", "booster": "rocket",
}

BADGE_DEFS = [
    # Messages
    {"id": "msg_100",    "name": "Chatterbox",     "desc": "Sent 100 messages",        "category": "messages", "threshold": 100},
    {"id": "msg_500",    "name": "Conversationalist","desc": "Sent 500 messages",       "category": "messages", "threshold": 500},
    {"id": "msg_1k",     "name": "Big Talker",     "desc": "Sent 1,000 messages",      "category": "messages", "threshold": 1000},
    {"id": "msg_5k",     "name": "Non-Stop",       "desc": "Sent 5,000 messages",      "category": "messages", "threshold": 5000},
    {"id": "msg_10k",    "name": "Legend",          "desc": "Sent 10,000 messages",     "category": "messages", "threshold": 10000},
    {"id": "msg_25k",    "name": "Living Chat",    "desc": "Sent 25,000 messages",     "category": "messages", "threshold": 25000},
    {"id": "msg_50k",    "name": "Server Soul",    "desc": "Sent 50,000 messages",     "category": "messages", "threshold": 50000},
    {"id": "msg_100k",   "name": "Myth",           "desc": "Sent 100,000 messages",    "category": "messages", "threshold": 100000},
    # Voice
    {"id": "vc_1h",      "name": "Lurker",         "desc": "1 hour in voice",          "category": "voice", "threshold": 60},
    {"id": "vc_10h",     "name": "Regular",        "desc": "10 hours in voice",        "category": "voice", "threshold": 600},
    {"id": "vc_50h",     "name": "Voice Veteran",  "desc": "50 hours in voice",        "category": "voice", "threshold": 3000},
    {"id": "vc_100h",    "name": "Echo",           "desc": "100 hours in voice",       "category": "voice", "threshold": 6000},
    {"id": "vc_500h",    "name": "Voice Lord",     "desc": "500 hours in voice",       "category": "voice", "threshold": 30000},
    {"id": "vc_1000h",   "name": "Voice Legend",   "desc": "1,000 hours in voice",     "category": "voice", "threshold": 60000},
    # Tenure
    {"id": "tenure_1m",  "name": "Newcomer",       "desc": "Member for 1 month",       "category": "tenure", "threshold": 30},
    {"id": "tenure_6m",  "name": "Regular",        "desc": "Member for 6 months",      "category": "tenure", "threshold": 180},
    {"id": "tenure_1y",  "name": "Veteran",        "desc": "Member for 1 year",        "category": "tenure", "threshold": 365},
    {"id": "tenure_2y",  "name": "Old Guard",      "desc": "Member for 2 years",       "category": "tenure", "threshold": 730},
    # Special
    {"id": "first_member","name": "First Member",  "desc": "First member of the server","category": "special", "threshold": 0},
    {"id": "booster",    "name": "Server Booster", "desc": "Currently boosting the server","category": "special", "threshold": 0},
]

BADGE_MAP = {b["id"]: b for b in BADGE_DEFS}


class Badges(commands.Cog, name="Badges"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._vc_sessions = {}  # (guild_id, user_id) -> join_timestamp

    # ── VC Tracking ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or member.guild is None:
            return

        guild_id = member.guild.id
        user_id = member.id
        key = (guild_id, user_id)

        now = time.time()

        # Joined a channel
        if before.channel is None and after.channel is not None:
            self._vc_sessions[key] = now

        # Left a channel
        elif before.channel is not None and after.channel is None:
            join_time = self._vc_sessions.pop(key, None)
            if join_time:
                minutes = int((now - join_time) / 60)
                if minutes > 0:
                    await self._add_vc_minutes(guild_id, user_id, minutes)

        # Moved channels (treat as leave + join)
        elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            join_time = self._vc_sessions.pop(key, None)
            if join_time:
                minutes = int((now - join_time) / 60)
                if minutes > 0:
                    await self._add_vc_minutes(guild_id, user_id, minutes)
            self._vc_sessions[key] = now

    async def _add_vc_minutes(self, guild_id: int, user_id: int, minutes: int):
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return
            now = datetime.datetime.utcnow().timestamp()
            await pool.execute(
                "INSERT INTO user_activity (guild_id, user_id, vc_minutes, last_voice_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET "
                "vc_minutes = user_activity.vc_minutes + ?, last_voice_at = ?",
                str(guild_id), str(user_id), minutes, now, minutes, now,
            )
            await self._check_badges(guild_id, user_id)
        except Exception as e:
            logger.warning(f"Failed to add VC minutes for {user_id} in {guild_id}: {e}")

    # ── Badge Checking ──────────────────────────────────────────────

    async def _check_badges(self, guild_id: int, user_id: int):
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return

            # Get user activity data
            row = await pool.fetchrow(
                "SELECT vc_minutes FROM user_activity WHERE guild_id = ? AND user_id = ?",
                str(guild_id), str(user_id),
            )
            vc_minutes = int(row["vc_minutes"]) if row and row.get("vc_minutes") else 0

            # Get message count from leveling_data
            lvl_row = await pool.fetchrow(
                "SELECT messages FROM leveling_data WHERE guild_id = ? AND user_id = ?",
                str(guild_id), str(user_id),
            )
            messages = int(lvl_row["messages"]) if lvl_row and lvl_row.get("messages") else 0

            # Get existing badges
            existing = await pool.fetch(
                "SELECT badge_id FROM user_badges WHERE guild_id = ? AND user_id = ?",
                str(guild_id), str(user_id),
            )
            existing_ids = {r["badge_id"] for r in existing}

            # Get guild member for tenure/special checks
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None

            now = datetime.datetime.utcnow().timestamp()
            new_badges = []

            for badge in BADGE_DEFS:
                if badge["id"] in existing_ids:
                    continue

                earned = False

                if badge["category"] == "messages" and messages >= badge["threshold"]:
                    earned = True
                elif badge["category"] == "voice" and vc_minutes >= badge["threshold"]:
                    earned = True
                elif badge["category"] == "tenure" and member and member.joined_at:
                    days = (datetime.datetime.utcnow() - member.joined_at).days
                    if days >= badge["threshold"]:
                        earned = True
                elif badge["category"] == "special":
                    if badge["id"] == "first_member" and member:
                        if member.guild.member_count <= 2 or (member.joined_at and member.joined_at == member.guild.created_at):
                            earned = True
                    elif badge["id"] == "booster" and member:
                        if member.premium_since:
                            earned = True

                if earned:
                    await pool.execute(
                        "INSERT INTO user_badges (guild_id, user_id, badge_id, awarded_at) VALUES (?, ?, ?, ?)",
                        str(guild_id), str(user_id), badge["id"], now,
                    )
                    new_badges.append(badge)

            return new_badges

        except Exception as e:
            logger.warning(f"Badge check failed for {user_id} in {guild_id}: {e}")
            return []

    # ── Message tracking hook (called from on_message in leveling) ──

    async def track_message(self, guild_id: int, user_id: int):
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return
            now = datetime.datetime.utcnow().timestamp()
            await pool.execute(
                "INSERT INTO user_activity (guild_id, user_id, last_message_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET last_message_at = ?",
                str(guild_id), str(user_id), now, now,
            )
        except Exception:
            pass

    # ── Commands ──────────────────────────────────────────────────────

    @commands.hybrid_command(name="badges", description="View your badges or another member's badges")
    async def badges_cmd(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        if not ctx.guild:
            return await ctx.send("Server only.")

        target = member or ctx.author
        pool = await neon_db.get_pool()
        if not pool:
            return await ctx.send("Database unavailable.")

        # Ensure activity data exists
        await pool.execute(
            "INSERT OR IGNORE INTO user_activity (guild_id, user_id, vc_minutes) VALUES (?, ?, 0)",
            str(ctx.guild.id), str(target.id),
        )

        # Run badge check
        await self._check_badges(ctx.guild.id, target.id)

        rows = await pool.fetch(
            "SELECT badge_id, awarded_at FROM user_badges WHERE guild_id = ? AND user_id = ? ORDER BY awarded_at ASC",
            str(ctx.guild.id), str(target.id),
        )

        # Get stats
        act_row = await pool.fetchrow(
            "SELECT vc_minutes FROM user_activity WHERE guild_id = ? AND user_id = ?",
            str(ctx.guild.id), str(target.id),
        )
        vc_minutes = int(act_row["vc_minutes"]) if act_row and act_row.get("vc_minutes") else 0

        lvl_row = await pool.fetchrow(
            "SELECT messages FROM leveling_data WHERE guild_id = ? AND user_id = ?",
            str(ctx.guild.id), str(target.id),
        )
        messages = int(lvl_row["messages"]) if lvl_row and lvl_row.get("messages") else 0

        # Build badge display
        if rows:
            lines = []
            for r in rows:
                badge = BADGE_MAP.get(r["badge_id"])
                if badge:
                    emoji_key = BADGE_EMOJI.get(badge["id"], "star")
                    emoji_str = EMBED_EMOJIS.get(emoji_key, "")
                    lines.append(f"{emoji_str} **{badge['name']}** — {badge['desc']}")
            badge_text = "\n".join(lines)
        else:
            badge_text = "No badges yet. Start chatting and hanging out in voice!"

        # Format stats
        vc_hours = vc_minutes // 60
        vc_days = vc_hours // 24
        if vc_days > 0:
            vc_str = f"{vc_days}d {vc_hours % 24}h"
        else:
            vc_str = f"{vc_hours}h {vc_minutes % 60}m"

        tenure_str = ""
        if target.joined_at:
            days = (datetime.datetime.utcnow() - target.joined_at).days
            if days >= 365:
                tenure_str = f"{days // 365}y {(days % 365) // 30}mo"
            elif days >= 30:
                tenure_str = f"{days // 30}mo {days % 30}d"
            else:
                tenure_str = f"{days}d"

        embed = (
            EmbedBuilder()
            .title(emoji_title("reward", f"{target.display_name}'s Badges"))
            .description(badge_text)
            .color("brand")
            .row(
                ("Messages", f"{messages:,}"),
                ("Voice", vc_str),
                ("Tenure", tenure_str),
            )
            .footer(f"{len(rows)}/{len(BADGE_DEFS)} badges earned")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="badgesboard", description="Show top badge earners in this server")
    async def badges_board(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("Server only.")

        pool = await neon_db.get_pool()
        if not pool:
            return await ctx.send("Database unavailable.")

        rows = await pool.fetch(
            "SELECT user_id, COUNT(*) as count FROM user_badges WHERE guild_id = ? GROUP BY user_id ORDER BY count DESC LIMIT 10",
            str(ctx.guild.id),
        )

        if not rows:
            return await ctx.send(
                embed=EmbedBuilder().title(emoji_title("info", "No Badges")).description("No one has earned badges yet.").color("blue").timestamp(datetime.datetime.utcnow()).build()
            )

        lines = []
        medals = [EMBED_EMOJIS.get("reward", ""), EMBED_EMOJIS.get("star", ""), EMBED_EMOJIS.get("sparkle", "")]
        for i, r in enumerate(rows):
            uid = int(r["user_id"])
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User ({uid})"
            count = int(r["count"])
            prefix = medals[i] if i < 3 else f"**{i+1}.**"
            lines.append(f"{prefix} **{name}** — {count} badge{'s' if count != 1 else ''}")

        embed = (
            EmbedBuilder()
            .title(emoji_title("reward", "Badge Leaderboard"))
            .description("\n".join(lines))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Badges(bot))
