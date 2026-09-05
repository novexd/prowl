"""
# Ediscord builders
Fluent builder utilities for Discord components.

Contains:
- EmbedBuilder   – chainable embed construction
- ButtonBuilder  – action buttons with callbacks
- LinkBuilder    – URL buttons and styled hyperlinks
- ModalBuilder   – text-input modals

Usage example:
    from Ediscord.builders import EmbedBuilder, ButtonBuilder, ModalBuilder

    embed = (
        EmbedBuilder()
        .title("Hello!")
        .description("Welcome to the server.")
        .color("blue")
        .field("Info", "Some value", inline=True)
        .footer("Powered by Prowl")
        .build()
    )
"""

import discord
from discord.ext import commands
from typing import Optional, Callable, Any, List, Union
from Ediscord import variables


# ==================================================================================================
#                                        UNIFIED BRAND / SEMANTIC COLORS
# ==================================================================================================

# Prowl brand palette - every embed should use one of these.
BRAND   = 0x8B5CF6   # violet - default / neutral
SUCCESS = 0x22C55E   # green  - actions that succeeded
ERROR   = 0xEF4444   # red    - failures / denied
WARN    = 0xF59E0B   # amber  - cautions / warnings
INFO    = 0x3B82F6   # blue   - informational

# ==================================================================================================
#                                     EMBED / CONTENT EMOJIS
# ==================================================================================================

