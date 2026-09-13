"""
# Ediscord utils package
Utility package for Discord.

Contains:
- Helper functions for easier maintenance.
"""

import json, os, logging, discord
from discord.ext import commands
from datetime import datetime
import typing
from Ediscord import variables
import asyncio
import time
import sys
from PIL import Image, ImageDraw, ImageFont
import random
import itertools
import psutil
import shutil
import glob

# ---------------------------------------------------------------------------------------------------
# -------------------------------------------- DEFINITIONS ------------------------------------------
# ---------------------------------------------------------------------------------------------------

def atomic_write_json(path, data):
    """Write data to a JSON file atomically to prevent corruption."""
    try:
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logging.error(f"Failed to atomically write to {path}: {e}")
        return False

def load_logging_config():
    """Load logging configuration from the JSON file."""
    try:
        with open(variables.LOGGING_CONFIG_FILE, "r") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}

def save_logging_config(data):
    """Save logging configuration to the JSON file."""
    atomic_write_json(variables.LOGGING_CONFIG_FILE, data)


def is_owner(ctx):
    """Check if the command issuer is the bot owner."""
    return ctx.author.id == 917515232065228890

async def is_owner_async(ctx):
    """Async version for use in command checks."""
    return ctx.author.id == 917515232065228890

def admin_or_owner():
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator or await is_owner_async(ctx)
    return commands.check(predicate)


