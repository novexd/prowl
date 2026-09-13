import discord
from discord.ext import commands
import datetime

from Ediscord import logger, EmbedBuilder
from Ediscord import db as neon_db
from Ediscord.builders import emoji_title


LOGGING_DEFAULTS = {
    "message_delete_channel": None,
    "message_edit_channel": None,
    "member_join_channel": None,
    "member_leave_channel": None,
    "member_ban_channel": None,
    "member_unban_channel": None,
    "nickname_channel": None,
    "member_roles_channel": None,
    "member_mute_channel": None,
    "channel_create_channel": None,
    "channel_delete_channel": None,
    "channel_update_channel": None,
    "role_create_channel": None,
    "role_delete_channel": None,
    "role_update_channel": None,
    "server_update_channel": None,
    "emoji_update_channel": None,
    "invite_create_channel": None,
    "voice_channel": None,
}


async def get_logging_settings(guild_id: int):
    return await neon_db.load_cached_settings("logging_settings", guild_id, LOGGING_DEFAULTS)


def _fmt_time(dt):
    if not dt:
        return "-"
    return f"<t:{int(dt.timestamp())}:F>"


class Logging(commands.Cog, name="Logging"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _target(self, guild, key):
        if not guild:
            return None
        settings = await get_logging_settings(guild.id)
        channel_id = settings.get(key)
        if not channel_id:
            return None
        return guild.get_channel(int(channel_id))

    async def _post(self, guild, key, embed):
        if not guild:
            return
        channel = await self._target(guild, key)
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Logging failed ({key}) in {guild.id}: {e}")

    # ── Messages ──
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.guild:
            return
        embed = (
            EmbedBuilder()
            .color("gray")
            .divider().row(
                ('Channel', message.channel.mention),
                ('Author', f'{message.author} (`{message.author.id}`)')
            )
            .timestamp(message.created_at or datetime.datetime.utcnow())
            .header(emoji_title("message", "Message Deleted")).build()
        )
        if message.content:
            embed.add_field(name="Content", value=message.content[:1000] or "*(empty)*", inline=False)
        await self._post(message.guild, "message_delete_channel", embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages or not messages[0].guild:
            return
        first = messages[0]
        embed = (
            EmbedBuilder()
            .color("gray")
            .divider().row(
                ('Channel', first.channel.mention),
                ('Messages', str(len(messages)))
            )
            .timestamp(datetime.datetime.utcnow())
            .header(emoji_title("message", "Bulk Message Deleted")).build()
        )
        await self._post(first.guild, "message_delete_channel", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if after.author.bot or not after.guild:
            return
        if before.content == after.content:
            return
        embed = (
            EmbedBuilder()
            .color("gray")
            .divider().row(
                ('Channel', after.channel.mention),
                ('Author', f'{after.author} (`{after.author.id}`)')
            )
            .field("Before", (before.content or "*(embed only)*")[:1000], inline=False)
            .field("After", (after.content or "*(embed only)*")[:1000], inline=False)
            .timestamp(datetime.datetime.utcnow())
            .header(emoji_title("message", "Message Edited")).build()
        )
        await self._post(after.guild, "message_edit_channel", embed)

    # ── Members ──
    @commands.Cog.listener()
    async def on_member_join(self, member):
        embed = (
            EmbedBuilder()
            .color("success")
            .description(f"{member.mention} - {member}").header(emoji_title("welcome", "Member Joined"))
            .thumbnail(member.display_avatar.url)
            .divider().row(
                ('Account Created', _fmt_time(member.created_at)),
                ('Member #', str(len(member.guild.members)))
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(member.guild, "member_join_channel", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        embed = (
            EmbedBuilder()
            .color("error")
            .description(f"{member} (`{member.id}`)").header(emoji_title("goodbye", "Member Left"))
            .thumbnail(member.display_avatar.url)
            .divider().row(
                ('Joined', _fmt_time(member.joined_at)),
                ('Roles', str(len(member.roles) - 1))
            )
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(member.guild, "member_leave_channel", embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        embed = (
            EmbedBuilder()
            .color("error")
            .description(f"{user} (`{user.id}`)").header(emoji_title("ban", "Member Banned"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(guild, "member_ban_channel", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        embed = (
            EmbedBuilder()
            .color("success")
            .description(f"{user} (`{user.id}`)").header(emoji_title("unban", "Member Unbanned"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(guild, "member_unban_channel", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.bot:
            return
        # Nickname
        if before.nick != after.nick:
            embed = (
                EmbedBuilder()
            .color("gray")
                .divider().row(
                    ('User', f'{after.mention} (`{after.id}`)'),
                    ('Before', before.nick or '*(none)*'),
                    ('After', after.nick or '*(none)*')
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("member", "Nickname Changed")).build()
            )
            await self._post(after.guild, "nickname_channel", embed)
        # Roles
        if before.roles != after.roles:
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            parts = []
            if added:
                parts.append("**Added:** " + ", ".join(r.mention for r in added))
            if removed:
                parts.append("**Removed:** " + ", ".join(r.mention for r in removed))
            embed = (
                EmbedBuilder()
            .color("brand")
                .divider().row(
                    ('User', f'{after.mention} (`{after.id}`)'),
                    ('Change', '\n'.join(parts) or '*(none)*')
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("role", "Roles Updated")).build()
            )
            await self._post(after.guild, "member_roles_channel", embed)
        # Mute (Discord timeout)
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                text = f"Muted until {_fmt_time(after.timed_out_until)}"
            else:
                text = "Mute lifted"
            embed = (
                EmbedBuilder()
            .color("warn")
                .divider().row(
                    ('User', f'{after.mention} (`{after.id}`)'),
                    ('Status', text)
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("mute", "Mute Changed")).build()
            )
            await self._post(after.guild, "member_mute_channel", embed)

    # ── Channels ──
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        embed = (
            EmbedBuilder()
            .color("gray")
            .description(f"{channel.mention} - `{channel.name}`").header(emoji_title("channel", "Channel Created"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(channel.guild, "channel_create_channel", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        embed = (
            EmbedBuilder()
            .color("gray")
            .description(f"`#{channel.name}`").header(emoji_title("channel", "Channel Deleted"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(channel.guild, "channel_delete_channel", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append(f"**Topic:** {after.topic or '*(cleared)*'}")
        if not changes:
            return
        embed = (
            EmbedBuilder()
            .color("gray")
            .description(f"{after.mention}\n" + "\n".join(changes)).header(emoji_title("channel", "Channel Updated"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(after.guild, "channel_update_channel", embed)

    # ── Roles ──
    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        embed = (
            EmbedBuilder()
            .color("brand")
            .description(role.mention).header(emoji_title("role", "Role Created"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(role.guild, "role_create_channel", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        embed = (
            EmbedBuilder()
            .color("brand")
            .description(f"`@{role.name}`").header(emoji_title("role", "Role Deleted"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(role.guild, "role_delete_channel", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.permissions.value != after.permissions.value:
            changes.append("**Permissions:** changed")
        if before.color != after.color:
            changes.append("**Color:** changed")
        if not changes:
            return
        embed = (
            EmbedBuilder()
            .color("brand")
            .description(f"{after.mention}\n" + "\n".join(changes)).header(emoji_title("role", "Role Updated"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(after.guild, "role_update_channel", embed)

    # ── Server ──
    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.icon != after.icon:
            changes.append("**Icon:** changed")
        if getattr(before, "banner", None) != getattr(after, "banner", None):
            changes.append("**Banner:** changed")
        if before.verification_level != after.verification_level:
            changes.append(f"**Verification Level:** changed")
        if not changes:
            return
        embed = (
            EmbedBuilder()
            .color("gray")
            .description("\n".join(changes)).header(emoji_title("server", "Server Updated"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(after, "server_update_channel", embed)

    # ── Emojis ──
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if not added and not removed:
            return
        parts = []
        if added:
            parts.append("**Added:** " + " ".join(str(e) for e in added))
        if removed:
            parts.append("**Removed:** " + " ".join(f":{e.name}:" for e in removed))
        embed = (
            EmbedBuilder()
            .color("pink")
            .description("\n".join(parts)).header(emoji_title("sparkle", "Emoji Updated"))
            .timestamp(datetime.datetime.utcnow())
            .build()
        )
        await self._post(guild, "emoji_update_channel", embed)

    # ── Invites ──
    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        guild = getattr(invite, "guild", None)
        if not guild:
            return
        embed = (
            EmbedBuilder()
            .color("success")
            .divider().row(
                ('Code', invite.code),
                ('Channel', invite.channel.mention if invite.channel else '-'),
                ('Max Uses', str(invite.max_uses) if invite.max_uses else '∞')
            )
            .timestamp(datetime.datetime.utcnow())
            .header(emoji_title("invite_create", "Invite Created")).build()
        )
        await self._post(guild, "invite_create_channel", embed)

    # ── Voice ──
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not member.guild:
            return
        if before.channel == after.channel:
            return
        embed = None
        if before.channel is None and after.channel is not None:
            embed = (
                EmbedBuilder()
                .color("gray")
                .divider().row(
                    ('User', member.mention),
                    ('Channel', after.channel.mention)
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("mic", "Joined Voice")).build()
            )
        elif before.channel is not None and after.channel is None:
            embed = (
                EmbedBuilder()
                .color("gray")
                .divider().row(
                    ('User', member.mention),
                    ('Channel', before.channel.mention)
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("mic", "Left Voice")).build()
            )
        elif before.channel is not None and after.channel is not None:
            embed = (
                EmbedBuilder()
                .color("gray")
                .divider().row(
                    ('User', member.mention),
                    ('Before', before.channel.mention),
                    ('After', after.channel.mention)
                )
                .timestamp(datetime.datetime.utcnow())
                .header(emoji_title("mic", "Moved Voice")).build()
            )
        if embed:
            await self._post(member.guild, "voice_channel", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
