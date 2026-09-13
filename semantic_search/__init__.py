"""
Prowl semantic search package.

Public entry point:
    from semantic_search import semantic_search_service
    results = await semantic_search_service.search("change my bot avatar")

The caller does not need to know about models, embeddings or caching.
"""

from .service import SemanticSearchService, semantic_search_service

__all__ = ["SemanticSearchService", "semantic_search_service"]
