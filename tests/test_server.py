"""Tests for app.server — webhook handling and signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.server import _verify_signature, app


@pytest.fixture(autouse=True)
def _clear_webhook_secret():
    """Ensure WEBHOOK_SECRET is empty for all tests unless explicitly set."""
    original = os.environ.get("WEBHOOK_SECRET")
    os.environ["WEBHOOK_SECRET"] = ""
    get_settings.cache_clear()
    yield
    if original is None:
        os.environ.pop("WEBHOOK_SECRET", None)
    else:
        os.environ["WEBHOOK_SECRET"] = original
    get_settings.cache_clear()


# ── Signature verification ───────────────────────────────────────────


class TestVerifySignature:
    def test_valid_signature(self):
        secret = "test-secret"
        payload = b'{"action": "opened"}'
        sig = (
            "sha256="
            + hmac.new(
                secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()
        )
        assert _verify_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        secret = "test-secret"
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "sha256=bad", secret) is False

    def test_empty_secret_allows_all(self):
        payload = b'{"action": "opened"}'
        assert _verify_signature(payload, "", "") is True
        assert _verify_signature(payload, "anything", "") is True


# ── Health endpoint ──────────────────────────────────────────────────


class TestHealthEndpoint:
    def test_health_returns_ok(self):
        """Health endpoint works even without full lifespan (graph_ready=False)."""
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


# ── Webhook endpoint ─────────────────────────────────────────────────


class TestWebhookEndpoint:
    def _make_pr_payload(
        self,
        action: str = "opened",
        owner: str = "testowner",
        repo: str = "testrepo",
        pr_number: int = 42,
    ) -> dict:
        return {
            "action": action,
            "pull_request": {
                "number": pr_number,
                "head": {"sha": "abc123"},
            },
            "repository": {
                "name": repo,
                "owner": {"login": owner},
            },
        }

    def test_ignores_non_pr_events(self):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/webhook",
            content=b'{"action": "created"}',
            headers={
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_ignores_unhandled_pr_actions(self):
        client = TestClient(app, raise_server_exceptions=False)
        payload = self._make_pr_payload(action="closed")
        resp = client.post(
            "/webhook",
            content=json.dumps(payload).encode(),
            headers={
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_rejects_invalid_signature(self):
        """When WEBHOOK_SECRET is set, invalid signatures should be rejected."""
        os.environ["WEBHOOK_SECRET"] = "real-secret"
        get_settings.cache_clear()

        client = TestClient(app, raise_server_exceptions=False)
        payload = self._make_pr_payload()
        resp = client.post(
            "/webhook",
            content=json.dumps(payload).encode(),
            headers={
                "x-github-event": "pull_request",
                "x-hub-signature-256": "sha256=invalid",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 401
