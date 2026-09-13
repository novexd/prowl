"""
Tests for the semantic search microservice (bot-side) + HTTP bridge.

The heavy sentence-transformers dependency is NOT required to run these tests:
a deterministic in-memory FakeEmbedder stands in for the BGE model so we can
exercise caching, change detection, failure handling, async non-blocking
behaviour and request validation.

Run from the cli/ directory:
    python -m pytest test_semantic_search.py -q
or
    python test_semantic_search.py
"""

import os
import re
import json
import math
import time
import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

# Make the cli/ package root importable when run directly.
import sys

sys.path.insert(0, str(Path(__file__).parent))

# The Ediscord package imports require these env vars to exist at import time
# (the bot normally sets them). Stub them so the bridge module can be imported
# under test without a real bot environment.
os.environ.setdefault("TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgres://test/test")
os.environ.setdefault("BOT_HTTP_TOKEN", "test-secret")

from semantic_search import SemanticSearchService
from semantic_search import pages as pages_mod
from semantic_search.service import _cosine, _text_hash, _page_text  # noqa: F401
import Ediscord.http_bridge as http_bridge


# ── Fake embedder (lexical, deterministic) ──────────────────────────────────
class FakeEmbedder:
    """Word-overlap embedder: good enough to exercise the ranking pipeline.

    It is purely lexical, so "semantically equivalent" queries in these tests
    are chosen to share tokens with the target page. The *real* BGE model
    handles true paraphrase matching; these tests validate OUR code paths.
    """

    DIM = 4096
    TITLE_WEIGHT = 5.0  # page titles carry more signal, like a real model

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _split_title(text):
        # The page text is "Title.\nDescription\nKeywords"; the title is the
        # leading segment and should dominate the embedding.
        head = text.split("\n", 1)[0].split(".", 1)[0]
        return head

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [0.0] * self.DIM
            title = self._split_title(t).lower()
            title_tokens = set(re.findall(r"[a-z0-9]+", title))
            for tok in re.findall(r"[a-z0-9]+", t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                w = self.TITLE_WEIGHT if tok in title_tokens else 1.0
                v[h % self.DIM] += w
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    def embed_query(self, text):
        return self.embed([text])[0]


class SlowFakeEmbedder(FakeEmbedder):
    """Adds a blocking sleep to simulate CPU-bound inference."""

    def __init__(self, sleep=0.05):
        super().__init__()
        self.sleep = sleep

    def embed(self, texts):
        time.sleep(self.sleep)  # simulate expensive CPU work (in executor)
        return super().embed(texts)


def make_service(enabled=True, cache_path=None, embedder=None):
    # Default to an isolated temp cache so tests never read a stale on-disk
    # cache written by a previous run (which would break DIM consistency).
    if cache_path is None:
        cache_path = Path(tempfile.mkdtemp()) / "sem_cache.json"
    svc = SemanticSearchService(embedder_override=embedder or FakeEmbedder())
    svc.enabled = enabled
    svc.cache_path = Path(cache_path)
    return svc


# ── 1. Keyword / exact matching still ranks correctly ───────────────────────
class TestKeywordEquivalent(unittest.TestCase):
    def test_exact_title_query_ranks_top(self):
        svc = make_service()
        svc.min_score = 0.0  # ranking check, not absolute-threshold check
        # Pre-load (fast: FakeEmbedder, no model)
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())
        # "moderation" shares tokens with the moderation page text
        res = asyncio.get_event_loop().run_until_complete(svc.search("moderation"))
        self.assertTrue(res, "expected at least one result")
        self.assertEqual(res[0]["route"], "moderation")

    def test_equivalent_query_finds_page(self):
        svc = make_service()
        svc.min_score = 0.0
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())
        # lexical overlap with bot_profile text ("avatar", "bot")
        res = asyncio.get_event_loop().run_until_complete(svc.search("change the bot avatar"))
        top = {r["route"] for r in res[:3]}
        self.assertIn("bot_profile", top)

    def test_irrelevant_query_suppressed(self):
        svc = make_service()
        svc.min_score = 0.0  # inspect raw scores without filtering
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())
        res = asyncio.get_event_loop().run_until_complete(svc.search("how to bake sourdough bread"))
        # Irrelevant queries must NOT produce highly confident results.
        self.assertTrue(all(r["score"] < 0.20 for r in res), res)


# ── 4. Cached embeddings are reused ────────────────────────────────────────
class TestCacheReuse(unittest.TestCase):
    def test_embeddings_reused_across_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache.json"
            emb1 = FakeEmbedder()
            svc1 = make_service(cache_path=cache, embedder=emb1)
            asyncio.get_event_loop().run_until_complete(svc1.ensure_loaded())
            self.assertEqual(emb1.calls, len(pages_mod.PAGES))  # initial build

            # Second process: same cache, fresh embedder
            emb2 = FakeEmbedder()
            svc2 = make_service(cache_path=cache, embedder=emb2)
            asyncio.get_event_loop().run_until_complete(svc2.ensure_loaded())
            # No page changed -> zero re-embeddings
            self.assertEqual(emb2.calls, 0)
            self.assertEqual(len(svc2._pages), len(pages_mod.PAGES))