# EMBED_EMOJIS = the ORIGINAL developer-portal emoji set.
# Used EVERYWHERE except buttons: embed title prefixes (emoji_title), action messages,
# DM text, reactions, etc. These are the canonical Prowl emojis - do not replace them
# with the lucide set below.
EMBED_EMOJIS = {
    # ── Moderation ──
    "ban":        "<:ban:1538638670423392316>",
    "tempban":    "<:tempban:1538638690224578621>",
    "kick":       "<:kick:1538638679810252882>",
    "mute":       "<:mute:1538638685317374032>",
    "unmute":     "<:unmute:1538638696994050129>",
    "warn":       "<:warn:1538638702950223892>",
    "unban":      "<:unban:1538638695505076234>",
    "purge":      "<:purge:1538638686428602408>",
    "modlog":     "<:modlog:1538638684298158260>",
    "dm":         "<:dm:1538638671740149871>",
    "timeout":    "<:timeout:1538660560152043682>",
    "softban":    "<:softban:1538660541357232179>",
    "case":       "<:case:1538660436101169313>",
    "evidence":   "<:evidence:1538660463318007830>",
    # ── Leveling ──
    "level_up":   "<:level_up:1538638682427367454>",
    "rank":       "<:rank:1538638687611654225>",
    "leaderboard":"<:leaderboard:1538638681102090340>",
    "xp":         "<:xp:1538660575897329694>",
    "streak":     "<:streak:1538660546398916649>",
    "milestone":  "<:milestone:1538660508448591978>",
    "reward":     "<:reward:1538660528229056562>",
    # ── Welcomer ──
    "welcome":    "<:welcome:1538638704069972059>",
    "goodbye":    "<:goodbye:1538638674156064931>",
    "auto_role":  "<:auto_role:1538638669198528623>",
    "boost":      "<:boost:1538660428790370396>",
    # ── Tickets ──
    "ticket":       "<:ticket:1538638691399106661>",
    "ticket_open":  "<:ticket_open:1538638694439985254>",
    "ticket_close": "<:ticket_close:1538638692795682856>",
    "ticket_claim": "<:ticket_claim:1538660555588636837>",
    "ticket_reopen":"<:ticket_reopen:1538660558566334596>",
    # ── Verification ──
    "verify":         "<:verify:1538638698860511414>",
    "verify_fail":    "<:verify_fail:1538638700978896906>",
    "verify_pending": "<:verify_pending:1538660568267886642>",
    # ── Invite Tracker ──
    "invite_join":    "<:invite_join:1538638675389456564>",
    "invite_stats":   "<:invite_stats:1538638678190985246>",
    "invite_create":  "<:invite_create:1538660485652684910>",
    "invite_revoke":  "<:invite_revoke:1538660487422546001>",
    # ── Global Chat ──
    "global_chat":    "<:global_chat:1538638672876937256>",
    "global_msg":     "<:global_msg:1538660477838565406>",
    "global_linked":  "<:global_linked:1538660476593119372>",
    # ── Anti-Raid / Security ──
    "anti_raid":     "<:anti_raid:1538638667692646430>",
    "raid_detected": "<:raid_detected:1538660525020414072>",
    "raid_blocked":  "<:raid_blocked:1538660523669987489>",
    # ── Status / Feedback ──
    "success":  "<:success:1538660547791429694>",
    "error":    "<:error:1538660462068113438>",
    "info":     "<:info:1538660484352311357>",
    "warning":  "<:warning:1538660572386697236>",
    "pending":  "<:pending:1538660516908765184>",
    # ── UI / General ──
    "settings":  "<:settings:1538638688806903938>",
    "dashboard": "<:dashboard:1538660455365484544>",
    "analytics": "<:analytics:1538660411786924065>",
    "database":  "<:database:1538660457286602802>",
    "server":    "<:server:1538660536344911893>",
    "member":    "<:member:1538660501309890570>",
    "members":   "<:members:1538660504426512525>",
    "channel":   "<:channel:1538660437187371018>",
    "role":      "<:role:1538660530628202637>",
    "bot":       "<:bot:1538660430090739782>",
    "tag":         "<:tag:1538660550932693032>",
    "link":      "<:link:1538660494481690624>",
    "copy":      "<:copy:1538660449824931901>",
    "save":      "<:save:1538660531781505144>",
    "search":    "<:search:1538660534071853196>",
    "refresh":   "<:refresh:1538660527096598609>",
    "download":  "<:download:1538660460738642011>",
    "upload":    "<:upload:1538660565042331758>",
    "lock":      "<:lock:1538660495878258688>",
    "unlock":    "<:unlock:1538660562223898714>",
    "key":       "<:key:1538660489310249080>",
    "star":      "<:star:1538660544356294747>",
    "pin":       "<:pin:1538660519479742545>",
    "clock":     "<:clock:1538660440245272596>",
    "calendar":  "<:calendar:1538660433786052618>",
    "bell":      "<:bell:1538660422251450468>",
    "bell_off":  "<:bell_off:1538660423992344606>",
    "eye":       "<:eye:1538660464723107870>",
    "eye_off":   "<:eye_off:1538660465809432696>",
    "check":     "<:check:1538660438626017370>",
    "cross":     "<:cross:1538660452530393199>",
    "heart":     "<:heart:1538660480711921856>",
    "bolt":      "<:bolt:1538660426353614991>",
    "fire":      "<:fire:1538660467931881623>",
    "code":      "<:code:1538660442870653123>",
    "terminal":  "<:terminal:1538660552988037311>",
    "bug":       "<:bug:1538660431609208832>",
    "rocket":    "<:rocket:1538660529508323328>",
    "sparkle":   "<:sparkle:1538660542896672890>",
    "cloud":     "<:cloud:1538660441759289344>",
    "sun":       "<:sun:1538660549297053786>",
    "moon":      "<:moon:1538660510478639155>",
    "leaf":      "<:leaf:1538660492262772867>",
    "mountain":  "<:mountain:1538660511493783562>",
    "flag":      "<:flag:1538660469236175001>",
    "compass":   "<:compass:1538660447459221535>",
    "map":       "<:map:1538660498265084036>",
    "globe":     "<:globe:1538660478879014983>",
    "anchor":    "<:anchor:1538660413208793138>",
    "tag":       "<:tag:1538660550932693032>",
    "bookmark":  "<:bookmark:1538660427771154674>",
    "folder":    "<:folder:1538660471958278246>",
    "file":      "<:file:1538660466883035237>",
    "archive":   "<:archive:1538660416555589672>",
    "package":   "<:package:1538660515553878076>",
    "cpu":       "<:cpu:1538660451259514991>",
    "wifi":      "<:wifi:1538660574731313162>",
    "bluetooth": "<:bluetooth:1538660425195987145>",
    "power":     "<:power:1538660521652523199>",
    "music":     "<:music:1538660512789958656>",
    "image":     "<:image:1538660482221605027>",
    "video":     "<:video:1538660569417252945>",
    "camera":    "<:camera:1538660435073695975>",
    "mic":       "<:mic:1538660507018596523>",
    "phone":     "<:phone:1538660518162731078>",
    "mail":      "<:mail:1538660496947806209>",
    "message":   "<:message:1538660505827151943>",
    "send":      "<:send:1538660535317561404>",
    "inbox":     "<:inbox:1538660483291283516>",
    "shield":    "<:shield:1538660540266709193>",
    "scan":      "<:scan:1538660532977016842>",
    "atom":      "<:atom:1538660417679794276>",
    "dna":       "<:dna:1538660459425701938>",
    "flask":     "<:flask:1538660470393671840>",
    "award":     "<:award:1538660419483344957>",
    "crown":     "<:crown:1538660454052921364>",
    "gem":       "<:gem:1538660473174753360>",
    "coffee":    "<:coffee:1538660444217278584>",
    "cake":      "<:cake:1538660432787537921>",
    "pizza":     "<:pizza:1538660520482045972>",
    "cookie":    "<:cookie:1538660448725901363>",
    "gift":      "<:gift:1538660474407878698>",
}


