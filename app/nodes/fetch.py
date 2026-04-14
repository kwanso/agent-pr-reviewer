"""Node: fetch PR details, check guards, load repo config, fetch files."""

from __future__ import annotations

import structlog

from app.config import get_settings
from app.services.github import get_github_client
from app.state import PRReviewState

log = structlog.get_logger()


async def fetch_pr(state: PRReviewState) -> dict:
    settings = get_settings()
    github = await get_github_client(state.get("installation_id"))
    owner = state["owner"]
    repo = state["repo"]
    pr_number = state["pr_number"]

    pr = await github.get_pr_details(owner, repo, pr_number)
    log.info(
        "pr_fetched",
        owner=owner,
        repo=repo,
        pr=pr_number,
        state=pr["state"],
        draft=pr["draft"],
        labels=pr["labels"],
        changed_files=pr["changed_files"],
    )

    # ── Guards ────────────────────────────────────────────────────
    if pr["state"] == "closed" or pr["draft"]:
        log.warning("pr_skipped", reason="closed_or_draft")
        return {"skipped": True, "skip_reason": "closed_or_draft"}

    if any(lbl.lower() == "no-ai-review" for lbl in pr["labels"]):
        log.warning("pr_skipped", reason="no_ai_review_label", labels=pr["labels"])
        return {"skipped": True, "skip_reason": "no_ai_review_label"}

    if pr["changed_files"] > settings.max_files:
        log.warning(
            "pr_skipped",
            reason="max_files_exceeded",
            changed_files=pr["changed_files"],
            max_files=settings.max_files,
        )
        return {"skipped": True, "skip_reason": "max_files_exceeded"}

    # ── Repo config (.pr-review.yml) ─────────────────────────────
    head_sha = state.get("head_sha") or "HEAD"
    repo_config = await github.get_repo_config(owner, repo, head_sha)
    log.info(
        "repo_config_loaded",
        config=repo_config,
        slack_channel=repo_config.get("slack_channel") if repo_config else None,
    )
    repo_context_block = _build_repo_context(repo_config) if repo_config else ""

    # ── PR files ─────────────────────────────────────────────────
    files = await github.get_pr_files(owner, repo, pr_number)
    log.info("pr_files_fetched", count=len(files))

    return {
        "pr_title": pr["title"],
        "pr_body": pr["body"],
        "pr_url": pr["html_url"],
        "pr_draft": pr["draft"],
        "pr_state": pr["state"],
        "pr_labels": pr["labels"],
        "pr_files": files,
        "repo_config": repo_config,
        "repo_context_block": repo_context_block,
    }


def _sanitize_config_value(value: any, max_length: int = 500) -> str:
    """Safely convert config value to string, rejecting complex types.

    Prevents prompt injection by:
    1. Rejecting nested dicts/lists (only allow scalar strings)
    2. Truncating to max_length
    3. Removing control characters
    """
    if isinstance(value, (dict, list)):
        return ""

    s = str(value)[:max_length]
    # Keep only printable characters + newline/tab
    return "".join(c for c in s if c.isprintable() or c in "\n\t")


def _build_repo_context(config: dict) -> str:
    """Build repo context block from config, with injection prevention."""
    parts: list[str] = []

    # Only process known safe keys
    safe_keys = ["language", "framework", "review_focus"]

    for key in safe_keys:
        value = config.get(key)
        if not value:
            continue

        # Handle special case for lists
        if key == "review_focus" and isinstance(value, list):
            focus_items = [_sanitize_config_value(f) for f in value[:5]]  # Max 5 items
            focus_items = [f for f in focus_items if f]  # Filter empty
            if focus_items:
                parts.append(f"Review focus: {', '.join(focus_items)}")
        else:
            sanitized = _sanitize_config_value(value)
            if sanitized:
                # Capitalize key for readability
                key_display = key.replace("_", " ").title()
                parts.append(f"{key_display}: {sanitized}")

    return "\n".join(parts)
