# Setup Guide

Complete step-by-step instructions for setting up the PR Review Agent in your environment.

## Table of Contents
1. [GitHub App Setup](#github-app-setup)
2. [Environment Configuration](#environment-configuration)
3. [Local Development](#local-development)
4. [Deployment](#deployment)
5. [Troubleshooting](#troubleshooting)

---

## GitHub App Setup

### Step 1: Create GitHub App

1. Go to https://github.com/settings/apps/new
2. Fill in the following:

   **App name:** `PR Review Agent` (or your preferred name)

   **Homepage URL:** `https://github.com/kwanso-khalid/kwanso-agent-pr`

   **Webhook URL:** `https://your-deployment-domain.com/webhook/github` (or localhost for testing)

   **Webhook secret:** Generate a random secret:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   (Copy this value to `.env` as `WEBHOOK_SECRET`)

### Step 2: Configure Permissions

Set the following permissions:

| Permission | Type | Access |
|-----------|------|--------|
| Pull requests | Read-only | Can read PR details |
| Contents | Read-only | Can read repository files |
| Commit statuses | Read & write | For marking reviews |

### Step 3: Configure Event Subscriptions

Subscribe to:
- ✅ Pull request

### Step 4: Get Credentials

1. **App ID:** Listed on the app settings page → Copy to `.env` as `GITHUB_APP_ID`
2. **Private Key:**
   - Click "Generate a private key"
   - Save as `app.pem` in repo root
   - Add path to `.env`: `GITHUB_APP_PRIVATE_KEY_PATH=./app.pem`

### Step 5: Install App

1. Go to "Install App" in sidebar
2. Select your repository or organization
3. Click "Install"
4. Note the installation ID (optional, not needed)

---

## Environment Configuration

### Create .env File

```bash
cp .env.example .env
```

### Required Variables

```bash
# GitHub
GITHUB_APP_ID=<your_app_id>
GITHUB_APP_PRIVATE_KEY_PATH=./app.pem
WEBHOOK_SECRET=<your_webhook_secret>

# Google AI / Gemini
LLM_API_KEY=<your_google_api_key>

# Server
PORT=3400
```

### Optional Variables

```bash
# Slack (for notifications)
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=#pr-reviews

# LLM Configuration
LLM_FLASH_MODEL=gemini-2.5-flash
REVIEW_PROFILE=cost_safe  # or: balanced, quality_heavy

# PR Limits
MAX_FILES=30
MAX_DIFF_SIZE=30000
```

See [.env.example](../.env.example) for all options with descriptions.

---

## Local Development

### Quick Start

```bash
# Clone repository
git clone https://github.com/kwanso-khalid/kwanso-agent-pr.git
cd kwanso-agent-pr

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install with dev dependencies
pip install -e ".[dev]"

# Copy and configure .env
cp .env.example .env
# Edit .env with your credentials

# Run server
python -m app.server
```

Server starts on `http://localhost:3400`

### Local Testing

Test without GitHub/LLM by enabling mock mode:

```bash
# .env
LLM_MOCK_MODE=true
WEBHOOK_SECRET=test-secret
GITHUB_APP_ID=12345
```

Then run:
```bash
pytest tests/
```

### Testing with Real GitHub

#### Option A: ngrok (Easiest)

1. **Install ngrok:** https://ngrok.com/download
2. **Start ngrok:**
   ```bash
   ngrok http 3400
   ```
   Note the forwarding URL (e.g., `https://abc123.ngrok.io`)

3. **Update GitHub App:**
   - Go to your App settings
   - Update Webhook URL to: `https://abc123.ngrok.io/webhook/github`
   - Save

4. **Open PR:** Create a test PR in your repository
   - Agent will receive webhook and post review

#### Option B: Expose locally (Advanced)

Use SSH tunneling or similar to expose your local server.

### Running Tests

```bash
# All tests
pytest tests/

# With coverage
pytest --cov=app tests/

# Specific test file
pytest tests/test_graph.py -v

# Watch mode (requires pytest-watch)
pip install pytest-watch
ptw tests/
```

---

## Deployment

### Docker

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install -e .

COPY app/ ./app/
COPY .env ./.env

CMD ["python", "-m", "app.server"]
```

Build and run:
```bash
docker build -t pr-review-agent .
docker run -p 3400:3400 --env-file .env pr-review-agent
```

### Using Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'
services:
  agent:
    build: .
    ports:
      - "3400:3400"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

Run:
```bash
docker-compose up -d
```

### Cloud Deployment

#### Google Cloud Run

```bash
# Deploy to Cloud Run
gcloud run deploy pr-review-agent \
  --source . \
  --platform managed \
  --region us-central1 \
  --set-env-vars GITHUB_APP_ID=123,LLM_API_KEY=xxx,WEBHOOK_SECRET=yyy
```

#### AWS Lambda (with API Gateway)

1. Package the application
2. Create Lambda function with FastAPI adapter (e.g., Mangum)
3. Set environment variables
4. Configure API Gateway for `/webhook` endpoint

#### Heroku

```bash
# Login and create app
heroku login
heroku create pr-review-agent

# Set environment variables
heroku config:set GITHUB_APP_ID=123
heroku config:set LLM_API_KEY=xxx
heroku config:set WEBHOOK_SECRET=yyy

# Deploy
git push heroku main
```

---

## Troubleshooting

### Webhook Not Receiving Events

**Symptom:** PR comments not appearing, no activity logs

**Solutions:**
1. Verify GitHub App webhook URL is correct and accessible
2. Check webhook delivery logs in GitHub App settings → Advanced → Deliveries
3. Ensure `WEBHOOK_SECRET` matches in app settings
4. Check server logs: `docker logs <container>` or console output

### "Invalid signature" Error

**Symptom:** 401 errors in webhook logs

**Solutions:**
1. Verify `WEBHOOK_SECRET` matches between GitHub and `.env`
2. Make sure secret is hexadecimal (run: `python -c "import secrets; print(secrets.token_hex(32))"`)
3. Regenerate secret if unsure:
   - Create new secret
   - Update GitHub App settings
   - Update `.env`

### API Rate Limiting

**Symptom:** Gemini API returning 429 errors

**Solutions:**
1. Switch to cheaper model: `LLM_FLASH_MODEL=gemini-2.5-flash-lite`
2. Increase chunk delay: `CHUNK_DELAY_MS=5000`
3. Use `cost_safe` review profile: `REVIEW_PROFILE=cost_safe`
4. Wait before submitting more PRs (rate limits reset hourly)

### LLM Connection Timeouts

**Symptom:** "Request timeout" or connection errors

**Solutions:**
1. Increase timeout: `LLM_TIMEOUT_S=45`
2. Increase retry count: `LLM_RETRY_COUNT=3`
3. Check your internet connection
4. Verify API key is valid: https://ai.google.dev

### Memory Issues

**Symptom:** Process crashes or "out of memory"

**Solutions:**
1. Reduce `MAX_DIFF_SIZE`: (e.g., 20000 instead of 30000)
2. Reduce `CHUNK_SIZE`: (e.g., 4000 instead of 5000)
3. Disable RAG: `ENABLE_RAG=false`
4. Use smaller review profile: `REVIEW_PROFILE=cost_safe`

### Database Locked

**Symptom:** SQLite "database is locked" error

**Solutions:**
1. Ensure only one server instance is running
2. Delete stale checkpoint: `rm -f data/checkpoints.db-wal`
3. Use distributed deployment with proper synchronization

### Finding Logs

**Location by deployment type:**
- **Local:** Console output
- **Docker:** `docker logs <container>`
- **Cloud Run:** Console or `gcloud run logs read <service>`
- **Lambda:** CloudWatch Logs

---

## Next Steps

1. Create a test repository
2. Install the GitHub App
3. Create a test PR
4. Verify review appears as a comment

For more information, see:
- [README.md](../README.md) — Project overview
- [ARCHITECTURE.md](./ARCHITECTURE.md) — System design
- [CONTRIBUTING.md](../CONTRIBUTING.md) — Development guidelines