# ==================================================================================================
#                                     BUTTON EMOJIS (lucide)
# ==================================================================================================

# BUTTON_EMOJIS = the white transparent lucide set hosted on the Prowl test/emoji guild
# (Absolute Testing Server). These are ONLY for buttons (e.g. under embeds) - they have a
# transparent background that looks clean on Discord's button surfaces. The bot must stay a
# member of the emoji guild or they break. Tier-0 guilds cap at 50 static emojis, so this
# dict intentionally holds exactly 50 keys.
# Regenerate PNGs: python whitebots.online\generate_emojis.py   Re-upload: upload_emojis.py
BUTTON_EMOJIS = {
    # ── Moderation / security ──
    "ban":              "<:ban:1540324596664901713>",
    "tempban":          "<:tempban:1540324599995047978>",
    "kick":             "<:kick:1540324603811864617>",
    "mute":             "<:mute:1540324607431676004>",
    "unmute":           "<:unmute:1540324610954891267>",
    "warn":             "<:warn:1540324615455506472>",
    "warning":          "<:warning:1540324618882257046>",
    "unban":            "<:unban:1540324623114043393>",
    "purge":            "<:purge:1540324626872270988>",
    "shield":           "<:shield:1540324643523530772>",
    "anti_raid":        "<:anti_raid:1540324647424360470>",
    "raid_detected":    "<:raid_detected:1540324651346169936>",

    # ── Status / feedback ──
    "success":    "<:success:1540324638733897738>",
    "error":      "<:error:1540324630722642041>",
    "info":       "<:info:1540324634476544000>",

    # ── Leveling ──
    "level_up":       "<:level_up:1540324655305466018>",
    "rank":           "<:rank:1540324659063423037>",
    "leaderboard":    "<:leaderboard:1540324662632784012>",

    # ── Welcomer / logging ──
    "welcome":     "<:welcome:1540324667255033908>",
    "goodbye":     "<:goodbye:1540324670728048702>",
    "member":      "<:member:1540324674955640895>",
    "members":     "<:members:1540324679259004978>",
    "server":      "<:server:1540324683071627375>",
    "channel":     "<:channel:1540324691137405019>",
    "role":        "<:role:1540324695717716091>",
    "message":     "<:message:1540324699341586523>",
    "mic":         "<:mic:1540324703271387247>",
    "bell":        "<:bell:1540324707126087720>",
    "bell_off":    "<:bell_off:1540324711211474944>",
    "sparkle":     "<:sparkle:1540324715497914418>",
    "bot":         "<:bot:1540324719260213370>",

    # ── Tickets / verification ──
    "ticket":          "<:ticket:1540324723085418576>",
    "ticket_open":     "<:ticket_open:1540324727095038073>",
    "ticket_close":    "<:ticket_close:1540324730769514576>",
    "verify":          "<:verify:1540324733952725054>",
    "verify_fail":     "<:verify_fail:1540324738260406312>",
    "lock":            "<:lock:1540324742312239195>",
    "unlock":          "<:unlock:1540324745797439610>",
    "check":           "<:check:1540324749589094486>",
    "cross":           "<:cross:1540324752609116161>",

    # ── Invites / social / misc ──
    "invite_join":      "<:invite_join:1540324756455428108>",
    "invite_stats":     "<:invite_stats:1540324760729157652>",
    "invite_create":    "<:invite_create:1540324764286189680>",
    "global_chat":      "<:global_chat:1540324768220454944>",
    "save":             "<:save:1540324772364161066>",
    "send":             "<:send:1540324775912669265>",
    "settings":         "<:settings:1540324780677275658>",
    "bolt":             "<:bolt:1540324784838152274>",
    "image":            "<:image:1540324789208490117>",
    "music":            "<:music:1540324792693956669>",

}


