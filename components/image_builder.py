from __future__ import annotations

import hashlib
import io
import json
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit
from PIL import Image, ImageDraw, ImageFont, ImageOps

import aiohttp
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


GRADIENT_DIRECTIONS = ("horizontal", "vertical", "diagonal", "radial")


def _clamp_channel(v, default=255):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(0, min(255, n))


def _sanitize_rgba_list(v, default=(255, 255, 255, 255)):
    """Normalize a user-supplied color to [r, g, b, a] (alpha kept)."""
    if isinstance(v, (list, tuple)) and 3 <= len(v) <= 4:
        try:
            out = [_clamp_channel(v[0]), _clamp_channel(v[1]), _clamp_channel(v[2])]
            out.append(_clamp_channel(v[3], 255) if len(v) > 3 else 255)
            return out
        except Exception:
            pass
    return list(default)


def _sanitize_gradient(v):
    """Normalize a user-supplied gradient to
    {"direction": ..., "colors": [[r,g,b,a], ...]} or return None."""
    if not isinstance(v, dict):
        return None
    direction = v.get("direction")
    if direction not in GRADIENT_DIRECTIONS:
        return None
    raw = v.get("colors", v.get("stops"))
    if not isinstance(raw, (list, tuple)) or not (2 <= len(raw) <= 6):
        return None
    colors = []
    for c in raw:
        if not isinstance(c, (list, tuple)) or not (3 <= len(c) <= 4):
            return None
        colors.append(_sanitize_rgba_list(c, (255, 255, 255, 255)))
    return {"direction": direction, "colors": colors}


def _color_spec(v, default=(255, 255, 255, 255)):
    """A color slot is either ("solid", (r,g,b,a)) or ("gradient", {...})."""
    if isinstance(v, dict):
        g = _sanitize_gradient(v)
        if g is not None:
            return ("gradient", g)
    return ("solid", tuple(_sanitize_rgba_list(v, default)))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_color(c1, c2, t):
    return tuple(int(round(_lerp(x, y, t))) for x, y in zip(c1, c2))


def _stops_at(stops, t):
    """Evenly-spaced multi-stop interpolation; stops are (r,g,b,a) tuples."""
    if t <= 0:
        return tuple(stops[0])
    if t >= 1:
        return tuple(stops[-1])
    n = len(stops) - 1
    pos = t * n
    i = min(int(pos), n - 1)
    return _lerp_color(stops[i], stops[i + 1], pos - i)


def _gradient_image(w, h, direction, colors):
    """Rasterize a gradient. Rendered small then upscaled (BICUBIC) so even
    the 900x300 card background costs ~65k pixel ops, not 270k."""
    w = max(1, int(w))
    h = max(1, int(h))
    stops = [tuple(c) for c in colors]
    if direction == "horizontal":
        ww, hh = 256, 1
        pos = lambda x, y: x / 255.0
    elif direction == "vertical":
        ww, hh = 1, 256
        pos = lambda x, y: y / 255.0
    elif direction == "radial":
        ww, hh = 256, 256
        pos = lambda x, y: min(1.0, (((x / 255.0 - 0.5) ** 2 + (y / 255.0 - 0.5) ** 2) ** 0.5 * 1.41421356))
    else:  # diagonal: top-left -> bottom-right
        ww, hh = 256, 256
        pos = lambda x, y: (x + y) / 510.0
    small = Image.new("RGBA", (ww, hh))
    px = small.load()
    for y in range(hh):
        for x in range(ww):
            px[x, y] = _stops_at(stops, pos(x, y))
    if (ww, hh) == (w, h):
        return small
    return small.resize((w, h), Image.Resampling.BICUBIC)


def _fill_image(size, spec):
    """Materialize a color spec into an RGBA image of the given size."""
    kind, val = spec
    if kind == "gradient":
        return _gradient_image(size[0], size[1], val["direction"], val["colors"])
    return Image.new("RGBA", (int(size[0]), int(size[1])), val)


# Default fill color source per element (panel/avatar draw no colored content).
ELEMENT_COLOR_DEFAULTS = {
    "panel": None,
    "avatar": None,
    "name": "accent",
    "xp_value": "accent",
    "rank": "primary",
    "xp_bar": "primary",
    "xp_ratio": "accent",
}

# Flat dark fill used when a legacy `background: null` (old Solid button) is
# loaded. The old button promised solid but rendered random; null now maps to
# the solid it always claimed to be.
LEGACY_SOLID_BG = (25, 25, 35, 255)


