"""API tests with a stubbed RPC layer (no signal-cli needed)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class StubRpc:
    connected = True

    def __init__(self):
        self.calls = []

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == "listAccounts":
            return [{"number": "+39000", "aci": "uuid-1"}]
        if method == "send":
            return {"timestamp": 999}
        return {}

    def on_notification(self, handler):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass


@pytest.fixture()
def client():
    app = create_app(Settings(api_key="secret", signal_cli_host="127.0.0.1"))
    app.state.sg.rpc = StubRpc()
    # send queue calls the rpc_call closure, which reads state().rpc fresh
    with TestClient(app) as c:
        c.sg_app = app
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_auth_required(client):
    assert client.get("/v1/accounts").status_code == 401
    assert client.get("/v1/accounts", headers={"X-API-Key": "secret"}).status_code == 200


def test_send_validates_target(client):
    resp = client.post("/v1/messages/send", headers={"X-API-Key": "secret"},
                       json={"account": "+39000", "message": "hi"})
    assert resp.status_code == 422


def test_register_normalizes_spaced_number(client):
    resp = client.post("/v1/accounts/register", headers={"X-API-Key": "secret"},
                       json={"account": "+34 657 84 49 47"})
    assert resp.status_code == 200
    method, params = client.sg_app.state.sg.rpc.calls[-1]
    assert method == "register"
    assert params["account"] == "+34657844947"


def test_register_rejects_non_e164(client):
    resp = client.post("/v1/accounts/register", headers={"X-API-Key": "secret"},
                       json={"account": "657844947"})
    assert resp.status_code == 422


def test_update_group_with_slash_in_group_id(client):
    # I codici gruppo sono base64 e possono contenere '/', '+' e '=':
    # il client li manda percent-encoded (%2F ecc.) e la rotta vera
    # del gateway deve accettarli e passare al daemon l'id originale.
    from urllib.parse import quote

    group_id = "WMY+5fMjr2xC5ghjTFb8UUgkSYU/x5ybdmH3kQZVxAw="
    resp = client.patch(
        f"/v1/groups/{quote(group_id, safe='')}",
        headers={"X-API-Key": "secret"},
        json={"account": "+39000", "add_members": ["member-1"],
              "remove_members": [], "name": None, "description": None})
    assert resp.status_code == 200
    method, params = client.sg_app.state.sg.rpc.calls[-1]
    assert method == "updateGroup"
    assert params["groupId"] == group_id
    assert params["member"] == ["member-1"]


def test_send_ok(client):
    resp = client.post("/v1/messages/send", headers={"X-API-Key": "secret"},
                       json={"account": "+39000", "message": "hi",
                             "recipients": ["+39111"]})
    assert resp.status_code == 200
    assert resp.json() == {"timestamp": 999}
