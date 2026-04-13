"""Tests for app.graph — routing functions and graph structure."""

from __future__ import annotations

import pytest

from app.graph import (
    build_graph,
    route_after_analysis,
    route_after_fetch,
    route_after_merge,
    route_after_review,
    route_after_validate,
)

# ── route_after_fetch ────────────────────────────────────────────────


class TestRouteAfterFetch:
    def test_skipped_returns_skip(self):
        assert route_after_fetch({"skipped": True}) == "skip"

    def test_not_skipped_returns_continue(self):
        assert route_after_fetch({}) == "continue"
        assert route_after_fetch({"skipped": False}) == "continue"


# ── route_after_analysis ─────────────────────────────────────────────


class TestRouteAfterAnalysis:
    def test_skipped_returns_skip(self):
        assert route_after_analysis({"skipped": True}) == "skip"

    def test_not_skipped_returns_continue(self):
        assert route_after_analysis({}) == "continue"
        assert route_after_analysis({"skipped": False}) == "continue"


# ── route_after_review ───────────────────────────────────────────────


class TestRouteAfterReview:
    def test_early_exit_returns_merge(self):
        state = {
            "early_exit": True,
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
        }
        assert route_after_review(state) == "merge"

    def test_last_chunk_with_reviews_returns_validate(self):
        state = {
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
        }
        assert route_after_review(state) == "validate"

    def test_last_chunk_without_reviews_returns_degraded(self):
        state = {
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [],
        }
        assert route_after_review(state) == "degraded"

    def test_more_chunks_returns_validate(self):
        state = {
            "chunk_plans": [{"risk_score": 1}, {"risk_score": 2}],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
        }
        assert route_after_review(state) == "validate"

    def test_quota_exhausted_skips_low_risk_remaining(self):
        state = {
            "quota_exhausted": True,
            "chunk_plans": [
                {"risk_score": 3},
                {"risk_score": 1},  # low-risk, should be skippable
            ],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
        }
        assert route_after_review(state) == "validate"

    def test_quota_exhausted_continues_for_high_risk(self):
        state = {
            "quota_exhausted": True,
            "chunk_plans": [
                {"risk_score": 3},
                {"risk_score": 5},  # high-risk, should continue
            ],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
        }
        assert route_after_review(state) == "validate"

    def test_empty_plans_returns_degraded(self):
        state = {
            "chunk_plans": [],
            "current_chunk_idx": 0,
            "chunk_reviews": [],
        }
        assert route_after_review(state) == "degraded"


# ── route_after_validate ─────────────────────────────────────────────


class TestRouteAfterValidate:
    def test_last_chunk_returns_merge(self):
        state = {
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
        }
        assert route_after_validate(state) == "merge"

    def test_more_chunks_returns_advance(self):
        state = {
            "chunk_plans": [{"risk_score": 1}, {"risk_score": 2}],
            "current_chunk_idx": 0,
        }
        assert route_after_validate(state) == "advance"


# ── route_after_merge ────────────────────────────────────────────────


class TestRouteAfterMerge:
    def test_empty_summary_returns_degraded(self):
        assert route_after_merge({"final_summary": ""}) == "degraded"

    def test_no_summary_returns_degraded(self):
        assert route_after_merge({}) == "degraded"

    def test_with_summary_returns_publish(self):
        assert route_after_merge({"final_summary": "## Review"}) == "publish"


# ── Graph structure ──────────────────────────────────────────────────


class TestBuildGraph:
    def test_graph_builds_without_error(self):
        builder = build_graph()
        assert builder is not None

    def test_graph_has_expected_nodes(self):
        builder = build_graph()
        expected_nodes = {
            "fetch_pr",
            "analyze_diff",
            "build_rag_index",
            "review_chunk",
            "validate_findings",
            "advance_chunk",
            "merge_results",
            "post_results",
            "handle_degraded",
        }
        # StateGraph stores nodes in .nodes dict
        assert expected_nodes.issubset(set(builder.nodes.keys()))

    def test_graph_compiles(self):
        builder = build_graph()
        compiled = builder.compile()
        assert compiled is not None
