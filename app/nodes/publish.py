"""Nodes: merge_results, post_results, handle_degraded."""

from __future__ import annotations

import re

import structlog

from app.models import (
    REVIEW_BUCKET_FIELDS,
    InlineComment,
    ReviewFinding,
    ReviewOutput,
    merge_reviews,
)
from app.services import rag
from app.services.github import get_github_client
from app.services.slack import get_slack_notifier
from app.state import PRReviewState

log = structlog.get_logger()


async def merge_results(state: PRReviewState) -> dict:
    """Combine all chunk reviews into one final summary."""
    chunk_reviews = state.get("chunk_reviews", [])
    filtered = state.get("filtered_reviews", [])
    raw_reviews = (
        filtered if filtered and len(filtered) == len(chunk_reviews) else chunk_reviews
    )
    if not raw_reviews:
        return {"final_summary": "", "inline_comments": [], "review_counts": {}}

    review_fields = set(ReviewOutput.model_fields)
    reviews = [
        ReviewOutput(**{k: v for k, v in r.items() if k in review_fields})
        for r in raw_reviews
    ]
    merged = merge_reviews(reviews)

    all_inline: list[dict] = []
    for r in raw_reviews:
        for ic in r.get("inline_comments", []):
            if isinstance(ic, dict):
                all_inline.append(ic)
            elif isinstance(ic, InlineComment):
                all_inline.append(ic.model_dump())

    # Extract per-category counts (only counting findings with confidence >= 0.5)
    review_counts = {}
    for field in REVIEW_BUCKET_FIELDS:
        items = getattr(merged, field, [])
        count = len([
            f for f in items
            if isinstance(f, ReviewFinding) and f.confidence >= 0.5
        ])
        review_counts[field] = count

    log.info(
        "reviews_merged",
        chunks=len(raw_reviews),
        used_filtered=len(filtered) == len(chunk_reviews) and bool(filtered),
        critical=review_counts.get("critical_issues", 0),
        reliability=review_counts.get("reliability_issues", 0),
        database=review_counts.get("database_issues", 0),
        resources=review_counts.get("resource_management", 0),
        code_quality=review_counts.get("code_quality", 0),
        input_validation=review_counts.get("input_validation_issues", 0),
        performance=review_counts.get("performance_scalability", 0),
        architecture=review_counts.get("architecture_issues", 0),
        production=review_counts.get("production_readiness", 0),
        inline=len(all_inline),
    )

    return {
        "final_summary": merged.to_markdown(),
        "inline_comments": all_inline,
        "review_counts": review_counts,
    }


async def post_results(state: PRReviewState) -> dict:
    """Post the review to GitHub and notify Slack."""
    github = await get_github_client(state.get("installation_id"))
    slack = get_slack_notifier()
    owner = state["owner"]
    repo = state["repo"]
    pr_number = state["pr_number"]

    summary = state.get("final_summary", "")
    inline = state.get("inline_comments", [])

    slack_msg, attachments = _build_slack_message(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        title=state.get("pr_title", ""),
        url=state.get("pr_url", ""),
        counts=state.get("review_counts", {}),
    )
    repo_config = state.get("repo_config") or {}
    slack_channel = repo_config.get("slack_channel")
    log.info(
        "sending_slack_notification",
        repo_config=repo_config,
        slack_channel=slack_channel,
    )
    await slack.send(slack_msg, channel=slack_channel, attachments=attachments)

    # Try inline comments first, fall back to summary comment.
    inline_posted = False
    if inline and state.get("head_sha"):
        try:
            await github.post_inline_comments(
                owner,
                repo,
                pr_number,
                state["head_sha"],
                inline,
            )
            inline_posted = True
        except Exception as exc:
            log.warning("inline_comments_failed", error=str(exc))

    if not inline_posted:
        await github.post_comment(owner, repo, pr_number, summary)

    rag.cleanup(state.get("delivery_id", ""))
    log.info("review_posted", owner=owner, repo=repo, pr=pr_number)
    return {"slack_message": slack_msg}


async def handle_degraded(state: PRReviewState) -> dict:
    """Post a fallback note when no usable review could be generated."""
    github = await get_github_client(state.get("installation_id"))
    slack = get_slack_notifier()
    owner = state["owner"]
    repo = state["repo"]
    pr_number = state["pr_number"]
    error = state.get("error", "Unknown error")

    note = _build_fallback_note(error)
    await github.post_comment(owner, repo, pr_number, note)
    repo_config = state.get("repo_config") or {}
    slack_channel = repo_config.get("slack_channel")
    log.info(
        "sending_degraded_slack_notification",
        repo_config=repo_config,
        slack_channel=slack_channel,
    )
    await slack.send(
        f"*PR Review Status*\n"
        f"*Repo:* {owner}/{repo}\n"
        f"*PR:* #{pr_number}\n\n{note}",
        channel=slack_channel,
    )

    rag.cleanup(state.get("delivery_id", ""))
    log.warning("degraded_review_posted", owner=owner, repo=repo, pr=pr_number)
    return {"final_summary": note}