def _sanitize_background(value):
    """Normalize the background slot.

    Returns "random", a manifest-id/legacy string, None (no image: flat
    base), or ("solid", rgba) / ("gradient", {...}) tuples.
    """
    if value is None:
        return ("solid", LEGACY_SOLID_BG)
    if isinstance(value, dict):
        mode = value.get("mode")
        if mode == "solid":
            return ("solid", tuple(_sanitize_rgba_list(value.get("color"), LEGACY_SOLID_BG)))
        if mode == "gradient":
            g = _sanitize_gradient(value)
            if g is not None:
                return ("gradient", g)
        return "random"
    if isinstance(value, str):
        return value or "random"
    return "random"


def _rank_cfg(config):
    """Normalize a persisted rank_card config, filling from RANKS_DEFAULT_CONFIG."""
    if not isinstance(config, dict):
        config = {}
    base = RANKS_DEFAULT_CONFIG
    merged = {
        "background": _sanitize_background(config.get("background", "random")),
        "primary_color": _color_spec(config.get("primary_color")),
        "accent_color": _color_spec(config.get("accent_color")),
        "panel_color": _color_spec(config.get("panel_color"), (10, 10, 16, 170)),
        "elements": {},
    }
    defaults = base["elements"]
    user_elems = config.get("elements") or {}
    if not isinstance(user_elems, dict):
        user_elems = {}
    for name, defs in defaults.items():
        user = user_elems.get(name) or {}
        el = {**defs, **user}
        want = ELEMENT_COLOR_DEFAULTS.get(name)
        el["color"] = el.get("color") if el.get("color") in ("primary", "accent") else want
        merged["elements"][name] = el
    return merged

STATIC_DIR = Path(__file__).resolve().parents[1] / "data" / "static"
FONT_DIR = STATIC_DIR / "fonts"
BACKGROUND_DIR = STATIC_DIR / "img"
BACKGROUND_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif",
}

# URL manifest for rank card backgrounds. The manifest itself is a tiny tracked
# JSON file (website/static/backgrounds.json: [{id, url, thumb?}]); bulk image
# bytes are NEVER committed and live at external URLs instead. The bot
# downloads each URL once and serves renders from a local disk cache.
BACKGROUND_MANIFEST_URL = os.environ.get(
    "RANK_BG_MANIFEST_URL", "https://prowlbot.xyz/static/backgrounds.json"
)
BACKGROUND_MANIFEST_TTL = 600
BACKGROUND_MAX_BYTES = 80 * 1024 * 1024

_manifest_cache = {"entries": None, "at": 0.0}


def _manifest_file() -> Optional[Path]:
    """Local manifest checkout (dev repo layout, incl. website tree).

    Also honors the deploy tree, where the release workflow ships a copy at
    data/static/backgrounds.json so validation/rendering never depend on the
    bot reaching the website over HTTP (this was the background-502 cause).
    """
    static_copy = STATIC_DIR / "backgrounds.json"
    try:
        if static_copy.is_file():
            return static_copy
    except OSError:
        pass
    here = Path(__file__).resolve()
    for cand in (
        here.parents[2] / "website" / "static" / "backgrounds.json",
        here.parents[1] / "website" / "static" / "backgrounds.json",
    ):
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _parse_manifest_entries(raw) -> list:
    try:
        items = raw.get("backgrounds") if isinstance(raw, dict) else None
    except AttributeError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("id") and it.get("url"):
            out.append({"id": str(it["id"]), "url": str(it["url"])})
    return out


