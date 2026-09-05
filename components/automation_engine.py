import discord
from discord.ext import commands
import asyncio
import datetime
import json
import re
import time

from Ediscord import logger, EmbedBuilder
from Ediscord.builders import emoji_title
from Ediscord import db as neon_db


NODE_DEFS = {
    "member_join": {"kind": "trigger"},
    "member_leave": {"kind": "trigger"},
    "message_any": {"kind": "trigger"},
    "message_contains": {"kind": "trigger", "fields": [{"key": "text"}]},
    "message_starts": {"kind": "trigger", "fields": [{"key": "text"}]},
    "message_ends": {"kind": "trigger", "fields": [{"key": "text"}]},
    "role_added": {"kind": "trigger", "fields": [{"key": "role_id"}]},
    "role_removed": {"kind": "trigger", "fields": [{"key": "role_id"}]},
    "channel_message": {"kind": "trigger", "fields": [{"key": "channel_id"}]},
    "send_message": {"kind": "action", "fields": [{"key": "channel_id"}, {"key": "text"}]},
    "send_dm": {"kind": "action", "fields": [{"key": "text"}]},
    "add_role": {"kind": "action", "fields": [{"key": "role_id"}]},
    "remove_role": {"kind": "action", "fields": [{"key": "role_id"}]},
    "kick": {"kind": "action", "fields": [{"key": "text"}]},
    "ban": {"kind": "action", "fields": [{"key": "text"}]},
    "mute": {"kind": "action", "fields": [{"key": "duration"}]},
    "set_nickname": {"kind": "action", "fields": [{"key": "text"}]},
    "send_ticket": {"kind": "action", "fields": [{"key": "channel_id"}, {"key": "title"}, {"key": "text"}]},
    "log_block": {"kind": "action", "fields": [{"key": "text"}]},
    "log_channel": {"kind": "action", "fields": [{"key": "channel_id"}, {"key": "text"}]},
    "repeat": {"kind": "flow", "fields": [{"key": "count"}]},
    "wait_for": {"kind": "flow", "fields": [{"key": "waitType"}, {"key": "duration"}, {"key": "condition"}]},
    "end_block": {"kind": "flow"},
    "sys_number": {"kind": "variable"},
    "sys_role": {"kind": "variable"},
    "sys_channel": {"kind": "variable"},
    "set_var": {"kind": "variable", "fields": [{"key": "name"}, {"key": "varType"}, {"key": "value"}]},
    "modify_var": {"kind": "variable", "fields": [{"key": "name"}, {"key": "modOp"}, {"key": "value"}]},
    "list_var": {"kind": "variable", "fields": [{"key": "name"}, {"key": "items"}]},
    "modify_list": {"kind": "variable", "fields": [{"key": "name"}, {"key": "listOp"}, {"key": "value"}]},
    "list_members": {"kind": "variable"},
    "list_roles": {"kind": "variable"},
    "list_channels": {"kind": "variable"},
}

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 30


