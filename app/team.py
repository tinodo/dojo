"""Team Dojo (ADR 0009): members, admission, roles, leaving, and deletion that stays deleted.

With team mode off (the default), every route class reduces to today's owner-only sign-in. With it on,
Easy Auth lets every user of the tenant through, and this module is the real gate: the owner plus the
members the owner approved. Membership lives in team/members.json and is read on every request, so a
removal takes effect on the next one.

What Dojo keeps about a member is deliberately small: no activity date, no per-member numbers. The
owner's Members page shows display name, Entra name, email, role, status and joined date, nothing else.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .core import (Learner, Principal, Settings, Store, current_learner, iso, learner_key, new_id, owner_key, parse_iso,
                   read_principal, sha256, utcnow, where_stored)
from .labs import SURROGATE, new_surrogate

log = logging.getLogger("dojo")

NOTICE_VERSION = 1
GRACE_DAYS = 30            # a removed member keeps export and delete this long, then the data is deleted
REQUEST_DAYS = 30          # open and declined access requests are deleted after this
TOMBSTONE_DAYS = 40        # longer than the 30-day App Service backups
MEMBERSHIP_MONTHS = 12
RENEW_WINDOW_DAYS = 30
AUDIT_DAYS = 365
END_NOTICE_DAYS = 14
MAX_MEMBERS = 30           # the supported scale on one B1 instance (ADR 0009, "Capacity")
SWEEP_SECONDS = 3600
STARTUP_WAIT = 90          # a serverless database wakes from auto-pause in about a minute
RETRY_SECONDS = 300        # while the lab database does not answer, the mirror tries again this often
NO_RESTORE = "No /home restore has happened since the Pause."

ROW_FIELDS = ("id", "key", "tid", "oid", "lab", "entra_name", "email", "display_name", "role", "status", "joined_at",
              "removed_at", "reason", "notice", "renewed", "retired", "deleted_at")


def refuse(status: int, code: str, message: str) -> HTTPException:
    """A refusal the SPA can act on: the code says which page to show."""
    return HTTPException(status, {"code": code, "message": message})


def _month(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _expiry(renewed: str) -> datetime:
    """A membership renewed in month M ends at the start of month M + 12."""
    y, m = (int(x) for x in renewed.split("-"))
    m += MEMBERSHIP_MONTHS
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return parse_iso(f"{y:04d}-{m:02d}-01T00:00:00+00:00")


def clean_name(value: str) -> str:
    """A display name: printable, one line, 2 to 40 characters."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] not in ("C",))
    text = re.sub(r"\s+", " ", text).strip()
    if not 2 <= len(text) <= 40:
        raise HTTPException(400, "A display name has 2 to 40 characters.")
    return text


@dataclass(frozen=True)
class Seat:
    """Who is asking, and in what capacity. `role` is "admin" (the owner), "learner" or None (signed in,
    not a member). `row` is the membership row of a member."""
    role: str | None
    learner: Learner | None
    principal: Principal | None
    row: dict | None = None

    @property
    def admin(self) -> bool:
        return self.role == "admin"


