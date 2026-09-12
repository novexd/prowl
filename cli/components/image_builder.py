import io
import random
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageOps

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

RANKS_DEFAULT_CONFIG = {
    "background": "random",
    "primary_color": [255, 255, 255],
    "accent_color": [255, 255, 255],
    "elements": {
        "panel": {"enabled": True},
        "avatar": {"enabled": True, "x": None, "y": None, "size": None},
        "name": {"enabled": True, "x": None, "y": None},
        "xp_value": {"enabled": True, "x": None, "y": None},
        "rank": {"enabled": True, "x": None, "y": None},
        "xp_bar": {"enabled": True, "x": None, "y": None, "width": None, "height": 14},
        "xp_ratio": {"enabled": True},
    },
}


def _to_rgba(color, default):
    if not color:
        return default
    try:
        parts = [int(c) for c in color[:3]] + ([int(color[3])] if len(color) > 3 else [255])
    except (TypeError, ValueError):
        return default
    return tuple(parts)


def _rank_cfg(config):
    """Normalize a persisted rank_card config, filling from RANKS_DEFAULT_CONFIG."""
    if not isinstance(config, dict):
        config = {}
    base = RANKS_DEFAULT_CONFIG
    merged = {
        "background": config.get("background") or "random",
        "primary_color": _to_rgba(config.get("primary_color"), WHITE),
        "accent_color": _to_rgba(config.get("accent_color"), WHITE),
        "elements": {},
    }
    defaults = base["elements"]
    user_elems = config.get("elements") or {}
    if not isinstance(user_elems, dict):
        user_elems = {}
    for name, defs in defaults.items():
        user = user_elems.get(name) or {}
        merged["elements"][name] = {**defs, **user}
    return merged

STATIC_DIR = Path(__file__).resolve().parents[1] / "data" / "static"
FONT_DIR = STATIC_DIR / "fonts"
BACKGROUND_DIR = STATIC_DIR / "img"
BACKGROUND_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif",
}

