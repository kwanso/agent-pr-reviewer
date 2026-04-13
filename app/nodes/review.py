"""Nodes: review_chunk (LLM call with structured output) and advance_chunk."""

from __future__ import annotations

import asyncio
import re

import structlog
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings
from app.models import (ReviewFinding, ReviewOutput, parse_review_json,
                        parse_review_text)
from app.services import rag
from app.state import PRReviewState
from app.utils.prompts import build_review_messages

log = structlog.get_logger()

# ── Rate-limit helpers ────────────────────────────────────────────────
_RETRY_DELAY_RE = re.compile(
    r"retry\s*(?:in|Delay['\"]?\s*:\s*['\"]?)\s*([\d.]+)", re.I
)
_MAX_RETRIES = 4
_DEFAULT_BACKOFF_S = 30.0


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in ("429", "quota", "rate", "resource_exhausted"))


def _parse_retry_delay(exc: Exception) -> float:
    """Extract the retry delay (seconds) the API suggests, or use default."""
    m = _RETRY_DELAY_RE.search(str(exc))
    return float(m.group(1)) if m else _DEFAULT_BACKOFF_S


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
    log.info(
        "reviewing_chunk",
        index=idx,
        total=len(plans),
        risk=plan["risk_score"],
        mode=plan["review_mode"],
    )

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

    # ── Mock mode ────────────────────────────────────────────────
    if settings.llm_mock_mode:
        review = _build_mock_review(plan["chunk_text"])
        return _success(review, idx, plans, settings)

    # ── LLM call with structured output + retry-with-backoff ─────
    llm = ChatGoogleGenerativeAI(
        model=settings.llm_flash_model,
        google_api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        max_retries=0,  # we handle retries ourselves
        timeout=settings.llm_timeout_s,
    )

    last_exc: Exception | None = None
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
                if review.is_clean() and raw_text.strip():
                    log.warning(
                        "review_fallback_empty",
                        hint="check LLM_MAX_OUTPUT_TOKENS if JSON was truncated",
                    )

            return _success(review, idx, plans, settings)

        except Exception as exc:
            last_exc = exc
            if _is_quota_error(exc) and attempt < _MAX_RETRIES - 1:
                delay = _parse_retry_delay(exc) + 2  # +2s safety margin
                log.warning(
                    "rate_limited_retrying",
                    index=idx,
                    attempt=attempt + 1,
                    retry_in_s=round(delay, 1),
                )
                await asyncio.sleep(delay)
                continue
            break

    # All retries exhausted or non-retriable error
    error_str = str(last_exc) if last_exc else "Unknown error"
    error_type = type(last_exc).__name__ if last_exc else "Unknown"

    log.error(
        "chunk_review_failed",
        index=idx,
        error=error_str,
        error_type=error_type,
    )

    # Service overload: stop processing to preserve quota
    if last_exc and "UNAVAILABLE" in error_str:
        log.warning("llm_service_overloaded", stopping_chunk_processing=True)
        return {"early_exit": True, "error": f"LLM unavailable: {error_str[:100]}"}

    if _is_quota_error(last_exc):
        return {"quota_exhausted": True, "error": error_str}

    return {"error": error_str}


def _success(
    review: ReviewOutput,
    idx: int,
    plans: list[dict],
    settings: object,
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

    return {
        "chunk_reviews": [review_dict],
        "model_usage": [getattr(settings, "llm_flash_model", "unknown")],
        "early_exit": early_exit,
    }


async def advance_chunk(state: PRReviewState) -> dict:
    """Increment chunk index with an inter-chunk delay for rate limiting."""
    settings = get_settings()
    if settings.chunk_delay_ms > 0:
        await asyncio.sleep(settings.chunk_delay_ms / 1000.0)
    return {"current_chunk_idx": state.get("current_chunk_idx", 0) + 1}