class Team:
    """Team mode's membership (ADR 0009): access requests and approval, the notice, roles, pause and end,
    leaving and removal, and deletion that stays deleted. Its sweep ends lapsed memberships and deletes
    removed members' data after the grace period."""
    def __init__(self, settings: Settings, store: Store, jobs: Any):
        self.settings = settings
        self.store = store
        self.jobs = jobs
        self.on_purge: list[Callable[[Learner], None]] = []   # further clean-up per learner (labs in PR 4)
        self.on_sweep: list[Callable[[], None]] = []          # further hourly work (budgets: old days go)
        # What must go the moment a membership ends (removal, leaving, expiry, the end of team mode), under the
        # Store lock, not after the 30 days: a removed member's place in shared spaces (ADR 0010, Play circles).
        self.on_removed: list[Callable[[Learner], None]] = []
        self._stamp: tuple | None = None
        self._doc: dict = {"members": [], "next": 1}
        self._sweeper_started = False
        # The mirror in the lab database (PR 4). None locally and with team mode off: the file is the only copy.
        self.mirror: Any = None
        self.on_synced: list[Callable[[set, set], Any]] = []   # labs: drop the schemas of deleted learners
        self._admit = threading.Event()   # members wait until the startup reconcile (or the wait) is over
        self._admit.set()
        self._kick = threading.Event()    # something changed: sync the mirror now
        self._sync_lock = threading.Lock()
        self._wait = STARTUP_WAIT
        self.sync: dict = {"state": "file"}
        self.owner_key = owner_key(settings)
        with store.lock:
            self._load_tombstones()
            self.repurge()

    # ---------------------------------------------------------------- files

    def members_doc(self) -> dict:
        """team/members.json, re-read when the file changes (a restore, or an edit by hand)."""
        p = self.store.path("team", "members.json")
        with self.store.lock:
            try:
                st = p.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                stamp = None
            if stamp != self._stamp:
                doc = self.store.read("team", "members", default=None) or {}
                rows = [r for r in doc.get("members") or [] if isinstance(r, dict) and r.get("key")]
                self._doc = {"members": rows, "next": int(doc.get("next") or len(rows) + 1)}
                self._stamp = stamp
            return self._doc

    def rows(self) -> list[dict]:
        return self.members_doc()["members"]

    def _save(self, doc: dict | None = None) -> None:
        with self.store.lock:
            doc = doc or self._doc
            self.store.write("team", "members", value={"members": doc["members"], "next": doc["next"]})
            self._stamp = None
            self.members_doc()

    def state(self) -> dict:
        return self.store.read("team", "state", default=None) or {}

    def _save_state(self, st: dict) -> None:
        self.store.write("team", "state", value=st)

    def _tomb_doc(self) -> dict:
        doc = self.store.read("team", "tombstones", default=None) or {}
        return {"epoch": int(doc.get("epoch") or 0), "tombstones": list(doc.get("tombstones") or []),
                "changes": list(doc.get("changes") or []), "retired": list(doc.get("retired") or [])}

    def _bump_epoch(self, change: dict | None = None) -> None:
        """Every change to the deletion list, the Pause state or a membership status moves the counter
        on by one. PR 4 mirrors it in the lab database, so a restore of /home can be told apart."""
        with self.store.lock:
            doc = self._tomb_doc()
            doc["epoch"] += 1
            if change:
                doc["changes"].append({**change, "epoch": doc["epoch"]})
            self.store.write("team", "tombstones", value=doc)
        self._kick.set()

    def _load_tombstones(self) -> None:
        doc = self._tomb_doc()
        self.store.set_tombstones({t["key"] for t in doc["tombstones"] if t.get("key")})

    def audit(self, action: str, who: str = "", member: str = "") -> None:
        """The admin action log: the action, the time and the display name concerned. No learner activity."""
        line = {"at": iso(), "action": action, "who": who}
        if member:
            line["member"] = sha256(member)[:16]
        self.store.append_line("team", "audit.jsonl", value=line)

    def _forget_in_audit(self, key: str) -> None:
        ref = sha256(key)[:16]
        with self.store.lock:
            lines = self.store.read_lines("team", "audit.jsonl")
            if not any(x.get("member") == ref for x in lines):
                return
            out = []
            for x in lines:
                if x.get("member") == ref:
                    x = {k: v for k, v in x.items() if k != "member"}
                    x["who"] = "a former member"
                out.append(x)
            self._rewrite_audit(out)

    def _rewrite_audit(self, lines: list[dict]) -> None:
        import json
        data = "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in lines).encode("utf-8")
        self.store.write_bytes("team", "audit.jsonl", data=data)

    def owner_doc(self) -> dict:
        return self.store.read("team", "owner", default=None) or {}

    # ---------------------------------------------------------------- who is asking

    def seat(self, request: Request) -> Seat:
        s = self.settings
        if s.local or not s.team:
            learner = current_learner(request)   # owner only, exactly as before team mode
            return Seat("admin", learner, None)
        p = read_principal(request)
        if p.tid != s.owner_tid:
            raise HTTPException(403, "This Dojo belongs to someone else.")
        if p.oid == s.owner_oid:
            return Seat("admin", Learner(f"{p.tid}:{p.oid}", self.owner_key, p.name or "Learner"), p)
        if not self._admit.is_set():
            # Before a /home restore is ruled out, nobody but the owner gets in (ADR 0009).
            raise HTTPException(503, "Dojo is starting, try again in a minute.", headers={"Retry-After": "60"})
        row = self.row_for(p.tid, p.oid)
        if row is None:
            return Seat(None, None, p)
        self._expire_if_due(row)
        self._end_if_due(row)
        return Seat("learner", Learner(f"{p.tid}:{p.oid}", row["key"], row.get("display_name") or "Member"), p, row)

    def row_for(self, tid: str, oid: str) -> dict | None:
        """The live membership row of this account. Rows whose data is deleted are history only."""
        for r in self.rows():
            if r.get("tid") == tid and r.get("oid") == oid and not r.get("deleted_at"):
                return r
        return None

    def row_by_id(self, row_id: str) -> dict:
        for r in self.rows():
            if r.get("id") == row_id and not r.get("deleted_at"):
                return r
        raise HTTPException(404, "No such member.")

    def paused(self) -> bool:
        return bool(self.state().get("paused"))

    def active_learners(self) -> list[Learner]:
        """Members who may use Dojo right now, the owner not included: admitted (the startup reconcile with
        the lab database is over), active, notice acknowledged, not paused. Empty with team mode off, and when members.json or the team state cannot be read, so
        nothing a member owns is served then; the owner is always handled apart (ADR 0009)."""
        if self.settings.local or not self.settings.team:
            return []
        if not self._admit.is_set():
            # Before the startup reconcile (or its wait) is over, a restored /home may still hold a member the
            # database knows is revoked, or miss a Pause: no member's token, feed or background work counts yet.
            return []
        now = utcnow()
        try:
            if self.paused() or self.ended(now):
                return []
            rows = list(self.rows())
            return [Learner(f"{r.get('tid')}:{r.get('oid')}", r["key"], r.get("display_name") or "Member") for r in rows
                    if r.get("status") == "active" and not r.get("deleted_at") and not self.lapsed(r, now)
                    and (r.get("notice") or {}).get("version") == NOTICE_VERSION and r.get("key") != self.owner_key]
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            log.warning("team members could not be read: %s", type(e).__name__)
            return []

    def ended(self, now: datetime | None = None) -> bool:
        """Team mode has reached its end date: no member is admitted, even before the sweeper has run."""
        end = (self.state().get("end") or {}).get("date")
        return bool(end) and (now or utcnow()).date().isoformat() >= end

    @staticmethod
    def lapsed(row: dict, now: datetime | None = None) -> bool:
        """The membership's 12 months are over, even if the row still says active (the sweeper is hourly)."""
        return bool(row.get("renewed")) and (now or utcnow()) >= _expiry(row["renewed"])

    def active_member(self, key: str) -> Learner | None:
        """For background work done for a member without a request (the nudges of ADR 0013): the member
        if they could use Dojo right now - active, notice accepted, team mode on and not paused."""
        now = utcnow()
        if self.settings.local or not self.settings.team or not self._admit.is_set() or self.paused() or self.ended(now):
            return None
        for r in self.rows():
            if r.get("key") == key and not r.get("deleted_at") and self.status_of(r) == "active" and not self.lapsed(r, now):
                return Learner(f"{r.get('tid')}:{r.get('oid')}", key, r.get("display_name") or "Member")
        return None

    def in_grace(self, row: dict) -> bool:
        if row.get("status") != "removed" or row.get("deleted_at"):
            return False
        at = row.get("removed_at")
        return bool(at) and utcnow() < parse_iso(at) + timedelta(days=GRACE_DAYS)

    def exit_seat(self, request: Request) -> Seat:
        """The learner_exit class: the owner, members (active, before the notice, paused, ending) and
        removed members within their 30 days. They may read /api/me, export, delete and leave."""
        seat = self.seat(request)
        if seat.admin:
            return seat
        if seat.row is None:
            raise refuse(403, "not_member", "You are not a member of this Dojo yet.")
        if seat.row.get("status") == "active" or self.in_grace(seat.row):
            return seat
        raise refuse(403, "not_member", "You are not a member of this Dojo.")

    def active_seat(self, request: Request) -> Seat:
        """The learner class: the owner, and active members who acknowledged the notice, while not paused."""
        seat = self.exit_seat(request)
        if seat.admin:
            return seat
        row = seat.row
        if row.get("status") != "active":
            raise refuse(403, "removed", f"Your access to this Dojo ended on {str(row.get('removed_at') or '')[:10]}.")
        if self.paused():
            raise refuse(403, "paused", "This Dojo is paused for members. You can still download or delete your record.")
        if (row.get("notice") or {}).get("version") != NOTICE_VERSION:
            raise refuse(403, "notice", "Read and acknowledge the notice first.")
        return seat

    def admin_seat(self, request: Request) -> Seat:
        seat = self.seat(request)
        if not seat.admin:
            if seat.row is None:
                raise refuse(403, "not_member", "You are not a member of this Dojo yet.")
            raise refuse(403, "admin_only", "Only the owner of this Dojo can do this.")
        return seat

    # ---------------------------------------------------------------- access requests

    def requests_doc(self) -> list[dict]:
        doc = self.store.read("team", "requests", default=None) or {}
        return [r for r in doc.get("requests") or [] if isinstance(r, dict)]

    def _save_requests(self, reqs: list[dict]) -> None:
        self.store.write("team", "requests", value={"requests": reqs})

    def access(self, seat: Seat) -> dict:
        if seat.role is not None:
            return {"member": True}
        p = seat.principal
        mine = next((r for r in self.requests_doc() if r.get("tid") == p.tid and r.get("oid") == p.oid), None)
        return {"member": False, "shown_as": p.name or p.email or "A colleague",
                "request": {"state": mine["state"], "at": mine["at"][:10], "used_before": mine.get("used_before", False)} if mine else None}

    def request_access(self, seat: Seat, used_before: bool) -> dict:
        if seat.role is not None:
            raise HTTPException(409, "You are already a member of this Dojo.")
        p = seat.principal
        with self.store.lock:
            reqs = self.requests_doc()
            mine = next((r for r in reqs if r.get("tid") == p.tid and r.get("oid") == p.oid), None)
            if mine and mine.get("state") == "declined":
                raise HTTPException(409, "The owner declined your request. Ask the owner directly if you think this is a mistake.")
            if mine is None:
                if len([r for r in reqs if r.get("state") == "open"]) >= 50:
                    raise HTTPException(429, "Too many people are waiting for access. Try again later.")
                mine = {"id": new_id("req"), "tid": p.tid, "oid": p.oid, "state": "open"}
                reqs.append(mine)
            mine.update(name=p.name, email=p.email, at=iso(), used_before=bool(used_before))
            self._save_requests(reqs)
        return self.access(seat)

    def _take_request(self, req_id: str) -> dict:
        reqs = self.requests_doc()
        req = next((r for r in reqs if r.get("id") == req_id and r.get("state") == "open"), None)
        if req is None:
            raise HTTPException(404, "No such open request.")
        self._save_requests([r for r in reqs if r is not req])
        return req

    # ---------------------------------------------------------------- the owner's Members page

    @staticmethod
    def status_of(row: dict) -> str:
        if row.get("status") == "active" and (row.get("notice") or {}).get("version") != NOTICE_VERSION:
            return "pending"
        return row.get("status") or "removed"

    def members_view(self) -> dict:
        """Only what admission needs. No activity, cost, counts or learning data (ADR 0009)."""
        od = self.owner_doc()
        rows = [{"id": "owner", "display_name": od.get("display_name") or "Owner", "entra_name": od.get("entra_name") or "",
                 "email": "", "role": "admin", "status": "active", "joined": None}]
        rows += [{"id": r["id"], "display_name": r.get("display_name") or "", "entra_name": r.get("entra_name") or "",
                  "email": r.get("email") or "", "role": "learner", "status": self.status_of(r), "joined": (r.get("joined_at") or "")[:10]}
                 for r in self.rows() if not r.get("deleted_at")]
        reqs = [{"id": r["id"], "name": r.get("name") or "", "email": r.get("email") or "",
                 "at": (r.get("at") or "")[:10], "used_before": bool(r.get("used_before"))}
                for r in self.requests_doc() if r.get("state") == "open"]
        st = self.state()
        return {"team": self.settings.team, "members": rows, "requests": reqs,
                "rebind_candidates": [{"id": r["id"], "display_name": r.get("display_name") or "", "entra_name": r.get("entra_name") or "",
                                       "email": r.get("email") or ""} for r in self.rows()
                                      if not r.get("deleted_at") and (r.get("status") == "active" or self.in_grace(r))],
                "paused": bool(st.get("paused")), "end": self._end_view(st), "max_members": MAX_MEMBERS,
                "suspended": self.suspended_count(), "sync": self.sync_view(st)}

    def _counted(self) -> int:
        return sum(1 for r in self.rows() if not r.get("deleted_at") and (r.get("status") == "active" or self.in_grace(r)))

    def approve(self, req_id: str) -> dict:
        with self.store.lock:
            if self._counted() >= MAX_MEMBERS:
                raise HTTPException(409, f"This Dojo supports up to {MAX_MEMBERS} members.")
            if self.state().get("end"):
                raise HTTPException(409, "Team mode is ending. No new members can join.")
            req = self._take_request(req_id)
            if self.row_for(req["tid"], req["oid"]):
                raise HTTPException(409, "This person is already a member.")
            key = self._fresh_key(req["tid"], req["oid"])
            doc = self.members_doc()
            n = doc["next"]
            now = utcnow()
            row = {"id": new_id("mem"), "key": key, "tid": req["tid"], "oid": req["oid"], "lab": self._fresh_surrogate(),
                   "entra_name": req.get("name") or "", "email": req.get("email") or "", "display_name": f"Member {n}",
                   "role": "learner", "status": "active", "joined_at": iso(now), "removed_at": None, "reason": None,
                   "notice": None, "renewed": _month(now), "retired": []}
            self._save({"members": doc["members"] + [row], "next": n + 1})
            self._bump_epoch({"member": sha256(key), "status": "active", "reason": "approved", "date": iso(now)[:10]})
            self.audit("approve", row["display_name"], key)
        return self.members_view()

    def _fresh_key(self, tid: str, oid: str) -> str:
        """learner_key(tid:oid), as for the owner. A guest object that left before gets a generation
        suffix, so a returning member starts fresh and never meets a deleted folder."""
        key, n = learner_key(f"{tid}:{oid}"), 1
        used = {r["key"] for r in self.rows()}
        while self.store.tombstoned(key) or key in used or key == self.owner_key:
            n += 1
            key = learner_key(f"{tid}:{oid}:{n}")
        return key

    def _fresh_surrogate(self) -> str:
        """A lab surrogate nobody uses and no tombstone names (ADR 0009, "Labs"): random, keyed to nothing."""
        used = {r.get("lab") for r in self.rows()} | {self.owner_doc().get("lab_surrogate")}
        gone = {t.get("lab") for t in self._tomb_doc()["tombstones"]}
        while True:
            s = new_surrogate()
            if s not in used and sha256(s) not in gone:
                return s

    def lab_surrogate(self, key: str) -> str:
        """The member's lab surrogate, from their row. A row from before PR 4 gets a new one, once."""
        with self.store.lock:
            for r in self.rows():
                if r.get("key") == key and not r.get("deleted_at"):
                    s = r.get("lab")
                    if not (isinstance(s, str) and SURROGATE.fullmatch(s)):
                        r["lab"] = s = self._fresh_surrogate()
                        self._save()
                    return s
        raise refuse(403, "not_member", "You are not a member of this Dojo.")

    def lab_of(self, key: str) -> str | None:
        for r in self.rows():
            if r.get("key") == key:
                return r.get("lab")
        return None

    def known_surrogates(self) -> set[str]:
        """Every surrogate some learner still has: the owner's and those of rows not yet deleted."""
        known = {r.get("lab") for r in self.rows() if not r.get("deleted_at") and r.get("lab")}
        own = self.owner_doc().get("lab_surrogate")
        return {s for s in known | {own} if isinstance(s, str) and s}

    def decline(self, req_id: str) -> dict:
        with self.store.lock:
            reqs = self.requests_doc()
            req = next((r for r in reqs if r.get("id") == req_id and r.get("state") == "open"), None)
            if req is None:
                raise HTTPException(404, "No such open request.")
            # `at` stays the asking time: the 30-day deletion runs from the ask, not from the decline.
            req.update(state="declined", declined_at=iso())
            self._save_requests(reqs)
            self.audit("decline", "a request")
        return self.members_view()

    def remove(self, row_id: str, reason: str = "removed") -> dict:
        if row_id == "owner":
            raise HTTPException(409, "The owner cannot be removed.")
        with self.store.lock:
            row = self.row_by_id(row_id)
            if row.get("status") != "active":
                raise HTTPException(409, "This member was already removed.")
            self._set_removed(row, reason, utcnow())
            self._save()
            self.audit("remove", row.get("display_name") or "", row["key"])
        return self.members_view()

    def _set_removed(self, row: dict, reason: str, when: datetime) -> None:
        row.update(status="removed", reason=reason, removed_at=iso(when))
        self._bump_epoch({"member": sha256(row["key"]), "status": "removed", "reason": reason, "date": iso(when)[:10]})
        # The push endpoints are capability URLs and a removed member gets no nudge: they go at once, not
        # with the rest of the data after the grace period (ADR 0013).
        self.store.delete("learners", row["key"], "push")
        learner = Learner(f"{row.get('tid')}:{row.get('oid')}", row["key"], row.get("display_name") or "")
        for fn in self.on_removed:
            try:
                fn(learner)
            except Exception:  # noqa: BLE001 - the removal itself must still happen
                log.exception("a clean-up step after a removal failed")

    def rebind(self, req_id: str, row_id: str, checked: bool) -> dict:
        """The owner checked out of band that the new account is the same person: the new tid and oid go
        into the old row. The key, the folder and the chain stay as they are; nothing is copied."""
        if not checked:
            raise HTTPException(400, "Confirm that you checked with the person directly.")
        with self.store.lock:
            reqs = self.requests_doc()
            req = next((r for r in reqs if r.get("id") == req_id and r.get("state") == "open"), None)
            if req is None or not req.get("used_before"):
                raise HTTPException(404, "No such request marked \"used Dojo before\".")
            row = self.row_by_id(row_id)
            if not (row.get("status") == "active" or self.in_grace(row)):
                raise HTTPException(409, "This member's data was already deleted, so there is nothing to re-bind.")
            if self.row_for(req["tid"], req["oid"]):
                raise HTTPException(409, "The new account is already a member.")
            row["retired"] = list(row.get("retired") or []) + [sha256(f"{row['tid']}:{row['oid']}")]
            row.update(tid=req["tid"], oid=req["oid"], email=req.get("email") or row.get("email") or "",
                       entra_name=req.get("name") or row.get("entra_name") or "")
            if row.get("status") != "active":
                row.update(status="active", reason=None, removed_at=None)
            self._bump_epoch({"member": sha256(row["key"]), "status": "active", "reason": "rebound", "date": iso()[:10]})
            self._save()
            self._save_requests([r for r in reqs if r is not req])
            self.audit("rebind", row.get("display_name") or "", row["key"])
        return self.members_view()

    # ---------------------------------------------------------------- the member's own controls

    def acknowledge(self, seat: Seat, version: int) -> dict:
        if seat.admin:
            return {"acknowledged": True}
        if version != NOTICE_VERSION:
            raise HTTPException(409, "The notice changed. Read it again.")
        with self.store.lock:
            row = self.row_by_id(seat.row["id"])
            if row.get("status") != "active":
                raise refuse(403, "removed", "Your access to this Dojo ended.")
            row["notice"] = {"version": NOTICE_VERSION, "at": iso()}
            self._save()
            # The opt-in folder for a later social feature (ADR 0010). Empty in Team.
            self.store.path("learners", row["key"], "social").mkdir(parents=True, exist_ok=True)
        return {"acknowledged": True}

    def set_display_name(self, seat: Seat, name: str) -> dict:
        name = clean_name(name)
        with self.store.lock:
            others = [r.get("display_name") or "" for r in self.rows()
                      if not r.get("deleted_at") and (seat.row is None or r["id"] != seat.row["id"])]
            if not seat.admin:
                others.append(self.owner_doc().get("display_name") or "")
            if name.casefold() in {o.casefold() for o in others if o}:
                raise HTTPException(409, "Someone in this Dojo already uses that name. Choose another.")
            if seat.admin:
                self.store.write("team", "owner", value={**self.owner_doc(), "display_name": name})
            else:
                row = self.row_by_id(seat.row["id"])
                row["display_name"] = name
                self._save()
        return {"display_name": name}

    def renew(self, seat: Seat) -> dict:
        if seat.admin:
            return {"renewed": True}
        with self.store.lock:
            row = self.row_by_id(seat.row["id"])
            if row.get("status") != "active":
                raise refuse(403, "removed", "Your access to this Dojo ended.")
            row["renewed"] = _month(utcnow())
            self._save()
        return {"renewed": True, "until": _expiry(row["renewed"]).date().isoformat()}

    def renew_by(self, row: dict | None, now: datetime | None = None) -> str | None:
        """The end of the membership, if it falls in the next 30 days: the member is asked once."""
        if not row or row.get("status") != "active" or not row.get("renewed"):
            return None
        end = _expiry(row["renewed"])
        now = now or utcnow()
        return end.date().isoformat() if end - timedelta(days=RENEW_WINDOW_DAYS) <= now < end else None

    def _expire_if_due(self, row: dict, now: datetime | None = None) -> None:
        if not self.settings.team or row.get("status") != "active" or not row.get("renewed"):
            return
        end = _expiry(row["renewed"])
        if (now or utcnow()) >= end:
            with self.store.lock:
                live = self.row_by_id(row["id"])
                if live.get("status") == "active":
                    self._set_removed(live, "expired", end)
                    self._save()
                    row.update(live)

    def _end_if_due(self, row: dict, now: datetime | None = None) -> None:
        """After the end date a member is removed at their next request, not only at the next sweep."""
        if not self.settings.team or row.get("status") != "active" or not self.ended(now):
            return
        with self.store.lock:
            live = self.row_by_id(row["id"])
            if live.get("status") == "active":
                self._set_removed(live, "team-ended", now or utcnow())
                self._save()
                row.update(live)

    def leave(self, seat: Seat) -> dict:
        if seat.admin:
            raise HTTPException(409, "The owner cannot leave this Dojo.")
        with self.store.lock:
            row = self.row_by_id(seat.row["id"])
            if row.get("status") == "active":
                self._set_removed(row, "left", utcnow())
            self._purge(row)
            self._save()
        return {"left": True}

    def _purge(self, row: dict) -> None:
        """Delete a member's data, in the order the ADR gives: the deletion list first, so nothing still
        running can write back; then the jobs, the folder, the job documents and the membership details."""
        key = row["key"]
        learner = Learner(f"{row.get('tid')}:{row.get('oid')}", key, row.get("display_name") or "")
        with self.store.lock:
            doc = self._tomb_doc()
            doc["tombstones"].append({"key": sha256(key), "lab": sha256(row.get("lab") or ""), "at": iso()[:10]})
            doc["epoch"] += 1
            doc["changes"].append({"member": sha256(key), "status": "removed", "reason": row.get("reason") or "removed",
                                   "date": iso()[:10], "epoch": doc["epoch"]})
            self.store.write("team", "tombstones", value=doc)
            self.store.tombstone(key)
            self._kick.set()                # the mirror records it, then the member's lab schemas go
            self.jobs.remove_for(learner)   # queued jobs never start; running ones are abandoned
            self.store.remove_tree("learners", key)
            for fn in self.on_purge:
                try:
                    fn(learner)
                except Exception:  # noqa: BLE001 - the rest of the deletion must still happen
                    log.exception("a clean-up step after a deletion failed")
            self._strip(row)

    def _strip(self, row: dict) -> None:
        """What stays of a deleted member until the tombstone expires: status, dates and hashes."""
        key = row["key"]
        if row.get("tid") or row.get("oid"):
            row["principal"] = sha256(f"{row.get('tid')}:{row.get('oid')}")
        for field in ("tid", "oid", "entra_name", "email", "display_name", "notice", "renewed", "lab"):
            row.pop(field, None)
        row["deleted_at"] = iso()
        self._forget_in_audit(key)

    # ---------------------------------------------------------------- pause and ending team mode

    def pause(self, on: bool, without_database: bool = False, no_restore: bool = False) -> dict:
        """Pause is mirrored in the lab database. Resume needs a successful reconcile with it first, so a
        restored team/state.json cannot let members back in. "Resume without the database" is the owner's
        logged override for a long outage, behind a second confirmation (ADR 0009)."""
        mirrored = self.mirror is not None and self.settings.team
        if not on and mirrored and self.paused():
            if without_database:
                if not no_restore:
                    raise HTTPException(400, f"Confirm: {NO_RESTORE}")
            else:
                try:
                    self.reconcile()
                except Exception as e:  # noqa: BLE001 - any failure to reach the database refuses the Resume
                    log.warning("resume refused: the lab database did not answer: %s", type(e).__name__)
                    raise refuse(409, "needs_database", "Resume needs the lab database, and it did not answer. Try again "
                                                        "in a minute, or resume without the database if no /home restore "
                                                        "has happened since the Pause.")
        with self.store.lock:
            st = self.state()
            if bool(st.get("paused")) == on:
                return self.members_view()
            st["paused"] = on
            self._bump_epoch()
            st["paused_epoch" if on else "resumed_epoch"] = self._tomb_doc()["epoch"]
            self._save_state(st)
            self.audit("pause" if on else ("resume without the database: " + NO_RESTORE if without_database and mirrored
                                           else "resume"))
        if not on and mirrored and not without_database:
            try:
                self.reconcile()   # the database learns of the Resume now, not at the next sync
            except Exception as e:  # noqa: BLE001 - the background sync tries again
                log.warning("the lab database did not take the Resume yet: %s", type(e).__name__)
        return self.members_view()

    def _end_view(self, st: dict) -> dict | None:
        end = st.get("end")
        if not end:
            return None
        return {"told_at": (end.get("told_at") or "")[:10] or None, "date": end.get("date"),
                "earliest": self._earliest_end(end)}

    @staticmethod
    def _earliest_end(end: dict) -> str | None:
        told = end.get("told_at")
        return (parse_iso(told) + timedelta(days=END_NOTICE_DAYS)).date().isoformat() if told else None

    def end_emails(self) -> dict:
        """Dojo cannot send mail (EG-5), so the owner tells every member himself. These are the addresses."""
        return {"emails": [r.get("email") or "" for r in self.rows() if not r.get("deleted_at") and r.get("status") == "active"],
                "end": self._end_view(self.state())}

    def end_told(self, confirmed: bool) -> dict:
        if not confirmed:
            raise HTTPException(400, "Confirm that you have told every member.")
        with self.store.lock:
            st = self.state()
            st["end"] = {"told_at": iso(), "date": None}
            self._save_state(st)
            self.audit("told every member")
        return self.members_view()

    def end_on(self, day: str) -> dict:
        with self.store.lock:
            st = self.state()
            end = st.get("end") or {}
            if not end.get("told_at"):
                raise HTTPException(409, "First tell every member, then confirm it here.")
            try:
                d = date.fromisoformat(day)
            except ValueError:
                raise HTTPException(400, "The end date must look like 2027-03-01.")
            if d.isoformat() < self._earliest_end(end):
                raise HTTPException(409, f"The end date must be at least {END_NOTICE_DAYS} days after you told every member: "
                                         f"{self._earliest_end(end)} or later.")
            end["date"] = d.isoformat()
            st["end"] = end
            self._save_state(st)
            self.audit("end team mode")
        return self.members_view()

    def end_cancel(self) -> dict:
        with self.store.lock:
            st = self.state()
            if st.pop("end", None) is not None:
                self._save_state(st)
                self.audit("end cancelled")
        return self.members_view()

    def banner(self, seat: Seat) -> dict | None:
        st = self.state()
        if seat.admin and self.suspended_count():
            n = self.suspended_count()
            return {"kind": "suspended", "text": f"Team mode is off but {n} member{'s' if n != 1 else ''} still "
                                                 f"{'have' if n != 1 else 'has'} data. Turn it back on, or turn it on and end team mode."}
        if not self.settings.team:
            return None
        end = st.get("end") or {}
        if end.get("date"):
            return {"kind": "ending", "date": end["date"],
                    "text": f"Team mode in this Dojo ends on {end['date']}. Until then everything works. Download your record "
                            f"before that day; afterwards your data is deleted."}
        if st.get("paused") and not seat.admin:
            return {"kind": "paused", "text": "This Dojo is paused for members. Nothing is deleted. You can still download "
                                              "or delete your record."}
        if st.get("paused"):
            return {"kind": "paused", "text": "Paused for members. Your own use is not affected."}
        return None

    def suspended_count(self) -> int:
        """Members who still have data while team mode is off: a suspension, nothing is deleted."""
        if self.settings.team:
            return 0
        return sum(1 for r in self.rows() if not r.get("deleted_at"))

    # ---------------------------------------------------------------- the sweeper

    def sweep(self, now: datetime | None = None) -> dict:
        """Expire memberships, end team mode on its date, delete removed members' data after 30 days,
        drop old requests, tombstones and audit lines. With team mode off nothing of members is deleted
        (a suspension)."""
        now = now or utcnow()
        done = {"expired": 0, "deleted": 0}
        with self.store.lock:
            if self.settings.team:
                end = (self.state().get("end") or {}).get("date")
                ended = bool(end) and now.date().isoformat() >= end
                for row in self.rows():
                    if row.get("deleted_at"):
                        continue
                    if ended and row.get("status") == "active":
                        self._set_removed(row, "team-ended", now)
                    self._expire_if_due(row, now)
                    if row.get("status") == "removed" and (
                            row.get("reason") == "team-ended" or now >= parse_iso(row["removed_at"]) + timedelta(days=GRACE_DAYS)):
                        self._purge(row)
                        done["deleted"] += 1
                    elif row.get("reason") == "expired":
                        done["expired"] += 1
                self._save()
            cutoff = iso(now - timedelta(days=REQUEST_DAYS))
            reqs = self.requests_doc()
            kept = [r for r in reqs if (r.get("at") or "") >= cutoff]
            if len(kept) != len(reqs):
                self._save_requests(kept)
            self._expire_tombstones(now)
            lines = self.store.read_lines("team", "audit.jsonl")
            old = iso(now - timedelta(days=AUDIT_DAYS))
            if any((x.get("at") or "") < old for x in lines):
                self._rewrite_audit([x for x in lines if (x.get("at") or "") >= old])
        return done

    def _expire_tombstones(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=TOMBSTONE_DAYS)).date().isoformat()
        doc = self._tomb_doc()
        gone = {t["key"] for t in doc["tombstones"] if (t.get("at") or "") < cutoff}
        if not gone:
            return
        doc["tombstones"] = [t for t in doc["tombstones"] if t["key"] not in gone]
        doc["changes"] = [c for c in doc["changes"] if (c.get("date") or "") >= cutoff]
        doc["retired"] = [r for r in doc["retired"] if (r.get("at") or "") >= cutoff]
        self.store.write("team", "tombstones", value=doc)
        self.store.set_tombstones({t["key"] for t in doc["tombstones"]})
        # The bare row (status, dates, hashes) goes with its tombstone.
        rows = [r for r in self.rows() if not (r.get("deleted_at") and sha256(r["key"]) in gone)]
        if len(rows) != len(self.rows()):
            self._save({"members": rows, "next": self.members_doc()["next"]})

    def repurge(self) -> int:
        """At start: delete again whatever a restore of /home brought back for a key on the deletion list:
        the learner folder, job documents and the membership details."""
        tombs = {t["key"] for t in self._tomb_doc()["tombstones"]}
        if not tombs:
            return 0
        n = 0
        learners = self.store.path("learners")
        if learners.is_dir():
            for folder in learners.iterdir():
                if folder.is_dir() and sha256(folder.name) in tombs:
                    self.store.remove_tree("learners", folder.name)
                    n += 1
        for name in self.store.names("jobs"):
            job = self.store.read("jobs", name)
            if isinstance(job, dict) and sha256(str(job.get("learner") or "")) in tombs:
                self.store.delete("jobs", name)
        changed = False
        for row in self.rows():
            if sha256(row["key"]) in tombs and not row.get("deleted_at"):
                row.update(status="removed", reason=row.get("reason") or "removed", removed_at=row.get("removed_at") or iso())
                self._strip(row)
                changed = True
        if changed:
            self._save()
        if n:
            log.warning("deleted %d learner folders again that a restore had brought back", n)
        return n

    # ---------------------------------------------------------------- the mirror in the lab database

    def attach_mirror(self, mirror: Any, wait: float = STARTUP_WAIT) -> None:
        """Team mode with a lab database: members wait for the startup reconcile, up to `wait` seconds."""
        self.mirror = mirror
        self._wait = wait
        self._admit.clear()
        self.sync = {"state": "waiting"}

    def admitting(self) -> bool:
        return self._admit.is_set()

    def reconcile(self) -> dict:
        """Bring the file and the database together (ADR 0009): the union of the deletion lists, the
        membership changes and the retired principals, the higher epoch, Pause if either copy says so
        (unless the file holds a later Resume). If the file was behind (a /home restore), the tombstoned
        keys are purged and newer revocations applied before any member is admitted. Then both copies
        hold the same, and the lab schemas of deleted learners are dropped."""
        if self.mirror is None:
            return {}
        with self._sync_lock:
            db = self.mirror.read()
            with self.store.lock:
                out = self._merge(db)
            cutoff = (utcnow() - timedelta(days=TOMBSTONE_DAYS)).date().isoformat()
            self.mirror.write(out, cutoff)
            self.sync = {"state": "synced", "at": iso(), "db_epoch": out["epoch"], "restored": out["restored"],
                         "unknown": self.sync.get("unknown", 0)}
            self._admit.set()
        if out["restored"]:
            log.warning("the deletion list in the lab database was ahead of team/tombstones.json: a restore of /home "
                        "was reconciled before members were admitted")
        tombed = {t.get("lab") for t in out["tombstones"] if t.get("lab")}
        known = self.known_surrogates()
        for fn in self.on_synced:
            try:
                res = fn(tombed, known)
                if isinstance(res, dict) and "unknown" in res:
                    self.sync["unknown"] = len(res["unknown"])
            except Exception as e:  # noqa: BLE001 - the next sync tries again
                log.warning("dropping deleted learners' lab schemas failed: %s", type(e).__name__)
        return out

    def _merge(self, db: dict) -> dict:
        now = utcnow()
        today = now.date().isoformat()
        cutoff = (now - timedelta(days=TOMBSTONE_DAYS)).date().isoformat()
        doc, st = self._tomb_doc(), self.state()
        f_epoch, d_epoch = doc["epoch"], int(db.get("epoch") or 0)
        tombs: dict[str, dict] = {}
        for t in list(db.get("tombstones") or []) + doc["tombstones"]:
            if t.get("key") and (t.get("at") or "") >= cutoff:
                old = tombs.get(t["key"])
                if old is None or (t.get("at") or "") < (old.get("at") or ""):
                    tombs[t["key"]] = {"key": t["key"], "lab": t.get("lab") or (old or {}).get("lab") or "", "at": t.get("at")}
        seen, changes = set(), []
        for c in doc["changes"] + list(db.get("changes") or []):
            k = (c.get("member"), c.get("status"), c.get("reason"), c.get("date"), int(c.get("epoch") or 0))
            if c.get("member") and (c.get("date") or "") >= cutoff and k not in seen:
                seen.add(k)
                changes.append({"member": k[0], "status": k[1], "reason": k[2], "date": k[3], "epoch": k[4]})
        changes.sort(key=lambda c: (c["date"] or "", c["epoch"]))
        listed = {h for r in self.rows() for h in r.get("retired") or []}
        retired: dict[str, str] = {}
        for r in list(db.get("retired") or []) + doc["retired"]:
            if r.get("principal") and ((r.get("at") or "") >= cutoff or r["principal"] in listed):
                retired[r["principal"]] = min(retired.get(r["principal"], r.get("at") or today), r.get("at") or today)
        for h in listed:
            retired.setdefault(h, today)
        doc.update(epoch=max(f_epoch, d_epoch), tombstones=sorted(tombs.values(), key=lambda t: t.get("at") or ""),
                   changes=changes, retired=[{"principal": h, "at": d} for h, d in sorted(retired.items())])
        self.store.write("team", "tombstones", value=doc)
        self.store.set_tombstones(set(tombs))
        self.repurge()   # folders, job documents and rows of every tombstoned key, before anyone is admitted
        latest = {c["member"]: c for c in changes}
        changed = False
        for row in self.rows():
            if row.get("deleted_at") or row.get("key") == self.owner_key:
                continue
            c = latest.get(sha256(row["key"]))
            if row.get("status") == "active" and c and c["status"] == "removed":
                self._apply_revocation(row, c["reason"] or "removed", c["date"])
                changed = True
            elif row.get("status") == "active" and row.get("tid") and sha256(f"{row['tid']}:{row['oid']}") in retired:
                self._set_removed(row, "rebound", now)
                changed = True
            if row.get("status") == "removed" and (
                    row.get("reason") == "team-ended" or now >= parse_iso(row["removed_at"]) + timedelta(days=GRACE_DAYS)):
                self._purge(row)
                changed = True
        if changed:
            self._save()
        paused = bool(st.get("paused")) or (bool(db.get("paused")) and int(st.get("resumed_epoch") or 0) <= d_epoch)
        if paused != bool(st.get("paused")):
            st = self.state()
            st["paused"] = paused
            self._save_state(st)
            self.audit("pause restored from the lab database")
        final = self._tomb_doc()
        return {"epoch": final["epoch"], "paused": paused, "tombstones": final["tombstones"], "changes": final["changes"],
                "retired": final["retired"], "restored": f_epoch < d_epoch}

    def _apply_revocation(self, row: dict, reason: str, day: str) -> None:
        """A revocation the database knows and a restored file does not: applied with its original date,
        so the 30 days count from then. No new change: it is already in both lists."""
        when = parse_iso(f"{day}T00:00:00+00:00")
        row.update(status="removed", reason=reason, removed_at=iso(when))
        self.store.delete("learners", row["key"], "push")
        learner = Learner(f"{row.get('tid')}:{row.get('oid')}", row["key"], row.get("display_name") or "")
        for fn in self.on_removed:
            try:
                fn(learner)
            except Exception:  # noqa: BLE001
                log.exception("a clean-up step after a restored revocation failed")
        self.audit("revocation restored from the lab database", row.get("display_name") or "", row["key"])

    def _open_unsynced(self) -> None:
        if not self._admit.is_set():
            self.sync = {**self.sync, "state": "unsynced"}
            log.warning("the lab database did not answer within %d s: members are admitted from team/tombstones.json",
                        int(self._wait))
            self._admit.set()

    def _db_loop(self) -> None:
        while True:
            self._kick.clear()
            wait = None
            try:
                self.reconcile()
            except Exception as e:  # noqa: BLE001 - never let the loop die
                log.warning("the lab database did not answer for the deletion list: %s", type(e).__name__)
                if self._admit.is_set():
                    self.sync = {**self.sync, "state": "unsynced"}
                wait = 10 if not self._admit.is_set() else RETRY_SECONDS
            # Synced: nothing until something changes, so the mirror never wakes the database on its own.
            self._kick.wait(timeout=wait)

    def sync_view(self, st: dict | None = None) -> dict:
        st = st if st is not None else self.state()
        state = self.sync.get("state", "file")
        text = {
            "file": "No lab database: the deletion list lives in team/tombstones.json only.",
            "waiting": "Waiting for the lab database (up to 90 seconds) before members are admitted.",
            "synced": "Tombstones synced with the lab database.",
            "unsynced": "Tombstones not synced: the lab database did not answer. Members are admitted from the file; "
                        "Dojo tries again every 5 minutes.",
        }[state]
        recorded = bool(st.get("paused")) and state == "synced" and int(self.sync.get("db_epoch") or 0) >= int(st.get("paused_epoch") or 0)
        return {"state": state, "text": text, "at": (self.sync.get("at") or "")[:16] or None,
                "pause_recorded": recorded, "unknown_lab_schemas": int(self.sync.get("unknown") or 0)}

    def start(self) -> None:
        if self._sweeper_started:
            return
        self._sweeper_started = True
        if self.mirror is not None:
            threading.Thread(target=self._db_loop, name="team-mirror", daemon=True).start()
            gate = threading.Timer(self._wait, self._open_unsynced)
            gate.daemon = True
            gate.start()
        threading.Thread(target=self._loop, name="team-sweeper", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("the team sweeper failed")
            for fn in self.on_sweep:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("an hourly team task failed")
            time.sleep(SWEEP_SECONDS)


# ---------------------------------------------------------------- the first sign-in notice
# The same facts as docs/privacy.md, in the member's words. A change here that changes a fact is a new
# NOTICE_VERSION, so every member is asked again.

def notice_view(team: "Team", models: dict[str, str], labs: bool = True) -> dict:
    od = team.owner_doc()
    operator = od.get("display_name") or od.get("entra_name") or "the owner of this Dojo"
    names = ", ".join(f"{v} ({k})" for k, v in models.items())
    stored = f"On this web app's own storage {where_stored()}, in your own folder. " + (
        "Lab tables live in one Azure SQL database in the same region." if labs else "This Dojo has no lab database.")
    sections = [
        ("Who runs this Dojo",
         f"This Dojo is run by {operator}. They approved your access and can remove it. They are Global "
         "Administrator of the Microsoft tenant you sign in to and Owner of the Azure subscription Dojo runs in."),
        ("What the operator can see",
         "As operator they can technically read everything Dojo stores, including your answers and record, and "
         "you cannot see when they do. Their written commitment: they do not read members' stored data, and if a "
         "support case ever needs it, they ask you first. A commitment is not a technical control."),
        ("What the owner's pages show about you",
         "Your display name, your name and email from sign-in, your role, your status and the date you joined. "
         "Nothing else: no activity, no results, no costs, no plan, no labs."),
        ("Never for performance",
         "Dojo data is never used for performance reviews, staffing or HR decisions, and nothing is shared with "
         "managers. Social features, if this Dojo ever has them, are opt-in, off by default, and leaving one "
         "deletes its data. Your record has its own hash chain; no chain spans two learners."),
        ("Where your data is stored", stored),
        ("Which AI models read what you write",
         f"Dojo's AI models are {names}, hosted by Microsoft in the operator's Azure resource. Dojo adds no name, "
         "email or account id to what it sends. But anything you type or say in an answer, a question or a "
         "dispute goes to them as you wrote it, so do not put anything there you would not want processed. The "
         "deployments may process a request in any Azure region, including outside the EU."),
        ("How long it is kept",
         "Until you delete it or leave. If your access is removed or expires, your data is deleted 30 days later. "
         "A membership lasts 12 months; you are asked once, near the end, whether to keep it. Deleted data can "
         "stay in the web app's automatic backups for up to 30 days, and lab tables in the database's backups "
         "for up to 7 days. Dojo cannot remove one person from those backups."),
        ("Your controls",
         "Download my record gives you everything Dojo holds about your learning, as one file. Delete my record "
         "starts over and keeps your membership. Leave Dojo and delete my data ends your membership and deletes "
         "your whole folder at once. All three keep working even if your access is removed, for 30 days."),
    ]
    return {"version": NOTICE_VERSION, "operator": operator,
            "sections": [{"title": t, "text": x} for t, x in sections]}


# ---------------------------------------------------------------- the five route classes (ADR 0009)
# Each is async on purpose: it runs in the request's own context, so the writer it binds (whose data
# this request may write, and its generation) reaches the route that runs after it.

async def _seat(request: Request, how: str) -> Seat:
    """Read the seat in the thread pool (it reads files under the store lock), then bind the writer here,
    in the request's own context, so the route that runs after it inherits it."""
    seat = await run_in_threadpool(getattr(request.app.state.team, how), request)
    if seat.learner is not None:
        store: Store = request.app.state.dojo.store
        store.bind_writer(seat.learner.key, store.generation(seat.learner.key))
    return seat


async def current_principal(request: Request) -> Seat:
    """signed_in_non_member: any signed-in user of the tenant, member or not."""
    return await _seat(request, "seat")


async def current_member_any(request: Request) -> Learner:
    """learner_exit: /api/me, the notice, export, delete, leave, renewal, display name."""
    return (await _seat(request, "exit_seat")).learner


async def current_active_learner(request: Request) -> Learner:
    """learner: every operational route."""
    return (await _seat(request, "active_seat")).learner


async def current_admin(request: Request) -> Learner:
    """admin: the owner only."""
    return (await _seat(request, "admin_seat")).learner


async def current_exit_seat(request: Request) -> Seat:
    """learner_exit, for routes that need the membership row as well."""
    return await _seat(request, "exit_seat")


async def current_admin_seat(request: Request) -> Seat:
    return await _seat(request, "admin_seat")


ROUTE_CLASSES = {
    current_principal: "signed_in_non_member",
    current_member_any: "learner_exit",
    current_exit_seat: "learner_exit",
    current_active_learner: "learner",
    current_admin: "admin",
    current_admin_seat: "admin",
}
