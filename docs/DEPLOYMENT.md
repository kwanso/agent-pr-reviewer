# Production Deployment Guide

This document covers critical considerations for running the PR Review Agent in production environments.

## Multi-Instance Deployment

### Current Limitations

The agent uses **SQLite for checkpoint persistence** via `AsyncSqliteSaver`. This is suitable for **single-instance deployments only**.

**Why:** Each webhook delivery maps to a `thread_id` used as a checkpoint key. If multiple instances process the same webhook (due to retries or misconfiguration), concurrent writes to SQLite will corrupt state.

### Single-Instance Deployment (Current)

✅ **Recommended for production until multi-instance support is added**

Configuration:
```bash
# Use a single application instance
docker run -it pr-review-agent:latest  # Single container

# Or single process
python -m app.server
```

Ensure:
- Load balancer routes all webhooks to the same instance
- Use sticky sessions / affinity rules if behind a proxy
- Monitor database lock contention: `SELECT * FROM sqlite_master`

### Multi-Instance Deployment (Planned)

For horizontal scaling, you **must** migrate from SQLite to PostgreSQL:

#### Migration Steps (Future Work)

1. Install PostgreSQL checkpointer:
   ```bash
   pip install langgraph-checkpoint-postgres
   ```

2. Update `app/server.py`:
   ```python
   from langgraph.checkpoint.postgres.aio import AsyncPostgresCheckpointer

   @asynccontextmanager
   async def lifespan(_app: FastAPI):
       async with AsyncPostgresCheckpointer.from_conn_string(
           f"postgresql+asyncpg://{db_user}:{db_pass}@{db_host}/{db_name}"
       ) as checkpointer:
           builder = build_graph()
           _compiled_graph = builder.compile(checkpointer=checkpointer)
           yield
   ```

3. Set environment variables:
   ```bash
   DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname
   ```

4. Create PostgreSQL:
   ```bash
   createdb pr_review_agent_checkpoints
   # Checkpointer creates tables automatically
   ```

---

## State & Idempotency

### Webhook Retries

GitHub retries failed webhooks using exponential backoff. The agent uses `x-github-delivery` header as the unique checkpoint ID.

**Guarantee:** If the same webhook is delivered twice with the same `delivery_id`:
- ✅ The graph is resumed from the last checkpoint
- ✅ Duplicate reviews are not posted (idempotent)
- ✅ Partial results are preserved

**How it works:**
1. First delivery → Executes graph steps → Checkpoints after each node
2. Webhook timeout → GitHub retries with same `delivery_id`
3. Graph resumes from last checkpoint → Skips completed nodes → Continues

---

## Resource Management

### Memory Usage

RAG indices are stored in-memory (FAISS + BM25). Each PR review creates indices sized ~file_count × rag_chunk_size.

**Automatic cleanup:**
```python
# In app/services/rag.py
cleanup_expired_indices()  # Removes indices older than 24 hours

# Manual cleanup
rag.cleanup(delivery_id)  # Called in publish.py after review completes
```

**Monitoring:**
```python
# Check in-memory indices
from app.services import rag
print(f"Active indices: {len(rag._stores)}")  # Should be low if cleanup runs
```

### Token Budget

Configure per-review token limits to prevent billing surprises:

```bash
# Default: 100,000 tokens per review
TOKEN_BUDGET_PER_REVIEW=100000

# Stop processing at 90% threshold
TOKEN_BUDGET_THRESHOLD_PCT=0.9
```

Behavior:
- Tracks cumulative tokens across all chunks
- Skips remaining chunks if threshold exceeded
- Logs token usage per chunk
- Returns partial review with available findings

---

## Error Handling & Observability

### Error Classification

All LLM errors are classified into types:
- `quota_exhausted` — API quota hit, retriable
- `service_unavailable` — Temporary outage, retriable
- `timeout` — Request timeout, retriable
- `auth_failed` — Credentials invalid, non-retriable
- `invalid_request` — Bad input, non-retriable
- `unknown` — Unclassified, non-retriable

See `app/errors.py` for details.

### Logging

All critical operations log structured data:

```python
log.info(
    "reviewing_chunk",
    index=0,
    total=5,
    risk=4,
    mode="deep",
    token_budget_pct=45,  # Token usage
)

log.error(
    "chunk_review_failed",
    index=0,
    chunk_size=4200,
    chunk_files=["app/models.py"],
    attempts_made=4,
    error_type="service_unavailable",
    error_message="Service temporarily unavailable; should retry",
)
```

**Required fields in logs:**
- `delivery_id` — Webhook trace ID
- `index` — Chunk number
- `error_type` — Classified error (not string matching)
- `timestamp` — UTC timestamp (automatic via structlog)

### Monitoring Dashboard

Set up alerts for:

