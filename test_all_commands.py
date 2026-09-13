"""
Comprehensive test suite for every Prowl Discord bot command.

Tests cover:
  - Pure utility/helper functions (no mocks needed)
  - Every slash command across all 21 cogs (with mocked Discord objects)
  - DB-dependent paths (mocked DB layer)

Run:
    cd cli && python -m pytest test_all_commands.py -v
    or: cd cli && python test_all_commands.py
"""

import asyncio
import datetime
import math
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

os.environ.setdefault("TOKEN", "test-token-for-testing-only")
os.environ.setdefault("DATABASE_URL", "libsql://fake-url")
os.environ.setdefault("DASHBOARD_URL", "")
os.environ.setdefault("SITE_URL", "")

import discord
from discord import app_commands
from discord.ext import commands


def _cmd(cog_method):
    """Get the callable callback from an app_commands.command or group.command decorated method.

    Group commands (e.g. @level_group.command) store the function as .callback
    on the Command object, while plain @app_commands.command decorated methods
    on a Cog are already the raw async function.
    """
    if hasattr(cog_method, 'callback'):
        return cog_method.callback
    return cog_method


# =========================================================================
#  MOCK DISCORD INFRASTRUCTURE
# =========================================================================

def make_mock_role(name="TestRole", rid=1111, position=1, color=None, permissions=None):
    role = MagicMock(spec=discord.Role)
    role.name = name
    role.id = rid
    role.mention = f"<@&{rid}>"
    role.position = position
    role.color = color or discord.Color.default()
    role.mentionable = False
    role.hoist = False
    role.managed = False
    role.created_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    role.permissions = permissions or discord.Permissions(permissions=0)
    role.guild = MagicMock()
    role.__ge__ = lambda self, other: self.position >= getattr(other, 'position', other)
    role.__gt__ = lambda self, other: self.position > getattr(other, 'position', other)
    role.__le__ = lambda self, other: self.position <= getattr(other, 'position', other)
    role.__lt__ = lambda self, other: self.position < getattr(other, 'position', other)
    role.__eq__ = lambda self, other: self.position == getattr(other, 'position', other)
    role.__hash__ = lambda self: hash(self.id)
    return role


_MOCK_GUILD_SENTINEL = object()

def make_mock_member(name="TestUser", uid=9999, roles=None, guild=_MOCK_GUILD_SENTINEL, top_role_pos=1, permissions=None, bot=False, joined_at=None):
    member = MagicMock(spec=discord.Member)
    member.name = name
    member.display_name = name
    member.id = uid
    member.mention = f"<@{uid}>"
    member.bot = bot
    member.nick = None
    member.joined_at = joined_at or datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    member.status = discord.Status.online
    member.display_avatar = MagicMock()
    member.display_avatar.url = "https://example.com/avatar.png"
    member.color = discord.Color.blue()

    if guild is _MOCK_GUILD_SENTINEL:
        _guild = _make_mock_guild_internal()
    else:
        _guild = guild
    member.guild = _guild

    _roles = roles or [make_mock_role(name="@everyone", rid=_guild.id, position=0)]
    member.roles = _roles
    member.top_role = max(_roles, key=lambda r: r.position) if _roles else _roles[0]

    perms = permissions or discord.Permissions(permissions=0)
    member.guild_permissions = perms

    member.send = AsyncMock()
    member.kick = AsyncMock()
    member.ban = AsyncMock()
    member.unban = AsyncMock()
    member.timeout = AsyncMock()
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.edit = AsyncMock()
    member.is_timed_out = MagicMock(return_value=False)
    return member


def make_mock_guild(name="Test Guild", gid=12345, member_count=50, owner_id=9999):
    return _make_mock_guild_internal(name=name, gid=gid, member_count=member_count, owner_id=owner_id)

def _make_mock_guild_internal(name="Test Guild", gid=12345, member_count=50, owner_id=9999):
    guild = MagicMock(spec=discord.Guild)
    guild.name = name
    guild.id = gid
    guild.member_count = member_count
    guild.owner_id = owner_id
    guild.icon = MagicMock()
    guild.icon.url = "https://example.com/icon.png"
    guild.premium_tier = 1
    guild.premium_subscription_count = 5
    guild.created_at = datetime.datetime(2023, 1, 1, tzinfo=datetime.timezone.utc)
    guild.emoji_count = 10
    guild.fetch_member = AsyncMock(return_value=MagicMock(name="FetchedMember"))

    everyone_role = make_mock_role(name="@everyone", rid=gid, position=0)
    guild.default_role = everyone_role
    guild.roles = [everyone_role]

    me = MagicMock(spec=discord.Member)
    me.name = "Prowl"
    me.id = 8888
    me.mention = "<@8888>"
    me.bot = True
    me.nick = None
    me.guild = guild
    me.roles = [everyone_role]
    me.top_role = everyone_role
    me.guild_permissions = discord.Permissions(permissions=0)
    me.display_avatar = MagicMock()
    me.display_avatar.url = "https://example.com/avatar.png"
    me.color = discord.Color.default()
    me.status = discord.Status.online
    guild.me = me
    guild.members = [me]

    guild.text_channels = []
    guild.voice_channels = []
    guild.categories = []
    guild.emojis = []
    guild.get_channel = MagicMock(return_value=None)
    guild.get_member = MagicMock(return_value=None)
    guild.get_role = MagicMock(return_value=None)
    guild.invites = AsyncMock(return_value=[])
    guild.channels = []
    guild.unban = AsyncMock()
    return guild


def make_mock_channel(name="test-channel", cid=55555, guild=None):
    ch = MagicMock(spec=discord.TextChannel)
    ch.name = name
    ch.id = cid
    ch.mention = f"<#{cid}>"
    ch.topic = "Test topic"
    ch.slowmode_delay = 0
    ch.nsfw = False
    ch.position = 0
    ch.type = discord.ChannelType.text
    ch.category = None
    ch.created_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    ch.send = AsyncMock()
    ch.purge = AsyncMock(return_value=[])
    ch.set_permissions = AsyncMock()
    ch.overwrites_for = MagicMock(return_value=discord.PermissionOverwrite())
    ch.guild = guild or make_mock_guild()
    return ch


