"""The deletion list, the membership status changes and the Pause state, mirrored in the lab database
(ADR 0009, "Deletion that stays deleted").

App Service restores do not touch the database, and database restores do not touch /home, so one copy
survives either restore. The tables live in the schema dojo_meta, owned by dbo. Only the admin connection
(the app's managed identity, the server's Entra admin) reads or writes them; no lab runner has any grant
on them. They hold hashes, statuses, reasons, dates and the epoch: no tid, oid, name or email.
"""
from __future__ import annotations

import re
from typing import Any

HEX64 = re.compile(r"[0-9a-f]{64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
STATUSES = ("active", "removed")
REASON = re.compile(r"[a-z-]{1,16}")
CHUNK = 400

SETUP = [
    "IF SCHEMA_ID(N'dojo_meta') IS NULL EXEC(N'CREATE SCHEMA [dojo_meta] AUTHORIZATION [dbo]')",
    """IF OBJECT_ID(N'dojo_meta.state', N'U') IS NULL EXEC(N'CREATE TABLE [dojo_meta].[state] (
        id int NOT NULL PRIMARY KEY CHECK (id = 1), epoch bigint NOT NULL, paused bit NOT NULL)')""",
    """IF OBJECT_ID(N'dojo_meta.tombstones', N'U') IS NULL EXEC(N'CREATE TABLE [dojo_meta].[tombstones] (
        key_hash char(64) NOT NULL PRIMARY KEY, lab_hash char(64) NOT NULL, deleted date NOT NULL)')""",
    """IF OBJECT_ID(N'dojo_meta.membership', N'U') IS NULL EXEC(N'CREATE TABLE [dojo_meta].[membership] (
        member_hash char(64) NOT NULL, epoch bigint NOT NULL, status varchar(8) NOT NULL,
        reason varchar(16) NOT NULL, changed date NOT NULL,
        CONSTRAINT pk_membership PRIMARY KEY (member_hash, epoch, status, reason, changed))')""",
    """IF OBJECT_ID(N'dojo_meta.retired_principals', N'U') IS NULL EXEC(N'CREATE TABLE [dojo_meta].[retired_principals] (
        principal_hash char(64) NOT NULL PRIMARY KEY, retired date NOT NULL)')""",
]

READ = [
    "SELECT epoch, CAST(paused AS int) FROM dojo_meta.state WHERE id = 1",
    "SELECT key_hash, lab_hash, CONVERT(char(10), deleted, 23) FROM dojo_meta.tombstones",
    "SELECT member_hash, epoch, status, reason, CONVERT(char(10), changed, 23) FROM dojo_meta.membership",
    "SELECT principal_hash, CONVERT(char(10), retired, 23) FROM dojo_meta.retired_principals",
]


def _hex(value: Any) -> str:
    v = str(value or "").strip()
    if not HEX64.fullmatch(v):
        raise ValueError("not a sha256 hash")
    return v


def _day(value: Any) -> str:
    v = str(value or "")
    if not DAY.fullmatch(v):
        raise ValueError("not a date")
    return v


class Mirror:
    def __init__(self, runner: Any):
        self.runner = runner   # labs.SqlRunner: fetch_all() on the admin connection

    def read(self) -> dict:
        """Creates the schema and tables if needed (idempotent), then reads everything."""
        out = self.runner.fetch_all(SETUP + READ)[len(SETUP):]
        state, tombs, members, retired = out
        epoch, paused = (int(state[0][0]), bool(int(state[0][1]))) if state else (0, False)
        return {
            "epoch": epoch, "paused": paused,
            "tombstones": [{"key": str(k).strip(), "lab": str(l).strip(), "at": str(d)[:10]} for k, l, d in tombs],
            "changes": [{"member": str(m).strip(), "epoch": int(e), "status": str(s).strip(), "reason": str(r).strip(),
                         "date": str(d)[:10]} for m, e, s, r, d in members],
            "retired": [{"principal": str(p).strip(), "at": str(d)[:10]} for p, d in retired],
        }

    def write(self, doc: dict, cutoff: str) -> None:
        """Make the database hold `doc` (already the union of both copies): the epoch and Pause state as
        given, every row added if missing, and rows older than `cutoff` deleted, as in the file."""
        cutoff = _day(cutoff)
        epoch = int(doc["epoch"])
        paused = 1 if doc.get("paused") else 0
        sql = [f"""IF EXISTS (SELECT 1 FROM dojo_meta.state WHERE id = 1)
            UPDATE dojo_meta.state SET epoch = {epoch}, paused = {paused} WHERE id = 1
            ELSE INSERT INTO dojo_meta.state (id, epoch, paused) VALUES (1, {epoch}, {paused})"""]
        tombs = [f"('{_hex(t['key'])}','{_hex(t.get('lab') or '0' * 64)}','{_day(t['at'])}')" for t in doc.get("tombstones") or []]
        for i in range(0, len(tombs), CHUNK):
            sql.append(f"""INSERT INTO dojo_meta.tombstones (key_hash, lab_hash, deleted)
                SELECT v.k, v.l, v.d FROM (VALUES {','.join(tombs[i:i + CHUNK])}) AS v(k, l, d)
                WHERE NOT EXISTS (SELECT 1 FROM dojo_meta.tombstones t WHERE t.key_hash = v.k)""")
        changes = []
        for c in doc.get("changes") or []:
            status, reason = str(c.get("status")), str(c.get("reason") or "removed")
            if status not in STATUSES or not REASON.fullmatch(reason):
                raise ValueError("not a membership change")
            changes.append(f"('{_hex(c['member'])}',{int(c.get('epoch') or 0)},'{status}','{reason}','{_day(c['date'])}')")
        for i in range(0, len(changes), CHUNK):
            sql.append(f"""INSERT INTO dojo_meta.membership (member_hash, epoch, status, reason, changed)
                SELECT v.m, v.e, v.s, v.r, v.d FROM (VALUES {','.join(changes[i:i + CHUNK])}) AS v(m, e, s, r, d)
                WHERE NOT EXISTS (SELECT 1 FROM dojo_meta.membership x WHERE x.member_hash = v.m AND x.epoch = v.e
                                  AND x.status = v.s AND x.reason = v.r AND x.changed = v.d)""")
        retired = [f"('{_hex(r['principal'])}','{_day(r['at'])}')" for r in doc.get("retired") or []]
        for i in range(0, len(retired), CHUNK):
            sql.append(f"""INSERT INTO dojo_meta.retired_principals (principal_hash, retired)
                SELECT v.p, v.d FROM (VALUES {','.join(retired[i:i + CHUNK])}) AS v(p, d)
                WHERE NOT EXISTS (SELECT 1 FROM dojo_meta.retired_principals x WHERE x.principal_hash = v.p)""")
        keep = {r["principal"] for r in doc.get("retired") or []}
        sql += [f"DELETE FROM dojo_meta.tombstones WHERE deleted < '{cutoff}'",
                f"DELETE FROM dojo_meta.membership WHERE changed < '{cutoff}'"]
        stale = "".join(f",'{_hex(p)}'" for p in sorted(keep))
        sql.append(f"DELETE FROM dojo_meta.retired_principals WHERE retired < '{cutoff}'"
                   + (f" AND principal_hash NOT IN ({stale[1:]})" if stale else ""))
        self.runner.fetch_all(SETUP + sql)
