import discord
from discord.ext import commands
from discord import app_commands
import json
import random
import string
import os
import datetime
from typing import Optional

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title, EMBED_EMOJIS, BUTTON_EMOJIS


SITE_URL = os.environ.get("SITE_URL") or "https://prowlbot.xyz"


def verify_link(guild_id, user_id):
    return f"{SITE_URL}/verify/{guild_id}?u={user_id}"


def provider_key(settings: dict, provider: str, kind: str) -> str:
    """Resolve a reCAPTCHA/Turnstile key: guild override first, then bot-owner .env default."""
    from_env = os.environ.get(f"{provider.upper()}_{kind.upper()}", "")
    if isinstance(settings, dict) and settings.get(f"{provider}_{kind}"):
        return settings[f"{provider}_{kind}"]
    return from_env


def captcha_solve_url(provider: str, code: str = "") -> str:
    """Public URL where a user solves the captcha and copies a token."""
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    url = f"{base}/captcha/{provider}"
    if code:
        url += f"?code={code}"
    return url


VERIFY_DEFAULTS = {
    "enabled": False, "channel_id": None, "verified_role_id": None,
    "log_channel_id": None, "type": "button", "captcha": False,
    "message": "Click the button below to verify yourself.",
    "reaction_emoji": EMBED_EMOJIS["check"],
    "recaptcha_site_key": "", "recaptcha_secret": "",
    "panel_embed": {}, "panel_message_id": None,
}


async def get_verify_settings(guild_id: int):
    return await neon_db.load_cached_settings("verify_settings", guild_id, VERIFY_DEFAULTS)


async def save_verify_settings(guild_id: int, settings: dict):
    await neon_db.save_cached_settings("verify_settings", guild_id, settings)


async def _verify_done(interaction: discord.Interaction, role_id, role_label="verified"):
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("error", "Error")).description("Verification role not found.").color("error").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )
        return
    if role in interaction.user.roles:
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("verify", "Already Verified")).description("You are already verified.").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )
        return
    await interaction.user.add_roles(role, reason=f"Verified via {role_label}")
    await interaction.response.send_message(
        embed=EmbedBuilder().title(emoji_title("verify", "Verified")).description("You have been verified!").color("success").timestamp(datetime.datetime.utcnow()).build(),
        ephemeral=True
    )


class VerifyButtonView(discord.ui.View):
    def __init__(self, role_id: int):
        super().__init__(timeout=None)
        self.role_id = role_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji=BUTTON_EMOJIS["check"], custom_id="verify:click")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Resolve the role fresh so persistent views keep working after restarts
        settings = await get_verify_settings(interaction.guild_id)
        role_id = int(settings.get("verified_role_id") or self.role_id or 0)
        await _verify_done(interaction, role_id, "button")


class CaptchaModal(discord.ui.Modal, title="Verification"):
    def __init__(self, role_id: int):
        super().__init__()
        self.role_id = role_id
        self.code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        self.add_item(discord.ui.TextInput(label=f"Enter this code: {self.code}", placeholder="Type the code above", max_length=6))

    async def on_submit(self, interaction: discord.Interaction):
        if self.children[0].value.strip().upper() != self.code:
            await interaction.response.send_message(
                embed=EmbedBuilder().title(emoji_title("verify_fail", "Failed")).description("Incorrect code. Try again.").color("error").timestamp(datetime.datetime.utcnow()).build(),
                ephemeral=True
            )
            return
        await _verify_done(interaction, self.role_id, "captcha")


class CaptchaButtonView(discord.ui.View):
    def __init__(self, role_id: int):
        super().__init__(timeout=None)
        self.role_id = role_id

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji=BUTTON_EMOJIS["lock"], custom_id="verify:captcha")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_verify_settings(interaction.guild_id)
        role_id = int(settings.get("verified_role_id") or self.role_id or 0)
        await interaction.response.send_modal(CaptchaModal(role_id))