def make_mock_interaction(user=None, guild=None, channel=None, command_name="test", guild_id=None, channel_id=None):
    interaction = MagicMock(spec=discord.Interaction)
    _guild = guild or make_mock_guild()
    _user = user or make_mock_member(guild=_guild)
    _channel = channel or make_mock_channel(guild=_guild)

    interaction.user = _user
    interaction.guild = _guild
    interaction.guild_id = guild_id or _guild.id
    interaction.channel_id = channel_id or _channel.id
    interaction.channel = _channel
    interaction.client = MagicMock()
    interaction.client.get_guild = MagicMock(return_value=_guild)
    interaction.client.get_channel = MagicMock(return_value=_channel)

    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    interaction.command = MagicMock()
    interaction.command.name = command_name

    return interaction


class _MockAsyncCtx:
    """async context manager that yields a mock conn."""
    def __init__(self, conn):
        self._conn = conn
    async def __aenter__(self):
        return self._conn
    async def __aexit__(self, *a):
        pass

def make_mock_pool():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=0)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=_MockAsyncCtx(conn))
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)
    return pool


# Patch Moderation's background tasks so they don't start real loops
from discord.ext import tasks as _tasks_mod

class _FakeLoop:
    """Mock for @tasks.loop decorator that produces objects with .start() etc."""
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
    def __call__(self, fn):
        loop_obj = MagicMock(name=f"Loop({fn.__name__})")
        loop_obj.start = MagicMock()
        loop_obj.cancel = MagicMock()
        loop_obj.restart = MagicMock()
        loop_obj.before_loop = MagicMock(return_value=fn)
        loop_obj.after_loop = MagicMock(return_value=fn)
        loop_obj.error = MagicMock(return_value=fn)
        loop_obj.is_running = MagicMock(return_value=False)
        loop_obj._injected = None
        loop_obj._callback = fn
        return loop_obj

# Replace at the module level before any cog imports happen
_original_loop = _tasks_mod.loop
_tasks_mod.loop = _FakeLoop


# =========================================================================
#  1. PURE UTILITY FUNCTION TESTS
# =========================================================================

class TestLevelingMath(unittest.TestCase):
    def test_xp_for_level(self):
        from components.leveling import xp_for_level
        self.assertEqual(xp_for_level(1), 100)
        self.assertEqual(xp_for_level(2), 250)
        self.assertEqual(xp_for_level(3), 400)

    def test_level_from_xp(self):
        from components.leveling import level_from_xp
        self.assertEqual(level_from_xp(0), 1)
        self.assertEqual(level_from_xp(50), 1)
        self.assertEqual(level_from_xp(100), 1)
        self.assertEqual(level_from_xp(250), 2)

    def test_create_progress_bar(self):
        from components.leveling import create_progress_bar
        bar = create_progress_bar(50, 100, 10)
        self.assertEqual(len(bar), 10)
        self.assertIn("X", bar.replace("\u2593", "X").replace("\u2591", "Y"))

    def test_create_progress_bar_zero_max(self):
        from components.leveling import create_progress_bar
        bar = create_progress_bar(0, 0, 10)
        self.assertEqual(len(bar), 10)

    def test_create_progress_bar_full(self):
        from components.leveling import create_progress_bar
        bar = create_progress_bar(100, 100, 10)
        self.assertEqual(len(bar), 10)


class TestModerationHelpers(unittest.TestCase):
    def test_render_template(self):
        from components.moderation import render_template
        member = make_mock_member(name="Alice")
        result = render_template("{username} was warned for {reason}", member, reason="spam")
        self.assertIn("Alice", result)
        self.assertIn("spam", result)

    def test_render_template_empty(self):
        from components.moderation import render_template
        member = make_mock_member()
        self.assertEqual(render_template("", member), "")

    def test_format_duration_minutes(self):
        from components.moderation import format_duration
        self.assertEqual(format_duration(30), "30 minutes")
        self.assertEqual(format_duration(1), "1 minute")

    def test_format_duration_hours(self):
        from components.moderation import format_duration
        self.assertEqual(format_duration(60), "1 hour")
        self.assertEqual(format_duration(120), "2 hours")

    def test_format_duration_hours_minutes(self):
        from components.moderation import format_duration
        self.assertEqual(format_duration(90), "1 hour 30 minutes")


