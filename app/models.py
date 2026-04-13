from __future__ import annotations

import json
import re
from typing import Any, Union

from pydantic import BaseModel, Field, field_validator, model_validator

_CLEAN_PHRASES = frozenset(
    {
        "no significant issues found",
        "no issues found",
        "no significant issues",
        "lgtm",
        "looks good",
        "none identified",
        "not applicable",
        "n/a",
    }
)

# All list buckets on ReviewOutput (order = markdown section order after context).
REVIEW_BUCKET_FIELDS: tuple[str, ...] = (
    "critical_issues",
    "reliability_issues",
    "database_issues",
    "resource_management",
    "code_quality",
    "input_validation_issues",
    "performance_scalability",
    "architecture_issues",
    "production_readiness",
)

_LEGACY_LIST_KEY_MAP: dict[str, str] = {
    "high_priority_improvements": "reliability_issues",
    "architecture_feedback": "architecture_issues",
    "production_considerations": "production_readiness",
}


class InlineComment(BaseModel):
    file: str
    line: int
    comment: str


_DEFAULT_WHY = "(Model omitted impact — infer from issue and evidence.)"
_DEFAULT_FIX = (
    "(Model omitted fix — address the issue with standard patterns for this codebase.)"
)


class ReviewFinding(BaseModel):
    """One production-grade finding: impact + fix, grounded in supplied code only."""

    issue: str = Field(
        ...,
        description="Precise issue; include path:line only if that path appears in the supplied diff",
    )
    why_it_matters: str = Field(
        default=_DEFAULT_WHY,
        description="Production impact: failure modes, security/reliability/data consequences",
    )
    suggested_fix: str = Field(
        default=_DEFAULT_FIX,
        description="Concrete remediation; optional short code in markdown fence",
    )
    evidence: str = Field(
        default="",
        description="Verbatim line/snippet or hunk reference from PROVIDED diff/RAG only",
    )
    file_path: str = Field(
        default="",
        description="Repo-relative path; must be in allowed file list or empty",
    )
    line: int | None = Field(
        default=None,
        description="Line in file if known from diff; null otherwise",
    )
    confidence: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="0.0–1.0; omit finding (or set <0.5) if speculative",
    )

    @field_validator("file_path", mode="before")
    @classmethod
    def _coerce_file_path(cls, v: Any) -> str:
        """Convert None/null file_path to empty string."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return ""
        return str(v).strip()

    @model_validator(mode="after")
    def _nonempty_detail_fields(self) -> ReviewFinding:
        updates: dict[str, str] = {}
        if not (self.why_it_matters or "").strip():
            updates["why_it_matters"] = _DEFAULT_WHY
        if not (self.suggested_fix or "").strip():
            updates["suggested_fix"] = _DEFAULT_FIX
        if updates:
            return self.model_copy(update=updates)
        return self


def _migrate_legacy_list_keys(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for old, new in _LEGACY_LIST_KEY_MAP.items():
        if old not in out:
            continue
        val = out.pop(old)
        if new in out and isinstance(out.get(new), list) and isinstance(val, list):
            out[new] = list(out[new]) + list(val)
        else:
            out[new] = val
    return out


class ReviewOutput(BaseModel):
    """Structured review: Phase 0 context + nine review dimensions."""

    context_summary: str = Field(
        default="",
        description=(
            "Phase 0 (mandatory): what the change does; external deps (DB, FS, env); "
            "key responsibilities; assumptions; explicitly list what is NOT visible in the diff."
        ),
    )
    critical_issues: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🔴 Security, data loss, system failure — must-fix",
    )
    reliability_issues: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🟠 Fault tolerance, errors, retries, crash-prone logic",
    )
    database_issues: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🟣 SQL safety, transactions, connection lifecycle",
    )
    resource_management: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🔵 Files, connections, cursors, memory under load",
    )
    code_quality: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🟡 Python idioms, typing, structure, duplication",
    )
    input_validation_issues: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🟤 Types, formats, ranges — integrity beyond injection",
    )
    performance_scalability: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="⚫ Hot paths, N+1, wasteful loops/IO",
    )
    architecture_issues: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description="🔷 Coupling, boundaries, testability, extensibility",
    )
    production_readiness: list[Union[ReviewFinding, str]] = Field(
        default_factory=list,
        description=(
            "⚪ Logging, observability, tests, config — never leave empty; "
            "if unknown say 'Not present in provided code' in the finding text."
        ),
    )
    inline_comments: list[InlineComment] = Field(default_factory=list)
    reasoning_trace: str = Field(
        default="", description="Optional internal notes; usually empty"
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_schema_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _migrate_legacy_list_keys(data)
        return data

    @field_validator(*REVIEW_BUCKET_FIELDS, mode="before")
    @classmethod
    def convert_strings_to_findings(cls, v: list) -> list:
        if not v:
            return v
        result = []
        for item in v:
            if isinstance(item, str):
                t = item.strip()
                result.append(
                    ReviewFinding(
                        issue=t,
                        why_it_matters="(Legacy one-line finding — expand in a full review.)",
                        suggested_fix="See issue description; add concrete remediation.",
                        confidence=0.75,
                    )
                )
            else:
                result.append(item)
        return result

    def _all_findings(self) -> list[ReviewFinding | str]:
        parts: list[ReviewFinding | str] = []
        for name in REVIEW_BUCKET_FIELDS:
            parts.extend(getattr(self, name))
        return parts

    def _item_text_lower(self, item: ReviewFinding | str) -> str:
        if isinstance(item, ReviewFinding):
            return item.issue.lower()
        return str(item).lower()

    def is_clean(self) -> bool:
        """True when no substantive findings in any bucket (context_summary ignored)."""
        items = self._all_findings()
        if not items:
            return True
        return all(
            any(p in self._item_text_lower(x) for p in _CLEAN_PHRASES) for x in items
        )

    def to_markdown(self) -> str:
        """Strict PR comment layout: context + nine dimensions."""

        def fmt_finding(i: int, f: ReviewFinding) -> list[str]:
            if f.confidence < 0.5:
                return []
            lines_: list[str] = [f"**{i}.** {f.issue}", ""]
            lines_.append(f"- **Impact:** {f.why_it_matters}")
            lines_.append(f"- **Fix:** {f.suggested_fix}")
            if f.evidence.strip():
                lines_.append(f"- **Evidence:** {f.evidence}")
            if f.file_path or f.line is not None:
                loc = f.file_path or "?"
                if f.line is not None:
                    loc = f"{loc}:{f.line}"
                lines_.append(f"- **Location:** `{loc}`")
            lines_.append(f"- *(confidence: {f.confidence:.0%})*")
            lines_.append("")
            return lines_

        def section(
            title: str, subtitle: str, findings: list[ReviewFinding | str]
        ) -> list[str]:
            out: list[str] = [f"## {title}", ""]
            if subtitle:
                out.append(f"*{subtitle}*")
                out.append("")
            n = 0
            for item in findings:
                if isinstance(item, str):
                    text = item.strip()
                    if not text:
                        continue
                    n += 1
                    out.append(f"**{n}.** {text}")
                    out.append("")
                    continue
                if item.confidence < 0.5:
                    continue
                n += 1
                out.extend(fmt_finding(n, item))
            if n == 0:
                out.append(
                    "*No issues identified in this category for the supplied change.*"
                )
                out.append("")
            return out

        lines: list[str] = ["## 🧠 Context Summary", ""]
        if (self.context_summary or "").strip():
            lines.append(self.context_summary.strip())
        else:
            lines.append(
                "*No context summary provided — scope and assumptions were not stated.* "
                "Verify against the diff only.",
            )
        lines.append("")

        lines.extend(
            section(
                "🔴 Critical Issues (Must Fix)",
                "Security breaches, data corruption/loss, production crashes.",
                self.critical_issues,
            )
        )
        lines.extend(
            section(
                "🟠 Reliability & Fault Tolerance Issues",
                "Runtime failures, unhandled errors, missing graceful degradation.",
                self.reliability_issues,
            )
        )
        lines.extend(
            section(
                "🟣 Database & State Management Issues",
                "Parameterized queries, transactions, rollback, connection lifecycle.",
                self.database_issues,
            )
        )
        lines.extend(
            section(
                "🔵 Resource Management Issues",
                "Leaks under failure or load: files, DB, cursors, memory.",
                self.resource_management,
            )
        )
        lines.extend(
            section(
                "🟡 Code Quality & Maintainability",
                "Pythonic style, typing, structure, duplication, readability.",
                self.code_quality,
            )
        )
        lines.extend(
            section(
                "🟤 Input Validation & Data Integrity",
                "Types, formats, constraints — not only SQL/injection.",
                self.input_validation_issues,
            )
        )
        lines.extend(
            section(
                "⚫ Performance & Scalability Concerns",
                "Growth and load: N+1, hot paths, wasteful work.",
                self.performance_scalability,
            )
        )
        lines.extend(
            section(
                "🔷 Architectural / Design Issues",
                "Separation of concerns, coupling, testability, extensibility.",
                self.architecture_issues,
            )
        )
        lines.extend(
            section(
                "⚪ Missing Production Considerations",
                "Logging, observability, testing, configuration — mandatory section.",
                self.production_readiness,
            )
        )

        if self.inline_comments:
            lines.append("## Inline review notes")
            lines.append("")
            for c in self.inline_comments:
                lines.append(f"- `{c.file}:{c.line}` — {c.comment}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


# ── Merge / parse helpers ────────────────────────────────────────────


def merge_reviews(reviews: list[ReviewOutput]) -> ReviewOutput:
    """Merge chunk reviews; dedupe by normalized issue text within each bucket."""

    merged = ReviewOutput()

    ctx_parts = [
        r.context_summary.strip() for r in reviews if r.context_summary.strip()
    ]
    merged.context_summary = "\n\n---\n\n".join(ctx_parts) if ctx_parts else ""

    for r in reviews:
        for name in REVIEW_BUCKET_FIELDS:
            getattr(merged, name).extend(getattr(r, name))
        merged.inline_comments.extend(r.inline_comments)

    def _norm(s: str) -> str:
        return " ".join(s.lower().split())[:500]

    def deduplicate(findings: list[ReviewFinding | str]) -> list[ReviewFinding]:
        text_map: dict[str, list[ReviewFinding]] = {}
        for f in findings:
            rf: ReviewFinding
            if isinstance(f, ReviewFinding):
                rf = f
            else:
                rf = ReviewFinding(
                    issue=str(f),
                    why_it_matters="(Merged legacy string finding.)",
                    suggested_fix="See issue text.",
                    confidence=0.7,
                )
            key = _norm(rf.issue)
            text_map.setdefault(key, []).append(rf)

        result: list[ReviewFinding] = []
        for group in text_map.values():
            avg_conf = sum(x.confidence for x in group) / len(group)
            ev = " | ".join(x.evidence for x in group if x.evidence)
            base = group[0]
            result.append(
                ReviewFinding(
                    issue=base.issue,
                    why_it_matters=base.why_it_matters,
                    suggested_fix=base.suggested_fix,
                    evidence=ev,
                    file_path=base.file_path,
                    line=base.line,
                    confidence=avg_conf,
                )
            )
        return result

    for name in REVIEW_BUCKET_FIELDS:
        setattr(merged, name, deduplicate(getattr(merged, name)))

    seen: set[tuple[str, int, str]] = set()
    unique: list[InlineComment] = []
    for c in merged.inline_comments:
        key = (c.file, c.line, c.comment)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    merged.inline_comments = unique
    return merged


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.IGNORECASE)
    return m.group(1).strip() if m else t


def _coerce_finding_dict(obj: Any) -> Any:
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    if "issue" not in out and isinstance(out.get("text"), str):
        out["issue"] = out.pop("text", "")
    for k in ("why_it_matters", "suggested_fix", "evidence", "file_path"):
        if out.get(k) is None:
            out[k] = ""
    return out


def _normalize_review_dict(d: dict[str, Any]) -> dict[str, Any]:
    fixed = _migrate_legacy_list_keys(dict(d))
    for key in REVIEW_BUCKET_FIELDS:
        items = fixed.get(key)
        if not isinstance(items, list):
            continue
        fixed[key] = [_coerce_finding_dict(x) for x in items]
    return fixed


def parse_review_json(text: str) -> ReviewOutput | None:
    """Recover ``ReviewOutput`` from raw LLM text when structured parsing fails."""
    t = _strip_json_fence(text)
    start = t.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(t[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ReviewOutput.model_validate(_normalize_review_dict(data))
    except Exception:
        return None


def parse_review_text(text: str) -> ReviewOutput:
    """Fallback markdown parse (nine dimensions + legacy three-bucket)."""

    def _extract_section(content: str, header_pattern: str) -> list[ReviewFinding]:
        rx = rf"##\s*{header_pattern}\s*([\s\S]*?)(?=\n##\s|\Z)"
        match = re.search(rx, content, re.IGNORECASE | re.MULTILINE)
        if not match:
            return []
        body = match.group(1).strip()
        # Strip italic subtitle lines under header
        body_lines = body.split("\n")
        while (
            body_lines
            and body_lines[0].strip().startswith("*")
            and body_lines[0].strip().endswith("*")
        ):
            body_lines = body_lines[1:]
        body = "\n".join(body_lines).strip()
        findings: list[ReviewFinding] = []
        for raw_line in body.split("\n"):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                findings.append(
                    ReviewFinding(
                        issue=line.lstrip("- ").strip(),
                        why_it_matters="(Recovered from fallback parse.)",
                        suggested_fix="Re-run with structured output for full detail.",
                        confidence=0.65,
                    )
                )
                continue
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if not para or para.startswith("#") or para.startswith("-"):
                continue
            if para.startswith("**"):
                lines = para.split("\n")
                first = lines[0]
                rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
                issue = re.sub(r"^\*\*\d+\.\*\*\s*", "", first)
                issue = issue.replace("**", "").strip()
                if not issue:
                    continue
                findings.append(
                    ReviewFinding(
                        issue=issue,
                        why_it_matters=rest.strip()[:2000] or "(See block.)",
                        suggested_fix="See parsed section body.",
                        confidence=0.65,
                    )
                )
        return findings

    def _ctx(content: str) -> str:
        rx = r"##\s*(?:🧠\s*)?Context Summary\s*([\s\S]*?)(?=\n##\s|\Z)"
        m = re.search(rx, content, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    out = ReviewOutput()
    ctx = _ctx(text)
    if ctx:
        out.context_summary = ctx

    # Nine-bucket headers (emoji optional)
    out.critical_issues.extend(
        _extract_section(text, r"(?:🔴\s*)?Critical Issues(?:\s*\(Must Fix\))?"),
    )
    out.reliability_issues.extend(
        _extract_section(
            text, r"(?:🟠\s*)?Reliability(?:\s*&\s*Fault Tolerance)?(?:\s*Issues)?"
        ),
    )
    out.database_issues.extend(
        _extract_section(
            text, r"(?:🟣\s*)?Database(?:\s*&\s*State Management)?(?:\s*Issues)?"
        ),
    )
    out.resource_management.extend(
        _extract_section(text, r"(?:🔵\s*)?Resource Management(?:\s*Issues)?"),
    )
    out.code_quality.extend(
        _extract_section(text, r"(?:🟡\s*)?Code Quality(?:\s*&\s*Maintainability)?"),
    )
    out.input_validation_issues.extend(
        _extract_section(
            text, r"(?:🟤\s*)?Input Validation(?:\s*&\s*Data Integrity)?(?:\s*Issues)?"
        ),
    )
    out.performance_scalability.extend(
        _extract_section(
            text, r"(?:⚫\s*)?Performance(?:\s*&\s*Scalability)?(?:\s*Concerns)?"
        ),
    )
    out.architecture_issues.extend(
        _extract_section(
            text, r"(?:🔷\s*)?Architectural(?:\s*/\s*Design)?(?:\s*Issues)?"
        ),
    )
    out.production_readiness.extend(
        _extract_section(text, r"(?:⚪\s*)?Missing Production Considerations?"),
    )

    # Legacy 5-bucket markdown (older agent)
    if not out.reliability_issues:
        out.reliability_issues.extend(
            _extract_section(text, r"(?:🟠\s*)?High Priority Improvements?")
        )
    if not out.architecture_issues:
        out.architecture_issues.extend(
            _extract_section(
                text, r"(?:🔵\s*)?Architectural(?:\s*/\s*Design)?\s*Feedback"
            ),
        )

    # Legacy three-bucket
    if not out.critical_issues:
        out.critical_issues.extend(_extract_section(text, r"Critical Issues"))
    if not out.reliability_issues:
        out.reliability_issues.extend(_extract_section(text, r"Improvements"))
    if not out.code_quality:
        out.code_quality.extend(_extract_section(text, r"Suggestions"))

    return out
