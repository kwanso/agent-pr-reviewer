"""LangGraph state-graph definition for the PR review pipeline.

Graph topology::

    START → fetch_pr
    fetch_pr  ──▶ analyze_diff | END (skip)
    analyze_diff ──▶ build_rag_index | END (skip)
    build_rag_index ──▶ review_chunk
    review_chunk ──▶ advance_chunk (loop) | merge_results | handle_degraded
    advance_chunk ──▶ review_chunk
    merge_results ──▶ post_results | handle_degraded
    post_results ──▶ END
    handle_degraded ──▶ END

Checkpointing with ``AsyncSqliteSaver`` gives crash recovery and
idempotency — each webhook delivery_id becomes a unique thread_id.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.nodes.analyze import analyze_diff_node, build_rag_index
from app.nodes.fetch import fetch_pr
from app.nodes.publish import handle_degraded, merge_results, post_results
from app.nodes.review import advance_chunk, review_chunk
from app.nodes.validate import validate_findings
from app.state import PRReviewState

# ── Routing functions ────────────────────────────────────────────────


def route_after_fetch(state: PRReviewState) -> str:
    return "skip" if state.get("skipped") else "continue"


def route_after_analysis(state: PRReviewState) -> str:
    return "skip" if state.get("skipped") else "continue"


def route_after_review(state: PRReviewState) -> str:
    """Route after review: validate findings or prepare for merge.

    Early exit conditions:
    - early_exit flag: First chunk clean, stop processing
    - quota_exhausted: API quota hit, stop immediately (can't recover)
    """
    plans = state.get("chunk_plans", [])
    idx = state.get("current_chunk_idx", 0)
    has_reviews = bool(state.get("chunk_reviews"))

    if state.get("early_exit"):
        return "merge"

    # Quota exhausted: stop immediately (no point retrying)
    if state.get("quota_exhausted"):
        return "validate" if has_reviews else "degraded"

    is_last = idx >= len(plans) - 1

    # Instead of merge/degraded directly, go to validate first
    if is_last:
        return "validate" if has_reviews else "degraded"

    return "validate"  # Validate before advancing


def route_after_validate(state: PRReviewState) -> str:
    """Route after validation: advance chunk or merge."""
    plans = state.get("chunk_plans", [])
    idx = state.get("current_chunk_idx", 0)
    is_last = idx >= len(plans) - 1

    if is_last:
        return "merge"
    return "advance"


def route_after_merge(state: PRReviewState) -> str:
    return "degraded" if not state.get("final_summary") else "publish"


# ── Graph construction ───────────────────────────────────────────────


def build_graph() -> StateGraph:
    """Build LangGraph with enhanced reasoning pipeline.

    New topology includes validation node for multi-step reasoning:

        review_chunk → validate_findings → {advance_chunk | merge_results}
    """
    builder = StateGraph(PRReviewState)

    builder.add_node("fetch_pr", fetch_pr)
    builder.add_node("analyze_diff", analyze_diff_node)
    builder.add_node("build_rag_index", build_rag_index)
    builder.add_node("review_chunk", review_chunk)
    builder.add_node("validate_findings", validate_findings)
    builder.add_node("advance_chunk", advance_chunk)
    builder.add_node("merge_results", merge_results)
    builder.add_node("post_results", post_results)
    builder.add_node("handle_degraded", handle_degraded)

    builder.add_edge(START, "fetch_pr")
    builder.add_conditional_edges(
        "fetch_pr",
        route_after_fetch,
        {
            "continue": "analyze_diff",
            "skip": END,
        },
    )
    builder.add_conditional_edges(
        "analyze_diff",
        route_after_analysis,
        {
            "continue": "build_rag_index",
            "skip": END,
        },
    )
    builder.add_edge("build_rag_index", "review_chunk")
    builder.add_conditional_edges(
        "review_chunk",
        route_after_review,
        {
            "validate": "validate_findings",
            "degraded": "handle_degraded",
        },
    )
    builder.add_conditional_edges(
        "validate_findings",
        route_after_validate,
        {
            "advance": "advance_chunk",
            "merge": "merge_results",
        },
    )
    builder.add_edge("advance_chunk", "review_chunk")
    builder.add_conditional_edges(
        "merge_results",
        route_after_merge,
        {
            "publish": "post_results",
            "degraded": "handle_degraded",
        },
    )
    builder.add_edge("post_results", END)
    builder.add_edge("handle_degraded", END)

    return builder
