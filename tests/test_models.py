"""Tests for app.models — ReviewOutput, merge_reviews, parse_review_text."""

from __future__ import annotations

import json

import pytest

from app.models import (InlineComment, ReviewFinding, ReviewOutput,
                        merge_reviews, parse_review_json, parse_review_text)

# ── ReviewOutput.is_clean ────────────────────────────────────────────


class TestIsClean:
    def test_empty_lists_is_clean(self):
        r = ReviewOutput()
        assert r.is_clean() is True

    def test_only_clean_phrases_is_clean(self):
        r = ReviewOutput(
            critical_issues=["No significant issues found"],
            reliability_issues=["LGTM"],
            code_quality=["Looks good"],
        )
        assert r.is_clean() is True

    def test_real_issues_not_clean(self):
        r = ReviewOutput(
            critical_issues=["src/auth.py:12 - Missing token validation"],
        )
        assert r.is_clean() is False

    def test_mixed_clean_and_real_is_not_clean(self):
        r = ReviewOutput(
            critical_issues=["No significant issues found"],
            reliability_issues=["src/db.py:5 - Use parameterised queries"],
        )
        assert r.is_clean() is False


# ── ReviewOutput.to_markdown ─────────────────────────────────────────


class TestToMarkdown:
    def test_empty_review_markdown(self):
        r = ReviewOutput()
        md = r.to_markdown()
        assert "🧠 Context Summary" in md
        assert "🔴 Critical Issues" in md
        assert "🟠 Reliability" in md
        assert "🟣 Database" in md
        assert "🔵 Resource Management" in md
        assert "🟡 Code Quality" in md
        assert "🟤 Input Validation" in md
        assert "⚫ Performance" in md
        assert "🔷 Architectural" in md
        assert "⚪ Missing Production" in md

    def test_populated_review_markdown(self):
        r = ReviewOutput(
            context_summary="**Scope:** API routes changed.",
            critical_issues=[
                ReviewFinding(
                    issue="auth.py:10 - SQL injection risk",
                    why_it_matters="Attacker-controlled input reaches query string.",
                    suggested_fix="Use bound parameters.",
                    evidence='+ query = f"SELECT ... {x}"',
                    file_path="auth.py",
                    line=10,
                    confidence=0.9,
                ),
            ],
            reliability_issues=[
                ReviewFinding(
                    issue="db.py:22 - Connection not closed",
                    why_it_matters="Pool exhaustion under load.",
                    suggested_fix="Use context manager.",
                    evidence="",
                    confidence=0.85,
                ),
            ],
            code_quality=[
                ReviewFinding(
                    issue="utils.py:3 - Prefer pathlib",
                    why_it_matters="Cross-platform edge cases.",
                    suggested_fix="Replace os.path joins with Path.",
                    evidence="",
                    confidence=0.7,
                ),
            ],
        )
        md = r.to_markdown()
        assert "Scope:" in md
        assert "SQL injection" in md
        assert "**Impact:**" in md
        assert "**Fix:**" in md
        assert "Connection not closed" in md
        assert "pathlib" in md

    def test_markdown_sections_ordered(self):
        r = ReviewOutput(
            context_summary="ctx",
            critical_issues=[
                ReviewFinding(
                    issue="issue1",
                    why_it_matters="a",
                    suggested_fix="b",
                    confidence=0.9,
                )
            ],
            reliability_issues=[
                ReviewFinding(
                    issue="improve1",
                    why_it_matters="a",
                    suggested_fix="b",
                    confidence=0.9,
                )
            ],
            code_quality=[
                ReviewFinding(
                    issue="suggest1",
                    why_it_matters="a",
                    suggested_fix="b",
                    confidence=0.9,
                )
            ],
        )
        md = r.to_markdown()
        ctx = md.index("🧠 Context")
        ci = md.index("🔴 Critical")
        rel = md.index("🟠 Reliability")
        cq = md.index("🟡 Code Quality")
        assert ctx < ci < rel < cq


# ── merge_reviews ────────────────────────────────────────────────────


