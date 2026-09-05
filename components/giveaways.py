"""
Server giveaways with button-entry, automatic winner selection, rerolls and
early-end. Giveaways are stored in the shared DB; a background loop posts
pending giveaways, ends due ones, and processes reroll requests. The dashboard
and slash commands both write to the same table.

Slash commands (manage-messages required):
  /giveaway start <prize> <duration> [winners] [channel] [description] [role]
  /giveaway end <message_id>
  /giveaway reroll <message_id>
  /giveaway list
"""

import re
import time
import json
import random
import asyncio
import datetime

import discord
from discord.ext import commands
from discord import app_commands

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title, embed_from_dict


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_END_RE = re.compile(
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days)",
    re.I,
)


def _parse_clock(text: str):
    text = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    h = int(m.group(1)); mi = int(m.group(2) or 0); ap = (m.group(3) or "")
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def _parse_end(text: str):
    """Parse a giveaway end spec into (epoch, None) or (None, error)."""
    text = (text or "").strip().lower()
    now = datetime.datetime.now()
    if text.isdigit():
        return (now + datetime.timedelta(minutes=int(text))).timestamp(), None
    if _END_RE.search(text):
        total = 0; matched = False
        for num, unit in _END_RE.findall(text):
            u = unit.lower()[0]
            if u not in "smhd":
                continue
            total += int(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
            matched = True
        if matched and total > 0:
            return (now + datetime.timedelta(seconds=total)).timestamp(), None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return t.timestamp(), None
    if text.startswith("tomorrow"):
        rest = text[len("tomorrow"):].strip()
        base = now + datetime.timedelta(days=1)
        if rest:
            c = _parse_clock(rest)
            if not c:
                return None, "I couldn't parse the time after 'tomorrow'."
            base = base.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
        else:
            base = base.replace(hour=9, minute=0, second=0, microsecond=0)
        return base.timestamp(), None
    for i, wd in enumerate(_WEEKDAYS):
        if text.startswith(wd[:3]) or text.startswith(wd):
            rest = text[len(wd):].strip()
            days = (i - now.weekday()) % 7
            if days == 0:
                days = 7
            base = now + datetime.timedelta(days=days)
            if rest:
                c = _parse_clock(rest)
                if c:
                    base = base.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
            else:
                base = base.replace(hour=9, minute=0, second=0, microsecond=0)
            return base.timestamp(), None
    return None, (
        "I couldn't understand that duration. Try `30m`, `2h`, `1d`, `tomorrow 9pm`, or `fri 18:00`."
    )


def _err(msg: str):
    return (
        EmbedBuilder().title(emoji_title("error", "Error"))
        .description(msg).color("red").timestamp(datetime.datetime.utcnow()).build()
    )


def _ok(msg: str, title="Success"):
    return (
        EmbedBuilder().title(emoji_title("success", title))
        .description(msg).color("green").timestamp(datetime.datetime.utcnow()).build()
    )


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉 Join", style=discord.ButtonStyle.primary, custom_id="gw_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = await neon_db.get_giveaway_by_message(str(interaction.guild_id), str(interaction.message.id))
        if not gw or gw["status"] != "active":
            return await interaction.response.send_message("This giveaway isn't active.", ephemeral=True)
        cog = interaction.client.get_cog("GiveawayCog")
        if cog:
            req_err = await cog.check_requirements(str(interaction.guild_id), interaction.user, gw)
            if req_err:
                return await interaction.response.send_message(req_err, ephemeral=True)
        if await neon_db.get_entry(gw["id"], str(interaction.user.id)):
            return await interaction.response.send_message("You've already joined!", ephemeral=True)
        await neon_db.add_entry(gw["id"], str(interaction.user.id))
        await interaction.response.send_message("🎉 You're in - good luck!", ephemeral=True)
        if cog:
            await cog.refresh_message(gw)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="gw_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = await neon_db.get_giveaway_by_message(str(interaction.guild_id), str(interaction.message.id))
        if not gw or gw["status"] != "active":
            return await interaction.response.send_message("This giveaway isn't active.", ephemeral=True)
        if not await neon_db.get_entry(gw["id"], str(interaction.user.id)):
            return await interaction.response.send_message("You haven't joined this giveaway.", ephemeral=True)
        await neon_db.remove_entry(gw["id"], str(interaction.user.id))
        await interaction.response.send_message("You left the giveaway.", ephemeral=True)
        cog = interaction.client.get_cog("GiveawayCog")
        if cog:
            await cog.refresh_message(gw)


class GiveawayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(GiveawayView())

    # ── Requirement enforcement ────────────────────────────────────────────

    @staticmethod
    def _level_from_xp(xp: int) -> int:
        lvl = 1
        while 100 * (lvl + 1) + 50 * lvl <= xp:
            lvl += 1
        return lvl

    async def check_requirements(self, guild_id: str, user, gw: dict):
        """Return an error string if the member can't join, else None."""
        if gw.get("required_role_id"):
            if not any(str(r.id) == str(gw["required_role_id"]) for r in getattr(user, "roles", [])):
                return "You need the required role to join this giveaway."
        need_xp = int(gw.get("required_xp") or 0)
        need_lvl = int(gw.get("required_level") or 0)
        need_msg = int(gw.get("required_msgs") or 0)
        if not (need_xp or need_lvl or need_msg):
            return None
        xp = 0
        msgs = 0
        try:
            pool = neon_db.get_pool()
            if pool:
                row = await pool.fetchrow(
                    "SELECT xp, messages FROM leveling_data WHERE guild_id = ? AND user_id = ?",
                    str(guild_id), str(user.id),
                )
                if row:
                    xp = int(row["xp"] or 0)
                    msgs = int(row["messages"] or 0)
        except Exception:
            pass
        level = self._level_from_xp(xp)
        if need_xp and xp < need_xp:
            return f"You need at least **{need_xp:,} XP** to join (you have {xp:,})."
        if need_lvl and level < need_lvl:
            return f"You need to be at least **level {need_lvl}** to join (you are level {level})."
        if need_msg and msgs < need_msg:
            return f"You need at least **{need_msg:,} messages** to join (you have {msgs:,})."
        return None

    # ── Embed / message helpers ──────────────────────────────────────────────

    def _gw_vars(self, gw: dict, guild):
        return {
            "{prize}": gw.get("prize") or "",
            "{winners}": str(gw.get("winners_count", 1)),
            "{host}": f"<@{gw.get('host_id')}>" if gw.get("host_id") else "Unknown",
            "{servername}": guild.name if guild else "",
            "{channel}": guild.get_channel(int(gw["channel_id"])).mention
            if guild and gw.get("channel_id") else "",
        }

    def _fmt_gw(self, text: str, gw: dict, guild):
        t = text or ""
        for k, v in self._gw_vars(gw, guild).items():
            t = t.replace(k, v)
        return t

    def _fmt_gw_embed(self, data: dict, gw: dict, guild):
        d = dict(data or {})
        for key in ("title", "description", "footer_text", "author_name", "url"):
            if d.get(key):
                d[key] = self._fmt_gw(str(d[key]), gw, guild)
        for f in (d.get("fields") or []):
            if not isinstance(f, dict):
                continue
            if f.get("name"):
                f["name"] = self._fmt_gw(str(f["name"]), gw, guild)
            if f.get("value"):
                f["value"] = self._fmt_gw(str(f["value"]), gw, guild)
        return d

    @staticmethod
    def _parse_embed(raw):
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _render_gw_message(self, gw: dict, guild, ended: bool, winners, count: int):
        mt = (gw.get("message_type") or "").strip()
        if mt == "basic":
            emoji = gw.get("emoji") or "\U0001F389"
            txt = self._fmt_gw(gw.get("message") or "{prize}", gw, guild)
            if ended and winners:
                txt += "\n\n\U0001F3C6 Winner(s): " + ", ".join(f"<@{w}>" for w in winners)
            desc = f"{emoji} {txt}" if emoji else txt
            return EmbedBuilder().description(desc).build()
        if mt == "custom" and gw.get("embed"):
            data = self._fmt_gw_embed(self._parse_embed(gw.get("embed")), gw, guild)
            if ended and winners:
                data.setdefault("fields", []).append(
                    {"name": "\U0001F3C6 Winner(s)", "value": ", ".join(f"<@{w}>" for w in winners), "inline": False}
                )
            return embed_from_dict(data)
        return self._build_embed(gw, count, ended, winners)

    def _build_embed(self, gw: dict, count: int, ended: bool, winners):
        end_dt = datetime.datetime.fromtimestamp(gw["end_ts"])
        ends_txt = "Ended" if ended else discord.utils.format_dt(end_dt, "R")
        embed = (
            EmbedBuilder()
            .title("🎉 " + (gw.get("prize") or "Giveaway")[:250])
            .color(0xF1C40F if not ended else 0x808080)
            .field("Status", "Ended" if ended else "Active", inline=True)
            .field("Ends", ends_txt, inline=True)
            .field("Entries", str(count), inline=True)
            .field("Winners", str(gw.get("winners_count", 1)), inline=True)
        )
        if gw.get("description"):
            embed.description(gw["description"][:900])
        if gw.get("required_role_id"):
            embed.field("Required role", f"<@&{gw['required_role_id']}>", inline=True)
        if gw.get("required_xp") or gw.get("required_level") or gw.get("required_msgs"):
            reqs = []
            if gw.get("required_xp"):
                reqs.append(f"{int(gw['required_xp']):,} XP")
            if gw.get("required_level"):
                reqs.append(f"Level {int(gw['required_level'])}")
            if gw.get("required_msgs"):
                reqs.append(f"{int(gw['required_msgs']):,} msgs")
            embed.field("Requirements", " · ".join(reqs), inline=True)
        if winners:
            embed.field("Winner(s)", "\n".join(f"<@{w}>" for w in winners), inline=False)
        embed.footer(f"Giveaway #{gw['id']}")
        if gw.get("thumbnail"):
            embed.thumbnail(gw["thumbnail"])
        return embed.build()

    async def refresh_message(self, gw: dict):
        channel = self.bot.get_channel(int(gw["channel_id"])) if gw.get("channel_id") else None
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(gw["message_id"]))
        except Exception:
            return
        count = await neon_db.count_entries(gw["id"])
        winners = [w for w in (gw.get("winners") or "").split(",") if w]
        ended = gw["status"] == "ended"
        try:
            await msg.edit(
                embed=self._render_gw_message(gw, channel.guild, ended, winners, count),
                view=None if ended else GiveawayView(),
            )
        except Exception as e:
            logger.error(f"refresh_message failed: {e}")

    async def post_giveaway(self, gw: dict):
        channel = self.bot.get_channel(int(gw["channel_id"])) if gw.get("channel_id") else None
        if not channel:
            logger.warning(f"Giveaway {gw['id']} channel {gw.get('channel_id')} not found; will retry.")
            return
        count = await neon_db.count_entries(gw["id"])
        embed = self._render_gw_message(gw, channel.guild, False, [], count)
        msg = await channel.send(embed=embed, view=GiveawayView())
        await neon_db.set_giveaway_posted(gw["id"], msg.id)

    async def end_giveaway(self, gw: dict):
        entries = await neon_db.get_entries(gw["id"])
        winner_ids = []
        if entries:
            k = min(int(gw.get("winners_count", 1)), len(entries))
            winner_ids = random.sample(entries, k)
        await neon_db.end_giveaway(gw["id"], ",".join(winner_ids))
        channel = self.bot.get_channel(int(gw["channel_id"])) if gw.get("channel_id") else None
        if channel:
            try:
                msg = await channel.fetch_message(int(gw["message_id"]))
                await msg.edit(
                    embed=self._render_gw_message(gw, channel.guild, True, winner_ids, len(entries)),
                    view=None,
                )
            except Exception as e:
                logger.error(f"end_giveaway edit failed: {e}")
            try:
                if winner_ids:
                    mentions = " ".join(f"<@{w}>" for w in winner_ids)
                    await channel.send(f"🎉 Congratulations {mentions}! You won **{gw['prize']}**!")
                else:
                    await channel.send(f"The giveaway for **{gw['prize']}** ended with no entries.")
            except Exception as e:
                logger.error(f"end_giveaway announce failed: {e}")

    async def reroll(self, gw: dict):
        if gw["status"] != "ended":
            return
        entries = await neon_db.get_entries(gw["id"])
        existing = [w for w in (gw.get("winners") or "").split(",") if w]
        pool = [e for e in entries if e not in existing] or entries
        if not pool:
            return
        new_winner = random.choice(pool)
        winners = existing + [new_winner]
        await neon_db.set_winners(gw["id"], ",".join(winners))
        await neon_db.decrement_reroll(gw["id"])
        channel = self.bot.get_channel(int(gw["channel_id"])) if gw.get("channel_id") else None
        if channel:
            gw2 = await neon_db.get_giveaway(gw["id"]) or gw
            try:
                msg = await channel.fetch_message(int(gw["message_id"]))
                count = await neon_db.count_entries(gw["id"])
                await msg.edit(embed=self._render_gw_message(gw2, channel.guild, True, winners, count), view=None)
            except Exception as e:
                logger.error(f"reroll edit failed: {e}")
            try:
                await channel.send(f"🎉 Rerolled! Congratulations <@{new_winner}> - you won **{gw['prize']}**!")
            except Exception as e:
                logger.error(f"reroll announce failed: {e}")

    async def _giveaway_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                for gw in await neon_db.list_giveaways_pending():
                    try:
                        await self.post_giveaway(gw)
                    except Exception as e:
                        logger.error(f"post_giveaway {gw.get('id')} failed: {e}")
                now = time.time()
                for gw in await neon_db.list_giveaways_due(now):
                    try:
                        await self.end_giveaway(gw)
                    except Exception as e:
                        logger.error(f"end_giveaway {gw.get('id')} failed: {e}")
                for gw in await neon_db.list_giveaways_reroll():
                    try:
                        await self.reroll(gw)
                    except Exception as e:
                        logger.error(f"reroll {gw.get('id')} failed: {e}")
            except Exception as e:
                logger.error(f"giveaway loop failed: {e}")
            await asyncio.sleep(10)

    # ── Slash commands ───────────────────────────────────────────────────────

    class GiveawayGroup(app_commands.Group):
        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return False
            if not interaction.user.guild_permissions.manage_messages:
                await interaction.response.send_message(
                    "You need the **Manage Messages** permission to use giveaway commands.",
                    ephemeral=True,
                )
                return False
            return True

    giveaway_group = GiveawayGroup(name="giveaway", description="Run server giveaways")

    @giveaway_group.command(name="start", description="Start a giveaway in this server.")
    @app_commands.describe(
        prize="What is being given away",
        duration="How long (30m, 2h, 1d, tomorrow 9pm, fri 18:00)",
        winners="How many winners to pick",
        channel="Channel to post in (defaults to current)",
        description="Optional description",
        required_role="Optional role required to enter",
    )
    async def giveaway_start(
        self, interaction: discord.Interaction, prize: str, duration: str,
        winners: int = 1, channel: discord.TextChannel = None,
        description: str = "", required_role: discord.Role = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(embed=_err("Use this in a server."), ephemeral=True)
        end_ts, err = _parse_end(duration)
        if err:
            return await interaction.response.send_message(embed=_err(err), ephemeral=True)
        if end_ts <= time.time():
            return await interaction.response.send_message(embed=_err("That end time is in the past."), ephemeral=True)
        if winners < 1:
            winners = 1
        if winners > 20:
            winners = 20
        target = channel or interaction.channel
        gid = await neon_db.create_giveaway(
            str(interaction.guild_id), str(target.id), str(interaction.user.id),
            prize[:300], description[:1000], "", winners,
            str(required_role.id) if required_role else "", end_ts, time.time(),
        )
        if not gid:
            return await interaction.response.send_message(embed=_err("Couldn't create the giveaway."), ephemeral=True)
        gw = await neon_db.get_giveaway(gid)
        if gw:
            try:
                await self.post_giveaway(gw)
            except Exception as e:
                logger.error(f"giveaway_start post failed: {e}")
        await interaction.response.send_message(
            embed=_ok(f"Giveaway started in {target.mention}!", "Giveaway created"), ephemeral=True
        )

    @giveaway_group.command(name="end", description="End a giveaway early by its message ID.")
    @app_commands.describe(message_id="The ID of the giveaway message")
    async def giveaway_end(self, interaction: discord.Interaction, message_id: str):
        gw = await neon_db.get_giveaway_by_message(str(interaction.guild_id), str(message_id))
        if not gw:
            return await interaction.response.send_message(embed=_err("No giveaway found with that message ID."), ephemeral=True)
        if gw["status"] != "active":
            return await interaction.response.send_message(embed=_err("That giveaway is not active."), ephemeral=True)
        await self.end_giveaway(gw)
        await interaction.response.send_message(embed=_ok("Giveaway ended.", "Giveaway ended"), ephemeral=True)

    @giveaway_group.command(name="reroll", description="Pick a new winner for an ended giveaway.")
    @app_commands.describe(message_id="The ID of the giveaway message")
    async def giveaway_reroll(self, interaction: discord.Interaction, message_id: str):
        gw = await neon_db.get_giveaway_by_message(str(interaction.guild_id), str(message_id))
        if not gw:
            return await interaction.response.send_message(embed=_err("No giveaway found with that message ID."), ephemeral=True)
        if gw["status"] != "ended":
            return await interaction.response.send_message(embed=_err("That giveaway hasn't ended yet."), ephemeral=True)
        await self.reroll(gw)
        await interaction.response.send_message(embed=_ok("Rerolled a new winner!", "Rerolled"), ephemeral=True)

    @giveaway_group.command(name="list", description="List this server's giveaways.")
    async def giveaway_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(embed=_err("Use this in a server."), ephemeral=True)
        rows = await neon_db.list_giveaways(str(interaction.guild_id))
        if not rows:
            return await interaction.response.send_message(embed=_ok("There are no giveaways yet."), ephemeral=True)
        lines = []
        for r in rows[:15]:
            status = r["status"]
            ends = discord.utils.format_dt(datetime.datetime.fromtimestamp(r["end_ts"]), "R")
            lines.append(f"**{r['prize'][:60]}** · `{status}` · ends {ends} · ID `{r['id']}`")
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("gift", "Giveaways"))
            .description("\n".join(lines)).color("blue").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = GiveawayCog(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog._giveaway_loop())