class AutomationEngine(commands.Cog, name="AutomationEngine"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._graph_cache = {}
        self._graph_cache_ts = {}
        self._rate_buckets = {}

    async def _load_graph(self, guild_id: int):
        now = time.time()
        cached = self._graph_cache.get(guild_id)
        if cached is not None and now - self._graph_cache_ts.get(guild_id, 0) < 30:
            return cached
        pool = await neon_db.get_pool()
        if not pool:
            return None
        try:
            row = await pool.fetchrow(
                "SELECT nodes, connections FROM automation_graph WHERE guild_id = ?",
                str(guild_id),
            )
        except Exception as e:
            logger.warning(f"Failed to load automation graph for {guild_id}: {e}")
            return None
        if not row:
            self._graph_cache[guild_id] = None
            self._graph_cache_ts[guild_id] = now
            return None
        nodes_raw = row["nodes"] or []
        conns_raw = row["connections"] or []
        if isinstance(nodes_raw, str):
            try:
                nodes_raw = json.loads(nodes_raw)
            except (json.JSONDecodeError, TypeError):
                nodes_raw = []
        if isinstance(conns_raw, str):
            try:
                conns_raw = json.loads(conns_raw)
            except (json.JSONDecodeError, TypeError):
                conns_raw = []
        graph = {"nodes": [n for n in nodes_raw if isinstance(n, dict) and "type" in n], "connections": conns_raw if isinstance(conns_raw, list) else []}
        self._graph_cache[guild_id] = graph
        self._graph_cache_ts[guild_id] = now
        return graph

    def _invalidate_cache(self, guild_id: int):
        self._graph_cache.pop(guild_id, None)
        self._graph_cache_ts.pop(guild_id, None)

    def _check_rate(self, guild_id: int) -> bool:
        now = time.time()
        bucket = self._rate_buckets.setdefault(guild_id, [])
        bucket[:] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True

    def _node_by_id(self, graph, nid):
        for n in graph["nodes"]:
            if isinstance(n, dict) and n.get("id") == nid:
                return n
        return None

    def _connections_from(self, graph, node_id, from_dot=None):
        result = []
        for c in graph["connections"]:
            if c["from"] == node_id:
                if from_dot is None or c.get("fromDot") == from_dot:
                    result.append(c)
        return result

    def _connections_to(self, graph, node_id):
        return [c for c in graph["connections"] if c["to"] == node_id]

    def _matches_trigger(self, node, event_type, member=None, message=None, before=None, after=None):
        if not isinstance(node, dict):
            return False
        t = node.get("type", "")
        cfg = node.get("config", {})
        if t == "member_join":
            return event_type == "member_join"
        if t == "member_leave":
            return event_type == "member_leave"
        if t == "message_any":
            return event_type == "message"
        if t == "message_contains":
            if event_type != "message" or not message or not message.content:
                return False
            text = cfg.get("text", "")
            return text.lower() in message.content.lower()
        if t == "message_starts":
            if event_type != "message" or not message or not message.content:
                return False
            return message.content.lower().startswith(cfg.get("text", "").lower())
        if t == "message_ends":
            if event_type != "message" or not message or not message.content:
                return False
            return message.content.lower().endswith(cfg.get("text", "").lower())
        if t == "role_added":
            if event_type != "role_change" or not before or not after:
                return False
            rid = str(cfg.get("role_id", ""))
            added = set(r.id for r in after.roles) - set(r.id for r in before.roles)
            return int(rid) in added if rid.isdigit() else False
        if t == "role_removed":
            if event_type != "role_change" or not before or not after:
                return False
            rid = str(cfg.get("role_id", ""))
            removed = set(r.id for r in before.roles) - set(r.id for r in after.roles)
            return int(rid) in removed if rid.isdigit() else False
        if t == "channel_message":
            if event_type != "message" or not message:
                return False
            return str(message.channel.id) == str(cfg.get("channel_id", ""))
        return False

    def _build_context(self, guild, member=None, channel=None, message=None, role=None, before=None, after=None):
        ctx = {
            "member": member, "server": guild, "channel": channel,
            "message": message, "role": role, "before": before, "after": after,
            "custom": {},
        }
        if member:
            ctx["member.name"] = member.name
            ctx["member.id"] = str(member.id)
            ctx["member.mention"] = member.mention
            ctx["member.nick"] = member.display_name
        if guild:
            ctx["server.name"] = guild.name
            ctx["server.id"] = str(guild.id)
            ctx["server.membercount"] = str(guild.member_count)
        if channel:
            ctx["channel"] = channel.mention
            ctx["channel.name"] = channel.name
            ctx["channel.id"] = str(channel.id)
        if message:
            ctx["message"] = message.content
            ctx["message.content"] = message.content
        if role:
            ctx["role"] = role.mention
            ctx["role.name"] = role.name
            ctx["role.id"] = str(role.id)
        ctx["time"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        return ctx

    def _gather_variables(self, graph, trigger_node, ctx):
        incoming = self._connections_to(graph, trigger_node["id"])
        for conn in incoming:
            var_node = self._node_by_id(graph, conn["from"])
            if not var_node:
                continue
            vdef = NODE_DEFS.get(var_node.get("type", ""), {})
            if vdef.get("kind") != "variable":
                continue
            cfg = var_node.get("config", {})
            vtype = cfg.get("varType", "Text")
            vname = cfg.get("name", "")
            vkey = f"var:{vname}" if vname else f"var:{var_node['id']}"
            raw = cfg.get("value", "")
            if vtype == "Boolean":
                ctx["custom"][vkey] = raw is True or raw == "true"
            elif vtype == "Number":
                try:
                    ctx["custom"][vkey] = float(raw) if raw else 0
                except (ValueError, TypeError):
                    ctx["custom"][vkey] = 0
            else:
                ctx["custom"][vkey] = str(raw) if raw else ""
        return ctx

    def _resolve(self, text, ctx):
        if not isinstance(text, str):
            return text

        def repl(m):
            key = m.group(1)
            if key in ctx:
                val = ctx[key]
                return val.mention if isinstance(val, discord.Member) or isinstance(val, discord.Role) else str(val)
            if key.startswith("var:"):
                custom_val = ctx.get("custom", {}).get(key)
                if custom_val is not None:
                    return str(custom_val)
            return m.group(0)

        text = re.sub(r"\{([^}]+)\}", repl, text)
        text = re.sub(r"\$\{(var:[^}]+)\}", repl, text)
        return text

    async def _resolve_id(self, value, ctx):
        if isinstance(value, str) and value.startswith("${var:"):
            resolved = self._resolve(value, ctx)
            return resolved
        return str(value) if value else None

    async def walk_and_execute(self, graph, start_id, ctx, visited=None):
        if visited is None:
            visited = set()
        if start_id in visited:
            return
        visited.add(start_id)

        node = self._node_by_id(graph, start_id)
        if not node or not isinstance(node, dict):
            return

        ntype = node.get("type", "")
        cfg = node.get("config", {})
        kind = NODE_DEFS.get(ntype, {}).get("kind", "")

        if kind == "variable":
            if ntype == "set_var":
                vname = cfg.get("name", "")
                if vname:
                    vtype = cfg.get("varType", "Text")
                    raw = cfg.get("value", "")
                    if vtype == "Boolean":
                        ctx["custom"][f"var:{vname}"] = raw is True or raw == "true"
                    elif vtype == "Number":
                        try:
                            ctx["custom"][f"var:{vname}"] = float(raw) if raw else 0
                        except (ValueError, TypeError):
                            ctx["custom"][f"var:{vname}"] = 0
                    else:
                        ctx["custom"][f"var:{vname}"] = str(raw) if raw else ""
            elif ntype == "modify_var":
                vname = cfg.get("name", "")
                if vname:
                    key = f"var:{vname}"
                    current = ctx["custom"].get(key, "")
                    raw = self._resolve(cfg.get("value", ""), ctx)
                    op = cfg.get("modOp", "set")
                    if op == "add":
                        try:
                            ctx["custom"][key] = float(current or 0) + float(raw or 0)
                        except (ValueError, TypeError):
                            ctx["custom"][key] = str(current) + str(raw)
                    else:
                        try:
                            ctx["custom"][key] = float(raw) if raw else 0
                        except (ValueError, TypeError):
                            ctx["custom"][key] = raw
            elif ntype == "list_var":
                vname = cfg.get("name", "")
                if vname:
                    items = [x.strip() for x in cfg.get("items", "").split("\n") if x.strip()]
                    ctx["custom"][f"var:{vname}"] = items
            elif ntype == "modify_list":
                vname = cfg.get("name", "")
                if vname:
                    key = f"var:{vname}"
                    current = ctx["custom"].get(key, [])
                    if not isinstance(current, list):
                        current = []
                    raw = self._resolve(cfg.get("value", ""), ctx)
                    op = cfg.get("listOp", "add")
                    if op == "add":
                        current.append(raw)
                    elif op == "remove":
                        current = [x for x in current if str(x) != str(raw)]
                    elif op == "clear":
                        current = []
                    elif op == "set":
                        current = [raw]
                    ctx["custom"][key] = current
            outgoing = self._connections_from(graph, start_id)
            for conn in outgoing:
                await self.walk_and_execute(graph, conn["to"], ctx, visited)
            return

        if ntype == "end_block":
            return

        if ntype == "repeat":
            count = int(cfg.get("count", 1) or 1)
            body_conns = self._connections_from(graph, start_id, "out-body")
            next_conns = self._connections_from(graph, start_id, "out-next")
            for _ in range(count):
                for conn in body_conns:
                    body_visited = set()
                    await self.walk_and_execute(graph, conn["to"], ctx, body_visited)
                    visited.update(body_visited)
            for conn in next_conns:
                await self.walk_and_execute(graph, conn["to"], ctx, visited)
            return

        if ntype == "wait_for":
            wait_type = cfg.get("waitType", "time")
            if wait_type == "time":
                duration = float(cfg.get("duration", 5) or 5)
                await asyncio.sleep(min(duration, 300))
            outgoing = self._connections_from(graph, start_id)
            for conn in outgoing:
                await self.walk_and_execute(graph, conn["to"], ctx, visited)
            return

        await self._execute_action(ntype, cfg, ctx, visited)

        outgoing = self._connections_from(graph, start_id)
        for conn in outgoing:
            await self.walk_and_execute(graph, conn["to"], ctx, visited)

    async def _execute_action(self, ntype, cfg, ctx, visited):
        guild = ctx.get("server")
        member = ctx.get("member")
        channel = ctx.get("channel")
        message = ctx.get("message")

        try:
            if ntype == "send_message":
                ch_id = await self._resolve_id(cfg.get("channel_id"), ctx)
                if not ch_id:
                    return
                ch = guild.get_channel(int(ch_id)) if ch_id.isdigit() else None
                if not ch:
                    return
                text = self._resolve(cfg.get("text", ""), ctx)
                if text:
                    await ch.send(text[:2000])

            elif ntype == "send_dm":
                if not member:
                    return
                text = self._resolve(cfg.get("text", ""), ctx)
                if text:
                    await member.send(text[:2000])

            elif ntype == "add_role":
                if not member:
                    return
                role_id = await self._resolve_id(cfg.get("role_id"), ctx)
                if not role_id or not role_id.isdigit():
                    return
                role = guild.get_role(int(role_id))
                if role:
                    await member.add_roles(role, reason="Automation")

            elif ntype == "remove_role":
                if not member:
                    return
                role_id = await self._resolve_id(cfg.get("role_id"), ctx)
                if not role_id or not role_id.isdigit():
                    return
                role = guild.get_role(int(role_id))
                if role:
                    await member.remove_roles(role, reason="Automation")

            elif ntype == "kick":
                if not member:
                    return
                reason = self._resolve(cfg.get("text", "Automation kick"), ctx)
                await member.kick(reason=reason[:512])

            elif ntype == "ban":
                if not member:
                    return
                reason = self._resolve(cfg.get("text", "Automation ban"), ctx)
                await member.ban(reason=reason[:512], delete_message_seconds=0)

            elif ntype == "mute":
                if not member:
                    return
                duration = float(cfg.get("duration", 60) or 60)
                until = discord.utils.utcnow() + datetime.timedelta(minutes=min(duration, 40320))
                await member.timeout(until, reason="Automation mute")

            elif ntype == "set_nickname":
                if not member:
                    return
                nick = self._resolve(cfg.get("text", ""), ctx)
                if nick:
                    await member.edit(nick=nick[:32], reason="Automation")

            elif ntype == "send_ticket":
                ch_id = await self._resolve_id(cfg.get("channel_id"), ctx)
                if not ch_id or not ch_id.isdigit():
                    return
                ch = guild.get_channel(int(ch_id))
                if not ch:
                    return
                title = self._resolve(cfg.get("title", "Ticket"), ctx)
                text = self._resolve(cfg.get("text", ""), ctx)
                embed = (
                    EmbedBuilder()
                    .title(emoji_title("ticket", title))
                    .description(text[:4000] if text else "No description")
                    .color("blue")
                    .timestamp(datetime.datetime.utcnow())
                    .build()
                )
                await ch.send(embed=embed)

            elif ntype == "log_block":
                text = self._resolve(cfg.get("text", ""), ctx)
                if text:
                    logger.info(f"[Automation] {guild.id}: {text}")

            elif ntype == "log_channel":
                ch_id = await self._resolve_id(cfg.get("channel_id"), ctx)
                if not ch_id or not ch_id.isdigit():
                    return
                ch = guild.get_channel(int(ch_id))
                if not ch:
                    return
                text = self._resolve(cfg.get("text", ""), ctx)
                if text:
                    embed = (
                        EmbedBuilder()
                        .title(emoji_title("log", "Automation Log"))
                        .description(text[:4000])
                        .color("grey")
                        .timestamp(datetime.datetime.utcnow())
                        .build()
                    )
                    await ch.send(embed=embed)

        except discord.Forbidden:
            logger.warning(f"Automation: missing permissions in {guild.id} for {ntype}")
        except Exception as e:
            logger.warning(f"Automation action {ntype} failed in {guild.id}: {e}")

    async def _log_run(self, guild_id: int):
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return
            bucket = int(time.time() // 3600) * 3600
            await pool.execute(
                "INSERT INTO automation_runs (guild_id, bucket_ts, count) VALUES (?, ?, 1) "
                "ON CONFLICT (guild_id, bucket_ts) DO UPDATE SET count = automation_runs.count + 1",
                str(guild_id), float(bucket),
            )
        except Exception as e:
            logger.warning(f"Failed to log automation run: {e}")

    async def _log_message(self, guild_id: int, message: str):
        try:
            pool = await neon_db.get_pool()
            if not pool:
                return
            await pool.execute(
                "INSERT INTO automation_logs (guild_id, message) VALUES (?, ?)",
                str(guild_id), message[:500],
            )
        except Exception as e:
            logger.warning(f"Failed to log automation message: {e}")

    async def _run_triggers(self, guild, event_type, member=None, message=None, before=None, after=None):
        graph = await self._load_graph(guild.id)
        if not graph or not graph.get("nodes"):
            return
        if not self._check_rate(guild.id):
            return

        triggers = [
            n for n in graph["nodes"]
            if isinstance(n, dict)
            and NODE_DEFS.get(n.get("type"), {}).get("kind") == "trigger"
            and self._matches_trigger(n, event_type, member, message, before, after)
        ]

        for trigger in triggers:
            ctx = self._build_context(guild, member, message.channel if message else None, message, before=before, after=after)
            self._gather_variables(graph, trigger, ctx)
            try:
                await self.walk_and_execute(graph, trigger["id"], ctx)
                await self._log_run(guild.id)
                await self._log_message(
                    guild.id,
                    f"Trigger `{trigger['type']}` fired in #{message.channel.name if message else 'N/A'}"
                    + (f" for {member}" if member else ""),
                )
            except Exception as e:
                logger.warning(f"Automation trigger {trigger['type']} failed in {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not member.guild:
            return
        await self._run_triggers(member.guild, "member_join", member=member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or not member.guild:
            return
        await self._run_triggers(member.guild, "member_leave", member=member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await self._run_triggers(message.guild, "message", message=message, member=message.author)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.bot or not before.guild:
            return
        if set(r.id for r in before.roles) == set(r.id for r in after.roles):
            return
        await self._run_triggers(before.guild, "role_change", member=after, before=before, after=after)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutomationEngine(bot))
