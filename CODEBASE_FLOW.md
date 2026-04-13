# PR Review Agent - Code & Flow Guide

## 🎯 **What This System Does (30-Second Summary)**

This is a **GitHub PR reviewer bot** that:
1. Receives PR webhooks from GitHub
2. Analyzes code changes with AI (LLM + RAG context)
3. Posts findings to GitHub & Slack
4. Supports multiple GitHub organizations

**Tech Stack:** FastAPI + LangGraph + Google Gemini + FAISS RAG + SQLite

---

## 📁 **Directory Structure**

```
pr-review-agent-py/
├── app/
│   ├── server.py              # FastAPI webhook endpoint + health check
│   ├── state.py               # TypedDict state flowing through pipeline
│   ├── graph.py               # LangGraph topology definition
│   ├── config.py              # Settings + environment loading
│   ├── models.py              # ReviewOutput, ReviewFinding schemas
│   │
│   ├── nodes/                 # Pipeline stages (each node = step in workflow)
│   │   ├── fetch.py           # Get PR metadata + load .pr-review.yml
│   │   ├── analyze.py         # Filter files + chunk diffs + risk scoring
│   │   ├── review.py          # Call LLM with RAG context
│   │   ├── validate.py        # Filter false positives (2nd LLM pass)
│   │   └── publish.py         # Post to GitHub + Slack
│   │
│   ├── services/              # External integrations
│   │   ├── github.py          # GitHub App auth + API client
│   │   ├── rag.py             # FAISS + BM25 indexing + retrieval
│   │   └── slack.py           # Slack notifications
│   │
│   └── utils/                 # Helpers
│       ├── diff.py            # Diff filtering + chunking + risk scoring
│       ├── prompts.py         # Prompt template building
│       └── metrics.py         # Token counting
│
├── tests/                      # Pytest tests
├── ARCHITECTURE.md             # System design + data flow
├── TECHNICAL_SUMMARY.md        # High-level overview
└── pyproject.toml             # Dependencies
```

---

## 🔄 **Complete Request Flow**

### **1️⃣ Webhook Entry (server.py:83-131)**

```python
POST /webhook
├─ Extract GitHub webhook
│  ├── owner, repo, pr_number, head_sha
│  ├── installation_id (for multi-org auth)
│  └── delivery_id (idempotency key)
│
├─ Verify HMAC-SHA256 signature
│
├─ Validate action ∈ {opened, synchronize, reopened}
│
└─ Schedule background task: _run_review(input_state)
   └─ Return 202 Accepted immediately
```

**Key:** Request returns immediately; review happens in background.

---

### **2️⃣ LangGraph Pipeline (graph.py:86-131)**

Graph topology:

```
START
  ↓
fetch_pr ──────────────────────┐
  ↓                            │
analyze_diff ──────────────┐   │
  ↓                        │   │
build_rag_index ──────────┤   │
  ↓                        │   │
review_chunk ───┐          │   │
  ↓              │          │   │
validate_findings   │          │   │
  ↓              │          │   │
advance_chunk ◄─┘          │   │
  ↓                        │   │
merge_results              │   │
  ↓                        │   │
post_results               │   │
  ↓                        │   │
END ◄──────────────────────┴───┴─── (skip paths)
```

**Conditional routing** decides paths based on state:
- `route_after_fetch`: Skip if marked
- `route_after_analysis`: Skip if no chunks
- `route_after_review`: Validate findings or exit on error
- `route_after_validate`: Advance or merge
- `route_after_merge`: Publish or degrade

---

## 🔍 **State Object (state.py:8-69)**

All data flows through `PRReviewState` (TypedDict):

```python
class PRReviewState(TypedDict, total=False):
    # Webhook input
    owner: str              # GitHub org
    repo: str              # Repo name
    pr_number: int         # PR #123
    head_sha: str          # Commit SHA
    installation_id: int   # Multi-org auth

    # Fetched PR data
    pr_title: str          # PR title
    pr_body: str           # PR description
    pr_files: list[dict]   # Changed files
    pr_draft: bool         # Draft status

    # Diff analysis results
    chunk_plans: list[dict]       # Chunks to review + risk scores
    current_chunk_idx: int        # Loop counter

    # RAG data
    rag_index_built: bool  # FAISS index ready?

    # Accumulated results (operator.add = append mode)
    chunk_reviews: list[dict]     # LLM outputs per chunk
    validated_findings: list[dict] # After filtering
    model_usage: list[str]        # Token counts

    # Control flow
    quota_exhausted: bool  # Hit rate limit?
    early_exit: bool       # Stop early?
    error: str             # Error message

    # Final output
    final_summary: str     # Markdown GitHub comment
    slack_message: str     # Slack notification
```