def save_user_data(data: dict):
    """Save user data with backup and atomic write."""
    try:
        cleaned_data = {}
        for k, v in data.items():
            if k.isdigit():
                cleaned_data[k] = v
            else:
                print(f"Warning: Skipping malformed root key '{k}'")

        os.makedirs(variables.BACKUP_FOLDER, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{timestamp}_user_data.json"
        backup_path = os.path.join(variables.BACKUP_FOLDER, backup_filename)
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(cleaned_data, f, indent=4)

        with open(variables.BACKUP_LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"{datetime.now()}: {backup_filename}\n")

        backups = sorted(os.listdir(variables.BACKUP_FOLDER))
        while len(backups) > variables.MAX_BACKUPS:
            os.remove(os.path.join(variables.BACKUP_FOLDER, backups.pop(0)))

        atomic_write_json(variables.USER_DATA_PATH, cleaned_data)

    except Exception as e:
        print(f"Error saving user data: {e}")

def load_user_data():
    """Load user data from the JSON file."""
    try:
        if not os.path.exists(variables.USER_DATA_FILE):
            return {}

        with open(variables.USER_DATA_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print("Error: user_data.json is corrupted. Initializing with an empty dictionary.")
        return {}
    except Exception as e:
        print(f"Error loading user data: {e}")
        return {}


def normalize_user_data(auto_fix=True):
    """Analyze and optionally fix anomalies in user_data.json.

    Returns a report dict with counts and anomaly messages.
    """
    report = {"checked": 0, "fixed": 0, "anomalies": []}
    data = load_user_data()
    cleaned = {}

    for k, v in list(data.items()):
        report["checked"] += 1
        if not str(k).isdigit():
            report["anomalies"].append(f"root_key_not_digit: {k}")
            continue

        if not isinstance(v, dict):
            report["anomalies"].append(f"user_not_object: {k}")
            if auto_fix:
                cleaned[k] = {
                    "xp": 0, "level": 1, "coins": 100, "gems": 0,
                    "balance": 0, "warnings": [], "censored_count": 0,
                    "strikes": 0, "messages": [],
                }
                report["fixed"] += 1
            continue

        user = v.copy()
        user.setdefault("warnings", [])
        user.setdefault("messages", [])
        user.setdefault("xp", 0)
        user.setdefault("level", 1)
        user.setdefault("coins", 100)
        user.setdefault("gems", 0)
        user.setdefault("balance", 0)
        user.setdefault("censored_count", 0)
        user.setdefault("strikes", 0)

        for num_key in ("xp", "level", "coins", "gems", "balance", "censored_count", "strikes"):
            val = user.get(num_key)
            if isinstance(val, (int, float)):
                user[num_key] = int(val)
            else:
                try:
                    user[num_key] = int(float(str(val)))
                    report["fixed"] += 1
                except Exception:
                    report["anomalies"].append(f"bad_numeric_{num_key}: user={k} value={val}")
                    user[num_key] = 0 if num_key != "level" else 1

        if not isinstance(user.get("messages"), list):
            report["anomalies"].append(f"messages_not_list: user={k}")
            user["messages"] = []
            report["fixed"] += 1

        cleaned[k] = user

    if auto_fix:
        try:
            os.makedirs(os.path.dirname(variables.USER_DATA_PATH), exist_ok=True)
            backup_path = variables.USER_DATA_PATH + ".bak"
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            report["backup"] = backup_path
        except Exception as e:
            report["anomalies"].append(f"backup_failed: {e}")

        try:
            save_user_data(cleaned)
        except Exception as e:
            report["anomalies"].append(f"save_failed: {e}")
    else:
        report["note"] = "dry-run: no files modified"

    return report


def normalize_generic_json_files(auto_fix=True, folder="."):
    """Generic normalizer for basic JSON files (conservative: only scalar coercions)."""
    report = {"checked": 0, "fixed": 0, "anomalies": [], "files": {}}

    candidates = [
        os.path.join(folder, "data", "bank.json"),
    ]
    candidates = [p for p in dict.fromkeys(candidates) if os.path.exists(p)]

    def _coerce_value(v):
        if isinstance(v, int):
            return v, False
        if isinstance(v, float):
            return int(v), True
        if isinstance(v, str):
            try:
                return int(v), True
            except Exception:
                try:
                    return int(float(v)), True
                except Exception:
                    return v, False
        return v, False

    for path in candidates:
        file_report = {"checked": 0, "fixed": 0, "anomalies": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            report["anomalies"].append(f"failed_read:{path}:{e}")
            continue

        report["checked"] += 1
        file_report["checked"] = 1
        modified = False

        if isinstance(raw, dict):
            for k, v in list(raw.items()):
                if isinstance(v, (str, int, float)):
                    new_v, changed = _coerce_value(v)
                    if changed:
                        raw[k] = new_v
                        modified = True
                        file_report["fixed"] += 1
                elif isinstance(v, list):
                    new_list = []
                    changed_any = False
                    for item in v:
                        new_item, changed = _coerce_value(item)
                        new_list.append(new_item)
                        if changed:
                            changed_any = True
                    if changed_any:
                        raw[k] = new_list
                        modified = True
                        file_report["fixed"] += 1
        elif isinstance(raw, list):
            new_list = []
            changed_any = False
            for item in raw:
                if isinstance(item, (str, int, float)):
                    new_item, changed = _coerce_value(item)
                    new_list.append(new_item)
                    if changed:
                        changed_any = True
                else:
                    new_list.append(item)
            if changed_any:
                raw = new_list
                modified = True
                file_report["fixed"] += 1
        else:
            file_report["anomalies"].append(f"top_level_not_object_or_list:{type(raw).__name__}")

        if modified:
            report["fixed"] += file_report["fixed"]
            if auto_fix:
                try:
                    bak = path + ".bak"
                    shutil.copy2(path, bak)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(raw, f, indent=2)
                    file_report["backup"] = bak
                except Exception as e:
                    file_report["anomalies"].append(f"save_failed:{e}")
            else:
                file_report["note"] = "dry-run: no file written"

        report["files"][path] = file_report

    return report


def fix_json_files(target="all", auto_fix=True):
    combined = {"reports": {}, "timestamp": datetime.now().isoformat()}
    if target in ("all", "user_data"):
        combined["reports"]["user_data"] = normalize_user_data(auto_fix=auto_fix)
    if target in ("all", "generic"):
        combined["reports"]["generic"] = normalize_generic_json_files(auto_fix=auto_fix)
    return combined


def backup_file(json_path, max_backups=10):
    """Create a dated backup of the given JSON file, organized by folder, and purge oldest if needed."""
    if not os.path.exists(json_path):
        print(f"Tried to back up missing file: {json_path}")
        return

    filename = os.path.basename(json_path)
    base_name = os.path.splitext(filename)[0]
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    backup_folder = os.path.join("backups", base_name)
    os.makedirs(backup_folder, exist_ok=True)

    backup_filename = f"{timestamp}_{filename}"
    backup_path = os.path.join(backup_folder, backup_filename)

    try:
        shutil.copy2(json_path, backup_path)

        log_path = os.path.join("backups", "backup_log.txt")
        log_entry = f"[{timestamp}] Backed up '{json_path}' to '{backup_path}'\n"
        with open(log_path, "a") as log_file:
            log_file.write(log_entry)

        existing_backups = sorted(glob.glob(os.path.join(backup_folder, f"*_{filename}")))
        if len(existing_backups) > max_backups:
            to_delete = existing_backups[:-max_backups]
            for path in to_delete:
                try:
                    os.remove(path)
                    print(f"Removed old backup: {path}")
                except Exception as e:
                    print(f"Failed to delete backup {path}: {e}")

    except Exception as e:
        print(f"Failed to back up {json_path}: {e}")

def write_bot_data(bot):
    """Write basic bot stats to bot_data.txt (without economy data)."""
    try:
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss // 1024 // 1024
        cpu = process.cpu_percent()
    except ImportError:
        mem = cpu = "N/A"

    total_users = len(bot.users)
    active_users = sum(1 for m in bot.get_all_members() if m.status != discord.Status.offline)
    total_commands = len(bot.tree.get_commands())
    launch_time = getattr(bot, "launch_time", None)
    uptime_seconds = int(time.time() - launch_time) if launch_time else 0
    uptime_str = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime_seconds))
    bot_status = "Running" if bot.is_ready() else "Not Running"
    bot_version = str(getattr(bot, "version", "unknown"))
    python_version = sys.version.replace("\n", " ")
    guilds = list(bot.guilds)
    num_guilds = len(guilds)
    guild_ids = [str(g.id) for g in guilds]
    num_channels = sum(len(g.channels) for g in guilds)
    num_roles = sum(len(g.roles) for g in guilds)
    num_emojis = sum(len(g.emojis) for g in guilds)
    loaded_cogs = list(bot.cogs.keys())
    all_commands = [cmd.name for cmd in bot.tree.get_commands()]
    last_restart = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(launch_time)) if launch_time else "unknown"

    data = (
        f"total_users={total_users}\n"
        f"active_users={active_users}\n"
        f"total_commands={total_commands}\n"
        f"uptime={uptime_str}\n"
        f"bot_status={bot_status}\n"
        f"bot_version={bot_version}\n"
        f"python_version={python_version}\n"
        f"num_guilds={num_guilds}\n"
        f"guild_ids={json.dumps(guild_ids)}\n"
        f"num_channels={num_channels}\n"
        f"num_roles={num_roles}\n"
        f"num_emojis={num_emojis}\n"
        f"loaded_cogs={json.dumps(loaded_cogs)}\n"
        f"all_commands={json.dumps(all_commands)}\n"
        f"memory_usage_mb={mem}\n"
        f"cpu_usage_percent={cpu}\n"
        f"last_restart={last_restart}\n"
    )

    with open(variables.BOT_DATA_FILE, "w", encoding="utf-8") as f:
        f.write(data)

    write_guild_data(bot)


