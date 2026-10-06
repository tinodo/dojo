"""The foundations every other module uses: settings, storage, sign-in and background jobs.

- Settings come from environment variables only (`load_settings`); docs/operations.md lists each one.
- Store keeps all data as files on one share: JSON documents written atomically, append-only JSON-lines
  logs, and each learner's Record as a hash-chained event log. One lock serialises all access, so Dojo
  must run as one process with one worker.
- Sign-in trusts only the identity App Service authentication passes in, and checks the owner's tenant and
  object id again. A learner's data is filed under a one-way key, never under their ids.
- Jobs and Slots run slow model work in background threads, a few at a time, in a fair queue. Writes for
  a learner who was deleted, or who started over, after the work began are dropped (Abandoned).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

log = logging.getLogger("dojo")
APP_DIR = Path(__file__).resolve().parent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """A time as stored everywhere in Dojo: ISO 8601 in UTC, to the second. Now if no time is given."""
    return (dt or utcnow()).isoformat(timespec="seconds")


def one_line(value: Any, limit: int = 200) -> str:
    """A value that came with a request, as one log line can carry it: bounded, and no line break can start a
    forged entry."""
    return str(value)[:limit].replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def where_stored() -> str:
    """Where the app's storage is, for About and the notice. App Service sets REGION_NAME (for example
    "Sweden Central"); without it, name no region rather than a wrong one."""
    region = os.environ.get("REGION_NAME", "").strip()
    return f"in Azure ({region})" if region else "in the operator's Azure region"


def parse_iso(value: str) -> datetime:
    """Reads a stored time back. A time without a zone is taken as UTC."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def canonical(value: Any) -> str:
    """One fixed JSON text per value (sorted keys, no spaces), so equal values hash the same."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def new_id(prefix: str) -> str:
    """A random id such as `ev-...`: the prefix says what it names, 80 random bits make it unique."""
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


class UserError(Exception):
    """A failure the learner should see in plain words."""


class Abandoned(Exception):
    """A write under a learner whose data was deleted (or started over) after the work began. It is
    dropped, so a late job or batch can never bring deleted data back (ADR 0009)."""


# ---------------------------------------------------------------- settings

# Team mode (ADR 0009) was built in four PRs; this flag kept DOJO_TEAM=1 ignored until the last one landed.
# It is finished now, and off by default: only the Bicep parameter teamMode (which sets DOJO_TEAM=1 and opens
# Easy Auth to the tenant; deploy.yml sets it from the repository variable DOJO_TEAM_MODE) switches it on,
# and that is the owner's decision after the reviews ADR 0009 names.
TEAM_READY = True

@dataclass(frozen=True)
class Settings:
    """Everything Dojo reads from its environment, read once at startup by `load_settings`."""
    local: bool
    real_ai: bool
    in_azure: bool
    data_dir: Path
    ai_endpoint: str
    ai_resource_id: str
    speech_endpoint: str
    speech_region: str
    models: dict
    owner_tid: str
    owner_oid: str
    auth_client_id: str
    identity_client_id: str
    tenant_id: str
    build: str
    model_sku: str = "GlobalStandard"   # the deployment type of every model (infra/main.bicep, modelSku)
    team: bool = False                  # team mode (ADR 0009): members besides the owner
    owner_key: str = ""                 # the owner's learner key after a lost account; empty = derived


def load_settings() -> Settings:
    """Reads the settings. DOJO_LOCAL=1 is ignored in Azure (WEBSITE_SITE_NAME is set there), so a stray
    setting can never switch off sign-in. A malformed DOJO_MODELS or DOJO_OWNER_KEY is ignored."""
    def env(name: str) -> str:
        return (os.environ.get(name) or "").strip()

    in_azure = bool(env("WEBSITE_SITE_NAME"))
    local = env("DOJO_LOCAL") == "1" and not in_azure
    try:
        models = json.loads(env("DOJO_MODELS") or "{}")
    except ValueError:
        models = {}
    build_file = APP_DIR / "build.txt"
    build = build_file.read_text("utf-8").strip() if build_file.exists() else "dev"
    team = env("DOJO_TEAM") == "1"
    if team and not TEAM_READY:
        log.warning("DOJO_TEAM=1 is ignored: team mode is not finished in this build, so Dojo stays owner-only")
        team = False
    owner_key = env("DOJO_OWNER_KEY")
    if owner_key and not re.fullmatch(r"l[0-9a-f]{31}", owner_key):
        log.warning("DOJO_OWNER_KEY is not a learner key and is ignored")
        owner_key = ""
    return Settings(
        local=local,
        real_ai=(not local) or env("DOJO_LOCAL_AI") == "real",
        in_azure=in_azure,
        data_dir=Path(env("DOJO_DATA_DIR") or APP_DIR.parent / ".dojo-data"),
        ai_endpoint=env("DOJO_AI_ENDPOINT").rstrip("/"),
        ai_resource_id=env("DOJO_AI_RESOURCE_ID"),
        speech_endpoint=env("DOJO_SPEECH_ENDPOINT").rstrip("/"),
        speech_region=env("DOJO_SPEECH_REGION") or "swedencentral",
        models=models if isinstance(models, dict) else {},
        owner_tid=env("DOJO_OWNER_TID"),
        owner_oid=env("DOJO_OWNER_OID"),
        auth_client_id=env("DOJO_AUTH_CLIENT_ID"),
        identity_client_id=env("AZURE_CLIENT_ID"),
        tenant_id=env("DOJO_OWNER_TID") or env("DOJO_TENANT_ID"),
        build=build,
        model_sku=env("DOJO_MODEL_SKU") or "GlobalStandard",
        team=team,
        owner_key=owner_key,
    )


class Tokens:
    """Caches Entra access tokens per scope (the CLI credential used locally does not cache)."""

    def __init__(self, credential: Any):
        self.credential = credential
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, scope: str) -> str:
        with self._lock:
            token = self._cache.get(scope)
            if token is None or token.expires_on - 300 < time.time():
                token = self.credential.get_token(scope)
                self._cache[scope] = token
            return token.token


# ---------------------------------------------------------------- storage

_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Whose data the current work writes, and the generation of that data when the work began: set by a
# job, a background batch and every signed-in request (ADR 0009, "Deletion that stays deleted").
_WRITER: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar("dojo_writer", default=None)


class Store:
    """JSON documents, byte files and append-only logs under one folder.

    In Azure the folder is the web app's persistent /home share. Documents are written to a temporary
    file and then renamed, so a reader never sees half a document. One lock serialises all access
    inside the single app process.

    Every write under learners/<key>/ is checked under that lock: nothing is written for a learner
    whose data was deleted (a tombstone), and nothing is written by work that began before the learner
    started over (an older generation). Such a write raises Abandoned instead.
    """

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self._heads: dict[str, str] = {}
        self._gens: dict[str, int] = {}
        self._tombs: set[str] = set()   # sha256 of deleted learner keys
        # Called with (learner key, event) after each event is added, still under the lock: what is derived
        # from a Record elsewhere and must change with it (Play's shared moments, ADR 0010).
        self.on_event: list[Callable[[str, dict], None]] = []
        root.mkdir(parents=True, exist_ok=True)

    # ---- deletion that stays deleted ----

    def generation(self, learner_key: str) -> int:
        """How many times this learner started over since startup. Work remembers it when it begins."""
        with self.lock:
            return self._gens.get(learner_key, 0)

    def bump(self, learner_key: str) -> None:
        """The learner started over: work that began before now writes nothing more."""
        with self.lock:
            self._gens[learner_key] = self._gens.get(learner_key, 0) + 1
            self._heads.pop(learner_key, None)

    def tombstone(self, learner_key: str) -> None:
        """The learner's data was deleted: nothing is written under their key again. Only a hash of
        the key is kept."""
        with self.lock:
            self._tombs.add(sha256(learner_key))
            self.bump(learner_key)

    def set_tombstones(self, hashes: set[str]) -> None:
        with self.lock:
            self._tombs = set(hashes)

    def tombstoned(self, learner_key: str) -> bool:
        with self.lock:
            return sha256(learner_key) in self._tombs

    @staticmethod
    def bind_writer(learner_key: str, generation: int) -> contextvars.Token:
        return _WRITER.set((learner_key, generation))

    @contextmanager
    def writing_as(self, learner_key: str):
        """Work for this learner from here on: its writes are dropped if the learner starts over or
        leaves before it ends."""
        token = self.bind_writer(learner_key, self.generation(learner_key))
        try:
            yield
        finally:
            _WRITER.reset(token)

    def carry_on(self, learner_key: str) -> None:
        """After a start-over inside work for this learner: that work goes on under the new generation.
        Work bound to no learner, or to another one, is left as it was."""
        writer = _WRITER.get()
        if writer and writer[0] == learner_key:
            _WRITER.set((learner_key, self.generation(learner_key)))

    def _guard(self, parts: tuple[str, ...]) -> None:
        if len(parts) < 2 or parts[0] != "learners":
            return
        key = parts[1]
        if sha256(key) in self._tombs:
            raise Abandoned("This learner's data was deleted.")
        writer = _WRITER.get()
        if writer and writer[0] == key and writer[1] != self._gens.get(key, 0):
            raise Abandoned("This learner's record was deleted while the work ran.")

    def path(self, *parts: str) -> Path:
        """The file for these name parts. Each part must be a plain name (letters, digits, `.`, `_`, `-`),
        so no part can climb out of the data folder."""
        for part in parts:
            if not isinstance(part, str) or not _SAFE_PART.match(part) or ".." in part:
                raise ValueError(f"unsafe storage name: {part!r}")
        return self.root.joinpath(*parts)

    def _doc(self, parts: tuple[str, ...]) -> Path:
        if not parts:
            raise ValueError("document name missing")
        return self.path(*parts[:-1], parts[-1] + ".json")

    def read(self, *parts: str, default: Any = None) -> Any:
        """The JSON document `parts[-1].json` in the folder `parts[:-1]`, or `default` if there is none."""
        p = self._doc(parts)
        with self.lock:
            try:
                return json.loads(p.read_text("utf-8"))
            except FileNotFoundError:
                return default

    def write(self, *parts: str, value: Any) -> None:
        """Writes a JSON document in one step (temporary file, then rename). Raises Abandoned for a
        learner whose data was deleted, or for work that began before they started over."""
        data = json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")
        with self.lock:
            self._guard(parts)
            self._write_raw(self._doc(parts), data)

    def delete(self, *parts: str) -> None:
        p = self._doc(parts)
        with self.lock:
            if p.exists():
                p.unlink()

    def names(self, *parts: str) -> list[str]:
        """The names of the JSON documents in a folder, without `.json`, sorted."""
        folder = self.path(*parts) if parts else self.root
        with self.lock:
            if not folder.is_dir():
                return []
            return sorted(
                x.name[:-5] for x in folder.iterdir()
                if x.is_file() and x.name.endswith(".json") and not x.name.startswith(".")
            )

    def read_bytes(self, *parts: str) -> bytes | None:
        p = self.path(*parts)
        with self.lock:
            try:
                return p.read_bytes()
            except FileNotFoundError:
                return None

    def write_bytes(self, *parts: str, data: bytes) -> None:
        with self.lock:
            self._guard(parts)
            self._write_raw(self.path(*parts), data)

    def exists(self, *parts: str) -> bool:
        return self.path(*parts).exists()

    def file_stats(self, *parts: str, suffix: str) -> list[tuple[str, int, float]]:
        """Name, size in bytes and last-used time of every file with this ending in one folder."""
        folder = self.path(*parts) if parts else self.root
        with self.lock:
            if not folder.is_dir():
                return []
            out = []
            for x in folder.iterdir():
                if not (x.is_file() and x.name.endswith(suffix) and not x.name.startswith(".")):
                    continue
                try:
                    st = x.stat()
                except OSError:
                    continue
                out.append((x.name, st.st_size, st.st_mtime))
            return out

    def touch(self, *parts: str) -> None:
        """Mark a file as used now, which is what least-recently-used clean-up sorts on."""
        p = self.path(*parts)
        with self.lock:
            try:
                os.utime(p, None)
            except OSError:
                pass

    def delete_file(self, *parts: str) -> None:
        p = self.path(*parts)
        with self.lock:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def remove_tree(self, *parts: str) -> None:
        p = self.path(*parts)
        with self.lock:
            if p.is_dir():
                shutil.rmtree(p)
            self._heads.clear()

    def _write_raw(self, p: Path, data: bytes) -> None:
        with self.lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
            with tmp.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, p)
                    return
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            os.replace(tmp, p)

    def append_line(self, *parts: str, value: Any) -> None:
        """Adds one JSON line to a log file and flushes it to disk. Logs are only ever appended to."""
        p = self.path(*parts)
        with self.lock:
            self._guard(parts)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def read_lines(self, *parts: str) -> list[Any]:
        """Every readable line of a log file. A damaged line is skipped and logged, not fatal."""
        p = self.path(*parts)
        with self.lock:
            if not p.exists():
                return []
            lines = p.read_text("utf-8").splitlines()
        out = []
        for line in lines:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    log.warning("skipping an unreadable line in %s", p.name)
        return out

    # ---- the learner's record: an append-only, hash-chained event log ----

    def append_event(self, learner_key: str, kind: str, data: dict) -> dict:
        """Adds an event to the learner's Record. Each event holds the hash of the one before it, and
        its own hash covers all its fields, so a change or a gap anywhere shows in `verify_chain`.
        Events are never edited or removed; only deleting the whole Record removes them."""
        with self.lock:
            prev = self._heads.get(learner_key)
            if prev is None:
                events = self.events(learner_key)
                prev = events[-1]["hash"] if events else "0" * 64
            event = {"id": new_id("ev"), "at": iso(), "type": kind, "data": data, "prev": prev}
            event["hash"] = sha256(canonical(event))
            self.append_line("learners", learner_key, "events.jsonl", value=event)
            self._heads[learner_key] = event["hash"]
            for fn in self.on_event:
                try:
                    fn(learner_key, event)
                except Exception:  # noqa: BLE001 - the event is written; a follower must not undo the request
                    log.exception("a step after a new Record event failed")
            return event

    def events(self, learner_key: str) -> list[dict]:
        return self.read_lines("learners", learner_key, "events.jsonl")

    def verify_chain(self, learner_key: str) -> dict:
        """Checks every hash in the learner's Record. Returns `ok`, the number of events and, if the
        chain is broken, the position of the first event that does not fit."""
        prev = "0" * 64
        events = self.events(learner_key)
        for n, event in enumerate(events):
            body = {k: v for k, v in event.items() if k != "hash"}
            if event.get("prev") != prev or sha256(canonical(body)) != event.get("hash"):
                return {"ok": False, "events": len(events), "broken_at": n}
            prev = event["hash"]
        return {"ok": True, "events": len(events)}


# ---------------------------------------------------------------- sign-in

TID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/tenantid", "tid")
OID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")
NAME_CLAIMS = ("name", "preferred_username", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
# For display on the owner's Members page only: never used to decide access or to key data (EG-5).
EMAIL_CLAIMS = ("email", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")


@dataclass(frozen=True)
class Learner:
    """A signed-in learner. `id` is "tenant:object id"; `key`, a one-way hash of it, names their data."""
    id: str
    key: str
    name: str


@dataclass(frozen=True)
class Principal:
    """Who signed in, as App Service authentication vouches for it."""
    tid: str
    oid: str
    name: str
    email: str


def learner_key(learner_id: str) -> str:
    """The folder name of a learner's data: a one-way hash of their id, so folder names reveal no ids."""
    return "l" + sha256("dojo-learner:" + learner_id)[:31]


