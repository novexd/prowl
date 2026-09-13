"""
Embedding abstraction for semantic search.

Uses the Hugging Face Inference API for BGE embeddings - no local PyTorch
required.  The API returns normalized vectors for BGE models, matching the
behaviour the cache and cosine scorer expect.

BGE models expect a query prefix for retrieval and normalized vectors; both are
handled here so callers only deal in plain text.
"""

import os
import logging
import time

import requests

logger = logging.getLogger(__name__)

# BGE retrieval instruction prepended to *queries* (not to documents).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Token the model was trained with for subword handling.
MODEL_MAX_SEQ = 512

# HF Inference API batch size limit (keep conservative to avoid timeouts).
API_BATCH_SIZE = 16

# How long to wait when the model is cold-starting on HF (seconds).
MAX_LOAD_WAIT = 120


class Embedder:
    """Minimal embedder contract used by the service."""

    def embed(self, texts):
        """Return a list of float vectors (one per input text)."""
        raise NotImplementedError

    def embed_query(self, text):
        """Embed a single search query (with the BGE retrieval prefix)."""
        return self.embed([QUERY_PREFIX + text])[0]


class HuggingFaceEmbedder(Embedder):
    """HF Inference API backed embedder (no local PyTorch needed)."""

    # Primary and fallback API endpoints
    _API_URLS = [
        "https://router.huggingface.co/hf-inference/models",
        "https://api-inference.huggingface.co/models",
    ]

    def __init__(self, model_name):
        self._model = model_name
        self._token = os.environ.get("HF_API_TOKEN") or os.environ.get("HUGGINGFACE_API_TOKEN")
        self._headers = {}
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

        # Find which endpoint works
        self._api_url = self._detect_endpoint()

        logger.info("HuggingFace embedder configured for %s", model_name)
        # Warm up: request a single dummy embedding to pull the model if cold.
        self._wait_for_model()

    def _detect_endpoint(self):
        """Try each endpoint and return the first that resolves DNS."""
        for base_url in self._API_URLS:
            url = f"{base_url}/{self._model}"
            try:
                resp = requests.post(
                    url,
                    headers=self._headers,
                    json={"inputs": ["test"]},
                    timeout=10,
                )
                if resp.status_code in (200, 503):  # 503 = model loading, still valid
                    logger.info("Using HF endpoint: %s", base_url)
                    return url
            except requests.RequestException:
                continue
        # Fallback to primary URL even if unreachable (will fail on actual calls)
        logger.warning("Could not reach any HF endpoint, using default")
        return f"{self._API_URLS[0]}/{self._model}"

    def _wait_for_model(self):
        """Ping the API until the model is loaded (or give up)."""
        start = time.time()
        attempts = 0
        while time.time() - start < MAX_LOAD_WAIT:
            attempts += 1
            try:
                resp = requests.post(
                    self._api_url,
                    headers=self._headers,
                    json={"inputs": ["warmup"]},
                    timeout=30,
                )
                if resp.status_code == 200:
                    logger.info("HF model %s is ready.", self._model)
                    return
                if resp.status_code == 503:
                    # Model is loading - back off and retry.
                    data = resp.json()
                    # API may return a list or a dict
                    if isinstance(data, list) and data:
                        data = data[0]
                    wait = data.get("estimated_time", 10) if isinstance(data, dict) else 10
                    logger.info("HF model loading, retrying in %.0fs...", wait)
                    time.sleep(min(wait, 30))
                    continue
                # Any other status - bail (will fail on first real call).
                logger.warning("HF warmup returned %d: %s", resp.status_code, resp.text[:200])
                return
            except requests.RequestException as e:
                if attempts >= 3:
                    logger.warning("HF warmup failed after %d attempts: %s", attempts, e)
                    break
                logger.warning("HF warmup attempt %d failed: %s", attempts, e)
                time.sleep(2)
        logger.warning("HF model %s did not become ready within %ds, will retry on first call.", self._model, MAX_LOAD_WAIT)

    def _call_api(self, texts):
        """POST texts to the HF Inference API and return embeddings."""
        resp = requests.post(
            self._api_url,
            headers=self._headers,
            json={"inputs": texts},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HF Inference API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        # Response may be: {"embeddings": [[...], ...]} or just [[...], ...]
        if isinstance(data, dict):
            return data.get("embeddings", data)
        # If it's a list, it's already the embeddings
        return data

    def embed(self, texts):
        all_vectors = []
        for i in range(0, len(texts), API_BATCH_SIZE):
            batch = texts[i : i + API_BATCH_SIZE]
            vectors = self._call_api(batch)
            all_vectors.extend(vectors)
        return all_vectors
