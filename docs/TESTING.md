# Testing Guide

This guide shows how to test the PR Review Agent locally without a full GitHub App setup.

## Quick Validation

After installation, validate your setup:

```bash
python scripts/validate_setup.py
```

This checks Python version, dependencies, and configuration.

---

## Unit Tests

Run the test suite (no GitHub setup needed):

```bash
# All tests
pytest tests/

# Specific test file
pytest tests/test_models.py -v

# With coverage report
pytest --cov=app tests/
```

Tests use mocking for external dependencies and run in seconds.

---

## Manual Server Testing

### 1. Start in Mock Mode

Test without external API calls:

```bash
# Generate a test webhook secret
export WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")

# Start server in mock mode
LLM_MOCK_MODE=true python -m app.server
```

Server runs on `http://localhost:3400`

Verify it's running:
```bash
curl http://localhost:3400/health
# Expected response: {"status":"ok","graph_ready":true}
```

### 2. Send Test Webhook

Create a file `test_webhook.json`:

```json
{
  "action": "opened",
  "pull_request": {
    "number": 123,
    "head": {
      "sha": "abc123def456"
    },
    "title": "Add feature X",
    "user": {
      "login": "testuser"
    }
  },
  "repository": {
    "name": "test-repo",
    "owner": {
      "login": "testorg"
    }
  },
  "installation": {
    "id": 12345
  }
}
```

Send it:
```bash
curl -X POST http://localhost:3400/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: sha256=test" \
  -d @test_webhook.json
```

Response: `{"status":"processing"}`

### 3. Check Logs

The server logs go to stdout:

```
2026-04-13 15:30:45 [INFO] review_started [delivery_id=...] [pr_number=123]
2026-04-13 15:30:46 [INFO] review_completed [delivery_id=...] [skipped=false]
```

---

## Integration Testing (Local GitHub)

### Prerequisites
- ngrok installed: https://ngrok.com/download
- Local GitHub App created (no real repo needed for testing)

### Steps

1. **Start ngrok** (in separate terminal):
   ```bash
   ngrok http 3400
   ```
   Note the forwarding URL: `https://abc123.ngrok.io`

2. **Update GitHub App webhook**:
   - Go to your GitHub App settings
   - Set Webhook URL: `https://abc123.ngrok.io/webhook/github`
   - Set Webhook secret: Use the same value in `.env` as `WEBHOOK_SECRET`

3. **Start the agent**:
   ```bash
   python -m app.server
   ```

4. **Create a test PR** in your repository:
   - The webhook will trigger automatically
   - Check PR for the review comment

---

## Docker Testing

Test with Docker Compose:

```bash
# Build and start in mock mode
docker-compose build
docker-compose up -d

# Check logs
docker-compose logs -f

# Send test webhook
curl -X POST http://localhost:3400/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -d @test_webhook.json

# Stop
docker-compose down
```

---

## Debugging

### Enable Debug Logging

Set environment variable:
```bash
LOGLEVEL=DEBUG python -m app.server
```

### Inspect State

Check SQLite checkpoint database:
```bash
sqlite3 data/checkpoints.db
> .tables
> SELECT * FROM checkpoint LIMIT 1;
```

### Test Individual Components

```python
# Test diff parsing
from app.utils.diff import analyze_diff
result = analyze_diff("your-diff-here", max_files=30, max_size=30000)

# Test models
from app.models import ReviewOutput, ReviewFinding
finding = ReviewFinding(issue="Test issue", why_it_matters="Why it matters")
review = ReviewOutput(critical_issues=[finding])
print(review.to_markdown())
```

---

## Common Issues

### "Module not found: app"

**Solution**: Install in development mode:
```bash
pip install -e .
```

### "LLM_MOCK_MODE not recognized"

**Solution**: Set it before running:
```bash
export LLM_MOCK_MODE=true
python -m app.server
```

### "Port 3400 already in use"

**Solution**: Use a different port:
```bash
PORT=3401 python -m app.server
```

### "Database is locked"

**Solution**: Only one server instance can access the database. Stop other instances:
```bash
pkill -f "python -m app.server"
```

---

## Performance Testing

For load testing:

```bash
# Install Apache Bench
# macOS
brew install httpd

# Ubuntu
sudo apt-get install apache2-utils

# Run 100 requests
ab -n 100 -c 10 http://localhost:3400/health
```

---

## Continuous Integration

Tests run automatically on GitHub Actions. See `.github/workflows/tests.yml` for the configuration.

To mimic CI locally:
```bash
# Run tests with coverage
pytest --cov=app tests/

# Check types
mypy app/

# Format check
black --check app/
```

---

## Next Steps

Once testing is complete:
1. Set up a real GitHub App (see [SETUP.md](./SETUP.md))
2. Deploy to your infrastructure
3. Monitor with observability tools
