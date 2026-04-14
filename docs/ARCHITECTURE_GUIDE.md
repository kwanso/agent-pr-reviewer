# Architecture & Code Execution Guide

Complete walkthrough of how the PR Review Agent works, from webhook to final review posting.

---

## 1. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         GitHub Organization                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ PR opened/updated
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    GitHub Webhook Event (JSON)                     │
│  {owner, repo, pr_number, head_sha, action, installation_id}       │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ HTTP POST
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│              FastAPI Webhook Server (app/server.py)                │
│  • Verify HMAC-SHA256 signature                                     │
│  • Validate payload schema (Pydantic)                               │
│  • Schedule background task                                         │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Queue task
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│        LangGraph State Machine (app/graph.py) - Background          │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  1. fetch_pr       → Get PR details, repo config            │  │
│  │  2. analyze_diff   → Filter files, chunk, score risk        │  │
│  │  3. build_rag_index → Index repo files (FAISS + BM25)       │  │
│  │  4. review_chunk   → LLM call on each chunk (LOOP)          │  │
│  │  5. validate_findings → Filter false positives              │  │
│  │  6. advance_chunk  → Rate limit delay                       │  │
│  │  7. merge_results  → Combine all chunk reviews              │  │
│  │  8. post_results   → GitHub comment + Slack                 │  │
│  │  9. handle_degraded → Fallback on errors                    │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  State stored in: SQLite checkpoint (data/checkpoints.db)          │
│  Idempotent via: delivery_id as thread_id                          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Posting results
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Output (Notifications)                          │
│  • GitHub PR comment (markdown review)                              │
│  • Slack message (summary with counts)                              │
│  • Per-line inline comments (optional)                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Breakdown

### 2.1 Request Entry Point: `app/server.py`

**What it does:** HTTP server that receives GitHub webhooks.

**Key functions:**
```python
_verify_signature()        # HMAC-SHA256 verification
webhook()                  # Main POST /webhook endpoint
_run_review()              # Background task runner
health()                   # GET /health for liveness checks
```

**Flow:**
1. Receives GitHub webhook POST
2. Verifies HMAC signature using `WEBHOOK_SECRET`
3. Parses JSON payload using Pydantic models
4. Validates schema (owner, repo, PR number, etc.)
5. Filters for actions: `opened`, `synchronize`, `reopened`
6. Queues `_run_review()` as background task
7. Returns 202 Accepted immediately (async)

**Key classes (Pydantic schemas):**
```
GitHubUser
  └─ login: str

GitHubRepository
  ├─ name: str
  └─ owner: GitHubUser

GitHubCommit
  └─ sha: str

GitHubPullRequest
  ├─ number: int
  ├─ title: str
  ├─ body: str
  ├─ draft: bool
  ├─ state: str
  ├─ head: GitHubCommit
  ├─ labels: list[dict]
  └─ changed_files: int

GitHubInstallation
  └─ id: int

GitHubWebhookPayload
  ├─ action: str
  ├─ pull_request: GitHubPullRequest
  ├─ repository: GitHubRepository
  └─ installation: GitHubInstallation | None
```

---

### 2.2 Graph Orchestration: `app/graph.py`

**What it does:** Defines the LangGraph state machine that orchestrates all review steps.

**Core concept:** State flows through nodes, with conditional routing between them.

```
State = PRReviewState (TypedDict)
  └─ Immutable between nodes
  └─ Accumulator fields (operator.add) append results instead of overwriting
  └─ Other fields use last-write-wins semantics

Graph topology:
  START
    ↓
  fetch_pr [guards: draft, closed, max_files, no-ai-review label]
    ├─ skip? → END
    └─ continue ↓
  analyze_diff [filter, chunk, score risk]
    ├─ skip? → END
    └─ continue ↓
  build_rag_index [semantic search context]
    ↓
  review_chunk [LLM call with structured output]
    ├─ quota_exhausted? → degraded
    ├─ token_budget_exhausted? → degraded
    └─ success ↓
  validate_findings [false-positive filtering]
    ├─ is_last_chunk? → merge
    └─ more_chunks? → advance ↓
  advance_chunk [rate limit delay]
    ↓
  review_chunk [loop back for next chunk]

  merge_results [deduplicate findings across chunks]
    ├─ has_findings? → post_results → END
    └─ no_findings? → degraded ↓
  handle_degraded [fallback message]
    ↓
  END
```

**Routing functions:**
```python
route_after_fetch(state)      # skip if draft/closed/no-review-label
route_after_analysis(state)   # skip if diff too large
route_after_review(state)     # validate or degrade or merge
route_after_validate(state)   # advance or merge
route_after_merge(state)      # publish or degrade
```

**Key insight:** Graph resumes from checkpoint if webhook retried with same delivery_id.

---

### 2.3 State Management: `app/state.py`

