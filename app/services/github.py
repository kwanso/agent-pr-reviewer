"""Async GitHub API client using httpx.

Supports both GitHub Apps (with installation tokens) and Personal Access Tokens (PAT).
GitHub App auth is auto-generated per installation_id from webhook payload.
Falls back to PAT if no app is configured.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx
import jwt as pyjwt
import structlog
import yaml

from app.config import get_settings

log = structlog.get_logger()


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _classify_status(status: int) -> str:
    match status:
        case 401:
            return "auth_invalid"
        case 403:
            return "permission_denied"
        case 404:
            return "not_found"
        case 429:
            return "rate_limited"
        case s if s >= 500:
            return "server_error"
        case _:
            return "unknown"


class GitHubAppTokenManager:
    """Manages GitHub App JWT creation and installation token caching."""

    def __init__(self, app_id: int, private_key_path: str = "", private_key: str = ""):
        self.app_id = app_id
        self.private_key = private_key or self._load_key(private_key_path)
        # Cache: installation_id -> (token, expires_at)
        self._token_cache: dict[int, tuple[str, float]] = {}

    def _load_key(self, path: str) -> str:
        if not path:
            return ""
        try:
            # If path is relative, resolve it relative to the app directory
            import pathlib

            p = pathlib.Path(path)
            if not p.is_absolute():
                # Get the directory where this file is located (app/)
                app_dir = pathlib.Path(__file__).parent.parent
                p = app_dir / path
            with open(p, "r") as f:
                return f.read()
        except Exception as exc:
            log.error("failed_to_load_app_private_key", path=path, error=str(exc))
            return ""

    def _create_jwt(self) -> str:
        """Create a 10-minute JWT for GitHub App authentication."""
        now = int(time.time())
        payload = {
            "iss": str(self.app_id),
            "iat": now,
            "exp": now + 600,  # 10 minutes
        }
        return pyjwt.encode(payload, self.private_key, algorithm="RS256")

    async def get_installation_token(
        self,
        installation_id: int,
        http: httpx.AsyncClient,
    ) -> str:
        """Get or refresh installation access token. Caches with 5-min buffer."""
        cached_token, expires_at = self._token_cache.get(
            installation_id,
            ("", 0),
        )
        now = time.time()
        # Refresh if within 5 min of expiry
        if cached_token and now < expires_at - 300:
            return cached_token

        jwt = self._create_jwt()
        try:
            resp = await http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt}"},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token", "")
            # GitHub returns expires_at as ISO 8601 string; parse it
            expires_str = data.get("expires_at", "")
            try:
                import datetime

                expires_dt = datetime.datetime.fromisoformat(
                    expires_str.replace("Z", "+00:00"),
                )
                expires_at = expires_dt.timestamp()
            except Exception:
                # Fallback: assume 1 hour from now
                expires_at = now + 3600

            self._token_cache[installation_id] = (token, expires_at)
            log.info(
                "installation_token_obtained",
                installation_id=installation_id,
                expires_in_s=int(expires_at - now),
            )
            return token
        except httpx.HTTPError as exc:
            log.error(
                "failed_to_obtain_installation_token",
                installation_id=installation_id,
                error=str(exc),
            )
            raise GitHubError(f"Failed to obtain installation token: {exc}") from exc


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str, timeout: float = 20.0):
        headers = {
            "User-Agent": "pr-review-agent-py",
            "Accept": "application/vnd.github.v3+json",
        }
        if token:
            headers["Authorization"] = f"token {token}"

        self._http = httpx.AsyncClient(
            base_url=self.BASE,
            timeout=timeout,
            headers=headers,
        )

    async def _request(
        self,
        method: str,
        path: str,
        retries: int = 3,
        **kwargs: Any,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = await self._http.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_exc = GitHubError(
                    str(exc),
                    status=status,
                    reason=_classify_status(status),
                )
                if (status == 429 or status >= 500) and attempt < retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise last_exc from exc
            except httpx.RequestError as exc:
                last_exc = GitHubError(str(exc))
                if attempt < retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise last_exc from exc
        raise last_exc or GitHubError("max retries exceeded")

    # ── PR operations ────────────────────────────────────────────────

    async def get_pr_details(self, owner: str, repo: str, pr_number: int) -> dict:
        data = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return {
            "title": data.get("title", ""),
            "body": data.get("body") or "",
            "html_url": data.get("html_url", ""),
            "draft": data.get("draft", False),
            "state": data.get("state", ""),
            "labels": [lbl["name"] for lbl in data.get("labels", [])],
            "changed_files": data.get("changed_files", 0),
        }

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        all_files: list[dict] = []
        page = 1
        while True:
            batch = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )
            all_files.extend(
                {"filename": f["filename"], "patch": f.get("patch", "")} for f in batch
            )
            if len(batch) < 100:
                break
            page += 1
        return all_files

    # ── File content ─────────────────────────────────────────────────

    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str,
    ) -> str | None:
        try:
            data = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
            if data.get("encoding") == "base64" and data.get("content"):
                return base64.b64decode(data["content"]).decode("utf-8")
            return data.get("content")
        except GitHubError as exc:
            log.warning("file_content_failed", path=path, status=exc.status)
            return None

    async def get_repo_config(
        self,
        owner: str,
        repo: str,
        ref: str,
        path: str = ".pr-review.yml",
    ) -> dict | None:
        content = await self.get_file_content(owner, repo, path, ref)
        if not content:
            return None
        try:
            return yaml.safe_load(content)
        except yaml.YAMLError:
            log.warning("repo_config_parse_failed", path=path)
            return None

    # ── Posting ──────────────────────────────────────────────────────

    async def post_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        if not body.strip():
            return
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    async def post_inline_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_id: str,
        comments: list[dict],
    ) -> None:
        normalized = [
            {
                "path": c["file"],
                "line": c["line"],
                "side": "RIGHT",
                "body": c["comment"],
            }
            for c in comments
            if c.get("file") and isinstance(c.get("line"), int) and c.get("comment")
        ][:10]
        if not normalized or not commit_id:
            return
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json={
                "commit_id": commit_id,
                "event": "COMMENT",
                "comments": normalized,
            },
        )

    async def close(self) -> None:
        await self._http.aclose()


# ── Module-level state ──────────────────────────────────────────────

_app_auth: GitHubAppTokenManager | None = None
_app_clients: dict[int, GitHubClient] = {}  # per installation_id
_pat_client: GitHubClient | None = None  # PAT fallback


async def get_github_client(installation_id: int | None = None) -> GitHubClient:
    """Factory for GitHubClient.

    If GitHub App is configured and installation_id is provided:
      - Get or refresh installation access token (cached, 1h lifetime)
      - Return a per-installation client (cached per installation_id)

    Otherwise, fall back to PAT singleton.
    GitHub may send multiple webhook deliveries; some may lack installation_id.
    """
    global _app_auth, _app_clients, _pat_client

    settings = get_settings()

    # GitHub App mode: enabled + installation_id provided
    if settings.github_app_id > 0 and installation_id:
        if _app_auth is None:
            _app_auth = GitHubAppTokenManager(
                settings.github_app_id,
                settings.github_app_private_key_path,
                settings.github_app_private_key,
            )

        # Check if we have a cached client for this installation
        if installation_id not in _app_clients:
            # Need to get the installation token
            async with httpx.AsyncClient(
                base_url=GitHubClient.BASE,
                timeout=20.0,
            ) as http:
                token = await _app_auth.get_installation_token(
                    installation_id,
                    http,
                )
            _app_clients[installation_id] = GitHubClient(token)
            log.info(
                "github_client_created", mode="app", installation_id=installation_id
            )

        return _app_clients[installation_id]

    # PAT fallback (no app configured or no installation_id)
    if _pat_client is None:
        if not settings.github_token:
            log.warning("github_no_credentials_configured")
        _pat_client = GitHubClient(settings.github_token)
        log.info("github_client_created", mode="pat")

    return _pat_client