def write_guild_data(bot):
    """Write detailed guild data as JSON for the dashboard."""
    guilds = []
    for guild in bot.guilds:
        icon_url = str(guild.icon.url) if guild.icon else None
        guilds.append({
            "id": guild.id,
            "name": guild.name,
            "icon_url": icon_url,
            "member_count": guild.member_count,
            "online_count": sum(1 for m in guild.members if m.status != discord.Status.offline),
            "channel_count": len(guild.channels),
            "text_channels": len(guild.text_channels),
            "voice_channels": len(guild.voice_channels),
            "role_count": len(guild.roles),
            "emoji_count": len(guild.emojis),
            "created_at": guild.created_at.isoformat(),
            "owner_id": guild.owner_id,
            "members": [{"id": m.id, "name": m.name, "display_name": m.display_name, "avatar": m.display_avatar.key} for m in guild.members],
            "channels": [{"id": c.id, "name": c.name, "type": c.type.value} for c in guild.channels],
            "roles": [{"id": r.id, "name": r.name, "color": r.color.value, "position": r.position, "is_mod": False} for r in guild.roles],
        })

    guild_data_path = os.path.join(os.path.dirname(variables.BOT_DATA_FILE), "data", "guild_data.json")
    atomic_write_json(guild_data_path, {"guilds": guilds})

