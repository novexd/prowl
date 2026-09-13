import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import datetime
import time

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title, EMBED_EMOJIS


# ── Constants ────────────────────────────────────────────────────────────────

HUB_NAMES = ["Hub 1", "Hub 2", "Hub 3", "Hub 4", "Hub 5"]
HUB_LIMIT = 20
HUB_NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

# ── DB Helpers ───────────────────────────────────────────────────────────────


async def _get_stat(key: str):
    pool = await neon_db.get_pool()
    if not pool:
        return None
    row = await pool.fetchrow("SELECT value FROM bot_stats WHERE key = ?", key)
    return row["value"] if row else None


async def _set_stat(key: str, value: str):
    pool = await neon_db.get_pool()
    if not pool:
        return
    await pool.execute(
        "INSERT INTO bot_stats (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        key, value,
    )


def _gc_key(guild_id: int, suffix: str) -> str:
    return f"global_chat_{suffix}_{guild_id}"


async def _get_bool(guild_id: int, suffix: str, default: bool = False) -> bool:
    val = await _get_stat(_gc_key(guild_id, suffix))
    if val is None:
        return default
    return val.lower() == "true"


async def _set_bool(guild_id: int, suffix: str, value: bool):
    await _set_stat(_gc_key(guild_id, suffix), str(value))


async def _get_list(guild_id: int, suffix: str) -> list:
    val = await _get_stat(_gc_key(guild_id, suffix))
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


async def _set_list(guild_id: int, suffix: str, items: list):
    await _set_stat(_gc_key(guild_id, suffix), json.dumps(items))


# ── Hub Helpers ──────────────────────────────────────────────────────────────


async def _get_hub(guild_id: int) -> str:
    val = await _get_stat(_gc_key(guild_id, "hub"))
    return val if val else ""


async def _set_hub(guild_id: int, hub_name: str):
    await _set_stat(_gc_key(guild_id, "hub"), hub_name)


async def _get_hubs() -> dict:
    val = await _get_stat("global_chat_hubs")
    if not val:
        return {name: [] for name in HUB_NAMES}
    try:
        hubs = json.loads(val)
        for name in HUB_NAMES:
            if name not in hubs:
                hubs[name] = []
        return hubs
    except (json.JSONDecodeError, TypeError):
        return {name: [] for name in HUB_NAMES}


async def _set_hubs(hubs: dict):
    await _set_stat("global_chat_hubs", json.dumps(hubs))


async def _join_hub(guild_id: int, hub_name: str) -> tuple[bool, str]:
    if hub_name not in HUB_NAMES:
        return False, "Invalid hub."
    hubs = await _get_hubs()
    if len(hubs.get(hub_name, [])) >= HUB_LIMIT:
        return False, f"{hub_name} is full ({HUB_LIMIT}/{HUB_LIMIT})."
    current = await _get_hub(guild_id)
    if current and current in hubs:
        hubs[current] = [g for g in hubs[current] if g != str(guild_id)]
    if str(guild_id) not in hubs.get(hub_name, []):
        hubs.setdefault(hub_name, []).append(str(guild_id))
    await _set_hubs(hubs)
    await _set_hub(guild_id, hub_name)
    return True, f"Joined {hub_name}."


async def _leave_hub(guild_id: int) -> str:
    hubs = await _get_hubs()
    current = await _get_hub(guild_id)
    if current and current in hubs:
        hubs[current] = [g for g in hubs[current] if g != str(guild_id)]
        await _set_hubs(hubs)
    await _set_hub(guild_id, "")
    was_enabled = await _get_bool(guild_id, "gc_enabled")
    if was_enabled:
        await _set_bool(guild_id, "gc_enabled", False)
    return "Left hub." if current else "Not in a hub."


# ── Activity Tracking ────────────────────────────────────────────────────────

HUB_INACTIVE_HOURS = 18


async def _update_activity(guild_id: int):
    await _set_stat(_gc_key(guild_id, "last_activity"), str(int(time.time())))