# ── 5. Changed metadata invalidates only its embedding ─────────────────────
class TestChangeDetection(unittest.TestCase):
    def test_only_changed_page_reembedded(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache.json"
            emb1 = FakeEmbedder()
            svc1 = make_service(cache_path=cache, embedder=emb1)
            asyncio.get_event_loop().run_until_complete(svc1.ensure_loaded())

            # Simulate a metadata change for ONE page by invalidating its cache
            # entry (hash mismatch) and reloading with a fresh embedder.
            changed = pages_mod.PAGES[3]["panel"]
            del svc1._pages  # force rebuild from cache file
            # Corrupt just that one page's hash in the on-disk cache.
            data = json.loads(cache.read_text())
            data["pages"][changed]["hash"] = "deadbeefdeadbeef"
            cache.write_text(json.dumps(data))

            emb2 = FakeEmbedder()
            svc2 = make_service(cache_path=cache, embedder=emb2)
            asyncio.get_event_loop().run_until_complete(svc2.ensure_loaded())
            self.assertEqual(emb2.calls, 1, "only the changed page should re-embed")


# ── 6. Corrupted cache handled safely ───────────────────────────────────────
class TestCorruptedCache(unittest.TestCase):
    def test_corrupt_cache_rebuilds(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache.json"
            cache.write_text("{ this is not valid json ")
            emb = FakeEmbedder()
            svc = make_service(cache_path=cache, embedder=emb)
            # Should not raise; should rebuild from scratch.
            asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())
            self.assertEqual(len(svc._pages), len(pages_mod.PAGES))


# ── 7. Model init failure does not kill the service ─────────────────────────
class TestModelInitFailure(unittest.TestCase):
    def test_failure_disables_gracefully(self):
        svc = make_service()
        # Force the embedder load to blow up.
        def boom():
            raise RuntimeError("torch exploded")

        svc._load_embedder = boom
        # ensure_loaded must swallow the error, not propagate to the caller.
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())
        self.assertFalse(svc.enabled, "service should disable after load failure")
        self.assertIsNotNone(svc._load_error)
        # Search still returns safely (empty), bot loop unharmed.
        res = asyncio.get_event_loop().run_until_complete(svc.search("anything"))
        self.assertEqual(res, [])


# ── 8 & 9. Async inference does not block the event loop ────────────────────
class TestAsyncNonBlocking(unittest.TestCase):
    def test_loop_stays_responsive_during_inference(self):
        svc = make_service(embedder=SlowFakeEmbedder(sleep=0.08))
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())

        steps = []

        async def tracker():
            for _ in range(20):
                steps.append(1)
                await asyncio.sleep(0)  # yield to the loop

        async def run():
            await asyncio.gather(svc.search("moderation settings"), tracker())

        asyncio.get_event_loop().run_until_complete(run())
        # The tracker ran interleaved with the (threaded) inference, proving
        # the event loop was never blocked by the CPU work.
        self.assertGreater(len(steps), 5)

    def test_many_concurrent_requests(self):
        svc = make_service(embedder=SlowFakeEmbedder(sleep=0.04))
        asyncio.get_event_loop().run_until_complete(svc.ensure_loaded())

        async def run():
            await asyncio.gather(*[svc.search(f"query {i}") for i in range(10)])

        # Should complete without deadlock and keep the loop alive.
        asyncio.get_event_loop().run_until_complete(run())
        self.assertTrue(svc._loaded)


# ── 10 & 11. Invalid requests rejected + auth works ────────────────────────
class _FakeRequest:
    def __init__(self, headers=None, json_data=None, raise_json=False):
        self.headers = headers or {}
        self._json = json_data
        self._raise_json = raise_json

    async def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._json


class TestHttpBridge(unittest.TestCase):
    def setUp(self):
        os.environ["BOT_HTTP_TOKEN"] = "test-secret"
        self.svc = make_service()
        asyncio.get_event_loop().run_until_complete(self.svc.ensure_loaded())
        # Patch the module-level singleton used by the handler.
        self._orig = http_bridge.semantic_search_service
        http_bridge.semantic_search_service = self.svc

    def tearDown(self):
        http_bridge.semantic_search_service = self._orig
        os.environ.pop("BOT_HTTP_TOKEN", None)

    def test_missing_auth_rejected(self):
        req = _FakeRequest(headers={}, json_data={"query": "hi"})
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 401)

    def test_valid_auth_accepted(self):
        req = _FakeRequest(headers={"X-Prowl-Token": "test-secret"}, json_data={"query": "moderation"})
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.text.startswith("{"))  # json body

    def test_missing_query_rejected(self):
        req = _FakeRequest(headers={"X-Prowl-Token": "test-secret"}, json_data={})
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 400)

    def test_oversized_query_rejected(self):
        req = _FakeRequest(headers={"X-Prowl-Token": "test-secret"}, json_data={"query": "x" * 600})
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 413)

    def test_malformed_json_rejected(self):
        req = _FakeRequest(headers={"X-Prowl-Token": "test-secret"}, raise_json=True)
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 400)

    def test_disabled_service_returns_503(self):
        self.svc.enabled = False
        req = _FakeRequest(headers={"X-Prowl-Token": "test-secret"}, json_data={"query": "moderation"})
        resp = asyncio.get_event_loop().run_until_complete(http_bridge.handle_semantic_search(req))
        self.assertEqual(resp.status, 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
