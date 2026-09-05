"""
Personal, member-facing utilities: reminders and a to-do list.

These are per-user (not guild-scoped) and delivered via DM so they follow the
member across every server Prowl is in. Reminders are polled by a background
loop; if a DM can't be delivered we fall back to the original channel.

Commands:
  /remind set   <when> <what>   schedule a reminder (30m, 2h, tomorrow 9am, fri 18:00)
  /remind list                  list your pending reminders
  /remind cancel <id>           cancel a reminder
  /todo add   <task>            add a to-do item
  /todo list                    list your to-do items
  /todo done  <id>              mark an item done
  /todo clear [done_only=true]  clear your list
"""

import re
import time
import asyncio
import datetime
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


# ── Time parsing ────────────────────────────────────────────────────────────

_REL_RE = re.compile(
    r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days)",
    re.I,
)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_clock(text: str):
    """Parse a clock like '9', '9:30', '9am', '9pm', '21:00' -> (h, m) or None."""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = (m.group(3) or "")
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def _parse_when(text: str, now: Optional[datetime.datetime] = None):
    """Parse a free-text `when` into a future datetime. Returns (dt, None) or
    (None, error_message)."""
    now = now or datetime.datetime.now()
    s = text.strip().lower()

    # Relative durations: "30m", "2h", "1d", "1h30m", "45 minutes"
    if _REL_RE.search(s):
        total = 0
        matched = False
        for num, unit in _REL_RE.findall(s):
            u = unit.lower()[0]
            if u not in "smhd":
                continue
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
            total += int(num) * mult
            matched = True
        if matched and total > 0:
            return now + datetime.timedelta(seconds=total), None

    # Bare clock "18:00" / "9:30"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return t, None

    # "tomorrow [time]"
    if s.startswith("tomorrow"):
        rest = s[len("tomorrow"):].strip()
        base = now + datetime.timedelta(days=1)
        if rest:
            c = _parse_clock(rest)
            if not c:
                return None, "I couldn't parse the time after 'tomorrow'."
            base = base.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
        else:
            base = base.replace(hour=9, minute=0, second=0, microsecond=0)
        return base, None

    # Weekday "fri", "friday 18:00", "mon 9am"
    for i, wd in enumerate(_WEEKDAYS):
        if s.startswith(wd[:3]) or s.startswith(wd):
            rest = s[len(wd):].strip()
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
            return base, None

    return None, (
        "I couldn't understand that time. Try something like `30m`, `2h`, "
        "`tomorrow 9am`, or `fri 18:00`."
    )


# ── Embed helpers ────────────────────────────────────────────────────────────

def _err(msg: str):
    return (
        EmbedBuilder()
        .title(emoji_title("error", "Error"))
        .description(msg)
        .color("red")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )


def _ok(msg: str, title="Success"):
    return (
        EmbedBuilder()
        .title(emoji_title("success", title))
        .description(msg)
        .color("green")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )


