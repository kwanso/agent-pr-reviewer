# Execution Flows - Visual Guide

ASCII diagrams showing how data flows through the system in different scenarios.

---

## 1. Complete Happy Path: PR → Review → Posted

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Step 1: Webhook Received (app/server.py)                               │
└─────────────────────────────────────────────────────────────────────────┘

  GitHub Event                      FastAPI Server
  ─────────────                    ──────────────
  {
    "action": "opened",
    "pull_request": {
      "number": 42,
      "title": "Fix DB leak",
      "head": {"sha": "abc123"}
    },
    "repository": {
      "name": "myrepo",
      "owner": {"login": "myorg"}
    },
    "installation": {"id": 123},
    "delivery": "webhook-xyz"
  }
       │
       ├─ Verify HMAC signature
       ├─ Validate schema (Pydantic) ✓
       ├─ Filter action (opened/sync/reopened) ✓
       └─ Queue background task (_run_review)
            │
            └─ Return 202 Accepted to GitHub


┌─────────────────────────────────────────────────────────────────────────┐
│ Step 2: LangGraph Initialization                                       │
└─────────────────────────────────────────────────────────────────────────┘

  Background Task               LangGraph Compiler
  ────────────────            ──────────────────
  _run_review()
       │
       ├─ Build graph topology
       ├─ Compile with SQLite checkpointer
       │  └─ thread_id = "webhook-xyz" (from delivery_id)
       │
       └─ ainvoke(input_state, thread_id="webhook-xyz")


┌─────────────────────────────────────────────────────────────────────────┐
│ Step 3-9: Node Execution (see below for details)                       │
└─────────────────────────────────────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────────┐
│ Step Final: Output Posted (app/nodes/publish.py)                       │
└─────────────────────────────────────────────────────────────────────────┘

  Final Summary (Markdown)              External Services
  ──────────────────────               ────────────────
  ┌──────────────────────────────┐
  │ ## 🧠 Context Summary        │
  │ Scope: Database connection.. │  ┌─────────────────────────┐
  │                              │  │ GitHub API              │
  │ ## 🔴 Critical Issues        │  ├─────────────────────────┤
  │ **1.** Connection not closed │─→│ POST /repos/.../comments│
  │ - Impact: Pool exhaustion    │  │ Body: final_summary     │
  │ - Fix: Use context manager   │  └─────────────────────────┘
  │                              │
  │ ... [9 dimensions total] ... │  ┌─────────────────────────┐
  └──────────────────────────────┘  │ Slack API               │
       │                             ├─────────────────────────┤
       ├─────────────────────────────│ POST /chat.postMessage  │
       │                             │ text: Formatted summary │
       │                             │ channel: #reviews       │
       │                             └─────────────────────────┘
       │
       └─ Cleanup: rag.cleanup("webhook-xyz")
          Delete _stores["webhook-xyz"]  ← Free memory


┌─────────────────────────────────────────────────────────────────────────┐
│ Result: User sees PR comment + Slack notification                       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Detailed Node Execution Flow

