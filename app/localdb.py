"""Read group lists straight from signal-cli's local account database.

Why: for very large accounts (hundreds of groups) the daemon's
``listGroups`` RPC can take longer than any sane timeout, because the
daemon is busy receiving. The groups are already synced into a local
SQLite database, so reading them from there — in pages — is fast and
keeps working while the daemon catches up. Adding people still goes
through the daemon RPC, exactly as before.

Only copies of the database files are ever opened; the daemon's own
files are never touched for writing.
"""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

_SNAPSHOTS: dict[str, tuple[tuple[int, int, int, int], Path, float]] = {}
_PARSED: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_SECONDS = 60.0


def resolve_data_dir(configured: str) -> Path:
    """Find the signal-cli data directory (accounts.json lives inside)."""
    candidates = [Path(configured),
                  Path(__file__).resolve().parent.parent / configured]
    for candidate in candidates:
        if (candidate / "data" / "accounts.json").is_file():
            return candidate
    return candidates[0]


def _compact(number: str) -> str:
    return "".join(ch for ch in number or "" if ch.isdigit() or ch == "+")


def find_account_db(data_dir: Path, account: str) -> tuple[Path, str | None]:
    """Map an account number to its (account.db path, ACI) via accounts.json."""
    manifest = data_dir / "data" / "accounts.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    wanted = _compact(account)
    matches: list[tuple[Path, str | None]] = []
    for entry in data.get("accounts") or []:
        if _compact(entry.get("number") or "") == wanted and entry.get("path"):
            db_path = data_dir / "data" / f"{entry['path']}.d" / "account.db"
            if db_path.is_file():
                matches.append((db_path, entry.get("uuid")))
    if not matches:
        raise FileNotFoundError(
            f"No local database for account {account} in {data_dir}")
    # Leftover entries from abandoned registration attempts can carry the
    # same number (even spaced differently) with an empty database. The
    # real account is the one with a registered UUID.
    matches.sort(key=lambda match: 0 if match[1] else 1)
    return matches[0]


def _fingerprint(db_path: Path) -> tuple[int, int, int, int]:
    def stat(path: Path) -> tuple[int, int]:
        try:
            info = path.stat()
            return info.st_size, int(info.st_mtime)
        except OSError:
            return 0, 0

    db_size, db_mtime = stat(db_path)
    wal_size, wal_mtime = stat(db_path.with_name(db_path.name + "-wal"))
    return db_size, db_mtime, wal_size, wal_mtime


def _snapshot(db_path: Path, force: bool = False) -> Path:
    """Copy db+wal+shm to a temp folder.

    The copy is reused for a minute even if the daemon keeps writing:
    during a paged GUI load every page must see the same data, and a
    slightly stale copy beats re-copying ~100 MB per page. With
    ``force`` the copy is refreshed immediately (used when checking who
    really entered a group right after an addition).
    """
    import time

    key = str(db_path)
    fingerprint = _fingerprint(db_path)
    cached = _SNAPSHOTS.get(key)
    if cached and not force and (cached[0] == fingerprint
                                 or time.time() - cached[2] < _CACHE_SECONDS):
        return cached[1]
    folder = cached[1].parent if cached else Path(
        tempfile.mkdtemp(prefix="sg-groups-"))
    target = folder / "account.db"
    for suffix in ("", "-wal", "-shm"):
        source = db_path.with_name(db_path.name + suffix)
        if source.is_file():
            shutil.copy2(source, folder / ("account.db" + suffix))
    _SNAPSHOTS[key] = (fingerprint, target, time.time())
    return target


def read_unregistered_acis(db_path: Path, force: bool = False) -> list[str]:
    """ACI che Signal ha già marcato come account cancellati.

    Colonna ``unregistered_timestamp`` nel DB locale: niente RPC,
    getUserStatus (CDSI) accetta solo numeri e sui membri dei gruppi
    (quasi tutti solo ACI) dà errore 4003.
    """
    snapshot = _snapshot(db_path, force=force)
    connection = sqlite3.connect(snapshot)
    try:
        cols = {row[1] for row in connection.execute(
            "PRAGMA table_info(recipient)")}
        if "aci" not in cols or "unregistered_timestamp" not in cols:
            return []
        rows = connection.execute(
            "SELECT aci FROM recipient"
            " WHERE aci IS NOT NULL"
            " AND unregistered_timestamp IS NOT NULL"
            " AND unregistered_timestamp != 0").fetchall()
        return [str(aci).lower() for (aci,) in rows if aci]
    finally:
        connection.close()


