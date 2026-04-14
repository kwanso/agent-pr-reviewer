# Quick Reference Guide

Fast lookup for code structure, key files, and common operations.

---

## File Structure & Responsibilities

```
app/
├── __init__.py                    # Package init
├── server.py                      # FastAPI webhook server + health endpoint
├── config.py                      # Settings (Pydantic) + profile system
├── state.py                       # PRReviewState TypedDict
├── graph.py                       # LangGraph topology + routing functions
├── models.py                      # ReviewOutput, ReviewFinding, parsing
├── errors.py                      # Error classification system
│
├── nodes/                         # Graph nodes (async functions)
│   ├── fetch.py                   # Get PR details, guards, config
│   ├── analyze.py                 # Filter files, chunk, risk-score + RAG build
│   ├── review.py                  # LLM call with structured output
│   ├── validate.py                # False-positive filtering (2nd LLM pass)
│   └── publish.py                 # GitHub/Slack posting + degraded fallback
│
├── services/                      # External integrations
│   ├── github.py                  # GitHub API client
│   ├── slack.py                   # Slack API client
│   └── rag.py                     # FAISS + BM25 hybrid retrieval
│
└── utils/                         # Helpers
    ├── diff.py                    # File filtering, chunking, risk scoring
    ├── prompts.py                 # LLM prompt builders (Phase 0 + Phase 1)
    ├── metrics.py                 # Metrics/cost tracking
    └── tokens.py                  # Token estimation utilities

docs/
├── ARCHITECTURE_GUIDE.md          # This file (10,000+ words)
├── DEPLOYMENT.md                  # Production deployment guide
├── QUICK_REFERENCE.md             # (you are here)
└── README.md

tests/
├── test_models.py                 # ReviewOutput parsing, merging
├── test_diff.py                   # File filtering, chunking
├── test_graph.py                  # Routing functions
├── test_error_scenarios.py        # Error classification, edge cases
├── test_server.py                 # Webhook validation
└── ... [8 test files total, 154 tests]
```

---

## Key Execution Paths

### Path 1: Happy Path (PR → Review → Posted)

```
GitHub Webhook
    ↓
server.py:webhook() [validates payload]
    ↓
_run_review() [queues background task]
    ↓
LangGraph builds and compiles with checkpointer
    ↓
fetch_pr() → PR passed guards? [Yes]
    ↓
analyze_diff() → Create chunks, risk-score
    ↓
build_rag_index() → Semantic search index
    ↓
review_chunk() → LLM CALL #1 [Loop for each chunk]
    ↓
validate_findings() → LLM CALL #2 [Filter hallucinations]
    ↓
[More chunks?] → advance_chunk() [2s delay]
    ↓
[All chunks done] → merge_results() [Deduplicate]
    ↓
post_results() → GitHub comment + Slack
    ↓
Graph END [Success logged]
```

### Path 2: Guard Rejection (PR skipped early)

```
fetch_pr()
  ├─ Is draft? → skip
  ├─ Is closed? → skip
  ├─ Has "no-ai-review" label? → skip
  └─ >30 files? → skip
    ↓
route_after_fetch() → "skip"
    ↓
Graph END [Logged as skipped]
```

### Path 3: LLM Failure (Quota Exhausted)

```
review_chunk()
  ├─ LLM call fails
  ├─ classify_llm_error() → ErrorType.QUOTA_EXHAUSTED
  ├─ Retry logic (4 attempts with backoff)
  └─ All retries fail
    ↓
Return {"quota_exhausted": True}
    ↓
route_after_review() → "degraded"
    ↓
handle_degraded() → "Review failed; quota exhausted"
    ↓
Post fallback comment to GitHub + Slack
    ↓
Graph END [Error logged]
```

### Path 4: Webhook Retry (Idempotent Resume)

```
First delivery (delivery_id="abc-123"):
  fetch_pr() → checkpoint saved
  analyze_diff() → checkpoint saved
  review_chunk() [Chunk 0] → CRASH ✗

GitHub sees error → retries with same delivery_id

Second delivery (delivery_id="abc-123"):
  LangGraph loads checkpoint for thread_id="abc-123"
  Resume from analyze_diff() [already saved state]
  Skip fetch_pr() and analyze_diff() [idempotent!]
  review_chunk() [Chunk 0] → LLM succeeds
  Continue normally
    ↓
Graph END [Completed on retry]
```

