"""
# Ediscord variables package
Variable package for Ediscord.

Contains:
- All global variables for easier access and maintenance.
"""

import time
from os import environ
from datetime import datetime
import os
import discord
import logging
import json
from discord.ext.commands import CooldownMapping, BucketType

# --------------------- CONSTANT VARIABLES --------------------
__version__ = "1.6.3"
SPAM_THRESHOLD = 4
TIME_WINDOW = 5

# ── API Keys ──
OPENAI_API_KEY = environ.get("OPENAI_API_KEY", "")
TWITCH_CLIENT_ID = environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = environ.get("TWITCH_CLIENT_SECRET", "")
TWITTER_BEARER_TOKEN = environ.get("TWITTER_BEARER_TOKEN", "")
NITTER_INSTANCE = environ.get("NITTER_INSTANCE", "https://nitter.net")
EASTER_FILE = "data/easter.json"
TROPHY_FILE = "data/trophies.json"
BOT_DATA_FILE = "bot_data.txt"
WEBSITE_COMMANDS_FILE = "website_commands.txt"
LIMITATIONS_FILE = "data/limitations.json"
LOGGING_CONFIG_FILE = "data/logging_config.json"
BANK_FILE = "data/bank.json"
INVENTORY_FILE = "data/inventory.json"
WELCOME_KEYWORDS = ["welcome", "start", "new-member", "greetings", "hello",
                    "WELCOME", "START", "New-Member", "GREETINGS", "HELLO"]
GOODBYE_KEYWORDS = ["goodbye", "departure", "leaving", "leave",
                    "GOODBYE", "DEPARTURE", "LEAVING", "LEAVE"]
USER_DATA_FILE = "data/user_data.json"
COGS_DIR = "components"
DATA_DIR = "../default/data"
SERVER_SETTINGS_FILE = "data/server_settings.json"
SCHEDULED_MSGS_FILE = "data/scheduled_messages.json"
COLOR_MAP = {
    "red": 0xFF0000,
    "orange": 0xFFA500,
    "yellow": 0xFFFF00,
    "green": 0x00FF00,
    "blue": 0x0000FF,
    "violet": 0x8A2BE2,
    "white": 0xFFFFFF,
    "black": 0x000000,
    "brown": 0x8B4513,
    "cyan": 0x00FFFF,
    "magenta": 0xFF00FF,
    "lightblue": 0xADD8E6,
    "pink": 0xFFC0CB,
    "grey": 0x808080,
}
HEX_REGEX = r"^#?([0-9a-fA-F]{6})$"
IS_LOCKDOWN = False
USER_DATA_PATH = "data/user_data.json"
BACKUP_FOLDER = "backups/user_data/"
BACKUP_LOG_FILE = "backups/backup_log.txt"
MAX_BACKUPS = 10
AUTO_ROLE_PATH = os.path.join(os.path.dirname(__file__), "../data/autoroles.json")
GIVEAWAY_PATH = os.path.join(os.path.dirname(__file__), "../data/giveaways.json")
INVENTORY_PATH = os.path.join(os.path.dirname(__file__), "../data/inventory.json")
SWEAR_JSON_PATH = os.path.join(os.path.dirname(__file__), "../data/swearwords.json")

# --------------------- VARIABLES --------------------
disabled_variants = set()
total_commands = 0
insider_FILE = "data/insider_servers.json"
start_time = time.time()
is_sleeping = False
custom_status = None
token = os.environ["TOKEN"]
last_activity_time = datetime.now()
trophies = {
    "trophy_1": {"name": "Coin Collector", "goal": "Collect 1,000 coins", "icon": "icons/coin_collector.png"},
    "trophy_2": {"name": "Gem Hoarder", "goal": "Collect 10 gems", "icon": "icons/gem_hoarder.png"},
    "trophy_3": {"name": "Impossible Victor", "goal": "Win 10 Impossible Easter fights", "icon": "icons/impossible_victor.png"},
    "trophy_4": {"name": "Level Master", "goal": "Reach Level 50", "icon": "icons/level_master.png"},
    "trophy_5": {"name": "Crate Opener", "goal": "Open 50 crates", "icon": "icons/crate_opener.png"},
}
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.reactions = True
intents.message_content = True
intents.invites = True
banned_servers_file = "data/banned_servers.json"
server_restrictions_file = "data/server_restrictions.json"
bot_info_file = "data/bot_info.json"
SUPPORT_STATUS_CHANNEL_ID = 1447688628808716298
CHANGELOG_CHANNEL_ID = 1447688630171598939
game_ongoing = False
board = [" " for _ in range(9)]
custom_cooldown = CooldownMapping.from_cooldown(1, 10, BucketType.user)
current_status = None
level_roles = {
    5: "[🌱 Novice]",
    10: "[🔰 Apprentice]",
    20: "[⚔️ Expert]",
    30: "[🏆 Master]",
    50: "[👑 Grandmaster]",
    100: "[💬 God of talking]"
}
last_activity = {}
message_cooldowns = {}
afk_users = {}
welcome_messages = {}
ffmpeg_path = r""

def get_qa_pipeline():
    """Lazy-load the GPT-2 pipeline to avoid slow startup."""
    if not hasattr(get_qa_pipeline, "_cache"):
        from transformers.pipelines import pipeline
        get_qa_pipeline._cache = pipeline("text-generation", model="gpt2")
    return get_qa_pipeline._cache

def get_translator():
    """Lazy-load the Google Translator to avoid slow startup."""
    if not hasattr(get_translator, "_cache"):
        from googletrans import Translator
        get_translator._cache = Translator()
    return get_translator._cache

user_strikes = {}
logger = logging.getLogger(__name__)
bubble_text = "Welcome!"
bubble_x, bubble_y = 670, 60
bubble_w, bubble_h = 170, 50
bubble_rect = (bubble_x, bubble_y, bubble_x + bubble_w, bubble_y + bubble_h)

# --------------------- CONDITIONAL VARIABLES --------------------
if os.path.exists("data/user_data.json"):
    with open("data/user_data.json", "r") as f:
        user_data = json.load(f)
else:
    user_data = {}

if os.path.exists(bot_info_file):
    try:
        with open(bot_info_file, "r") as f:
            bot_info = json.load(f)
    except json.JSONDecodeError:
        bot_info = {"version": "1.0.0", "new_stuff": "Initial release"}
else:
    bot_info = {"version": "1.0.0", "new_stuff": "Initial release"}