def _info(msg: str):
    return (
        EmbedBuilder()
        .title(emoji_title("info", "Heads up"))
        .description(msg)
        .color("blue")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Remind group ─────────────────────────────────────────────────────────

    remind_group = app_commands.Group(name="remind", description="Personal reminders")

    @remind_group.command(name="set", description="Set a reminder. e.g. /remind set when:30m what:take out trash")
    @app_commands.describe(
        when="When to remind (30m, 2h, tomorrow 9am, fri 18:00)",
        what="What to remind you about",
    )
    async def remind_set(self, interaction: discord.Interaction, when: str, what: str):
        when_dt, err = _parse_when(when)
        if err:
            return await interaction.response.send_message(embed=_err(err), ephemeral=True)
        if when_dt <= datetime.datetime.now():
            return await interaction.response.send_message(
                embed=_err("That time is in the past."), ephemeral=True
            )
        rid = await neon_db.add_reminder(
            str(interaction.user.id),
            str(interaction.guild_id) if interaction.guild_id else None,
            str(interaction.channel_id) if interaction.channel_id else None,
            what[:900],
            when_dt.timestamp(),
        )
        if not rid:
            return await interaction.response.send_message(
                embed=_err("Couldn't save the reminder. Try again."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=_ok(
                f"I'll DM you {discord.utils.format_dt(when_dt, 'R')} to remind you:\n> {what}",
                "Reminder set",
            ),
            ephemeral=True,
        )

    @remind_group.command(name="list", description="List your pending reminders")
    async def remind_list(self, interaction: discord.Interaction):
        rows = await neon_db.list_reminders(str(interaction.user.id))
        if not rows:
            return await interaction.response.send_message(
                embed=_info("You have no active reminders."), ephemeral=True
            )
        lines = [
            f"`{r['id']}` - {discord.utils.format_dt(datetime.datetime.fromtimestamp(r['remind_at']), 'R')} - {r['message']}"
            for r in rows
        ]
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("remind", "Your reminders"))
            .description("\n".join(lines))
            .color("blue")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @remind_group.command(name="cancel", description="Cancel a reminder by its ID (see /remind list)")
    @app_commands.describe(id="Reminder ID from /remind list")
    async def remind_cancel(self, interaction: discord.Interaction, id: int):
        ok = await neon_db.cancel_reminder(id, str(interaction.user.id))
        if ok:
            await interaction.response.send_message(
                embed=_ok(f"Cancelled reminder `{id}`.", "Reminder cancelled"), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=_err("No matching reminder found."), ephemeral=True
            )

    # ── Todo group ───────────────────────────────────────────────────────────

    todo_group = app_commands.Group(name="todo", description="Personal to-do list")

    @todo_group.command(name="add", description="Add a to-do item")
    @app_commands.describe(task="The task to remember")
    async def todo_add(self, interaction: discord.Interaction, task: str):
        rid = await neon_db.add_todo(str(interaction.user.id), task[:900])
        if not rid:
            return await interaction.response.send_message(
                embed=_err("Couldn't save that. Try again."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=_ok(f"Added: {task}", "To-do added"), ephemeral=True
        )

    @todo_group.command(name="list", description="List your to-do items")
    async def todo_list(self, interaction: discord.Interaction):
        rows = await neon_db.list_todos(str(interaction.user.id))
        if not rows:
            return await interaction.response.send_message(
                embed=_info("Your to-do list is empty. Add one with `/todo add`."), ephemeral=True
            )
        lines = []
        for r in rows:
            mark = "✅" if r["done"] else "⬜"
            lines.append(f"{mark} `{r['id']}` {r['task']}")
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("todo", "Your to-do list"))
            .description("\n".join(lines))
            .color("blue")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @todo_group.command(name="done", description="Mark a to-do item as done")
    @app_commands.describe(id="To-do ID from /todo list")
    async def todo_done(self, interaction: discord.Interaction, id: int):
        ok = await neon_db.complete_todo(id, str(interaction.user.id))
        if ok:
            await interaction.response.send_message(
                embed=_ok(f"Marked `{id}` done.", "To-do updated"), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=_err("No matching to-do item found."), ephemeral=True
            )

    @todo_group.command(name="clear", description="Clear your to-do list")
    @app_commands.describe(done_only="Only clear completed items (leave the rest)")
    async def todo_clear(self, interaction: discord.Interaction, done_only: bool = False):
        await neon_db.clear_todos(str(interaction.user.id), done_only)
        msg = "Cleared completed items." if done_only else "Cleared your to-do list."
        await interaction.response.send_message(embed=_ok(msg, "To-do cleared"), ephemeral=True)

    # ── Delivery loop ────────────────────────────────────────────────────────

    async def _safe_fetch_user(self, uid: int):
        try:
            return await self.bot.fetch_user(uid)
        except Exception:
            return None

    async def _deliver_reminder(self, r: dict):
        rid = r["id"]
        user_id = int(r["user_id"])
        message = r.get("message") or "(no message)"
        content = f"⏰ **Reminder:** {message}"
        sent = False

        user = self.bot.get_user(user_id) or await self._safe_fetch_user(user_id)
        if user:
            try:
                await user.send(content)
                sent = True
            except Exception:
                sent = False

        if not sent and r.get("channel_id"):
            ch = self.bot.get_channel(int(r["channel_id"]))
            if ch:
                try:
                    await ch.send(f"<@{user_id}> {content}")
                    sent = True
                except Exception:
                    sent = False

        # Mark done either way so a permanently unreachable user doesn't loop.
        await neon_db.mark_reminder_done(rid)
        if not sent:
            logger.warning(f"Reminder {rid} could not be delivered to user {user_id}.")

    async def _reminder_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                due = await neon_db.get_due_reminders(time.time())
                for r in due:
                    await self._deliver_reminder(r)
            except Exception as e:
                logger.error(f"reminder loop failed: {e}")
            await asyncio.sleep(15)


async def setup(bot: commands.Bot):
    cog = Reminders(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog._reminder_loop())