**What it does:** Defines the immutable state dictionary passed between nodes.

```python
PRReviewState (TypedDict):

# Input (from webhook)
  owner, repo, pr_number, head_sha, action, delivery_id, installation_id

# Fetched data
  pr_title, pr_body, pr_url, pr_draft, pr_state, pr_labels, pr_files
  repo_config, repo_context_block

# Diff analysis
  diff_skip, diff_skip_reason, chunk_plans

# RAG
  rag_index_built

# Review loop
  current_chunk_idx
  chunk_reviews[]          # Accumulates (operator.add)
  model_usage[]            # Accumulates (operator.add)

# Validation
  pending_findings, validated_findings[], filtered_reviews[]

# Control flow
  quota_exhausted, early_exit, error
  token_budget_max, token_budget_used, token_budget_exhausted

# Output
  final_summary, inline_comments[], slack_message

# Skip flags
  skipped, skip_reason
```

**Important:** Fields with `Annotated[list[dict], operator.add]` **accumulate** instead of replace. Each node appends to them.

---

### 2.4 Nodes: How Data Gets Processed

Each node is an async function that:
1. Reads from state
2. Performs work (API calls, LLM, computation)
3. Returns dict of updates (merged back into state)

#### **Node 1: `fetch_pr()` - Get PR metadata**

**File:** `app/nodes/fetch.py`

**Inputs from state:**
```
owner, repo, pr_number, installation_id
```

**What it does:**
1. Calls GitHub API to get PR details
2. Checks guards:
   - Is PR draft? → Skip
   - Is PR closed? → Skip
   - Has "no-ai-review" label? → Skip
   - Changed files > `max_files`? → Skip
3. Loads `.pr-review.yml` from repo (if exists)
4. Sanitizes repo config (prevents prompt injection)
5. Fetches all PR file metadata

**Outputs to state:**
```python
{
  "pr_title": "Fix database connection leak",
  "pr_body": "...",
  "pr_url": "https://github.com/...",
  "pr_draft": False,
  "pr_state": "open",
  "pr_labels": ["bug", "database"],
  "pr_files": [
    {"filename": "app/db.py", "patch": "...", "status": "modified", ...}
  ],
  "repo_config": {"language": "python", "review_depth": "deep"},
  "repo_context_block": "Language: Python\nReview focus: performance"
}
```

**Guard logic (returns skip if true):**
```python
pr["draft"] or pr["state"] == "closed"
  or any("no-ai-review" in lbl.lower() for lbl in pr["labels"])
  or pr["changed_files"] > settings.max_files
```

---

#### **Node 2: `analyze_diff()` - Filter files, create chunks, score risk**

**File:** `app/nodes/analyze.py`

**Inputs from state:**
```
pr_files, repo_config
```

**What it does:**
1. Calls `analyze_diff()` from `app/utils/diff.py`
2. Filters files:
   - Ignores: `node_modules/`, `dist/`, `__pycache__/`, `.git/`
   - Ignores: `*.min.js`, `*.min.css`, generated files
   - Ignores: config files (`pyproject.toml`, `.env`, `Dockerfile`, etc.)
   - Ignores: lock files (`package-lock.json`, `yarn.lock`, etc.)
   - Ignores: binary files (images, PDFs, etc.)
   - Applies repo-specific ignore patterns from `.pr-review.yml`

3. Creates chunks:
   - Groups related files together
   - Splits by size (`chunk_size` = 5000 chars default)
   - Each chunk is independent review

4. Scores risk (6 factors):
   - **Path risk:** `/auth/`, `/payment/`, `/db/`, etc. → +1 each
   - **Keywords:** `auth`, `token`, `secret`, `payment`, `migration`, `schema` → +1 each
   - **File count:** per-chunk → +1 if >1 file
   - **Complexity:** AST analysis → +1 if complex
   - **Size:** per-chunk → +1 if >100 lines
   - **User preference:** from `.pr-review.yml` → +0–2

5. Determines review mode:
   - **LIGHT:** Low risk (score ≤2) → Fast, high confidence threshold (0.8)
   - **DEEP:** High risk (score ≥3) → Thorough, lower threshold (0.6)

**Output to state:**
```python
{
  "chunk_plans": [
    {
      "index": 0,
      "chunk_text": "--- app/db.py\n@@ -22,5 +22,7 @@\n...",
      "files": ["app/db.py"],
      "risk_score": 4,
      "review_mode": "deep"
    },
    {
      "index": 1,
      "chunk_text": "--- app/utils.py\n...",
      "files": ["app/utils.py"],
      "risk_score": 0,
      "review_mode": "light"
    }
  ],
  "current_chunk_idx": 0
}
```

**Skip logic:**
- PR touches >30 files? → Skip
- PR diff >30KB? → Skip
- All files are config-only? → Skip (if `skip_config_only=true`)

---

