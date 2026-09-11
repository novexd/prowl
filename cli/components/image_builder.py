import io
import math
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import discord

try:
    from Ediscord import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


CARD_WIDTH = 900
CARD_HEIGHT = 300
AVATAR_SIZE = 180
AVATAR_PADDING = 30

DEFAULT_BG_COLOR = (25, 25, 35, 255)
PRIMARY_COLOR = (139, 92, 246, 255)
ACCENT_COLOR = (229, 237, 245, 255)
MUTED_COLOR = (150, 150, 160, 255)
PROGRESS_BG = (45, 45, 55, 255)
PROGRESS_FG = (139, 92, 246, 255)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a font, falling back to default if needed."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int, int, int],
    radius: int,
    fill: Tuple[int, int, int, int],
) -> None:
    """Draw a rounded rectangle."""
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _draw_progress_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    progress: float,
    bg_color: Tuple[int, int, int, int],
    fg_color: Tuple[int, int, int, int],
    radius: int = 8,
) -> None:
    """Draw a progress bar with rounded corners."""
    _draw_rounded_rect(draw, (x, y, x + width, y + height), radius, bg_color)
    if progress > 0:
        fill_width = int(width * min(1.0, max(0.0, progress)))
        if fill_width > 0:
            _draw_rounded_rect(draw, (x, y, x + fill_width, y + height), radius, fg_color)


