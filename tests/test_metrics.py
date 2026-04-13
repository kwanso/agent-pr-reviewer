"""Tests for app.utils.metrics — counter operations and summary computation."""
from __future__ import annotations

import pytest

from app.utils.metrics import increment, reset, snapshot, summary


@pytest.fixture(autouse=True)
def _clean_counters():
    """Reset counters before each test to avoid cross-test pollution."""
    reset()
    yield
    reset()


# ── Basic operations ─────────────────────────────────────────────────


class TestIncrement:
    def test_increment_creates_counter(self):
        increment("new_counter")
        assert snapshot()["new_counter"] == 1

    def test_increment_adds_to_existing(self):
        increment("counter", 5)
        increment("counter", 3)
        assert snapshot()["counter"] == 8

    def test_increment_default_by_one(self):
        increment("one")
        increment("one")
        assert snapshot()["one"] == 2


class TestSnapshot:
    def test_empty_snapshot(self):
        assert snapshot() == {}

    def test_snapshot_is_copy(self):
        increment("x")
        snap = snapshot()
        snap["x"] = 999
        assert snapshot()["x"] == 1


class TestReset:
    def test_reset_clears_all(self):
        increment("a", 10)
        increment("b", 20)
        reset()
        assert snapshot() == {}


# ── Summary ──────────────────────────────────────────────────────────


class TestSummary:
    def test_empty_summary(self):
        s = summary()
        assert s["review_outcomes"]["total"] == 0
        assert s["review_outcomes"]["full_rate"] == 0
        assert s["review_outcomes"]["fallback_rate"] == 0
        assert s["review_outcomes"]["by_reason"] == {}
        assert s["provider_signals"]["github_content_403"] == 0

    def test_summary_computes_rates(self):
        increment("reviews_total", 10)
        increment("reviews_full", 7)
        increment("reviews_fallback_total", 3)

        s = summary()
        assert s["review_outcomes"]["total"] == 10
        assert s["review_outcomes"]["full"] == 7
        assert s["review_outcomes"]["fallback"] == 3
        assert s["review_outcomes"]["full_rate"] == 0.7
        assert s["review_outcomes"]["fallback_rate"] == 0.3

    def test_summary_fallback_reasons(self):
        increment("reviews_fallback_by_reason.low_quality", 2)
        increment("reviews_fallback_by_reason.provider_unavailable", 1)

        s = summary()
        assert s["review_outcomes"]["by_reason"]["low_quality"] == 2
        assert s["review_outcomes"]["by_reason"]["provider_unavailable"] == 1

    def test_summary_provider_signals(self):
        increment("github_content_403_total", 3)
        increment("llm_429_total", 5)
        increment("low_quality_total", 2)

        s = summary()
        assert s["provider_signals"]["github_content_403"] == 3
        assert s["provider_signals"]["llm_429"] == 5
        assert s["provider_signals"]["low_quality"] == 2