DEFAULT_BG_COLOR = (25, 25, 35, 255)
PRIMARY_COLOR = (139, 92, 246, 255)
ACCENT_COLOR = (229, 237, 245, 255)
MUTED_COLOR = (150, 150, 160, 255)
WHITE = (255, 255, 255, 255)
PANEL_COLOR = (10, 10, 16, 170)
PROGRESS_BG = (20, 20, 28, 190)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a font, falling back to default if needed."""
    candidates = []
    if FONT_DIR.is_dir():
        font_files = sorted(
            path for path in FONT_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}
        )
        if font_files:
            if bold:
                bold_files = [
                    path for path in font_files
                    if any(token in path.name.lower() for token in ("bold", "semibold", "medium"))
                ]
                candidates.extend(bold_files or font_files)
            else:
                candidates.extend(font_files)

    candidates.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ])
    for path in candidates:
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


def _get_background_paths() -> list[Path]:
    if not BACKGROUND_DIR.is_dir():
        return []
    return sorted(
        path for path in BACKGROUND_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in BACKGROUND_EXTENSIONS
    )


def _open_background(path: Path) -> Optional[Image.Image]:
    try:
        with Image.open(path) as source:
            return source.convert("RGBA")
    except Exception as e:
        logger.warning(f"Failed to load background {path.name}: {e}")
        return None


def _prepare_background(
    background: Optional[Image.Image],
    random_background: bool = False,
) -> Optional[Image.Image]:
    if background is None:
        if not random_background:
            return None
        paths = _get_background_paths()
        if not paths:
            return None
        source = _open_background(random.choice(paths))
    elif isinstance(background, (str, Path)):
        source = _open_background(Path(background))
    else:
        source = background.convert("RGBA")

    if source is None:
        return None
    return ImageOps.fit(
        source,
        (CARD_WIDTH, CARD_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if not text or max_width <= 0:
        return ""
    try:
        if draw.textlength(text, font=font) <= max_width:
            return text
    except Exception:
        return text

    suffix = "..."
    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid] + suffix
        try:
            fits = draw.textlength(candidate, font=font) <= max_width
        except Exception:
            fits = False
        if fits:
            low = mid
        else:
            high = mid - 1
    return text[:low] + suffix


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
    primary_color: Tuple[int, int, int, int] = WHITE,
    accent_color: Tuple[int, int, int, int] = WHITE,
    config: Optional[dict] = None,
) -> io.BytesIO:
    """
    Create a rank card showing user's position, level, and XP progress.

    Returns BytesIO ready for discord.File()

    ``config`` is a persisted ``rank_card`` settings dict (see ``_rank_cfg``)
    controlling background, colors, and element visibility/positions.
    """
    cfg = _rank_cfg(config)
    primary_color = cfg["primary_color"]
    accent_color = cfg["accent_color"]

    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), DEFAULT_BG_COLOR)
    draw = ImageDraw.Draw(img)

    background_image = _prepare_background(
        background,
        random_background=cfg["background"] == "random",
    )
    if background_image is None and cfg["background"] != "random" and isinstance(cfg["background"], str):
        background_image = _prepare_background(cfg["background"], random_background=False)
    if background_image is not None:
        overlay = Image.new("RGBA", background_image.size, (0, 0, 0, 110))
        img = Image.alpha_composite(background_image, overlay)
        draw = ImageDraw.Draw(img)

    panel_x = AVATAR_PADDING
    panel_y = AVATAR_PADDING
    panel_w = CARD_WIDTH - 2 * AVATAR_PADDING
    panel_h = CARD_HEIGHT - 2 * AVATAR_PADDING
    if cfg["elements"]["panel"]["enabled"]:
        _draw_rounded_rect(draw, (panel_x, panel_y, panel_x + panel_w, panel_y + panel_h), 16, PANEL_COLOR)

    avatar_img = None
    if isinstance(user, (discord.Member, discord.User)):
        avatar_img = await _fetch_avatar(user.display_avatar, AVATAR_SIZE)
    if avatar_img is None:
        avatar_img = _get_default_avatar(user.id)

    mask = _create_avatar_mask(AVATAR_SIZE)

    av_cfg = cfg["elements"]["avatar"]
    avatar_size = av_cfg.get("size") or AVATAR_SIZE
    if avatar_size != AVATAR_SIZE:
        avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        mask = _create_avatar_mask(avatar_size)
    avatar_output = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
    avatar_output.paste(avatar_img, (0, 0), mask)

    avatar_x = av_cfg.get("x") or (panel_x + 24)
    avatar_y = av_cfg.get("y") or (panel_y + (panel_h - avatar_size) // 2)
    if av_cfg["enabled"]:
        img.paste(avatar_output, (avatar_x, avatar_y), avatar_output)
        draw.ellipse(
            (avatar_x - 4, avatar_y - 4, avatar_x + avatar_size + 4, avatar_y + avatar_size + 4),
            outline=WHITE,
            width=3,
        )

    name_font = _load_font(32, bold=True)
    small_font = _load_font(18)
    rank_font = _load_font(22, bold=True)
    tiny_font = _load_font(16)

    text_x = avatar_x + avatar_size + 24
    text_y = avatar_y + 12
    display_name = str(getattr(user, "display_name", "") or str(user))
    level_suffix = f"- {level}"
    level_width = draw.textlength(level_suffix, font=name_font)

    name_elems = cfg["elements"]["name"]
    if name_elems["enabled"]:
        name_x = name_elems.get("x") or text_x
        name_y = name_elems.get("y") or text_y
        max_name_width = panel_x + panel_w - name_x - 20 - level_width - 12
        name = _truncate_to_width(draw, display_name, name_font, max_name_width)
        label = f"{name} {level_suffix}" if name else level_suffix
        draw.text((name_x, name_y), label, font=name_font, fill=accent_color)

    xp_value_elems = cfg["elements"]["xp_value"]
    if xp_value_elems["enabled"]:
        xp_x = xp_value_elems.get("x") or text_x
        xp_y = xp_value_elems.get("y") or (text_y + 42)
        draw.text((xp_x, xp_y), f"{xp:,} XP", font=small_font, fill=accent_color)

    bar_cfg = cfg["elements"]["xp_bar"]
    bar_x = bar_cfg.get("x") or text_x
    bar_y = bar_cfg.get("y") or (avatar_y + avatar_size - 40)
    bar_width = bar_cfg.get("width") or (panel_w - (avatar_size + 120))
    bar_height = bar_cfg.get("height") or 14

    rank_cfg = cfg["elements"]["rank"]
    if rank_cfg["enabled"]:
        rank_text = _truncate_to_width(
            draw,
            f"#{rank} of {total_members:,}",
            rank_font,
            bar_width,
        )
        rank_width = draw.textlength(rank_text, font=rank_font) if rank_text else 0
        rank_x = rank_cfg.get("x") or max(bar_x, bar_x + bar_width - rank_width)
        rank_y = rank_cfg.get("y") or (bar_y - 24)
        draw.text((rank_x, rank_y), rank_text, font=rank_font, fill=primary_color)

    progress = (xp - xp_for_level(level)) / max(1, xp_for_level(level + 1) - xp_for_level(level))
    if bar_cfg["enabled"]:
        _draw_progress_bar(draw, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, primary_color, 7)

    next_level_xp = xp + xp_needed if xp_needed > 0 else xp_for_level(level + 1)
    ratio_cfg = cfg["elements"]["xp_ratio"]
    if ratio_cfg["enabled"]:
        ratio_x = ratio_cfg.get("x") or bar_x
        ratio_y = ratio_cfg.get("y") or (bar_y + bar_height + 8)
        draw.text(
            (ratio_x, ratio_y),
            f"{xp:,} / {next_level_xp:,} XP",
            font=tiny_font,
            fill=accent_color,
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
    "RANKS_DEFAULT_CONFIG",
    "_rank_cfg",
]


async def setup(bot):
    pass
