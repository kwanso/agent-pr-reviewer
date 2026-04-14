"""Token estimation and budget management utilities."""

from __future__ import annotations


def estimate_tokens(text: str, model: str = "gemini-2.5-flash") -> int:
    """Estimate token count for LLM input.

    Uses a simple heuristic: ~4 characters per token for most models.
    This is approximate but good enough for budgeting.

    Args:
        text: Text to estimate tokens for
        model: LLM model name (for future model-specific estimation)

    Returns:
        Estimated token count
    """
    # Gemini and most models use ~4 chars per token on average
    # JSON/code is denser, prose is more sparse
    char_count = len(text)
    return max(1, char_count // 4)


def estimate_messages_tokens(messages: list, model: str = "gemini-2.5-flash") -> int:
    """Estimate total tokens for a list of messages.

    Handles both plain dicts and LangChain Message objects.

    Args:
        messages: List of message dicts or LangChain Message objects
        model: LLM model name

    Returns:
        Estimated token count including overhead
    """
    total = 0
    for msg in messages:
        # Handle LangChain Message objects (have .content attribute)
        if hasattr(msg, "content"):
            content = msg.content
        # Handle plain dicts
        elif isinstance(msg, dict):
            content = msg.get("content", "")
        else:
            continue

        if isinstance(content, str):
            total += estimate_tokens(content, model)
        elif isinstance(content, list):
            # For structured content (e.g., vision messages)
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    total += estimate_tokens(item["text"], model)

    # Add 10% overhead for message framing
    return int(total * 1.1)


def is_budget_exhausted(used: int, max_budget: int, threshold_pct: float = 0.9) -> bool:
    """Check if token budget is exhausted.

    Args:
        used: Tokens used so far
        max_budget: Maximum token budget
        threshold_pct: Percentage (0.0-1.0) at which to consider exhausted

    Returns:
        True if used >= (max_budget * threshold_pct)
    """
    threshold = max_budget * threshold_pct
    return used >= threshold