def owner_key(s: Settings) -> str:
    """The owner's learner key: derived from the owner's tenant and object id, unless the owner's lost
    account was recovered and DOJO_OWNER_KEY keeps the old key (ADR 0009, "Lost accounts")."""
    if s.local:
        return learner_key("local:dev")
    return s.owner_key or learner_key(f"{s.owner_tid}:{s.owner_oid}")


def _claim(claims: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if claims.get(name):
            return claims[name]
    return ""


def read_principal(request: Request) -> Principal:
    """App Service authentication signs the user in and passes the verified identity in
    X-MS-CLIENT-PRINCIPAL. Raises 401 if it is missing or cannot be read, and 503 if no owner is set."""
    s: Settings = request.app.state.settings
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        raise HTTPException(401, "Sign in first.")
    try:
        principal = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
        claims: dict[str, str] = {}
        for c in principal.get("claims") or []:
            if isinstance(c, dict) and isinstance(c.get("typ"), str) and isinstance(c.get("val"), str):
                claims.setdefault(c["typ"], c["val"])
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "The sign-in information could not be read.")
    if principal.get("auth_typ") != "aad":
        raise HTTPException(401, "Sign in with Microsoft Entra ID.")
    if not s.owner_tid or not s.owner_oid:
        raise HTTPException(503, "This Dojo has no owner configured.")
    tid, oid = _claim(claims, TID_CLAIMS), _claim(claims, OID_CLAIMS)
    header_oid = request.headers.get("x-ms-client-principal-id", "")
    if not tid or not oid or (header_oid and header_oid != oid):
        raise HTTPException(403, "This Dojo belongs to someone else.")
    return Principal(tid, oid, _claim(claims, NAME_CLAIMS), _claim(claims, EMAIL_CLAIMS))


