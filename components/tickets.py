import discord
from discord.ext import commands
from discord import app_commands
import json
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import embed_from_dict, emoji_title, BUTTON_EMOJIS


TICKET_DEFAULTS = {
    "enabled": False,
    "channel_id": None,
    "support_role_id": None,
    "log_channel_id": None,
    "welcome_message": "Support will be with you shortly. Please describe your issue.",
    "ticket_limit": 3,
    "auto_archive_hours": 72,
    "panel_embed": {},
    "questions": [],
}


async def get_ticket_settings(guild_id: int):
    return await neon_db.load_cached_settings("ticket_settings", guild_id, TICKET_DEFAULTS)


async def save_ticket_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("ticket_settings", guild_id, settings)


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji=BUTTON_EMOJIS.get("ticket_close", "\U0001f6ab"), custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not a Ticket")).description("This command can only be used inside a ticket thread.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        confirm_view = discord.ui.View()

        async def confirm_cb(i: discord.Interaction):
            await i.response.defer()
            transcript = []
            async for msg in channel.history(limit=200, oldest_first=True):
                transcript.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {msg.author.name}: {msg.content}")
            transcript_text = "\n".join(transcript[-100:])

            pool = await neon_db.get_pool()
            if pool:
                try:
                    await pool.execute(
                        "INSERT INTO ticket_logs (guild_id, channel_id, user_id, transcript, closed_at) VALUES (?, ?, ?, ?, ?)",
                        str(i.guild_id), str(channel.id), str(interaction.user.id), transcript_text[:5000], datetime.datetime.utcnow().isoformat(),
                    )
                except Exception as e:
                    logger.warning(f"Failed to log ticket: {e}")

            settings = await get_ticket_settings(i.guild_id)
            log_channel_id = settings.get("log_channel_id")
            if log_channel_id:
                log_channel = i.guild.get_channel(int(log_channel_id))
                if log_channel:
                    owner = channel.owner
                    log_embed = (
                        EmbedBuilder()
                        .title(emoji_title("ticket", "Ticket Closed"))
                        .description(f"Ticket **{channel.name}** has been closed.")
                        .color("red")
                        .row(
                            ("Opened By", owner.mention if owner else "Unknown"),
                            ("Closed By", i.user.mention),
                            ("Messages", str(len(transcript))),
                        )
                        .field("Transcript", f"```\n{transcript_text[:1000]}\n```" if transcript else "No messages.")
                        .footer(f"Thread ID: {channel.id}")
                        .timestamp(datetime.datetime.utcnow())
                        .build()
                    )
                    await log_channel.send(embed=log_embed)

            await channel.edit(archived=True, locked=True, reason=f"Ticket closed by {i.user}")
            try:
                await i.user.send(
                    embed=EmbedBuilder()
                    .title(emoji_title("ticket", "Ticket Closed"))
                    .description(f"Your ticket in **{i.guild.name}** has been closed.")
                    .color("grey")
                    .timestamp(datetime.datetime.utcnow())
                    .build()
                )
            except discord.Forbidden:
                pass

        async def cancel_cb(i: discord.Interaction):
            await i.response.edit_message(
                embed=EmbedBuilder().title(emoji_title("info", "Cancelled")).description("Ticket close cancelled.").color("grey").timestamp(datetime.datetime.utcnow()).build(),
                view=None,
            )

        confirm_btn = discord.ui.Button(label="Confirm Close", style=discord.ButtonStyle.danger)
        confirm_btn.callback = confirm_cb
        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = cancel_cb

        confirm_view.add_item(confirm_btn)
        confirm_view.add_item(cancel_btn)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("warning", "Close Ticket?")).description("This will archive the thread and lock it.").color("orange").timestamp(datetime.datetime.utcnow()).build(),
            view=confirm_view,
            ephemeral=True,
        )


class CreateTicketView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji=BUTTON_EMOJIS.get("ticket_open", "\U0001f3ab"), custom_id="ticket:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_ticket_settings(interaction.guild_id)
        if not settings.get("enabled"):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Configured")).description("Ticket system is not set up yet.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        questions = settings.get("questions") or []
        if questions:
            modal = TicketQuestionsModal(self.cog, questions)
            await interaction.response.send_modal(modal)
        else:
            await self.cog._create_ticket(interaction, [])