async def _get_last_activity(guild_id: int) -> float:
    val = await _get_stat(_gc_key(guild_id, "last_activity"))
    if not val:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ── Modals ───────────────────────────────────────────────────────────────────


class GCBlockUserModal(discord.ui.Modal, title="Block User from Global Chat"):
    user_id = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789012345678 or @user", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id.value.strip()
        uid = raw.strip("<@!>")
        if not uid.isdigit():
            return await interaction.response.send_message("Invalid user ID.", ephemeral=True)
        blocked = await _get_list(interaction.guild_id, "gc_blocked_users")
        if uid not in blocked:
            blocked.append(uid)
            await _set_list(interaction.guild_id, "gc_blocked_users", blocked)
        await interaction.response.defer()
        await _refresh_panel(interaction.client, interaction.guild_id)


class GCUnblockUserModal(discord.ui.Modal, title="Unblock User from Global Chat"):
    user_id = discord.ui.TextInput(label="User ID or @mention", placeholder="e.g. 123456789012345678 or @user", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id.value.strip()
        uid = raw.strip("<@!>")
        if not uid.isdigit():
            return await interaction.response.send_message("Invalid user ID.", ephemeral=True)
        blocked = await _get_list(interaction.guild_id, "gc_blocked_users")
        if uid in blocked:
            blocked.remove(uid)
            await _set_list(interaction.guild_id, "gc_blocked_users", blocked)
        await interaction.response.defer()
        await _refresh_panel(interaction.client, interaction.guild_id)


class GCBlockServerModal(discord.ui.Modal, title="Block Server from Global Chat"):
    server_id = discord.ui.TextInput(label="Server ID", placeholder="e.g. 123456789012345678", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.server_id.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        blocked = await _get_list(interaction.guild_id, "gc_blocked_servers")
        if raw not in blocked:
            blocked.append(raw)
            await _set_list(interaction.guild_id, "gc_blocked_servers", blocked)
        await interaction.response.defer()
        await _refresh_panel(interaction.client, interaction.guild_id)


class GCUnblockServerModal(discord.ui.Modal, title="Unblock Server from Global Chat"):
    server_id = discord.ui.TextInput(label="Server ID", placeholder="e.g. 123456789012345678", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.server_id.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message("Invalid server ID.", ephemeral=True)
        blocked = await _get_list(interaction.guild_id, "gc_blocked_servers")
        if raw in blocked:
            blocked.remove(raw)
            await _set_list(interaction.guild_id, "gc_blocked_servers", blocked)
        await interaction.response.defer()
        await _refresh_panel(interaction.client, interaction.guild_id)


class GCPickChannelModal(discord.ui.Modal, title="Change Global Chat Channel"):
    channel_id = discord.ui.TextInput(label="Channel ID", placeholder="Paste the new channel ID", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.channel_id.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
        ch = interaction.guild.get_channel(int(raw))
        if ch is None:
            return await interaction.response.send_message("Channel not found in this server.", ephemeral=True)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Must be a text channel.", ephemeral=True)
        await _set_stat(_gc_key(interaction.guild_id, "channel"), str(raw))
        await interaction.response.defer()
        await _refresh_panel(interaction.client, interaction.guild_id)


# ── Embed Builders ───────────────────────────────────────────────────────────


def _build_panel_embed(guild: discord.Guild, enabled: bool, muted: bool,
                       channel_id: str | None, blocked_users: list, blocked_servers: list,
                       hub: str):
    ch_mention = f"<#{channel_id}>" if channel_id else "Not set"
    status = f"{EMBED_EMOJIS['check']} **Enabled**" if enabled else f"{EMBED_EMOJIS['cross']} **Disabled**"
    mute_str = f"{EMBED_EMOJIS['mute']} **Muted**" if muted else f"{EMBED_EMOJIS['unmute']} **Active**"
    hub_str = f"{EMBED_EMOJIS['globe']} **Hub:** {hub}" if hub else f"**Hub:** Not in a hub"
    users_str = ", ".join(f"`{u}`" for u in blocked_users[:10]) or "None"
    servers_str = ", ".join(f"`{s}`" for s in blocked_servers[:10]) or "None"
    if len(blocked_users) > 10:
        users_str += f" +{len(blocked_users) - 10} more"
    if len(blocked_servers) > 10:
        servers_str += f" +{len(blocked_servers) - 10} more"
    embed = (
        EmbedBuilder()
        .description(
            f"Use the buttons below to manage global chat.\n\n"
            f"**STATUS**: {status}\n"
            f"{hub_str}\n\n"
            f"Channel: {ch_mention}\n"
            f"Muted: {'Yes' if muted else 'No'}\n"
            f"Blocked Users: {users_str}\n"
            f"Blocked Servers: {servers_str}"
        )
        .header(emoji_title("global_chat" if enabled and not muted else "mute" if muted else "globe", "Global Chat Control Panel"))
        .color("blue" if enabled and not muted else "orange" if muted else "gray")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )
    return embed


def _build_hub_panel_embed(hubs: dict, current_hub: str):
    lines = []
    for i, name in enumerate(HUB_NAMES):
        count = len(hubs.get(name, []))
        marker = " <:check:1538660438626017370>" if name == current_hub else ""
        lines.append(f"**{HUB_NUMBER_EMOJIS[i]} {name}:** {count}/{HUB_LIMIT}{marker}")
    current_str = f"**{current_hub}**" if current_hub else "**None** — join a hub below"
    embed = (
        EmbedBuilder()
        .description(
            f"Join a hub to connect with other servers.\n"
            f"Each hub supports up to **{HUB_LIMIT}** servers.\n\n"
            + "\n".join(lines) +
            f"\n\n**Your Hub:** {current_str}"
        )
        .header(emoji_title("global_chat", "Hub Control Panel"))
        .color("blue")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )
    return embed


# ── Views ────────────────────────────────────────────────────────────────────


async def _get_panel_state(guild_id: int):
    enabled = await _get_bool(guild_id, "gc_enabled")
    muted = await _get_bool(guild_id, "gc_muted")
    channel_id = await _get_stat(_gc_key(guild_id, "channel"))
    blocked_users = await _get_list(guild_id, "gc_blocked_users")
    blocked_servers = await _get_list(guild_id, "gc_blocked_servers")
    hub = await _get_hub(guild_id)
    return enabled, muted, channel_id, blocked_users, blocked_servers, hub


class GCControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        if not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
            return False
        return True

    # Row 1: Toggle, Mute, Block User, Unblock User
    @discord.ui.button(emoji="<:CURRENT_OFF:1546117436556714155>", custom_id="gc_btn_toggle", style=discord.ButtonStyle.danger, row=1)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        hub = await _get_hub(interaction.guild_id)
        if not hub:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You must join a hub before enabling global chat.").header(emoji_title("error", "Hub Required")).color("red").build(),
                ephemeral=True
            )
        current = await _get_bool(interaction.guild_id, "gc_enabled")
        new_state = not current
        await _set_bool(interaction.guild_id, "gc_enabled", new_state)
        enabled, muted, channel_id, blocked_users, blocked_servers, hub = await _get_panel_state(interaction.guild_id)
        embed = _build_panel_embed(interaction.guild, enabled, muted, channel_id, blocked_users, blocked_servers, hub)
        view = _build_main_view(enabled, muted)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(emoji="<:mute_white:1546113568527749220>", custom_id="gc_btn_mute", style=discord.ButtonStyle.secondary, row=1)
    async def btn_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = await _get_bool(interaction.guild_id, "gc_muted")
        new_muted = not current
        await _set_bool(interaction.guild_id, "gc_muted", new_muted)
        if not new_muted:
            await _remove_mute_embed(interaction.client, interaction.guild_id)
        else:
            ch_id = await _get_stat(_gc_key(interaction.guild_id, "channel"))
            if ch_id:
                ch = interaction.guild.get_channel(int(ch_id))
                if ch:
                    try:
                        await ch.send(
                            embed=EmbedBuilder()
                            .description(f"This server has been **muted** by admins.\nMessages will not be relayed to other servers.").header(emoji_title("mute", "Global Chat Muted"))
                            .color("orange")
                            .timestamp(datetime.datetime.utcnow())
                            .build()
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send mute notice in guild {interaction.guild_id}: {e}")
        enabled, muted, channel_id, blocked_users, blocked_servers, hub = await _get_panel_state(interaction.guild_id)
        embed = _build_panel_embed(interaction.guild, enabled, muted, channel_id, blocked_users, blocked_servers, hub)
        view = _build_main_view(enabled, muted)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(emoji="<:user_block_white:1546114552104493066>", custom_id="gc_btn_block_user", style=discord.ButtonStyle.secondary, row=1)
    async def btn_block_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCBlockUserModal())

    @discord.ui.button(emoji="<:user_unblock_white:1546114944385286144>", custom_id="gc_btn_unblock_user", style=discord.ButtonStyle.secondary, row=1)
    async def btn_unblock_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCUnblockUserModal())

    # Row 2: Block Server, Unblock Server, Change Channel, Hubs
    @discord.ui.button(emoji="<:server_block_white:1546115797007597639>", custom_id="gc_btn_block_server", style=discord.ButtonStyle.secondary, row=2)
    async def btn_block_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCBlockServerModal())

    @discord.ui.button(emoji="<:server_unblock_white:1546116037773230163>", custom_id="gc_btn_unblock_server", style=discord.ButtonStyle.secondary, row=2)
    async def btn_unblock_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCUnblockServerModal())

    @discord.ui.button(emoji="<:reload_channels:1546116677719298078>", custom_id="gc_btn_change_channel", style=discord.ButtonStyle.primary, row=2)
    async def btn_change_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCPickChannelModal())

    @discord.ui.button(emoji="<:GLOBAL_GLOBE:1546207683713957958>", custom_id="gc_btn_hubs", style=discord.ButtonStyle.primary, row=2)
    async def btn_hubs(self, interaction: discord.Interaction, button: discord.ui.Button):
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        view = GCHubView()
        await interaction.response.edit_message(embed=embed, view=view)