def current_learner(request: Request) -> Learner:
    """The signed-in owner: with team mode off, Dojo admits only the configured owner."""
    s: Settings = request.app.state.settings
    if s.local:
        return Learner("local:dev", learner_key("local:dev"), "Local learner")
    p = read_principal(request)
    if p.tid != s.owner_tid or p.oid != s.owner_oid:
        raise HTTPException(403, "This Dojo belongs to someone else.")
    return Learner(f"{p.tid}:{p.oid}", owner_key(s), p.name or "Learner")


# ---------------------------------------------------------------- background jobs

OWNER_JOBS = 6    # jobs queued or running at once: the owner
MEMBER_JOBS = 3   # ...and a member (ADR 0009)


def current_writer() -> str | None:
    """The learner the current work is for: set by a job, a background batch and every signed-in
    request. Model calls are attributed to it (ADR 0009; metered in PR 3)."""
    writer = _WRITER.get()
    return writer[0] if writer else None


@dataclass(frozen=True)
class Spend:
    """What the current paid work is for (ADR 0009, "Money"): the learner (None for shared content work),
    the action, the exam, and the reservation it spends from (app/budget.py), if any."""
    learner: str | None
    action: str
    package: str | None = None
    hold: Any = None


_SPEND: contextvars.ContextVar[Spend | None] = contextvars.ContextVar("dojo_spend", default=None)