#### **Node 3: `build_rag_index()` - Create searchable repo context**

**File:** `app/nodes/analyze.py`

**Inputs from state:**
```
chunk_plans, delivery_id, installation_id, owner, repo
```

**What it does:**
1. Extracts all filenames from chunk plans
2. Fetches full file contents from GitHub (async, parallel)
3. Creates hybrid search index:

   **FAISS (semantic search):**
   - Uses Google Gemini embeddings
   - Chunks files with recursive splitter
   - Stores vector embeddings in memory
   - Expensive (API calls) but powerful

   **BM25 (keyword search):**
   - Uses TF-IDF scoring
   - Free (no API cost)
   - Fallback if FAISS quota exhausted

4. Stores in module-level dicts (keyed by `delivery_id`)
5. Auto-cleanup after 24 hours

**Why RAG?** When reviewing a chunk, the LLM needs context about the broader codebase. RAG retrieves relevant code snippets.

**Output to state:**
```python
{
  "rag_index_built": True  # or False if failed
}
```

**Internal storage (not in state):**
```python
_stores[delivery_id] = faiss_index          # Vector store
_bm25_stores[delivery_id] = bm25_retriever  # Keyword index
_metadata_stores[delivery_id] = {           # File metadata
  "app/db.py": {
    "size": 12400,
    "lines": 340,
    "is_test": False,
    "language": "python"
  }
}
_index_created_at[delivery_id] = time.time()
```

---

#### **Node 4: `review_chunk()` - LLM call with structured output**

**File:** `app/nodes/review.py`

**Inputs from state:**
```
chunk_plans, current_chunk_idx, rag_index_built, delivery_id,
pr_title, pr_body, repo_context_block
```

**What it does:**
1. **Initialization (first chunk only):**
   - Set token budget limits
   - Initialize token usage counter

2. **Token budget check:**
   - Estimate tokens needed for this chunk
   - Check if remaining budget > needed tokens
   - If budget exhausted (≥90% used) → early exit

3. **Retrieve RAG context:**
   - Get current chunk text
   - Query hybrid index for relevant code
   - Format as "Evidence from repo:" block

4. **Build LLM prompt (3 parts):**

   **System prompt:**
   ```
   You are a Staff-level engineer doing production code review.
   Think like the on-call person responsible for this system.
   ```

   **Context (Phase 0):**
   ```
   ## Phase 0 — Context Understanding (MANDATORY)
   1. What changed? (scope)
   2. External dependencies? (DB, HTTP, filesystem, etc.)
   3. Key responsibilities (each changed function)
   4. Assumptions
   5. What's NOT visible?
   ```

   **Review instructions (Phase 1):**
   ```
   ## Phase 1 — Multi-Dimensional Review
   Review across 9 dimensions:
   - Critical issues (security, data loss, crashes)
   - Reliability (error handling, retries)
   - Database (SQL safety, transactions)
   - Resources (leaks, cleanup)
   - Code quality (pythonic, typing)
   - Input validation
   - Performance (N+1, hot paths)
   - Architecture (coupling, testability)
   - Production readiness (logging, testing)
   ```

   **Evidence context:**
   ```
   DIFF (the chunk being reviewed):
   <chunk_text>

   RAG RETRIEVAL (related code from repo):
   <rag_context>

   ALLOWED_FILE_PATHS (only files this PR touches):
   <chunk.files>
   ```

5. **LLM call with retry logic:**
   ```python
   for attempt in range(4):  # Max 4 retries
     try:
       response = llm.with_structured_output(ReviewOutput).ainvoke(messages)
       if response.get("parsed"):
         review = response["parsed"]  # Structured JSON
       else:
         raw_text = response["raw"]
         review = parse_review_json(raw_text) or parse_review_text(raw_text)
       return _success(review, ...)
     except Exception as exc:
       error = classify_llm_error(exc)
       if error.retriable and attempt < 3:
         await asyncio.sleep(error.retry_after_s)
         continue
       else:
         break  # Non-retriable or out of retries
   ```

6. **Track token usage:**
   - Accumulate tokens per chunk
   - Update state: `token_budget_used += tokens_consumed`

**Output to state:**
```python
{
  "chunk_reviews": [
    {
      "chunk_index": 0,
      "review_mode": "deep",
      "risk_score": 4,

      # Findings by dimension:
      "context_summary": "What changed: DB connection management...",
      "critical_issues": [
        {
          "issue": "Database connection not closed in error path",
          "why_it_matters": "Pool exhaustion under load → service outage",
          "suggested_fix": "Use try/finally to ensure close()",
          "evidence": "Line 42: except Exception: return",
          "file_path": "app/db.py",
          "line": 42,
          "confidence": 0.92
        }
      ],
      "reliability_issues": [...],
      "database_issues": [...],
      "resource_management": [...],
      "code_quality": [...],
      "input_validation_issues": [...],
      "performance_scalability": [...],
      "architecture_issues": [...],
      "production_readiness": [...],
      "inline_comments": [
        {"file": "app/db.py", "line": 42, "comment": "Add finally block"}
      ]
    }
  ],
  "model_usage": ["gemini-2.5-flash"],
  "token_budget_used": 12400,
  "early_exit": False  # True if first chunk clean & LIGHT mode
}
```