def get_uptime():
    """Calculate bot uptime."""
    uptime_seconds = time.time() - variables.start_time
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"

def save_bot_info():
    atomic_write_json(variables.bot_info_file, variables.bot_info)

# --- Rounded rectangle helper ---
def rounded_rectangle(draw, xy, radius, fill, outline, width):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

# Centered text helper
def draw_centered_text(draw, rect, text, font, fill):
    x1, y1, x2, y2 = rect
    w, h = draw.textbbox((0, 0), text, font=font)[2:]
    text_x = x1 + ((x2 - x1) - w) // 2
    text_y = y1 + ((y2 - y1) - h) // 2
    draw.text((text_x, text_y), text, font=font, fill=fill)

def draw_centered_outlined_text(draw, rect, text, font, fill, outline, outline_width):
    x1, y1, x2, y2 = rect
    w, h = draw.textbbox((0, 0), text, font=font)[2:]
    text_x = x1 + ((x2 - x1) - w) // 2
    text_y = y1 + ((y2 - y1) - h) // 2
    draw.text(
        (text_x, text_y), text, font=font, fill=fill,
        stroke_width=outline_width, stroke_fill=outline,
    )

def add_rounded_corners(im, rad):
    circle = Image.new("L", (rad * 2, rad * 2), 0)
    draw_c = ImageDraw.Draw(circle)
    draw_c.ellipse((0, 0, rad * 2, rad * 2), fill=255)
    alpha = Image.new("L", im.size, 255)
    w, h = im.size
    alpha.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    alpha.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    alpha.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    alpha.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    im.putalpha(alpha)
    return im

