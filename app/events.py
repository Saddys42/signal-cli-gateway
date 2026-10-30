"""Fan-out of incoming signal-cli events to WebSocket clients and a webhook."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, webhook_url: str = "", webhook_secret: str = "") -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret

    # ------------------------------------------------------------- WebSocket
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # ---------------------------------------------------------------- publish
    async def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping event for a slow WebSocket consumer")
                self._subscribers.discard(queue)
        if self.webhook_url:
            asyncio.create_task(self._deliver_webhook(event))

    async def _deliver_webhook(self, event: dict[str, Any]) -> None:
        body = json.dumps(event).encode()
        headers = {"Content-Type": "application/json"}
        if self.webhook_secret:
            sig = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-SG-Signature"] = f"sha256={sig}"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(self.webhook_url, content=body, headers=headers)
                    resp.raise_for_status()
                return
            except Exception:
                if attempt == 2:
                    log.exception("Webhook delivery failed after 3 attempts")
                await asyncio.sleep(2**attempt)


def normalize_receive(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a raw signal-cli `receive` notification into a stable public event.

    Returns None for notifications we don't expose (keep-alives, etc.).
    The raw envelope is kept under `raw` so no information is lost.
    """
    if payload.get("method") != "receive":
        return None
    params = payload.get("params") or {}
    # manual receive-mode wraps the envelope in params.result
    envelope = params.get("envelope") or (params.get("result") or {}).get("envelope") or {}
    if not envelope:
        return None
    data = envelope.get("dataMessage") or {}
    sync = (envelope.get("syncMessage") or {}).get("sentMessage") or {}
    message = data or sync
    group = (message.get("groupInfo") or {}).get("groupId")
    return {
        "type": "message",
        "account": params.get("account"),
        "source": envelope.get("sourceNumber") or envelope.get("sourceUuid"),
        "source_name": envelope.get("sourceName"),
        "timestamp": envelope.get("timestamp"),
        "is_sync": bool(sync),  # sent from another linked device of the same account
        "group_id": group,
        "text": message.get("message"),
        "attachments": message.get("attachments") or [],
        "mentions": message.get("mentions") or [],
        "quote": message.get("quote"),
        "reaction": (envelope.get("dataMessage") or {}).get("reaction"),
        "raw": envelope,
    }