def read_group_people(db_path: Path, group_id_b64: str) -> dict[str, Any]:
    """Fresh (members, pending) UUID lists of ONE group, for checking
    who really entered right after an addition block."""
    snapshot = _snapshot(db_path, force=True)
    try:
        raw_id = base64.b64decode(group_id_b64)
    except (ValueError, TypeError):
        raw_id = b""
    connection = sqlite3.connect(snapshot)
    try:
        row = connection.execute(
            "SELECT group_data FROM group_v2 WHERE group_id = ?",
            (raw_id,)).fetchone()
        if row is None:  # some builds may store the id as text
            row = connection.execute(
                "SELECT group_data FROM group_v2 WHERE group_id = ?",
                (group_id_b64,)).fetchone()
        if row is None:
            return {"members": [], "pendingMembers": []}
        _title, members, pending = _parse_group(row[0])
        return {"members": members, "pendingMembers": pending}
    finally:
        connection.close()


def _varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while i < len(data):
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, i


def _iter_fields(data: bytes):
    """Yield (field_number, value) for one protobuf message level."""
    i = 0
    while i < len(data):
        tag, i = _varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            length, i = _varint(data, i)
            yield field, data[i:i + length]
            i += length
        elif wire == 0:
            value, i = _varint(data, i)
            yield field, value
        elif wire == 1:
            yield field, data[i:i + 8]
            i += 8
        elif wire == 5:
            yield field, data[i:i + 4]
            i += 4
        else:
            return


def _parse_group(group_data: bytes | None) -> tuple[str, list[str], list[str]]:
    """(title, member UUIDs, pending UUIDs) from a stored GV2 snapshot.

    Field 2 is the title; field 7 repeats once per member, fields 8/9
    once per invited/requesting person — each with the person's 16-byte
    ACI in its own field 1. This snapshot — not the group_v2_member
    table — is what the daemon itself reports, and it stays complete
    even for groups whose member table was never filled, including
    groups the account has since left.
    """
    import uuid as uuid_module

    def person_uuid(entry: bytes) -> str | None:
        for sub_field, sub_value in _iter_fields(entry):
            if sub_field == 1 and isinstance(sub_value, bytes) \
                    and len(sub_value) == 16:
                return str(uuid_module.UUID(bytes=sub_value))
        return None

    title = ""
    members: list[str] = []
    pending: list[str] = []
    if not group_data:
        return title, members, pending
    for field, value in _iter_fields(group_data):
        if field == 2 and isinstance(value, bytes) and not title:
            title = value.decode("utf-8", "replace")
        elif field == 7 and isinstance(value, bytes):
            found = person_uuid(value)
            if found:
                members.append(found)
        elif field in (8, 9) and isinstance(value, bytes):
            found = person_uuid(value)
            if found:
                pending.append(found)
    return title, members, pending


def read_groups_page(db_path: Path, aci: str | None, offset: int,
                     limit: int, member_only: bool = True) -> dict[str, Any]:
    """One page of groups, with their members.

    With ``member_only`` only groups the account currently belongs to
    are returned; otherwise every stored group is returned too, each
    flagged with ``isMember`` (Signal keeps the records of groups the
    account has left, and they can still be browsed)."""
    import time

    snapshot = _snapshot(db_path)
    cached = _PARSED.get(str(snapshot))
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        parsed = cached[1]
        if member_only:
            parsed = [g for g in parsed if g["isMember"]]
        return {"total": len(parsed),
                "groups": parsed[offset:offset + limit]}

    # Normal (writable) open of the COPY: SQLite must recover the WAL to
    # see the latest data. A read-only open cannot, and would silently
    # show the stale main file. The daemon's own files are never opened.
    connection = sqlite3.connect(snapshot)
    try:
        numbers = {row_aci: number for row_aci, number in connection.execute(
            "SELECT aci, number FROM recipient"
            " WHERE aci IS NOT NULL AND number IS NOT NULL").fetchall()}

        parsed: list[dict[str, Any]] = []
        for group_id, group_data, blocked in connection.execute(
                "SELECT group_id, group_data, blocked FROM group_v2"
                " ORDER BY _id").fetchall():
            title, member_uuids, pending_uuids = _parse_group(group_data)
            if isinstance(group_id, bytes):
                group_id = base64.b64encode(group_id).decode()
            parsed.append({
                "id": group_id,
                "name": title or "(senza nome)",
                "isMember": (aci.lower() in member_uuids) if aci else True,
                "isBlocked": bool(blocked),
                "members": [{"number": numbers.get(member), "uuid": member}
                            for member in member_uuids],
                "pendingMembers": [
                    {"number": numbers.get(person), "uuid": person}
                    for person in pending_uuids],
                "description": "",
                "source": "local",
            })
        _PARSED[str(snapshot)] = (time.time(), parsed)
        if member_only:
            parsed = [g for g in parsed if g["isMember"]]
        return {"total": len(parsed),
                "groups": parsed[offset:offset + limit]}
    finally:
        connection.close()
