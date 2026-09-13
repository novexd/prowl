import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from Ediscord import logger
from Ediscord import db as neon_db


ALIAS_DEFAULTS = {
    "enabled": False,
    "aliases": {},
}

# Must match the website's validation (website/api/index.py _sanitize_aliases)
ALIAS_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class CommandAliases(commands.Cog):
    """Per-guild slash command aliases.

    The dashboard stores alias -> command-path mappings in alias_settings.
    This cog registers renamed copies of the real commands scoped to that
    guild (Discord merges guild-scoped commands with global ones), so typing
    e.g. /b invokes the same callback as /ban with identical checks/params.
    """

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> {alias: target} currently registered at Discord
        self._applied = {}
        self._task = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._refresh_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _refresh_loop(self):
        await self.bot.wait_until_ready()
        # Give start.py's initial global tree.sync() a moment to finish first
        await asyncio.sleep(20)
        while not self.bot.is_closed():
            try:
                await self.refresh_all()
            except Exception as e:
                logger.warning(f"Alias refresh failed: {e}")
            await asyncio.sleep(60)

    async def refresh_all(self):
        pool = await neon_db.get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT guild_id, settings FROM alias_settings")
        for row in rows:
            gid = str(row["guild_id"])
            settings = neon_db.parse_settings(row["settings"], ALIAS_DEFAULTS)
            raw = settings.get("aliases") or {} if settings.get("enabled") else {}
            desired = {}
            if isinstance(raw, dict):
                for name, entry in raw.items():
                    if isinstance(entry, dict) and entry.get("target"):
                        desired[str(name)] = str(entry["target"])
            if self._applied.get(gid) == desired:
                continue
            await self._apply(gid, desired)
            self._applied[gid] = desired

    def _find_command(self, path: str):
        parts = [p for p in path.split() if p]
        if not parts:
            return None
        cmd = self.bot.tree.get_command(parts[0])
        for part in parts[1:]:
            if isinstance(cmd, app_commands.Group):
                cmd = cmd.get_command(part)
            elif cmd is not None:
                return None
        return cmd if isinstance(cmd, app_commands.Command) else None

    def _duplicate(self, src: app_commands.Command, name: str) -> app_commands.Command:
        binding = getattr(src, "binding", None)
        dup = src._copy_with(
            parent=None,
            binding=binding,
            bindings={binding: binding} if binding is not None else {},
            set_on_binding=False,
        )
        dup.name = name
        return dup

    async def _apply(self, guild_id: str, desired: dict):
        guild = discord.Object(id=int(guild_id))
        tree = self.bot.tree
        tree.clear_commands(guild=guild)
        added = 0
        for name, target in desired.items():
            if not ALIAS_NAME_RE.match(name):
                continue
            src = self._find_command(target)
            if src is None:
                logger.warning(f"Alias /{name} in guild {guild_id}: command '{target}' not found, skipping")
                continue
            dup = self._duplicate(src, name)
            try:
                tree.add_command(dup, guild=guild)
                added += 1
            except app_commands.errors.CommandAlreadyRegistered:
                logger.warning(f"Alias /{name} in guild {guild_id} conflicts with an existing command")
        try:
            await tree.sync(guild=guild)
            logger.info(f"Aliases synced for guild {guild_id}: {added} alias(es)")
        except Exception as e:
            logger.warning(f"Alias sync failed for guild {guild_id}: {e}")


async def setup(bot):
    await bot.add_cog(CommandAliases(bot))
