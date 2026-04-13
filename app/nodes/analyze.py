"""Nodes: analyze_diff (filter + chunk + risk-sort) and build_rag_index."""
from __future__ import annotations

import asyncio

import structlog

from app.config import get_settings
from app.services import rag
from app.services.github import get_github_client
from app.state import PRReviewState
from app.utils.diff import analyze_diff

log = structlog.get_logger()


async def analyze_diff_node(state: PRReviewState) -> dict:
    settings = get_settings()
    repo_config = state.get("repo_config") or {}

    analysis = analyze_diff(
        state["pr_files"],
        max_files=settings.max_files,
        max_diff_size=settings.max_diff_size,
        chunk_size=settings.chunk_size,
        skip_config_only=settings.skip_config_only,
        extra_ignore_patterns=repo_config.get("ignore_patterns", []),
        use_compact=settings.compact_diff,
        compact_keep=settings.compact_context_lines,
        review_depth=repo_config.get("review_depth", "balanced"),
        extra_risk_paths=repo_config.get("risk_paths", []),
    )

    log.info(
        "diff_analyzed",
        skip=analysis.skip,
        reason=analysis.reason,
        chunks=len(analysis.chunk_plans),
        included=len(analysis.files_included),
        skipped=len(analysis.files_skipped),
        tokens_est=analysis.tokens_estimated,
    )

    if analysis.skip:
        return {"skipped": True, "skip_reason": analysis.reason}

    chunk_plans = [
        {
            "index": p.index,
            "chunk_text": p.text,
            "files": p.files,
            "risk_score": p.risk_score,
            "review_mode": p.review_mode,
        }
        for p in analysis.chunk_plans
    ]

    return {
        "chunk_plans": chunk_plans,
        "current_chunk_idx": 0,
        "diff_skip": False,
    }


async def build_rag_index(state: PRReviewState) -> dict:
    """Fetch full file contents and build an in-memory FAISS index.

    Falls back gracefully to no-RAG if embedding fails.
    """
    settings = get_settings()

    if not settings.enable_rag or not settings.llm_api_key:
        return {"rag_index_built": False}

    github = await get_github_client(state.get("installation_id"))
    owner = state["owner"]
    repo = state["repo"]
    ref = state.get("head_sha") or "HEAD"

    filenames: set[str] = set()
    for plan in state.get("chunk_plans", []):
        filenames.update(plan.get("files", []))

    async def _fetch(fname: str) -> dict | None:
        content = await github.get_file_content(owner, repo, fname, ref)
        return {"filename": fname, "content": content} if content else None

    results = await asyncio.gather(
        *(_fetch(f) for f in filenames), return_exceptions=True,
    )
    file_contents = [r for r in results if isinstance(r, dict)]

    log.info("rag_files_fetched", requested=len(filenames), fetched=len(file_contents))

    success = await rag.build_index(
        state["delivery_id"],
        file_contents,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
        api_key=settings.llm_api_key,
        embedding_model=settings.rag_embedding_model,
    )
    return {"rag_index_built": success}