class TestReminderParsing(unittest.TestCase):
    def test_parse_relative_minutes(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("30m", now)
        self.assertIsNone(err)
        self.assertEqual(dt, now + datetime.timedelta(minutes=30))

    def test_parse_relative_hours(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("2h", now)
        self.assertIsNone(err)
        self.assertEqual(dt, now + datetime.timedelta(hours=2))

    def test_parse_relative_days(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("1d", now)
        self.assertIsNone(err)
        self.assertEqual(dt, now + datetime.timedelta(days=1))

    def test_parse_mixed_units(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("1h30m", now)
        self.assertIsNone(err)
        self.assertEqual(dt, now + datetime.timedelta(hours=1, minutes=30))

    def test_parse_tomorrow(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("tomorrow", now)
        self.assertIsNone(err)
        self.assertEqual(dt.date(), (now + datetime.timedelta(days=1)).date())

    def test_parse_clock(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("18:00", now)
        self.assertIsNone(err)
        self.assertEqual(dt.hour, 18)
        self.assertEqual(dt.minute, 0)

    def test_parse_invalid(self):
        from components.reminders import _parse_when
        dt, err = _parse_when("never")
        self.assertIsNone(dt)
        self.assertIsNotNone(err)

    def test_parse_clock_9am(self):
        from components.reminders import _parse_clock
        self.assertEqual(_parse_clock("9am"), (9, 0))

    def test_parse_clock_9pm(self):
        from components.reminders import _parse_clock
        self.assertEqual(_parse_clock("9pm"), (21, 0))

    def test_parse_clock_2100(self):
        from components.reminders import _parse_clock
        self.assertEqual(_parse_clock("21:00"), (21, 0))

    def test_parse_clock_invalid(self):
        from components.reminders import _parse_clock
        self.assertIsNone(_parse_clock("25:00"))


# =========================================================================
#  2. GENERAL COG
# =========================================================================

class TestGeneralCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self.bot.guilds = [make_mock_guild()]
        self.bot.users = [make_mock_member()]
        self.bot.cogs = {"General": MagicMock()}
        self.bot.tree = MagicMock()
        self.bot.tree.get_commands = MagicMock(return_value=[])
        self._db_patcher = patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool())
        self._db_patcher.start()
        from components.general import General
        self.cog = General(self.bot)

    async def asyncTearDown(self):
        self._db_patcher.stop()

    async def test_ping(self):
        interaction = make_mock_interaction(command_name="ping")
        await _cmd(self.cog.ping)(self.cog, interaction)
        interaction.response.defer.assert_called_once()
        interaction.followup.send.assert_called_once()

    async def test_info(self):
        interaction = make_mock_interaction(command_name="info")
        await _cmd(self.cog.info)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_invite(self):
        interaction = make_mock_interaction(command_name="invite")
        await _cmd(self.cog.invite)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_serverinfo(self):
        interaction = make_mock_interaction(command_name="server_info")
        await _cmd(self.cog.serverinfo)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_avatar_default(self):
        interaction = make_mock_interaction(command_name="avatar")
        await _cmd(self.cog.avatar)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_avatar_with_user(self):
        interaction = make_mock_interaction(command_name="avatar")
        target = make_mock_member(name="Target")
        await _cmd(self.cog.avatar)(self.cog, interaction, user=target)
        interaction.response.send_message.assert_called_once()

    async def test_roleinfo(self):
        interaction = make_mock_interaction(command_name="role_info")
        role = make_mock_role()
        await _cmd(self.cog.roleinfo)(self.cog, interaction, role=role)
        interaction.response.send_message.assert_called_once()

    async def test_channelinfo_default(self):
        interaction = make_mock_interaction(command_name="channel_info")
        await _cmd(self.cog.channelinfo)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_channelinfo_specific(self):
        interaction = make_mock_interaction(command_name="channel_info")
        ch = make_mock_channel()
        await _cmd(self.cog.channelinfo)(self.cog, interaction, channel=ch)
        interaction.response.send_message.assert_called_once()

    async def test_userinfo_self(self):
        interaction = make_mock_interaction(command_name="user_info")
        await _cmd(self.cog.userinfo)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_userinfo_other(self):
        interaction = make_mock_interaction(command_name="user_info")
        target = make_mock_member(name="Other")
        await _cmd(self.cog.userinfo)(self.cog, interaction, user=target)
        interaction.response.send_message.assert_called_once()

    async def test_say_with_permission(self):
        interaction = make_mock_interaction(command_name="say")
        interaction.user.guild_permissions = discord.Permissions(manage_messages=True)
        await _cmd(self.cog.say)(self.cog, interaction, text="Hello world")
        interaction.response.send_message.assert_called()

    async def test_say_no_permission(self):
        interaction = make_mock_interaction(command_name="say")
        interaction.user.guild_permissions = discord.Permissions(manage_messages=False)
        await _cmd(self.cog.say)(self.cog, interaction, text="Hello world")
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        self.assertTrue(call_kwargs.get("ephemeral", False))


# =========================================================================
#  3. LEVELING COG
# =========================================================================

class TestLevelingCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._pool = make_mock_pool()
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=self._pool),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
            patch("components.leveling.get_user_xp", new_callable=AsyncMock, return_value={"xp": 0, "level": 1}),
            patch("components.leveling.set_user_xp", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.leveling import Leveling
        self.cog = Leveling(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_rank_no_data(self):
        interaction = make_mock_interaction(command_name="rank")
        await _cmd(self.cog.rank)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_rank_with_xp(self):
        interaction = make_mock_interaction(command_name="rank")
        with patch("components.leveling.get_user_xp", new_callable=AsyncMock, return_value={"xp": 500, "level": 3}):
            await _cmd(self.cog.rank)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_leaderboard_empty(self):
        interaction = make_mock_interaction(command_name="leaderboard")
        self._pool.fetch = AsyncMock(return_value=[])
        self._pool.fetchrow = AsyncMock(return_value={"count": 0})
        await _cmd(self.cog.leaderboard)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_leaderboard_with_data(self):
        interaction = make_mock_interaction(command_name="leaderboard")
        self._pool.fetch = AsyncMock(return_value=[
            {"user_id": "1111", "xp": 1000},
            {"user_id": "2222", "xp": 500},
        ])
        self._pool.fetchrow = AsyncMock(return_value={"count": 2})
        await _cmd(self.cog.leaderboard)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_toggle_no_permission(self):
        interaction = make_mock_interaction(command_name="toggle")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.toggle)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args[1]
        self.assertTrue(call_kwargs.get("ephemeral", False))

    async def test_setxp_no_permission(self):
        interaction = make_mock_interaction(command_name="setxp")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        member = make_mock_member()
        await _cmd(self.cog.setxp)(self.cog, interaction, member=member, xp=100)
        interaction.response.send_message.assert_called_once()

    async def test_setxp_negative(self):
        interaction = make_mock_interaction(command_name="setxp")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        member = make_mock_member()
        await _cmd(self.cog.setxp)(self.cog, interaction, member=member, xp=-10)
        interaction.response.send_message.assert_called_once()

    async def test_reset_no_permission(self):
        interaction = make_mock_interaction(command_name="reset")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        member = make_mock_member()
        await _cmd(self.cog.reset)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_config_no_permission(self):
        interaction = make_mock_interaction(command_name="config")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.config)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_setrole_no_permission(self):
        interaction = make_mock_interaction(command_name="setrole")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        role = make_mock_role()
        await _cmd(self.cog.setrole)(self.cog, interaction, level=5, role=role)
        interaction.response.send_message.assert_called_once()

    async def test_setrole_invalid_level(self):
        interaction = make_mock_interaction(command_name="setrole")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        role = make_mock_role()
        await _cmd(self.cog.setrole)(self.cog, interaction, level=0, role=role)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  4. MODERATION COG
# =========================================================================

class TestModerationCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self.bot.loop = asyncio.get_event_loop()
        self.bot.guilds = [make_mock_guild()]
        self.bot.fetch_user = AsyncMock(return_value=make_mock_member(uid=7777))

        default_settings = {
            "cmd_ban": True, "cmd_kick": True, "cmd_tempban": True,
            "cmd_unban": True, "cmd_mute": True, "cmd_timeout": True,
            "cmd_unmute": True, "cmd_warn": True, "cmd_purge": True,
            "mod_roles": [], "emergency_lock": False, "mute_evasion": False,
            "modlog_channel_id": None, "dm_on_action": True, "silent_mod": False,
            "ban_dm": True, "tempban_dm": True, "kick_dm": True,
            "mute_dm": True, "warn_dm": True,
            "ban_purge": True, "tempban_purge": True,
            "ban_message": "{username} has been banned.", "ban_message_enabled": True,
            "ban_message_mode": "basic", "ban_embed": {},
            "tempban_message": "{username} has been temporarily banned for {time}.",
            "tempban_message_enabled": True, "tempban_message_mode": "basic",
            "tempban_embed": {}, "tempban_duration": 1440,
            "mute_duration": 60, "mute_message": "{username} has been muted for {time}.",
            "mute_message_enabled": True, "mute_message_mode": "basic", "mute_embed": {},
            "kick_message": "{username} has been kicked.", "kick_message_enabled": True,
            "kick_message_mode": "basic", "kick_embed": {},
            "warn_message": "{username} has been warned.", "warn_message_enabled": True,
            "warn_message_mode": "basic", "warn_embed": {},
            "require_reason": True, "auto_thread": False, "track_stats": True,
        }
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.push_mod_event", new_callable=AsyncMock),
            patch("components.moderation.get_mod_settings", new_callable=AsyncMock, return_value=default_settings),
        ]
        for p in self._patches:
            p.start()

        from components.moderation import Moderation
        self.cog = Moderation.__new__(Moderation)
        self.cog.bot = self.bot
        self.cog.msg_counts = {}
        self.cog.message_accum = {}
        self.cog.member_counts = {}
        import time as _time
        self.cog.hour_started = int(_time.time())
        # Mock the background task loops so cog_unload works
        self.cog.flush_history = MagicMock()
        self.cog.flush_history.start = MagicMock()
        self.cog.flush_history.cancel = MagicMock()
        self.cog.flush_messages = MagicMock()
        self.cog.flush_messages.start = MagicMock()
        self.cog.flush_messages.cancel = MagicMock()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_kick_self(self):
        interaction = make_mock_interaction(command_name="kick")
        member = interaction.user
        await _cmd(self.cog.kick)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_kick_high_role(self):
        interaction = make_mock_interaction(command_name="kick")
        target = make_mock_member(name="Higher")
        interaction.guild.me.top_role = make_mock_role(position=1)
        target.top_role = make_mock_role(position=100)
        await _cmd(self.cog.kick)(self.cog, interaction, member=target)
        interaction.response.send_message.assert_called_once()

    async def test_ban_self(self):
        interaction = make_mock_interaction(command_name="ban")
        member = interaction.user
        await _cmd(self.cog.ban)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_ban_invalid_delete_days(self):
        interaction = make_mock_interaction(command_name="ban")
        target = make_mock_member(name="BanTarget")
        interaction.guild.me.top_role = make_mock_role(position=10)
        target.top_role = make_mock_role(position=1)
        await _cmd(self.cog.ban)(self.cog, interaction, member=target, delete_days=10)
        interaction.response.send_message.assert_called_once()

    async def test_mute_self(self):
        interaction = make_mock_interaction(command_name="mute")
        member = interaction.user
        await _cmd(self.cog.mute)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_mute_invalid_duration(self):
        interaction = make_mock_interaction(command_name="mute")
        target = make_mock_member(name="MuteTarget")
        interaction.guild.me.top_role = make_mock_role(position=10)
        target.top_role = make_mock_role(position=1)
        await _cmd(self.cog.mute)(self.cog, interaction, member=target, duration=0)
        interaction.response.send_message.assert_called_once()

    async def test_mute_too_long(self):
        interaction = make_mock_interaction(command_name="mute")
        target = make_mock_member(name="MuteTarget")
        interaction.guild.me.top_role = make_mock_role(position=10)
        target.top_role = make_mock_role(position=1)
        await _cmd(self.cog.mute)(self.cog, interaction, member=target, duration=99999)
        interaction.response.send_message.assert_called_once()

    async def test_unmute_not_muted(self):
        interaction = make_mock_interaction(command_name="unmute")
        target = make_mock_member(name="UnmuteTarget")
        target.is_timed_out = MagicMock(return_value=False)
        # Set up roles so top_role comparison works
        bot_role = make_mock_role(name="BotRole", position=10)
        user_role = make_mock_role(name="UserRole", position=1)
        interaction.guild.me.top_role = bot_role
        target.top_role = user_role
        target.roles = [user_role]
        await _cmd(self.cog.unmute)(self.cog, interaction, member=target)
        interaction.response.send_message.assert_called_once()

    async def test_warn_self(self):
        interaction = make_mock_interaction(command_name="warn")
        member = interaction.user
        await _cmd(self.cog.warn)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_purge_out_of_range(self):
        interaction = make_mock_interaction(command_name="purge")
        await _cmd(self.cog.purge)(self.cog, interaction, count=0)
        interaction.response.send_message.assert_called_once()

    async def test_purge_over_limit(self):
        interaction = make_mock_interaction(command_name="purge")
        await _cmd(self.cog.purge)(self.cog, interaction, count=101)
        interaction.response.send_message.assert_called_once()

    async def test_purge_valid(self):
        interaction = make_mock_interaction(command_name="purge")
        interaction.channel.purge = AsyncMock(return_value=[MagicMock() for _ in range(5)])
        await _cmd(self.cog.purge)(self.cog, interaction, count=5)
        interaction.response.send_message.assert_called_once()

    async def test_view_settings(self):
        interaction = make_mock_interaction(command_name="settings")
        await _cmd(self.cog.view_settings)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_muteevasion_toggle(self):
        interaction = make_mock_interaction(command_name="mute_evasion")
        await _cmd(self.cog.muteevasion)(self.cog, interaction, enabled=True)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  5. AFK COG
# =========================================================================

class TestAFKCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.get_afk", new_callable=AsyncMock, return_value=None),
            patch("Ediscord.db.set_afk", new_callable=AsyncMock),
            patch("Ediscord.db.clear_afk", new_callable=AsyncMock),
            patch("Ediscord.db.get_afk_settings", new_callable=AsyncMock, return_value={"enabled": True}),
        ]
        for p in self._patches:
            p.start()
        from components.afk import AFK
        self.cog = AFK(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_afk_set(self):
        interaction = make_mock_interaction(command_name="afk")
        await _cmd(self.cog.afk)(self.cog, interaction, reason="brb food")
        interaction.response.send_message.assert_called_once()

    async def test_afk_clear(self):
        interaction = make_mock_interaction(command_name="afk")
        with patch("Ediscord.db.get_afk", new_callable=AsyncMock, return_value={"reason": "old"}):
            await _cmd(self.cog.afk)(self.cog, interaction, reason="")
        interaction.response.send_message.assert_called_once()

    async def test_afk_no_existing_no_reason(self):
        interaction = make_mock_interaction(command_name="afk")
        await _cmd(self.cog.afk)(self.cog, interaction, reason="")
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  6. REMINDERS COG
# =========================================================================

class TestRemindersCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.add_reminder", new_callable=AsyncMock, return_value=1),
            patch("Ediscord.db.list_reminders", new_callable=AsyncMock, return_value=[]),
            patch("Ediscord.db.cancel_reminder", new_callable=AsyncMock, return_value=True),
            patch("Ediscord.db.add_todo", new_callable=AsyncMock, return_value=1),
            patch("Ediscord.db.list_todos", new_callable=AsyncMock, return_value=[]),
            patch("Ediscord.db.complete_todo", new_callable=AsyncMock, return_value=True),
            patch("Ediscord.db.clear_todos", new_callable=AsyncMock, return_value=5),
        ]
        for p in self._patches:
            p.start()
        from components.reminders import Reminders
        self.cog = Reminders(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_remind_set(self):
        interaction = make_mock_interaction(command_name="remind set")
        await _cmd(self.cog.remind_set)(self.cog, interaction, when="30m", what="take out trash")
        interaction.response.send_message.assert_called_once()

    async def test_remind_list_empty(self):
        interaction = make_mock_interaction(command_name="remind list")
        await _cmd(self.cog.remind_list)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_remind_cancel(self):
        interaction = make_mock_interaction(command_name="remind cancel")
        await _cmd(self.cog.remind_cancel)(self.cog, interaction, id=1)
        interaction.response.send_message.assert_called_once()

    async def test_todo_add(self):
        interaction = make_mock_interaction(command_name="todo add")
        await _cmd(self.cog.todo_add)(self.cog, interaction, task="Buy groceries")
        interaction.response.send_message.assert_called_once()

    async def test_todo_list_empty(self):
        interaction = make_mock_interaction(command_name="todo list")
        await _cmd(self.cog.todo_list)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_todo_done(self):
        interaction = make_mock_interaction(command_name="todo done")
        await _cmd(self.cog.todo_done)(self.cog, interaction, id=1)
        interaction.response.send_message.assert_called_once()

    async def test_todo_clear(self):
        interaction = make_mock_interaction(command_name="todo clear")
        await _cmd(self.cog.todo_clear)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  7. WELCOMER COG
# =========================================================================

class TestWelcomerCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock, return_value={}),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.welcomer import Welcomer
        self.cog = Welcomer(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_toggle_no_permission(self):
        interaction = make_mock_interaction(command_name="welcomer toggle")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.toggle)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_set_channel_no_permission(self):
        interaction = make_mock_interaction(command_name="welcomer channel")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        ch = make_mock_channel()
        await _cmd(self.cog.set_channel)(self.cog, interaction, channel=ch)
        interaction.response.send_message.assert_called_once()

    async def test_config(self):
        interaction = make_mock_interaction(command_name="welcomer config")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        await _cmd(self.cog.config)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_dm_toggle(self):
        interaction = make_mock_interaction(command_name="welcomer dm")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        await _cmd(self.cog.dm)(self.cog, interaction, enabled=True, message="Welcome!")
        interaction.response.send_message.assert_called_once()

    async def test_nickname_no_permission(self):
        interaction = make_mock_interaction(command_name="welcomer nickname")
        interaction.user.guild_permissions = discord.Permissions(manage_nicknames=False)
        await _cmd(self.cog.nickname)(self.cog, interaction, nickname="Test")
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  8. TICKETS COG
# =========================================================================

class TestTicketsCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.tickets import Tickets
        self.cog = Tickets(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_add_user_wrong_channel(self):
        interaction = make_mock_interaction(command_name="ticket add")
        interaction.channel.name = "general"
        member = make_mock_member()
        await _cmd(self.cog.add_user)(self.cog, interaction, user=member)
        interaction.response.send_message.assert_called_once()

    async def test_remove_user_wrong_channel(self):
        interaction = make_mock_interaction(command_name="ticket remove")
        interaction.channel.name = "general"
        member = make_mock_member()
        await _cmd(self.cog.remove_user)(self.cog, interaction, user=member)
        interaction.response.send_message.assert_called_once()

    async def test_add_user_ticket_channel(self):
        interaction = make_mock_interaction(command_name="ticket add")
        interaction.channel.name = "ticket-1234"
        interaction.channel.overwrites_for = MagicMock(return_value=discord.PermissionOverwrite())
        interaction.channel.set_permissions = AsyncMock()
        member = make_mock_member(name="Helper")
        await _cmd(self.cog.add_user)(self.cog, interaction, user=member)
        interaction.response.send_message.assert_called_once()

    async def test_remove_user_ticket_channel(self):
        interaction = make_mock_interaction(command_name="ticket remove")
        interaction.channel.name = "ticket-1234"
        interaction.channel.overwrites_for = MagicMock(return_value=discord.PermissionOverwrite())
        interaction.channel.set_permissions = AsyncMock()
        member = make_mock_member(name="Helper")
        await _cmd(self.cog.remove_user)(self.cog, interaction, user=member)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  9. VERIFICATION COG
# =========================================================================

class TestVerificationCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock, return_value={}),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.verification import Verification
        self.cog = Verification(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_config(self):
        interaction = make_mock_interaction(command_name="verify config")
        await _cmd(self.cog.config)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  10. SOCIAL ALERTS COG
# =========================================================================

class TestSocialAlertsCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock, return_value={}),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.social_alerts import SocialAlerts
        self.cog = SocialAlerts(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_config(self):
        interaction = make_mock_interaction(command_name="social config")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=True)
        await _cmd(self.cog.config)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_remove_no_permission(self):
        interaction = make_mock_interaction(command_name="social remove")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.remove)(self.cog, interaction, platform="youtube")
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  11. GLOBAL CHAT COG
# =========================================================================

class TestGlobalChatCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.global_chat import GlobalChat
        self.cog = GlobalChat(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_info(self):
        interaction = make_mock_interaction(command_name="globalchat info")
        await _cmd(self.cog.info)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_link_no_permission(self):
        interaction = make_mock_interaction(command_name="globalchat link")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.link)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()

    async def test_unlink_no_permission(self):
        interaction = make_mock_interaction(command_name="globalchat unlink")
        interaction.user.guild_permissions = discord.Permissions(manage_guild=False)
        await _cmd(self.cog.unlink)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  12. INVITE TRACKER COG
# =========================================================================

class TestInviteTrackerCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.invite_tracker import InviteTracker
        self.cog = InviteTracker(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_stats_empty(self):
        interaction = make_mock_interaction(command_name="invites stats")
        pool = make_mock_pool()
        pool.fetch = AsyncMock(return_value=[])
        with patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=pool):
            await _cmd(self.cog.stats)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  13. MEMBERS COG
# =========================================================================

class TestMembersCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
        ]
        for p in self._patches:
            p.start()
        from components.members import Members
        self.cog = Members(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_list_no_permission(self):
        interaction = make_mock_interaction(command_name="members list")
        interaction.user.guild_permissions = discord.Permissions(manage_roles=False, administrator=False)
        role = make_mock_role()
        await _cmd(self.cog.list_members)(self.cog, interaction, role=role)
        interaction.response.send_message.assert_called_once()

    async def test_member_info(self):
        interaction = make_mock_interaction(command_name="members info")
        member = make_mock_member()
        await _cmd(self.cog.member_info)(self.cog, interaction, member=member)
        interaction.response.send_message.assert_called_once()

    async def test_role_no_permission(self):
        interaction = make_mock_interaction(command_name="members role")
        interaction.user.guild_permissions = discord.Permissions(manage_roles=False, administrator=False)
        member = make_mock_member()
        role = make_mock_role()
        await _cmd(self.cog.role)(self.cog, interaction, member=member, role=role)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  14. GIVEAWAYS COG
# =========================================================================

class TestGiveawaysCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        self._patches = [
            patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()),
            patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock),
            patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock),
        ]
        for p in self._patches:
            p.start()
        from components.giveaways import GiveawayCog
        self.cog = GiveawayCog(self.bot)

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()

    async def test_giveaway_list_empty(self):
        interaction = make_mock_interaction(command_name="giveaway list")
        interaction.user.guild_permissions = discord.Permissions(manage_messages=True)
        with patch("Ediscord.db.list_giveaways", new_callable=AsyncMock, return_value=[]):
            await _cmd(self.cog.giveaway_list)(self.cog, interaction)
        interaction.response.send_message.assert_called_once()