def emoji_for(key: str) -> str:
    """Return the emoji registered for an embed type ("" if none).

    Uses the canonical developer-portal set (EMBED_EMOJIS) - for embed titles,
    messages, reactions, etc.
    """
    return EMBED_EMOJIS.get(key, "")


def button_emoji(key: str) -> str:
    """Return the lucide/transparent emoji for a button ("" if none).

    Uses the dedicated BUTTON_EMOJIS set - only for buttons, never for embeds.
    """
    return BUTTON_EMOJIS.get(key, "")


def emoji_title(key: str, text: str) -> str:
    """Prefix *text* with the type's emoji, separated by two spaces."""
    emoji = emoji_for(key)
    if not emoji:
        return text
    return f"{emoji}  {text}"

_SEMANTIC = {
    "brand": BRAND, "violet": BRAND, "purple": BRAND,
    "success": SUCCESS, "green": SUCCESS, "ok": SUCCESS,
    "error": ERROR, "danger": ERROR, "red": ERROR,
    "warn": WARN, "warning": WARN, "yellow": WARN, "amber": WARN, "orange": WARN,
    "info": INFO, "blue": INFO, "blurple": 0x5865F2,
    "fire": 0xF97316,
    "pink": 0xF472B6,
    "gray": 0x6B7280, "grey": 0x6B7280,
}


# ==================================================================================================
#                                            EMBED BUILDER
# ==================================================================================================


