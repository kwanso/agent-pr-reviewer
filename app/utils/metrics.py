"""Simple in-process counters for review-pipeline observability.

Ported from the JS agent's ``metrics.js``.  Thread-safe via a lock so
``increment`` can be called from async background tasks safely.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_counters: dict[str, int] = {}


def increment(name: str, by: int = 1) -> None:
    """Atomically bump counter *name* by *by*."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + by


def snapshot() -> dict[str, int]:
    """Return a shallow copy of all counters."""
    with _lock:
        return dict(_counters)


def reset() -> None:
    """Clear all counters (useful in tests)."""
    with _lock:
        _counters.clear()


def summary() -> dict[str, Any]:
    """Compute a high-level summary identical to the JS ``getMetricsSummary``.

    Returns::

        {
            "review_outcomes": {
                "total": int,
                "full": int,
                "fallback": int,
                "full_rate": float,
                "fallback_rate": float,
                "by_reason": { "<reason>": int, ... },
            },
            "provider_signals": {
                "github_content_403": int,
                "llm_429": int,
                "low_quality": int,
            },
        }
    """
    snap = snapshot()
    total = snap.get("reviews_total", 0)
    full = snap.get("reviews_full", 0)
    fallback = snap.get("reviews_fallback_total", 0)

    by_reason: dict[str, int] = {}
    prefix = "reviews_fallback_by_reason."
    for key, val in snap.items():
        if key.startswith(prefix):
            by_reason[key[len(prefix) :]] = val

    return {
        "review_outcomes": {
            "total": total,
            "full": full,
            "fallback": fallback,
            "full_rate": round(full / total, 4) if total else 0,
            "fallback_rate": round(fallback / total, 4) if total else 0,
            "by_reason": by_reason,
        },
        "provider_signals": {
            "github_content_403": snap.get("github_content_403_total", 0),
            "llm_429": snap.get("llm_429_total", 0),
            "low_quality": snap.get("low_quality_total", 0),
        },
    }
