import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import EMBED_EMOJIS, emoji_title, embed_from_dict


BIRTHDAY_DEFAULTS = {
    "enabled": True,
    "channel_id": None,
    "role_id": None,
    "message": "Happy birthday {member}! 🎂 Wishing you an amazing day!",
    "message_type": "basic",
    "embed_data": None,
    "allow_year": True,
}


async def get_birthday_settings(guild_id: int):
    return await neon_db.load_cached_settings("birthday_settings", guild_id, BIRTHDAY_DEFAULTS)


async def save_birthday_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("birthday_settings", guild_id, settings)


MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


class Birthday(commands.Cog, name="Birthday"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._birthday_loop())

    # ── Commands ──────────────────────────────────────────────────────

    birthday_group = app_commands.Group(name="birthday", description="Birthday tracking commands")

    @birthday_group.command(name="set", description="Set your birthday")
    @app_commands.describe(
        month="Your birth month (1-12)",
        day="Your birth day (1-31)",
        year="Your birth year (optional, e.g. 1995)",
    )
    async def birthday_set(
        self,
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12],
        day: app_commands.Range[int, 1, 31],
        year: Optional[app_commands.Range[int, 1900, 2026]] = None,
    ):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        try:
            birth_date = datetime.date(year or 2000, month, day)
        except ValueError:
            embed = (
                EmbedBuilder()
                .title(emoji_title("error", "Invalid Date"))
                .description(f"{MONTH_NAMES[month]} {day} is not a valid date.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            embed = (
                EmbedBuilder()
                .title(emoji_title("error", "Error"))
                .description("Database unavailable.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        now = datetime.datetime.utcnow().timestamp()
        await pool.execute(
            "INSERT INTO birthdays (guild_id, user_id, month, day, year, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET month=?, day=?, year=?, created_at=?",
            str(interaction.guild_id),
            str(interaction.user.id),
            month,
            day,
            year,
            now,
            month,
            day,
            year,
            now,
        )

        age_str = ""
        if year:
            today = datetime.date.today()
            try:
                age = today.year - year - (1 if (today.month, today.day) < (month, day) else 0)
                age_str = f" (turning **{age}** this year)" if age > 0 else ""
            except Exception:
                pass

        embed = (
            EmbedBuilder()
            .title(emoji_title("cake", "Birthday Set"))
            .description(f"Birthday set to **{MONTH_NAMES[month]} {day}**{age_str}!")
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @birthday_group.command(name="remove", description="Remove your birthday")
    async def birthday_remove(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            embed = (
                EmbedBuilder()
                .title(emoji_title("error", "Error"))
                .description("Database unavailable.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await pool.execute(
            "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
            str(interaction.guild_id),
            str(interaction.user.id),
        )

        embed = (
            EmbedBuilder()
            .title(emoji_title("check", "Birthday Removed"))
            .description("Your birthday has been removed.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @birthday_group.command(name="list", description="List all birthdays in this server")
    async def birthday_list(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            embed = (
                EmbedBuilder()
                .title(emoji_title("error", "Error"))
                .description("Database unavailable.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        rows = await pool.fetch(
            "SELECT user_id, month, day, year FROM birthdays WHERE guild_id = ? ORDER BY month, day",
            str(interaction.guild_id),
        )

        if not rows:
            embed = (
                EmbedBuilder()
                .title(emoji_title("info", "No Birthdays"))
                .description("No birthdays set in this server.")
                .color("blue")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        today = datetime.date.today()
        lines = []
        for r in rows:
            uid = int(r["user_id"])
            m, d = int(r["month"]), int(r["day"])
            yr = int(r["year"]) if r.get("year") else None
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"User ({uid})"
            age_str = ""
            if yr:
                try:
                    age = today.year - yr - (1 if (today.month, today.day) < (m, d) else 0)
                    if age > 0:
                        age_str = f" ({age})"
                except Exception:
                    pass
            lines.append(f"**{name}** — {MONTH_NAMES[m]} {d}{age_str}")

        embed = (
            EmbedBuilder()
            .title(emoji_title("cake", f"Birthdays ({len(rows)} total)"))
            .description("\n".join(lines))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @birthday_group.command(name="upcoming", description="Show birthdays in the next 7 days")
    async def birthday_upcoming(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        pool = await neon_db.get_pool()
        if not pool:
            embed = (
                EmbedBuilder()
                .title(emoji_title("error", "Error"))
                .description("Database unavailable.")
                .color("red")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        rows = await pool.fetch(
            "SELECT user_id, month, day, year FROM birthdays WHERE guild_id = ?",
            str(interaction.guild_id),
        )

        if not rows:
            embed = (
                EmbedBuilder()
                .title(emoji_title("info", "No Upcoming"))
                .description("No birthdays set in this server.")
                .color("blue")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        today = datetime.date.today()
        upcoming = []

        for r in rows:
            uid = int(r["user_id"])
            m, d = int(r["month"]), int(r["day"])
            yr = int(r["year"]) if r.get("year") else None

            try:
                bday_this_year = datetime.date(today.year, m, d)
                if bday_this_year < today:
                    bday_next = datetime.date(today.year + 1, m, d)
                else:
                    bday_next = bday_this_year
            except ValueError:
                continue

            days_until = (bday_next - today).days
            if days_until <= 7:
                member = interaction.guild.get_member(uid)
                name = member.display_name if member else f"User ({uid})"
                age_str = ""
                if yr:
                    age = bday_next.year - yr
                    age_str = f" (turns **{age}**)"
                upcoming.append((days_until, f"**{name}** — {MONTH_NAMES[m]} {d}{age_str}"))

        if not upcoming:
            embed = (
                EmbedBuilder()
                .title(emoji_title("info", "No Upcoming"))
                .description("No birthdays in the next 7 days.")
                .color("blue")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        upcoming.sort(key=lambda x: x[0])
        lines = []
        for days, text in upcoming:
            if days == 0:
                lines.append(f"{EMBED_EMOJIS.get('cake', '🎂')} {text} — **TODAY!**")
            elif days == 1:
                lines.append(f"{EMBED_EMOJIS.get('sparkle', '🔜')} {text} — **tomorrow!**")
            else:
                lines.append(f"{EMBED_EMOJIS.get('info', '📅')} {text} — in {days} days")

        embed = (
            EmbedBuilder()
            .title(emoji_title("cake", "Upcoming Birthdays"))
            .description("\n".join(lines))
            .color("brand")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Background Loop ──────────────────────────────────────────────

    async def _birthday_loop(self):
        await self.bot.wait_until_ready()
        last_run_date = None
        while not self.bot.is_closed():
            try:
                now = datetime.datetime.utcnow()
                today = now.date()

                if last_run_date != today and now.hour == 0:
                    last_run_date = today
                    await self._announce_birthdays(today)
            except Exception as e:
                logger.error(f"Birthday loop failed: {e}")
            await asyncio.sleep(60)

    async def _announce_birthdays(self, today: datetime.date):
        pool = await neon_db.get_pool()
        if not pool:
            return

        settings_rows = await pool.fetch(
            "SELECT guild_id, settings FROM birthday_settings"
        )

        for row in settings_rows:
            try:
                import json
                settings = json.loads(row["settings"]) if isinstance(row["settings"], str) else row["settings"]
            except Exception:
                settings = {}

            if not settings.get("enabled", True):
                continue

            channel_id = settings.get("channel_id")
            if not channel_id:
                continue

            guild_id = int(row["guild_id"])
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(int(channel_id))
            if not channel:
                continue

            birthday_rows = await pool.fetch(
                "SELECT user_id, year FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?",
                str(guild_id),
                today.month,
                today.day,
            )

            if not birthday_rows:
                continue

            role_id = settings.get("role_id")
            role_mention = f"<@&{role_id}>" if role_id else ""
            msg_template = settings.get("message", BIRTHDAY_DEFAULTS["message"])
            message_type = settings.get("message_type", "basic")
            embed_data = settings.get("embed_data")

            for br in birthday_rows:
                uid = int(br["user_id"])
                member = guild.get_member(uid)
                if not member:
                    continue

                yr = int(br["year"]) if br.get("year") else None
                age_str = ""
                if yr:
                    age = today.year - yr
                    age_str = str(age)

                replacements = {
                    "{member}": member.mention,
                    "{name}": member.display_name,
                    "{server}": guild.name,
                    "{age}": age_str,
                }

                if message_type == "custom" and embed_data:
                    rendered = dict(embed_data)
                    for key in ("title", "description", "footer_text", "author_name"):
                        if rendered.get(key):
                            val = str(rendered[key])
                            for k, v in replacements.items():
                                val = val.replace(k, v)
                            rendered[key] = val
                    embed = embed_from_dict(rendered)
                else:
                    message = msg_template
                    for k, v in replacements.items():
                        message = message.replace(k, v)
                    embed = (
                        EmbedBuilder()
                        .title(emoji_title("cake", f"Happy Birthday, {member.display_name}!"))
                        .description(message)
                        .color("brand")
                        .thumbnail(str(member.display_avatar.url))
                        .timestamp(datetime.datetime.utcnow())
                        .build()
                    )
                try:
                    await channel.send(content=role_mention if role_mention else None, embed=embed)

                    if role_id:
                        try:
                            role = guild.get_role(int(role_id))
                            if role:
                                await member.add_roles(role, reason="Birthday role")
                                self.bot.loop.create_task(self._remove_birthday_role(guild_id, uid, role_id))
                        except Exception as e:
                            logger.warning(f"Failed to assign birthday role in {guild_id}: {e}")

                except Exception as e:
                    logger.warning(f"Failed to send birthday message in {guild_id}: {e}")

    async def _remove_birthday_role(self, guild_id: int, user_id: int, role_id: str):
        await asyncio.sleep(24 * 60 * 60)  # 24 hours
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        member = guild.get_member(user_id)
        if not member:
            return
        role = guild.get_role(int(role_id))
        if not role:
            return
        try:
            await member.remove_roles(role, reason="Birthday role expired (24h)")
        except Exception as e:
            logger.warning(f"Failed to remove birthday role in {guild_id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Birthday(bot))