```
┌────────────────────────────────────────────────────────────────┐
│ FETCH_PR (app/nodes/fetch.py)                                 │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Input State:                                                  │
│    owner: "myorg"                                              │
│    repo: "myrepo"                                              │
│    pr_number: 42                                               │
│    installation_id: 123                                        │
│                                                                │
│  Process:                                                      │
│    ┌──────────────────┐                                        │
│    │ GitHub API       │                                        │
│    │ GET /repos/.../  │                                        │
│    │ pulls/42         │                                        │
│    └────────┬─────────┘                                        │
│             │                                                  │
│    PR Details:                                                 │
│    {                                                           │
│      "title": "Fix DB leak",                                   │
│      "state": "open",                 ← Guard: not closed ✓    │
│      "draft": false,                  ← Guard: not draft ✓     │
│      "labels": ["bug"],               ← Guard: no-ai-review? ✓ │
│      "changed_files": 3               ← Guard: <30? ✓          │
│    }                                                           │
│             │                                                  │
│    All guards pass → Continue                                  │
│             │                                                  │
│    ┌──────────────────┐                                        │
│    │ GitHub API       │                                        │
│    │ GET .pr-review   │                                        │
│    │ .yml @ HEAD      │                                        │
│    └────────┬─────────┘                                        │
│             │                                                  │
│    Repo Config (optional):                                     │
│    {                                                           │
│      "language": "python",                                     │
│      "review_depth": "balanced"                                │
│    }                                                           │
│             │                                                  │
│    Sanitize config:                                            │
│    - Reject dicts/lists (only scalars)                         │
│    - Max 500 chars per value                                   │
│    - Remove control chars                                      │
│             │                                                  │
│    ┌──────────────────┐                                        │
│    │ GitHub API       │                                        │
│    │ GET /repos/.../  │                                        │
│    │ pulls/42/files   │                                        │
│    └────────┬─────────┘                                        │
│             │                                                  │
│    PR Files:                                                   │
│    [                                                           │
│      {                                                         │
│        "filename": "app/db.py",       ← Source code ✓          │
│        "patch": "@@...",                                       │
│        "changes": 15,                                          │
│        "status": "modified"                                    │
│      },                                                        │
│      {                                                         │
│        "filename": "tests/test_db.py" ← Test file ✓            │
│        ...                                                     │
│      },                                                        │
│      {                                                         │
│        "filename": "pyproject.toml"   ← Config ✗ (filter it)  │
│        ...                                                     │
│      }                                                         │
│    ]                                                           │
│             │                                                  │
│  Output State:                                                 │
│    pr_title: "Fix DB leak"                                     │
│    pr_body: "..."                                              │
│    pr_url: "https://github.com/..."                            │
│    pr_labels: ["bug"]                                          │
│    pr_files: [{app/db.py}, {tests/test_db.py}] ← Config removed
│    repo_config: {language, review_depth}                       │
│    repo_context_block: "Language: Python\nReview depth: ..."   │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ ANALYZE_DIFF (app/nodes/analyze.py)                           │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Input: pr_files = [{app/db.py}, {tests/test_db.py}]         │
│                                                                │
│  Step 1: Filter                                                │
│  ┌─────────────────────────────┐                               │
│  │ app/db.py                   │                               │
│  │ ✓ Not ignored               │                               │
│  │ ✓ Not binary                │                               │
│  │ ✓ Not generated             │                               │
│  │ ✓ Not in node_modules       │                               │
│  │ → KEEP                      │                               │
│  └─────────────────────────────┘                               │
│                                                                │
│  ┌─────────────────────────────┐                               │
│  │ tests/test_db.py            │                               │
│  │ ✓ Not ignored               │                               │
│  │ → KEEP                      │                               │
│  └─────────────────────────────┘                               │
│                                                                │
│  Result: 2 files to review                                     │
│             │                                                  │
│  Step 2: Create Chunks                                         │
│  ┌─────────────────────────────────────────┐                   │
│  │ app/db.py (diff)                        │                   │
│  │ Size: 2300 chars                        │                   │
│  │ Risk keyword: "connection", "db"        │                   │
│  │ → Chunk 0                               │                   │
│  └─────────────────────────────────────────┘                   │
│                                                                │
│  ┌─────────────────────────────────────────┐                   │
│  │ tests/test_db.py (diff)                 │                   │
│  │ Size: 1200 chars                        │                   │
│  │ No high-risk keywords                   │                   │
│  │ → Chunk 1                               │                   │
│  └─────────────────────────────────────────┘                   │
│             │                                                  │
│  Step 3: Score Risk (6 factors)                                │
│                                                                │
│  Chunk 0 (app/db.py):                                          │
│    ┌──────────────────────────────────────┐                    │
│    │ Factor          │ Score │ Notes      │                    │
│    ├──────────────────────────────────────┤                    │
│    │ Path: /app/     │ 0     │            │                    │
│    │ Keywords        │ 2     │ db, conn   │                    │
│    │ File count: 1   │ 0     │            │                    │
│    │ Complexity      │ 1     │ AST check  │                    │
│    │ Size: 2300 chars│ 1     │ Medium     │                    │
│    │ User pref       │ 0     │            │                    │
│    ├──────────────────────────────────────┤                    │
│    │ TOTAL           │ 4     │ High risk  │                    │
│    └──────────────────────────────────────┘                    │
│    → review_mode = "DEEP"                                      │
│                                                                │
│  Chunk 1 (tests/test_db.py):                                   │
│    ┌──────────────────────────────────────┐                    │
│    │ Factor          │ Score │ Notes      │                    │
│    ├──────────────────────────────────────┤                    │
│    │ Path: /tests/   │ 0     │            │                    │
│    │ Keywords        │ 0     │ None       │                    │
│    │ ...             │ 0     │ ...        │                    │
│    ├──────────────────────────────────────┤                    │
│    │ TOTAL           │ 0     │ Low risk   │                    │
│    └──────────────────────────────────────┘                    │
│    → review_mode = "LIGHT"                                     │
│                                                                │
│  Output State:                                                 │
│    chunk_plans: [                                              │
│      {                                                         │
│        "index": 0,                                             │
│        "chunk_text": "--- app/db.py\n@@ ...",                  │
│        "files": ["app/db.py"],                                 │
│        "risk_score": 4,                                        │
│        "review_mode": "deep"                                   │
│      },                                                        │
│      {                                                         │
│        "index": 1,                                             │
│        "chunk_text": "--- tests/test_db.py\n@@ ...",           │
│        "files": ["tests/test_db.py"],                          │
│        "risk_score": 0,                                        │
│        "review_mode": "light"                                  │
│      }                                                         │
│    ]                                                           │
│    current_chunk_idx: 0                                        │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ BUILD_RAG_INDEX (app/nodes/analyze.py)                        │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Input: chunk_plans (determines which files to index)         │
│    Files to index: ["app/db.py", "tests/test_db.py"]          │
│                                                                │
│  Process:                                                      │
│    GitHub API: Fetch full file contents @ HEAD                │
│      ├─ app/db.py (200 lines, no diffs, full file)            │
│      └─ tests/test_db.py (150 lines, full file)               │
│         │                                                      │
│    Chunk files with RecursiveCharacterTextSplitter:           │
│      ├─ Split on: class def, async def, \n\n, \n, space      │
│      ├─ Size: 800 chars per chunk, 100 overlap               │
│      └─ Result: 50 chunks total                               │
│         │                                                      │
│    Create embeddings (Google API):                             │
│      ├─ For each chunk: generate_embedding(text)              │
│      ├─ Dimension: 768-d vectors                              │
│      └─ Cost: ~1 API call per chunk                           │
│         │                                                      │
│    Build FAISS index (semantic):                               │
│      ├─ Add vectors to FAISS                                  │
│      └─ Enable similarity_search(query_vector, k=5)           │
│         │                                                      │
│    Build BM25 index (keyword):                                 │
│      ├─ TF-IDF scoring on documents                           │
│      └─ Enable get_relevant_documents(query_text, k=5)        │
│         │                                                      │
│    Store in memory:                                            │
│      _stores["webhook-xyz"] = faiss_index                      │
│      _bm25_stores["webhook-xyz"] = bm25_index                  │
│      _metadata_stores["webhook-xyz"] = file_metadata           │
│      _index_created_at["webhook-xyz"] = time.time()            │
│                                                                │
│  Output State:                                                 │
│    rag_index_built: True                                       │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ REVIEW_CHUNK [LOOP: Chunk 0, 1, ...]                         │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  For each chunk:                                               │
│                                                                │
│  Input State:                                                  │
│    current_chunk_idx: 0 (or 1, 2, ...)                         │
│    chunk_plans[0]: chunk_text, files, risk_score, review_mode │
│    rag_index_built: True                                       │
│                                                                │
│  Step 1: Token Budget Check                                   │
│    ┌──────────────────────────────────────┐                    │
│    │ token_budget_max: 100000             │                    │
│    │ token_budget_used: 0 (chunk 0) ✓     │                    │
│    │ Threshold: 90000 (90%)               │                    │
│    │ → Budget OK, proceed                 │                    │
│    └──────────────────────────────────────┘                    │
│                                                                │
│  Step 2: Retrieve RAG Context                                 │
│    Query: "database connection closing error handling"        │
│      │                                                        │
│    FAISS semantic search:                                      │
│      └─ similarity_search(query_vector, k=5)                  │
│         Result: [doc_1 (score: 0.92), doc_2 (0.87), ...]      │
│      │                                                        │
│    BM25 keyword search:                                        │
│      └─ get_relevant_documents("database connection...", k=5) │
│         Result: [doc_3, doc_4, ...]                            │
│      │                                                        │
│    Merge results (prefer docs in both):                        │
│      └─ Ranked list of 5 docs with metadata                   │
│      │                                                        │
│    Format as evidence block:                                   │
│      RAG_CONTEXT = """                                         │
│      ## Related Code from Repository                           │
│      \n\nDoc 1: (source: app/db.py:10-15)                      │
│      def close_connection():                                   │
│        conn.close()                                            │
│      \n\nDoc 2: ...                                             │
│      """                                                       │
│                                                                │
│  Step 3: Build LLM Prompt                                      │
│    ┌─────────────────────────────────────┐                     │
│    │ [SYSTEM]                            │                     │
│    │ You are a Staff-level engineer...   │                     │
│    │                                     │                     │
│    │ [CONTEXT - Phase 0]                 │                     │
│    │ ## Phase 0: Context Understanding   │                     │
│    │ 1. What changed?                    │                     │
│    │ 2. External dependencies?           │                     │
│    │ ...                                 │                     │
│    │                                     │                     │
│    │ [REVIEW INSTRUCTIONS - Phase 1]     │                     │
│    │ ## Phase 1: Multi-Dimensional...    │                     │
│    │ Dimensions:                         │                     │
│    │ 1. Critical Issues                  │                     │
│    │ 2. Reliability                      │                     │
│    │ ...                                 │                     │
│    │ 9. Production Readiness             │                     │
│    │                                     │                     │
│    │ [SUPPLIED MATERIALS]                │                     │
│    │ DIFF:                               │                     │
│    │ --- app/db.py                       │                     │
│    │ @@ -22,5 +22,7 @@                  │                     │
│    │ try:                                │                     │
│    │   query()                           │                     │
│    │ +except Exception:                  │                     │
│    │ +  return  # BUG: no close!         │                     │
│    │                                     │                     │
│    │ RAG_CONTEXT:                        │                     │
│    │ ## Related Code from Repository     │                     │
│    │ [RAG retrieval results...]          │                     │
│    │                                     │                     │
│    │ REPO_CONTEXT:                       │                     │
│    │ Language: Python                    │                     │
│    │ Review focus: Reliability           │                     │
│    │                                     │                     │
│    │ ALLOWED_FILE_PATHS:                 │                     │
│    │ - app/db.py                         │                     │
│    │ - tests/test_db.py                  │                     │
│    │                                     │                     │
│    │ MODE: DEEP (risk_score=4)           │                     │
│    └─────────────────────────────────────┘                     │
│         │                                                      │
│  Step 4: LLM Call (with retries)                               │
│    ┌──────────────────────────┐                                │
│    │ ChatGoogleGenerativeAI   │                                │
│    │ model: gemini-2.5-flash  │                                │
│    │ temp: 0.2 (deterministic)│                                │
│    │ max_tokens: 8192         │                                │
│    │ timeout: 25s             │                                │
│    └────────┬─────────────────┘                                │
│             │                                                  │
│    Attempt 1:                                                  │
│      └─ Response parsed ✓ (Pydantic validation passed)        │
│         JSON → ReviewOutput object                             │
│         │                                                      │
│    Response:                                                   │
│    {                                                           │
│      "context_summary": "Scope: Database conn management...",  │
│      "critical_issues": [                                      │
│        {                                                       │
│          "issue": "Connection not closed in error path",       │
│          "why_it_matters": "Pool exhaustion → outage",        │
│          "suggested_fix": "Use try/finally",                   │
│          "evidence": "Line 25: except Exception: return",      │
│          "file_path": "app/db.py",                             │
│          "line": 25,                                           │
│          "confidence": 0.92                                    │
│        }                                                       │
│      ],                                                        │
│      "reliability_issues": [                                   │
│        {...},                                                  │
│        {...}                                                   │
│      ],                                                        │
│      ... (9 dimensions total)                                  │
│    }                                                           │
│         │                                                      │
│  Step 5: Track Token Usage                                     │
│    Estimated: 1200 input + 2000 output = 3200 total           │
│    used = 0 + 3200 = 3200                                      │
│                                                                │
│  Output State (ACCUMULATES):                                   │
│    chunk_reviews: [                                            │
│      {                                                         │
│        "chunk_index": 0,                                       │
│        "review_mode": "deep",                                  │
│        "risk_score": 4,                                        │
│        "context_summary": "...",                               │
│        "critical_issues": [...],                               │
│        ... (all 9 dimensions)                                  │
│      }                                                         │
│    ]                                                           │
│    token_budget_used: 3200                                     │
│    early_exit: False                                           │
│                                                                │
│  [Routing decision: Is last chunk? No → go to VALIDATE]       │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
│  ... [VALIDATE, ADVANCE, LOOP for Chunk 1, etc.] ...          │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ MERGE_RESULTS (app/nodes/publish.py)                          │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Input: chunk_reviews = [chunk_0_review, chunk_1_review, ...] │
│         filtered_reviews = [filtered_0, filtered_1, ...]      │
│                                                                │
│  Step 1: Choose source                                         │
│    if len(filtered_reviews) == len(chunk_reviews):             │
│      use = filtered_reviews  ← Prefer validated                │
│    else:                                                       │
│      use = chunk_reviews     ← Fallback                        │
│                                                                │
│  Step 2: Deduplicate by dimension                              │
│                                                                │
│    critical_issues:                                            │
│      - Normalize text: "connection not closed" (lowercase)      │
│      - Group by normalized text                                │
│      - Chunk 0: confidence 0.92, evidence "line 25"            │
│      - Chunk 1: confidence 0.88, evidence "line 50"            │
│      - Merged: avg confidence 0.90, both evidences             │
│                                                                │
│    [repeat for all 9 dimensions]                               │
│                                                                │
│  Step 3: Merge context summaries                               │
│    final_context = """                                         │
│    Scope (from chunk 0): Database connection...                │
│    ---                                                         │
│    Scope (from chunk 1): Test file changes...                  │
│    """                                                         │
│                                                                │
│  Output State:                                                 │
│    final_summary: """                                          │
│    ## 🧠 Context Summary                                       │
│    Scope: [merged contexts above]                              │
│                                                                │
│    ## 🔴 Critical Issues (Must Fix)                            │
│    **1.** Connection not closed in error path                  │
│    - Impact: Pool exhaustion → service outage                 │
│    - Fix: Use try/finally to ensure close()                    │
│    - Evidence: Line 25 (chunk 0) | Line 50 (chunk 1)          │
│                                                                │
│    ... [9 sections] ...                                        │
│    """                                                         │
│    inline_comments: [                                          │
│      {"file": "app/db.py", "line": 25, "comment": "Add..."},  │
│      {"file": "tests/test_db.py", "line": 10, "comment": "..."} │
│    ]                                                           │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ POST_RESULTS (app/nodes/publish.py)                           │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  Input: final_summary, inline_comments                        │
│                                                                │
│  Step 1: Post inline comments to GitHub                        │
│    GitHub API: POST /repos/myorg/myrepo/pulls/42/reviews      │
│      ├─ Comments on line 25 (app/db.py)                       │
│      └─ Comments on line 10 (tests/test_db.py)                │
│                                                                │
│  Step 2: Post PR comment (fallback)                            │
│    GitHub API: POST /repos/myorg/myrepo/issues/42/comments    │
│      └─ Body: final_summary (full markdown)                    │
│                                                                │
│  Step 3: Post to Slack                                         │
│    Convert markdown to Slack format:                            │
│      - ## Header → *Header*                                    │
│      - **bold** → *bold*                                       │
│      - Count issues per dimension                              │
│                                                                │
│    Slack API: POST /chat.postMessage                           │
│      {                                                         │
│        "channel": "#reviews",                                  │
│        "text": "━━━━━━━━━━\n*📋 PR Review Summary*\n...",   │
│        "blocks": [...]                                         │
│      }                                                         │
│                                                                │
│  Step 4: Cleanup RAG                                           │
│    rag.cleanup("webhook-xyz")                                  │
│      ├─ _stores.pop("webhook-xyz")                             │
│      ├─ _bm25_stores.pop("webhook-xyz")                        │
│      ├─ _metadata_stores.pop("webhook-xyz")                    │
│      └─ _index_created_at.pop("webhook-xyz")                   │
│                                                                │
│  Output State:                                                 │
│    slack_message: "..."                                        │
│                                                                │
│  Side effects:                                                 │
│    ✓ PR comment visible on GitHub                              │
│    ✓ Slack notification in #reviews channel                    │
│    ✓ Memory freed (RAG indices deleted)                        │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                        END (Success)
```

