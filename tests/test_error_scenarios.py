"""Integration tests for error scenarios and edge cases.

Tests error handling, recovery, and fallback behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.errors import ErrorType, classify_llm_error
from app.graph import route_after_review
from app.models import ReviewFinding, ReviewOutput
from app.nodes.review import _success
from app.state import PRReviewState


class TestErrorClassification:
    """Tests for error classification system."""

    def test_classify_quota_error(self):
        """Classify 429 rate limit errors."""
        exc = Exception("429: Quota exceeded. Retry in 60 seconds.")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.QUOTA_EXHAUSTED
        assert error.retriable is True
        assert error.retry_after_s > 60  # Includes safety margin

    def test_classify_service_unavailable(self):
        """Classify UNAVAILABLE errors."""
        exc = Exception("Service UNAVAILABLE: Backend overloaded")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.SERVICE_UNAVAILABLE
        assert error.retriable is True

    def test_classify_timeout(self):
        """Classify timeout errors."""
        exc = Exception("Request timeout after 30 seconds")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.TIMEOUT
        assert error.retriable is True

    def test_classify_auth_failed(self):
        """Classify authentication errors (non-retriable)."""
        exc = Exception("401: Invalid API key")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.AUTH_FAILED
        assert error.retriable is False

    def test_classify_invalid_request(self):
        """Classify invalid request errors (non-retriable)."""
        exc = Exception("400: Bad request; malformed input")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.INVALID_REQUEST
        assert error.retriable is False

    def test_classify_not_found(self):
        """Classify 404 not found errors (non-retriable)."""
        exc = Exception("404: File not found")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.NOT_FOUND
        assert error.retriable is False

    def test_classify_unknown_error(self):
        """Unknown errors are non-retriable."""
        exc = Exception("Something went very wrong and we don't know what")
        error = classify_llm_error(exc)

        assert error.error_type == ErrorType.UNKNOWN
        assert error.retriable is False


class TestReviewFindingValidation:
    """Tests for finding validation and evidence requirements."""

    def test_finding_low_confidence_no_evidence_allowed(self):
        """Low confidence findings don't require evidence."""
        finding = ReviewFinding(
            issue="Minor code style issue",
            confidence=0.4,
            evidence="",  # Empty is OK for low confidence
        )
        # Should not raise and should keep low confidence
        assert finding.confidence == 0.4

    def test_finding_high_confidence_missing_evidence_penalty(self):
        """High confidence findings without evidence get confidence penalty."""
        finding = ReviewFinding(
            issue="Critical security issue",
            confidence=0.85,
            evidence="",  # Empty evidence
        )
        # Confidence should be reduced
        assert (
            finding.confidence < 0.6
        ), "High confidence without evidence should be penalized"

    def test_finding_high_confidence_with_evidence(self):
        """High confidence findings with evidence are preserved."""
        finding = ReviewFinding(
            issue="SQL injection risk",
            confidence=0.9,
            evidence="Line 42: query = f'SELECT * FROM users WHERE id={user_id}'",
        )
        # Confidence should be preserved
        assert finding.confidence >= 0.85


class TestReviewChunkErrorHandling:
    """Tests for error handling in chunk review."""

    @pytest.mark.asyncio
    async def test_quota_exhaustion_stops_processing(self):
        """When quota is exhausted, stop processing remaining chunks."""
        state: PRReviewState = {
            "owner": "test",
            "repo": "test",
            "pr_number": 1,
            "delivery_id": "test-delivery",
            "chunk_plans": [
                {
                    "risk_score": 1,
                    "review_mode": "light",
                    "chunk_text": "code1",
                    "files": [],
                },
                {
                    "risk_score": 1,
                    "review_mode": "light",
                    "chunk_text": "code2",
                    "files": [],
                },
            ],
            "current_chunk_idx": 0,
            "chunk_reviews": [],
            "quota_exhausted": False,
        }

        # After chunk 0, quota is exhausted
        state["quota_exhausted"] = True
        state["current_chunk_idx"] = 1

        # Should skip low-risk chunks when quota exhausted
        from app.config import get_settings
        from app.nodes.review import review_chunk

        # Mock to check if we skip processing
        with patch("app.nodes.review.log") as mock_log:
            result = await review_chunk(state)
            # Should return empty (skip processing)
            assert result == {} or result.get("error")

    @pytest.mark.asyncio
    async def test_token_budget_stops_processing(self):
        """When token budget is exhausted, stop processing."""
        from app.utils.tokens import is_budget_exhausted

        # Direct test of budget exhaustion check
        used = 9000
        max_budget = 10000
        threshold = 0.9

        exhausted = is_budget_exhausted(used, max_budget, threshold)
        assert exhausted is True

        # Test when not exhausted
        used = 8000
        exhausted = is_budget_exhausted(used, max_budget, threshold)
        assert exhausted is False


