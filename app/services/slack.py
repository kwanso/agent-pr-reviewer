"""Async Slack notification client."""

from __future__ import annotations

import structlog
from slack_sdk.web.async_client import AsyncWebClient

from app.config import get_settings

log = structlog.get_logger()


class SlackNotifier:
    def __init__(self, token: str, channel: str):
        self._client = AsyncWebClient(token=token) if token else None
        self._channel = channel

    async def send(self, message: str, channel: str | None = None) -> None:
        if not self._client:
            log.warning("slack_skip_no_token")
            return
        target_channel = channel or self._channel
        log.info(
            "slack_send_attempt",
            passed_channel=channel,
            default_channel=self._channel,
            target_channel=target_channel,
        )
        if not target_channel:
            log.warning("slack_skip_no_channel")
            return
        try:
            await self._client.chat_postMessage(
                channel=target_channel,
                text=message,
                mrkdwn=True,
            )
        except Exception as exc:
            log.error("slack_send_failed", error=str(exc))


_notifier: SlackNotifier | None = None


def get_slack_notifier() -> SlackNotifier:
    global _notifier
    if _notifier is None:
        s = get_settings()
        _notifier = SlackNotifier(s.slack_bot_token, s.slack_channel)
    return _notifier