def _build_main_view(enabled: bool, muted: bool) -> GCControlView:
    view = GCControlView()
    if enabled:
        view.btn_toggle.emoji = "<:CURRENT_ON:1546117669932114014>"
        view.btn_toggle.style = discord.ButtonStyle.success
    else:
        view.btn_toggle.emoji = "<:CURRENT_OFF:1546117436556714155>"
        view.btn_toggle.style = discord.ButtonStyle.danger
    if muted:
        view.btn_mute.emoji = "<:unmute_white:1546113892978270248>"
    else:
        view.btn_mute.emoji = "<:mute_white:1546113568527749220>"
    return view


class GCHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        if not (perms.manage_guild or perms.administrator):
            await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
            return False
        return True

    # Row 1: Hub 1-4
    @discord.ui.button(emoji="1️⃣", custom_id="gc_hub_join_1", style=discord.ButtonStyle.secondary, row=1)
    async def hub_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await _join_hub(interaction.guild_id, HUB_NAMES[0])
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji="2️⃣", custom_id="gc_hub_join_2", style=discord.ButtonStyle.secondary, row=1)
    async def hub_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await _join_hub(interaction.guild_id, HUB_NAMES[1])
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji="3️⃣", custom_id="gc_hub_join_3", style=discord.ButtonStyle.secondary, row=1)
    async def hub_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await _join_hub(interaction.guild_id, HUB_NAMES[2])
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji="4️⃣", custom_id="gc_hub_join_4", style=discord.ButtonStyle.secondary, row=1)
    async def hub_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await _join_hub(interaction.guild_id, HUB_NAMES[3])
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    # Row 2: Hub 5, Leave, Back
    @discord.ui.button(emoji="5️⃣", custom_id="gc_hub_join_5", style=discord.ButtonStyle.secondary, row=2)
    async def hub_5(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await _join_hub(interaction.guild_id, HUB_NAMES[4])
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji="<:NUH_UH2:1546212427375321260>", custom_id="gc_hub_leave", style=discord.ButtonStyle.danger, row=2)
    async def hub_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _leave_hub(interaction.guild_id)
        hubs = await _get_hubs()
        current_hub = await _get_hub(interaction.guild_id)
        embed = _build_hub_panel_embed(hubs, current_hub)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji="<:LEMME_GO_BACk:1546208102725193758>", custom_id="gc_hub_back", style=discord.ButtonStyle.secondary, row=2)
    async def hub_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        enabled, muted, channel_id, blocked_users, blocked_servers, hub = await _get_panel_state(interaction.guild_id)
        embed = _build_panel_embed(interaction.guild, enabled, muted, channel_id, blocked_users, blocked_servers, hub)
        view = _build_main_view(enabled, muted)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Panel Refresh ────────────────────────────────────────────────────────────