def _create_avatar_mask(size: int) -> Image.Image:
    """Create a circular mask for avatars."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    return mask


async def _fetch_avatar(asset: discord.Asset, size: int = 512) -> Optional[Image.Image]:
    """Download and process a Discord avatar asset."""
    try:
        data = await asset.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        logger.warning(f"Failed to fetch avatar: {e}")
        return None


def _get_default_avatar(user_id: int) -> Image.Image:
    """Generate a default avatar based on user ID."""
    colors = [
        (139, 92, 246), (22, 163, 74), (249, 115, 22),
        (239, 68, 68), (6, 182, 212), (236, 72, 153),
    ]
    color = colors[user_id % len(colors)]
    img = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), color + (255,))
    draw = ImageDraw.Draw(img)
    text = str(user_id)[0].upper()
    font = _load_font(60, bold=True)
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((AVATAR_SIZE - w) // 2, (AVATAR_SIZE - h) // 2),
        text, font=font, fill=(255, 255, 255, 255)
    )
    return img


async def create_level_up_card(
    user: discord.Member | discord.User,
    level: int,
    xp: int,
    xp_needed: int,
    *,
    guild_name: str = "Server",
    background: Optional[Image.Image] = None,
    primary_color: Tuple[int, int, int, int] = PRIMARY_COLOR,
    accent_color: Tuple[int, int, int, int] = ACCENT_COLOR,
) -> io.BytesIO:
    """
    Create a level up celebration card.
    
    Returns BytesIO ready for discord.File()
    """
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), DEFAULT_BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    if background:
        bg = background.resize((CARD_WIDTH, CARD_HEIGHT), Image.Resampling.LANCZOS)
        if bg.mode == "RGBA":
            img = Image.alpha_composite(img, bg)
            draw = ImageDraw.Draw(img)
        else:
            img = bg.convert("RGBA")
            draw = ImageDraw.Draw(img)
    
    avatar_img = None
    if isinstance(user, (discord.Member, discord.User)):
        avatar_img = await _fetch_avatar(user.display_avatar, AVATAR_SIZE)
    if avatar_img is None:
        avatar_img = _get_default_avatar(user.id)
    
    mask = _create_avatar_mask(AVATAR_SIZE)
    avatar_output = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (0, 0, 0, 0))
    avatar_output.paste(avatar_img, (0, 0), mask)
    
    avatar_x = CARD_WIDTH - AVATAR_SIZE - AVATAR_PADDING
    avatar_y = (CARD_HEIGHT - AVATAR_SIZE) // 2
    img.paste(avatar_output, (avatar_x, avatar_y), avatar_output)
    
    name_font = _load_font(36, bold=True)
    level_font = _load_font(72, bold=True)
    small_font = _load_font(24)
    tiny_font = _load_font(18)
    
    text_x = AVATAR_PADDING
    text_y = AVATAR_PADDING
    
    draw.text((text_x, text_y), "LEVEL UP!", font=name_font, fill=primary_color)
    text_y += 44
    
    display_name = getattr(user, 'display_name', str(user))
    name = display_name[:32]
    draw.text((text_x, text_y), name, font=level_font, fill=accent_color)
    text_y += 80
    
    lvl_text = f"LEVEL {level}"
    draw.text((text_x, text_y), lvl_text, font=name_font, fill=primary_color)
    
    progress = (xp - xp_for_level(level)) / max(1, xp_for_level(level + 1) - xp_for_level(level)) if xp_needed > 0 else 0
    bar_x = text_x
    bar_y = text_y + 50
    bar_width = CARD_WIDTH - AVATAR_SIZE - AVATAR_PADDING - 2 * AVATAR_PADDING
    bar_height = 16
    
    _draw_progress_bar(draw, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, primary_color, 8)
    
    draw.text(
        (bar_x, bar_y + bar_height + 8),
        f"{xp:,} XP • {xp_needed:,} XP to next level",
        font=small_font, fill=MUTED_COLOR
    )
    
    draw.text(
        (text_x, CARD_HEIGHT - 40),
        f"Server: {guild_name}",
        font=tiny_font, fill=MUTED_COLOR
    )
    
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


async def create_rank_card(
    user: discord.Member | discord.User,
    level: int,
    xp: int,
    xp_needed: int,
    rank: int,
    total_members: int,
    *,
    guild_name: str = "Server",
    background: Optional[Image.Image] = None,
    primary_color: Tuple[int, int, int, int] = PRIMARY_COLOR,
    accent_color: Tuple[int, int, int, int] = ACCENT_COLOR,
) -> io.BytesIO:
    """
    Create a rank card showing user's position, level, and XP progress.
    
    Returns BytesIO ready for discord.File()
    """
    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), DEFAULT_BG_COLOR)
    draw = ImageDraw.Draw(img)
    
    if background:
        bg = background.resize((CARD_WIDTH, CARD_HEIGHT), Image.Resampling.LANCZOS)
        if bg.mode == "RGBA":
            img = Image.alpha_composite(img, bg)
            draw = ImageDraw.Draw(img)
        else:
            img = bg.convert("RGBA")
            draw = ImageDraw.Draw(img)
    
    panel_x = AVATAR_PADDING
    panel_y = AVATAR_PADDING
    panel_w = CARD_WIDTH - 2 * AVATAR_PADDING
    panel_h = CARD_HEIGHT - 2 * AVATAR_PADDING
    _draw_rounded_rect(draw, (panel_x, panel_y, panel_x + panel_w, panel_y + panel_h), 16, (35, 35, 45, 255))
    
    avatar_img = None
    if isinstance(user, (discord.Member, discord.User)):
        avatar_img = await _fetch_avatar(user.display_avatar, AVATAR_SIZE)
    if avatar_img is None:
        avatar_img = _get_default_avatar(user.id)
    
    mask = _create_avatar_mask(AVATAR_SIZE)
    avatar_output = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (0, 0, 0, 0))
    avatar_output.paste(avatar_img, (0, 0), mask)
    
    avatar_x = panel_x + 24
    avatar_y = panel_y + (panel_h - AVATAR_SIZE) // 2
    img.paste(avatar_output, (avatar_x, avatar_y), avatar_output)
    
    name_font = _load_font(32, bold=True)
    level_font = _load_font(28, bold=True)
    small_font = _load_font(22)
    tiny_font = _load_font(16)
    
    text_x = avatar_x + AVATAR_SIZE + 24
    text_y = avatar_y + 12
    
    display_name = getattr(user, 'display_name', str(user))
    name = display_name[:28]
    draw.text((text_x, text_y), name, font=name_font, fill=accent_color)
    text_y += 38
    
    draw.text((text_x, text_y), f"Level {level}", font=level_font, fill=primary_color)
    text_y += 40
    
    rank_text = f"#{rank} of {total_members:,}"
    draw.text((text_x, text_y), rank_text, font=small_font, fill=MUTED_COLOR)
    
    bar_x = text_x
    bar_y = avatar_y + AVATAR_SIZE - 40
    bar_width = panel_w - (AVATAR_SIZE + 48)
    bar_height = 14
    
    progress = (xp - xp_for_level(level)) / max(1, xp_for_level(level + 1) - xp_for_level(level))
    _draw_progress_bar(draw, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, primary_color, 7)
    
    draw.text(
        (bar_x, bar_y - 24),
        f"{xp:,} / {xp + xp_needed:,} XP",
        font=tiny_font, fill=MUTED_COLOR
    )
    
    medal = ["🥇", "🥈", "🥉"]
    badge = medal[rank - 1] if 1 <= rank <= 3 else f"#{rank}"
    badge_font = _load_font(48)
    badge_bbox = draw.textbbox((0, 0), badge, font=badge_font)
    badge_w = badge_bbox[2] - badge_bbox[0]
    draw.text(
        (panel_x + panel_w - badge_w - 24, panel_y + 24),
        badge, font=badge_font, fill=primary_color
    )
    
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def xp_for_level(level: int) -> int:
    """Calculate XP needed for a specific level."""
    return 100 * level + 50 * (level - 1)


__all__ = [
    "create_level_up_card",
    "create_rank_card",
    "xp_for_level",
    "CARD_WIDTH",
    "CARD_HEIGHT",
]