# ── Formatting helpers ───────────────────────────────────────────────


def _build_fallback_note(error: str) -> str:
    return "\n".join(
        [
            "## Automated Review Status",
            "",
            "The review pipeline ran, but could not generate a complete code review.",
            "",
            f"Reason: {error[:220]}",
            "",
            "Please retry after resolving the issue, or set `LLM_MOCK_MODE=true` for testing.",
        ]
    )


def _build_slack_message(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    counts: dict,
) -> tuple[str, list]:
    """Build a Slack message with 3-tier severity breakdown and color coding.

    Returns (fallback_text, [attachment]) for rich formatting.
    """
    # Map categories to severity tiers
    high_categories = ["critical_issues"]
    medium_categories = [
        "reliability_issues",
        "database_issues",
        "resource_management",
        "input_validation_issues",
    ]
    low_categories = [
        "code_quality",
        "performance_scalability",
        "architecture_issues",
        "production_readiness",
    ]

    # Compute tier totals
    high_count = sum(counts.get(cat, 0) for cat in high_categories)
    medium_count = sum(counts.get(cat, 0) for cat in medium_categories)
    low_count = sum(counts.get(cat, 0) for cat in low_categories)
    total_count = high_count + medium_count + low_count

    # Determine overall status and color
    if high_count > 0:
        status = "🚨 Needs Attention"
        color = "#E01E5A"  # red
    elif medium_count > 0:
        status = "⚠️ Review Recommended"
        color = "#ECB22E"  # orange
    else:
        status = "✅ Looks Clean"
        color = "#2EB67D"  # green

    # Build severity breakdown lines
    severity_lines = [f"*🔴 High:* {high_count}"]
    if medium_count > 0:
        severity_lines.append(f"*🟠 Medium:* {medium_count}")
    if low_count > 0:
        severity_lines.append(f"*🔵 Low:* {low_count}")

    # Build per-category breakdown for each non-empty tier
    detail_lines = []

    if high_count > 0:
        detail_lines.append("*High severity:*")
        for cat in high_categories:
            count = counts.get(cat, 0)
            if count > 0:
                detail_lines.append(f"  • {_format_category_name(cat)}: {count}")

    if medium_count > 0:
        detail_lines.append("*Medium severity:*")
        for cat in medium_categories:
            count = counts.get(cat, 0)
            if count > 0:
                detail_lines.append(f"  • {_format_category_name(cat)}: {count}")

    if low_count > 0:
        detail_lines.append("*Low severity:*")
        for cat in low_categories:
            count = counts.get(cat, 0)
            if count > 0:
                detail_lines.append(f"  • {_format_category_name(cat)}: {count}")

    # Build Slack attachment (Block Kit)
    fallback_text = f"PR Review: {title or 'No title'} - {status}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📋 PR Review Summary",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Repo:*\n{owner}/{repo}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*PR:*\n#{pr_number}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Title:*\n{title or '(no title)'}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Status:*\n{status}",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": " | ".join(severity_lines),
            },
        },
    ]

    # Add detail breakdown if there are any issues
    if detail_lines:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(detail_lines),
                },
            }
        )

    # Add total findings and link
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Total findings:* {total_count}\n<{url}|📖 View Full Review on GitHub>",
            },
        }
    )

    attachment = {
        "fallback": fallback_text,
        "color": color,
        "blocks": blocks,
    }

    # Also return plain-text version as primary message text
    plain_text = f"{fallback_text}\n\n{' | '.join(severity_lines)}"

    return (plain_text, [attachment])


def _format_category_name(category: str) -> str:
    """Convert category field name to readable label."""
    labels = {
        "critical_issues": "Critical Issues",
        "reliability_issues": "Reliability Issues",
        "database_issues": "Database Issues",
        "resource_management": "Resource Management",
        "input_validation_issues": "Input Validation",
        "code_quality": "Code Quality",
        "performance_scalability": "Performance & Scalability",
        "architecture_issues": "Architecture Issues",
        "production_readiness": "Production Readiness",
    }
    return labels.get(category, category.replace("_", " ").title())