class TicketQuestionsModal(discord.ui.Modal):
    def __init__(self, cog, questions):
        super().__init__(title="Open a Ticket")
        self.cog = cog
        self._inputs = []
        for q in questions[:5]:
            item = discord.ui.TextInput(
                label=(q.get("label") or "Question")[:45],
                placeholder=(q.get("placeholder") or "")[:100] or None,
                required=bool(q.get("required", True)),
                max_length=1000,
            )
            self.add_item(item)
            self._inputs.append(item)

    async def on_submit(self, interaction: discord.Interaction):
        answers = [inp.value for inp in self._inputs]
        await self.cog._create_ticket(interaction, answers)


class Tickets(commands.Cog, name="Tickets"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _create_ticket(self, interaction: discord.Interaction, answers: list):
        settings = await get_ticket_settings(interaction.guild_id)
        channel_id = settings.get("channel_id")
        if not channel_id:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not Configured")).description("No ticket channel has been set. Ask an admin to run `/ticket setup`.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        parent = interaction.guild.get_channel(int(channel_id))
        if not parent or not isinstance(parent, discord.TextChannel):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Channel Not Found")).description("The configured ticket channel no longer exists.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        ticket_limit = settings.get("ticket_limit", 3)
        active_threads = [t for t in parent.threads if not t.archived and t.owner_id == interaction.user.id]
        if len(active_threads) >= ticket_limit:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Limit Reached")).description(f"You already have **{len(active_threads)}** open tickets (limit: {ticket_limit}).").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        name = f"ticket-{interaction.user.name[:20].lower().replace(' ', '-')}"
        archive_duration = settings.get("auto_archive_hours", 72)

        try:
            thread = await parent.create_thread(
                name=name,
                auto_archive_duration=min(archive_duration, 10080),
                reason=f"Ticket created by {interaction.user}",
            )
        except Exception as e:
            logger.error(f"Failed to create ticket thread: {e}")
            return await interaction.followup.send(
                embed=EmbedBuilder().title(emoji_title("error", "Error")).description(f"Could not create ticket: {str(e)[:100]}").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )

        welcome = settings.get("welcome_message", "Support will be with you shortly.")
        embed = (
            EmbedBuilder()
            .title(emoji_title("ticket", "Support Ticket"))
            .description(welcome)
            .color("blue")
            .row(
                ("Opened By", interaction.user.mention),
                ("Thread", thread.mention),
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )

        content = interaction.user.mention
        questions = settings.get("questions") or []
        if answers and questions:
            qa = "\n".join(f"**{q.get('label', 'Question')}:** {a}" for q, a in zip(questions[:5], answers) if a)
            if qa:
                content = f"{interaction.user.mention}\n\n{qa}"

        support_role_id = settings.get("support_role_id")
        if support_role_id:
            role = interaction.guild.get_role(int(support_role_id))
            if role:
                content = f"{role.mention} {content}"

        await thread.send(content=content, embed=embed, view=TicketView())

        await interaction.followup.send(
            embed=EmbedBuilder().title(emoji_title("success", "Ticket Opened")).description(f"Your ticket is ready: {thread.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )

    ticket_group = app_commands.Group(name="ticket", description="Ticket system commands")

    @ticket_group.command(name="setup", description="Set up the ticket system using threads")
    @app_commands.describe(
        channel="Channel where the ticket panel lives",
        role="Support role to ping on new tickets",
        log_channel="Channel for ticket transcripts",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
        log_channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Administrator permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        settings = {
            "enabled": True,
            "channel_id": str(channel.id),
            "support_role_id": str(role.id) if role else None,
            "log_channel_id": str(log_channel.id) if log_channel else None,
        }
        await save_ticket_settings(interaction.guild_id, settings)

        embed = (
            EmbedBuilder()
            .title(emoji_title("ticket", "Support Tickets"))
            .description("Need help? Click the button below to open a support ticket.\nA private thread will be created for you.")
            .color("blue")
            .row(
                ("Channel", channel.mention),
                ("Support Role", role.mention if role else "None"),
                ("Transcripts", log_channel.mention if log_channel else "None"),
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        view = CreateTicketView(self)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Setup Complete")).description(f"Ticket panel sent to {channel.mention}. Tickets will open as threads there.").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )

    @ticket_group.command(name="panel", description="Send the ticket panel to the current channel")
    async def panel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Administrator permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        embed = (
            EmbedBuilder()
            .title(emoji_title("ticket", "Support Tickets"))
            .description("Need help? Click the button below to open a support ticket.")
            .color("blue")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        view = CreateTicketView(self)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Panel Sent")).description("Ticket panel sent.").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )

    @ticket_group.command(name="add", description="Add a user to the current ticket thread")
    @app_commands.describe(user="The user to add")
    async def add_user(self, interaction: discord.Interaction, user: discord.Member):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not a Ticket")).description("Use this inside a ticket thread.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        try:
            await interaction.channel.add_member(user)
        except Exception as e:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Failed")).description(f"Could not add user: {str(e)[:100]}").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "User Added"))
            .description(f"{user.mention} has been added to this ticket.")
            .color("green")
            .field("Added By", interaction.user.mention)
            .timestamp(datetime.datetime.utcnow())
            .build(),
        )

    @ticket_group.command(name="remove", description="Remove a user from the current ticket thread")
    @app_commands.describe(user="The user to remove")
    async def remove_user(self, interaction: discord.Interaction, user: discord.Member):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not a Ticket")).description("Use this inside a ticket thread.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        try:
            await interaction.channel.remove_member(user)
        except Exception as e:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Failed")).description(f"Could not remove user: {str(e)[:100]}").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "User Removed"))
            .description(f"{user.mention} has been removed from this ticket.")
            .color("orange")
            .field("Removed By", interaction.user.mention)
            .timestamp(datetime.datetime.utcnow())
            .build(),
        )

    @ticket_group.command(name="rename", description="Rename the current ticket thread")
    @app_commands.describe(name="The new ticket name")
    async def rename(self, interaction: discord.Interaction, name: str):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Not a Ticket")).description("Use this inside a ticket thread.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        new_name = f"ticket-{name[:30].lower().replace(' ', '-')}"
        await interaction.channel.edit(name=new_name, reason=f"Renamed by {interaction.user}")
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "Renamed"))
            .description(f"Ticket renamed to **{new_name}**")
            .color("blue")
            .field("Renamed By", interaction.user.mention)
            .timestamp(datetime.datetime.utcnow())
            .build(),
        )

    @ticket_group.command(name="stats", description="View ticket statistics")
    async def stats(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        settings = await get_ticket_settings(interaction.guild_id)
        channel_id = settings.get("channel_id")
        open_count = 0
        if channel_id:
            parent = interaction.guild.get_channel(int(channel_id))
            if parent:
                open_count = sum(1 for t in parent.threads if not t.archived)

        closed_count = 0
        pool = await neon_db.get_pool()
        if pool:
            try:
                row = await pool.fetchrow(
                    "SELECT COUNT(*) as count FROM ticket_logs WHERE guild_id = ?",
                    str(interaction.guild_id),
                )
                closed_count = row["count"] if row else 0
            except Exception:
                pass

        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("info", "Ticket Statistics"))
            .color("blue")
            .row(
                ("Open Tickets", str(open_count)),
                ("Closed Tickets", str(closed_count)),
                ("Total", str(open_count + closed_count)),
            )
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @ticket_group.command(name="config", description="View current ticket configuration")
    async def config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        settings = await get_ticket_settings(interaction.guild_id)
        channel = interaction.guild.get_channel(int(settings.get("channel_id") or 0))
        role = interaction.guild.get_role(int(settings.get("support_role_id") or 0))
        log_ch = interaction.guild.get_channel(int(settings.get("log_channel_id") or 0))
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("settings", "Ticket Configuration"))
            .color("blue")
            .row(
                ("Enabled", "Yes" if settings.get("enabled") else "No"),
                ("Ticket Channel", channel.mention if channel else "Not set"),
                ("Support Role", role.mention if role else "None"),
                ("Log Channel", log_ch.mention if log_ch else "None"),
                ("Ticket Limit", str(settings.get("ticket_limit", 3))),
                ("Auto-Archive", f"{settings.get('auto_archive_hours', 72)}h"),
            )
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