async def _find_panel_message(ch: discord.TextChannel, bot_user: discord.ClientUser):
    async for message in ch.history(limit=50):
        if message.author == bot_user and message.embeds:
            title = message.embeds[0].title or ""
            if "Global Chat Control Panel" in title or "Hub Control Panel" in title:
                return message
    return None


async def _find_mute_message(ch: discord.TextChannel, bot_user: discord.ClientUser):
    async for message in ch.history(limit=50):
        if message.author == bot_user and message.embeds:
            title = message.embeds[0].title or ""
            if "Global Chat Muted" in title:
                return message
    return None


async def _remove_panel(bot: commands.Bot, guild_id: int):
    mgmt_ch_id = await _get_stat(_gc_key(guild_id, "gc_management_channel"))
    if not mgmt_ch_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    ch = guild.get_channel(int(mgmt_ch_id))
    if not ch:
        return
    try:
        msg = await _find_panel_message(ch, bot.user)
        if msg:
            await msg.delete()
    except Exception as e:
        logger.warning(f"Failed to remove GC panel in guild {guild_id}: {e}")


async def _remove_mute_embed(bot: commands.Bot, guild_id: int):
    ch_id = await _get_stat(_gc_key(guild_id, "channel"))
    if not ch_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    ch = guild.get_channel(int(ch_id))
    if not ch:
        return
    try:
        msg = await _find_mute_message(ch, bot.user)
        if msg:
            await msg.delete()
    except Exception as e:
        logger.warning(f"Failed to remove mute embed in guild {guild_id}: {e}")