**Note:** Fields with `Annotated[list[dict], operator.add]` accumulate across nodes.

---

## 🧩 **Node Details (What Each Node Does)**

### **Node 1: fetch_pr (nodes/fetch.py)**

Runs first. Fetches PR metadata.

```python
async def fetch_pr(state) -> dict:
    1. Get GitHub client (org-specific JWT auth)
    2. Fetch PR details (title, body, files, draft status)
    3. Load .pr-review.yml from repo
    4. Extract repo context block
    5. Guard checks:
       - Skip if draft
       - Skip if internal labels
       - Skip if already reviewed
    6. Return: pr_title, pr_body, pr_files, repo_config
```

---

### **Node 2: analyze_diff_node (nodes/analyze.py:17-62)**

Prepares chunks for review.

```python
async def analyze_diff_node(state) -> dict:
    from app.utils.diff import analyze_diff

    1. Filter files (ignore config, lock files, etc.)
    2. Group files by directory
    3. Create chunks (~5000 chars each)
    4. Risk score each chunk:
       - Path keywords (auth, payment, secret) → +2
       - Code keywords (token, password) → +2
       - Complexity (if/for/class/def count)
       - File size > 2000 chars → +0.5-1
       - User preference (deep/light) ±2
    5. Sort by risk (highest first)
    6. Return: chunk_plans[]
```

**Example output:**
```python
chunk_plans = [
    {
        "index": 0,
        "files": ["auth_service.py"],
        "chunk_text": "--- auth_service.py\n-old\n+new",
        "risk_score": 4,
        "review_mode": "deep"     # High risk → thorough review
    },
    {
        "index": 1,
        "files": ["utils.py"],
        "chunk_text": "...",
        "risk_score": 0,
        "review_mode": "light"    # Low risk → quick scan
    }
]
```

---

### **Node 3: build_rag_index (nodes/analyze.py:65-104)**

Fetches full file content and builds search index.

```python
async def build_rag_index(state) -> dict:
    1. Extract filenames from chunk_plans
    2. Fetch FULL FILE content for each file
       (GitHub API: GET /repos/{owner}/{repo}/contents/{file})
    3. Build FAISS + BM25 indexes:

       FAISS (Vector embeddings):
       └─ GoogleGenerativeAIEmbeddings
          └─ Semantic search (slow, accurate)

       BM25 (Keyword index):
       └─ rank_bm25 library
          └─ Keyword matching (fast, free)

    4. Store in memory:
       _stores[delivery_id] = FAISS index
       _bm25_stores[delivery_id] = BM25 index

    5. Return: rag_index_built = True
```

**What's stored:** Full file content, not just diffs.

---

### **Node 4: review_chunk (nodes/review.py:55-180)**

**Core LLM call** with structured output.

```python
async def review_chunk(state) -> dict:
    chunk = chunk_plans[current_chunk_idx]

    1. Query RAG for context:
       docs = rag.retrieve(chunk["chunk_text"], k=5)
       rag_context = "\n\n".join(docs)

    2. Build LLM prompt:
       messages = [
           {role: "system", content: "You review PRs..."},
           {role: "user", content: f"""
               PR Title: {pr_title}

               DIFF:
               {chunk['chunk_text']}

               CONTEXT (full files):
               {rag_context}

               Review for: critical issues, reliability, database, etc.
           """}
       ]

    3. Call LLM with structured output (Pydantic schema):
       llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")
       structured = llm.with_structured_output(ReviewOutput)
       result = await structured.ainvoke(messages)

    4. Handle rate limit (429):
       if 429:
           sleep(suggested_delay + 2s)
           retry (max 4 times)

    5. Parse output:
       review: ReviewOutput {
           context_summary: "...",
           critical_issues: [ReviewFinding, ...],
           reliability_issues: [...],
           ...
       }

    6. Return: chunk_reviews += [review]
```

**ReviewOutput schema (models.py):**
```python
class ReviewFinding:
    issue: str              # "Uncaught promise rejection"
    why_it_matters: str     # "Unhandled errors crash the service"
    suggested_fix: str      # "Add .catch() handler"
    evidence: str           # Code snippet
    file_path: str          # "src/api.py"
    line: int              # 42
    confidence: float      # 0-1, filter < 0.5

class ReviewOutput:
    context_summary: str
    critical_issues: list[ReviewFinding]
    reliability_issues: list[ReviewFinding]
    database_issues: list[ReviewFinding]
    resource_management: list[ReviewFinding]
    code_quality: list[ReviewFinding]
    input_validation_issues: list[ReviewFinding]
    performance_scalability: list[ReviewFinding]
    architecture_issues: list[ReviewFinding]
    production_readiness: list[ReviewFinding]
```

