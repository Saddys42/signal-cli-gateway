"""RPC client against a fake signal-cli TCP daemon."""

import asyncio
import json

import pytest

from app.rpc import RpcError, SignalCliRpc


async def _fake_daemon(on_request):
    async def handle(reader, writer):
        while line := await reader.readline():
            req = json.loads(line)
            result = on_request(req)
            if isinstance(result, Exception):
                payload = {"jsonrpc": "2.0", "id": req["id"],
                           "error": {"code": -32602, "message": str(result)}}
            else:
                payload = {"jsonrpc": "2.0", "id": req["id"], "result": result}
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            # emit an incoming message notification after the first answer
            writer.write((json.dumps({
                "jsonrpc": "2.0", "method": "receive",
                "params": {"account": "+39000", "envelope": {
                    "sourceNumber": "+39111", "timestamp": 42,
                    "dataMessage": {"message": "hi"}}}}) + "\n").encode())
            await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_call_and_notification():
    server, port = await _fake_daemon(lambda req: {"timestamp": 123})
    rpc = SignalCliRpc("127.0.0.1", port)
    seen = []

    async def handler(payload):
        seen.append(payload)

    rpc.on_notification(handler)
    try:
        assert await rpc.call("send", {"message": "x"}) == {"timestamp": 123}
        await asyncio.sleep(0.1)
        assert seen and seen[0]["method"] == "receive"
    finally:
        await rpc.close()
        server.close()


@pytest.mark.asyncio
async def test_large_single_line_response():
    # Post-link sync payloads are single-line JSON far beyond 64 KiB.
    big = {"blob": "x" * 300_000}
    server, port = await _fake_daemon(lambda req: big)
    rpc = SignalCliRpc("127.0.0.1", port)
    try:
        assert await rpc.call("listGroups", {}) == big
    finally:
        await rpc.close()
        server.close()


@pytest.mark.asyncio
async def test_rpc_error_is_raised():
    server, port = await _fake_daemon(lambda req: ValueError("bad params"))
    rpc = SignalCliRpc("127.0.0.1", port)
    try:
        with pytest.raises(RpcError):
            await rpc.call("send", {})
    finally:
        await rpc.close()
        server.close()
