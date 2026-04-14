"""RAG layer: Hybrid retrieval (semantic + keyword) per PR review session.

Combines FAISS semantic search with BM25 keyword matching for robust retrieval.
Relevant code is found by both meaning and exact keyword matches.

The stores are not serializable, so they live in module-level dicts
keyed by ``delivery_id``. After the review completes, ``cleanup()`` frees memory.

Memory leak prevention: Indices older than MAX_INDEX_AGE_HOURS are automatically
removed. Indices should be explicitly cleaned up via cleanup() when a review finishes.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

log = structlog.get_logger()

_stores: dict[str, Any] = {}  # FAISS vector stores
_bm25_stores: dict[str, Any] = {}  # BM25 keyword stores
_metadata_stores: dict[str, Any] = {}  # File metadata for ranking
_index_created_at: dict[str, float] = {}  # delivery_id -> creation timestamp

# Maximum age for in-memory indices before auto-cleanup (hours)
MAX_INDEX_AGE_HOURS = 24


async def build_index(
    delivery_id: str,
    file_contents: list[dict],
    *,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    api_key: str = "",
    embedding_model: str = "models/gemini-embedding-001",
) -> bool:
    """Build hybrid index: FAISS (semantic) + BM25 (keyword). Returns True on success."""
    if not file_contents or not api_key:
        return False

    try:
        from langchain_community.vectorstores import FAISS
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\nclass ", "\ndef ", "\nasync def ", "\n\n", "\n", " "],
        )

        docs = []
        doc_texts = []  # For BM25
        metadata = {}

        for item in file_contents:
            content = item.get("content") or ""
            filename = item.get("filename", "")
            if not content.strip():
                continue

            chunks = splitter.create_documents(
                [content],
                metadatas=[{"source": filename}],
            )
            docs.extend(chunks)

            # Store metadata for ranking (file importance)
            metadata[filename] = {
                "size": len(content),
                "lines": content.count("\n"),
                "is_test": "test" in filename.lower(),
                "language": _infer_language(filename),
            }

            # Collect full document texts for BM25
            for chunk in chunks:
                doc_texts.append(chunk.page_content)

        if not docs:
            return False

        # Build BM25 keyword index (lightweight, optional fallback)
        # Note: Requires rank_bm25 package (pip install rank_bm25)
        bm25_store = None
        try:
            from langchain_community.retrievers import BM25Retriever

            bm25_store = BM25Retriever.from_documents(docs)
            _bm25_stores[delivery_id] = bm25_store
            log.info("bm25_index_built", delivery_id=delivery_id, chunks=len(docs))
        except ImportError as e:
            if "rank_bm25" in str(e):
                log.warning(
                    "bm25_skipped",
                    reason="rank_bm25 not installed (pip install rank_bm25)",
                )
            else:
                log.warning("bm25_import_failed", error=str(e))
        except Exception as e:
            log.warning("bm25_index_failed", error=str(e))

        # Build FAISS semantic index (may fail on quota)
        try:
            embeddings = GoogleGenerativeAIEmbeddings(
                model=embedding_model,
                google_api_key=api_key,
            )
            faiss_store = await FAISS.afrom_documents(docs, embeddings)
            _stores[delivery_id] = faiss_store
            log.info("faiss_index_built", delivery_id=delivery_id, chunks=len(docs))
        except Exception as exc:
            error_str = str(exc)
            if "RESOURCE_EXHAUSTED" in error_str or "429" in error_str:
                log.warning(
                    "faiss_quota_exhausted",
                    delivery_id=delivery_id,
                    falling_back_to_bm25=bm25_store is not None,
                )
                # Fall back to BM25 only (keyword search still works)
                if not bm25_store:
                    return False
            else:
                log.error(
                    "faiss_index_failed", delivery_id=delivery_id, error=error_str
                )
                return False

        _metadata_stores[delivery_id] = metadata
        _index_created_at[delivery_id] = time.time()

        # Cleanup expired indices before logging success
        cleanup_expired_indices()

        log.info(
            "rag_index_built",
            delivery_id=delivery_id,
            chunks=len(docs),
            files=len(metadata),
            faiss_available=delivery_id in _stores,
            bm25_available=delivery_id in _bm25_stores,
        )
        return True

    except Exception as exc:
        log.error("rag_index_failed", delivery_id=delivery_id, error=str(exc))
        return False


def _infer_language(filename: str) -> str:
    """Infer programming language from file extension."""
    ext_map = {
        ".py": "python",
        ".js": "js",
        ".ts": "ts",
        ".tsx": "ts",
        ".jsx": "js",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cpp": "cpp",
        ".c": "c",
        ".h": "c",
        ".rb": "ruby",
        ".php": "php",
    }
    for ext, lang in ext_map.items():
        if filename.endswith(ext):
            return lang
    return "unknown"


def retrieve(delivery_id: str, query: str, k: int = 5) -> list[Any]:
    """Hybrid retrieval: combine semantic (FAISS) + keyword (BM25) search.

    Falls back gracefully:
    1. Try FAISS + BM25 hybrid
    2. Fall back to FAISS only if BM25 fails
    3. Fall back to BM25 only if FAISS unavailable
    4. Return empty list if both fail
    """
    faiss_store = _stores.get(delivery_id)
    bm25_store = _bm25_stores.get(delivery_id)

    # Neither index available
    if not faiss_store and not bm25_store:
        return []

    # Only BM25 available (FAISS quota exhausted)
    if not faiss_store and bm25_store:
        try:
            results = bm25_store.get_relevant_documents(query)
            log.info(
                "rag_retrieve_bm25_only", delivery_id=delivery_id, results=len(results)
            )
            return results[:k]
        except Exception as exc:
            log.warning("rag_retrieve_bm25_failed", error=str(exc))
            return []

    # Try hybrid retrieval (FAISS available)
    try:
        # Semantic search
        semantic_results = faiss_store.similarity_search(query, k=k * 2)
        semantic_map = {doc.page_content: (doc, 1.0) for doc in semantic_results}

        # Keyword search (BM25) - complement semantic with exact matches
        keyword_results = []
        if bm25_store:
            try:
                keyword_results = bm25_store.get_relevant_documents(query)
            except Exception as e:
                log.warning("rag_retrieve_bm25_fallback_failed", error=str(e))

        # Merge: prefer documents found in both, with semantic score decay
        for doc in keyword_results[:k]:
            if doc.page_content not in semantic_map:
                # Keyword-only match gets lower score
                semantic_map[doc.page_content] = (doc, 0.7)

        # Rank by score and return top k
        ranked = sorted(
            semantic_map.values(),
            key=lambda x: x[1],  # Sort by score
            reverse=True,
        )
        return [doc for doc, _ in ranked[:k]]

    except Exception as exc:
        log.warning("rag_retrieve_hybrid_failed", error=str(exc))
        # Fallback to semantic only
        try:
            results = faiss_store.similarity_search(query, k=k)
            log.info(
                "rag_retrieve_faiss_only", delivery_id=delivery_id, results=len(results)
            )
            return results
        except Exception as e:
            log.error("rag_retrieve_all_failed", error=str(e))
            return []


def cleanup(delivery_id: str) -> None:
    """Free all in-memory stores for a completed review."""
    _stores.pop(delivery_id, None)
    _bm25_stores.pop(delivery_id, None)
    _metadata_stores.pop(delivery_id, None)
    _index_created_at.pop(delivery_id, None)
    log.info("rag_cleanup_completed", delivery_id=delivery_id)


def cleanup_expired_indices() -> None:
    """Remove RAG indices older than MAX_INDEX_AGE_HOURS.

    Called periodically to prevent memory leaks from abandoned reviews.
    """
    now = time.time()
    max_age_seconds = MAX_INDEX_AGE_HOURS * 3600

    expired_ids = [
        d_id
        for d_id, created_at in _index_created_at.items()
        if (now - created_at) > max_age_seconds
    ]

    if not expired_ids:
        return

    for d_id in expired_ids:
        cleanup(d_id)

    log.info(
        "rag_expired_indices_cleanup",
        removed_count=len(expired_ids),
        remaining_count=len(_index_created_at),
        max_age_hours=MAX_INDEX_AGE_HOURS,
    )
