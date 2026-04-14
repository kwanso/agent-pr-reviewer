"""Nodes: review_chunk (LLM call with structured output) and advance_chunk."""

from __future__ import annotations

import asyncio

import structlog
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings
from app.errors import ErrorType, classify_llm_error
from app.models import ReviewFinding, ReviewOutput, parse_review_json, parse_review_text
from app.services import rag
from app.state import PRReviewState
from app.utils.prompts import build_review_messages
from app.utils.tokens import estimate_messages_tokens, is_budget_exhausted

log = structlog.get_logger()

_MAX_RETRIES = 4


def _build_mock_review(diff: str) -> ReviewOutput:
    line_count = diff.count("\n") + 1
    return ReviewOutput(
        context_summary=(
            f"**What changed:** Mock review only; ~{line_count} diff lines processed.\n"
            "**Dependencies / assumptions:** Not present in provided code — LLM bypassed.\n"
            "**Gaps:** No model inference; set LLM_MOCK_MODE=false for a real review."
        ),
        production_readiness=[
            ReviewFinding(
                issue="LLM mock mode — no automated findings",
                why_it_matters="Pipeline smoke test only; not a production code assessment.",
                suggested_fix="Disable LLM_MOCK_MODE for real reviews.",
                evidence="(mock)",
                confidence=1.0,
            ),
        ],
    )


