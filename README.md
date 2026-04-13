# PR Review Agent

> **AI-powered GitHub pull request reviewer using LangGraph and Retrieval-Augmented Generation (RAG)**

An intelligent code review agent that automatically analyzes GitHub PRs, identifies critical issues, and provides production-grade feedback directly on pull requests. Built with LangGraph for orchestration, Google Gemini for analysis, and FAISS for semantic code search.

![Tests](https://github.com/kwanso-khalid/kwanso-agent-pr/workflows/Tests/badge.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## 🎯 Features

- **🤖 Automated Code Review** — Analyzes diffs for critical issues, reliability concerns, database safety, resource management, and more
- **📚 RAG-Enhanced Context** — Retrieves relevant code from your repository for better understanding of changes
- **🎚️ Configurable Profiles** — Choose between cost-optimized, balanced, or quality-heavy review modes
- **⚡ Efficient Chunking** — Processes large PRs by breaking them into manageable chunks with rate limiting
- **🔴 9-Dimension Review Framework** — Structured feedback across critical issues, reliability, databases, resources, code quality, validation, performance, architecture, and production readiness
- **💬 Slack Integration** — Post review summaries to Slack for team awareness
- **🔄 Graceful Degradation** — Continues review even if LLM calls fail; merges partial results
- **⏱️ Crash Recovery** — SQLite checkpointing preserves progress across restarts

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- GitHub account with a repository
- Google AI API key (free tier available at [ai.google.dev](https://ai.google.dev))
- Optional: Slack workspace for notifications

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/kwanso-khalid/kwanso-agent-pr.git
   cd kwanso-agent-pr
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -e .
   ```

4. **Install development dependencies** (optional, for testing)
   ```bash
   pip install -e ".[dev]"
   ```

### Verify Installation

Before setting up GitHub, verify everything works:

```bash
# Check Python, dependencies, and configuration
python scripts/validate_setup.py

# Run tests (no GitHub setup needed)
pytest tests/

# Test the server in mock mode (no API calls)
LLM_MOCK_MODE=true python -m app.server
```

Once the server starts, test the health endpoint in another terminal:
```bash
curl http://localhost:3400/health
# Response: {"status":"ok","graph_ready":true}
```

For more testing options, see [docs/TESTING.md](docs/TESTING.md).

### Setup

1. **Set up GitHub App**
   - Go to https://github.com/settings/apps/new
   - Fill in app details (see [GITHUB_APP_SETUP.md](docs/SETUP.md#github-app-setup))
   - Download private key as `app.pem`

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   - Add your GitHub App ID to `GITHUB_APP_ID`
   - Add your Google AI API key to `LLM_API_KEY`
   - Add GitHub webhook secret to `WEBHOOK_SECRET`

3. **Run the server**
   ```bash
   python -m app.server
   ```
   - Server starts on `http://localhost:3400`
   - Health check: `curl http://localhost:3400/health`

4. **Set up GitHub webhook**
   - Go to your GitHub App settings → Webhook
   - Set Payload URL: `https://your-deployment-domain.com/webhook/github` (replace with your actual domain)
   - Set Secret: (the value in `WEBHOOK_SECRET`)
   - Subscribe to `pull_request` events

## 📖 Documentation

| Document | Purpose |
|----------|---------|
| [docs/SETUP.md](docs/SETUP.md) | Step-by-step setup guide for GitHub App and configuration |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, LangGraph topology, and data flow |
| [docs/API.md](docs/API.md) | API endpoints, webhook format, and integration |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute, development setup, and code standards |

## 🔧 Configuration

All settings are managed through environment variables in `.env`. Key options:

### Review Profiles
- **`cost_safe`** — Minimal API usage, fastest (~2-3 min for large PRs)
- **`balanced`** — Standard detail level (~3-5 min)
- **`quality_heavy`** — Most thorough analysis (~5-10 min)

### PR Limits
- `MAX_FILES` — Skip PRs exceeding this file count (default: 30)
- `MAX_DIFF_SIZE` — Skip PRs exceeding this byte size (default: 30000)
- `CHUNK_SIZE` — Size of diff chunks sent to LLM (default: 5000)

### RAG Settings
- `ENABLE_RAG` — Enable repository code retrieval (default: true)
- `RAG_TOP_K` — Number of code examples to include (default: 5)

See [.env.example](.env.example) for all available options.

## 🧪 Testing

### Quick Test
```bash
pytest tests/
```

### With Coverage
```bash
pytest --cov=app tests/
```

### Detailed Testing Guide
See [docs/TESTING.md](docs/TESTING.md) for:
- Validation script
- Mock mode testing
- Manual webhook testing
- Docker testing
- Debugging techniques

## 📋 Review Dimensions

The agent evaluates code across nine dimensions:

1. **🔴 Critical Issues** — Security breaches, data loss, system crashes (must-fix)
2. **🟠 Reliability** — Error handling, fault tolerance, graceful degradation
3. **🟣 Database** — SQL safety, transactions, connection lifecycle
4. **🔵 Resources** — File/DB/memory leaks, lifecycle management
5. **🟡 Code Quality** — Python idioms, typing, structure, duplication
6. **🟤 Input Validation** — Type safety, bounds checking, data integrity
7. **⚫ Performance** — N+1 queries, hot paths, wasteful work
8. **🔷 Architecture** — Coupling, boundaries, testability, extensibility
9. **⚪ Production Readiness** — Logging, observability, testing, config

Each finding includes:
- **Impact** — Why it matters in production
- **Evidence** — Code excerpt from the diff
- **Fix** — Concrete remediation steps
- **Confidence** — 0.0–1.0 (low-confidence findings are filtered)

## 🏗️ Architecture

```
START
  ↓
fetch_pr (get PR details)
  ↓
analyze_diff (validate & chunk)
  ↓
build_rag_index (embed code for context)
  ↓
review_chunk (LLM analyzes each chunk)
  ↓
merge_results (combine findings)
  ↓
validate_findings (dedup, filter low-confidence)
  ↓
post_results (comment on PR + Slack)
  ↓
END
```

For detailed architecture, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 🔌 Integration

### GitHub
- Receives webhooks for `pull_request` events (opened, synchronize, reopened)
- Posts review as a comment on the PR
- Automatically deletes old reviews before posting new ones

### Slack (Optional)
- Posts review summary after commenting on PR
- Includes PR title, author, and key findings

### LLM
- Uses Google Gemini 2.5 Flash by default
- Supports mock mode for testing without API calls
- Implements retry logic with exponential backoff

## 🚨 Error Handling

The agent gracefully degrades:
- **LLM timeouts** → Continues with partial results
- **Quota exhausted** → Stops immediately, posts what it has
- **Network errors** → Retries with backoff
- **Malformed responses** → Falls back to text parsing

## 📊 Performance

On typical PRs:
| Profile | Chunks | Time | Cost |
|---------|--------|------|------|
| cost_safe | 2-3 | 2-3 min | ~$0.01-0.02 |
| balanced | 3-5 | 4-6 min | ~$0.03-0.05 |
| quality_heavy | 5+ | 6-10 min | ~$0.05-0.10 |

See [docs/CAPACITY_ANALYSIS.md](docs/CAPACITY_ANALYSIS.md) for detailed benchmarks.

## 🤝 Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Development setup
- Code style and testing requirements
- How to submit PRs

## 📜 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## 🆘 Support

- **Bug reports:** Open an [issue](https://github.com/kwanso-khalid/kwanso-agent-pr/issues)
- **Feature requests:** [Discussions](https://github.com/kwanso-khalid/kwanso-agent-pr/discussions)
- **Security concerns:** Email [security@kwanso.com](mailto:security@kwanso.com)

## 🙏 Acknowledgments

Built with:
- [LangGraph](https://github.com/langchain-ai/langgraph) — Agent orchestration
- [FastAPI](https://fastapi.tiangolo.com/) — Web framework
- [Google Gemini](https://ai.google.dev) — LLM
- [FAISS](https://github.com/facebookresearch/faiss) — Vector search
- [Pydantic](https://docs.pydantic.dev) — Data validation

---

**Made with ❤️ by Kwanso**
# kwanso-agent-pr
