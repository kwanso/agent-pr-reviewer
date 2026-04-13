"""FastAPI application — webhook receiver, health endpoint, graph runner."""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import get_settings
from app.graph import build_graph

log = structlog.get_logger()

_compiled_graph: Any = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: compile graph with SQLite checkpointer.  Shutdown: close clients."""
    global _compiled_graph
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings = get_settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(settings.db_path) as checkpointer:
        builder = build_graph()
        _compiled_graph = builder.compile(checkpointer=checkpointer)

        log.info("server_started", port=settings.port, profile=settings.review_profile)
        yield

        from app.services.github import _client as gh

        if gh is not None:
            await gh.close()


app = FastAPI(title="PR Review Agent", lifespan=lifespan)


# ── Webhook signature verification ───────────────────────────────────


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True
    expected = (
        "sha256="
        + hmac.new(
            secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


# ── Background graph runner ──────────────────────────────────────────


async def _run_review(input_state: dict) -> None:
    delivery = input_state["delivery_id"]
    try:
        result = await _compiled_graph.ainvoke(
            input_state,
            config={"configurable": {"thread_id": delivery}},
        )
        log.info(
            "review_completed",
            delivery_id=delivery,
            skipped=result.get("skipped", False),
            skip_reason=result.get("skip_reason", ""),
            error=result.get("error", ""),
        )
    except Exception as exc:
        log.error("review_failed", delivery_id=delivery, error=str(exc))


# ── Routes ───────────────────────────────────────────────────────────


@app.post("/webhook")
@app.post("/webhook/github")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    settings = get_settings()
    body = await request.body()

    sig = request.headers.get("x-hub-signature-256", "")
    if not _verify_signature(body, sig, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("x-github-event", "")
    if event != "pull_request":
        return {"status": "ignored", "event": event}

    payload = await request.json()
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": action}

    pr = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})
    owner = repo_data.get("owner", {}).get("login", "")
    repo_name = repo_data.get("name", "")
    pr_number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha", "")
    delivery_id = request.headers.get(
        "x-github-delivery",
        str(uuid.uuid4()),
    )
    installation_id = payload.get("installation", {}).get("id")

    if not all([owner, repo_name, pr_number]):
        raise HTTPException(status_code=400, detail="Missing required PR fields")

    input_state: dict = {
        "owner": owner,
        "repo": repo_name,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "action": action,
        "delivery_id": delivery_id,
        "installation_id": installation_id,
        # Pre-initialise reducer fields so operator.add works on first append.
        "chunk_reviews": [],
        "model_usage": [],
    }

    background_tasks.add_task(_run_review, input_state)
    log.info("webhook_accepted", delivery_id=delivery_id, pr=pr_number)
    return {"status": "accepted", "delivery_id": delivery_id}


@app.get("/health")
async def health():
    return {"status": "ok", "graph_ready": _compiled_graph is not None}


# ── CLI entry point ──────────────────────────────────────────────────


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