async def review_chunk(state: PRReviewState) -> dict:
    settings = get_settings()
    idx = state.get("current_chunk_idx", 0)
    plans = state.get("chunk_plans", [])

    if idx >= len(plans):
        return {}

    plan = plans[idx]

    # Initialize token budget on first chunk
    if idx == 0:
        state["token_budget_max"] = settings.token_budget_per_review
        state["token_budget_used"] = 0
        state["token_budget_exhausted"] = False

    log.info(
        "reviewing_chunk",
        index=idx,
        total=len(plans),
        risk=plan["risk_score"],
        mode=plan["review_mode"],
        token_budget_pct=round(
            100 * state.get("token_budget_used", 0) / state.get("token_budget_max", 1)
        ),
    )

    # Check token budget exhaustion
    if is_budget_exhausted(
        state.get("token_budget_used", 0),
        state.get("token_budget_max", settings.token_budget_per_review),
        settings.token_budget_threshold_pct,
    ):
        log.warning(
            "token_budget_exhausted",
            index=idx,
            used=state.get("token_budget_used"),
            max=state.get("token_budget_max"),
        )
        state["token_budget_exhausted"] = True
        # Stop processing; return empty to skip this chunk
        return {"token_budget_exhausted": True}

    # Skip low-risk chunks when quota is exhausted.
    if state.get("quota_exhausted") and plan["risk_score"] < 2:
        log.info("skipping_low_risk_chunk", index=idx, risk=plan["risk_score"])
        return {}

    # ── RAG context ──────────────────────────────────────────────
    rag_context = ""
    if state.get("rag_index_built"):
        docs = rag.retrieve(
            state.get("delivery_id", ""),
            plan["chunk_text"],
            k=settings.rag_top_k,
        )
        if docs:
            rag_context = "\n\n".join(doc.page_content for doc in docs)

    # ── Prompt ───────────────────────────────────────────────────
    messages = build_review_messages(
        title=state.get("pr_title", ""),
        description=state.get("pr_body", ""),
        diff=plan["chunk_text"],
        rag_context=rag_context,
        repo_context=state.get("repo_context_block", ""),
        review_mode=plan["review_mode"],
        allowed_file_paths=plan.get("files") or [],
    )

    # ── Token estimation ─────────────────────────────────────────
    estimated_input_tokens = estimate_messages_tokens(
        messages, settings.llm_flash_model
    )
    estimated_total_tokens = estimated_input_tokens + settings.llm_max_output_tokens

    log.info(
        "review_chunk_token_estimate",
        index=idx,
        input_tokens=estimated_input_tokens,
        output_tokens=settings.llm_max_output_tokens,
        total_estimated=estimated_total_tokens,
        budget_remaining=state.get("token_budget_max", 0)
        - state.get("token_budget_used", 0),
    )

    # Check if this chunk would exceed budget
    remaining_budget = state.get(
        "token_budget_max", settings.token_budget_per_review
    ) - state.get("token_budget_used", 0)
    if estimated_total_tokens > remaining_budget * 0.8:  # 80% of remaining
        log.warning(
            "review_chunk_would_exceed_budget",
            index=idx,
            estimated=estimated_total_tokens,
            remaining=remaining_budget,
        )
        return {"token_budget_exhausted": True, "early_exit": True}

    # ── Mock mode ────────────────────────────────────────────────
    if settings.llm_mock_mode:
        review = _build_mock_review(plan["chunk_text"])
        return _success(review, idx, plans, settings, estimated_total_tokens, state)

    # ── LLM call with structured output + retry-with-backoff ─────
    llm = ChatGoogleGenerativeAI(
        model=settings.llm_flash_model,
        google_api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        max_retries=0,  # we handle retries ourselves
        timeout=settings.llm_timeout_s,
    )

    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            structured = llm.with_structured_output(ReviewOutput, include_raw=True)
            result = await structured.ainvoke(messages)

            if result.get("parsed"):
                review = result["parsed"]
            else:
                raw_text = result["raw"].content if result.get("raw") else ""
                log.warning(
                    "structured_output_fallback", error=str(result.get("parsing_error"))
                )
                review = parse_review_json(raw_text) or parse_review_text(raw_text)
                if review is None:
                    log.error(
                        "review_parsing_failed",
                        raw_length=len(raw_text),
                        hint="structured output could not be parsed as JSON or markdown",
                    )
                    return {"error": "Review output parsing failed"}
                if review.is_clean() and raw_text.strip():
                    log.warning(
                        "review_fallback_empty",
                        hint="check LLM_MAX_OUTPUT_TOKENS if JSON was truncated",
                    )

            return _success(review, idx, plans, settings, estimated_total_tokens, state)

        except Exception as exc:
            last_error = classify_llm_error(exc)

            # Retriable error and attempts remaining
            if last_error.retriable and attempt < _MAX_RETRIES - 1:
                log.warning(
                    "review_chunk_retrying",
                    index=idx,
                    attempt=attempt + 1,
                    error_type=last_error.error_type.value,
                    retry_after_s=round(last_error.retry_after_s, 1),
                )
                await asyncio.sleep(last_error.retry_after_s)
                continue

            # Non-retriable or retries exhausted
            break

    # All retries exhausted or non-retriable error
    if last_error is None:
        last_error = classify_llm_error(Exception("Unknown error"))

    log.error(
        "chunk_review_failed",
        index=idx,
        chunk_size=len(plan["chunk_text"]),
        chunk_files=plan.get("files"),
        attempts_made=_MAX_RETRIES if last_error.retriable else 1,
        error_type=last_error.error_type.value,
        error_message=last_error.message,
    )

    # Handle specific error types
    if last_error.error_type == ErrorType.SERVICE_UNAVAILABLE:
        log.warning(
            "llm_service_unavailable",
            stopping_chunk_processing=True,
            recommendation="circuit-break and retry later",
        )
        return {"early_exit": True, "error": last_error.message}

    if last_error.error_type == ErrorType.QUOTA_EXHAUSTED:
        return {"quota_exhausted": True, "error": last_error.message}

    if last_error.error_type == ErrorType.AUTH_FAILED:
        log.critical("llm_auth_failed", recommendation="check API credentials")
        return {
            "early_exit": True,
            "error": f"LLM authentication failed: {last_error.message}",
        }

    # Generic error
    return {"error": last_error.message}


def _success(
    review: ReviewOutput,
    idx: int,
    plans: list[dict],
    settings: object,
    estimated_tokens: int = 0,
    state: dict | None = None,
) -> dict:
    review_dict = review.model_dump()
    review_dict["chunk_index"] = idx
    review_dict["review_mode"] = plans[idx]["review_mode"]
    review_dict["risk_score"] = plans[idx]["risk_score"]

    early_exit = (
        getattr(settings, "enable_early_exit", False)
        and idx == 0
        and len(plans) > 1
        and review.is_clean()
    )

    result = {
        "chunk_reviews": [review_dict],
        "model_usage": [getattr(settings, "llm_flash_model", "unknown")],
        "early_exit": early_exit,
    }

    # Track token usage
    if state is not None and estimated_tokens > 0:
        result["token_budget_used"] = (
            state.get("token_budget_used", 0) + estimated_tokens
        )

    return result


async def advance_chunk(state: PRReviewState) -> dict:
    """Increment chunk index with an inter-chunk delay for rate limiting."""
    settings = get_settings()
    if settings.chunk_delay_ms > 0:
        await asyncio.sleep(settings.chunk_delay_ms / 1000.0)
    return {"current_chunk_idx": state.get("current_chunk_idx", 0) + 1}