class ExternalCaptchaButtonView(discord.ui.View):
    """Button that sends the user a personal one-time captcha link (no modal, no token pasting)."""

    def __init__(self, role_id: int = 0, provider: str = ""):
        super().__init__(timeout=None)
        self.role_id = role_id
        self.provider = provider
        label = "Google reCAPTCHA" if provider == "recaptcha" else "Cloudflare Turnstile"
        verify = discord.ui.Button(label=f"Verify with {label}", style=discord.ButtonStyle.success, emoji=BUTTON_EMOJIS["shield"], custom_id=f"verify:{provider}")
        async def cb(i: discord.Interaction):
            settings = await get_verify_settings(i.guild_id)
            if not settings.get("enabled") or settings.get("type") != self.provider:
                await i.response.send_message("Verification is not active for this method.", ephemeral=True)
                return
            code = await neon_db.create_captcha_code(self.provider, i.guild_id, i.user.id)
            if not code:
                await i.response.send_message("Could not create a verification link. Try again.", ephemeral=True)
                return
            link = captcha_solve_url(self.provider, code)
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Complete verification", style=discord.ButtonStyle.link, url=link))
            await i.response.send_message(
                "Click the button to complete verification in your browser.",
                view=view, ephemeral=True,
            )
        verify.callback = cb
        self.add_item(verify)