---

## 3. Error Path: LLM Call Fails with Quota Exhaustion

```
review_chunk() [Chunk 0]
    │
    ├─ LLM call attempt 1:
    │   └─ Exception: "429 Too Many Requests: Quota Exceeded"
    │
    ├─ classify_llm_error(exc):
    │   ├─ msg = "429 Too Many Requests..."
    │   ├─ matches "429"? Yes!
    │   └─ return ClassifiedError(
    │         error_type=ErrorType.QUOTA_EXHAUSTED,
    │         retriable=True,
    │         retry_after_s=62  ← extracted from error
    │       )
    │
    ├─ Retry logic:
    │   ├─ retriable=True AND attempt < 3? Yes
    │   ├─ await asyncio.sleep(62 + 2)  ← +2s safety margin
    │   └─ Attempt 2...
    │
    ├─ LLM call attempt 2:
    │   └─ Exception: "429 Too Many Requests..." (still failing)
    │
    ├─ Still retriable AND attempt < 3? Yes
    │   └─ Attempt 3...
    │
    ├─ LLM call attempt 3:
    │   └─ Exception: "429 Too Many Requests..."
    │
    ├─ Still retriable AND attempt < 3? No (attempt == 3)
    │   └─ Break out of retry loop
    │
    └─ Return {
         "quota_exhausted": True,
         "error": "API quota exhausted; retry later"
       }
           │
           ▼
       route_after_review():
           ├─ quota_exhausted=True? Yes
           ├─ has_reviews? No (chunk 0 never completed)
           └─ return "degraded"
               │
               ▼
           handle_degraded():
               ├─ Build fallback message:
               │   ```
               │   ## Automated Review Status
               │
               │   The review pipeline ran, but could not generate...
               │   Reason: API quota exhausted; retry later
               │   ```
               │
               ├─ Post to GitHub + Slack
               └─ Cleanup RAG
                   │
                   ▼
               Graph END [Error state logged]
```

