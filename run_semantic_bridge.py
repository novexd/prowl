"""
Standalone launcher for the semantic-search bridge (local testing only).

Boots ONLY the aiohttp HTTP bridge - no Discord token / guilds required. The
/semantic-search route does not depend on the bot object, so this is enough to
exercise the real model + cache + endpoint end-to-end.

Usage:
    cd cli
    # in .env.local: SEMANTIC_SEARCH_ENABLED=true  BOT_HTTP_TOKEN=test
    pip install requests aiohttp python-dotenv
    python run_semantic_bridge.py
"""
import os
import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env.local")
load_dotenv(Path(__file__).parent / ".env")

# The Ediscord package imports require these at import time; dummy values are
# fine because we never actually connect to Discord in bridge-only mode.
os.environ.setdefault("TOKEN", "dummy")
os.environ.setdefault("DATABASE_URL", "postgres://dummy/dummy")

import Ediscord.http_bridge as bridge  # noqa: E402


async def main():
    if not os.environ.get("BOT_HTTP_TOKEN"):
        print("Set BOT_HTTP_TOKEN in cli/.env.local first.")
        return
    if os.environ.get("SEMANTIC_SEARCH_ENABLED", "").lower() not in ("1", "true", "yes", "on"):
        print("Set SEMANTIC_SEARCH_ENABLED=true in cli/.env.local first.")
        return
    await bridge.start_http_server()
    print("Semantic bridge running on :24612  (Ctrl+C to stop)")
    stop = asyncio.Event()
    try:
        await stop.wait()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    asyncio.run(main())
