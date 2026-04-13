"""Nodes: merge_results, post_results, handle_degraded."""
from __future__ import annotations

import re
import structlog

from app.models import InlineComment, ReviewOutput, merge_reviews
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
        filtered
        if filtered and len(filtered) == len(chunk_reviews)
        else chunk_reviews
    )
    if not raw_reviews:
        return {"final_summary": "", "inline_comments": []}

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

    log.info(
        "reviews_merged",
        chunks=len(raw_reviews),
        used_filtered=len(filtered) == len(chunk_reviews) and bool(filtered),
        critical=len(merged.critical_issues),
        reliability=len(merged.reliability_issues),
        database=len(merged.database_issues),
        resources=len(merged.resource_management),
        code_quality=len(merged.code_quality),
        input_validation=len(merged.input_validation_issues),
        performance=len(merged.performance_scalability),
        architecture=len(merged.architecture_issues),
        production=len(merged.production_readiness),
        inline=len(all_inline),
    )

    return {
        "final_summary": merged.to_markdown(),
        "inline_comments": all_inline,
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

    slack_msg = _build_slack_message(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        title=state.get("pr_title", ""),
        url=state.get("pr_url", ""),
        summary=summary,
    )
    repo_config = state.get("repo_config") or {}
    slack_channel = repo_config.get("slack_channel")
    log.info(
        "sending_slack_notification",
        repo_config=repo_config,
        slack_channel=slack_channel,
    )
    await slack.send(slack_msg, channel=slack_channel)

    # Try inline comments first, fall back to summary comment.
    inline_posted = False
    if inline and state.get("head_sha"):
        try:
            await github.post_inline_comments(
                owner, repo, pr_number, state["head_sha"], inline,
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


def _convert_markdown_to_slack(text: str) -> str:
    """Convert markdown to Slack-friendly format with better readability."""
    lines = text.split('\n')
    converted_lines = []
    in_code_block = False

    for line in lines:
        # Skip converting content inside code blocks
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            converted_lines.append(line)
            continue

        if in_code_block:
            converted_lines.append(line)
            continue

        # Convert markdown headers (## Header → *Header*)
        if line.startswith('### '):
            line = '*' + line[4:].strip() + '*'
        elif line.startswith('## '):
            line = '*' + line[3:].strip() + '*'
        elif line.startswith('# '):
            line = '*' + line[2:].strip() + '*'

        # Convert **bold** to *bold*
        line = line.replace('**', '*')

        # Convert markdown bullet points with better formatting
        # Handle nested bullets (-, --, etc.)
        if line.startswith('- '):
            indent = len(line) - len(line.lstrip('-').lstrip(' '))
            spaces = '  ' * (indent // 2) if indent > 0 else ''
            line = spaces + '• ' + line.lstrip('- ').strip()

        converted_lines.append(line)

    # Convert markdown links [text](url) to Slack format <url|text>
    result = '\n'.join(converted_lines)
    result = re.sub(r"\[(.+?)\]\((.+?)\)", r"<\2|\1>", result)

    return result


def _build_fallback_note(error: str) -> str:
    return "\n".join([
        "## Automated Review Status",
        "",
        "The review pipeline ran, but could not generate a complete code review.",
        "",
        f"Reason: {error[:220]}",
        "",
        "Please retry after resolving the issue, or set `LLM_MOCK_MODE=true` for testing.",
    ])


def _build_slack_message(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    summary: str,
) -> str:
    """Build a concise, clean Slack message from markdown review."""
    slack_summary = _convert_markdown_to_slack(summary)

    # Extract category sections and count issues
    categories_data = {
        '🔴 Critical': ('critical_issues', 0),
        '🟠 Reliability': ('reliability_issues', 0),
        '🟣 Database': ('database_issues', 0),
        '🔵 Resources': ('resource_management', 0),
        '🟡 Quality': ('code_quality', 0),
        '🟤 Input': ('input_validation', 0),
        '⚫ Performance': ('performance_scalability', 0),
        '🔷 Architecture': ('architecture_issues', 0),
        '⚪ Production': ('production_readiness', 0),
    }

    # Count issues by searching for "**1." pattern in sections
    issue_counts = {}
    lines = slack_summary.split('\n')

    for i, line in enumerate(lines):
        for label, (key, _) in categories_data.items():
            if label in line or any(keyword in line for keyword in ['Critical', 'Reliability', 'Database', 'Resource', 'Quality', 'Input', 'Performance', 'Architecture', 'Production']):
                # Count consecutive issues in this section
                count = 0
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith('**') and '.' in lines[j]:
                        count += 1
                    elif lines[j].startswith('## ') or (lines[j].strip() == '' and j + 1 < len(lines) and lines[j + 1].startswith('## ')):
                        break
                    j += 1
                issue_counts[label] = count
                break

    # Build concise summary
    summary_lines = slack_summary.split('\n')
    concise_findings = []

    for label, count in issue_counts.items():
        if count > 0:
            concise_findings.append(f"*{label}* — {count} issue{'s' if count > 1 else ''}")

    message_parts = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "*📋 PR Review Summary*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*Repo:* {owner}/{repo}",
        f"*PR:* #{pr_number} • {title or '(no title)'}",
        "",
    ]

    if concise_findings:
        message_parts.extend(concise_findings)
        message_parts.append("")

    message_parts.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<{url}|📖 View Full Review on GitHub>",
    ])

    return "\n".join(message_parts)