---

## 4. Idempotency: Webhook Retried After Timeout

```
FIRST DELIVERY (delivery_id = "abc-123")
──────────────────────────────────────

T0:00  GitHub sends webhook
       └─ POST /webhook with delivery_id="abc-123"

T0:01  server.py receives, queues _run_review()

T0:02  LangGraph starts with thread_id="abc-123"

T0:05  fetch_pr() completes
       └─ Checkpoint saved: state @ fetch_pr

T0:08  analyze_diff() completes
       └─ Checkpoint saved: state @ analyze_diff

T0:10  review_chunk() [Chunk 0]
       ├─ LLM call IN PROGRESS...
       └─ Timeout! (timeout=25s at T0:35)

T0:40  Graph execution dies (timeout)

T0:41  POST response never sent to GitHub ✗

GitHub sees timeout → Auto-retry with same delivery_id


SECOND DELIVERY (delivery_id = "abc-123" — SAME)
──────────────────────────────────────────────

T1:00  GitHub sends webhook (retry)
       └─ POST /webhook with delivery_id="abc-123"

T1:01  server.py receives

T1:02  LangGraph compiler initializes
       ├─ thread_id = "abc-123"
       ├─ AsyncSqliteSaver.get_last_step(thread_id="abc-123")
       └─ Returns: state @ analyze_diff (last checkpoint)
              │
              └─ state includes:
                  - pr_title, pr_body, pr_files ✓ (already fetched)
                  - chunk_plans ✓ (already analyzed)
                  - current_chunk_idx = 0

T1:02  LangGraph RESUMES from last node
       ├─ Skip: fetch_pr() [already done]
       ├─ Skip: analyze_diff() [already done]
       ├─ Skip: build_rag_index() [already done]
       └─ Start: review_chunk() [same chunk, fresh attempt]
           │
           ├─ LLM call attempt 1
           └─ SUCCESS! ✓
               └─ review_dict written to chunk_reviews[]
                  └─ Checkpoint saved

T1:15  Continue normally:
       ├─ validate_findings()
       ├─ advance_chunk()
       ├─ [loop for chunk 1...]
       └─ merge_results()

T1:30  post_results()
       ├─ GitHub comment posted ✓
       └─ Slack message sent ✓

T1:35  Graph END (SUCCESS)

RESULT: PR reviewed exactly once, despite 2 webhook deliveries!
```