---

## Common Code Patterns

### Reading from State

```python
# In any node function
async def my_node(state: PRReviewState) -> dict:
  owner = state["owner"]           # Required key
  chunk_idx = state.get("current_chunk_idx", 0)  # Optional, default 0
  reviews = state.get("chunk_reviews", [])       # Accumulator field

  # Return updates (merged back into state)
  return {
    "chunk_idx": chunk_idx + 1,
    "chunk_reviews": [new_review]  # Appended, not replaced!
  }
```

### Error Classification

```python
from app.errors import classify_llm_error, ErrorType

try:
  result = await llm.ainvoke(messages)
except Exception as exc:
  error = classify_llm_error(exc)

  if error.error_type == ErrorType.QUOTA_EXHAUSTED:
    return {"quota_exhausted": True}
  elif error.error_type == ErrorType.AUTH_FAILED:
    return {"early_exit": True, "error": error.message}
  elif error.retriable:
    await asyncio.sleep(error.retry_after_s)
    # retry...
  else:
    return {"error": error.message}
```

### RAG Retrieval

```python
from app.services import rag

# Building index
success = await rag.build_index(
  delivery_id="webhook-123",
  file_contents=[
    {"filename": "app/db.py", "content": "..."},
    {"filename": "app/models.py", "content": "..."}
  ]
)

# Retrieving context
docs = rag.retrieve(
  delivery_id="webhook-123",
  query="database connection handling",
  k=5
)
# docs = [Document(page_content="...", metadata={"source": "app/db.py"}), ...]

# Cleanup (called in publish.py)
rag.cleanup(delivery_id)
```

### Pydantic Model Validation

```python
from app.models import ReviewFinding, ReviewOutput

# Create finding (validators run automatically)
finding = ReviewFinding(
  issue="SQL injection risk",
  confidence=0.92,
  evidence="SELECT * FROM users WHERE id={user_id}"
)
# Validators:
# - High confidence (≥0.6) without evidence? → Penalized
# - Empty why_it_matters or suggested_fix? → Filled with defaults
# - file_path=None? → Converted to ""

# Parse from LLM response
review_dict = {
  "critical_issues": [{"issue": "...", "confidence": 0.95}],
  "reliability_issues": [],
  # ... other fields
}
review = ReviewOutput.model_validate(review_dict)

# Convert to markdown
markdown = review.to_markdown()
# Returns 9 sections with formatted issues
```

### Token Budget Tracking

```python
from app.utils.tokens import estimate_messages_tokens, is_budget_exhausted

# Estimate tokens for this chunk
messages = build_review_messages(...)
estimated = estimate_messages_tokens(messages)
# estimated ≈ 1200 input + 2000 output

# Check if budget exhausted
remaining = state.get("token_budget_max") - state.get("token_budget_used")
if is_budget_exhausted(
  state.get("token_budget_used"),
  state.get("token_budget_max"),
  threshold_pct=0.9  # Stop at 90%
):
  return {"token_budget_exhausted": True}

# Track usage
return {
  "token_budget_used": state.get("token_budget_used") + estimated
}
```

### Webhook Validation

```python
from app.server import GitHubWebhookPayload
from pydantic import ValidationError

try:
  payload_dict = await request.json()
  payload = GitHubWebhookPayload(**payload_dict)

  # Access validated fields
  owner = payload.repository.owner.login
  pr_number = payload.pull_request.number
  action = payload.action

except ValidationError as e:
  # Schema validation failed
  log.warning("webhook_validation_failed", errors=e.errors())
  raise HTTPException(status_code=400, detail="Invalid payload")
```

---

## Important Constants & Settings

### File Filtering

```python
# app/utils/diff.py

IGNORED_EXACT = {
  "package-lock.json", "yarn.lock", "pnpm-lock.yaml"
}

IGNORED_DIR_PREFIXES = (
  "node_modules/", "dist/", "build/", "__pycache__/", ".git/"
)

_CONFIG_FILE_RE = r"(\.env|tsconfig|webpack|docker|pyproject|requirements)"

_BINARY_EXTENSIONS = {
  ".png", ".jpg", ".gif", ".pdf", ".zip", ".so", ".exe", ...
}

_RISK_KEYWORDS_RE = r"(auth|token|secret|payment|migration|schema)"

_DEFAULT_RISK_PATHS = [
  "/auth/", "/payment/", "/db/", "migration", "schema", ...
]
```