async def _refresh_panel(bot: commands.Bot, guild_id: int):
    mgmt_ch_id = await _get_stat(_gc_key(guild_id, "gc_management_channel"))
    if not mgmt_ch_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    ch = guild.get_channel(int(mgmt_ch_id))
    if not ch:
        return
    enabled = await _get_bool(guild_id, "gc_enabled")
    muted = await _get_bool(guild_id, "gc_muted")
    channel_id = await _get_stat(_gc_key(guild_id, "channel"))
    blocked_users = await _get_list(guild_id, "gc_blocked_users")
    blocked_servers = await _get_list(guild_id, "gc_blocked_servers")
    hub = await _get_hub(guild_id)
    embed = _build_panel_embed(guild, enabled, muted, channel_id, blocked_users, blocked_servers, hub)
    view = GCControlView()
    if enabled:
        view.btn_toggle.emoji = "<:CURRENT_ON:1546117669932114014>"
        view.btn_toggle.style = discord.ButtonStyle.success
    else:
        view.btn_toggle.emoji = "<:CURRENT_OFF:1546117436556714155>"
        view.btn_toggle.style = discord.ButtonStyle.danger
    if muted:
        view.btn_mute.emoji = "<:unmute_white:1546113892978270248>"
    else:
        view.btn_mute.emoji = "<:mute_white:1546113568527749220>"
    try:
        msg = await _find_panel_message(ch, bot.user)
        if msg:
            await msg.edit(embed=embed, view=view)
        else:
            await ch.send(embed=embed, view=view)
    except Exception as e:
        logger.warning(f"Failed to refresh GC panel in guild {guild_id}: {e}")


