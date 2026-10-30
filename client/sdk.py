"""Tiny Python SDK for signal-gateway.

    from client.sdk import SignalGateway

    sg = SignalGateway("http://localhost:8000", api_key="...")
    sg.send(account="+39333...", recipients=["+39333..."], message="ciao")

    async for event in sg.events(account="+39333..."):
        print(event)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Self
from urllib.parse import quote

import httpx
import websockets


class SignalGatewayError(RuntimeError):
    pass


class SignalGateway:
    def __init__(self, base_url: str = "http://localhost:8000", api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key} if api_key else {},
            timeout=620,
        )

    # ------------------------------------------------------------- low level
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise SignalGatewayError(f"{resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Escape hatch: any signal-cli JSON-RPC method."""
        return self._request("POST", "/v1/rpc", json={"method": method, "params": params or {}})

    # --------------------------------------------------------------- accounts
    def accounts(self) -> Any:
        return self._request("GET", "/v1/accounts")

    def link_start(self) -> dict[str, Any]:
        """Returns device_link_uri + qr_png_base64 to scan from the Signal app."""
        return self._request("POST", "/v1/accounts/link/start")

    def link_finish(self, device_link_uri: str, device_name: str = "signal-gateway") -> Any:
        return self._request("POST", "/v1/accounts/link/finish", json={
            "device_link_uri": device_link_uri, "device_name": device_name})

    def register(self, account: str, captcha: str | None = None, voice: bool = False,
                 reregister: bool = False) -> Any:
        return self._request("POST", "/v1/accounts/register", json={
            "account": account, "captcha": captcha, "voice": voice,
            "reregister": reregister})

    def verify(self, account: str, code: str, pin: str | None = None) -> Any:
        return self._request("POST", f"/v1/accounts/{account}/verify",
                             json={"code": code, "pin": pin})

    # --------------------------------------------------------------- messages
    def send(self, account: str, message: str | None = None, *,
             recipients: list[str] | None = None, group_id: str | None = None,
             note_to_self: bool = False, attachments: list[str] | None = None) -> Any:
        return self._request("POST", "/v1/messages/send", json={
            "account": account, "message": message, "recipients": recipients or [],
            "group_id": group_id, "note_to_self": note_to_self,
            "attachments": attachments or []})

    def react(self, account: str, emoji: str, target_author: str, target_timestamp: int, *,
              recipient: str | None = None, group_id: str | None = None) -> Any:
        return self._request("POST", "/v1/messages/reaction", json={
            "account": account, "emoji": emoji, "target_author": target_author,
            "target_timestamp": target_timestamp, "recipient": recipient,
            "group_id": group_id})

    # ------------------------------------------------------------------ data
    def contacts(self, account: str) -> Any:
        return self._request("GET", "/v1/contacts", params={"account": account})

    def unregistered_local(self, account: str, force: bool = False) -> Any:
        """ACI già marcati eliminati nel DB locale di signal-cli."""
        return self._request("GET", "/v1/contacts/local/unregistered",
                             params={"account": account, "force": force})

    def groups(self, account: str) -> Any:
        return self._request("GET", "/v1/groups", params={"account": account})

    def groups_local(self, account: str, offset: int = 0,
                     limit: int = 200, member_only: bool = True) -> Any:
        """One page of groups read from the locally synced database —
        fast even for accounts with hundreds of groups."""
        return self._request("GET", "/v1/groups/local", params={
            "account": account, "offset": offset, "limit": limit,
            "member_only": member_only})

    def group_people_local(self, account: str, group_id: str) -> Any:
        """Fresh (members, pendingMembers) of one group from the local
        database: who really entered, who is still invited."""
        return self._request("GET", "/v1/groups/local/people", params={
            "account": account, "group_id": group_id})

    def create_group(self, account: str, name: str, members: list[str] | None = None) -> Any:
        return self._request("POST", "/v1/groups", json={
            "account": account, "name": name, "members": members or []})

    def update_group(self, account: str, group_id: str, *,
                     add_members: list[str] | None = None,
                     remove_members: list[str] | None = None,
                     name: str | None = None, description: str | None = None) -> Any:
        # group_id is base64: '+', '/' and '=' must be percent-encoded,
        # otherwise '/' splits the path and the gateway answers 404.
        return self._request(
            "PATCH", f"/v1/groups/{quote(str(group_id), safe='')}", json={
                "account": account, "add_members": add_members or [],
                "remove_members": remove_members or [], "name": name,
                "description": description})

    def add_group_members(self, account: str, group_id: str, members: list[str]) -> Any:
        """Add members to a group the account belongs to. Depending on each
        user's privacy settings, Signal may turn some adds into pending
        invites that the person has to accept."""
        return self.update_group(account, group_id, add_members=members)

    def remove_group_members(self, account: str, group_id: str, members: list[str]) -> Any:
        """Remove members — for a pending invitee this revokes the invite."""
        return self.update_group(account, group_id, remove_members=members)

    def quit_group(self, account: str, group_id: str) -> Any:
        return self._request(
            "POST", f"/v1/groups/{quote(str(group_id), safe='')}/quit",
            params={"account": account})

    def join_group(self, account: str, uri: str) -> Any:
        """Join a group via its https://signal.group/#... invite link."""
        return self.rpc("joinGroup", {"account": account, "uri": uri})

    # ----------------------------------------------------------------- events
    async def events(self, account: str | None = None) -> AsyncIterator[dict[str, Any]]:
        ws_base = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        query = []
        if account:
            query.append(f"account={account}")
        if self.api_key:
            query.append(f"api_key={self.api_key}")
        url = f"{ws_base}/v1/events" + (f"?{'&'.join(query)}" if query else "")
        async with websockets.connect(url) as ws:
            async for raw in ws:
                yield json.loads(raw)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