class TestMergeReviews:
    def test_merge_empty_list(self):
        merged = merge_reviews([])
        assert merged.critical_issues == []
        assert merged.reliability_issues == []
        assert merged.code_quality == []
        assert merged.inline_comments == []

    def test_merge_single_review(self):
        r = ReviewOutput(
            context_summary="A",
            critical_issues=[
                ReviewFinding(
                    issue="issue1",
                    why_it_matters="x",
                    suggested_fix="y",
                    confidence=0.9,
                )
            ],
            reliability_issues=[
                ReviewFinding(
                    issue="imp1",
                    why_it_matters="x",
                    suggested_fix="y",
                    confidence=0.9,
                )
            ],
            code_quality=[
                ReviewFinding(
                    issue="sug1",
                    why_it_matters="x",
                    suggested_fix="y",
                    confidence=0.9,
                )
            ],
            inline_comments=[InlineComment(file="a.py", line=1, comment="fix")],
        )
        merged = merge_reviews([r])
        assert merged.context_summary == "A"
        assert len(merged.critical_issues) == 1
        assert merged.critical_issues[0].issue == "issue1"
        assert len(merged.reliability_issues) == 1
        assert merged.reliability_issues[0].issue == "imp1"
        assert len(merged.code_quality) == 1
        assert merged.code_quality[0].issue == "sug1"
        assert len(merged.inline_comments) == 1

    def test_merge_deduplicates_items(self):
        r1 = ReviewOutput(critical_issues=["dup"], reliability_issues=["imp"])
        r2 = ReviewOutput(critical_issues=["dup"], reliability_issues=["imp2"])
        merged = merge_reviews([r1, r2])
        assert len(merged.critical_issues) == 1
        assert merged.critical_issues[0].issue == "dup"
        assert len(merged.reliability_issues) == 2
        assert sorted(f.issue for f in merged.reliability_issues) == ["imp", "imp2"]

    def test_merge_context_summaries(self):
        m = merge_reviews(
            [
                ReviewOutput(context_summary="Chunk A"),
                ReviewOutput(context_summary="Chunk B"),
            ]
        )
        assert "Chunk A" in m.context_summary
        assert "Chunk B" in m.context_summary
        assert "---" in m.context_summary

    def test_merge_deduplicates_inline_comments(self):
        ic = InlineComment(file="a.py", line=10, comment="same")
        r1 = ReviewOutput(inline_comments=[ic])
        r2 = ReviewOutput(inline_comments=[ic])
        merged = merge_reviews([r1, r2])
        assert len(merged.inline_comments) == 1

    def test_merge_keeps_unique_inline_comments(self):
        r1 = ReviewOutput(
            inline_comments=[InlineComment(file="a.py", line=1, comment="c1")],
        )
        r2 = ReviewOutput(
            inline_comments=[InlineComment(file="b.py", line=2, comment="c2")],
        )
        merged = merge_reviews([r1, r2])
        assert len(merged.inline_comments) == 2

    def test_merge_multiple_reviews(self):
        reviews = [
            ReviewOutput(
                critical_issues=[
                    ReviewFinding(
                        issue=f"crit{i}",
                        why_it_matters="a",
                        suggested_fix="b",
                        confidence=0.9,
                    )
                ],
                reliability_issues=[
                    ReviewFinding(
                        issue=f"imp{i}",
                        why_it_matters="a",
                        suggested_fix="b",
                        confidence=0.9,
                    )
                ],
            )
            for i in range(3)
        ]
        merged = merge_reviews(reviews)
        assert len(merged.critical_issues) == 3
        assert len(merged.reliability_issues) == 3


class TestLegacySchemaMigration:
    def test_high_priority_renames_to_reliability(self):
        r = ReviewOutput(high_priority_improvements=["legacy"])
        assert len(r.reliability_issues) == 1
        assert r.reliability_issues[0].issue == "legacy"


# ── parse_review_text ────────────────────────────────────────────────


class TestParseReviewText:
    def test_parse_well_formed_markdown(self):
        text = (
            "## Critical Issues\n"
            "- auth.py:10 - SQL injection\n"
            "- db.py:5 - Missing index\n"
            "\n"
            "## Improvements\n"
            "- utils.py:3 - Use pathlib\n"
            "\n"
            "## Suggestions\n"
            "- readme.md - Update docs\n"
        )
        r = parse_review_text(text)
        assert len(r.critical_issues) == 2
        assert len(r.reliability_issues) == 1
        assert len(r.code_quality) == 1

    def test_parse_empty_sections(self):
        text = (
            "## Critical Issues\n"
            "- No significant issues found\n"
            "\n"
            "## Improvements\n"
            "- No significant issues found\n"
            "\n"
            "## Suggestions\n"
            "- No significant issues found\n"
        )
        r = parse_review_text(text)
        assert len(r.critical_issues) == 1
        assert r.is_clean() is True

    def test_parse_no_matching_sections(self):
        r = parse_review_text("Just some random text without sections")
        assert r.critical_issues == []
        assert r.reliability_issues == []
        assert r.code_quality == []

    def test_parse_empty_string(self):
        r = parse_review_text("")
        assert r.critical_issues == []
        assert r.reliability_issues == []
        assert r.code_quality == []

    def test_parse_partial_sections(self):
        text = "## Critical Issues\n- bug in auth\n"
        r = parse_review_text(text)
        assert len(r.critical_issues) == 1
        assert r.reliability_issues == []
        assert r.code_quality == []

    def test_parse_context_summary(self):
        text = (
            "## 🧠 Context Summary\n"
            "The diff adds a health check.\n"
            "\n"
            "## 🔴 Critical Issues (Must Fix)\n"
            "- None\n"
        )
        r = parse_review_text(text)
        assert "health check" in r.context_summary


# ── parse_review_json ────────────────────────────────────────────────


class TestParseReviewJson:
    def test_partial_finding_gets_defaults(self):
        raw = json.dumps(
            {
                "critical_issues": [
                    {"issue": "SQLi", "evidence": '+ query = f"SELECT {x}"'},
                ],
                "high_priority_improvements": [
                    {"issue": "Leak", "why_it_matters": "fd leak"},
                ],
            }
        )
        r = parse_review_json(raw)
        assert r is not None
        assert len(r.critical_issues) == 1
        assert r.critical_issues[0].issue == "SQLi"
        assert "Model omitted" in r.critical_issues[0].why_it_matters
        assert "Model omitted" in r.critical_issues[0].suggested_fix
        assert len(r.reliability_issues) == 1
        assert "Model omitted" in r.reliability_issues[0].suggested_fix

    def test_raw_decode_ignores_trailing_text(self):
        raw = '{"critical_issues": []} trailing prose'
        r = parse_review_json(raw)
        assert r is not None
        assert r.critical_issues == []

    def test_fenced_json(self):
        raw = '```json\n{"critical_issues": [{"issue": "x", "confidence": 0.9}]}\n```'
        r = parse_review_json(raw)
        assert r is not None
        assert r.critical_issues[0].issue == "x"

    def test_invalid_returns_none(self):
        assert parse_review_json("not json") is None
        assert parse_review_json("{broken") is None