async def _refresh_hub_panel(bot: commands.Bot, guild_id: int):
    mgmt_ch_id = await _get_stat(_gc_key(guild_id, "gc_management_channel"))
    if not mgmt_ch_id:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    ch = guild.get_channel(int(mgmt_ch_id))
    if not ch:
        return
    hubs = await _get_hubs()
    current_hub = await _get_hub(guild_id)
    embed = _build_hub_panel_embed(hubs, current_hub)
    view = GCHubView()
    try:
        msg = await _find_panel_message(ch, bot.user)
        if msg:
            await msg.edit(embed=embed, view=view)
        else:
            await ch.send(embed=embed, view=view)
    except Exception as e:
        logger.warning(f"Failed to refresh GC hub panel in guild {guild_id}: {e}")


# ── Cog ──────────────────────────────────────────────────────────────────────


class GlobalChat(commands.Cog, name="GlobalChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(GCControlView())
        bot.add_view(GCHubView())
        self._hub_cleanup_task.start()

    def cog_unload(self):
        self._hub_cleanup_task.cancel()

    @tasks.loop(hours=1)
    async def _hub_cleanup_task(self):
        try:
            hubs = await _get_hubs()
            now = time.time()
            changed = False
            for hub_name, guild_ids in list(hubs.items()):
                inactive = []
                for gid in guild_ids:
                    last = await _get_last_activity(int(gid))
                    if last == 0.0 or (now - last) >= HUB_INACTIVE_HOURS * 3600:
                        inactive.append(gid)
                if inactive:
                    hubs[hub_name] = [g for g in guild_ids if g not in inactive]
                    changed = True
                    for gid in inactive:
                        await _set_stat(_gc_key(int(gid), "hub"), "")
                        await _set_bool(int(gid), "gc_enabled", False)
                        logger.info(f"Auto-left hub: guild {gid} inactive for {HUB_INACTIVE_HOURS}h in {hub_name}")
            if changed:
                await _set_hubs(hubs)
        except Exception as e:
            logger.warning(f"Hub cleanup failed: {e}")

    @_hub_cleanup_task.before_loop
    async def _before_hub_cleanup(self):
        await self.bot.wait_until_ready()

    async def get_linked_channel(self, guild_id: int):
        val = await _get_stat(_gc_key(guild_id, "channel"))
        if not val or val == "0":
            return None
        return str(val)

    async def set_linked_channel(self, guild_id: int, channel_id: str):
        await _set_stat(_gc_key(guild_id, "channel"), channel_id)

    async def get_all_linked_channels(self):
        pool = await neon_db.get_pool()
        if not pool:
            return []
        rows = await pool.fetch(
            "SELECT key, value FROM bot_stats WHERE key LIKE 'global_chat_channel_%' AND value != '0' AND value != ''"
        )
        results = []
        for row in rows:
            guild_id = row["key"].rsplit("_", 1)[-1]
            channel_id = row["value"]
            results.append((guild_id, channel_id))
        return results

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        my_channel_id = await self.get_linked_channel(message.guild.id)
        if not my_channel_id:
            return
        if str(message.channel.id) != my_channel_id:
            return

        await _update_activity(message.guild.id)

        enabled = await _get_bool(message.guild.id, "gc_enabled")
        if not enabled:
            return

        muted = await _get_bool(message.guild.id, "gc_muted")
        if muted:
            return

        blocked_users = await _get_list(message.guild.id, "gc_blocked_users")
        if str(message.author.id) in blocked_users:
            return

        blocked_servers = await _get_list(message.guild.id, "gc_blocked_servers")
        if str(message.guild.id) in blocked_servers:
            return

        my_hub = await _get_hub(message.guild.id)
        all_linked = await self.get_all_linked_channels()

        content = message.content[:1000] if message.content else "[attachment]"
        for guild_id, channel_id in all_linked:
            if str(message.guild.id) == guild_id and str(message.channel.id) == channel_id:
                continue

            target_hub = await _get_hub(int(guild_id))
            if my_hub:
                if target_hub != my_hub:
                    continue
            else:
                if target_hub:
                    continue

            target_guild = self.bot.get_guild(int(guild_id))
            if not target_guild:
                continue
            if str(message.guild.id) in blocked_servers:
                continue
            target_channel = target_guild.get_channel(int(channel_id))
            if not target_channel:
                continue
            target_muted = await _get_bool(int(guild_id), "gc_muted")
            if target_muted:
                continue
            target_enabled = await _get_bool(int(guild_id), "gc_enabled")
            if not target_enabled:
                continue
            target_blocked_users = await _get_list(int(guild_id), "gc_blocked_users")
            if str(message.author.id) in target_blocked_users:
                continue
            webhooks = await target_channel.webhooks()
            webhook = discord.utils.get(webhooks, name="GlobalChat")
            if not webhook:
                try:
                    webhook = await target_channel.create_webhook(name="GlobalChat")
                except Exception as e:
                    logger.warning(f"Failed to create GlobalChat webhook: {e}")
                    continue
            try:
                await webhook.send(
                    content=content,
                    username=f"{message.author.display_name} ({message.guild.name})",
                    avatar_url=message.author.display_avatar.url,
                )
            except Exception as e:
                logger.warning(f"GlobalChat webhook send failed: {e}")
                continue

    gc_group = app_commands.Group(name="globalchat", description="Global chat commands")

    @gc_group.command(name="link", description="Link this channel to the global chat network")
    async def link(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Server permission.").header(emoji_title("error", "Permission Denied")).color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        hub = await _get_hub(interaction.guild.id)
        if not hub:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You must join a hub first. Use the control panel to join one.").header(emoji_title("error", "Hub Required")).color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.set_linked_channel(interaction.guild.id, str(interaction.channel_id))
        embed = (
            EmbedBuilder()
            .description(f"This channel ({interaction.channel.mention}) is now linked to the global chat!").header(emoji_title("global_chat", "Global Chat Linked"))
            .color("blue")
            .divider().field("Channel ID", str(interaction.channel_id))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed)

    @gc_group.command(name="unlink", description="Unlink this channel from the global chat")
    async def unlink(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().description("You need Manage Server permission.").header(emoji_title("error", "Permission Denied")).color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        await self.set_linked_channel(interaction.guild.id, "0")
        await interaction.response.send_message(
            embed=EmbedBuilder().description("This channel has been unlinked from global chat.").header(emoji_title("success", "Global Chat Unlinked")).color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @gc_group.command(name="info", description="Check global chat status")
    async def info(self, interaction: discord.Interaction):
        hub_channel_id = await self.get_linked_channel(interaction.guild.id)
        hub = await _get_hub(interaction.guild.id)
        if hub_channel_id:
            channel = self.bot.get_channel(int(hub_channel_id))
            embed = (
                EmbedBuilder()
                .description(f"Global chat is linked to {channel.mention if channel else f'<#{hub_channel_id}>'}").header(emoji_title("global_chat", "Global Chat Status"))
                .color("blue")
                .divider().field("Channel ID", str(hub_channel_id))
                .field("Hub", hub or "None")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        else:
            embed = (
                EmbedBuilder()
                .description("Global chat is not set up yet.").header(emoji_title("globe", "Global Chat Status"))
                .color("gray")
                .timestamp(datetime.datetime.utcnow())
                .build()
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalChat(bot))
