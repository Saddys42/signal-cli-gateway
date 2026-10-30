"""Local (database) group listing: paged reads straight from a synced
signal-cli account.db, without involving the daemon."""

import base64
import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import localdb

ACI_SELF = "11111111-1111-4111-8111-111111111111"
ACI_OTHER = "22222222-2222-4222-8222-222222222222"
ACI_PENDING = "33333333-3333-4333-8333-333333333333"


def _field(number: int, payload: bytes) -> bytes:
    return bytes([(number << 3) | 2, len(payload)]) + payload


def _group_data(title: str, member_acis: list[str],
                pending_acis: list[str] | None = None) -> bytes:
    data = _field(2, title.encode())
    for aci in member_acis:
        data += _field(7, _field(1, uuid.UUID(aci).bytes))
    for aci in pending_acis or []:
        data += _field(8, _field(1, uuid.UUID(aci).bytes))
    return data


def _make_account(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "signal-cli-data"
    folder = data_dir / "data" / "123.d"
    folder.mkdir(parents=True)
    bogus = data_dir / "data" / "999.d"
    bogus.mkdir(parents=True)
    (bogus / "account.db").write_bytes(b"")  # abandoned attempt, same number
    (data_dir / "data" / "accounts.json").write_text(json.dumps({
        "accounts": [{"path": "999", "number": "+393331111111", "uuid": None},
                     {"path": "123", "number": "+39 333 1111111",
                      "uuid": ACI_SELF}]}), encoding="utf-8")

    db = sqlite3.connect(folder / "account.db")
    db.executescript("""
        CREATE TABLE recipient (_id INTEGER PRIMARY KEY, number TEXT, aci TEXT);
        CREATE TABLE group_v2 (_id INTEGER PRIMARY KEY, group_id BLOB,
                               group_data BLOB, blocked INTEGER);
    """)
    db.executemany("INSERT INTO recipient VALUES (?, ?, ?)", [
        (1, "+393331111111", ACI_SELF),
        (2, "+393332222222", ACI_OTHER),
    ])
    gid = bytes(range(32))
    db.execute("INSERT INTO group_v2 VALUES (1, ?, ?, 0)",
               (gid, _group_data("Gruppo Prova", [ACI_SELF, ACI_OTHER],
                                 pending_acis=[ACI_PENDING])))
    db.execute("INSERT INTO group_v2 VALUES (2, ?, ?, 0)",
               (bytes(range(32, 64)), _group_data("Altro", [ACI_OTHER])))
    db.commit()
    db.close()
    return data_dir, base64.b64encode(gid).decode()


def test_find_account_db_prefers_registered_entry(tmp_path):
    data_dir, _ = _make_account(tmp_path)
    db_path, aci = localdb.find_account_db(data_dir, "+393331111111")
    assert db_path.parent.name == "123.d" and aci == ACI_SELF


def test_read_groups_page_only_member_groups(tmp_path):
    data_dir, expected_id = _make_account(tmp_path)
    db_path, aci = localdb.find_account_db(data_dir, "+393331111111")
    page = localdb.read_groups_page(db_path, aci, offset=0, limit=10)
    assert page["total"] == 1  # group 2 skipped: self is not a member
    (group,) = page["groups"]
    assert group["id"] == expected_id
    assert group["name"] == "Gruppo Prova"
    assert {m["uuid"] for m in group["members"]} == {ACI_SELF, ACI_OTHER}
    # Pending invitations are extracted separately from full members.
    assert {m["uuid"] for m in group["pendingMembers"]} == {ACI_PENDING}
    by_uuid = {m["uuid"]: m["number"] for m in group["members"]}
    assert by_uuid[ACI_OTHER] == "+393332222222"


def test_read_groups_page_can_show_left_groups(tmp_path):
    data_dir, _ = _make_account(tmp_path)
    db_path, aci = localdb.find_account_db(data_dir, "+393331111111")
    page = localdb.read_groups_page(db_path, aci, offset=0, limit=10,
                                    member_only=False)
    assert page["total"] == 2  # the group self left is listed too
    by_name = {g["name"]: g["isMember"] for g in page["groups"]}
    assert by_name == {"Gruppo Prova": True, "Altro": False}


def test_read_group_people(tmp_path):
    data_dir, expected_id = _make_account(tmp_path)
    db_path, _aci = localdb.find_account_db(data_dir, "+393331111111")
    people = localdb.read_group_people(db_path, expected_id)
    assert set(people["members"]) == {ACI_SELF, ACI_OTHER}
    assert people["pendingMembers"] == [ACI_PENDING]
    assert localdb.read_group_people(db_path, "AAAA") == {
        "members": [], "pendingMembers": []}


def test_read_unregistered_acis(tmp_path):
    data_dir, _ = _make_account(tmp_path)
    db_path, _aci = localdb.find_account_db(data_dir, "+393331111111")
    assert localdb.read_unregistered_acis(db_path) == []
    db = sqlite3.connect(db_path)
    db.execute("ALTER TABLE recipient ADD COLUMN unregistered_timestamp INTEGER")
    db.execute(
        "UPDATE recipient SET unregistered_timestamp = 1 WHERE aci = ?",
        (ACI_OTHER,))
    db.commit()
    db.close()
    localdb._SNAPSHOTS.clear()
    assert localdb.read_unregistered_acis(db_path) == [ACI_OTHER]


def test_read_groups_page_paging(tmp_path):
    data_dir, _ = _make_account(tmp_path)
    db_path, aci = localdb.find_account_db(data_dir, "+393331111111")
    assert localdb.read_groups_page(db_path, aci, 0, 1)["total"] == 1
    assert localdb.read_groups_page(db_path, aci, 1, 1)["groups"] == []