### LLM Configuration

```python
# app/config.py

llm_flash_model = "gemini-2.5-flash"      # Model to use
llm_max_output_tokens = 8192              # Max response length
llm_temperature = 0.2                     # Deterministic (low randomness)
llm_timeout_s = 25                        # Timeout per request
llm_retry_count = 1                       # Max retries (handled separately)

# Token budgeting
token_budget_per_review = 100000          # Max tokens per PR
token_budget_threshold_pct = 0.9          # Stop at 90% usage
```

### Review Modes

```python
# Determined by risk_score in chunk_plan

LIGHT (risk_score ≤ 2):
  - Confidence threshold: ≥ 0.8 (higher bar)
  - Context: Brief
  - Coverage: Focus on critical issues only
  - Speed: Fast

DEEP (risk_score ≥ 3):
  - Confidence threshold: ≥ 0.6 (lower bar)
  - Context: Detailed
  - Coverage: All 9 dimensions
  - Speed: Slow
```

### Error Types

```python
# app/errors.py

ErrorType enum:
  QUOTA_EXHAUSTED      → Retriable (429, "quota")
  SERVICE_UNAVAILABLE  → Retriable (overloaded)
  TIMEOUT              → Retriable
  AUTH_FAILED          → Non-retriable (401, 403)
  INVALID_REQUEST      → Non-retriable (400)
  NOT_FOUND            → Non-retriable (404)
  SERVER_ERROR         → Retriable (5xx)
  UNKNOWN              → Non-retriable
```

---

## State Accumulation Rules

### Last-Write-Wins (Overwrites Previous Value)

```python
current_chunk_idx: int
error: str
final_summary: str
rag_index_built: bool
quota_exhausted: bool
token_budget_used: int
```

**Example:**
```python
# Initial
state = {"current_chunk_idx": 0, "error": ""}

# After node 1
state = {..., "current_chunk_idx": 1}  # Overwritten

# After node 2
state = {..., "current_chunk_idx": 1, "error": "timeout"}  # error added
```

### Accumulating (Appends to List)

```python
chunk_reviews: Annotated[list[dict], operator.add]
validated_findings: Annotated[list[dict], operator.add]
filtered_reviews: Annotated[list[dict], operator.add]
model_usage: Annotated[list[str], operator.add]
```

**Example:**
```python
# Initial
state = {"chunk_reviews": []}

# After node 1 (review_chunk for chunk 0)
state = {"chunk_reviews": [chunk_0_review]}

# After node 2 (advance_chunk + review_chunk for chunk 1)
state = {"chunk_reviews": [chunk_0_review, chunk_1_review]}  # APPENDED!

# After node 3 (validate_findings)
state = {"chunk_reviews": [...], "validated_findings": [finding_1, finding_2]}
```

---

## Testing Cheat Sheet

### Run all tests
```bash
.venv/bin/python -m pytest tests/ -v
```

### Run specific test file
```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

### Run specific test class
```bash
.venv/bin/python -m pytest tests/test_error_scenarios.py::TestErrorClassification -v
```

### Run specific test
```bash
.venv/bin/python -m pytest tests/test_error_scenarios.py::TestErrorClassification::test_classify_quota_error -v
```

### Run with coverage
```bash
.venv/bin/python -m pytest tests/ --cov=app --cov-report=html
```

### Run in mock mode (no LLM API calls)
```bash
LLM_MOCK_MODE=true .venv/bin/python -m pytest tests/
```

---

## Debugging Tips

### Enable detailed logging

```bash
export RUST_LOG=debug
export LANGSMITH_API_KEY=...  # Optional: LangSmith tracing
python -m app.server
```

### Check webhook delivery

```bash
# Find delivery_id from logs
grep "delivery_id=abc-123" app.log

