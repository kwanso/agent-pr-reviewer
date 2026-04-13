#!/usr/bin/env python
"""Validate that the environment and dependencies are properly set up.

This script checks:
1. Python version
2. Required dependencies installed
3. Configuration loading
4. Optional dependencies (for development)
"""
from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


def check_python_version() -> bool:
    """Check if Python 3.11+ is installed."""
    print("🐍 Checking Python version...")
    version = sys.version_info
    required = (3, 11)

    if (version.major, version.minor) >= required:
        print(f"   ✅ Python {version.major}.{version.minor} (required: 3.11+)")
        return True
    else:
        print(f"   ❌ Python {version.major}.{version.minor} (required: 3.11+)")
        return False


def check_dependencies() -> bool:
    """Check if required dependencies are installed."""
    print("\n📦 Checking dependencies...")

    required = [
        "langgraph",
        "langchain",
        "fastapi",
        "pydantic",
        "structlog",
    ]

    all_ok = True
    for pkg in required:
        try:
            import_module(pkg)
            print(f"   ✅ {pkg}")
        except ImportError:
            print(f"   ❌ {pkg} (missing)")
            all_ok = False

    return all_ok


def check_config() -> bool:
    """Check if configuration can be loaded."""
    print("\n⚙️  Checking configuration...")

    try:
        from app.config import get_settings
        settings = get_settings()

        # Check key settings
        checks = [
            ("Port configured", settings.port > 0),
            ("Review profile valid", settings.review_profile in ["cost_safe", "balanced", "quality_heavy"]),
            ("Python requirements met", sys.version_info >= (3, 11)),
        ]

        all_ok = True
        for name, result in checks:
            status = "✅" if result else "⚠️ "
            print(f"   {status} {name}")
            if not result:
                all_ok = False

        return all_ok
    except Exception as e:
        print(f"   ❌ Config loading failed: {e}")
        return False


def check_optional_deps() -> None:
    """Check if optional dev dependencies are installed."""
    print("\n🛠️  Optional dependencies (for development)...")

    optional = {
        "pytest": "Testing framework",
        "pytest_asyncio": "Async test support",
        "respx": "HTTP mocking",
    }

    for pkg, desc in optional.items():
        try:
            import_module(pkg)
            print(f"   ✅ {pkg} - {desc}")
        except ImportError:
            print(f"   ⚠️  {pkg} - {desc} (optional, install with 'pip install -e .[dev]')")


def main() -> int:
    """Run all validation checks."""
    print("=" * 60)
    print("PR Review Agent - Setup Validation")
    print("=" * 60)

    checks = [
        ("Python Version", check_python_version),
        ("Dependencies", check_dependencies),
        ("Configuration", check_config),
    ]

    results = []
    for name, check_fn in checks:
        result = check_fn()
        results.append((name, result))

    check_optional_deps()

    print("\n" + "=" * 60)
    print("Summary:")
    print("=" * 60)

    all_passed = all(result for _, result in results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")

    print("=" * 60)

    if all_passed:
        print("\n🎉 Setup validation passed! You can now:")
        print("   1. Run tests: pytest tests/")
        print("   2. Run in mock mode: LLM_MOCK_MODE=true python -m app.server")
        print("   3. Setup GitHub App: https://github.com/kwanso-khalid/kwanso-agent-pr/blob/main/docs/SETUP.md")
        return 0
    else:
        print("\n⚠️  Some checks failed. Please fix the issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
