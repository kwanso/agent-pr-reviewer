"""Node: validate_findings — false-positive pass on the latest chunk review."""

from __future__ import annotations

import json
from collections import defaultdict

import structlog
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings
from app.models import REVIEW_BUCKET_FIELDS, ReviewFinding, ReviewOutput
from app.state import PRReviewState

log = structlog.get_logger()


def _review_from_chunk_dict(chunk: dict) -> ReviewOutput:
    fields = set(ReviewOutput.model_fields)
    return ReviewOutput(**{k: v for k, v in chunk.items() if k in fields})


def _flatten_for_validation(review: ReviewOutput) -> list[dict]:
    pending: list[dict] = []
    for bucket in REVIEW_BUCKET_FIELDS:
        for item in getattr(review, bucket):
            rf: ReviewFinding
            if isinstance(item, ReviewFinding):
                rf = item
            else:
                rf = ReviewFinding(
                    issue=str(item),
                    why_it_matters="(String finding — validate carefully.)",
                    suggested_fix="See issue text.",
                    confidence=0.7,
                )
            pending.append(
                {
                    "bucket": bucket,
                    "issue": rf.issue,
                    "why_it_matters": rf.why_it_matters,
                    "suggested_fix": rf.suggested_fix,
                    "evidence": rf.evidence,
                    "file_path": rf.file_path,
                    "line": rf.line,
                    "confidence": rf.confidence,
                }
            )
    return pending


def _rebuild_review(pending: list[dict]) -> ReviewOutput:
    buckets: dict[str, list[ReviewFinding]] = defaultdict(list)
    for p in pending:
        if p.get("confidence", 0) < 0.5:
            continue
        b = p.get("bucket", "")
        if b not in REVIEW_BUCKET_FIELDS:
            continue
        buckets[b].append(
            ReviewFinding(
                issue=p["issue"],
                why_it_matters=p["why_it_matters"],
                suggested_fix=p["suggested_fix"],
                evidence=p.get("evidence", ""),
                file_path=p.get("file_path", ""),
                line=p.get("line"),
                confidence=float(p.get("confidence", 0.7)),
            ),
        )
    kw = {name: buckets[name] for name in REVIEW_BUCKET_FIELDS}
    return ReviewOutput(**kw)


async def validate_findings(state: PRReviewState) -> dict:
    """Drop hallucinated or weak findings using the chunk diff + allowed paths as ground truth.

    Validates all unvalidated chunks (chunk_reviews that are not yet in filtered_reviews).
    Uses the corresponding chunk plan to ground truth validation.
    """
    settings = get_settings()
    chunk_reviews = state.get("chunk_reviews", [])
    filtered_reviews = state.get("filtered_reviews", [])
    plans = state.get("chunk_plans", [])

    if not chunk_reviews:
        return {}

    # Determine which chunks need validation
    # filtered_reviews accumulates, so we validate chunk_reviews[len(filtered_reviews):]
    num_already_validated = len(filtered_reviews)
    chunks_to_validate = chunk_reviews[num_already_validated:]

    if not chunks_to_validate:
        return {}

    all_validated = []

    for chunk_review in chunks_to_validate:
        # Get the corresponding plan for this chunk
        chunk_idx = chunk_review.get("chunk_index", 0)
        if chunk_idx >= len(plans):
            log.warning("validate_findings_plan_not_found", chunk_index=chunk_idx)
            all_validated.append(chunk_review)
            continue

        plan = plans[chunk_idx]
        chunk_diff = plan.get("chunk_text", "")
        allowed = plan.get("files", [])

        review = _review_from_chunk_dict(chunk_review)
        pending = _flatten_for_validation(review)

        if not pending:
            # No findings to validate; pass through
            out = {**chunk_review}
            all_validated.append(out)
            continue

        if settings.llm_mock_mode:
            all_validated.append(chunk_review)
            continue

        # Validate this chunk's findings
        findings_text = "\n".join(
            f"[{i}] ({p['bucket']}) {p['issue'][:400]}\n"
            f"    evidence: {p.get('evidence', '')[:300]}\n"
            f"    file_path={p.get('file_path')!r} line={p.get('line')}\n"
            f"    confidence={p.get('confidence', 0.7):.2f}"
            for i, p in enumerate(pending)
        )

        validator_user = f"""Ground-truth for this chunk (chunk_index={chunk_idx}):
ALLOWED_FILE_PATHS (only valid targets for file_path / inline file):
{chr(10).join(f"- {p}" for p in allowed) if allowed else "(paths must match diff headers only)"}

DIFF (excerpt; findings must be supported by this text):
```
{chunk_diff[:12000]}
```

Findings to validate (by index):
{findings_text}

For EACH index, return JSON objects with:
- "index": number
- "keep": true/false  (false if generic, unsupported by diff, wrong file not in ALLOWED_FILE_PATHS, or hallucination)
- "confidence": 0.0-1.0
- "reason": one short sentence

Output a single JSON array only, no markdown."""

        llm = ChatGoogleGenerativeAI(
            model=settings.llm_flash_model,
            google_api_key=settings.llm_api_key,
            temperature=0.0,
            max_output_tokens=2048,
            max_retries=1,
            timeout=settings.llm_timeout_s,
        )

        try:
            response = await llm.ainvoke(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a strict code-review auditor. "
                            "Reject findings that are not clearly supported by the supplied diff excerpt, "
                            "that reference files outside ALLOWED_FILE_PATHS (when that list is non-empty), "
                            "or that restate generic advice. Prefer precision over politeness."
                        ),
                    },
                    {"role": "user", "content": validator_user},
                ]
            )

            validated_pending = [dict(p) for p in pending]
            text = response.content
            start = text.find("[")
            end = text.rfind("]") + 1
            if start != -1 and end > start:
                adjustments = json.loads(text[start:end])
                for adj in adjustments:
                    i = int(adj.get("index", -1))
                    if 0 <= i < len(validated_pending):
                        if not adj.get("keep", True):
                            validated_pending[i]["confidence"] = 0.0
                        else:
                            c = float(
                                adj.get(
                                    "confidence", validated_pending[i]["confidence"]
                                )
                            )
                            validated_pending[i]["confidence"] = max(0.0, min(1.0, c))

            rebuilt = _rebuild_review(validated_pending)
            rebuilt.inline_comments = review.inline_comments

            filtered = rebuilt.model_dump()
            filtered["chunk_index"] = chunk_review.get("chunk_index", chunk_idx)
            filtered["review_mode"] = chunk_review.get("review_mode", "")
            filtered["risk_score"] = chunk_review.get("risk_score", 0)

            log.info(
                "findings_validated",
                chunk_index=chunk_idx,
                total=len(pending),
                kept=sum(1 for p in validated_pending if p.get("confidence", 0) >= 0.5),
            )

            all_validated.append(filtered)

        except Exception as exc:
            log.warning("validation_failed", chunk_index=chunk_idx, error=str(exc))
            # On validation error, pass through the original chunk review
            all_validated.append(chunk_review)

    return {"filtered_reviews": all_validated}
