"""Tests for app.services.rag — index building, retrieval, and cleanup."""
from __future__ import annotations

import pytest

from app.services.rag import _stores, build_index, cleanup, retrieve


# ── Cleanup ──────────────────────────────────────────────────────────


class TestCleanup:
    def test_cleanup_removes_store(self):
        _stores["test-id"] = "fake-store"
        cleanup("test-id")
        assert "test-id" not in _stores

    def test_cleanup_nonexistent_is_noop(self):
        cleanup("nonexistent-id")  # should not raise


# ── Retrieve ─────────────────────────────────────────────────────────


class TestRetrieve:
    def test_retrieve_no_store_returns_empty(self):
        result = retrieve("missing-id", "query")
        assert result == []

    def test_retrieve_with_mock_store(self):
        class MockStore:
            def similarity_search(self, query, k=5):
                return [f"doc about {query}"]

        _stores["mock-id"] = MockStore()
        try:
            result = retrieve("mock-id", "auth")
            assert len(result) == 1
            assert "auth" in result[0]
        finally:
            cleanup("mock-id")

    def test_retrieve_handles_exception(self):
        class FailingStore:
            def similarity_search(self, query, k=5):
                raise RuntimeError("embedding failed")

        _stores["fail-id"] = FailingStore()
        try:
            result = retrieve("fail-id", "query")
            assert result == []
        finally:
            cleanup("fail-id")


# ── Build index ──────────────────────────────────────────────────────


class TestBuildIndex:
    @pytest.mark.asyncio
    async def test_build_index_no_contents_returns_false(self):
        result = await build_index("d1", [], api_key="key")
        assert result is False

    @pytest.mark.asyncio
    async def test_build_index_no_api_key_returns_false(self):
        contents = [{"filename": "a.py", "content": "def foo(): pass"}]
        result = await build_index("d2", contents, api_key="")
        assert result is False

    @pytest.mark.asyncio
    async def test_build_index_empty_content_returns_false(self):
        contents = [{"filename": "a.py", "content": ""}]
        result = await build_index("d3", contents, api_key="key")
        assert result is False
