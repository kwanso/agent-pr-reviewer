"""Tests for app.services.github — inline comment validation and request logic."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.github import GitHubClient, GitHubError, _classify_status

# ── Status classification ────────────────────────────────────────────


class TestClassifyStatus:
    def test_401_auth(self):
        assert _classify_status(401) == "auth_invalid"

    def test_403_permission(self):
        assert _classify_status(403) == "permission_denied"

    def test_404_not_found(self):
        assert _classify_status(404) == "not_found"

    def test_429_rate_limited(self):
        assert _classify_status(429) == "rate_limited"

    def test_500_server_error(self):
        assert _classify_status(500) == "server_error"

    def test_502_server_error(self):
        assert _classify_status(502) == "server_error"

    def test_418_unknown(self):
        assert _classify_status(418) == "unknown"


# ── Inline comment normalisation ─────────────────────────────────────


class TestInlineCommentNormalisation:
    """Verify that post_inline_comments correctly filters and caps comments."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_valid_comments_are_posted(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/pulls/1/reviews",
        ).respond(200, json={})

        comments = [
            {"file": "a.py", "line": 10, "comment": "Fix this"},
            {"file": "b.py", "line": 20, "comment": "And this"},
        ]
        await client.post_inline_comments("owner", "repo", 1, "abc123", comments)
        assert route.called
        body = route.calls[0].request.content
        assert b"a.py" in body
        assert b"b.py" in body
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_invalid_comments_are_filtered(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/pulls/1/reviews",
        ).respond(200, json={})

        comments = [
            {"file": "", "line": 10, "comment": "no file"},  # empty file
            {"file": "a.py", "line": None, "comment": "no line"},  # no line
            {"file": "b.py", "line": 5, "comment": ""},  # empty comment
            {"file": "c.py", "line": 1, "comment": "valid"},  # valid
        ]
        await client.post_inline_comments("owner", "repo", 1, "abc123", comments)
        assert route.called
        body = route.calls[0].request.content
        assert b"c.py" in body
        assert b"a.py" not in body
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_caps_at_10_comments(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/pulls/1/reviews",
        ).respond(200, json={})

        comments = [
            {"file": f"f{i}.py", "line": i, "comment": f"issue {i}"}
            for i in range(1, 16)
        ]
        await client.post_inline_comments("owner", "repo", 1, "sha", comments)
        assert route.called
        import json

        body = json.loads(route.calls[0].request.content)
        assert len(body["comments"]) == 10
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_post_when_empty_commit_id(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/pulls/1/reviews",
        ).respond(200, json={})

        comments = [{"file": "a.py", "line": 1, "comment": "ok"}]
        await client.post_inline_comments("owner", "repo", 1, "", comments)
        assert not route.called
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_post_when_no_valid_comments(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/pulls/1/reviews",
        ).respond(200, json={})

        comments = [{"file": "", "line": None, "comment": ""}]
        await client.post_inline_comments("owner", "repo", 1, "sha", comments)
        assert not route.called
        await client.close()


# ── Post comment ─────────────────────────────────────────────────────


class TestPostComment:
    @respx.mock
    @pytest.mark.asyncio
    async def test_posts_comment(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/issues/1/comments",
        ).respond(200, json={})

        await client.post_comment("owner", "repo", 1, "Review body")
        assert route.called
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_body_skips_post(self):
        client = GitHubClient(token="test-token")
        route = respx.post(
            "https://api.github.com/repos/owner/repo/issues/1/comments",
        ).respond(200, json={})

        await client.post_comment("owner", "repo", 1, "   ")
        assert not route.called
        await client.close()


# ── Retry logic ──────────────────────────────────────────────────────


class TestRetry:
    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_on_500(self):
        client = GitHubClient(token="test-token")
        route = respx.get(
            "https://api.github.com/repos/owner/repo/pulls/1",
        )
        route.side_effect = [
            httpx.Response(500, json={"message": "Internal Server Error"}),
            httpx.Response(500, json={"message": "Internal Server Error"}),
            httpx.Response(
                200,
                json={
                    "title": "Test",
                    "body": "",
                    "html_url": "",
                    "draft": False,
                    "state": "open",
                    "labels": [],
                    "changed_files": 1,
                },
            ),
        ]
        result = await client.get_pr_details("owner", "repo", 1)
        assert result["title"] == "Test"
        assert route.call_count == 3
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        client = GitHubClient(token="test-token")
        respx.get(
            "https://api.github.com/repos/owner/repo/pulls/1",
        ).respond(500, json={"message": "Server Error"})

        with pytest.raises(GitHubError) as exc_info:
            await client.get_pr_details("owner", "repo", 1)
        assert exc_info.value.status == 500
        assert exc_info.value.reason == "server_error"
        await client.close()