class Verification(commands.Cog, name="Verification"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.panel_messages = {}  # message_id -> guild_id (for reaction tracking)

    async def cog_load(self):
        self.bot.loop.create_task(self._register_persistent_views())

    async def _register_persistent_views(self):
        """Re-register panel buttons so they keep working after a bot restart."""
        await self.bot.wait_until_ready()
        try:
            self.bot.add_view(VerifyButtonView(0))
            self.bot.add_view(CaptchaButtonView(0))
            self.bot.add_view(ExternalCaptchaButtonView(0, "recaptcha"))
            logger.info("Registered persistent verification views.")
        except Exception as e:
            logger.error(f"Failed to register persistent verification views: {e}")

    async def _build_view(self, settings) -> discord.ui.View:
        vtype = settings.get("type", "button")
        role_id = int(settings.get("verified_role_id") or 0)
        if vtype == "reaction":
            return None  # reaction panels have no view
        if vtype == "captcha":
            return CaptchaButtonView(role_id)
        if vtype == "recaptcha":
            return ExternalCaptchaButtonView(role_id, "recaptcha")
        return VerifyButtonView(role_id)

    def _build_panel_embed(self, settings) -> discord.Embed:
        """Build the verification panel embed from the custom embed builder (or a sensible default)."""
        pe = settings.get("panel_embed") or {}
        if not isinstance(pe, dict):
            pe = {}
        embed = discord.Embed()
        if pe.get("title"):
            embed.title = pe["title"]
        else:
            embed.title = emoji_title("verify", "Verification")
        if pe.get("description"):
            embed.description = pe["description"]
        elif not pe.get("title") and not pe.get("description"):
            embed.description = settings.get("message") or "Click the button below to verify yourself."
        color = pe.get("color")
        if color:
            try:
                embed.color = int(str(color).lstrip("#"), 16)
            except (ValueError, TypeError):
                embed.color = discord.Color.green()
        else:
            embed.color = discord.Color.green()
        if pe.get("url"):
            embed.url = pe["url"]
        if pe.get("author_name"):
            embed.set_author(name=pe["author_name"], icon_url=pe.get("author_icon") or None)
        if pe.get("footer_text"):
            embed.set_footer(text=pe["footer_text"], icon_url=pe.get("footer_icon") or None)
        if pe.get("image_url"):
            embed.set_image(url=pe["image_url"])
        if pe.get("thumbnail_url"):
            embed.set_thumbnail(url=pe["thumbnail_url"])
        for f in (pe.get("fields") or []):
            if f.get("name"):
                embed.add_field(name=f["name"], value=f.get("value") or "\u200b", inline=bool(f.get("inline")))
        return embed

    async def _send_panel(self, guild: discord.Guild, settings) -> bool:
        import logging
        logger = logging.getLogger("Ediscord")
        channel = guild.get_channel(int(settings.get("channel_id") or 0))
        if not channel or not isinstance(channel, discord.TextChannel):
            logger.warning(f"_send_panel: channel not found (channel_id={settings.get('channel_id')}, guild_channels={len(guild.channels)})")
            return False

        # Delete the previous panel message if one exists
        old_id = settings.get("panel_message_id")
        if old_id:
            try:
                old = await channel.fetch_message(int(old_id))
                await old.delete()
            except Exception:
                pass

        embed = self._build_panel_embed(settings)

        vtype = settings.get("type", "button")
        if vtype == "reaction":
            view = discord.ui.View()
            message = await channel.send(embed=embed, view=view)
            emoji = settings.get("reaction_emoji") or "✅"
            try:
                await message.add_reaction(emoji)
            except Exception:
                await message.add_reaction("✅")
            self.panel_messages[message.id] = guild.id
        else:
            view = await self._build_view(settings)
            message = await channel.send(embed=embed, view=view)

        # Track the new panel so the next deploy replaces it
        settings["panel_message_id"] = message.id
        await save_verify_settings(guild.id, settings)
        return True

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if payload.message_id not in self.panel_messages:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        settings = await get_verify_settings(payload.guild_id)
        if not settings.get("enabled") or settings.get("type") != "reaction":
            return
        role = guild.get_role(int(settings.get("verified_role_id") or 0))
        member = guild.get_member(payload.user_id)
        if not role or not member:
            return
        try:
            if role not in member.roles:
                await member.add_roles(role, reason="Verified via reaction")
                await member.send("You have been verified!")
        except Exception as e:
            logger.error(f"Reaction verify failed: {e}")

    verify_group = app_commands.Group(name="verify", description="Verification system commands")

    @verify_group.command(name="setup", description="Set up the verification panel")
    @app_commands.describe(channel="Channel for the verification panel", role="Role to assign on verification", type="Verification method")
    @app_commands.choices(type=[
        app_commands.Choice(name="Button", value="button"),
        app_commands.Choice(name="Reaction Role", value="reaction"),
        app_commands.Choice(name="Captcha Code", value="captcha"),
        app_commands.Choice(name="reCAPTCHA", value="recaptcha"),
    ])
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role, type: str = "button"):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You need Administrator permission.", ephemeral=True)
        settings = await get_verify_settings(interaction.guild_id)
        settings.update({"enabled": True, "channel_id": channel.id, "verified_role_id": role.id, "type": type})
        await save_verify_settings(interaction.guild_id, settings)
        ok = await self._send_panel(interaction.guild, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Setup Complete")).description(f"Verification panel {'deployed in ' + channel.mention if ok else 'saved but channel not found'}.").color("success").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @verify_group.command(name="deploy", description="(Re)post the verification panel to the configured channel")
    async def deploy(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You need Administrator permission.", ephemeral=True)
        settings = await get_verify_settings(interaction.guild_id)
        ok = await self._send_panel(interaction.guild, settings)
        await interaction.response.send_message(
            embed=EmbedBuilder().title(emoji_title("success", "Panel Deployed")).description("Verification panel posted." if ok else "Configure a channel first.").color("success" if ok else "error").timestamp(datetime.datetime.utcnow()).build(),
            ephemeral=True
        )

    @verify_group.command(name="config", description="View current verification settings")
    async def config(self, interaction: discord.Interaction):
        settings = await get_verify_settings(interaction.guild_id)
        channel = interaction.guild.get_channel(settings.get("channel_id") or 0)
        role = interaction.guild.get_role(settings.get("verified_role_id") or 0)
        log_channel = interaction.guild.get_channel(settings.get("log_channel_id") or 0)
        embed = EmbedBuilder().title(emoji_title("settings", "Verification Settings")).color("brand") \
            .row(
                ("Status", "Active" if settings.get("enabled") else "Inactive"),
                ("Channel", channel.mention if channel else "Not set"),
                ("Verified Role", role.mention if role else "Not set"),
                ("Log Channel", log_channel.mention if log_channel else "None"),
                ("Type", settings.get("type", "button").title()),
                ("CAPTCHA", "On" if settings.get("captcha") else "Off"),
            ) \
            .field("Welcome Message", (settings.get("message") or "None")[:100]) \
            .timestamp(datetime.datetime.utcnow()) \
            .build()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @verify_group.command(name="remove", description="Remove the verification system")
    async def remove(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You need Administrator permission.", ephemeral=True)
        await self._delete_panel(interaction.guild)
        await save_verify_settings(interaction.guild_id, {"enabled": False})
        await interaction.response.send_message("Verification system removed.", ephemeral=True)

    async def _delete_panel(self, guild: discord.Guild):
        """Delete the deployed verification panel message (if any) and forget it."""
        settings = await get_verify_settings(guild.id)
        mid = settings.get("panel_message_id")
        if not mid:
            return
        channel = guild.get_channel(int(settings.get("channel_id") or 0))
        if channel:
            try:
                msg = await channel.fetch_message(int(mid))
                await msg.delete()
            except Exception:
                pass
        settings["panel_message_id"] = None
        await save_verify_settings(guild.id, settings)
        if mid in self.panel_messages:
            del self.panel_messages[mid]


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
