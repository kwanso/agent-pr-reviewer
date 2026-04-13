"""Tests for app.utils.prompts — prompt building and message structure."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.utils.prompts import (
    build_review_messages,
    build_system_prompt,
    build_user_prompt,
)

# ── build_system_prompt ──────────────────────────────────────────────


class TestBuildSystemPrompt:
    def test_contains_staff_engineer_persona(self):
        prompt = build_system_prompt()
        assert "staff-level engineer" in prompt.lower()

    def test_deep_mode_instruction(self):
        prompt = build_system_prompt(review_mode="deep")
        assert "DEEP" in prompt

    def test_light_mode_instruction(self):
        prompt = build_system_prompt(review_mode="light")
        assert "LIGHT" in prompt

    def test_includes_repo_context(self):
        prompt = build_system_prompt(
            repo_context="Language: Python\nFramework: FastAPI"
        )
        assert "Python" in prompt
        assert "FastAPI" in prompt

    def test_no_repo_context(self):
        prompt = build_system_prompt(repo_context="")
        assert "Repository context" not in prompt

    def test_unknown_mode_defaults_to_deep(self):
        prompt = build_system_prompt(review_mode="unknown_mode")
        assert "DEEP" in prompt


# ── build_user_prompt ────────────────────────────────────────────────


class TestBuildUserPrompt:
    def test_includes_title_and_description(self):
        prompt = build_user_prompt("My PR", "This fixes bug #123", "diff text")
        assert "My PR" in prompt
        assert "This fixes bug #123" in prompt
        assert "diff text" in prompt

    def test_empty_title_shows_no_title(self):
        prompt = build_user_prompt("", "", "diff")
        assert "(no title)" in prompt

    def test_empty_description_shows_no_description(self):
        prompt = build_user_prompt("Title", "", "diff")
        assert "(no description)" in prompt

    def test_includes_rag_context(self):
        prompt = build_user_prompt(
            "Title", "Desc", "diff", rag_context="def helper(): ..."
        )
        assert "def helper(): ..." in prompt
        assert "Relevant source context" in prompt

    def test_no_rag_context(self):
        prompt = build_user_prompt("Title", "Desc", "diff", rag_context="")
        assert "Relevant source context" not in prompt

    def test_empty_diff_shows_placeholder(self):
        prompt = build_user_prompt("T", "D", "")
        assert "(empty)" in prompt

    def test_allowed_file_paths_in_user_prompt(self):
        prompt = build_user_prompt(
            "T", "D", "diff", allowed_file_paths=["a.py", "b.py"]
        )
        assert "ALLOWED_FILE_PATHS" in prompt
        assert "a.py" in prompt
        assert "b.py" in prompt


# ── build_review_messages ────────────────────────────────────────────


class TestBuildReviewMessages:
    def test_returns_system_and_human_messages(self):
        messages = build_review_messages(
            title="PR",
            description="desc",
            diff="diff",
        )
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)

    def test_system_message_uses_review_mode(self):
        messages = build_review_messages(
            title="PR",
            description="desc",
            diff="diff",
            review_mode="light",
        )
        assert "LIGHT" in messages[0].content

    def test_human_message_contains_diff(self):
        messages = build_review_messages(
            title="PR",
            description="desc",
            diff="+new line",
        )
        assert "+new line" in messages[1].content

    def test_repo_context_in_system_message(self):
        messages = build_review_messages(
            title="PR",
            description="desc",
            diff="diff",
            repo_context="Language: Rust",
        )
        assert "Rust" in messages[0].content

    def test_rag_context_in_human_message(self):
        messages = build_review_messages(
            title="PR",
            description="desc",
            diff="diff",
            rag_context="def existing_func(): pass",
        )
        assert "existing_func" in messages[1].content
