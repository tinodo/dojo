"""What Dojo keeps about the search for each skill's pages (ADR 0018, 4.6): the documents, the lease and the ledger.

Every document is on the data share under `sourcing/`. A lease says which process works on an exam: a file
that is taken over only after it expired, with an epoch that grows at every takeover, so a process that lost
its lease (it was slow, or the app restarted while the old instance was still running) is recognised and
stops before it writes. Each paid step is written down before it is made and marked when it is done; the key
holds the cycle, the round and the step, and an input fingerprint (models, prompt version, phrases, page
hashes) tells a step that has to be made again from one that is done. A step that failed three times is dead:
its skill is blocked for the day, never called exhausted."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from ..core import Store, iso, parse_iso, sha256, utcnow
from ..sources import normalize

MAX_TRIES = 3
LEASE_TTL_S = 600
STATES = ("covered", "partial", "searching", "exhausted", "blocked")


class LeaseLost(Exception):
    """Another process holds the lease now: this one writes nothing more."""


class DeadStep(Exception):
    """A step that failed MAX_TRIES times."""


def fingerprint(*parts: Any) -> str:
    """A short mark of a step's inputs. Models, prompt version, phrases and page hashes belong in it."""
    return sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str))[:16]


class Lease:
    """One owner at a time for one exam's work. The file is `sourcing/<name>.lease`."""

    def __init__(self, store: Store, name: str, owner: str | None = None, ttl: int = LEASE_TTL_S,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], None] = time.sleep):
        self.path: Path = store.path("sourcing", f"{name}.lease")
        self.owner = owner or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.ttl = ttl
        self.clock = clock
        self.pause = pause
        self.epoch = 0
        self._kept = 0.0
        self._lock = threading.Lock()

    def _read(self) -> dict | None:
        try:
            doc = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _write(self, doc: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(doc), "utf-8")
        os.replace(tmp, self.path)

    def acquire(self) -> bool:
        """True when this owner holds the lease now (it already did, or the lease was free or expired)."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            held = self._read()
            now = self.clock()
            if held and held.get("owner") == self.owner and held.get("epoch") == self.epoch and self.epoch:
                self._write({"owner": self.owner, "epoch": self.epoch, "expires": now + self.ttl})
                return True
            if held and float(held.get("expires") or 0) > now:
                return False
            epoch = int((held or {}).get("epoch") or 0) + 1
            try:
                if held is None and not self.path.exists():
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps({"owner": self.owner, "epoch": epoch, "expires": now + self.ttl}))
                else:
                    self._write({"owner": self.owner, "epoch": epoch, "expires": now + self.ttl})
            except OSError:
                return False
            self.pause(0.05)   # a second process may have written at the same moment: whoever is named now has it
            after = self._read()
            if after and after.get("owner") == self.owner and after.get("epoch") == epoch:
                self.epoch = epoch
                return True
            return False

    def check(self) -> None:
        """Raises LeaseLost unless the lease still names this owner and epoch."""
        held = self._read()
        if not (held and held.get("owner") == self.owner and held.get("epoch") == self.epoch and self.epoch):
            raise LeaseLost("another process holds the sourcing lease")

    def renew(self) -> None:
        with self._lock:
            self.check()
            self._kept = self.clock()
            self._write({"owner": self.owner, "epoch": self.epoch, "expires": self._kept + self.ttl})

    def keep(self) -> None:
        """Before a write: still the owner (else LeaseLost), and the lease is renewed once a minute, so a long step
        never lets it expire under its own feet."""
        self.check()
        if self.clock() - self._kept >= 60:
            self.renew()

    def release(self) -> None:
        with self._lock:
            held = self._read()
            if held and held.get("owner") == self.owner and held.get("epoch") == self.epoch:
                self._write({"owner": self.owner, "epoch": self.epoch, "expires": 0})


def new_skill(key: str, cycle: int = 1) -> dict:
    return {"key": key, "state": "searching", "round": 0, "cycle": cycle, "plan": {}, "pool": {}, "accepted": [],
            "set": None, "steps": {}, "days": {}, "activated": None, "blocked": None, "next": None, "epoch": 0,
            "updated": iso()}


def begin(doc: dict, step: str, fp: str) -> bool:
    """Write down that a step starts. False when it is done already under this fingerprint. Raises DeadStep after
    MAX_TRIES tries without a result. The caller saves the document before it makes the paid call."""
    key = f"{doc['cycle']}.{doc['round'] + 1}.{step}"
    old = doc["steps"].get(key)
    if old and old.get("fp") == fp:
        if old.get("done"):
            return False
        if old.get("tries", 0) >= MAX_TRIES:
            raise DeadStep(key)
        tries = old.get("tries", 0)
    else:
        tries = 0
    doc["steps"][key] = {"tries": tries + 1, "done": False, "fp": fp, "at": iso()}
    return True


def finish(doc: dict, step: str) -> None:
    key = f"{doc['cycle']}.{doc['round'] + 1}.{step}"
    if key in doc["steps"]:
        doc["steps"][key].update(done=True, at=iso())


def dead(doc: dict) -> list[str]:
    """The steps of the current cycle that are out of tries."""
    return [k for k, s in doc["steps"].items() if not s.get("done") and s.get("tries", 0) >= MAX_TRIES
            and k.startswith(f"{doc['cycle']}.")]


class Books:
    """The documents of one exam's search, written only by the owner of the lease."""

    def __init__(self, store: Store, pid: str, lease: Lease):
        self.store, self.pid, self.lease = store, pid, lease
        self._lock = threading.RLock()

    def index(self) -> dict:
        doc = self.store.read("sourcing", self.pid, default=None)
        base = {"package": self.pid, "generation": 0, "cycle": 1, "calls": {}, "epoch": 0, "audit": None}
        return {**base, **doc} if isinstance(doc, dict) else base

    def save_index(self, doc: dict) -> None:
        with self._lock:
            self.lease.keep()
            self._fenced(doc)
            self.store.write("sourcing", self.pid, value=doc)

    def keys(self) -> list[str]:
        return [n for n in self.store.names("sourcing", self.pid) if not n.startswith("side-")]

    def skill(self, key: str) -> dict:
        doc = self.store.read("sourcing", self.pid, key, default=None)
        return doc if isinstance(doc, dict) and doc.get("key") == key else new_skill(key, self.index()["cycle"])

    def save_skill(self, doc: dict) -> None:
        with self._lock:
            self.lease.keep()
            self._fenced(doc)
            doc["updated"] = iso()
            self.store.write("sourcing", self.pid, doc["key"], value=doc)

    def side(self, name: str, default: Any = None) -> Any:
        """A document of the exam that belongs to no skill (the R7 scan, the audit): `side-<name>`."""
        return self.store.read("sourcing", self.pid, f"side-{name}", default=default)

    def save_side(self, name: str, value: Any) -> None:
        with self._lock:
            self.lease.keep()
            self.store.write("sourcing", self.pid, f"side-{name}", value=value)

    def _fenced(self, doc: dict) -> None:
        """A document written by a newer owner is never written over by an older one."""
        if int(doc.get("epoch") or 0) > self.lease.epoch:
            raise LeaseLost("this document was written by a newer owner")
        doc["epoch"] = self.lease.epoch

    # ------------------------------------------------------------ the day's model calls

    def calls_today(self, day: str | None = None) -> int:
        return int((self.index().get("calls") or {}).get(day or utcnow().date().isoformat()) or 0)

    def add_calls(self, n: int) -> None:
        with self._lock:
            doc = self.index()
            day = utcnow().date().isoformat()
            doc["calls"] = {d: v for d, v in (doc.get("calls") or {}).items() if d >= (utcnow() - timedelta(days=3)).date().isoformat()}
            doc["calls"][day] = int(doc["calls"].get(day) or 0) + n
            self.save_index(doc)


def due(doc: dict, now: Any = None) -> bool:
    """Whether a skill that stopped with `next` set may be worked on again."""
    when = doc.get("next")
    return not when or parse_iso(when) <= (now or utcnow())


def skill_key(domain: str, group: str, text: str) -> str:
    """A skill's key in the build document and in the search's books: position-free, from its words."""
    return sha256(f"{normalize(domain)}|{normalize(group)}|{normalize(text)}")[:16]