async def _load_manifest_entries() -> list:
    """Manifest entries: local file first, remote fallback, memory-cached."""
    now = time.monotonic()
    if _manifest_cache["entries"] is not None and now - _manifest_cache["at"] < BACKGROUND_MANIFEST_TTL:
        return _manifest_cache["entries"]
    entries: list = []
    mf = _manifest_file()
    if mf is not None:
        try:
            entries = _parse_manifest_entries(json.loads(mf.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"Failed to read background manifest: {e}")
    if not entries:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(BACKGROUND_MANIFEST_URL, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status == 200:
                        entries = _parse_manifest_entries(await resp.json())
        except Exception as e:
            logger.warning(f"Failed to fetch background manifest: {e}")
    _manifest_cache["entries"] = entries
    _manifest_cache["at"] = now
    return entries


def _manifest_url_for(background_id: str) -> Optional[str]:
    for e in _manifest_cache.get("entries") or []:
        if e["id"] == background_id:
            return e["url"]
    return None


def _bg_cache_dir() -> Path:
    for cand in (STATIC_DIR / "cache" / "bg", Path(tempfile.gettempdir()) / "prowl_bg"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def _cache_name_for_url(url: str) -> str:
    try:
        ext = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        ext = "." + ext if ("." + ext) in BACKGROUND_EXTENSIONS else ".png"
    except Exception:
        ext = ".png"
    return hashlib.sha1(url.encode("utf-8")).hexdigest() + ext


async def _open_background_url(url: str) -> Optional[Image.Image]:
    """Download an image URL once, then serve from local disk cache."""
    try:
        path = _bg_cache_dir() / _cache_name_for_url(url)
        if not path.is_file():
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "ProwlBot/1.0"}) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
            if len(data) > BACKGROUND_MAX_BYTES:
                logger.warning(f"Background too large, refusing: {url}")
                return None
            try:
                path.write_bytes(data)
            except OSError as e:
                logger.warning(f"Background cache write failed: {e}")
        return _open_background(path)
    except Exception as e:
        logger.warning(f"Failed to load background URL: {e}")
        return None

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


def _draw_progress_bar_gradient(
    img: Image.Image,
    x: int,
    y: int,
    width: int,
    height: int,
    progress: float,
    bg_color: Tuple[int, int, int, int],
    grad: dict,
    radius: int = 8,
) -> ImageDraw.ImageDraw:
    """Progress bar whose fill is clipped through a gradient spec."""
    draw = ImageDraw.Draw(img)
    _draw_rounded_rect(draw, (x, y, x + width, y + height), radius, bg_color)
    if progress > 0:
        fill_width = int(width * min(1.0, max(0.0, progress)))
        if fill_width > 0:
            mask = Image.new("L", (fill_width, height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, fill_width, height), radius=radius, fill=255
            )
            fill = _gradient_image(fill_width, height, grad["direction"], grad["colors"])
            img.paste(fill, (x, y), mask)
    return ImageDraw.Draw(img)


def _draw_text(img, xy, text, font, spec):
    """Draw text filled with a solid color or clipped through a gradient.

    Returns a fresh ImageDraw for the (possibly pasted-upon) image.
    """
    draw = ImageDraw.Draw(img)
    if not text:
        return draw
    kind, val = spec
    if kind != "gradient":
        draw.text(xy, text, font=font, fill=val)
        return draw
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
    except Exception:
        draw.text(xy, text, font=font, fill=(255, 255, 255, 255))
        return draw
    w, h = int(bbox[2]), int(bbox[3])
    if w <= 0 or h <= 0:
        return draw
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=255)
    fill = _gradient_image(w, h, val["direction"], val["colors"])
    img.paste(fill, (int(xy[0]), int(xy[1])), mask)
    return ImageDraw.Draw(img)


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


