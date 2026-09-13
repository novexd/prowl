"""
Semantic search service (runs on the HidenCloud bot server).

Design goals (see project brief):
  * Page embeddings are precomputed and cached to disk; only the *query* is
    embedded per request.
  * Embedding runs in a ThreadPoolExecutor so it never blocks the
    Discord bot's asyncio event loop.
  * The embedding cache survives restarts, detects metadata changes (hash) and
    regenerates only what changed. Corruption is handled gracefully.
  * If the HF API fails, the service disables itself but the bot keeps
    running - search just degrades to "no results".

The public surface is intentionally tiny:
    results = await semantic_search_service.search("change my bot avatar")
so callers never touch embeddings or the model directly.
"""

import os
import json
import math
import asyncio
import logging
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .pages import PAGES

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_PATH = str(Path(__file__).parent.parent / "data" / "semantic_cache.json")
MAX_QUERY_CHARS = 500


def _page_text(page):
    """Build the text we actually embed from page metadata (not raw HTML).

    Includes the section/block names so queries that name a specific in-page
    section (e.g. "auto roles", "score threshold", "DJ permissions") rank the
    correct page.
    """
    parts = [page["title"], page["description"], page["keywords"]]
    blocks = page.get("blocks")
    if blocks:
        parts.append("Sections: " + ", ".join(blocks))
    return "\n".join(parts)


def _text_hash(text, model):
    return hashlib.sha256(f"{model}::{text}".encode("utf-8")).hexdigest()[:16]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticSearchService:
    def __init__(self, embedder_override=None):
        self.enabled = os.environ.get("SEMANTIC_SEARCH_ENABLED", "false").lower() in (
            "1", "true", "yes", "on",
        )
        self.model_name = os.environ.get("SEMANTIC_MODEL", DEFAULT_MODEL)
        self.min_score = float(os.environ.get("SEMANTIC_MIN_SCORE", "0.20"))
        self.top_k = int(os.environ.get("SEMANTIC_TOP_K", "8"))
        self.cache_path = Path(os.environ.get("SEMANTIC_CACHE_PATH") or DEFAULT_CACHE_PATH)

        self._embedder_override = embedder_override
        self._embedder = None
        self._executor = None
        self._pages = {}            # panel -> {"hash": str, "embedding": [...]}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._load_error = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def ensure_loaded(self):
        """Load the model + cache once. Safe to call repeatedly / concurrently.

        Heavy work (model load, encoding pages) runs in a thread pool so the
        calling asyncio event loop is never blocked. On failure we disable the
        service but NEVER raise - the bot must keep running.
        """
        if self._loaded or not self.enabled:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                loop = asyncio.get_event_loop()
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="sem-emb"
                )
                self._embedder = await loop.run_in_executor(
                    self._executor, self._load_embedder
                )
                await loop.run_in_executor(self._executor, self._build_cache)
                self._loaded = True
                logger.info(
                    "Semantic search ready (%d pages cached, model %s).",
                    len(self._pages), self.model_name,
                )
            except Exception as e:  # pragma: no cover - defensive
                self._loaded = True
                self.enabled = False
                self._load_error = e
                logger.error("Semantic search disabled - model init failed: %s", e)

    def _load_embedder(self):
        if self._embedder_override is not None:
            return self._embedder_override
        from .embedder import HuggingFaceEmbedder

        return HuggingFaceEmbedder(self.model_name)

    def _build_cache(self):
        """Load cached embeddings, regenerate only changed/missing pages."""
        cached = self._load_cache()
        rebuilt = False
        for page in PAGES:
            pid = page["panel"]
            text = _page_text(page)
            h = _text_hash(text, self.model_name)
            entry = cached.get(pid)
            if entry and entry.get("hash") == h and entry.get("embedding"):
                self._pages[pid] = entry
            else:
                emb = self._embedder.embed([text])[0]
                self._pages[pid] = {"hash": h, "embedding": emb}
                cached[pid] = self._pages[pid]
                rebuilt = True
        if rebuilt:
            self._save_cache(cached)

    def _load_cache(self):
        try:
            if self.cache_path.exists():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("pages"), dict):
                    return data["pages"]
        except Exception as e:
            logger.warning("Semantic cache corrupt - rebuilding: %s", e)
        return {}

    def _save_cache(self, pages):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {"model": self.model_name, "pages": pages},
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to persist semantic cache: %s", e)

    # ── Search ─────────────────────────────────────────────────────────────

    async def search(self, query, top_k=None):
        """Return ranked pages: [{"route": panel, "score": float}, ...]."""
        if not self.enabled:
            return []
        await self.ensure_loaded()
        if not self.enabled or self._embedder is None or not self._pages:
            return []

        top_k = top_k or self.top_k
        loop = asyncio.get_event_loop()
        q_emb = await loop.run_in_executor(
            self._executor, self._embedder.embed_query, query
        )

        results = []
        for pid, entry in self._pages.items():
            score = _cosine(q_emb, entry["embedding"])
            if score >= self.min_score:
                results.append({"route": pid, "score": round(score, 4)})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]


# Module-level singleton used by the HTTP bridge and anywhere else.
semantic_search_service = SemanticSearchService()
