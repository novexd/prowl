import discord
from discord.ext import commands
from discord import app_commands
import json
import datetime
import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
import aiohttp

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import embed_from_dict, emoji_title


WELCOME_BASIC_EMOJI = "\U0001F44B"   # 👋
GOODBYE_BASIC_EMOJI = "\U0001F494"  # 💔

WELCOME_DEFAULTS = {
    "enabled": False,
    "channel_id": None,
    "goodbye_channel_id": None,
    "welcome_message": "Welcome {member} to {server}!",
    "welcome_mode": "default",
    "welcome_embed_data": {},
    "welcome_image_config": None,
    "goodbye_message": "{member} has left {server}.",
    "goodbye_mode": "default",
    "goodbye_embed_data": {},
    "goodbye_image_config": None,
    "welcome_dm": False,
    "welcome_dm_message": "Welcome to **{server}**! Make sure to read the rules.",
    "auto_role_ids": [],
    "bot_auto_role": None,
    "auto_nickname": None,
    "boost_enabled": False,
    "boost_channel_id": None,
    "boost_message": "{user} just boosted **{server}**! {emoji} Thank you for the boost!",
    "boost_emoji": "<:boost:1538660428790370396>",
}

DEFAULT_IMAGE_CONFIG = {
    "enabled": True,
    "width": 950,
    "height": 450,
    "bg_type": "gradient",
    "gradient": {"color1": "#1a1a2e", "color2": "#16213e"},
    "solid_color": "#1a1a2e",
    "bg_image": "",
    "bg_opacity": 100,
    "avatar_border": "#ffffff",
    "avatar_border_width": 6,
    "avatar_border_style": "solid",
    "avatar_size": 150,
    "avatar_y": 60,
    "text_layers": [
        {"content": "Welcome!", "x": 0, "y": 260, "font_size": 38, "color": "#ffffff", "enabled": True},
        {"content": "{name}", "x": 0, "y": 310, "font_size": 26, "color": "#aaaaaa", "enabled": True},
        {"content": "Member #{count}", "x": 0, "y": 350, "font_size": 18, "color": "#666666", "enabled": True},
    ],
}


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\calibrib.ttf" if bold else "C:\\Windows\\Fonts\\calibri.ttf",
        "C:\\Windows\\Fonts\\segoeuib.ttf" if bold else "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _circle_avatar(avatar_bytes: bytes, size: int, border_color: str = "#ffffff",
                   border_width: int = 6, border_style: str = "solid") -> Image.Image:
    raw = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    raw = raw.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(raw, mask=mask)

    bw = max(0, border_width)
    if bw == 0:
        return result

    total = size + bw * 2
    bordered = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    bc = _hex_to_rgb(border_color) + (255,)
    d = ImageDraw.Draw(bordered)
    cx, cy = total // 2, total // 2
    outer_r = total // 2 - 1
    inner_r = outer_r - bw

    if border_style == "none" or bw == 0:
        pass
    elif border_style == "dashed":
        import math
        num_arcs = max(8, outer_r // 4)
        arc_len = math.pi * 2 / num_arcs
        gap = arc_len * 0.45
        for i in range(num_arcs):
            start = i * arc_len + gap / 2
            end = start + arc_len - gap
            d.arc(
                [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
                start=math.degrees(start) - 90, end=math.degrees(end) - 90,
                fill=bc, width=bw,
            )
    elif border_style == "dotted":
        import math
        spacing = max(4, bw + 2)
        circumference = 2 * math.pi * ((outer_r + inner_r) / 2)
        n_dots = max(6, int(circumference / spacing))
        dot_r = max(1, bw // 2)
        mid_r = (outer_r + inner_r) / 2
        for i in range(n_dots):
            angle = (2 * math.pi * i) / n_dots
            dx = cx + mid_r * math.cos(angle)
            dy = cy + mid_r * math.sin(angle)
            d.ellipse([dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r], fill=bc)
    else:
        d.ellipse([0, 0, total - 1, total - 1], fill=bc)

    bordered.paste(result, (bw, bw), result)
    return bordered


def render_image_text(template: str, member: discord.Member) -> str:
    return (template
            .replace("{name}", member.name)
            .replace("{server}", member.guild.name)
            .replace("{count}", str(member.guild.member_count)))


async def generate_card_image(member: discord.Member, config: dict) -> bytes:
    if not config:
        config = DEFAULT_IMAGE_CONFIG
    w = config.get("width", 950)
    h = config.get("height", 450)
    bg_type = config.get("bg_type", "gradient")
    bg_opacity = max(0, min(100, config.get("bg_opacity", 100)))
    alpha = int(255 * bg_opacity / 100)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background by type
    if bg_type == "image":
        bg_url = config.get("bg_image", "")
        if bg_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(bg_url) as resp:
                        if resp.status == 200:
                            bg_data = await resp.read()
                            bg = Image.open(io.BytesIO(bg_data)).convert("RGBA").resize((w, h), Image.LANCZOS)
                            if bg_opacity < 100:
                                overlay = Image.new("RGBA", (w, h), (0, 0, 0, 255 - alpha))
                                bg = Image.alpha_composite(bg, overlay)
                            img.paste(bg, (0, 0))
            except Exception as e:
                logger.warning(f"Failed to load background image: {e}")
        # Fallback gradient behind image if load fails
        if bg_url and img.getextrema() == ((0, 0), (0, 0), (0, 0), (0, 0)):
            bg_type = "gradient"

    if bg_type == "solid":
        solid_hex = config.get("solid_color", "#1a1a2e")
        sc = _hex_to_rgb(solid_hex)
        solid_layer = Image.new("RGBA", (w, h), sc + (alpha,))
        img = Image.alpha_composite(img, solid_layer)
        draw = ImageDraw.Draw(img)

    if bg_type == "gradient":
        grad = config.get("gradient", {})
        c1 = _hex_to_rgb(grad.get("color1", "#1a1a2e"))
        c2 = _hex_to_rgb(grad.get("color2", "#16213e"))
        grad_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad_layer)
        for y_pos in range(h):
            ratio = y_pos / h
            r = int(c1[0] + (c2[0] - c1[0]) * ratio)
            g = int(c1[1] + (c2[1] - c1[1]) * ratio)
            b = int(c1[2] + (c2[2] - c1[2]) * ratio)
            gd.line([(0, y_pos), (w, y_pos)], fill=(r, g, b, alpha))
        img = Image.alpha_composite(img, grad_layer)
        draw = ImageDraw.Draw(img)

    # Avatar
    try:
        avatar_bytes = await member.display_avatar.read()
        av_size = config.get("avatar_size", 150)
        av_y = config.get("avatar_y", 60)
        av_border = config.get("avatar_border", "#ffffff")
        av_bw = config.get("avatar_border_width", 6)
        av_bs = config.get("avatar_border_style", "solid")
        av = _circle_avatar(avatar_bytes, av_size, av_border, av_bw, av_bs)
        av_x = (w - av.width) // 2
        img.paste(av, (av_x, av_y), av)
        draw = ImageDraw.Draw(img)
    except Exception as e:
        logger.warning(f"Failed to load avatar for card: {e}")

    # Text layers
    for layer in config.get("text_layers", []):
        if not layer.get("enabled", True):
            continue
        content = render_image_text(layer.get("content", ""), member)
        font_size = layer.get("font_size", 24)
        font = _load_font(font_size, bold=True)
        color_hex = layer.get("color", "#ffffff")
        color = _hex_to_rgb(color_hex)
        bbox = draw.textbbox((0, 0), content, font=font)
        tw = bbox[2] - bbox[0]
        tx_raw = layer.get("x", 0)
        if tx_raw == 0:
            tx = (w - tw) // 2
        else:
            tx = max(0, min(w - tw, int(tx_raw)))
        ty = layer.get("y", h // 2)
        draw.text((tx, ty), content, font=font, fill=color + (255,))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def get_welcome_settings(guild_id: int):
    settings = await neon_db.load_cached_settings("welcome_settings", guild_id, WELCOME_DEFAULTS)
    # "basic" used to be the bot's default styled embed. Now "basic" is a minimal
    # emoji+description embed, so existing servers that were using "basic" should
    # keep their current look under the new "default" mode. Persist the migration
    # once so an explicit future choice of "basic" is respected.
    changed = False
    if settings.get("welcome_mode") == "basic":
        settings["welcome_mode"] = "default"
        changed = True
    if settings.get("goodbye_mode") == "basic":
        settings["goodbye_mode"] = "default"
        changed = True
    if changed:
        await save_welcome_settings(guild_id, settings)
    return settings


async def save_welcome_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("welcome_settings", guild_id, settings)


def render_welcome(template: str, member: discord.Member) -> str:
    return (template
            .replace("{member}", member.mention)
            .replace("{member.name}", member.name)
            .replace("{member.tag}", member.discriminator if member.discriminator != "0" else "")
            .replace("{member.nick}", member.display_name)
            .replace("{member.id}", str(member.id))
            .replace("{avatar}", str(member.display_avatar.url))
            .replace("{server}", member.guild.name)
            .replace("{server.id}", str(member.guild.id))
            .replace("{count}", str(member.guild.member_count))
            .replace("{server.membercount}", str(member.guild.member_count)))


def render_welcome_embed(data: dict, member: discord.Member) -> dict:
    """Render welcome placeholders inside a custom embed dict's text fields."""
    out = dict(data or {})
    for key in ("title", "description", "footer_text", "author_name", "url"):
        if out.get(key):
            out[key] = render_welcome(str(out[key]), member)
    return out


def _styled_welcome_embed(member: discord.Member, settings: dict) -> discord.Embed:
    """The bot's default styled welcome embed (what 'default' mode reproduces)."""
    msg = render_welcome(settings.get("welcome_message", ""), member)
    return (
        EmbedBuilder()
        .title(emoji_title("welcome", "Welcome!"))
        .description(msg)
        .color("green")
        .thumbnail(member.display_avatar.url)
        .row(
            ('Account Created', discord.utils.format_dt(member.created_at, style='R')),
            ('Member Count', f'{member.guild.member_count:,}')
        )
        .footer(f"User ID: {str(member.id)}")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )


def _basic_welcome_embed(member: discord.Member, settings: dict) -> discord.Embed:
    """Minimal welcome embed: just an emoji and the message, no title/author."""
    msg = render_welcome(settings.get("welcome_message", ""), member)
    return EmbedBuilder().description(f"{WELCOME_BASIC_EMOJI} {msg}").build()


def _styled_goodbye_embed(member: discord.Member, settings: dict) -> discord.Embed:
    """The bot's default styled goodbye embed (what 'default' mode reproduces)."""
    msg = render_welcome(settings.get("goodbye_message", ""), member)
    return (
        EmbedBuilder()
        .title(emoji_title("goodbye", "Goodbye"))
        .description(msg)
        .color("red")
        .thumbnail(member.display_avatar.url)
        .field("Member Count", f"{member.guild.member_count:,}")
        .footer(f"User ID: {str(member.id)}")
        .timestamp(datetime.datetime.utcnow())
        .build()
    )


def _basic_goodbye_embed(member: discord.Member, settings: dict) -> discord.Embed:
    """Minimal goodbye embed: just an emoji and the message, no title/author."""
    msg = render_welcome(settings.get("goodbye_message", ""), member)
    return EmbedBuilder().description(f"{GOODBYE_BASIC_EMOJI} {msg}").build()


def _welcome_embed(member: discord.Member, settings: dict) -> discord.Embed:
    mode = settings.get("welcome_mode", "default")
    if mode == "custom" and settings.get("welcome_embed_data"):
        return embed_from_dict(render_welcome_embed(settings["welcome_embed_data"], member))
    if mode == "basic":
        return _basic_welcome_embed(member, settings)
    return _styled_welcome_embed(member, settings)


def _goodbye_embed(member: discord.Member, settings: dict) -> discord.Embed:
    mode = settings.get("goodbye_mode", "default")
    if mode == "custom" and settings.get("goodbye_embed_data"):
        return embed_from_dict(render_welcome_embed(settings["goodbye_embed_data"], member))
    if mode == "basic":
        return _basic_goodbye_embed(member, settings)
    return _styled_goodbye_embed(member, settings)


def render_boost(template: str, member: discord.Member, emoji: str = "") -> str:
    return (template
            .replace("{user}", member.mention)
            .replace("{member}", member.mention)
            .replace("{name}", member.display_name)
            .replace("{server}", member.guild.name)
            .replace("{emoji}", emoji)
            .replace("{boost_count}", str(member.guild.premium_subscription_count or 0))
            .replace("{boost_tier}", str(member.guild.premium_tier)))


class Welcomer(commands.Cog, name="Welcomer"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = await get_welcome_settings(member.guild.id)
        if not settings.get("enabled"):
            return

        channel = member.guild.get_channel(int(settings.get("channel_id") or 0))
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Custom embed mode renders the user-configured embed; "default" is the
        # bot's styled embed; "basic" is a minimal emoji+description embed.
        mode = settings.get("welcome_mode", "default")
        image_config = settings.get("welcome_image_config")
        card_file = None
        if image_config and image_config.get("enabled"):
            try:
                card_bytes = await generate_card_image(member, image_config)
                card_file = discord.File(io.BytesIO(card_bytes), filename="welcome.png")
            except Exception as e:
                logger.warning(f"Failed to generate welcome card: {e}")

        try:
            embed = _welcome_embed(member, settings)
            if card_file:
                embed.set_image(url="attachment://welcome.png")
            if card_file:
                await channel.send(embed=embed, file=card_file)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send welcome message: {e}")

        if settings.get("welcome_dm"):
            dm_msg = render_welcome(settings.get("welcome_dm_message", "Welcome to **{server}**!"), member)
            try:
                dm_embed = (
                    EmbedBuilder()
                    .title(emoji_title("welcome", f"Welcome to {member.guild.name}!"))
                    .description(dm_msg)
                    .color("green")
                    .thumbnail(member.guild.icon.url if member.guild.icon else None)
                    .timestamp(datetime.datetime.utcnow())
                    .build()
                )
                await member.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Auto-role: add every configured role (falls back to the old single-role key)
        auto_roles = settings.get("auto_role_ids") or []
        if not auto_roles and settings.get("auto_role_id"):
            auto_roles = [settings["auto_role_id"]]
        for rid in auto_roles:
            role = member.guild.get_role(int(rid))
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except Exception as e:
                    logger.warning(f"Failed to add auto-role {rid}: {e}")

        if member.bot:
            bot_role_id = settings.get("bot_auto_role")
            if bot_role_id:
                role = member.guild.get_role(int(bot_role_id))
                if role:
                    try:
                        await member.add_roles(role, reason="Bot auto-role")
                    except Exception as e:
                        logger.warning(f"Failed to add bot auto-role: {e}")

        auto_nick = settings.get("auto_nickname")
        if auto_nick and not member.bot:
            try:
                nick = auto_nick.replace("{user}", member.name).replace("{server}", member.guild.name)
                await member.edit(nick=nick[:32], reason="Auto-nickname")
            except Exception as e:
                logger.warning(f"Failed to set auto-nickname: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        settings = await get_welcome_settings(member.guild.id)
        if not settings.get("enabled"):
            return
        goodbye_ch_id = settings.get("goodbye_channel_id") or settings.get("channel_id")
        channel = member.guild.get_channel(int(goodbye_ch_id or 0))
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        if not settings.get("goodbye_message"):
            return
        mode = settings.get("goodbye_mode", "default")
        image_config = settings.get("goodbye_image_config")
        card_file = None
        if image_config and image_config.get("enabled"):
            try:
                card_bytes = await generate_card_image(member, image_config)
                card_file = discord.File(io.BytesIO(card_bytes), filename="goodbye.png")
            except Exception as e:
                logger.warning(f"Failed to generate goodbye card: {e}")
        try:
            embed = _goodbye_embed(member, settings)
            if card_file:
                embed.set_image(url="attachment://goodbye.png")
            if card_file:
                await channel.send(embed=embed, file=card_file)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send goodbye message: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since and not after.premium_since:
            return
        if not (not before.premium_since and after.premium_since):
            return
        settings = await get_welcome_settings(after.guild.id)
        if not settings.get("boost_enabled"):
            return
        ch_id = settings.get("boost_channel_id") or settings.get("channel_id")
        channel = after.guild.get_channel(int(ch_id or 0)) if ch_id else None
        if not channel or not isinstance(channel, discord.TextChannel):
            return
        emoji = settings.get("boost_emoji") or "<:boost:1538660428790370396>"
        msg = render_boost(
            settings.get("boost_message") or "{user} just boosted **{server}**! {emoji} Thank you for the boost!",
            after, emoji,
        )
        embed = (
            EmbedBuilder()
            .title(emoji_title("boost", "Server Boost!"))
            .description(msg)
            .color("f47fff")
            .thumbnail(after.display_avatar.url)
            .row(
                ('Total Boosts', str(after.guild.premium_subscription_count or 0)),
                ('Boost Tier', f"Tier {after.guild.premium_tier}"),
            )
            .footer(f"User ID: {after.id}")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Failed to send boost message: {e}")

    welcomer_group = app_commands.Group(name="welcomer", description="Welcome message settings")

    @welcomer_group.command(name="toggle", description="Enable or disable welcome messages")
    async def toggle(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["enabled"] = not settings.get("enabled")
        await save_welcome_settings(interaction.guild_id, settings)
        status = "enabled" if settings["enabled"] else "disabled"
        color = "green" if settings["enabled"] else "red"
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Welcomer Toggled")).description(f"Welcome messages **{status}**.").color(color).timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @welcomer_group.command(name="channel", description="Set the welcome message channel")
    @app_commands.describe(channel="The channel for welcome messages")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["channel_id"] = str(channel.id)
        await save_welcome_settings(interaction.guild_id, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Channel Set")).description(f"Welcome channel set to {channel.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @welcomer_group.command(name="goodbyechannel", description="Set the goodbye message channel")
    @app_commands.describe(channel="The channel for goodbye messages. Leave empty to use the welcome channel.")
    async def set_goodbye_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["goodbye_channel_id"] = str(channel.id) if channel else None
        await save_welcome_settings(interaction.guild_id, settings)
        if channel:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("success", "Goodbye Channel Set")).description(f"Goodbye channel set to {channel.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Goodbye Channel Reset")).description("Goodbye messages will use the welcome channel.").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @welcomer_group.command(name="message", description="Set the welcome message")
    @app_commands.describe(message="Use {member}, {server}, {count} as placeholders")
    async def set_message(self, interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        if len(message) > 500:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Too Long")).description("Message too long (max 500 characters).").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["welcome_message"] = message
        await save_welcome_settings(interaction.guild_id, settings)
        preview = render_welcome(message, interaction.user)
        embed = (
            EmbedBuilder()
            .title(emoji_title("success", "Welcome Message Updated"))
            .description(f"**Preview:**\n{preview}")
            .color("green")
            .field("Placeholders", "`{member}` `{member.name}` `{server}` `{count}`")
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcomer_group.command(name="goodbye", description="Set the goodbye message")
    @app_commands.describe(message="Use {member}, {server} as placeholders. Set to 'off' to disable.")
    async def set_goodbye(self, interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["goodbye_message"] = None if message.lower() == "off" else message
        await save_welcome_settings(interaction.guild_id, settings)
        if message.lower() == "off":
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Goodbye Disabled")).description("Goodbye messages have been disabled.").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            preview = render_welcome(message, interaction.user)
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("success", "Goodbye Message Updated")).description(f"**Preview:**\n{preview}").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @welcomer_group.command(name="autorole", description="Set a role to give to new members on join")
    @app_commands.describe(role="The role to assign automatically. Leave empty to remove.")
    async def autorole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["auto_role_id"] = str(role.id) if role else None
        await save_welcome_settings(interaction.guild_id, settings)
        if role:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("success", "Auto-Role Set")).description(f"New members will receive {role.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Auto-Role Removed")).description("Auto-role has been removed.").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @welcomer_group.command(name="botrole", description="Set a role for bots on join")
    @app_commands.describe(role="The role for bots. Leave empty to remove.")
    async def botrole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Roles permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["bot_auto_role"] = str(role.id) if role else None
        await save_welcome_settings(interaction.guild_id, settings)
        if role:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("success", "Bot Auto-Role Set")).description(f"Bots will receive {role.mention}").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Bot Auto-Role Removed")).description("Bot auto-role has been removed.").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @welcomer_group.command(name="nickname", description="Set auto-nickname for new members")
    @app_commands.describe(nickname="Nickname template (use {user} and {server}). Leave empty to disable.")
    async def nickname(self, interaction: discord.Interaction, nickname: str = None):
        if not interaction.user.guild_permissions.manage_nicknames:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Nicknames permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["auto_nickname"] = nickname
        await save_welcome_settings(interaction.guild_id, settings)
        if nickname:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("success", "Auto-Nickname Set")).description(f"New members will be nicknamed: `{nickname}`").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("info", "Auto-Nickname Disabled")).description("Auto-nickname has been removed.").color("green").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )

    @welcomer_group.command(name="test", description="Test the welcome message")
    async def test(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        image_config = settings.get("welcome_image_config")
        card_file = None
        if image_config and image_config.get("enabled"):
            try:
                card_bytes = await generate_card_image(interaction.user, image_config)
                card_file = discord.File(io.BytesIO(card_bytes), filename="welcome.png")
            except Exception as e:
                logger.warning(f"Failed to generate test card: {e}")
        embed = _welcome_embed(interaction.user, settings)
        if card_file:
            embed.set_image(url="attachment://welcome.png")
        if card_file:
            await interaction.response.send_message(embed=embed, file=card_file, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcomer_group.command(name="config", description="View current welcomer configuration")
    async def config(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        channel_id = settings.get("channel_id")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        goodbye_ch_id = settings.get("goodbye_channel_id")
        goodbye_channel = interaction.guild.get_channel(int(goodbye_ch_id)) if goodbye_ch_id else None
        auto_role_id = settings.get("auto_role_id")
        auto_role = interaction.guild.get_role(int(auto_role_id)) if auto_role_id else None
        bot_role_id = settings.get("bot_auto_role")
        bot_role = interaction.guild.get_role(int(bot_role_id)) if bot_role_id else None
        boost_ch_id = settings.get("boost_channel_id")
        boost_ch = interaction.guild.get_channel(int(boost_ch_id)) if boost_ch_id else None
        embed = (
            EmbedBuilder()
            .title(emoji_title("info", "Welcomer Configuration"))
            .color("blue")
            .row(
                ('Enabled', 'Yes' if settings.get('enabled') else 'No'),
                ('Welcome Channel', channel.mention if channel else 'Not set'),
                ('Goodbye Channel', goodbye_channel.mention if goodbye_channel else 'Same as welcome'),
                ('Welcome Mode', settings.get('welcome_mode', 'default').title()),
                ('Goodbye Mode', settings.get('goodbye_mode', 'default').title()),
                ('Welcome DM', 'Yes' if settings.get('welcome_dm') else 'No'),
                ('Auto-Role', auto_role.mention if auto_role else 'None'),
                ('Bot Auto-Role', bot_role.mention if bot_role else 'None'),
                ('Auto-Nickname', settings.get('auto_nickname') or 'Disabled'),
                ('Welcome Message', settings.get('welcome_message', 'Not set')[:1024]),
                ('Goodbye Message', settings.get('goodbye_message') or 'Disabled'),
                ('Boost Enabled', 'Yes' if settings.get('boost_enabled') else 'No'),
                ('Boost Channel', boost_ch.mention if boost_ch else 'Same as welcome'),
                ('Boost Message', settings.get('boost_message', 'Not set')[:1024])
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcomer_group.command(name="dm", description="Configure welcome DM messages")
    @app_commands.describe(enabled="Enable or disable welcome DMs", message="The DM message (optional)")
    async def dm(self, interaction: discord.Interaction, enabled: bool, message: str = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        settings["welcome_dm"] = enabled
        if message:
            settings["welcome_dm_message"] = message
        await save_welcome_settings(interaction.guild_id, settings)
        status = "enabled" if enabled else "disabled"
        color = "green" if enabled else "red"
        embed = (
            EmbedBuilder()
            .title(emoji_title("success", "Welcome DM Updated"))
            .description(f"Welcome DMs are now **{status}**.")
            .color(color)
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        if enabled and message:
            embed.add_field(name="DM Message", value=message[:1024])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcomer_group.command(name="boost", description="Configure the boost announcement")
    @app_commands.describe(enabled="Enable or disable", channel="Channel for boost messages", message="Boost message template")
    async def boost(self, interaction: discord.Interaction, enabled: bool = None, channel: discord.TextChannel = None, message: str = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        if enabled is not None:
            settings["boost_enabled"] = enabled
        if channel is not None:
            settings["boost_channel_id"] = str(channel.id)
        if message is not None:
            settings["boost_message"] = message
        await save_welcome_settings(interaction.guild_id, settings)
        boost_ch_id = settings.get("boost_channel_id")
        boost_ch = interaction.guild.get_channel(int(boost_ch_id)) if boost_ch_id else None
        embed = (
            EmbedBuilder()
            .title(emoji_title("boost", "Boost Announcement"))
            .color("f47fff")
            .row(
                ('Enabled', 'Yes' if settings.get('boost_enabled') else 'No'),
                ('Channel', boost_ch.mention if boost_ch else 'Same as welcome'),
                ('Emoji', settings.get('boost_emoji', '<:boost:1538660428790370396>')),
            )
            .field("Message", settings.get('boost_message', '(default)')[:1024], inline=False)
            .field("Placeholders", "`{user}` `{server}` `{emoji}` `{boost_count}` `{boost_tier}`", inline=False)
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcomer_group.command(name="boosttest", description="Test the boost announcement")
    async def boosttest(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("error", "Permission Denied")).description("You need Manage Server permission.").color("red").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
        settings = await get_welcome_settings(interaction.guild_id)
        emoji = settings.get("boost_emoji") or "<:boost:1538660428790370396>"
        msg = render_boost(
            settings.get("boost_message") or "{user} just boosted **{server}**! {emoji} Thank you for the boost!",
            interaction.user, emoji,
        )
        embed = (
            EmbedBuilder()
            .title(emoji_title("boost", "Server Boost!"))
            .description(msg)
            .color("f47fff")
            .thumbnail(interaction.user.display_avatar.url)
            .row(
                ('Total Boosts', str(interaction.guild.premium_subscription_count or 0)),
                ('Boost Tier', f"Tier {interaction.guild.premium_tier}"),
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcomer(bot))