async def _prepare_background(
    background: Optional[object],
    random_background: bool = False,
) -> Optional[Image.Image]:
    """Resolve a background to a card-fitted image.

    ``background`` may be a PIL image, a manifest id, a legacy local
    filename inside BACKGROUND_DIR, an absolute local path, or a raw
    http(s) URL (kept working for configs saved via the old free-text
    field). ``random_background`` picks a random manifest entry first,
    falling back to the legacy local directory.
    """
    if background is None and not random_background:
        return None
    try:
        entries = await _load_manifest_entries()
    except Exception:
        entries = []
    source: Optional[Image.Image] = None
    if random_background:
        if entries:
            source = await _open_background_url(random.choice(entries)["url"])
        if source is None:
            paths = _get_background_paths()
            if paths:
                source = _open_background(random.choice(paths))
    elif isinstance(background, Image.Image):
        source = background.convert("RGBA")
    elif isinstance(background, (str, Path)):
        name = str(background)
        url = _manifest_url_for(name)
        if url:
            source = await _open_background_url(url)
        if source is None:
            source = _open_background_name(name)
        if source is None and name.startswith(("http://", "https://")):
            source = await _open_background_url(name)
    else:
        try:
            source = background.convert("RGBA")
        except Exception:
            source = None
    if source is None:
        return None
    return ImageOps.fit(
        source,
        (CARD_WIDTH, CARD_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _open_background_name(name: str) -> Optional[Image.Image]:
    """Open a legacy local background: filename inside BACKGROUND_DIR
    (containment-checked) or an absolute local path."""
    try:
        cand = Path(name)
        if not cand.is_absolute():
            base = BACKGROUND_DIR.resolve()
            cand = (BACKGROUND_DIR / name).resolve()
            try:
                cand.relative_to(base)
            except ValueError:
                return None
        if cand.is_file() and cand.suffix.lower() in BACKGROUND_EXTENSIONS:
            return _open_background(cand)
    except OSError:
        pass
    return None


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


LU_WIDTH = 900
LU_HEIGHT = 360
LU_AVATAR = 150


async def create_level_up_card(
    user: discord.Member | discord.User,
    level: int,
    xp: int,
    xp_needed: int,
    *,
    guild_name: str = "Server",
    background: Optional[Image.Image] = None,
    primary_color: Tuple[int, int, int, int] = PRIMARY_COLOR,
    accent_color: Tuple[int, int, int, int] = WHITE,
) -> io.BytesIO:
    """Create a level up celebration card.

    Fixed (non-editable) layout matching the rank card aesthetic:
    dark base with a brand glow, avatar + name + progress on the left,
    giant level numeral on the right.

    Returns BytesIO ready for discord.File()
    """
    img = Image.new("RGBA", (LU_WIDTH, LU_HEIGHT), DEFAULT_BG_COLOR)
    glow = _gradient_image(
        LU_WIDTH, LU_HEIGHT, "radial",
        [PRIMARY_COLOR[:3] + (72,), PRIMARY_COLOR[:3] + (0,)],
    )
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    avatar_img = None
    if isinstance(user, (discord.Member, discord.User)):
        avatar_img = await _fetch_avatar(user.display_avatar, 256)
    if avatar_img is None:
        try:
            uid = int(getattr(user, "id", 0))
        except (TypeError, ValueError):
            uid = 0
        avatar_img = _get_default_avatar(uid)
    if avatar_img.size != (LU_AVATAR, LU_AVATAR):
        avatar_img = avatar_img.resize((LU_AVATAR, LU_AVATAR), Image.Resampling.LANCZOS)
    mask = _create_avatar_mask(LU_AVATAR)
    avatar_output = Image.new("RGBA", (LU_AVATAR, LU_AVATAR), (0, 0, 0, 0))
    avatar_output.paste(avatar_img, (0, 0), mask)

    avatar_x, avatar_y = 48, (LU_HEIGHT - LU_AVATAR) // 2
    img.paste(avatar_output, (avatar_x, avatar_y), avatar_output)
    draw.ellipse(
        (avatar_x - 4, avatar_y - 4, avatar_x + LU_AVATAR + 4, avatar_y + LU_AVATAR + 4),
        outline=WHITE,
        width=3,
    )

    eyebrow_font = _load_font(30, bold=True)
    name_font = _load_font(44, bold=True)
    small_font = _load_font(20)
    tiny_font = _load_font(17)
    giant_font = _load_font(150, bold=True)

    text_x = avatar_x + LU_AVATAR + 32
    # Reserve the right third for the giant numeral.
    num_zone_x = 640
    draw.text((text_x, 56), "LEVEL UP", font=eyebrow_font, fill=primary_color)

    display_name = str(getattr(user, "display_name", "") or str(user))
    max_name_width = num_zone_x - text_x - 16
    name = _truncate_to_width(draw, display_name, name_font, max_name_width)
    draw.text((text_x, 96), name, font=name_font, fill=accent_color)

    progress = (xp - xp_for_level(level)) / max(1, xp_for_level(level + 1) - xp_for_level(level)) if xp_needed > 0 else 0
    bar_x, bar_y = text_x, 196
    bar_width, bar_height = num_zone_x - text_x - 16, 16
    _draw_progress_bar(draw, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, primary_color, 8)
    draw.text(
        (bar_x, bar_y + bar_height + 10),
        f"{xp:,} XP  •  {xp_needed:,} XP to Level {level + 1}",
        font=small_font, fill=MUTED_COLOR,
    )
    draw.text((text_x, LU_HEIGHT - 44), f"Server: {guild_name}"[:48], font=tiny_font, fill=MUTED_COLOR)

    # Giant level numeral, right-aligned in its zone.
    num_text = str(level)
    try:
        num_w = draw.textlength(num_text, font=giant_font)
    except Exception:
        num_w = 0
    num_x = LU_WIDTH - 48 - num_w
    draw.text((num_x, 84), num_text, font=giant_font, fill=accent_color)
    try:
        label = "LEVEL"
        label_w = draw.textlength(label, font=eyebrow_font)
    except Exception:
        label_w = 0
    draw.text((LU_WIDTH - 48 - label_w, 64), label, font=eyebrow_font, fill=primary_color)

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
    png_optimize: bool = True,
) -> io.BytesIO:
    """
    Create a rank card showing user's position, level, and XP progress.

    Returns BytesIO ready for discord.File()

    ``config`` is a persisted ``rank_card`` settings dict (see ``_rank_cfg``)
    controlling background, colors, and element visibility/positions.
    ``png_optimize`` skips PNG optimizer passes for latency-sensitive
    previews (smaller CPU cost, slightly larger bytes).
    """
    cfg = _rank_cfg(config)
    primary_color = cfg["primary_color"]
    accent_color = cfg["accent_color"]

    def _el_spec(name):
        src = cfg["elements"][name].get("color")
        return primary_color if src == "primary" else accent_color

    img = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), DEFAULT_BG_COLOR)
    draw = ImageDraw.Draw(img)

    bg_val = cfg["background"]
    if isinstance(bg_val, tuple):
        background_image = _fill_image((CARD_WIDTH, CARD_HEIGHT), bg_val)
    else:
        background_image = await _prepare_background(
            background,
            random_background=bg_val == "random",
        )
        if background_image is None and bg_val != "random" and isinstance(bg_val, str):
            background_image = await _prepare_background(bg_val, random_background=False)
    if background_image is not None:
        overlay = Image.new("RGBA", background_image.size, (0, 0, 0, 110))
        img = Image.alpha_composite(background_image, overlay)
        draw = ImageDraw.Draw(img)

    panel_x = AVATAR_PADDING
    panel_y = AVATAR_PADDING
    panel_w = CARD_WIDTH - 2 * AVATAR_PADDING
    panel_h = CARD_HEIGHT - 2 * AVATAR_PADDING
    if cfg["elements"]["panel"]["enabled"]:
        panel_spec = cfg["panel_color"]
        if panel_spec[0] == "gradient":
            pmask = Image.new("L", (panel_w, panel_h), 0)
            ImageDraw.Draw(pmask).rounded_rectangle((0, 0, panel_w, panel_h), radius=16, fill=255)
            pfill = _gradient_image(panel_w, panel_h, panel_spec[1]["direction"], panel_spec[1]["colors"])
            img.paste(pfill, (panel_x, panel_y), pmask)
            draw = ImageDraw.Draw(img)
        else:
            _draw_rounded_rect(draw, (panel_x, panel_y, panel_x + panel_w, panel_y + panel_h), 16, panel_spec[1])

    avatar_img = None
    try:
        asset = getattr(user, "display_avatar", None)
        if asset is not None and hasattr(asset, "read"):
            avatar_img = await _fetch_avatar(asset, AVATAR_SIZE)
    except Exception as e:
        logger.warning(f"Failed to fetch avatar: {e}")
    if avatar_img is None:
        try:
            uid = int(getattr(user, "id", 0))
        except (TypeError, ValueError):
            uid = 0
        avatar_img = _get_default_avatar(uid)

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
    level_suffix = f"- Level {level}"
    level_width = draw.textlength(level_suffix, font=name_font)

    name_elems = cfg["elements"]["name"]
    if name_elems["enabled"]:
        name_x = name_elems.get("x") or text_x
        name_y = name_elems.get("y") or text_y
        max_name_width = panel_x + panel_w - name_x - 20 - level_width - 12
        name = _truncate_to_width(draw, display_name, name_font, max_name_width)
        label = f"{name} {level_suffix}" if name else level_suffix
        draw = _draw_text(img, (name_x, name_y), label, name_font, _el_spec("name"))

    xp_value_elems = cfg["elements"]["xp_value"]
    if xp_value_elems["enabled"]:
        xp_x = xp_value_elems.get("x") or text_x
        xp_y = xp_value_elems.get("y") or (text_y + 42)
        draw = _draw_text(img, (xp_x, xp_y), f"{xp:,} XP", small_font, _el_spec("xp_value"))

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
        draw = _draw_text(img, (rank_x, rank_y), rank_text, rank_font, _el_spec("rank"))

    progress = (xp - xp_for_level(level)) / max(1, xp_for_level(level + 1) - xp_for_level(level))
    if bar_cfg["enabled"]:
        bar_spec = _el_spec("xp_bar")
        if bar_spec[0] == "gradient":
            draw = _draw_progress_bar_gradient(
                img, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, bar_spec[1], 7
            )
        else:
            _draw_progress_bar(draw, bar_x, bar_y, bar_width, bar_height, progress, PROGRESS_BG, bar_spec[1], 7)

    next_level_xp = xp + xp_needed if xp_needed > 0 else xp_for_level(level + 1)
    ratio_cfg = cfg["elements"]["xp_ratio"]
    if ratio_cfg["enabled"]:
        ratio_x = ratio_cfg.get("x") or bar_x
        ratio_y = ratio_cfg.get("y") or (bar_y + bar_height + 8)
        draw = _draw_text(
            img,
            (ratio_x, ratio_y),
            f"{xp:,} / {next_level_xp:,} XP",
            tiny_font,
            _el_spec("xp_ratio"),
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=png_optimize)
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
