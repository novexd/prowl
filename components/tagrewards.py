"""Server-tag rewards: role + XP multiplier for members displaying the guild tag.

Detection uses Member.primary_guild (discord.py 2.6+): a member "has the tag
equipped" when their primary guild is this guild and the identity is enabled.

Role grants/removals run two ways:
  - on_message: fast-path grant when a tagged member talks without the role.
  - audit loop (10 min, rotating 250-member slices): grant on equip and,
    when tag_remove_on_loss is set, remove on unequip.

The XP multiplier is exposed via get_tag_multiplier() and consumed by the
leveling cog's rate calculation (same pattern as frenzy).
"""
import discord
from discord.ext import commands, tasks

from Ediscord import logger

AUDIT_SLICE = 250


def has_server_tag(member: discord.Member) -> bool:
    """True when the member publicly displays this guild's server tag."""
    try:
        pg = member.primary_guild
    except AttributeError:
        return False
    if pg is None:
        return False
    try:
        return pg.id == member.guild.id and bool(pg.identity_enabled)
    except Exception:
        return False


async def get_tag_multiplier(guild_id: int, member: discord.Member) -> float:
    """XP multiplier for tagged members (1.0 when disabled/untagged)."""
    try:
        from .leveling import get_leveling_settings
        settings = await get_leveling_settings(guild_id)
    except Exception:
        return 1.0
    if not settings.get("tag_rewards_enabled"):
        return 1.0
    if not has_server_tag(member):
        return 1.0
    try:
        return max(1.0, float(settings.get("tag_xp_multiplier", 1.0)))
    except (TypeError, ValueError):
        return 1.0


async def _resolve_role(guild: discord.Guild, settings: dict):
    raw = settings.get("tag_role_id")
    if not raw:
        return None
    try:
        return guild.get_role(int(raw))
    except (TypeError, ValueError):
        return None


def _bot_can_manage(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    if me is None or role is None:
        return False
    try:
        return me.guild_permissions.manage_roles and me.top_role > role
    except Exception:
        return False


class TagRewards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cursor: dict[int, int] = {}
        self._audit.start()

    def cog_unload(self):
        self._audit.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        try:
            from .leveling import get_leveling_settings
            settings = await get_leveling_settings(message.guild.id)
        except Exception:
            return
        if not settings.get("tag_rewards_enabled"):
            return
        if not has_server_tag(member):
            return
        role = await _resolve_role(message.guild, settings)
        if role is None or role in member.roles:
            return
        if not _bot_can_manage(message.guild, role):
            return
        try:
            await member.add_roles(role, reason="Server tag reward")
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Tag role grant failed in {message.guild.id}: {e}")

    @tasks.loop(minutes=10)
    async def _audit(self):
        for guild in self.bot.guilds:
            try:
                from .leveling import get_leveling_settings
                settings = await get_leveling_settings(guild.id)
            except Exception:
                continue
            if not settings.get("tag_rewards_enabled"):
                continue
            role = await _resolve_role(guild, settings)
            if role is None or not _bot_can_manage(guild, role):
                continue
            remove_on_loss = bool(settings.get("tag_remove_on_loss"))
            members = [m for m in guild.members if not m.bot]
            if not members:
                continue
            start = self._cursor.get(guild.id, 0) % len(members)
            batch = (members[start:] + members[:start])[:AUDIT_SLICE]
            self._cursor[guild.id] = start + len(batch)
            for member in batch:
                try:
                    tagged = has_server_tag(member)
                    has_role = role in member.roles
                    if tagged and not has_role:
                        await member.add_roles(role, reason="Server tag reward")
                    elif remove_on_loss and not tagged and has_role:
                        await member.remove_roles(role, reason="Server tag removed")
                except (discord.Forbidden, discord.HTTPException):
                    continue
                except Exception as e:
                    logger.warning(f"Tag audit failed for {member.id}: {e}")

    @_audit.before_loop
    async def _before_audit(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TagRewards(bot))