class TestValidationPipelineAllChunks:
    """Tests for validation of all chunks, not just the latest."""

    @pytest.mark.asyncio
    async def test_validate_all_unvalidated_chunks(self):
        """Validate all chunks that haven't been validated yet."""
        from app.nodes.validate import validate_findings

        state: PRReviewState = {
            "owner": "test",
            "repo": "test",
            "pr_number": 1,
            "delivery_id": "test-delivery",
            "chunk_plans": [
                {"chunk_text": "code1", "files": ["file1.py"], "risk_score": 1},
                {"chunk_text": "code2", "files": ["file2.py"], "risk_score": 1},
            ],
            "current_chunk_idx": 1,
            "chunk_reviews": [
                {
                    "chunk_index": 0,
                    "critical_issues": [],
                    "reliability_issues": [],
                    "database_issues": [],
                    "resource_management": [],
                    "code_quality": [],
                    "input_validation_issues": [],
                    "performance_scalability": [],
                    "architecture_issues": [],
                    "production_readiness": [],
                    "inline_comments": [],
                },
                {
                    "chunk_index": 1,
                    "critical_issues": [],
                    "reliability_issues": [],
                    "database_issues": [],
                    "resource_management": [],
                    "code_quality": [],
                    "input_validation_issues": [],
                    "performance_scalability": [],
                    "architecture_issues": [],
                    "production_readiness": [],
                    "inline_comments": [],
                },
            ],
            "filtered_reviews": [],  # None validated yet
        }

        with patch("app.nodes.review.log"):
            with patch("app.nodes.review.ChatGoogleGenerativeAI"):
                result = await validate_findings(state)
                # Should return both chunks in filtered_reviews
                assert len(result.get("filtered_reviews", [])) == 2


class TestRoutingErrorConditions:
    """Tests for graph routing under error conditions."""

    def test_route_after_review_quota_exhausted(self):
        """Route to degraded if quota exhausted with no reviews."""
        state: PRReviewState = {
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [],
            "quota_exhausted": True,
        }
        route = route_after_review(state)
        assert route == "degraded"

    def test_route_after_review_token_budget_exhausted(self):
        """Route to degraded if token budget exhausted with no reviews."""
        state: PRReviewState = {
            "chunk_plans": [{"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [],
            "token_budget_exhausted": True,
        }
        route = route_after_review(state)
        assert route == "degraded"

    def test_route_after_review_early_exit_with_reviews(self):
        """Early exit routes to merge if we have reviews."""
        state: PRReviewState = {
            "chunk_plans": [{"risk_score": 1}, {"risk_score": 1}],
            "current_chunk_idx": 0,
            "chunk_reviews": [{"some": "review"}],
            "early_exit": True,
        }
        route = route_after_review(state)
        assert route == "merge"


class TestRAGCleanup:
    """Tests for RAG memory management."""

    def test_rag_cleanup_on_review_complete(self):
        """RAG indices are cleaned up after review."""
        from app.services import rag

        # Add a fake index
        rag._stores["test-delivery"] = MagicMock()
        rag._bm25_stores["test-delivery"] = MagicMock()
        rag._metadata_stores["test-delivery"] = {"file": "data"}
        rag._index_created_at["test-delivery"] = 1.0

        # Cleanup
        rag.cleanup("test-delivery")

        # Should be removed
        assert "test-delivery" not in rag._stores
        assert "test-delivery" not in rag._bm25_stores
        assert "test-delivery" not in rag._metadata_stores

    def test_rag_cleanup_expired_indices(self):
        """Expired indices are removed automatically."""
        import time

        from app.services import rag

        # Add old and new indices
        old_time = time.time() - (25 * 3600)  # 25 hours ago
        new_time = time.time()

        rag._stores["old-delivery"] = MagicMock()
        rag._stores["new-delivery"] = MagicMock()
        rag._index_created_at["old-delivery"] = old_time
        rag._index_created_at["new-delivery"] = new_time

        # Cleanup expired
        rag.cleanup_expired_indices()

        # Old should be removed, new should remain
        assert "old-delivery" not in rag._stores
        assert "new-delivery" in rag._stores


class TestWebhookValidation:
    """Tests for webhook payload validation."""

    def test_webhook_payload_valid(self):
        """Valid webhook payload passes validation."""
        from app.server import GitHubWebhookPayload

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "Fix bug",
                "body": "Description",
                "draft": False,
                "state": "open",
                "head": {"sha": "abc123"},
                "labels": [],
                "changed_files": 3,
            },
            "repository": {
                "name": "my-repo",
                "owner": {"login": "user"},
            },
            "installation": {"id": 123},
        }

        validated = GitHubWebhookPayload(**payload)
        assert validated.pull_request.number == 42

    def test_webhook_payload_accepts_any_action(self):
        """Webhook accepts any action; filtering happens later."""
        from app.server import GitHubWebhookPayload

        payload = {
            "action": "deleted",  # Not in our handled list
            "pull_request": {
                "number": 42,
                "title": "Fix",
                "body": "",
                "draft": False,
                "state": "open",
                "head": {"sha": "abc123"},
                "labels": [],
                "changed_files": 3,
            },
            "repository": {
                "name": "repo",
                "owner": {"login": "user"},
            },
        }

        # Should validate successfully; filtering happens in webhook handler
        payload_obj = GitHubWebhookPayload(**payload)
        assert payload_obj.action == "deleted"

    def test_webhook_payload_missing_pr_number(self):
        """Missing PR number is rejected."""
        from pydantic import ValidationError

        from app.server import GitHubWebhookPayload

        payload = {
            "action": "opened",
            "pull_request": {
                # Missing number
                "title": "Fix",
                "body": "",
                "draft": False,
                "state": "open",
                "head": {"sha": "abc123"},
                "labels": [],
                "changed_files": 3,
            },
            "repository": {
                "name": "repo",
                "owner": {"login": "user"},
            },
        }

        with pytest.raises(ValidationError):
            GitHubWebhookPayload(**payload)