class EmbedBuilder:
    """Fluent builder for :class:`discord.Embed`.

    All setter methods return ``self`` so calls can be chained.
    Call :meth:`build` to get the final ``discord.Embed``.

    Semantic shortcuts keep embeds consistent:
        EmbedBuilder().success("Member Muted")              # green, title only
        EmbedBuilder().error("Missing permissions")         # red, title only
        EmbedBuilder().info("Leaderboard").field("1.", "User")  # blue + fields
        EmbedBuilder().warn("Rate limited", "Try again later.")  # amber + description
    """

    def __init__(self):
        self._title: Optional[str] = None
        self._description: Optional[str] = None
        self._color: Optional[Union[int, discord.Color]] = None
        self._url: Optional[str] = None
        self._timestamp = None
        self._author: Optional[dict] = None
        self._footer: Optional[dict] = None
        self._image: Optional[str] = None
        self._thumbnail: Optional[str] = None
        self._fields: list = []

    # --- semantic shortcuts ---------------------------------------------------

    def success(self, title: str, description: str = None) -> "EmbedBuilder":
        return self.title(title).color("success").description(description or "")

    def error(self, title: str, description: str = None) -> "EmbedBuilder":
        return self.title(title).color("error").description(description or "")

    def warn(self, title: str, description: str = None) -> "EmbedBuilder":
        return self.title(title).color("warn").description(description or "")

    def info(self, title: str, description: str = None) -> "EmbedBuilder":
        return self.title(title).color("info").description(description or "")

    def brand(self, title: str, description: str = None) -> "EmbedBuilder":
        return self.title(title).color("brand").description(description or "")

    # --- core setters ----------------------------------------------------------

    def title(self, text: str) -> "EmbedBuilder":
        self._title = text[:256]
        return self

    def description(self, text: str) -> "EmbedBuilder":
        self._description = text[:4096] if text else None
        return self

    def color(self, value: Union[str, int, discord.Color]) -> "EmbedBuilder":
        if isinstance(value, str):
            resolved = _SEMANTIC.get(value.lower())
            if resolved is None:
                resolved = variables.COLOR_MAP.get(value.lower(), BRAND)
            self._color = discord.Color(resolved)
        elif isinstance(value, int):
            self._color = discord.Color(value)
        elif isinstance(value, discord.Color):
            self._color = value
        return self

    def hex_color(self, hex_str: str) -> "EmbedBuilder":
        hex_str = hex_str.lstrip("#")
        try:
            self._color = discord.Color(int(hex_str, 16))
        except ValueError:
            self._color = discord.Color.default()
        return self

    def url(self, url: str) -> "EmbedBuilder":
        self._url = url
        return self

    def timestamp(self, ts=None) -> "EmbedBuilder":
        """Set a timestamp. Defaults to *now* if no argument is given."""
        self._timestamp = ts
        return self

    # --- author / footer -------------------------------------------------------

    def author(self, name: str, url: str = None, icon_url: str = None) -> "EmbedBuilder":
        self._author = {"name": name, "url": url, "icon_url": icon_url}
        return self

    def footer(self, text: str, icon_url: str = None) -> "EmbedBuilder":
        self._footer = {"text": text, "icon_url": icon_url}
        return self

    # --- images ----------------------------------------------------------------

    def image(self, url: str) -> "EmbedBuilder":
        self._image = url
        return self

    def thumbnail(self, url: str) -> "EmbedBuilder":
        self._thumbnail = url
        return self

    # --- fields ----------------------------------------------------------------

    def field(self, name: str, value: str, inline: bool = False) -> "EmbedBuilder":
        self._fields.append({
            "name": name[:256],
            "value": value[:1024],
            "inline": inline,
        })
        return self

    def fields_from_dict(self, data: dict, inline: bool = False) -> "EmbedBuilder":
        for k, v in data.items():
            self.field(str(k), str(v), inline=inline)
        return self

    def clear_fields(self) -> "EmbedBuilder":
        self._fields.clear()
        return self

    def row(self, *fields, columns: int = 2) -> "EmbedBuilder":
        """Add a row of inline fields rendered in a ``columns``-wide grid (default 2).

        Each positional argument is a ``(name, value)`` tuple. Discord lays inline
        fields out left-to-right and wraps, so passing pairs (or any multiple of
        ``columns``) produces a clean grid of ``columns`` per row. A partial final
        row is padded with a blank zero-width inline field so columns stay aligned.

        Example::

            EmbedBuilder().title("Server Stats").row(
                ("Members", "1,204"),
                ("Boosts", "3"),
                ("Roles", "42"),
                ("Emojis", "18"),
            )  # -> two rows of two columns
        """
        if columns < 1:
            columns = 1
        for name, value in fields:
            self._fields.append({
                "name": str(name)[:256],
                "value": str(value)[:1024],
                "inline": True,
            })
        # pad the last row so columns remain aligned
        remainder = len(fields) % columns
        if remainder:
            for _ in range(columns - remainder):
                self._fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
        return self

    # --- build / send ----------------------------------------------------------

    def build(self) -> discord.Embed:
        embed = discord.Embed(
            title=self._title,
            description=self._description,
            color=self._color or discord.Color(BRAND),
            url=self._url,
            timestamp=self._timestamp,
        )
        if self._author:
            embed.set_author(**{k: v for k, v in self._author.items() if v is not None})
        if self._footer:
            embed.set_footer(**{k: v for k, v in self._footer.items() if v is not None})
        if self._image:
            embed.set_image(url=self._image)
        if self._thumbnail:
            embed.set_thumbnail(url=self._thumbnail)
        for f in self._fields:
            embed.add_field(**f)
        # Discord rejects embeds with no content at all
        if not (embed.title or embed.description or embed.fields or embed.author or embed.footer or embed.image or embed.thumbnail):
            embed.description = "\u200b"
        return embed

    async def send(self, channel, content: str = None, **kwargs) -> discord.Message:
        """Build and send the embed to *channel*."""
        return await channel.send(content=content, embed=self.build(), **kwargs)

    async def respond(self, interaction: discord.Interaction, content: str = None, **kwargs):
        """Build and respond to an interaction."""
        return await interaction.response.send_message(
            content=content, embed=self.build(), **kwargs
        )