---

### **Node 5: advance_chunk (nodes/review.py)**

Loop counter increment.

```python
async def advance_chunk(state) -> dict:
    return {"current_chunk_idx": state["current_chunk_idx"] + 1}
```

Routes back to `review_chunk` for next chunk.

---

### **Node 6: validate_findings (nodes/validate.py)**

**Second LLM pass** to filter false positives.

```python
async def validate_findings(state) -> dict:
    # For each finding from review_chunk:
    for finding in state["chunk_reviews"]:
        1. Re-query LLM with context:
           "Is this issue real? Confidence 0-1?"

        2. Filter: confidence < 0.5 → discard

        3. Return: validated_findings += [finding]
```

Reduces noise from LLM hallucinations.

---

### **Node 7: merge_results (nodes/publish.py)**

Combines all chunks' findings.

```python
async def merge_results(state) -> dict:
    1. Collect all validated_findings from all chunks

    2. Deduplicate (same issue across chunks)

    3. Group by category:
       - Critical issues
       - Reliability
       - Database
       - etc.

    4. Render markdown:
       final_summary = f"""
       # PR Review Summary

       ## 🔴 Critical Issues (2)
       - Issue 1: ...
       - Issue 2: ...

       ## 🟠 Reliability Issues (1)
       - Issue 3: ...

       [View on GitHub](pr_url)
       """

    5. Return: final_summary
```

---

### **Node 8: post_results (nodes/publish.py)**

Posts to GitHub + Slack.

```python
async def post_results(state) -> dict:
    1. Post to GitHub:
       github.create_review_comment(
           owner, repo, pr_number,
           final_summary,
           commit_sha=head_sha
       )

    2. Post to Slack:
       - Read slack_channel from repo_config
       - Format summary
       - slack_client.chat_postMessage(
             channel="#backend-reviews",
             blocks=[...]
         )

    3. Return: {"posted": True}
```

---

### **Node 9: handle_degraded**

Fallback for errors. Posts minimal error note.

---

## 🔐 **GitHub App Authentication (services/github.py)**

For multi-org support:

```
GitHub App installed in Org A, Org B, Org C

Each PR webhook includes: installation_id

Agent creates:
  1. JWT (signed with app private key)
  2. Exchange JWT for 1-hour access token
  3. Cache token (refresh 5 min before expiry)
  4. Make API calls with token

Fallback: GITHUB_TOKEN (personal access token)
```

---

## 🧠 **RAG System (services/rag.py)**

Hybrid search for context:

```
Input: Diff + full file contents

FAISS (Semantic):
├─ Chunk full files
├─ Embed with GoogleGenerativeAI
├─ Store vectors
└─ Query: "What's the connection lifecycle?"
   → Similar code patterns

BM25 (Keyword - Fallback):
├─ TF-IDF index
├─ No API calls (free)
└─ Query: "connection" → Exact matches

Retrieve(query):
├─ FAISS.search() → Top docs
├─ BM25.search() → Top docs
└─ Merge + dedup → Top 5 results
```

**Why both?**
- FAISS is smart but quota-limited
- BM25 is reliable fallback when quota exhausted

---

## 🔄 **Chunk Loop Example**

Given 2 chunks (auth_service.py, utils.py):

```
State: current_chunk_idx = 0

1. review_chunk (idx=0)
   └─ LLM reviews auth_service.py
   └─ chunk_reviews += [ReviewOutput {...}]
   └─ State: chunk_reviews = [review0]

2. advance_chunk
   └─ current_chunk_idx = 1

3. review_chunk (idx=1)
   └─ LLM reviews utils.py
   └─ chunk_reviews += [ReviewOutput {...}]
   └─ State: chunk_reviews = [review0, review1]

4. advance_chunk
   └─ current_chunk_idx = 2

5. route_after_review
   └─ 2 >= len(plans)-1? YES
   └─ Route to "validate"

6. validate_findings
   └─ Filter both findings
   └─ validated_findings += [filtered...]

7. route_after_validate
   └─ Last chunk? YES
   └─ Route to "merge"

8. merge_results
   └─ Combine + deduplicate
   └─ final_summary = "# Review Summary..."

9. route_after_merge
   └─ Has findings? YES
   └─ Route to "publish"

10. post_results
    └─ Post to GitHub + Slack
```

---

## 📊 **Configuration Files**

### **.env (Environment)**

```bash
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=./app.pem
SLACK_BOT_TOKEN=xoxb-xxx
LLM_API_KEY=google-api-key
WEBHOOK_SECRET=random-hex
```