**Error handling:**
- **Quota exhausted:** Return `{"quota_exhausted": True}`
- **Service unavailable:** Return `{"early_exit": True}` (stop processing)
- **Auth failed:** Return `{"early_exit": True}` (can't recover)
- **Timeout/unknown:** Return `{"error": "..."}`

---

#### **Node 5: `validate_findings()` - Filter false positives**

**File:** `app/nodes/validate.py`

**Inputs from state:**
```
chunk_reviews (all reviews so far)
filtered_reviews (already validated)
chunk_plans
```

**What it does:**
1. **Identify unvalidated chunks:**
   ```python
   unvalidated = chunk_reviews[len(filtered_reviews):]
   # If filtered_reviews = [chunk0], validate chunk1 onwards
   ```

2. **For each unvalidated chunk:**
   - Get corresponding plan (chunk_index → chunk_plans[index])
   - Extract chunk diff and allowed files
   - Flatten findings into list for validation

3. **Validation LLM pass (second LLM call):**
   - System: "You are a strict auditor. Reject generic, unsupported, or hallucinated findings."
   - User: Present each finding with index, ask keep/reject, confidence adjustment

   **Example validation prompt:**
   ```
   ALLOWED_FILE_PATHS:
   - app/db.py
   - app/utils.py

   DIFF (excerpt):
   @@ -42,5 +42,7 @@
    try:
      conn.query()
   +except Exception:
   +  return  # ERROR: no close!

   Findings to validate:
   [0] (database_issues) "Connection not closed in error path"
       evidence: "except Exception: return"
       file_path="app/db.py" line=44
       confidence=0.92

   For each finding return:
   {"index": 0, "keep": true/false, "confidence": 0.0-1.0, "reason": "..."}
   ```

4. **Rebuild review with adjusted confidences:**
   - Drop findings with confidence < 0.5
   - Keep only findings with valid file_path
   - Merge results

**Output to state:**
```python
{
  "filtered_reviews": [  # Accumulates
    {
      ...same structure as chunk_reviews item...
      # But with:
      # - Findings with confidence < 0.5 removed
      # - Invalid file_path removed
      # - Confidences possibly adjusted down by validator
    }
  ]
}
```

**Why two LLM passes?**
- First pass (review) finds issues
- Second pass (validate) questions findings, removes hallucinations
- Cost: 2x LLM calls, but higher precision

---

#### **Node 6: `advance_chunk()` - Rate limiting**

**File:** `app/nodes/review.py`

**Inputs from state:**
```
current_chunk_idx, chunk_delay_ms (setting)
```

**What it does:**
1. Sleep for `chunk_delay_ms` milliseconds (default 2000ms = 2 seconds)
2. Increment chunk index

**Output to state:**
```python
{
  "current_chunk_idx": current_chunk_idx + 1
}
```

**Why?** Google Gemini API has rate limits. Delaying between chunks prevents quota exhaustion.

---

#### **Node 7: `merge_results()` - Combine all chunks**

**File:** `app/nodes/publish.py`

**Inputs from state:**
```
chunk_reviews, filtered_reviews, current_chunk_idx
```

**What it does:**
1. **Choose which reviews to merge:**
   ```python
   if filtered_reviews and len(filtered_reviews) == len(chunk_reviews):
     use = filtered_reviews  # Use validated (filtered) reviews
   else:
     use = chunk_reviews  # Fallback to unfiltered
   ```

2. **Deduplicate findings:**
   - For each dimension (critical_issues, reliability_issues, etc.):
   - Normalize issue text (lowercase, trim whitespace)
   - Group by normalized text
   - Keep first, merge evidence, average confidence

3. **Merge context summaries:**
   ```
   Part 1: <context from chunk 0>
   ---
   Part 2: <context from chunk 1>
   ```

4. **Deduplicate inline comments:**
   - Use (file, line, comment) as unique key

**Output to state:**
```python
{
  "final_summary": """
## 🧠 Context Summary
Scope: Database connection management in order service...

## 🔴 Critical Issues
**1.** Connection not closed in error path
- **Impact:** Pool exhaustion → service outage
- ...

## 🟠 Reliability Issues
...
[9 sections total]
""",
  "inline_comments": [
    {"file": "app/db.py", "line": 42, "comment": "Add finally block"}
  ]
}
```

---

#### **Node 8: `post_results()` - GitHub + Slack**

**File:** `app/nodes/publish.py`

**Inputs from state:**
```
final_summary, inline_comments, owner, repo, pr_number,
head_sha, pr_title, pr_url, delivery_id
```

**What it does:**
1. **Post to GitHub:**
   - Try inline comments first (if head_sha available):
     ```python
     await github.post_inline_comments(
       owner, repo, pr_number, head_sha,
       [{"file": "app/db.py", "line": 42, "comment": "..."}]
     )
     ```
   - Fallback to PR comment:
     ```python
     await github.post_comment(owner, repo, pr_number, final_summary)
     ```

2. **Post to Slack (if configured):**
   - Convert markdown to Slack format
   - Count issues per dimension
   - Build summary message:
     ```
     🔴 Critical — 1 issue
     🟠 Reliability — 2 issues
     ...
     ```
   - Send to channel from repo config

3. **Cleanup:**
   ```python
   rag.cleanup(delivery_id)  # Free in-memory indices
   ```

**Output to state:**
```python
{
  "slack_message": "..."
}
```

---

#### **Node 9: `handle_degraded()` - Fallback on error**

**File:** `app/nodes/publish.py`

**Inputs from state:**
```
error, owner, repo, pr_number
```

**What it does:**
1. Build fallback message:
   ```
   ## Automated Review Status

   The review pipeline ran, but could not generate a complete code review.
   Reason: <error message>

   Please retry after resolving the issue.
   ```

2. Post to GitHub and Slack
3. Cleanup RAG indices

**Output to state:**
```python
{
  "final_summary": "<fallback message>"
}
```

---

## 3. Key Models & Data Structures

### ReviewOutput (Main LLM output)

```python
class ReviewOutput(BaseModel):
  context_summary: str
  critical_issues: list[ReviewFinding | str]
  reliability_issues: list[ReviewFinding | str]
  database_issues: list[ReviewFinding | str]
  resource_management: list[ReviewFinding | str]
  code_quality: list[ReviewFinding | str]
  input_validation_issues: list[ReviewFinding | str]
  performance_scalability: list[ReviewFinding | str]
  architecture_issues: list[ReviewFinding | str]
  production_readiness: list[ReviewFinding | str]
  inline_comments: list[InlineComment]
  reasoning_trace: str  # Optional internal notes
```

### ReviewFinding (Individual issue)

```python
class ReviewFinding(BaseModel):
  issue: str                    # The problem
  why_it_matters: str          # Production impact
  suggested_fix: str           # Concrete remediation
  evidence: str                # Verbatim from diff
  file_path: str               # Repo-relative path
  line: int | None             # Line number
  confidence: float            # 0.0–1.0, default 0.60

  # Validators:
  # - High confidence (≥0.6) without evidence → penalized
  # - Empty fields filled with defaults
```

### ChunkPlan (What to review)

```python
{
  "index": 0,
  "chunk_text": "--- app/db.py\n@@ ...",
  "files": ["app/db.py"],
  "risk_score": 4,      # 0–10
  "review_mode": "deep" # or "light"
}
```

---

## 4. External Integrations

### GitHub API (via `app/services/github.py`)

```python
class GitHubClient:
  get_pr_details(owner, repo, pr_number)      # PR metadata
  get_pr_files(owner, repo, pr_number)        # Changed files + diffs
  get_file_content(owner, repo, path, ref)    # Full file contents
  get_repo_config(owner, repo, ref)           # .pr-review.yml
  post_comment(owner, repo, pr_number, text)  # Comment on PR
  post_inline_comments(...)                   # Review on specific lines
```

**Auth:** GitHub App + installation token (or PAT fallback)

### Google Gemini LLM (via `app/nodes/review.py`)

```python
llm = ChatGoogleGenerativeAI(
  model="gemini-2.5-flash",
  max_output_tokens=8192,
  temperature=0.2
)

# Structured output (Pydantic validation)
structured = llm.with_structured_output(ReviewOutput, include_raw=True)
result = await structured.ainvoke(messages)  # {"parsed": ..., "raw": ...}
```

**Retries:** Up to 4 attempts with exponential backoff for rate limits

### Slack API (via `app/services/slack.py`)

```python
slack.send(message, channel="...")  # Post notification
```

### RAG Retrieval (via `app/services/rag.py`)

```python
rag.build_index(delivery_id, files)  # Create FAISS + BM25
rag.retrieve(delivery_id, query)     # Search for relevant code
rag.cleanup(delivery_id)             # Free memory
```

---

## 5. Complete Execution Flow - Step by Step

### Example: User opens PR with database code

**Timeline:**
```
T0:00 User opens PR #42 on myrepo
  └─ GitHub sends webhook POST /webhook

T0:01 FastAPI server receives webhook
  ├─ Verifies HMAC signature ✓
  ├─ Validates payload schema (Pydantic) ✓
  ├─ Queues _run_review() background task
  └─ Returns 202 Accepted to GitHub

T0:02 Background task starts (LangGraph)
  └─ Thread ID = delivery_id (for idempotency)

T0:03 Node: fetch_pr()
  ├─ GitHub API: Get PR details
  ├─ Guard checks:
  │   ├─ Draft? No ✓
  │   ├─ Closed? No ✓
  │   ├─ no-ai-review label? No ✓
  │   └─ >30 files? No (3 files) ✓
  ├─ GitHub API: Get .pr-review.yml → review_depth: "deep"
  ├─ GitHub API: Get PR file list
  └─ State: {pr_title, pr_body, pr_files, repo_config, ...}

T0:05 Node: analyze_diff()
  ├─ Filter files:
  │   ├─ app/db.py → Keep (source code)
  │   ├─ app/tests/db_test.py → Keep (test file relevant to change)
  │   └─ pyproject.toml → Skip (config only)
  ├─ Create chunks:
  │   ├─ Chunk 0: app/db.py (2500 chars) → risk_score=5, mode=DEEP
  │   └─ Chunk 1: app/tests/... (1800 chars) → risk_score=1, mode=LIGHT
  └─ State: {chunk_plans=[...], current_chunk_idx=0}

T0:06 Node: build_rag_index()
  ├─ Extract files: {app/db.py, app/tests/db_test.py}
  ├─ GitHub API: Fetch full content of both files
  ├─ Chunk files with RecursiveCharacterTextSplitter
  ├─ Google API: Generate embeddings for chunks
  ├─ Build FAISS index (semantic search)
  ├─ Build BM25 index (keyword search)
  ├─ Store in _stores[delivery_id]
  └─ State: {rag_index_built=True}

T0:08 Node: review_chunk() [Chunk 0: db.py]
  ├─ Initialize token budget: max=100k, used=0
  ├─ Check budget: 0 < 90k ✓
  ├─ Retrieve RAG context:
  │   ├─ Query: "database connection closing error handling"
  │   ├─ FAISS: Similar chunks (semantic)
  │   ├─ BM25: Keyword matches
  │   └─ Result: "def close_connection(): ... # found via RAG"
  ├─ Build messages:
  │   ├─ System: Staff-level engineer prompt
  │   ├─ User: Phase 0 context + Phase 1 review instructions
  │   └─ Context: diff + RAG context + repo context
  ├─ LLM call (attempt 1):
  │   ├─ Tokens: ~1200 input + 2000 output
  │   ├─ Response: {critical_issues: [...], reliability_issues: [...], ...}
  │   └─ Parsed ✓
  ├─ Token usage: used = 3200
  └─ State: {chunk_reviews=[{critical_issues: [...], ...}], token_budget_used=3200}

T0:15 Node: validate_findings() [Chunk 0]
  ├─ Unvalidated chunks: [chunk_0]
  ├─ Extract findings: 1 critical, 2 reliability, ...
  ├─ Validation LLM call:
  │   ├─ System: "Strict auditor, reject hallucinations"
  │   ├─ User: "Keep or reject each finding"
  │   ├─ Response: {"index": 0, "keep": true, "confidence": 0.92}
  │   └─ Result: All findings pass validation
  ├─ Filter: Remove confidence < 0.5, invalid file_path
  └─ State: {filtered_reviews=[...]}

T0:18 Node: route_after_review()
  ├─ Is last chunk? No (chunk 0 of 2)
  └─ Route: "advance"

T0:19 Node: advance_chunk()
  ├─ Sleep 2000ms (rate limiting)
  └─ State: {current_chunk_idx=1}

T0:21 Node: review_chunk() [Chunk 1: test file - LIGHT mode]
  ├─ Check budget: 3200 < 90k ✓
  ├─ Retrieve RAG context (same repo)
  ├─ Build messages (LIGHT mode → higher confidence threshold)
  ├─ LLM call: Response → {code_quality: [...], architecture_issues: [...]}
  └─ State: {chunk_reviews=[..., {...}], token_budget_used=5400}

T0:25 Node: validate_findings() [Chunk 1]
  ├─ Unvalidated chunks: [chunk_1]
  ├─ Validation LLM call
  └─ State: {filtered_reviews=[..., {...}]}

T0:28 Node: route_after_review()
  ├─ Is last chunk? Yes (chunk 1 of 2) ✓
  └─ Route: "merge"

T0:28 Node: merge_results()
  ├─ Merge chunk 0 + chunk 1 reviews
  ├─ Deduplicate:
  │   ├─ Group by normalized issue text
  │   ├─ Keep first, merge evidence
  │   ├─ Average confidence: (0.92 + 0.88) / 2 = 0.90
  │   └─ Result: "Connection not closed" with 2 pieces of evidence
  ├─ Merge context summaries (concat with ---)
  ├─ Convert to markdown (to_markdown())
  └─ State: {final_summary="## 🔴 Critical Issues\n**1.** Connection not closed...\n..."}

T0:29 Node: route_after_merge()
  ├─ Has final_summary? Yes ✓
  └─ Route: "publish"

T0:30 Node: post_results()
  ├─ Prepare GitHub comment
  ├─ Try inline comments:
  │   ├─ File: app/db.py, Line: 42
  │   ├─ Comment: "Add finally block to ensure close()"
  │   └─ GitHub API: POST /repos/.../pulls/42/reviews
  ├─ Post PR comment (fallback if inline fails):
  │   └─ GitHub API: POST /repos/.../issues/42/comments
  ├─ Prepare Slack message:
  │   ├─ Count issues: 🔴1, 🟠2, 🟡3
  │   └─ Build summary
  ├─ Slack API: Send message to #reviews channel
  ├─ RAG cleanup:
  │   └─ rag.cleanup(delivery_id) → Delete from _stores[delivery_id]
  └─ State: {slack_message="..."}

T0:32 Graph completes (END node)
  └─ LangGraph checkpoint saved (idempotent)

T0:33 Result logged
  └─ log.info("review_completed", delivery_id=..., chunks=2, critical=1, ...)
```

**Total time: ~30 seconds**

---

## 6. Error Handling Flow

### Example: LLM Quota Exhausted

```
T0:08 Node: review_chunk() [Chunk 0]
  ├─ LLM call fails:
  │   └─ Exception: "429: Quota exceeded. Retry in 60 seconds."
  ├─ Classify error: classify_llm_error(exc)
  │   └─ ErrorType.QUOTA_EXHAUSTED, retriable=True, retry_after_s=62
  ├─ Retry logic:
  │   ├─ Attempt 1: Failed
  │   ├─ Sleep 62s
  │   ├─ Attempt 2: Still failing (quota not recovered)
  │   └─ Max retries reached
  ├─ Return: {"quota_exhausted": True, "error": "API quota exhausted..."}
  └─ State: {quota_exhausted=True}

T1:10 Node: route_after_review()
  ├─ quota_exhausted=True? Yes
  └─ Has reviews? Chunk 0 not complete, no reviews
  └─ Route: "degraded"

T1:11 Node: handle_degraded()
  ├─ Build fallback message
  ├─ Post to GitHub + Slack
  ├─ RAG cleanup
  └─ Graph END

Result: User sees "Review pipeline failed; quota exhausted" comment
```

### Example: Token Budget Exhausted

```
T0:08 Node: review_chunk() [Chunk 0]
  ├─ Estimate tokens: 1200 input + 2000 output = 3200 total
  ├─ Check budget: used=0, max=100k, threshold=90k
  │   └─ is_budget_exhausted(0, 100k, 0.9)? No
  ├─ LLM call succeeds
  └─ State: {token_budget_used=3200}

T0:20 Node: review_chunk() [Chunk 1]
  ├─ Estimate tokens: 1500 + 2000 = 3500
  ├─ Budget check: used=3200, remaining=96.8k
  ├─ Would this chunk exceed budget?
  │   └─ 3500 > 96.8k * 0.8 = 77.4k? No, proceed
  ├─ LLM call succeeds
  └─ State: {token_budget_used=6700}

T0:32 Node: review_chunk() [Chunk 50 - many more chunks]
  ├─ Estimate tokens: 3000
  ├─ Check budget: used=87k, remaining=13k
  │   └─ is_budget_exhausted(87k, 100k, 0.9)? Yes! (87k ≥ 90k)
  ├─ Return: {"token_budget_exhausted": True}
  └─ State: {token_budget_exhausted=True}

T0:33 Node: route_after_review()
  ├─ token_budget_exhausted=True? Yes
  └─ Route: "degrade" or "validate" (depends on reviews)

Result: Stops processing; returns partial review with chunks 0-49
```

---

## 7. State Mutation & Accumulation

### Last-Write-Wins Fields
```python
current_chunk_idx = 0  # First write
# ... later ...
return {"current_chunk_idx": 1}  # Overwrites
# state.current_chunk_idx = 1
```

### Accumulating Fields (operator.add)
```python
chunk_reviews: Annotated[list[dict], operator.add]

# Initial state:
state["chunk_reviews"] = []

# Node 1 returns:
{"chunk_reviews": [{chunk_0_review}]}
# State becomes: chunk_reviews = [{chunk_0_review}]

# Node 2 returns:
{"chunk_reviews": [{chunk_1_review}]}
# State becomes: chunk_reviews = [{chunk_0_review}, {chunk_1_review}]  ← APPENDED!

# (NOT overwritten, but accumulated)
```

**Why?** Chunks are processed in a loop, and we need all results.

---

## 8. Idempotency & Checkpoint Recovery

### Scenario: Webhook delivered twice (GitHub retry)

```
First delivery (delivery_id="abc-123"):
T0:00 receive webhook with delivery_id="abc-123"
T0:05 node fetch_pr() executed
     └─ checkpoint saved
T0:10 node analyze_diff() executed
     └─ checkpoint saved
T0:15 node review_chunk() [Chunk 0]
     └─ timeout during LLM call!
T0:25 POST /webhook timeout to GitHub ✗

GitHub sees timeout → automatically retries with same delivery_id="abc-123"

Second delivery (same delivery_id="abc-123"):
T1:00 receive webhook with delivery_id="abc-123"
     └─ thread_id="abc-123" (same as before)
T1:01 LangGraph loads checkpoint for thread_id="abc-123"
     └─ Resumes from analyze_diff() (last saved state)
T1:02 Skip fetch_pr() and analyze_diff() (already done)
T1:03 Start review_chunk() [Chunk 0] again
     └─ LLM succeeds this time
T1:04 Continue as normal
...

Result: PR reviewed only once, despite two webhook deliveries
```

**Implementation:**
```python
# In server.py
result = await _compiled_graph.ainvoke(
  input_state,
  config={"configurable": {"thread_id": delivery_id}}
)

# LangGraph checkpointer:
# - Saves state after each node
# - Key = thread_id (delivery_id)
# - Resume = find saved thread, start from last node
```

---

## 9. Configuration & Settings

### `app/config.py` - Environment-driven

```python
class Settings:
  # Server
  port: int = 3400

  # GitHub
  github_token: str = ""
  github_app_id: int = 0
  github_app_private_key_path: str = ""

  # LLM
  llm_api_key: str = ""
  llm_flash_model: str = "gemini-2.5-flash"
  llm_temperature: float = 0.2
  llm_max_output_tokens: int = 8192
  llm_timeout_s: int = 25

  # PR guards
  max_files: int = 30
  max_diff_size: int = 30000  # bytes
  chunk_size: int = 5000      # chars per chunk

  # Optimization
  enable_early_exit: bool = False  # Exit after first clean chunk
  enable_rag: bool = True

  # Budget
  token_budget_per_review: int = 100000
  token_budget_threshold_pct: float = 0.9

  # Profiles (presets)
  review_profile: str = "cost_safe"  # "cost_safe", "balanced", "quality_heavy"
```

### Profile system
```python
PROFILES = {
  "cost_safe": {
    "min_response_length": 70,
    "enable_early_exit": True,
    "chunk_delay_ms": 3500
  },
  "balanced": {},
  "quality_heavy": {
    "min_response_length": 120,
    "enable_early_exit": False
  }
}
```

**How it works:**
```python
# In Settings validator
profile_name = os.environ.get("REVIEW_PROFILE", "cost_safe")
for key, val in PROFILES[profile_name].items():
  if key.upper() not in os.environ:  # Don't override explicit env vars
    values.setdefault(key, val)

# Precedence: Explicit env > Profile defaults > Base defaults
```

---

## 10. Logging & Observability

### Structured logging (structlog)

```python
log.info(
  "reviewing_chunk",
  index=0,
  total=2,
  risk=4,
  mode="deep",
  token_budget_pct=12
)

# Output:
# 2024-04-14T14:51:51.234Z [info] reviewing_chunk index=0 total=2 risk=4 mode=deep token_budget_pct=12
```

### Key log points

```
webhook_accepted                 # Webhook received
pr_fetched                        # PR metadata loaded
pr_skipped                        # Guard rejected PR
diff_analyzed                     # Diff chunking complete
rag_files_fetched                 # RAG files loaded
rag_index_built                   # Semantic index ready
reviewing_chunk                   # Starting LLM call
review_chunk_token_estimate       # Token estimate
structured_output_fallback        # JSON parsing fell back to regex
findings_validated                # Validation pass complete
reviews_merged                    # Chunks merged
review_posted                     # GitHub + Slack posted
review_completed                  # Graph finished (success)
review_failed                     # Graph crashed (error)
```

All logs include `delivery_id` for request tracing.

---

## Summary

The PR Review Agent is a **multi-stage pipeline** that:

1. **Receives** webhook from GitHub
2. **Guards** against processing drafts, closed PRs, large changes
3. **Analyzes** which files to review and risk-scores them
4. **Indexes** full repository for semantic search (RAG)
5. **Reviews** each chunk with LLM (structured output)
6. **Validates** findings against ground truth (second LLM pass)
7. **Merges** results across chunks
8. **Posts** to GitHub (inline comments + PR comment) and Slack
9. **Cleans up** resources (RAG indices)

All while **tracking tokens**, **handling errors gracefully**, and **ensuring idempotency** via checkpoint recovery.

The entire flow is **production-hardened** with proper error classification, budget tracking, config validation, and comprehensive logging.
