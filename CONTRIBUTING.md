# Contributing to PR Review Agent

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing code, documentation, and bug reports.

## 🙋 Getting Help

- **Questions?** Check [Discussions](https://github.com/kwanso-khalid/kwanso-agent-pr/discussions)
- **Found a bug?** Open an [Issue](https://github.com/kwanso-khalid/kwanso-agent-pr/issues)
- **Security issue?** Email [khalid.rasool@kwanso.com](mailto:khalid.rasool@kwanso.com) instead of opening a public issue

## 🚀 Development Setup

### Prerequisites
- Python 3.11 or higher
- Git
- Familiarity with async Python (LangGraph uses async/await)

### Quick Start

1. **Fork and clone**
   ```bash
   git clone https://github.com/YOUR-USERNAME/pr-review-agent.git
   cd pr-review-agent
   ```

2. **Create a branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Set up environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

4. **Verify setup**
   ```bash
   pytest tests/
   ```

## 💻 Code Standards

### Style & Formatting
- **Format:** [Black](https://black.readthedocs.io/) (auto-format with `black app/`)
- **Linting:** Follow [PEP 8](https://pep8.org/)
- **Imports:** Organized with `isort` (optional)
- **Type hints:** Required for public functions and module-level variables

### Example:
```python
from __future__ import annotations

from typing import Optional

def review_chunk(diff: str, context: Optional[str] = None) -> ReviewOutput:
    """Analyze a diff chunk using LLM.

    Args:
        diff: Unified diff format
        context: Optional RAG context from repository

    Returns:
        Structured review findings
    """
    # Implementation
```

### Type Checking
```bash
pip install mypy
mypy app/
```

### Docstrings
- Use Google-style docstrings
- Include Args, Returns, Raises sections for complex functions
- Keep module-level docstrings concise

## ✍️ Writing Tests

### Guidelines
- **Coverage:** Aim for >80% on new code
- **Location:** `tests/` mirrors `app/` structure
- **Naming:** `test_<function>` or `test<ClassName>.<method>`
- **Isolation:** Each test should be independent
- **Mocking:** Use `respx` for HTTP mocking, avoid database mocks

### Example:
```python
# tests/test_graph.py
import pytest
from app.graph import build_graph
from app.state import PRReviewState

@pytest.mark.asyncio
async def test_graph_skips_config_only_changes():
    graph = build_graph()
    compiled = graph.compile()

    state = PRReviewState(
        owner="user",
        repo="repo",
        pr_number=1,
        files=[{"path": "package.json", "patch": "..."}],
        skip_config_only=True,
    )

    result = await compiled.ainvoke(state)
    assert result.get("skipped") is True
    assert result.get("skip_reason") == "config_only"
```

### Running Tests
```bash
# All tests
pytest tests/

# Specific file
pytest tests/test_graph.py

# Specific test
pytest tests/test_graph.py::test_graph_skips_config_only_changes

# With coverage
pytest --cov=app tests/

# Watch mode (requires pytest-watch)
ptw tests/
```

## 📝 Making Changes

### Commit Messages
Use clear, descriptive messages:
- ✅ `feat: add RAG filtering by confidence score`
- ✅ `fix: handle malformed JSON in LLM responses`
- ✅ `docs: clarify database setup in README`
- ❌ `update stuff`
- ❌ `fix`

### What to Include
- **Code changes** — Implementation of feature/fix
- **Tests** — New tests for new behavior
- **Docs** — Updates to README, docstrings, examples
- **Changelog** — Brief summary of what changed (optional)

### What NOT to Include
- Secrets or API keys
- Generated files (`build/`, `dist/`, `*.egg-info/`)
- IDE settings (`.vscode/`, `.idea/`)
- Internal notes or debugging code

## 🔄 Submitting Changes

### Before Submitting

1. **Run tests**
   ```bash
   pytest tests/
   ```

2. **Type check**
   ```bash
   mypy app/
   ```

3. **Format code**
   ```bash
   black app/
   ```

4. **Update documentation** if behavior changed

### Opening a Pull Request

1. Push your branch to your fork
2. Open PR against `main` branch
3. Fill in the PR template:
   ```markdown
   ## Description
   Brief summary of changes

   ## Testing
   How to test these changes

   ## Checklist
   - [ ] Tests added/updated
   - [ ] Docs updated
   - [ ] No breaking changes
   ```

### PR Review Process
- At least one maintainer review required
- All tests must pass
- Code coverage should not decrease
- Constructive feedback welcome!

## 🏗️ Project Structure

```
pr-review-agent/
├── app/
│   ├── nodes/          # LangGraph nodes (fetch, analyze, review, etc.)
│   ├── services/       # External integrations (GitHub, Slack, RAG)
│   ├── utils/          # Utilities (diff parsing, metrics, prompts)
│   ├── config.py       # Settings management
│   ├── models.py       # Data models (ReviewOutput, ReviewFinding)
│   ├── state.py        # LangGraph state definition
│   ├── graph.py        # Graph topology & routing
│   └── server.py       # FastAPI application
├── tests/              # Test files (mirrors app/ structure)
├── docs/               # Documentation
├── README.md           # Project overview
├── CONTRIBUTING.md     # This file
├── .env.example        # Configuration template
└── pyproject.toml      # Project metadata & dependencies
```

## 🎯 Areas for Contribution

### High Priority
- [ ] Additional LLM providers (Claude, OpenAI, etc.)
- [ ] More comprehensive test fixtures
- [ ] Performance optimizations
- [ ] Documentation improvements

### Medium Priority
- [ ] Additional version control systems (GitLab, Gitea)
- [ ] Database persistence layers
- [ ] Caching strategies
- [ ] Metrics & monitoring

### Low Priority
- [ ] Web UI for configuration
- [ ] Additional notification channels (Teams, Discord)
- [ ] Historical review tracking

## ⚠️ Important Notes

### Backwards Compatibility
- Breaking changes require discussion in an issue first
- Deprecation warnings for removed features
- Version bumps follow [Semantic Versioning](https://semver.org/)

### License
- All contributions are licensed under the Kwanso License
- By submitting a PR, you agree to license your contribution

### Code of Conduct
- Be respectful and inclusive
- No harassment, discrimination, or hostility
- Assume good intent in discussions

## 🚨 Security

If you discover a security vulnerability:
1. **DO NOT** open a public issue
2. Email [security@kwanso.com](mailto:security@kwanso.com) with:
   - Description of vulnerability
   - Steps to reproduce
   - Potential impact
3. Allow 48 hours for response

We appreciate responsible disclosure!

## 📚 Useful Resources

- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [FastAPI Guide](https://fastapi.tiangolo.com/)
- [Pydantic Tutorial](https://docs.pydantic.dev/)
- [Python Type Hints](https://docs.python.org/3/library/typing.html)
- [Google Style Guide](https://google.github.io/styleguide/pyguide.html)

## ✨ Thank You!

Your contributions help make this project better for everyone. Thank you for investing your time! 🙏

---

**Questions?** Open a [Discussion](https://github.com/kwanso-khalid/kwanso-agent-pr/discussions) or reach out on [Slack](https://kwanso.slack.com).