| Metric | Threshold | Action |
|--------|-----------|--------|
| `quota_exhausted` errors | >5/hour | Page oncall |
| Average review latency | >120s | Investigate LLM API |
| `token_budget_exhausted` | >10%/day | Increase budget or optimize prompts |
| `rag_index_build_failed` | >5/day | Check GitHub API quota |
| Active RAG indices | >100 | Cleanup not running |

---

## Security

### API Keys & Secrets

**Never commit to version control:**
```bash
# .gitignore
.env
.env.local
app/github_app_key.pem
data/checkpoints.db
```

**Store securely:**
- GitHub App private key → AWS Secrets Manager / HashiCorp Vault
- LLM API key → Environment variable or secrets manager
- Webhook secret → Environment variable
- Database credentials → Secrets manager

### Repo Config Injection

Repository `.pr-review.yml` is sanitized before use in prompts (see `app/nodes/fetch.py`):
- Only scalar string values allowed
- Max 500 characters per field
- Control characters removed
- Only whitelisted keys processed

---

## Performance Tuning

### Chunk Size

Larger chunks = fewer LLM calls but slower analysis:

```bash
# Default: 5000 characters
CHUNK_SIZE=5000

# For large PRs: increase
CHUNK_SIZE=8000  # Faster, fewer calls

# For detailed reviews: decrease
CHUNK_SIZE=3000  # Slower, more thorough
```

### Review Mode

Risk-scored chunks use different review depths:
- `light` mode (low-risk) — Confidence ≥ 0.8, faster
- `deep` mode (high-risk) — Confidence ≥ 0.6, thorough

Override per-repo:
```yaml
# .pr-review.yml in repository
review_depth: "deep"  # Always use deep review
```

### Rate Limiting

Inter-chunk delay prevents API quota exhaustion:

```bash
# Default: 2000ms = 2 seconds between chunks
CHUNK_DELAY_MS=2000

# If rate-limited: increase
CHUNK_DELAY_MS=5000

# If quota available: decrease
CHUNK_DELAY_MS=1000
```

---

## Troubleshooting

### Reviews Not Posting to GitHub

**Symptom:** `log.warning("inline_comments_failed")`

**Causes:**
1. GitHub API quota exhausted
2. PR branch deleted (can't post inline comments)
3. Invalid `installation_id` in webhook

**Fix:**
```bash
# Check GitHub API rate limits
curl -H "Authorization: token YOUR_TOKEN" https://api.github.com/rate_limit

# Verify webhook secret is set
echo $WEBHOOK_SECRET
```

### High Token Usage

**Symptom:** `token_budget_exhausted` on many PRs

**Causes:**
1. Context too large (long PR titles/bodies)
2. RAG retrieving too much context
3. Low confidence threshold causing retries

**Fix:**
```bash
# Increase budget
TOKEN_BUDGET_PER_REVIEW=200000

# Or reduce context
COMPACT_DIFF=true        # Strip context lines
COMPACT_CONTEXT_LINES=0  # No context in diffs
RAG_TOP_K=3              # Fewer RAG results
```

### Memory Growing Indefinitely

**Symptom:** Process memory grows; `active_indices` counter high

**Causes:**
1. RAG cleanup not running
2. Exception in cleanup code

**Fix:**
```python
# Force manual cleanup
from app.services import rag
rag.cleanup_expired_indices()  # Called every 24 hours by default
```

---

## Compliance & Audit

### Data Retention

The agent does **not** persist PR diffs or findings to disk after posting:
- Checkpoints are overwritten after review completes
- RAG indices are freed immediately
- No logs include sensitive diff content

### Audit Trail

All actions are logged with:
- Request ID (`delivery_id`)
- Actor (GitHub App installation)
- Action (review chunk, post result)
- Timestamp
- Status (success/failure/retry)

Access logs via:
```bash
# Structured logs to stdout
docker logs pr-review-agent-container | grep review_completed

# Example:
# delivery_id=abc-123 owner=myorg repo=myrepo pr=42 skipped=false chunks=3
```

---

## Maintenance

### Update Checks

Monitor for:
- New LangGraph checkpoint API (may require code changes)
- GitHub API deprecations (breaking webhook format)
- Google Gemini API changes (model deprecation)

### Backup & Recovery

**Checkpoint database:**
```bash
# SQLite backup
cp data/checkpoints.db data/checkpoints.db.backup.$(date +%s)

# PostgreSQL backup (if multi-instance)
pg_dump pr_review_agent_checkpoints > backup.sql
```

**Recovery:** Restore the backup file. In-progress reviews will resume on next webhook retry.

---

## Support

For issues:
1. Check logs with `delivery_id` from webhook
2. Verify API credentials and quotas
3. Run `python scripts/validate_setup.py` to test configuration
4. Check this guide for troubleshooting steps