def current_spend() -> Spend | None:
    """What the paid work running now is for, or None outside any action."""
    return _SPEND.get()


@contextmanager
def spending(spend: Spend | None):
    """Runs the block as work for `spend`: its model and speech calls are counted against it."""
    token = _SPEND.set(spend)
    try:
        yield spend
    finally:
        _SPEND.reset(token)


def carried(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn` for a thread pool: each call runs in a copy of the caller's context, so the learner a model call
    is attributed to, its reservation and the generation check travel with it. Python 3.13 threads start
    with an empty context otherwise."""
    parent = contextvars.copy_context()
    return lambda *args, **kwargs: parent.copy().run(fn, *args, **kwargs)


class Slots:
    """The few work slots everything shares. It behaves like a bounded semaphore and also counts who
    is holding one, because not every holder has a job document: the exam pool and the delivery check
    take a slot directly. Work that wants several slots at once can then ask how many are really free
    instead of guessing from the job documents and crowding those workers out."""

    def __init__(self, count: int):
        self.count = count
        self._cond = threading.Condition(threading.Lock())
        self._held = 0
        self._waiting: list[tuple[str, int]] = []   # (whose work, ticket), in the order they asked
        self._served: dict[str, int] = {}           # whose work got a slot, and when (a counter)
        self._seq = 0

    def _first(self) -> tuple[str, int] | None:
        """The waiter served next: the work of whoever was served least recently, its oldest request
        first (ADR 0009, the fair queue). With one learner this is first in, first out."""
        if not self._waiting:
            return None
        return min(self._waiting, key=lambda t: (self._served.get(t[0], -1), t[1]))

    def acquire(self, blocking: bool = True, timeout: float | None = None, who: str = "") -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            self._seq += 1
            ticket = (who, self._seq)
            self._waiting.append(ticket)
            try:
                while not (self._held < self.count and self._first() == ticket):
                    if not blocking:
                        return False
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._cond.wait(left)
                self._held += 1
                self._seq += 1
                self._served[who] = self._seq
                return True
            finally:
                self._waiting.remove(ticket)
                self._cond.notify_all()

    def take_spare(self, queued: int = 0) -> bool:
        """Takes one slot for background work only while at least two stay free for learners, nobody is
        waiting for one, and `queued` jobs are not about to ask. The check and the take are one step."""
        with self._cond:
            if self._waiting or self.count - self._held - queued < 2:
                return False
            self._held += 1
            return True

    def wait_release(self, timeout: float) -> None:
        """Waits until a slot is given back, or for `timeout` seconds."""
        with self._cond:
            self._cond.wait(timeout)

    def release(self) -> None:
        with self._cond:
            if self._held <= 0:
                raise ValueError("Slots released too many times")
            self._held -= 1
            self._cond.notify_all()

    def __enter__(self) -> "Slots":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    def held(self) -> int:
        with self._cond:
            return self._held

    def free(self) -> int:
        with self._cond:
            return max(0, self.count - self._held)


class Jobs:
    """Background work (model calls take minutes). State is persisted with a heartbeat, so after a
    restart unfinished work is marked interrupted instead of staying 'running' forever."""

    def __init__(self, store: Store, parallel: int = 3, on_failure: Callable[[str, str, str], None] | None = None):
        self.store = store
        self.parallel = parallel
        self.slots = Slots(parallel)
        self.on_failure = on_failure
        self.owner_key: str | None = None   # set at startup: the owner keeps OWNER_JOBS, members MEMBER_JOBS
        self._lock = threading.Lock()  # capacity checks, starts and deletions happen one at a time
        self._recover()

    def _recover(self) -> None:
        cutoff = utcnow() - timedelta(days=14)
        for name in self.store.names("jobs"):
            job = self.store.read("jobs", name)
            if not isinstance(job, dict):
                continue
            if parse_iso(job.get("created", iso())) < cutoff:
                self.store.delete("jobs", name)
            elif job.get("state") in ("queued", "running"):
                job.update(state="interrupted", error="Dojo restarted while this was running. Start it again.")
                self.store.write("jobs", name, value=job)

    def start(self, learner: Learner, kind: str, fn: Callable[..., Any], *args: Any, ref: str | None = None,
              only_if_free: bool = False, busy_message: str | None = None) -> dict:
        """Queues `fn(step, *args)` as a job for this learner and returns the job at once. `step` lets the
        work report progress. The job waits for a free slot, runs as work for this learner (with the
        request's reservation, if any) and ends as done, failed or abandoned. Raises 429 when the
        learner already has too many jobs, or with `only_if_free` when no slot is free. With
        `busy_message`, raises 409 with it while a job of this kind for the same `ref` is queued or
        running: the check and the start are one step, so two requests cannot both start paid work."""
        with self._lock:
            busy = [j for j in self.for_learner(learner) if j.get("state") in ("queued", "running")]
            if busy_message is not None and any(j.get("kind") == kind and j.get("ref") == ref for j in busy):
                raise HTTPException(409, busy_message)
            # A member may have fewer at once than the owner, so one member cannot fill the queue (ADR 0009).
            limit = OWNER_JOBS if self.owner_key in (None, learner.key) else MEMBER_JOBS
            if len(busy) >= limit:
                raise HTTPException(429, "Several things are already running. Wait for one to finish.")
            # Asked for by work that starts several jobs at once: it takes what is free right now and
            # leaves the rest alone. The count is read here, inside the lock, so two starts cannot both
            # take the last slot.
            if only_if_free and self._free() <= 0:
                raise HTTPException(429, "The work slots are busy. Wait for one to finish.")
            job = {
                "id": new_id("job"), "kind": kind, "learner": learner.key, "ref": ref, "state": "queued",
                "step": "Waiting for a free slot", "created": iso(), "heartbeat": iso(), "result": None, "error": None,
            }
            self.store.write("jobs", job["id"], value=job)
            gen = self.store.generation(learner.key)
            # The reservation the request made for this action (app/budget.py) goes with the job, which
            # gives it back when it ends.
            spend = _SPEND.get()
            hold = spend.hold.adopt() if spend is not None and spend.hold is not None else None
            # The exam the action is for goes with it, so the job's spend lands in that exam's pooled sum.
            package = getattr(hold, "package", None) or (spend.package if spend is not None else None)
            threading.Thread(target=self._run, args=(job["id"], learner.key, kind, fn, args, gen, hold, package),
                             name=job["id"], daemon=True).start()
        return self.public(job)

    @contextmanager
    def exclusive(self, learner: Learner):
        """Hold off new jobs; fail if any of this learner's jobs is still queued or running."""
        with self._lock:
            if any(j.get("state") in ("queued", "running") for j in self.for_learner(learner)):
                raise HTTPException(409, "Something is still running. Wait until it has finished, then try again.")
            yield

    def _update(self, job_id: str, **fields: Any) -> bool:
        """Writes the job's new state. A job whose document is gone (its learner left) is not
        written again, so a late job cannot bring its document back."""
        with self.store.lock:
            job = self.store.read("jobs", job_id)
            if not isinstance(job, dict) or self.store.tombstoned(str(job.get("learner") or "")):
                return False
            job.update(fields, heartbeat=iso())
            self.store.write("jobs", job_id, value=job)
            return True

    def _run(self, job_id: str, learner: str, kind: str, fn: Callable[..., Any], args: tuple, gen: int = 0,
             hold: Any = None, package: str | None = None) -> None:
        self.store.bind_writer(learner, gen)   # this thread's writes belong to this learner's data as it is now
        _SPEND.set(Spend(learner, kind, package, hold))   # its model calls are this learner's, for this action
        released = False

        def settle(failed: bool = True) -> None:
            # The reservation is given back before the job reports its end, so whoever waits for the
            # job sees the allowance as it stays. A job that did not finish says so: if no paid call ran,
            # the action's count comes back (ADR 0009, "Money").
            nonlocal released
            if hold is not None and not released:
                released = True
                hold.release(failed=failed)

        try:
            self._work(job_id, learner, kind, fn, args, settle)
        finally:
            settle()

    def _work(self, job_id: str, learner: str, kind: str, fn: Callable[..., Any], args: tuple,
              settle: Callable[..., None] = lambda failed=True: None) -> None:
        self.slots.acquire(who=learner)   # the fair queue: whoever was served least recently goes first
        try:
            if not self._update(job_id, state="running", step="Starting"):
                settle(failed=True)
                return   # cancelled while it waited for a slot
            try:
                result = fn(lambda step: self._update(job_id, step=step), *args)
                settle(failed=False)
                self._update(job_id, state="done", step="Done", result=result)
            except Abandoned:
                settle(failed=True)
                self._update(job_id, state="abandoned", step="Abandoned", error="Your data was deleted while this ran.")
            except UserError as e:
                settle(failed=True)
                self._update(job_id, state="failed", error=str(e))
                self._report(learner, kind, str(e))
            except Exception as e:  # noqa: BLE001 - the learner must see a failure, not a hang
                settle(failed=True)
                log.exception("job %s failed", job_id)
                message = f"{type(e).__name__}: {str(e)[:400]}"
                self._update(job_id, state="failed", error=f"Something went wrong. {message}")
                self._report(learner, kind, message)
        finally:
            self.slots.release()

    def take_spare(self) -> bool:
        """One slot for a background batch, only while two stay free for learners (ADR 0009, "Spare
        slots"). Jobs that are queued but not yet waiting for a slot count as taken."""
        with self._lock:
            jobs = (self.store.read("jobs", n) for n in self.store.names("jobs"))
            queued = sum(1 for j in jobs if isinstance(j, dict) and j.get("state") == "queued")
            return self.slots.take_spare(queued)

    def _report(self, learner: str, kind: str, message: str) -> None:
        if self.on_failure:
            try:
                self.on_failure(learner, kind, message)
            except Exception:  # noqa: BLE001
                log.exception("could not record a job failure")

    def get(self, learner: Learner, job_id: str) -> dict:
        try:
            job = self.store.read("jobs", job_id)
        except ValueError:
            job = None
        if not isinstance(job, dict) or job.get("learner") != learner.key:
            raise HTTPException(404, "No such job.")
        return self.public(job)

    def busy(self, learner: Learner, kind: str, ref: str) -> bool:
        return any(j.get("kind") == kind and j.get("ref") == ref and j.get("state") in ("queued", "running")
                   for j in self.for_learner(learner))

    def free(self) -> int:
        """How many of the shared slots nobody is using right now. Work that starts several jobs at
        once asks first, so it takes what is free instead of filling the queue and making everything
        else wait behind it. Slots held without a job document (the exam pool, the delivery check)
        count too, and so does every job that is queued for one."""
        with self._lock:
            return self._free()

    def _free(self) -> int:
        jobs = (self.store.read("jobs", n) for n in self.store.names("jobs"))
        queued = sum(1 for j in jobs if isinstance(j, dict) and j.get("state") == "queued")
        return max(0, self.slots.free() - queued)

    def existing(self, learner: Learner, kind: str, ref: str) -> dict | None:
        """The job already working on this, so asking twice does not start the work twice."""
        running = [j for j in self.for_learner(learner)
                   if j.get("kind") == kind and j.get("ref") == ref and j.get("state") in ("queued", "running")]
        return self.public(sorted(running, key=lambda j: j.get("created") or "")[-1]) if running else None

    def for_learner(self, learner: Learner) -> list[dict]:
        jobs = (self.store.read("jobs", n) for n in self.store.names("jobs"))
        return [j for j in jobs if isinstance(j, dict) and j.get("learner") == learner.key]

    def remove_for(self, learner: Learner) -> None:
        for job in self.for_learner(learner):
            self.store.delete("jobs", job["id"])

    @staticmethod
    def public(job: dict) -> dict:
        return {k: job.get(k) for k in ("id", "kind", "state", "step", "created", "heartbeat", "result", "error")}