# Trace all operations for that delivery
grep "delivery_id=abc-123" app.log | less
```

### Mock the LLM for testing

```bash
# All LLM calls return fake review (no API cost)
export LLM_MOCK_MODE=true
python -m app.server
```

### Check SQLite checkpoint

```bash
sqlite3 data/checkpoints.db

# List all threads (deliveries)
SELECT DISTINCT thread_id FROM checkpoints;

# View state for specific delivery
SELECT * FROM checkpoints WHERE thread_id='abc-123' ORDER BY step DESC LIMIT 1;
```

### Monitor token usage

```bash
# In logs, look for
grep "token_budget" app.log

# Example:
# reviewing_chunk token_budget_pct=45  ← 45% of budget used
```

### Check RAG memory

```python
from app.services import rag

# In Python REPL
print(f"Active indices: {len(rag._stores)}")
print(f"Oldest index age: {time.time() - min(rag._index_created_at.values())}")

# Manual cleanup if needed
rag.cleanup_expired_indices()
```

---

## Performance Tuning

### Reduce Cost (fewer LLM calls)

```bash
# Larger chunks = fewer calls
export CHUNK_SIZE=8000  # Default 5000

# Skip RAG (semantic search disabled)
export ENABLE_RAG=false

# Early exit after first clean chunk
export ENABLE_EARLY_EXIT=true

# Use cost_safe profile
export REVIEW_PROFILE=cost_safe
```

### Improve Quality (more thorough review)

```bash
# Smaller chunks = more detailed review
export CHUNK_SIZE=3000

# Use RAG for richer context
export ENABLE_RAG=true

# Never exit early
export ENABLE_EARLY_EXIT=false

# Use quality_heavy profile
export REVIEW_PROFILE=quality_heavy
```

### Optimize Speed (faster completion)

```bash
# Reduce inter-chunk delay
export CHUNK_DELAY_MS=1000  # Default 2000

# Use light mode for low-risk chunks
# (Automatic, based on risk_score)
```

---

## Common Issues & Solutions

| Issue | Symptom | Fix |
|-------|---------|-----|
| Quota exhausted | "429: Quota exceeded" in logs | Increase CHUNK_DELAY_MS, reduce chunk_size |
| Token budget hit | "token_budget_exhausted" logged | Increase TOKEN_BUDGET_PER_REVIEW |
| RAG memory growing | High memory usage, slow startup | Restart service (cleanup runs after 24h) |
| Validation pass failing | Findings getting dropped | Check ALLOWED_FILE_PATHS in chunk_plan |
| Webhook not received | No log entries | Check GitHub App webhook delivery |
| GitHub auth failing | "401: Invalid API key" | Verify GITHUB_TOKEN or App private key |
| Slack not posting | No Slack notification | Check SLACK_BOT_TOKEN, verify channel exists |
| Hallucinated findings | Wrong file paths in review | Evidence is now validated; confidence should be lower |

---

## Key Metrics to Monitor

```
Performance:
  - End-to-end latency (should be <2 min for typical PR)
  - Token usage per review (should be <50k for typical PR)
  - Chunk processing time (should be <10s per chunk)

Reliability:
  - Error rate (should be <1%)
  - Quota exhaustion rate (should be <5%)
  - LLM validation accuracy (% of findings that survive validation)

Cost:
  - Tokens per review
  - LLM API calls per review (2 per chunk: review + validate)
  - Total monthly cost
```

---

## Deployment Checklist

- [ ] Set all required env vars (GITHUB_TOKEN, LLM_API_KEY, WEBHOOK_SECRET)
- [ ] Create .pr-review.yml in your repo (optional)
- [ ] Run validation: `python scripts/validate_setup.py`
- [ ] Run tests: `pytest tests/ -q`
- [ ] Start server: `python -m app.server`
- [ ] Verify health: `curl http://localhost:3400/health`
- [ ] Create test PR to verify webhook works
- [ ] Check logs for errors: `grep ERROR app.log`
- [ ] Monitor memory usage (RAG indices)
- [ ] Set up monitoring for quota exhaustion

---

This guide should get you oriented quickly. For deep dives, see:
- `docs/ARCHITECTURE_GUIDE.md` — 10,000+ word deep dive
- `docs/DEPLOYMENT.md` — Production deployment
- Source code comments — Implementation details
