"""Asyncio-safe in-memory TTL + LRU cache for the Prowl bot server.

Replaces the previous ad-hoc ``_SETTINGS_CACHE`` dict in ``Ediscord.db``. The
design intentionally keeps Turso as the source of truth:

* Values are only ever cached when they were successfully loaded from Turso.
  A loader that fails returns ``None`` and is intentionally NOT cached, so a
  Turso outage can never poison the cache with garbage or with stale defaults.
* The store is a plain ``OrderedDict``; all mutations run without ``await`` in
  between, so they are atomic under the bot's single-threaded event loop.
* The only lock is a per-key ``asyncio.Lock`` used by ``get_or_load`` to
  collapse concurrent misses for the same key (stampede / singleflight
  protection). A single shared guard protects the lock registry; it is never
  held across a store-touching ``await``, so there is no deadlock or avoidable
  contention.

The interface (``get``/``set``/``get_or_load``/``invalidate``/``invalidate_all``)
is deliberately backend-agnostic so the store could later be swapped for Redis
without changing call sites.
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict

import asyncio


class AsyncTTLCache:
    def __init__(self, default_ttl: float | None = None, maxsize: int = 5000):
        # Default TTL 60s (configurable via CACHE_TTL_SECONDS). Within the
        # 30-60s range: long enough to cut Turso reads, short enough that a
        # missed invalidation self-heals quickly.
        env_ttl = os.environ.get("CACHE_TTL_SECONDS")
        if default_ttl is None:
            try:
                default_ttl = float(env_ttl) if env_ttl else 60.0
            except ValueError:
                default_ttl = 60.0
        self.default_ttl = default_ttl
        try:
            self.maxsize = max(1, int(os.environ.get("CACHE_MAX_ENTRIES", maxsize)))
        except ValueError:
            self.maxsize = max(1, int(maxsize))
        self._store: "OrderedDict[object, dict]" = OrderedDict()
        # Per-key locks for stampede protection (singleflight on miss).
        self._locks: "dict[object, asyncio.Lock]" = {}
        self._lock_guard: asyncio.Lock | None = None

    async def get(self, key):
        """Return the cached value or ``None`` on miss / expiry (lazy purge)."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry["exp"] <= time.time():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return entry["value"]

    async def set(self, key, value, ttl: float | None = None):
        if ttl is None:
            ttl = self.default_ttl
        self._store[key] = {"value": value, "exp": time.time() + ttl}
        self._store.move_to_end(key)
        # LRU eviction only when over budget (no await -> atomic).
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    async def get_or_load(self, key, loader, ttl: float | None = None):
        """Return the cached value, loading + caching on miss.

        If the loader returns ``None`` the result is NOT cached (so failures
        are retried on the next call rather than poisoning the cache). Concurrent
        misses for the same key collapse into a single loader call.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        if self._lock_guard is None:
            self._lock_guard = asyncio.Lock()
        async with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
        async with lock:
            # Re-check: another coroutine may have loaded it while we waited.
            cached = await self.get(key)
            if cached is not None:
                return cached
            value = await loader()
            if value is not None:
                await self.set(key, value, ttl)
            return value

    async def invalidate(self, key):
        self._store.pop(key, None)

    async def invalidate_prefix(self, prefix):
        """Drop every entry whose key is a ``(table, guild_id)`` tuple matching the guild."""
        for k in [k for k in self._store if isinstance(k, tuple) and len(k) >= 2 and k[1] == prefix]:
            self._store.pop(k, None)

    async def invalidate_all(self):
        self._store.clear()
        self._locks.clear()


# Single shared settings cache for the whole bot process.
settings_cache = AsyncTTLCache()
