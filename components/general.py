import time
import random
import discord
from discord import app_commands
from discord.ext import commands
import datetime
from typing import Optional

from Ediscord import variables, utils, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


class General(commands.Cog):
    """General-purpose commands for Prowl."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check Prowl's latency and stats.")
    async def ping(self, interaction: discord.Interaction):
        import psutil, os
        await interaction.response.defer()
        latency = round(self.bot.latency * 1000)
        uptime = utils.get_uptime()
        guilds = len(self.bot.guilds)
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024
        cpu = process.cpu_percent(interval=0.1)
        embed = (
            EmbedBuilder()
            .title(emoji_title("bolt", "Pong!"))
            .color("warn")
            .row(
                ("Latency", f"{latency}ms"),
                ("Uptime", uptime),
                ("Servers", f"{guilds:,}"),
            )
            .row(
                ("Users", f"{users:,}"),
                ("Memory", f"{mem:.1f} MB"),
                ("CPU", f"{cpu:.1f}%"),
            )
            .row(
                ("Python", f"{__import__('sys').version.split()[0]}"),
                ("discord.py", f"{__import__('discord').__version__}"),
                ("Version", variables.__version__),
            )
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="info", description="Show Prowl's info.")
    async def info(self, interaction: discord.Interaction):
        uptime = utils.get_uptime()
        embed = (
            EmbedBuilder()
            .title(emoji_title("bot", "Prowl"))
            .description("A silly little cat bot with a ton of abilities")
            .color("gray")
            .thumbnail("https://prowlbot.xyz/static/favicon.png")
            .field("Servers", str(len(self.bot.guilds)), inline=True)
            .field("Users", str(len(self.bot.users)), inline=True)
            .field("Uptime", uptime, inline=True)
            .field("Cogs Loaded", str(len(self.bot.cogs)), inline=True)
            .field("Commands", str(len(self.bot.tree.get_commands())), inline=True)
            .field("Python Version", f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}.{__import__('sys').version_info.micro}", inline=True)
            .field("discord.py Version", discord.__version__, inline=True)
            .footer(f"Prowl v{variables.__version__}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="say", description="Echo back your message.")
    @app_commands.describe(text="The text to echo back.", channel="Channel to send to (optional)")
    async def say(self, interaction: discord.Interaction, text: str, channel: discord.TextChannel = None):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Messages permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        target = channel or interaction.channel
        embed = (
            EmbedBuilder()
            .description(text)
            .color("blurple")
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await target.send(embed=embed)
        if target != interaction.channel:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("send", "Message Sent")).description(f"Sent to {target.mention}").color("success").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("send", "Message Sent")).color("success").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @app_commands.command(name="serverinfo", description="Show server information.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        owner = await guild.fetch_member(guild.owner_id) if guild.owner_id else None
        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        categories = len(guild.categories)
        online = sum(1 for m in guild.members if m.status == discord.Status.online)
        idle = sum(1 for m in guild.members if m.status == discord.Status.idle)
        dnd = sum(1 for m in guild.members if m.status == discord.Status.dnd)
        offline = sum(1 for m in guild.members if m.status == discord.Status.offline)
        boost_level = guild.premium_tier
        boost_count = guild.premium_subscription_count or 0
        embed = (
            EmbedBuilder()
            .title(emoji_title("server", guild.name))
            .color("gray")
            .thumbnail(guild.icon.url if guild.icon else None)
            .field("Owner", owner.mention if owner else "Unknown")
            .field("Members", str(guild.member_count), inline=True)
            .field("Humans", str(sum(1 for m in guild.members if not m.bot)), inline=True)
            .field("Bots", str(sum(1 for m in guild.members if m.bot)), inline=True)
            .field("Online", f"🟢 {online}", inline=True)
            .field("Idle", f"🟡 {idle}", inline=True)
            .field("DND", f"🔴 {dnd}", inline=True)
            .field("Text Channels", str(text_channels), inline=True)
            .field("Voice Channels", str(voice_channels), inline=True)
            .field("Categories", str(categories), inline=True)
            .row(
                ('Roles', str(len(guild.roles))),
                ('Emojis', str(len(guild.emojis))),
                ('Boost Level', f'Level {boost_level} ({boost_count} boosts)'),
                ('Created', discord.utils.format_dt(guild.created_at, style='F')),
                ('Server ID', str(guild.id))
            )
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show information about a user.")
    @app_commands.describe(user="The user to look up")
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        roles = [r.mention for r in target.roles if r != target.guild.default_role]
        roles_str = ", ".join(roles[:20]) if roles else "None"
        if len(roles) > 20:
            roles_str += f" and {len(roles) - 20} more..."
        permissions = [p for p, v in target.guild_permissions if v]
        key_perms = [p.replace("_", " ").title() for p in permissions if p in ["administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages", "ban_members", "kick_members"]]
        perms_str = ", ".join(key_perms[:5]) if key_perms else "None"
        embed = (
            EmbedBuilder()
            .title(target.display_name)
            .color(target.color if target.color != discord.Color.default() else "gray")
            .thumbnail(target.display_avatar.url)
            .field("Username", target.name, inline=True)
            .field("Nickname", target.nick or "None", inline=True)
            .field("User ID", str(target.id), inline=True)
            .field("Account Created", discord.utils.format_dt(target.created_at, style="F"), inline=True)
            .field("Joined Server", discord.utils.format_dt(target.joined_at, style="F") if target.joined_at else "Unknown", inline=True)
            .field("Roles", roles_str[:1024], inline=True)
            .field("Key Permissions", perms_str, inline=True)
            .field("Status", str(target.status).title(), inline=True)
            .field("Bot", "Yes" if target.bot else "No", inline=True)
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Show a user's avatar.")
    @app_commands.describe(user="The user whose avatar to show")
    async def avatar(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        avatar_url = target.display_avatar.url
        embed = (
            EmbedBuilder()
            .title(emoji_title("member", f"{target.display_name}'s Avatar"))
            .color("gray")
            .image(avatar_url)
            .description(f"[Open in Browser]({avatar_url})")
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="roleinfo", description="Show information about a role.")
    @app_commands.describe(role="The role to look up")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        members_with_role = [m for m in role.guild.members if role in m.roles]
        embed = (
            EmbedBuilder()
            .title(emoji_title("role", role.name))
            .color(role.color if role.color != discord.Color.default() else "brand")
            .field("Role ID", str(role.id), inline=True)
            .field("Color", f"#{role.color.value:06x}" if role.color != discord.Color.default() else "Default", inline=True)
            .field("Position", str(role.position), inline=True)
            .field("Members", str(len(members_with_role)), inline=True)
            .field("Mentionable", "Yes" if role.mentionable else "No", inline=True)
            .field("Hoisted", "Yes" if role.hoist else "No", inline=True)
            .field("Managed", "Yes" if role.managed else "No", inline=True)
            .field("Created", discord.utils.format_dt(role.created_at, style="F"), inline=True)
            .field("Permissions", ", ".join([p.replace("_", " ").title() for p, v in role.permissions if v][:10]) or "None", inline=True)
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="channelinfo", description="Show information about a channel.")
    @app_commands.describe(channel="The channel to look up")
    async def channelinfo(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        topic = target.topic or "No topic set"
        slowmode = target.slowmode_delay
        slowmode_str = f"{slowmode}s" if slowmode else "Disabled"
        embed = (
            EmbedBuilder()
            .title(emoji_title("channel", target.name))
            .color("gray")
            .field("Channel ID", str(target.id), inline=True)
            .field("Type", str(target.type).title(), inline=True)
            .field("Category", target.category.name if target.category else "None", inline=True)
            .field("Topic", topic[:1024], inline=True)
            .field("Slowmode", slowmode_str, inline=True)
            .field("NSFW", "Yes" if target.nsfw else "No", inline=True)
            .field("Position", str(target.position), inline=True)
            .field("Created", discord.utils.format_dt(target.created_at, style="F"), inline=True)
            .footer(f"Requested by {interaction.user.display_name}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="refreshcommands", description="Force re-sync all slash commands with Discord (admin only)")
    async def refreshcommands(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Administrator permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            tree = self.bot.tree
            saved = list(tree.get_commands())
            tree.clear_commands(guild=None)
            await tree.sync()
            for cmd in saved:
                tree.add_command(cmd)
            synced = await tree.sync()
            msg = f"Synced **{len(synced)}** global commands. Stale commands removed."
        except Exception as e:
            msg = f"Sync failed: {e}"
        await interaction.followup.send(
            embed=EmbedBuilder().title(emoji_title("refresh", "Commands Refreshed")).description(msg).color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True,
        )


    # ── Reaction Roles ────────────────────────────────────────────────────

    reactionrole_group = app_commands.Group(name="reactionrole", description="Set up reaction roles")

    @reactionrole_group.command(name="add", description="Add a reaction role to a message")
    @app_commands.describe(message_link="Link to the message", emoji="The emoji to react with", role="The role to give")
    async def rr_add(self, interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Roles permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        parsed = self._parse_message_link(message_link)
        if not parsed:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Invalid Link")).description("Provide a valid message link (right-click > Copy Message Link).").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        guild_id, channel_id, message_id = parsed
        if guild_id != str(interaction.guild_id):
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Wrong Server")).description("That message is not in this server.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Channel Not Found")).description("Could not find that channel.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Message Not Found")).description("Could not find that message.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        if role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Role Too High")).description("That role is higher than or equal to your highest role.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        await neon_db.add_reaction_role(interaction.guild_id, channel_id, message_id, emoji, role.id)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "Reaction Role Added"))
            .description(f"Reacting with {emoji} on [that message]({message_link}) will now give {role.mention}.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @reactionrole_group.command(name="remove", description="Remove a reaction role from a message")
    @app_commands.describe(message_link="Link to the message", emoji="The emoji to remove")
    async def rr_remove(self, interaction: discord.Interaction, message_link: str, emoji: str):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Roles permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        parsed = self._parse_message_link(message_link)
        if not parsed:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Invalid Link")).description("Provide a valid message link.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        _, channel_id, message_id = parsed
        await neon_db.remove_reaction_role(interaction.guild_id, message_id, emoji)
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "Reaction Role Removed"))
            .description(f"Removed reaction role for {emoji}.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @reactionrole_group.command(name="list", description="List all reaction roles in this server")
    async def rr_list(self, interaction: discord.Interaction):
        rows = await neon_db.get_all_reaction_roles(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "No Reaction Roles")).description("No reaction roles set up yet.").color("blue").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        lines = []
        for r in rows[:25]:
            ch = interaction.guild.get_channel(int(r["channel_id"]))
            role = interaction.guild.get_role(int(r["role_id"]))
            ch_name = ch.mention if ch else f"`{r['channel_id']}`"
            role_name = role.mention if role else f"`{r['role_id']}`"
            lines.append(f"{r['emoji']} {role_name} in {ch_name}")
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("hash", "Reaction Roles"))
            .description("\n".join(lines))
            .color("blue")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    @reactionrole_group.command(name="clear", description="Remove all reaction roles in this server")
    async def rr_clear(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Administrator permission.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True,
            )
        await neon_db.clear_reaction_roles(interaction.guild_id)
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("success", "Cleared"))
            .description("All reaction roles have been removed.")
            .color("green")
            .timestamp(datetime.datetime.utcnow())
            .build(),
            ephemeral=True,
        )

    def _parse_message_link(self, link: str):
        """Parse a Discord message link into (guild_id, channel_id, message_id) or None."""
        import re
        m = re.match(r"https?://(?:www\.)?(?:discord\.com|discord\.app)/channels/(\d+)/(\d+)/(\d+)", link)
        if m:
            return m.group(1), m.group(2), m.group(3)
        return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.member.bot:
            return
        rr = await neon_db.get_reaction_role_by_emoji(payload.guild_id, payload.message_id, str(payload.emoji))
        if not rr:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(int(rr["role_id"]))
        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        rr = await neon_db.get_reaction_role_by_emoji(payload.guild_id, payload.message_id, str(payload.emoji))
        if not rr:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return
        role = guild.get_role(int(rr["role_id"]))
        if role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.Forbidden:
                pass


HELP_CATEGORIES = {
    "General": {
        "emoji": "wrench",
        "commands": [
            ("/ping", "Check bot latency"),
            ("/info", "Show bot info & stats"),
            ("/avatar", "Show a user's avatar"),
            ("/serverinfo", "Show server information"),
            ("/userinfo", "Show information about a user"),
            ("/roleinfo", "Show information about a role"),
            ("/channelinfo", "Show information about a channel"),
            ("/say", "Echo back your message"),
            ("/reactionrole add", "Add a reaction role to a message"),
            ("/reactionrole remove", "Remove a reaction role"),
            ("/reactionrole list", "List all reaction roles"),
            ("/reactionrole clear", "Clear all reaction roles"),
        ],
    },
    "Moderation": {
        "emoji": "shield",
        "commands": [
            ("/kick", "Kick a member"),
            ("/ban", "Ban a member"),
            ("/tempban", "Temporarily ban a member"),
            ("/unban", "Unban a user by ID"),
            ("/mute", "Mute a member"),
            ("/unmute", "Remove a mute"),
            ("/warn", "Warn a member"),
            ("/purge", "Bulk delete messages"),
            ("/muteevasion", "Toggle mute evasion detection"),
            ("/lockdown", "Toggle emergency server lockdown"),
            ("/settings", "View moderation settings"),
        ],
    },
    "Welcomer": {
        "emoji": "wave",
        "commands": [
            ("/welcomer toggle", "Enable/disable welcome messages"),
            ("/welcomer channel", "Set welcome channel"),
            ("/welcomer goodbyechannel", "Set goodbye channel"),
            ("/welcomer message", "Set welcome message"),
            ("/welcomer goodbye", "Set goodbye message"),
            ("/welcomer autorole", "Set auto-role for new members"),
            ("/welcomer botrole", "Set role for bots on join"),
            ("/welcomer nickname", "Set auto-nickname template"),
            ("/welcomer dm", "Configure welcome DM messages"),
            ("/welcomer test", "Test the welcome message"),
            ("/welcomer config", "View welcomer config"),
        ],
    },
    "Leveling": {
        "emoji": "chart",
        "commands": [
            ("/level rank", "Check your or another member's rank"),
            ("/level leaderboard", "Show the XP leaderboard"),
            ("/level setxp", "Set a user's XP (admin)"),
            ("/level reset", "Reset a user's XP (admin)"),
            ("/level setrole", "Set a role reward for a level"),
            ("/level toggle", "Enable/disable XP gain"),
            ("/level config", "View leveling config"),
        ],
    },
    "Tickets": {
        "emoji": "ticket",
        "commands": [
            ("/ticket setup", "Set up the ticket system"),
            ("/ticket panel", "Send the ticket panel"),
            ("/ticket add", "Add a user to a ticket"),
            ("/ticket remove", "Remove a user from a ticket"),
            ("/ticket rename", "Rename a ticket"),
            ("/ticket stats", "View ticket statistics"),
        ],
    },
    "Giveaways": {
        "emoji": "gift",
        "commands": [
            ("/giveaway start", "Start a giveaway"),
            ("/giveaway end", "End a giveaway early"),
            ("/giveaway reroll", "Pick a new winner"),
            ("/giveaway list", "List active giveaways"),
        ],
    },
    "AI": {
        "emoji": "robot",
        "commands": [
            ("/ai chat", "Chat with the AI"),
            ("/ai imagine", "Generate an image from text"),
            ("/ai clear", "Clear AI conversation history"),
            ("/ai model", "Set the AI model"),
            ("/ai prompt", "Set the AI system prompt"),
            ("/ai config", "View AI configuration"),
        ],
    },
    "Utilities": {
        "emoji": "bulb",
        "commands": [
            ("/afk", "Mark yourself as AFK"),
            ("/remind set", "Set a reminder"),
            ("/remind list", "List your reminders"),
            ("/remind cancel", "Cancel a reminder"),
            ("/todo add", "Add a to-do item"),
            ("/todo list", "List your to-dos"),
            ("/todo done", "Mark a to-do as done"),
            ("/todo clear", "Clear your to-do list"),
            ("/invites stats", "Show invite leaderboard"),
            ("/invites user", "Show invite stats for a user"),
            ("/members list", "List members with a role"),
            ("/members info", "Get member details"),
            ("/members note", "Add a note about a member"),
            ("/members warnings", "View a member's warnings"),
        ],
    },
    "Other": {
        "emoji": "grid",
        "commands": [
            ("/globalchat link", "Link to global chat network"),
            ("/globalchat unlink", "Unlink from global chat"),
            ("/verify setup", "Set up verification panel"),
            ("/autoresponder add", "Add an auto-response"),
            ("/autoresponder remove", "Remove an auto-response"),
            ("/social youtube", "Set YouTube upload alerts"),
            ("/social twitch", "Set Twitch stream alerts"),
        ],
    },
}


class HelpView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.page = 0
        self.pages = list(HELP_CATEGORIES.keys())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return False
        return True

    def build_embed(self):
        cat_name = self.pages[self.page]
        cat = HELP_CATEGORIES[cat_name]
        lines = [f"`{cmd}` — {desc}" for cmd, desc in cat["commands"]]
        embed = (
            EmbedBuilder()
            .title(emoji_title(cat["emoji"], f"Help — {cat_name}"))
            .description("\n".join(lines))
            .color("blue")
            .footer(f"Page {self.page + 1}/{len(self.pages)}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = (self.page + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.select(
        placeholder="Jump to category...",
        options=[
            discord.SelectOption(label=name, value=str(i), emoji=cat["emoji"])
            for i, (name, cat) in enumerate(HELP_CATEGORIES.items())
        ],
    )
    async def jump_to(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.page = int(select.values[0])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


    @app_commands.command(name="help", description="Show all available commands")
    async def help_command(self, interaction: discord.Interaction):
        view = HelpView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view)


async def setup(bot):
    await bot.add_cog(General(bot))