def load_server_settings():
    """Loads server settings from the JSON file."""
    if os.path.exists(variables.SERVER_SETTINGS_FILE):
        with open(variables.SERVER_SETTINGS_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: {variables.SERVER_SETTINGS_FILE} is empty or corrupted. Starting with empty settings.")
                return {}
    return {}

def save_server_settings(data):
    """Saves server settings to the JSON file."""
    with open(variables.SERVER_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_guild_setting(guild_id, key, default=None):
    """Retrieves a specific setting for a guild."""
    settings = load_server_settings()
    return settings.get(str(guild_id), {}).get(key, default)

def set_guild_setting(guild_id, key, value):
    """Sets a specific setting for a guild."""
    settings = load_server_settings()
    if str(guild_id) not in settings:
        settings[str(guild_id)] = {}
    settings[str(guild_id)][key] = value
    save_server_settings(settings)

def get_guild_welcome_message(guild_id):
    settings = load_server_settings()
    return settings.get(str(guild_id), {}).get("welcome_message", None)

def set_guild_welcome_message(guild_id, message):
    settings = load_server_settings()
    guild_settings = settings.get(str(guild_id), {})
    guild_settings["welcome_message"] = message
    settings[str(guild_id)] = guild_settings
    save_server_settings(settings)

def get_guild_goodbye_message(guild_id):
    settings = load_server_settings()
    return settings.get(str(guild_id), {}).get("goodbye_message", None)

def set_guild_goodbye_message(guild_id, message):
    settings = load_server_settings()
    guild_settings = settings.get(str(guild_id), {})
    guild_settings["goodbye_message"] = message
    settings[str(guild_id)] = guild_settings
    save_server_settings(settings)


def is_insider_server(guild_id: int) -> bool:
    if not os.path.exists(variables.insider_FILE):
        return False
    with open(variables.insider_FILE, "r", encoding="utf-8") as f:
        try:
            servers = json.load(f)
            return guild_id in servers
        except json.JSONDecodeError:
            return False

def load_insider_servers():
    if not os.path.exists(variables.insider_FILE):
        return []
    with open(variables.insider_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_insider_servers(servers):
    with open(variables.insider_FILE, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)

def load_scheduled_messages():
    if not os.path.exists(variables.SCHEDULED_MSGS_FILE):
        return {}
    with open(variables.SCHEDULED_MSGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_scheduled_messages(data):
    with open(variables.SCHEDULED_MSGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def little_unknowncommand_variant():
    messages = [
        "Hmm... that command doesn't exist. Did you spell it right?",
        "I don't recognize that command.",
        "That's not a thing. Check your spelling?",
        "Command not found! But hey, nobody's perfect.",
        "Nope, not a command.",
        "Did you just make that up?",
        "That command doesn't exist, but I appreciate the creativity.",
        "Ouch! That doesn't look right. Want to double-check?",
        "Not sure what to do with that.",
        "Command not found.",
    ]
    return random.choice(messages)

def little_error_variant():
    messages = [
        "Something went wrong.",
        "That didn't work. Try again?",
        "An error occurred.",
        "Ouch! That hurt.",
        "Well that failed miserably...",
        "The command broke! Try again.",
        "I tried my best. It wasn't good enough.",
        "Nope. Still doesn't work.",
        "Hmm. Not sure what happened there.",
        "Something broke. Try again.",
    ]
    return random.choice(messages)

def little_unsure_variant():
    messages = [
        "You sure about that?",
        "That's a bold move.",
        "Well... okay then.",
        "I wouldn't do that, but go off I guess.",
        "This could backfire. Just saying.",
        "Hmm... interesting choice.",
        "Alright... if you're really sure.",
        "Well, alrighty then.",
        "Proceeding... cautiously.",
    ]
    return random.choice(messages)

def welcome_message_random():
    messages = [
        "Welcome! Glad you're here!",
        "Hey there! Welcome to the server!",
        "Welcome aboard!",
        "Glad you could join us!",
        "Hello and welcome!",
        "Make yourself at home!",
        "Welcome to the community!",
        "Great to have you here!",
    ]
    return random.choice(messages)

def goodbye_message_random():
    messages = [
        "Goodbye! See you around.",
        "Welp, goodbye.",
        "We hope you enjoyed your stay.",
        "See you later!",
        "Goodbye! Come back soon!",
        "Farewell, friend!",
        "Take care! We'll miss you!",
        "Safe travels!",
        "It's sad seeing you go...",
        "See you next time!",
    ]
    return random.choice(messages)

def little_try_again_variant():
    messages = [
        "Give it another shot?",
        "Try again, maybe?",
        "You got this!",
        "Want to try that one more time?",
        "Oops! Wanna try again?",
        "Could be a typo... go again!",
        "Don't give up yet!",
        "Retry, retry, retry!",
        "That one got away. Try once more?",
        "Failure is the first step to greatness!",
    ]
    return random.choice(messages)

def get_user(user_data, user_id: str) -> dict:
    """Ensures the user ID exists in the data with all required keys."""
    if user_id not in user_data:
        user_data[user_id] = {
            "xp": 0, "level": 1, "coins": 100, "gems": 0,
            "balance": 0, "warnings": [], "censored_count": 0, "strikes": 0
        }
    else:
        defaults = {
            "xp": 0, "level": 1, "coins": 100, "gems": 0,
            "balance": 0, "warnings": [], "censored_count": 0, "strikes": 0
        }
        for key, default in defaults.items():
            user_data[user_id].setdefault(key, default)

    return user_data[user_id]


# ---------------------------------------------------------------------------------------------------
# --------------------------------------- ASYNC DEFINITIONS -----------------------------------------
# ---------------------------------------------------------------------------------------------------

async def change_status(bot):
    """Rotate statuses dynamically or use a custom status."""
    statuses = itertools.cycle(
        [
            discord.Game("playing with commands"),
            discord.Activity(
                type=discord.ActivityType.watching,
                name="the server",
            ),
            discord.Game("Prowl v" + variables.__version__),
            discord.Activity(
                type=discord.ActivityType.listening,
                name="for commands",
            ),
        ]
    )
    while True:
        if variables.is_sleeping:
            await asyncio.sleep(10)
            continue

        if variables.custom_status:
            await bot.change_presence(
                status=discord.Status.online, activity=variables.custom_status
            )
        else:
            current_status = next(statuses)
            await bot.change_presence(
                status=discord.Status.online, activity=current_status
            )
        await asyncio.sleep(360)

        await asyncio.sleep(60)

async def log_event(guild, message):
    """Log an event to the logs channel if logging is enabled."""
    logging_config = load_logging_config()
    guild_id = str(guild.id)

    if logging_config.get(guild_id, False):
        logs_channel = discord.utils.get(guild.text_channels, name="logs")
        if logs_channel:
            try:
                await logs_channel.send(message)
            except discord.Forbidden:
                print(f"Unable to send message to the logs channel in {guild.name}.")