# ==================================================================================================
#                                           BUTTON BUILDER
# ==================================================================================================


class ButtonView(discord.ui.View):
    """A ``View`` that holds one or more buttons created by :class:`ButtonBuilder`."""

    def __init__(self, buttons: list, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        for btn in buttons:
            self.add_item(btn)


class ButtonBuilder:
    """Fluent builder for :class:`discord.ui.Button`.

    Call :meth:`build` for a single ``Button``, or :meth:`view` / :meth:`send`
    to wrap everything in a ``View`` and ship it.
    """

    STYLES = {
        "primary":   discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success":   discord.ButtonStyle.success,
        "danger":    discord.ButtonStyle.danger,
        "link":      discord.ButtonStyle.link,
    }

    def __init__(self):
        self._label: Optional[str] = None
        self._style: discord.ButtonStyle = discord.ButtonStyle.primary
        self._emoji: Optional[str] = None
        self._custom_id: Optional[str] = None
        self._url: Optional[str] = None
        self._disabled: bool = False
        self._row: Optional[int] = None
        self._callback: Optional[Callable] = None

    # --- setters ---------------------------------------------------------------

    def label(self, text: str) -> "ButtonBuilder":
        self._label = text[:80]
        return self

    def style(self, value: str) -> "ButtonBuilder":
        self._style = self.STYLES.get(value.lower(), discord.ButtonStyle.primary)
        return self

    def emoji(self, value: str) -> "ButtonBuilder":
        self._emoji = value
        return self

    def custom_id(self, cid: str) -> "ButtonBuilder":
        self._custom_id = cid
        return self

    def url(self, url: str) -> "ButtonBuilder":
        self._url = url
        self._style = discord.ButtonStyle.link
        return self

    def disabled(self, value: bool = True) -> "ButtonBuilder":
        self._disabled = value
        return self

    def row(self, r: int) -> "ButtonBuilder":
        self._row = r
        return self

    def on_click(self, callback: Callable) -> "ButtonBuilder":
        """Register an ``async def callback(interaction)`` for this button."""
        self._callback = callback
        return self

    # --- build / send ----------------------------------------------------------

    def build(self) -> discord.ui.Button:
        kwargs = {
            "label": self._label,
            "style": self._style,
            "disabled": self._disabled,
        }
        if self._emoji:
            kwargs["emoji"] = self._emoji
        if self._url:
            kwargs["url"] = self._url
        elif self._custom_id:
            kwargs["custom_id"] = self._custom_id
        if self._row is not None:
            kwargs["row"] = self._row
        return discord.ui.Button(**kwargs)

    def view(self, *, timeout: float = 180.0) -> ButtonView:
        """Wrap the button in a :class:`ButtonView`."""
        v = ButtonView([self.build()], timeout=timeout)
        if self._callback:
            btn = v.children[0]
            btn.callback = self._callback
        return v

    async def send(self, channel, content: str = None, **kwargs) -> discord.Message:
        """Build a view and send it."""
        return await channel.send(content=content, view=self.view(), **kwargs)


def button_row(*builders: "ButtonBuilder", timeout: float = 180.0) -> ButtonView:
    """Create a ``ButtonView`` from multiple :class:`ButtonBuilder` instances."""
    view = ButtonView([b.build() for b in builders], timeout=timeout)
    for b, item in zip(builders, view.children):
        if b._callback:
            item.callback = b._callback
    return view


# ==================================================================================================
#                                            LINK BUILDER
# ==================================================================================================


class LinkBuilder:
    """Fluent builder for URL-based buttons and styled hyperlink embeds.

    Use :meth:`button` for a ``discord.ui.Button`` (link style),
    or :meth:`embed` to create an embed that highlights the link.
    """

    def __init__(self):
        self._url: str = ""
        self._label: Optional[str] = None
        self._emoji: Optional[str] = None
        self._description: Optional[str] = None
        self._color: Union[str, int, discord.Color] = "blurple"

    # --- setters ---------------------------------------------------------------

    def url(self, url: str) -> "LinkBuilder":
        self._url = url
        return self

    def label(self, text: str) -> "LinkBuilder":
        self._label = text[:80]
        return self

    def emoji(self, value: str) -> "LinkBuilder":
        self._emoji = value
        return self

    def description(self, text: str) -> "LinkBuilder":
        self._description = text
        return self

    def color(self, value: Union[str, int, discord.Color]) -> "LinkBuilder":
        self._color = value
        return self

    # --- build -----------------------------------------------------------------

    def button(self) -> discord.ui.Button:
        """Return a link-style ``Button``."""
        kwargs = {
            "style": discord.ButtonStyle.link,
            "url": self._url,
            "label": self._label,
        }
        if self._emoji:
            kwargs["emoji"] = self._emoji
        return discord.ui.Button(**{k: v for k, v in kwargs.items() if v is not None})

    def embed(self) -> discord.Embed:
        """Return an ``Embed`` that displays the link with a description."""
        eb = EmbedBuilder()
        if self._label:
            eb.title(self._label)
        if self._description:
            eb.description(self._description)
        eb.color(self._color)
        eb.field("Link", f"[Click here]({self._url})", inline=False)
        return eb.build()

    def view(self, *, timeout: float = 180.0) -> ButtonView:
        """Wrap the link button in a ``ButtonView``."""
        return ButtonView([self.button()], timeout=timeout)

    async def send(self, channel, content: str = None, **kwargs) -> discord.Message:
        """Send the link button in a view."""
        return await channel.send(content=content, view=self.view(), **kwargs)


# ==================================================================================================
#                                           MODAL BUILDER
# ==================================================================================================


class _ModalInput:
    """Internal descriptor for a single text input row."""

    def __init__(
        self,
        custom_id: str,
        label: str,
        *,
        style: str = "short",
        placeholder: str = None,
        default: str = None,
        required: bool = True,
        min_length: int = None,
        max_length: int = None,
    ):
        self.custom_id = custom_id
        self.label = label
        self.style = style
        self.placeholder = placeholder
        self.default = default
        self.required = required
        self.min_length = min_length
        self.max_length = max_length

    def to_input(self) -> discord.ui.TextInput:
        style_map = {
            "short": discord.TextStyle.short,
            "paragraph": discord.TextStyle.paragraph,
        }
        kwargs = {
            "custom_id": self.custom_id,
            "label": self.label,
            "style": style_map.get(self.style, discord.TextStyle.short),
            "required": self.required,
        }
        if self.placeholder:
            kwargs["placeholder"] = self.placeholder
        if self.default:
            kwargs["default"] = self.default
        if self.min_length is not None:
            kwargs["min_length"] = self.min_length
        if self.max_length is not None:
            kwargs["max_length"] = self.max_length
        return discord.ui.TextInput(**kwargs)


class ModalView(discord.ui.Modal):
    """A ``Modal`` generated by :class:`ModalBuilder`."""

    def __init__(self, title: str, inputs: list, *, on_submit: Callable = None):
        super().__init__(title=title)
        self._on_submit = on_submit
        for inp in inputs:
            self.add_item(inp.to_input())

    async def on_submit(self, interaction: discord.Interaction):
        if self._on_submit:
            await self._on_submit(interaction, self)
        else:
            await interaction.response.send_message("Submitted!", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(
            f"Something went wrong: {error}", ephemeral=True
        )


class ModalBuilder:
    """Fluent builder for :class:`discord.ui.Modal`.

    Call :meth:`add_input` to define text-input rows, then :meth:`build` or
    :meth:`send` / :meth:`respond`.
    """

    def __init__(self):
        self._title: str = "Modal"
        self._inputs: List[_ModalInput] = []
        self._on_submit: Optional[Callable] = None

    # --- setters ---------------------------------------------------------------

    def title(self, text: str) -> "ModalBuilder":
        self._title = text[:45]
        return self

    def add_input(
        self,
        custom_id: str,
        label: str,
        *,
        style: str = "short",
        placeholder: str = None,
        default: str = None,
        required: bool = True,
        min_length: int = None,
        max_length: int = None,
    ) -> "ModalBuilder":
        """Append a text-input row (max 5)."""
        if len(self._inputs) >= 5:
            raise ValueError("A modal can have at most 5 text inputs.")
        self._inputs.append(_ModalInput(
            custom_id, label,
            style=style, placeholder=placeholder, default=default,
            required=required, min_length=min_length, max_length=max_length,
        ))
        return self

    def on_submit(self, callback: Callable) -> "ModalBuilder":
        """Set an ``async def callback(interaction, modal)`` handler."""
        self._on_submit = callback
        return self

    # --- build / send ----------------------------------------------------------

    def build(self) -> ModalView:
        return ModalView(self._title, self._inputs, on_submit=self._on_submit)

    async def send(self, interaction: discord.Interaction):
        """Respond to an interaction by showing this modal."""
        await interaction.response.send_modal(self.build())

    async def respond(self, interaction: discord.Interaction):
        """Alias for :meth:`send`."""
        await self.send(interaction)


# ==================================================================================================
#                                          CONVENIENCE ALIASES
# ==================================================================================================


def quick_embed(title: str, description: str = None, color: str = "brand") -> discord.Embed:
    """One-liner embed shortcut (title-first, optional description)."""
    eb = EmbedBuilder()
    getattr(eb, color if color in ("success", "error", "warn", "info", "brand") else "brand")(title, description)
    return eb.build()


def success_embed(title: str, description: str = None) -> discord.Embed:
    """Green success embed - title only unless a description is given."""
    return EmbedBuilder().success(title, description).build()


def error_embed(title: str, description: str = None) -> discord.Embed:
    """Red error embed."""
    return EmbedBuilder().error(title, description).build()


def info_embed(title: str, description: str = None) -> discord.Embed:
    """Blue info embed."""
    return EmbedBuilder().info(title, description).build()


def embed_from_dict(data: dict) -> discord.Embed:
    """Build a :class:`discord.Embed` from a dashboard-configured dict.

    Shared across every cog that renders dashboard embeds (moderation actions,
    leveling, welcome/goodbye, tickets, ...).
    """
    if not isinstance(data, dict):
        data = {}
    color = data.get("color")
    try:
        color = int(str(color).lstrip("#"), 16) if color else BRAND
    except (ValueError, TypeError):
        color = BRAND
    embed = discord.Embed(
        title=data.get("title") or None,
        description=data.get("description") or None,
        color=color,
    )
    if data.get("url"):
        embed.url = data["url"]
    if data.get("author_name"):
        embed.set_author(name=data["author_name"], url=data.get("author_url") or None, icon_url=data.get("author_icon") or None)
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    if data.get("thumbnail_url"):
        embed.set_thumbnail(url=data["thumbnail_url"])
    if data.get("footer_text") or data.get("footer_icon"):
        embed.set_footer(text=data.get("footer_text") or "", icon_url=data.get("footer_icon") or None)
    for f in (data.get("fields") or []):
        if isinstance(f, dict) and f.get("name"):
            embed.add_field(name=f["name"][:256], value=(f.get("value") or "\u200b")[:1024], inline=bool(f.get("inline")))
    # Discord rejects embeds with no content at all - fall back so the action
    # still works even when a custom embed is configured but empty.
    if not (embed.title or embed.description or embed.fields or embed.author or embed.footer or embed.image or embed.thumbnail):
        embed.title = "Action Completed"
        embed.description = "\u200b"
    return embed


def basic_action_embed(key: str, message: str, color: str = "brand") -> discord.Embed:
    """Basic-mode action embed: emoji + two spaces + message as the title (no fields)."""
    return EmbedBuilder().title(emoji_title(key, message)).color(color).build()
