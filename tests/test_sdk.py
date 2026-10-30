"""SDK tests: the group_id must travel percent-encoded in the URL.

Group ids are base64 and can contain '/', '+' and '='. If '/' reaches
the URL raw it splits the path and the gateway answers
404 {"detail": "Not Found"} — the bug that broke the automatic
distribution. No real gateway/daemon: httpx MockTransport.
"""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.sdk import SignalGateway  # noqa: E402

GROUP_ID = "WMY+5fMjr2xC5ghjTFb8UUgkSYU/x5ybdmH3kQZVxAw="


def _gateway_capturing(captured: dict) -> SignalGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["raw_path"] = request.url.raw_path
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    sg = SignalGateway("http://localhost:8000")
    sg._http = httpx.Client(
        base_url="http://localhost:8000",
        transport=httpx.MockTransport(handler), timeout=10)
    return sg


def test_add_group_members_percent_encodes_group_id():
    captured: dict = {}
    sg = _gateway_capturing(captured)
    sg.add_group_members("+393775451814", GROUP_ID, ["f76c4853-d4fe-4146-b5c2-b71a2a0a6b96"])
    assert captured["method"] == "PATCH"
    # Esattamente la rotta vera del gateway, un solo segmento codificato.
    assert captured["raw_path"] == (
        b"/v1/groups/WMY%2B5fMjr2xC5ghjTFb8UUgkSYU%2F"
        b"x5ybdmH3kQZVxAw%3D")
    assert b"add_members" in captured["body"]


def test_remove_group_members_percent_encodes_group_id():
    captured: dict = {}
    sg = _gateway_capturing(captured)
    sg.remove_group_members("+447781792285", GROUP_ID, ["member-1"])
    assert captured["method"] == "PATCH"
    assert captured["raw_path"].count(b"/") == 3  # /v1/groups/<id>
