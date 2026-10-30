"""Async JSON-RPC client for the signal-cli daemon (TCP, newline-delimited).

One persistent connection is used for both requests and `receive`
notifications, mirroring how signal-cli's daemon mode is meant to be driven:

    {"jsonrpc":"2.0","method":"send","params":{...},"id":"1"}
    {"jsonrpc":"2.0","result":{"timestamp":123},"id":"1"}
    {"jsonrpc":"2.0","method":"receive","params":{"envelope":{...}}}   <- no id

Reference: https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-jsonrpc.5.adoc
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class RpcConnectionError(RpcError):
    def __init__(self, message: str) -> None:
        super().__init__(-32000, message)


class SignalCliRpc:
    """Reconnecting JSON-RPC client. Thread-unsafe by design: one event loop."""

    def __init__(self, host: str, port: int, timeout: float = 600.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._handlers: list[NotificationHandler] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------------ events
    def on_notification(self, handler: NotificationHandler) -> None:
        self._handlers.append(handler)

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # -------------------------------------------------------------- connection
    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            try:
                # limit: signal-cli answers/notifications are single-line JSON
                # and a full sync after linking easily exceeds asyncio's
                # default 64 KiB line limit (which killed the connection).
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port,
                                            limit=64 * 1024 * 1024),
                    timeout=10
                )
            except (OSError, TimeoutError) as exc:
                raise RpcConnectionError(
                    f"Cannot reach signal-cli daemon at {self.host}:{self.port} ({exc}). "
                    "Is it running? e.g. `signal-cli daemon --tcp "
                    f"{self.host}:{self.port}` or `docker compose up`."
                ) from exc
            self._reader_task = asyncio.create_task(self._read_loop(), name="rpc-reader")
            log.info("Connected to signal-cli daemon at %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()
        self._fail_pending(RpcConnectionError("Connection closed"))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Non-JSON line from signal-cli: %r", line[:200])
                    continue
                msg_id = payload.get("id")
                if msg_id is not None and str(msg_id) in self._pending:
                    future = self._pending.pop(str(msg_id))
                    if "error" in payload:
                        err = payload["error"] or {}
                        future.set_exception(
                            RpcError(err.get("code", -1), err.get("message", "RPC error"),
                                     err.get("data"))
                        )
                    else:
                        future.set_result(payload.get("result"))
                elif payload.get("method"):
                    for handler in self._handlers:
                        asyncio.create_task(self._safe_call(handler, payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("signal-cli reader loop failed")
        finally:
            self._writer = None
            self._fail_pending(RpcConnectionError("signal-cli connection lost"))

    async def _safe_call(self, handler: NotificationHandler, payload: dict) -> None:
        try:
            await handler(payload)
        except Exception:
            log.exception("Notification handler failed")

    def _fail_pending(self, exc: RpcError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -------------------------------------------------------------------- call
    async def call(self, method: str, params: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> Any:
        await self.connect()
        assert self._writer is not None
        req_id = str(next(self._ids))
        request = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params:
            request["params"] = {k: v for k, v in params.items() if v is not None}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        async with self._write_lock:
            self._writer.write((json.dumps(request) + "\n").encode())
            await self._writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise RpcConnectionError(
                f"signal-cli did not answer '{method}' within {timeout or self.timeout}s"
            ) from exc