# =========================================================================
#  14b. UTILITIES COG
# =========================================================================

class TestUtilitiesCog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.latency = 0.05
        from components.utilities import Utilities
        self.cog = Utilities(self.bot)

    def _make_attachment(self, data: bytes, filename: str, content_type: str = "image/png"):
        att = MagicMock(spec=discord.Attachment)
        att.filename = filename
        att.content_type = content_type
        att.url = f"https://cdn.example.com/{filename}"
        att.read = AsyncMock(return_value=data)
        return att

    async def test_convert_image_png_to_jpg(self):
        from PIL import Image
        import io
        img = Image.new("RGBA", (64, 64), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        att = self._make_attachment(buf.read(), "test.png", "image/png")

        interaction = make_mock_interaction(command_name="convert")
        await _cmd(self.cog.convert_cmd)(self.cog, interaction, att, "jpg")
        interaction.followup.send.assert_called_once()

    async def test_convert_image_to_webp(self):
        from PIL import Image
        import io
        img = Image.new("RGB", (32, 32), (0, 128, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        att = self._make_attachment(buf.read(), "photo.png", "image/png")

        interaction = make_mock_interaction(command_name="convert")
        await _cmd(self.cog.convert_cmd)(self.cog, interaction, att, "webp")
        interaction.followup.send.assert_called_once()

    async def test_convert_non_image_rejected(self):
        att = self._make_attachment(b"not an image", "doc.pdf", "application/pdf")
        interaction = make_mock_interaction(command_name="convert")
        await _cmd(self.cog.convert_cmd)(self.cog, interaction, att, "jpg")
        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        self.assertIn("Cannot convert", call_args[0][0])

    async def test_resize_image(self):
        from PIL import Image
        import io
        img = Image.new("RGB", (200, 100), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        att = self._make_attachment(buf.read(), "big.png", "image/png")

        interaction = make_mock_interaction(command_name="resize")
        await _cmd(self.cog.resize_cmd)(self.cog, interaction, att, 50, 25)
        interaction.followup.send.assert_called_once()

    async def test_resize_invalid_dimensions(self):
        att = self._make_attachment(b"data", "img.png", "image/png")
        interaction = make_mock_interaction(command_name="resize")
        await _cmd(self.cog.resize_cmd)(self.cog, interaction, att, 0, 100)
        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        self.assertIn("must be between", call_args[0][0])

    async def test_compress_image(self):
        from PIL import Image
        import io
        img = Image.new("RGB", (128, 128), (100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        att = self._make_attachment(buf.read(), "big.png", "image/png")

        interaction = make_mock_interaction(command_name="compress")
        await _cmd(self.cog.compress_cmd)(self.cog, interaction, att, 50)
        interaction.followup.send.assert_called_once()

    async def test_compress_invalid_quality(self):
        att = self._make_attachment(b"data", "img.png", "image/png")
        interaction = make_mock_interaction(command_name="compress")
        await _cmd(self.cog.compress_cmd)(self.cog, interaction, att, 0)
        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        self.assertIn("must be between", call_args[0][0])

    async def test_makezip_single_file(self):
        att = self._make_attachment(b"hello world", "test.txt", "text/plain")
        interaction = make_mock_interaction(command_name="makezip")
        with patch("aiohttp.ClientSession.post", new_callable=AsyncMock) as mock_post:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.text = AsyncMock(return_value="https://litterbox.catbox.moe/abcdef.zip")
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_post.return_value = mock_resp
            with patch("aiohttp.FormData"):
                with patch("aiohttp.ClientSession") as mock_session_cls:
                    mock_session = AsyncMock()
                    mock_session.post = AsyncMock(return_value=mock_resp)
                    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_session.__aexit__ = AsyncMock(return_value=False)
                    mock_session_cls.return_value = mock_session
                    await _cmd(self.cog.makezip_cmd)(self.cog, interaction, att)
        interaction.followup.send.assert_called_once()

    async def test_makezip_with_password(self):
        att = self._make_attachment(b"secret data", "secret.txt", "text/plain")
        interaction = make_mock_interaction(command_name="makezip")
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.text = AsyncMock(return_value="https://litterbox.catbox.moe/test123.zip")
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_resp)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session
            with patch("aiohttp.FormData"):
                await _cmd(self.cog.makezip_cmd)(self.cog, interaction, att, password="mypass123")
        interaction.followup.send.assert_called_once()

    async def test_makezip_empty_file_list(self):
        att = self._make_attachment(b"data", "file.txt", "text/plain")
        interaction = make_mock_interaction(command_name="makezip")
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.text = AsyncMock(return_value="https://litterbox.catbox.moe/x.zip")
            mock_session = AsyncMock()
            mock_session.post = AsyncMock(return_value=mock_resp)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session
            with patch("aiohttp.FormData"):
                await _cmd(self.cog.makezip_cmd)(self.cog, interaction, att)
        interaction.followup.send.assert_called_once()


# =========================================================================
#  15. COG SETUP FUNCTIONS
# =========================================================================

class TestCogSetup(unittest.IsolatedAsyncioTestCase):
    async def _test_setup(self, module_name):
        mod = __import__(f"components.{module_name}", fromlist=["*"])
        setup_fn = getattr(mod, "setup")
        bot = MagicMock(spec=commands.Bot)
        bot.add_cog = AsyncMock()
        bot.loop = asyncio.get_event_loop()
        with patch("Ediscord.db.get_pool", new_callable=AsyncMock, return_value=make_mock_pool()), \
             patch("Ediscord.db.load_cached_settings", new_callable=AsyncMock), \
             patch("Ediscord.db.save_cached_settings", new_callable=AsyncMock), \
             patch("Ediscord.db.push_mod_event", new_callable=AsyncMock):
            await setup_fn(bot)
        bot.add_cog.assert_called_once()

    async def test_setup_general(self):
        await self._test_setup("general")

    async def test_setup_leveling(self):
        await self._test_setup("leveling")

    async def test_setup_moderation(self):
        await self._test_setup("moderation")

    async def test_setup_afk(self):
        await self._test_setup("afk")

    async def test_setup_reminders(self):
        await self._test_setup("reminders")

    async def test_setup_welcomer(self):
        await self._test_setup("welcomer")

    async def test_setup_verification(self):
        await self._test_setup("verification")

    async def test_setup_tickets(self):
        await self._test_setup("tickets")

    async def test_setup_social_alerts(self):
        await self._test_setup("social_alerts")

    async def test_setup_global_chat(self):
        await self._test_setup("global_chat")

    async def test_setup_invite_tracker(self):
        await self._test_setup("invite_tracker")

    async def test_setup_members(self):
        await self._test_setup("members")

    async def test_setup_giveaways(self):
        await self._test_setup("giveaways")

    async def test_setup_automation_engine(self):
        await self._test_setup("automation_engine")

    async def test_setup_ai(self):
        await self._test_setup("ai")

    async def test_setup_autoresponder(self):
        await self._test_setup("autoresponder")

    async def test_setup_music(self):
        await self._test_setup("music")

    async def test_setup_sticky(self):
        await self._test_setup("sticky")

    async def test_setup_utilities(self):
        await self._test_setup("utilities")


# =========================================================================
#  16. EDGE CASES
# =========================================================================

class TestEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_leveling_progress_bar_edge_cases(self):
        from components.leveling import create_progress_bar
        bar = create_progress_bar(0, 0, 20)
        self.assertEqual(len(bar), 20)
        bar = create_progress_bar(100, 100, 20)
        self.assertEqual(len(bar), 20)
        bar = create_progress_bar(200, 100, 20)
        self.assertEqual(len(bar), 20)

    async def test_reminders_parse_mixed_units(self):
        from components.reminders import _parse_when
        now = datetime.datetime(2024, 6, 15, 12, 0, 0)
        dt, err = _parse_when("1h30m", now)
        self.assertIsNone(err)
        self.assertEqual(dt, now + datetime.timedelta(hours=1, minutes=30))

    async def test_moderation_format_duration_boundary(self):
        from components.moderation import format_duration
        self.assertEqual(format_duration(60), "1 hour")
        self.assertEqual(format_duration(59), "59 minutes")
        self.assertEqual(format_duration(1440), "24 hours")

    async def test_sticky_helpers(self):
        from components.sticky import _skey
        self.assertEqual(_skey(123, "enabled"), "more_sticky_enabled_123")
        self.assertEqual(_skey(123, "messages"), "more_sticky_messages_123")


# =========================================================================
#  17. BUILDER / EMBED UTILITIES
# =========================================================================

class TestBuilders(unittest.TestCase):
    def test_embed_builder_basic(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().title("Test").description("Desc").color("blue").build()
        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, "Test")

    def test_embed_builder_fields(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().field("Name", "Value", inline=True).field("Name2", "Value2", inline=False).build()
        self.assertEqual(len(embed.fields), 2)

    def test_embed_builder_footer(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().footer("Test footer").build()
        self.assertEqual(embed.footer.text, "Test footer")

    def test_embed_builder_thumbnail(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().thumbnail("https://example.com/thumb.png").build()
        self.assertIsNotNone(embed.thumbnail)

    def test_embed_builder_image(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().image("https://example.com/image.png").build()
        self.assertIsNotNone(embed.image)

    def test_embed_builder_row(self):
        from Ediscord.builders import EmbedBuilder
        # row() pads with zero-width spaces to fill column grid
        embed = EmbedBuilder().row(("F1", "V1"), ("F2", "V2")).build()
        self.assertEqual(len(embed.fields), 2)

    def test_embed_builder_row_odd(self):
        from Ediscord.builders import EmbedBuilder
        embed = EmbedBuilder().row(("F1", "V1"), ("F2", "V2"), ("F3", "V3")).build()
        # 3 fields + 1 padding = 4
        self.assertEqual(len(embed.fields), 4)

    def test_embed_builder_color_named(self):
        from Ediscord.builders import EmbedBuilder
        for color_name in ["red", "green", "blue", "orange", "gray"]:
            embed = EmbedBuilder().color(color_name).build()
            self.assertIsNotNone(embed.color)

    def test_emoji_title(self):
        from Ediscord.builders import emoji_title
        result = emoji_title("ban", "Test")
        self.assertIn("Test", result)

    def test_brand_colors(self):
        from Ediscord.builders import BRAND, SUCCESS, ERROR, WARN, INFO
        self.assertIsInstance(BRAND, int)
        self.assertIsInstance(SUCCESS, int)
        self.assertIsInstance(ERROR, int)
        self.assertIsInstance(WARN, int)
        self.assertIsInstance(INFO, int)


# =========================================================================
#  18. DB MODULE TESTS
# =========================================================================

class TestDBModule(unittest.TestCase):
    def test_record_getitem(self):
        from Ediscord.db import Record
        rec = Record(["name", "age"], ("Alice", 30))
        self.assertEqual(rec["name"], "Alice")
        self.assertEqual(rec["age"], 30)

    def test_record_get(self):
        from Ediscord.db import Record
        rec = Record(["name"], ("Alice",))
        self.assertEqual(rec.get("name"), "Alice")
        self.assertIsNone(rec.get("missing"))
        self.assertEqual(rec.get("missing", "default"), "default")

    def test_record_contains(self):
        from Ediscord.db import Record
        rec = Record(["name"], ("Alice",))
        self.assertIn("name", rec)
        self.assertNotIn("age", rec)

    def test_record_keys_values(self):
        from Ediscord.db import Record
        rec = Record(["a", "b"], (1, 2))
        self.assertEqual(list(rec.keys()), ["a", "b"])
        self.assertEqual(tuple(rec.values()), (1, 2))

    def test_record_len(self):
        from Ediscord.db import Record
        rec = Record(["a", "b", "c"], (1, 2, 3))
        self.assertEqual(len(rec), 3)

    def test_record_iter(self):
        from Ediscord.db import Record
        rec = Record(["x", "y"], (10, 20))
        items = dict(rec)
        self.assertEqual(items, {"x": 10, "y": 20})

    def test_record_repr(self):
        from Ediscord.db import Record
        rec = Record(["a"], (1,))
        self.assertIn("Record", repr(rec))

    def test_to_http_url_libsql(self):
        from Ediscord.db import _to_http_url
        self.assertTrue(_to_http_url("libsql://my-db.turso.io").startswith("https://"))

    def test_to_http_url_ws(self):
        from Ediscord.db import _to_http_url
        self.assertTrue(_to_http_url("ws://localhost:8080").startswith("http://"))

    def test_to_http_url_wss(self):
        from Ediscord.db import _to_http_url
        self.assertTrue(_to_http_url("wss://example.com").startswith("https://"))

    def test_to_http_url_passthrough(self):
        from Ediscord.db import _to_http_url
        self.assertEqual(_to_http_url("https://example.com/v2/pipeline"), "https://example.com/v2/pipeline")


# =========================================================================
#  19. CACHE MODULE TESTS
# =========================================================================

class TestCache(unittest.IsolatedAsyncioTestCase):
    async def test_cache_set_get(self):
        from Ediscord.cache import AsyncTTLCache
        cache = AsyncTTLCache(default_ttl=60, maxsize=100)
        await cache.set("key1", "value1")
        result = await cache.get("key1")
        self.assertEqual(result, "value1")

    async def test_cache_miss(self):
        from Ediscord.cache import AsyncTTLCache
        cache = AsyncTTLCache(default_ttl=60, maxsize=100)
        result = await cache.get("missing")
        self.assertIsNone(result)

    async def test_cache_eviction(self):
        from Ediscord.cache import AsyncTTLCache
        cache = AsyncTTLCache(default_ttl=60, maxsize=2)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        result_a = await cache.get("a")
        result_b = await cache.get("b")
        result_c = await cache.get("c")
        self.assertIsNone(result_a)
        self.assertEqual(result_b, 2)
        self.assertEqual(result_c, 3)


# =========================================================================
#  20. VARIABLES MODULE TESTS
# =========================================================================

class TestVariables(unittest.TestCase):
    def test_version_format(self):
        from Ediscord import variables
        parts = variables.__version__.split(".")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertTrue(part.isdigit())

    def test_color_map(self):
        from Ediscord import variables
        self.assertIn("red", variables.COLOR_MAP)
        self.assertIn("blue", variables.COLOR_MAP)
        self.assertIn("green", variables.COLOR_MAP)

    def test_intents(self):
        from Ediscord import variables
        self.assertTrue(variables.intents.guilds)
        self.assertTrue(variables.intents.members)
        self.assertTrue(variables.intents.message_content)


# =========================================================================
#  RUNNER
# =========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