### **.pr-review.yml (Per-Repo)**

Place in repo root:

```yaml
slack_channel: "#backend-reviews"
review_depth: balanced    # light, balanced, deep
review_focus:
  - security
  - error-handling
  - performance
ignore_patterns:
  - "*.test.js"
  - "tests/**"
  - "migrations/**"
risk_paths:
  - "auth/"
  - "payment/"
```

---

## ⚡ **Key Design Decisions**

| Decision | Why |
|----------|-----|
| **FastAPI + async** | High concurrency, fast webhook handling |
| **LangGraph** | Complex state machine with checkpointing |
| **Chunking** | LLM token limits; can't review whole PR |
| **Hybrid RAG** | FAISS (smart) + BM25 (reliable) fallback |
| **Validation pass** | Reduce false positives from LLM hallucinations |
| **SQLite checkpointing** | Crash recovery; resume from last state |
| **GitHub App** | Multi-org support without sharing tokens |

---

## 🎯 **Error Handling & Fallbacks**

| Failure | Fallback |
|---------|----------|
| LLM 429 (quota) | Backoff + retry up to 4 times |
| LLM parsing fails | Try Markdown fallback parse |
| FAISS quota exhausted | Switch to BM25 only |
| GitHub API fails | Retry with exponential backoff |
| RAG both fail | Review without context |
| Slack post fails | Log warning, continue |

---

## 📈 **Performance**

| Operation | Time |
|-----------|------|
| Fetch PR metadata | ~100ms |
| Build RAG index | ~500ms-1s |
| Review 1 chunk | ~10-30s (LLM inference) |
| Validate findings | ~3-5s |
| Post to GitHub + Slack | ~500ms |
| **Total per PR** | ~30-60s |

---

## 🚀 **Running the System**

```bash
# Install dependencies
pip install -e .

# Set environment
export GITHUB_APP_ID=...
export GITHUB_APP_PRIVATE_KEY_PATH=./app.pem
export LLM_API_KEY=...
export WEBHOOK_SECRET=...

# Start server
python -m app.server

# Or via CLI
cd pr-review-agent-py
python -m app.server

# Server listens on: http://localhost:8000/webhook
```

Configure GitHub App webhook to: `https://yourdomain.com/webhook`

---

## 📚 **Important Files to Know**

| File | Lines | Key Function |
|------|-------|--------------|
| `server.py` | 150 | FastAPI + webhook routing |
| `graph.py` | 130 | LangGraph topology + routing |
| `state.py` | 70 | State schema |
| `nodes/fetch.py` | 80 | PR metadata + config |
| `nodes/analyze.py` | 100 | Diff + chunking + RAG index |
| `nodes/review.py` | 200+ | LLM call with retry logic |
| `nodes/validate.py` | 150 | Filter false positives |
| `nodes/publish.py` | 250+ | GitHub + Slack posting |
| `services/github.py` | 400+ | GitHub API + JWT auth |
| `services/rag.py` | 200+ | FAISS + BM25 indexing |
| `utils/diff.py` | 300+ | Diff parsing + chunking + risk |
| `models.py` | 550+ | Pydantic schemas |

---

## 🎓 **Understanding Key Concepts**

### **What's a "Chunk"?**
A chunk is a portion of the PR diff (default 5000 chars). Larger PRs are split into chunks because LLM has token limits.

### **What's "RAG"?**
Retrieval-Augmented Generation. Instead of just showing the LLM the diff, we also fetch the **full file content** and use semantic/keyword search to give the LLM surrounding context.

### **What's a "Delivery"?**
Each webhook is a delivery with a unique `delivery_id`. This ID becomes the SQLite checkpoint thread_id for crash recovery.

### **Why Multi-Org?**
GitHub App installed in multiple orgs → Each webhook includes `installation_id` → Agent creates org-specific JWT → No shared token.

### **Why Validate After Review?**
LLMs hallucinate. Second pass filters low-confidence findings to reduce noise.

---

## 🔗 **Data Flow Summary**

```
Webhook Event
  ↓
Extract: owner, repo, pr_number, installation_id
  ↓
fetch_pr: Get PR metadata + config
  ↓
analyze_diff: Create chunks + risk scores
  ↓
build_rag_index: Fetch full files + index
  ↓
For each chunk:
  ├─ Query RAG for context
  ├─ Call LLM with diff + context
  ├─ Get ReviewOutput with findings
  └─ Validate findings
  ↓
merge_results: Combine + deduplicate
  ↓
post_results: GitHub comment + Slack
  ↓
END
```

---

This is your complete **code + flow** guide! Use this to understand any part of the system.
