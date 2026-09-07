import time
import random
import discord
from discord import app_commands
from discord.ext import commands
import datetime
from pathlib import Path
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
        preview_path = Path(__file__).resolve().parent.parent.parent / "data" / "preview.png"
        has_preview = preview_path.exists()
        embed = (
            EmbedBuilder()
            .title(emoji_title("bot", "Prowl"))
            .description("A silly little cat bot with a ton of abilities")
            .color("gray")
            .thumbnail("https://prowlbot.xyz/static/favicon.png")
            .field("Servers", str(len(self.bot.guilds)), inline=True)
            .field("Users", str(len(self.bot.users)), inline=True)
            .field("Uptime", uptime, inline=True)
            .field("Python", f"{__import__('sys').version.split()[0]}", inline=True)
            .field("discord.py", discord.__version__, inline=True)
            .field("Version", f"v{variables.__version__}", inline=True)
            .field(
                "Powered By",
                "discord.py, aiohttp, Pillow, psutil, python-dotenv, requests",
                inline=False,
            )
            .field(
                "Links",
                "[GitHub](https://github.com/novexd/prowl) · [Website](https://prowlbot.xyz) · [Status](https://status.prowlbot.xyz)",
                inline=False,
            )
            .footer(f"Prowl v{variables.__version__}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        if has_preview:
            embed.set_image(url="attachment://preview.png")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Website", url="https://prowlbot.xyz", style=discord.ButtonStyle.link))
        view.add_item(discord.ui.Button(label="Status", url="https://status.prowlbot.xyz", style=discord.ButtonStyle.link))
        if has_preview:
            file = discord.File(str(preview_path), filename="preview.png")
            await interaction.response.send_message(embed=embed, view=view, file=file)
        else:
            await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="invite", description="Get Prowl's invite link")
    async def invite(self, interaction: discord.Interaction):
        url = discord.utils.oauth_url(
            self.bot.user.id,
            permissions=discord.Permissions.general(),
            scopes=["bot", "applications.commands"],
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Invite Prowl", url=url, style=discord.ButtonStyle.link))
        embed = (
            EmbedBuilder()
            .title(emoji_title("invite_join", "Invite Prowl"))
            .description(f"Click the button below to add Prowl to your server.\n\n[Direct link]({url})")
            .color("brand")
            .build()
        )
        await interaction.response.send_message(embed=embed, view=view)

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

    # ── /server ───────────────────────────────────────────────────────────
    server_group = app_commands.Group(name="server", description="Server information commands")

    @server_group.command(name="info", description="Show server information.")
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

    @server_group.command(name="id", description="Get this server's ID.")
    async def serverid(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("id", "Server ID"))
            .description(f"```{interaction.guild.id}```")
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )

    # ── /user ─────────────────────────────────────────────────────────────
    user_group = app_commands.Group(name="user", description="User information commands")

    @user_group.command(name="info", description="Show information about a user.")
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

    # ── /role ─────────────────────────────────────────────────────────────
    role_group = app_commands.Group(name="role", description="Role information commands")

    @role_group.command(name="info", description="Show information about a role.")
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

    @role_group.command(name="id", description="Get the ID of a role by name.")
    @app_commands.describe(role="The role to look up")
    async def roleid(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("id", f"{role.name} ID"))
            .description(f"```{role.id}```")
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )

    # ── /channel ──────────────────────────────────────────────────────────
    channel_group = app_commands.Group(name="channel", description="Channel information and management")

    @channel_group.command(name="info", description="Show information about a channel.")
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

    @channel_group.command(name="lock", description="Lock a channel (prevent members from sending messages).")
    @app_commands.describe(channel="The channel to lock (defaults to current channel)")
    async def channel_lock(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        if interaction.user.guild_permissions.manage_channels:
            overwrites = target.overwrites_for(target.guild.default_role)
            overwrites.send_messages = False
            await target.set_permissions(target.guild.default_role, overwrite=overwrites)
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("lock", f"Locked {target.name}")).description(f"{target.mention} has been locked. Members cannot send messages.").color("error").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)
        else:
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Channels permission.").color("error").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

    @channel_group.command(name="unlock", description="Unlock a channel (allow members to send messages).")
    @app_commands.describe(channel="The channel to unlock (defaults to current channel)")
    async def channel_unlock(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        if interaction.user.guild_permissions.manage_channels:
            overwrites = target.overwrites_for(target.guild.default_role)
            overwrites.send_messages = True
            await target.set_permissions(target.guild.default_role, overwrite=overwrites)
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("unlock", f"Unlocked {target.name}")).description(f"{target.mention} has been unlocked. Members can send messages again.").color("success").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)
        else:
            await interaction.response.send_message(embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Channels permission.").color("error").timestamp(datetime.datetime.utcnow()).build(), ephemeral=True)

    @channel_group.command(name="id", description="Get the ID of a channel.")
    @app_commands.describe(channel="The channel to look up")
    async def channelid(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        await interaction.response.send_message(
            embed=EmbedBuilder()
            .title(emoji_title("id", f"{target.name} ID"))
            .description(f"```{target.id}```")
            .color("gray")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )

    # ── /refresh ──────────────────────────────────────────────────────────
    refresh_group = app_commands.Group(name="refresh", description="Command refresh utilities")

    @refresh_group.command(name="commands", description="Force re-sync all slash commands with Discord (admin only)")
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

    @app_commands.command(name="help", description="Show all available commands")
    async def help_command(self, interaction: discord.Interaction):
        view = HelpView(interaction.user.id)
        await interaction.response.send_message(embed=view.build_embed(), view=view)


HELP_CATEGORIES = {
    "General": {
        "emoji": "milestone", "color": "purple",
        "description": "Basic bot utilities and information.",
        "commands": [
            {"name": "/ping", "desc": "Check Prowl's latency and stats.", "usage": "", "perms": None},
            {"name": "/info", "desc": "Show Prowl's info and stats.", "usage": "", "perms": None},
            {"name": "/invite", "desc": "Get Prowl's invite link.", "usage": "", "perms": None},
            {"name": "/avatar", "desc": "Show a user's avatar.", "usage": "[user]", "perms": None},
            {"name": "/server info", "desc": "Show server information.", "usage": "", "perms": None},
            {"name": "/user info", "desc": "Show information about a user.", "usage": "[user]", "perms": None},
            {"name": "/role info", "desc": "Show information about a role.", "usage": "<role>", "perms": None},
            {"name": "/channel info", "desc": "Show information about a channel.", "usage": "[channel]", "perms": None},
            {"name": "/channel lock", "desc": "Lock a channel.", "usage": "[channel]", "perms": "Manage Channels"},
            {"name": "/channel unlock", "desc": "Unlock a channel.", "usage": "[channel]", "perms": "Manage Channels"},
            {"name": "/say", "desc": "Echo back your message.", "usage": "<text> [channel]", "perms": "Manage Messages"},
            {"name": "/refresh commands", "desc": "Force re-sync slash commands with Discord.", "usage": "", "perms": "Administrator"},
            {"name": "/reactionrole add", "desc": "Add a reaction role to a message.", "usage": "<message_link> <emoji> <role>", "perms": "Manage Roles"},
            {"name": "/reactionrole remove", "desc": "Remove a reaction role.", "usage": "<message_link> <emoji>", "perms": "Manage Roles"},
            {"name": "/reactionrole list", "desc": "List all reaction roles.", "usage": "", "perms": None},
            {"name": "/reactionrole clear", "desc": "Clear all reaction roles.", "usage": "", "perms": "Administrator"},
        ],
    },
    "Moderation": {
        "emoji": "shield", "color": "gray",
        "description": "Kick, ban, mute, warn and manage rule-breakers.",
        "commands": [
            {"name": "/kick", "desc": "Kick a member from the server.", "usage": "<member> [reason]", "perms": "Moderator"},
            {"name": "/ban", "desc": "Ban a member from the server.", "usage": "<member> [reason] [delete_days]", "perms": "Moderator"},
            {"name": "/tempban", "desc": "Temporarily ban a member (auto-unbans).", "usage": "<member> <duration> [reason]", "perms": "Moderator"},
            {"name": "/unban", "desc": "Unban a user by ID.", "usage": "<user_id> [reason]", "perms": "Moderator"},
            {"name": "/mute", "desc": "Mute a member.", "usage": "<member> [duration] [reason]", "perms": "Moderator"},
            {"name": "/unmute", "desc": "Remove a mute from a member.", "usage": "<member> [reason]", "perms": "Moderator"},
            {"name": "/warn", "desc": "Warn a member.", "usage": "<member> [reason]", "perms": "Moderator"},
            {"name": "/purge", "desc": "Bulk delete messages in a channel.", "usage": "<count> [member]", "perms": "Moderator"},
            {"name": "/muteevasion", "desc": "Toggle mute evasion detection.", "usage": "<enabled>", "perms": "Moderator"},
            {"name": "/lockdown", "desc": "Toggle emergency server lockdown.", "usage": "", "perms": "Moderator"},
            {"name": "/settings", "desc": "View moderation settings.", "usage": "", "perms": "Moderator"},
        ],
    },
    "Welcomer": {
        "emoji": "welcome", "color": "green",
        "description": "Welcome and goodbye messages for new and leaving members.",
        "commands": [
            {"name": "/welcomer toggle", "desc": "Enable/disable welcome messages.", "usage": "", "perms": "Manage Server"},
            {"name": "/welcomer channel", "desc": "Set the welcome channel.", "usage": "<channel>", "perms": "Manage Server"},
            {"name": "/welcomer goodbyechannel", "desc": "Set the goodbye channel.", "usage": "[channel]", "perms": "Manage Server"},
            {"name": "/welcomer message", "desc": "Set the welcome message.", "usage": "<message>", "perms": "Manage Server"},
            {"name": "/welcomer goodbye", "desc": "Set the goodbye message.", "usage": "<message>", "perms": "Manage Server"},
            {"name": "/welcomer autorole", "desc": "Auto-role for new members.", "usage": "[role]", "perms": "Manage Server"},
            {"name": "/welcomer botrole", "desc": "Role for bots on join.", "usage": "[role]", "perms": "Manage Roles"},
            {"name": "/welcomer nickname", "desc": "Auto-nickname template.", "usage": "[template]", "perms": "Manage Nicknames"},
            {"name": "/welcomer dm", "desc": "Configure welcome DMs.", "usage": "<enabled> [message]", "perms": "Manage Server"},
            {"name": "/welcomer boost", "desc": "Configure boost announcement.", "usage": "<enabled> [channel] [message]", "perms": "Manage Server"},
            {"name": "/welcomer test", "desc": "Test the welcome message.", "usage": "", "perms": "Manage Server"},
            {"name": "/welcomer config", "desc": "View welcomer config.", "usage": "", "perms": "Manage Server"},
        ],
    },
    "Leveling": {
        "emoji": "level_up", "color": "green",
        "description": "XP system, rank cards, leaderboard and level rewards.",
        "commands": [
            {"name": "/rank", "desc": "Check your or another member's rank.", "usage": "[member]", "perms": None},
            {"name": "/level leaderboard", "desc": "Show the XP leaderboard.", "usage": "[page]", "perms": None},
            {"name": "/level toggle", "desc": "Enable/disable XP gain.", "usage": "", "perms": "Manage Server"},
            {"name": "/level setxp", "desc": "Set a user's XP.", "usage": "<member> <xp>", "perms": "Manage Server"},
            {"name": "/level reset", "desc": "Reset a user's XP.", "usage": "<member>", "perms": "Manage Server"},
            {"name": "/level setrole", "desc": "Set a role reward for a level.", "usage": "<level> <role>", "perms": "Manage Server"},
            {"name": "/level announcement", "desc": "Set level-up announcement.", "usage": "<message>", "perms": "Manage Server"},
            {"name": "/level config", "desc": "View leveling config.", "usage": "", "perms": "Manage Server"},
        ],
    },
    "Tickets": {
        "emoji": "ticket", "color": "purple",
        "description": "Support ticket system with panels and threads.",
        "commands": [
            {"name": "/ticket setup", "desc": "Set up the ticket system.", "usage": "<channel> <role> [log_channel]", "perms": "Administrator"},
            {"name": "/ticket panel", "desc": "Send the ticket panel.", "usage": "", "perms": "Administrator"},
            {"name": "/ticket add", "desc": "Add a user to a ticket.", "usage": "<user>", "perms": None},
            {"name": "/ticket remove", "desc": "Remove a user from a ticket.", "usage": "<user>", "perms": None},
            {"name": "/ticket rename", "desc": "Rename a ticket thread.", "usage": "<name>", "perms": None},
            {"name": "/ticket stats", "desc": "View ticket statistics.", "usage": "", "perms": "Manage Server"},
            {"name": "/ticket config", "desc": "View ticket config.", "usage": "", "perms": "Manage Server"},
        ],
    },
    "Giveaways": {
        "emoji": "gift", "color": "green",
        "description": "Create and manage server giveaways.",
        "commands": [
            {"name": "/giveaway start", "desc": "Start a giveaway.", "usage": "<prize> <duration> <winners> [channel] [desc] [role]", "perms": "Manage Messages"},
            {"name": "/giveaway end", "desc": "End a giveaway early.", "usage": "<message_id>", "perms": "Manage Messages"},
            {"name": "/giveaway reroll", "desc": "Pick a new winner.", "usage": "<message_id>", "perms": "Manage Messages"},
            {"name": "/giveaway list", "desc": "List active giveaways.", "usage": "", "perms": "Manage Messages"},
        ],
    },
    "AI": {
        "emoji": "rot", "color": "gray",
        "description": "AI chatbot, image generation and model configuration.",
        "commands": [
            {"name": "/ai chat", "desc": "Chat with the AI.", "usage": "<message>", "perms": None},
            {"name": "/ai imagine", "desc": "Generate an image from text.", "usage": "<prompt>", "perms": None},
            {"name": "/ai clear", "desc": "Clear AI conversation history.", "usage": "", "perms": None},
            {"name": "/ai model", "desc": "Set the AI model.", "usage": "<model>", "perms": "Manage Server"},
            {"name": "/ai prompt", "desc": "Set the AI system prompt.", "usage": "<prompt>", "perms": "Manage Server"},
            {"name": "/ai config", "desc": "View AI configuration.", "usage": "", "perms": "Manage Server"},
        ],
    },
    "Utilities": {
        "emoji": "package", "color": "gray",
        "description": "Reminders, to-dos, invites, members and AFK.",
        "commands": [
            {"name": "/afk", "desc": "Mark yourself as AFK.", "usage": "[reason]", "perms": None},
            {"name": "/remind set", "desc": "Set a reminder.", "usage": "<when> <what>", "perms": None},
            {"name": "/remind list", "desc": "List your reminders.", "usage": "", "perms": None},
            {"name": "/remind cancel", "desc": "Cancel a reminder.", "usage": "<id>", "perms": None},
            {"name": "/todo add", "desc": "Add a to-do item.", "usage": "<task>", "perms": None},
            {"name": "/todo list", "desc": "List your to-dos.", "usage": "", "perms": None},
            {"name": "/todo done", "desc": "Mark a to-do as done.", "usage": "<id>", "perms": None},
            {"name": "/todo clear", "desc": "Clear your to-do list.", "usage": "[done_only]", "perms": None},
            {"name": "/invites stats", "desc": "Show invite leaderboard.", "usage": "", "perms": None},
            {"name": "/invites user", "desc": "Show invite stats for a user.", "usage": "[user]", "perms": None},
            {"name": "/members list", "desc": "List members with a role.", "usage": "<role>", "perms": "Manage Roles"},
            {"name": "/members note", "desc": "Add a note about a member.", "usage": "<member> <note>", "perms": "Manage Roles"},
            {"name": "/members warnings", "desc": "View a member's warnings.", "usage": "<member>", "perms": "Manage Roles"},
            {"name": "/convert", "desc": "Convert an image or audio file to another format.", "usage": "<file> <format>", "perms": None},
            {"name": "/resize", "desc": "Resize an image to specific dimensions.", "usage": "<file> <width> <height>", "perms": None},
            {"name": "/compress", "desc": "Compress an image to reduce file size.", "usage": "<file> [quality]", "perms": None},
            {"name": "/makezip", "desc": "Create a zip archive from files (hosted 24h).", "usage": "<file1> [file2..5] [password]", "perms": None},
        ],
    },
    "Server Setup": {
        "emoji": "heart", "color": "red",
        "description": "Verification, social alerts, global chat and auto-responses.",
        "commands": [
            {"name": "/verify setup", "desc": "Set up the verification panel.", "usage": "<channel> <role> <type>", "perms": "Administrator"},
            {"name": "/verify deploy", "desc": "Repost the verification panel.", "usage": "", "perms": "Administrator"},
            {"name": "/verify remove", "desc": "Remove verification system.", "usage": "", "perms": "Administrator"},
            {"name": "/social youtube", "desc": "Set YouTube upload alerts.", "usage": "<channel_id> [role] [announce]", "perms": "Manage Server"},
            {"name": "/social twitch", "desc": "Set Twitch stream alerts.", "usage": "<channel> [role] [announce]", "perms": "Manage Server"},
            {"name": "/social twitter", "desc": "Set Twitter/X post alerts.", "usage": "<handle> [role] [announce]", "perms": "Manage Server"},
            {"name": "/social config", "desc": "View social alert settings.", "usage": "", "perms": "Manage Server"},
            {"name": "/globalchat link", "desc": "Link to global chat network.", "usage": "", "perms": "Manage Server"},
            {"name": "/globalchat unlink", "desc": "Unlink from global chat.", "usage": "", "perms": "Manage Server"},
            {"name": "/autoresponder add", "desc": "Add an auto-response.", "usage": "<trigger> <response> [match] [channel] [cooldown]", "perms": "Manage Server"},
            {"name": "/autoresponder remove", "desc": "Remove an auto-response.", "usage": "<trigger>", "perms": "Manage Server"},
            {"name": "/autoresponder list", "desc": "List all auto-responses.", "usage": "", "perms": "Manage Server"},
        ],
    },
    "Extras": {
        "emoji": "sparkle", "color": "pink",
        "description": "Music, birthdays, badges, activity roles, temp channels and frenzy.",
        "commands": [
            {"name": "/music play", "desc": "Play a song from URL or search.", "usage": "<query>", "perms": "DJ Role"},
            {"name": "/music skip", "desc": "Skip the current song.", "usage": "", "perms": "DJ Role"},
            {"name": "/music stop", "desc": "Stop playback and clear queue.", "usage": "", "perms": "DJ Role"},
            {"name": "/music queue", "desc": "Show the music queue.", "usage": "", "perms": "DJ Role"},
            {"name": "/music volume", "desc": "Set the player volume.", "usage": "<level>", "perms": "DJ Role"},
            {"name": "/music loop", "desc": "Toggle loop for current track.", "usage": "", "perms": "DJ Role"},
            {"name": "/music shuffle", "desc": "Shuffle the queue.", "usage": "", "perms": "DJ Role"},
            {"name": "/birthday set", "desc": "Set your birthday.", "usage": "<month> <day> [year]", "perms": None},
            {"name": "/birthday list", "desc": "List all birthdays.", "usage": "", "perms": None},
            {"name": "/birthday upcoming", "desc": "Birthdays in the next 7 days.", "usage": "", "perms": None},
            {"name": "/badges", "desc": "View your or another member's badges.", "usage": "[member]", "perms": None},
            {"name": "/activityrole add", "desc": "Auto-assign role by game activity.", "usage": "<activity> <role>", "perms": "Manage Server"},
            {"name": "/activityrole remove", "desc": "Remove an activity role rule.", "usage": "<activity>", "perms": "Manage Server"},
            {"name": "/activityrole list", "desc": "List activity role rules.", "usage": "", "perms": None},
            {"name": "/temp chat", "desc": "Create a temporary text channel.", "usage": "[duration] [name]", "perms": None},
            {"name": "/frenzy", "desc": "Multiply XP gains temporarily.", "usage": "<action> [multiplier] [duration]", "perms": "Manage Server"},
            {"name": "/id", "desc": "Get ID of a member, role, channel or emoji.", "usage": "[member] [role] [channel] [emoji]", "perms": None},
            {"name": "/role_id", "desc": "Get a role ID by name.", "usage": "<role>", "perms": None},
            {"name": "/channel_id", "desc": "Get a channel ID.", "usage": "<channel>", "perms": None},
            {"name": "/server_id", "desc": "Get this server's ID.", "usage": "", "perms": None},
        ],
    },
}


class HelpView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.page = 0
        self.pages = ["Home"] + list(HELP_CATEGORIES.keys())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return False
        return True

    def build_embed(self):
        cat_name = self.pages[self.page]
        if cat_name == "Home":
            return self._build_home()
        cat = HELP_CATEGORIES[cat_name]
        lines = []
        for cmd in cat["commands"]:
            perm_tag = f" `{cmd['perms']}`" if cmd["perms"] else ""
            usage = f" `{cmd['usage']}`" if cmd["usage"] else ""
            lines.append(f"**{cmd['name']}**{usage}\n{cmd['desc']}{perm_tag}")
        embed = (
            EmbedBuilder()
            .title(emoji_title(cat["emoji"], cat_name))
            .description(cat["description"])
            .color(cat["color"])
            .field("Commands", "\n\n".join(lines), inline=False)
            .footer(f"Page {self.page}/{len(self.pages) - 1}  •  <required>  [optional]  `perms`")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        return embed

    def _build_home(self):
        embed = (
            EmbedBuilder()
            .title(emoji_title("sparkle", "Prowl Help"))
            .description("Select a category below or use the buttons to browse.\nArgument keys: `<required>` `[optional]`\nPermission tags show when a role or perm is needed.")
            .color("pink")
            .thumbnail("https://prowlbot.xyz/static/favicon.png")
        )
        for name, cat in HELP_CATEGORIES.items():
            count = len(cat["commands"])
            embed.field(f"{name} ({count})", cat["description"], inline=True)
        embed.footer(f"Page 0/{len(self.pages) - 1}  •  {sum(len(c['commands']) for c in HELP_CATEGORIES.values())} commands total")
        embed.timestamp(datetime.datetime.utcnow())
        return embed.build()

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
            discord.SelectOption(label="Home", value="0"),
        ] + [
            discord.SelectOption(label=name, value=str(i + 1))
            for i, (name, cat) in enumerate(HELP_CATEGORIES.items())
        ],
    )
    async def jump_to(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.page = int(select.values[0])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


async def setup(bot):
    await bot.add_cog(General(bot))
