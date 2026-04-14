from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class PRReviewState(TypedDict, total=False):
    """Full state flowing through the LangGraph review pipeline.

    Fields annotated with ``operator.add`` accumulate across node invocations
    (each node appends rather than replaces).  All other fields use last-write-
    wins semantics.
    """

    # ── Webhook input ────────────────────────────────────────────────
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    action: str
    delivery_id: str
    installation_id: int | None

    # ── Fetched PR metadata ──────────────────────────────────────────
    pr_title: str
    pr_body: str
    pr_url: str
    pr_draft: bool
    pr_state: str
    pr_labels: list[str]
    pr_files: list[dict]

    # ── Per-repo .pr-review.yml ──────────────────────────────────────
    repo_config: dict | None
    repo_context_block: str

    # ── Diff analysis ────────────────────────────────────────────────
    diff_skip: bool
    diff_skip_reason: str
    chunk_plans: list[dict]

    # ── RAG index ────────────────────────────────────────────────────
    rag_index_built: bool

    # ── Chunk loop ───────────────────────────────────────────────────
    current_chunk_idx: int
    chunk_reviews: Annotated[list[dict], operator.add]
    model_usage: Annotated[list[str], operator.add]

    # ── Validation pipeline ──────────────────────────────────────
    pending_findings: list[dict]
    validated_findings: Annotated[list[dict], operator.add]
    # filtered_reviews: same item shape as chunk_reviews, after false-positive pass
    filtered_reviews: Annotated[list[dict], operator.add]

    # ── Control flow signals ─────────────────────────────────────────
    quota_exhausted: bool
    early_exit: bool
    error: str

    # ── Final output ─────────────────────────────────────────────────
    final_summary: str
    inline_comments: list[dict]
    slack_message: str

    # ── Skip ─────────────────────────────────────────────────────────
    skipped: bool
    skip_reason: str

    # ── Token budget tracking ─────────────────────────────────────────
    token_budget_max: int
    token_budget_used: int
    token_budget_exhausted: bool