---

## 5. Token Budget Exhaustion

```
review_chunk() [Chunk 0]
  ├─ Initialize budget: max=100k, used=0
  └─ LLM call succeeds
      └─ used = 3200

review_chunk() [Chunk 1]
  ├─ Budget check: used=3200, remaining=96.8k ✓
  └─ LLM call succeeds
      └─ used = 6700

... [more chunks] ...

review_chunk() [Chunk 45]
  ├─ Budget check: used=87k, remaining=13k ✓
  └─ LLM call succeeds
      └─ used = 90.2k

review_chunk() [Chunk 46]
  ├─ Estimate tokens: 3500
  ├─ Budget check:
  │   used = 90.2k
  │   threshold = 100k * 0.9 = 90k
  │   is_budget_exhausted(90.2k, 100k, 0.9)?
  │   └─ 90.2k >= 90k? YES! ✓
  │
  ├─ Return {
  │   "token_budget_exhausted": True
  │ }
  │
  └─ state.token_budget_exhausted = True

route_after_review():
  ├─ token_budget_exhausted=True? Yes
  └─ return "degraded"

Result: Review stops at chunk 45
        Return partial review with 45 chunks analyzed
```

---

This visual guide shows the complete flow of data through the system. Refer to this when tracing execution or understanding error scenarios.
