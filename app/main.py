"""signal-gateway HTTP API.

REST + WebSocket layer on top of a multi-account signal-cli daemon.
Interactive docs (Swagger) are served at /docs when the app runs.
"""

from __future__ import annotations

import base64
import io
import logging
from contextlib import asynccontextmanager
from typing import Any

import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

from fastapi.concurrency import run_in_threadpool

from . import localdb
from .config import Settings, get_settings
from .events import EventBus, normalize_receive
from .models import (
    CreateGroupRequest,
    FinishLinkRequest,
    ReactionRequest,
    RegisterRequest,
    RpcPassthroughRequest,
    SendMessageRequest,
    UpdateGroupRequest,
    VerifyRequest,
    normalize_e164,
)
from .rpc import RpcConnectionError, RpcError, SignalCliRpc
from .sendqueue import SendQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# --------------------------------------------------------------------- state
class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rpc = SignalCliRpc(settings.signal_cli_host, settings.signal_cli_port,
                                timeout=settings.rpc_timeout)
        self.bus = EventBus(settings.webhook_url, settings.webhook_secret)
        self.queue = SendQueue(settings.send_min_interval_seconds,
                               settings.send_burst_per_minute)
        self.rpc.on_notification(self._on_notification)

    async def _on_notification(self, payload: dict[str, Any]) -> None:
        event = normalize_receive(payload)
        if event:
            await self.bus.publish(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: State = app.state.sg
    try:
        await state.rpc.connect()
    except RpcConnectionError as exc:
        # Start anyway: /health reports the problem and calls retry lazily.
        log.warning(str(exc))
    yield
    await state.rpc.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="signal-gateway",
        version="0.1.0",
        description="Script any Signal account: REST + WebSocket API over signal-cli.",
        lifespan=lifespan,
    )
    app.state.sg = State(settings)

    # ------------------------------------------------------------------ auth
    async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    protected = [Depends(require_api_key)]

    def state() -> State:
        return app.state.sg

    async def rpc_call(method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await state().rpc.call(method, params)
        except RpcError as exc:
            status = 502 if isinstance(exc, RpcConnectionError) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    # ---------------------------------------------------------------- health
    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, Any]:
        return {"status": "ok", "signal_cli_connected": state().rpc.connected}

    # --------------------------------------------------------------- accounts
    @app.get("/v1/accounts", dependencies=protected, tags=["accounts"])
    async def list_accounts() -> Any:
        return await rpc_call("listAccounts")

    @app.post("/v1/accounts/link/start", dependencies=protected, tags=["accounts"])
    async def link_start() -> dict[str, Any]:
        """Start linking as a secondary device. Scan qr_png_base64 with the
        Signal app (Settings -> Linked devices) within ~60s, then call finish."""
        result = await rpc_call("startLink")
        uri = result["deviceLinkUri"]
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "device_link_uri": uri,
            "qr_png_base64": base64.b64encode(buf.getvalue()).decode(),
            "next": "POST /v1/accounts/link/finish with device_link_uri",
        }

    @app.post("/v1/accounts/link/finish", dependencies=protected, tags=["accounts"])
    async def link_finish(body: FinishLinkRequest) -> Any:
        return await rpc_call("finishLink", {
            "deviceLinkUri": body.device_link_uri, "deviceName": body.device_name})

    @app.post("/v1/accounts/register", dependencies=protected, tags=["accounts"])
    async def register(body: RegisterRequest) -> Any:
        """Register a dedicated number (primary device). Often needs captcha:
        solve it at signalcaptchas.org and pass the full signalcaptcha:// link.
        Then confirm with /v1/accounts/{account}/verify."""
        return await rpc_call("register", {
            "account": body.account, "captcha": body.captcha,
            "voice": body.voice, "reregister": body.reregister})

    @app.post("/v1/accounts/{account}/verify", dependencies=protected, tags=["accounts"])
    async def verify(account: str, body: VerifyRequest) -> Any:
        return await rpc_call("verify", {
            "account": normalize_e164(account),
            "verificationCode": body.code, "pin": body.pin})

    @app.get("/v1/accounts/{account}/devices", dependencies=protected, tags=["accounts"])
    async def list_devices(account: str) -> Any:
        return await rpc_call("listDevices", {"account": account})

    # --------------------------------------------------------------- messages
    @app.post("/v1/messages/send", dependencies=protected, tags=["messages"])
    async def send_message(body: SendMessageRequest) -> Any:
        # signal-cli accepts both singular (man page) and plural (generated
        # client) spellings across versions; send both where they exist.
        params: dict[str, Any] = {
            "account": body.account, "message": body.message,
            "recipient": body.recipients or None,
            "recipients": body.recipients or None,
            "groupId": body.group_id,
            "groupIds": [body.group_id] if body.group_id else None,
            "noteToSelf": body.note_to_self or None,
            "attachment": body.attachments or None,
            "attachments": body.attachments or None,
            "quoteTimestamp": body.quote_timestamp, "quoteAuthor": body.quote_author,
            "quoteMessage": body.quote_message,
        }
        return await state().queue.run(
            body.account, lambda: rpc_call("send", params))

    @app.post("/v1/messages/reaction", dependencies=protected, tags=["messages"])
    async def send_reaction(body: ReactionRequest) -> Any:
        params = {
            "account": body.account, "emoji": body.emoji,
            "targetAuthor": body.target_author, "targetTimestamp": body.target_timestamp,
            "recipient": [body.recipient] if body.recipient else None,
            "recipients": [body.recipient] if body.recipient else None,
            "groupId": body.group_id,
            "groupIds": [body.group_id] if body.group_id else None,
            "remove": body.remove or None,
        }
        return await state().queue.run(
            body.account, lambda: rpc_call("sendReaction", params))

    # --------------------------------------------------------------- contacts
    @app.get("/v1/contacts", dependencies=protected, tags=["contacts"])
    async def list_contacts(account: str) -> Any:
        return await rpc_call("listContacts", {"account": account})

    @app.get("/v1/contacts/local/unregistered", dependencies=protected,
             tags=["contacts"])
    async def unregistered_local(account: str, force: bool = False) -> Any:
        """Account che Signal ha già segnato come eliminati, dal DB locale."""
        data_dir = localdb.resolve_data_dir(settings.signal_cli_data_dir)
        try:
            db_path, _aci = localdb.find_account_db(data_dir, account)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        ids = await run_in_threadpool(
            localdb.read_unregistered_acis, db_path, force)
        return {"account": account, "ids": ids}

    @app.post("/v1/contacts/{contact}/block", dependencies=protected, tags=["contacts"])
    async def block(account: str, contact: str) -> Any:
        return await rpc_call("block", {"account": account, "recipient": [contact],
                                        "recipients": [contact]})

    @app.post("/v1/contacts/{contact}/unblock", dependencies=protected, tags=["contacts"])
    async def unblock(account: str, contact: str) -> Any:
        return await rpc_call("unblock", {"account": account, "recipient": [contact],
                                          "recipients": [contact]})

    # ----------------------------------------------------------------- groups
    @app.get("/v1/groups", dependencies=protected, tags=["groups"])
    async def list_groups(account: str) -> Any:
        return await rpc_call("listGroups", {"account": account})

    @app.get("/v1/groups/local", dependencies=protected, tags=["groups"])
    async def list_groups_local(account: str, offset: int = 0,
                                limit: int = 200,
                                member_only: bool = True) -> Any:
        """Groups read page by page from the locally synced database.

        The daemon's listGroups RPC cannot answer in time for very large
        accounts; this reads the same data the daemon already synced to
        disk. Adding members still uses the daemon RPC as usual.
        """
        data_dir = localdb.resolve_data_dir(settings.signal_cli_data_dir)
        try:
            db_path, aci = localdb.find_account_db(data_dir, account)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        page = await run_in_threadpool(
            localdb.read_groups_page, db_path, aci,
            max(0, offset), max(1, min(limit, 500)), member_only)
        return {"account": account, "offset": max(0, offset), **page}

    @app.get("/v1/groups/local/people", dependencies=protected,
             tags=["groups"])
    async def group_people_local(account: str, group_id: str) -> Any:
        """Fresh member/pending lists of one group from the local
        database — used to report, per addition block, who really
        entered and who only got an invitation."""
        data_dir = localdb.resolve_data_dir(settings.signal_cli_data_dir)
        try:
            db_path, _aci = localdb.find_account_db(data_dir, account)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        people = await run_in_threadpool(
            localdb.read_group_people, db_path, group_id)
        return {"account": account, "groupId": group_id, **people}

    @app.post("/v1/groups", dependencies=protected, tags=["groups"])
    async def create_group(body: CreateGroupRequest) -> Any:
        # signal-cli has no createGroup: updateGroup without groupId creates one.
        return await rpc_call("updateGroup", {
            "account": body.account, "name": body.name,
            "member": body.members, "members": body.members,
            "description": body.description})

    # group_id is base64 and may contain '/': ':path' keeps the whole
    # (percent-encoded) id in one parameter instead of a 404.
    @app.patch("/v1/groups/{group_id:path}", dependencies=protected, tags=["groups"])
    async def update_group(group_id: str, body: UpdateGroupRequest) -> Any:
        return await rpc_call("updateGroup", {
            "account": body.account, "groupId": group_id, "name": body.name,
            "description": body.description,
            "member": body.add_members, "members": body.add_members,
            "removeMember": body.remove_members})

    @app.post("/v1/groups/{group_id:path}/quit", dependencies=protected, tags=["groups"])
    async def quit_group(group_id: str, account: str) -> Any:
        return await rpc_call("quitGroup", {"account": account, "groupId": group_id})

    # ----------------------------------------------------------------- events
    @app.websocket("/v1/events")
    async def events_ws(websocket: WebSocket, account: str | None = None,
                        api_key: str | None = None) -> None:
        if settings.api_key and api_key != settings.api_key:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = state().bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if account and event.get("account") not in (None, account):
                    continue
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            state().bus.unsubscribe(queue)

    # ----------------------------------------------------------- escape hatch
    @app.post("/v1/rpc", dependencies=protected, tags=["advanced"])
    async def rpc_passthrough(body: RpcPassthroughRequest) -> Any:
        """Call any signal-cli JSON-RPC method (sendPollCreate, updateProfile,
        listIdentities, trust, ...) without waiting for a typed endpoint."""
        return await rpc_call(body.method, body.params)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
