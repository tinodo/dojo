"""Dojo's files in Azure Blob Storage (ADR 0030).

The Store keeps writing to its folder on the web app's disk, and every file it writes is also put into one private
blob container. The container is where the data lives; the folder is a copy, which keeps reads fast and lets every
other module go on using plain paths. A web app that starts with an empty folder fills it again from the container:
the small files (documents, logs, lessons, sources) before it serves anything, the audio and video files when they
are first used.

Three kinds of file (`classify`):

- hot: everything small. The folder always holds all of them. A write is put into the container under the Store's own
  lock, so the container sees the writes in the order the folder did.
- cold: the audio (.mp3) and video (.webm) files. They are written once and never changed (their names are hashes of
  what they say), so they are put after the lock is released, and they are fetched from the container when first used.
- skipped: temporary files, the sourcing leases (they only mean something on one machine) and the podcast's joined
  episodes (built again from the narration when asked for).

Rules that keep the data safe:

- A folder that has data is the truth: the container is made to match it, never the other way round. Only a folder
  that is empty, or that a restore began and did not finish (`.blob-restoring`), is filled from the container, and
  then Dojo will not start without the container, because starting with nothing would begin a second Record.
- A delete is written to `.blob-deletes.json` before anything is removed, and is repeated until the container has
  done it, so a delete that storage could not do at the time is not undone by a later restore.
- A write that storage cannot take is not lost and does not fail the request: the folder has it, and the background
  thread puts it again until storage takes it. `status()` says how many wait, and /healthz says "degraded" while
  storage does not answer.
- Every file is sent with its MD5 (the service refuses a body that does not match it and keeps the sum), and a pass
  ends by listing the container again and comparing every file with the folder: `.blob-report.json` holds the result.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import httpx

log = logging.getLogger("dojo.blob")

API_VERSION = "2023-11-03"
SCOPE = "https://storage.azure.com/.default"
MAX_BLOB_BYTES = 256 * 1024 * 1024   # one Put Blob; the biggest film is about 23 MB
COLD_SUFFIXES = (".mp3", ".webm")
RESTORING = ".blob-restoring"
JOURNAL = ".blob-deletes.json"
REPORT = ".blob-report.json"
HOT, COLD, SKIP = "hot", "cold", "skip"
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")   # the Store's rule for a name part
_TRANSIENT = (408, 429, 500, 502, 503, 504)
_TYPES = {".json": "application/json", ".jsonl": "application/x-ndjson", ".mp3": "audio/mpeg", ".webm": "video/webm"}
# For a call made while the Store's lock is held: short waits and no second try, so storage that hangs cannot freeze the
# app. What fails is put again by the background thread.
FAST = httpx.Timeout(30.0, connect=3.0)


class BlobError(OSError):
    """Storage could not do what was asked. An OSError, so code that already copes with a failing disk copes with this."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class BlobPaused(BlobError):
    """Not asked at all: storage failed twice a moment ago, and asking again would only wait for another timeout."""


@dataclass(frozen=True)
class BlobInfo:
    size: int
    md5: str            # base64, as the service reports it; "" if it has none
    lm: int = 0         # modification time (ns) of the folder's file when it was put
    modified: float = 0.0


def classify(rel: str) -> str:
    """HOT, COLD or SKIP for a name relative to the data folder."""
    parts = rel.split("/")
    name = parts[-1]
    if name.startswith(".") or name.endswith((".tmp", ".part", ".lease")):
        return SKIP
    if parts[0] == "podcast" and name.endswith(".mp3"):
        return SKIP
    return COLD if name.endswith(COLD_SUFFIXES) else HOT


def valid(rel: str) -> bool:
    """A name that a blob may carry into the folder: the Store's own rule, so a name cannot climb out of it."""
    return bool(rel) and all(_PART.match(p) and ".." not in p for p in rel.split("/"))


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")


class BlobClient:
    """The few Blob service calls Dojo needs, over HTTPS with the app's own Microsoft Entra token (no keys).
    `token` returns a bearer token for SCOPE. Transient failures are tried again after a pause; the rest raise BlobError."""

    def __init__(self, account: str, container: str, token: Callable[[], str], http: httpx.Client | None = None,
                 endpoint: str | None = None, sleep: Callable[[float], None] = time.sleep, retries: int = 2):
        self.base = f"{(endpoint or f'https://{account}.blob.core.windows.net').rstrip('/')}/{container}"
        self._token = token
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))
        self._sleep = sleep
        self._retries = retries

    def _url(self, name: str) -> str:
        return f"{self.base}/{quote(name, safe='/')}"

    def _headers(self, extra: dict | None = None) -> dict:
        try:
            token = self._token()
        except Exception as e:  # noqa: BLE001 - whatever the credential raises, storage cannot be reached
            raise BlobError(f"no token for storage: {type(e).__name__}", 401, "token") from e
        headers = {"Authorization": f"Bearer {token}", "x-ms-version": API_VERSION, "x-ms-date": formatdate(usegmt=True)}
        headers.update(extra or {})
        return headers

    def _pause(self, attempt: int) -> None:
        if attempt < self._retries:
            self._sleep(min(8.0, 0.5 * 3 ** attempt))

    def _send(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
              content: bytes | None = None, ok: tuple[int, ...] = (200,), fast: bool = False) -> httpx.Response:
        last: BlobError | None = None
        tries = 1 if fast else self._retries + 1
        extra = {"timeout": FAST} if fast else {}
        for attempt in range(tries):
            try:
                r = self._http.request(method, url, params=params, headers=self._headers(headers), content=content, **extra)
            except httpx.HTTPError as e:
                last = BlobError(f"{method}: {type(e).__name__}")
            else:
                if r.status_code in ok:
                    return r
                code = r.headers.get("x-ms-error-code", "")
                last = BlobError(f"{method}: {r.status_code} {code}".strip(), r.status_code, code)
                if r.status_code not in _TRANSIENT:
                    raise last
            if attempt + 1 < tries:
                self._pause(attempt)
        raise last  # type: ignore[misc]

    def list(self, prefix: str = "") -> dict[str, BlobInfo]:
        """Every blob in the container (or under a prefix), with its size, MD5 and the `lm` metadata."""
        out: dict[str, BlobInfo] = {}
        marker = ""
        while True:
            params = {"restype": "container", "comp": "list", "maxresults": "5000", "include": "metadata"}
            if prefix:
                params["prefix"] = prefix
            if marker:
                params["marker"] = marker
            try:
                root = ET.fromstring(self._send("GET", self.base, params=params).content)
            except ET.ParseError as e:
                raise BlobError("the listing of the container was not readable") from e
            for blob in root.iterfind("./Blobs/Blob"):
                props = blob.find("Properties")
                if props is None:
                    continue
                meta = blob.find("Metadata")
                try:
                    modified = parsedate_to_datetime(props.findtext("Last-Modified") or "").timestamp()
                except (TypeError, ValueError):
                    modified = 0.0
                lm = (meta.findtext("lm") if meta is not None else None) or "0"
                out[blob.findtext("Name") or ""] = BlobInfo(int(props.findtext("Content-Length") or 0),
                                                            props.findtext("Content-MD5") or "", int(lm) if lm.isdigit() else 0, modified)
            marker = root.findtext("NextMarker") or ""
            if not marker:
                return out

    def put(self, name: str, data: bytes, md5: str, lm: int = 0, fast: bool = False) -> None:
        """Writes a whole blob. The service refuses it if the body does not match `md5`, and keeps the sum.
        `fast`: one try with short waits, for a call made while the Store's lock is held."""
        if len(data) > MAX_BLOB_BYTES:
            raise BlobError(f"{name} is too large for one upload ({len(data)} bytes)", 413)
        ctype = _TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
        self._send("PUT", self._url(name), content=data, ok=(201,), fast=fast,
                   headers={"x-ms-blob-type": "BlockBlob", "Content-MD5": md5, "x-ms-meta-lm": str(lm), "Content-Type": ctype})

    def get(self, name: str, dest: Path) -> None:
        """Downloads a blob into `dest` (written beside it first and renamed, so a reader never sees half a file)
        and checks the MD5 the service holds for it."""
        url = self._url(name)
        last: BlobError | None = None
        for attempt in range(self._retries + 1):
            part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
            try:
                with self._http.stream("GET", url, headers=self._headers()) as r:
                    if r.status_code != 200:
                        r.read()
                        code = r.headers.get("x-ms-error-code", "")
                        last = BlobError(f"GET: {r.status_code} {code}".strip(), r.status_code, code)
                        if r.status_code not in _TRANSIENT:
                            raise last
                    else:
                        digest = hashlib.md5(usedforsecurity=False)
                        with part.open("wb") as f:
                            for chunk in r.iter_bytes(1 << 20):
                                digest.update(chunk)
                                f.write(chunk)
                            f.flush()
                            os.fsync(f.fileno())
                        want = r.headers.get("Content-MD5", "")
                        if want and want != base64.b64encode(digest.digest()).decode("ascii"):
                            last = BlobError(f"GET: {name} arrived damaged")
                        else:
                            os.replace(part, dest)
                            return
            except httpx.HTTPError as e:
                last = BlobError(f"GET: {type(e).__name__}")
            finally:
                part.unlink(missing_ok=True)
            self._pause(attempt)
        raise last  # type: ignore[misc]

    def delete(self, name: str, fast: bool = False) -> None:
        self._send("DELETE", self._url(name), ok=(202, 404), fast=fast)


class BlobMirror:
    """Keeps one data folder and one blob container the same. The Store calls `wrote`, `begin_delete` and
    `finish_delete`; a background thread finishes what could not be done at once and brings the two in line."""

    def __init__(self, root: Path, client: BlobClient, lock: threading.RLock, workers: int = 6, retry_s: float = 15.0,
                 reconcile_s: float = 3600.0, connect_attempts: int = 4, connect_pause_s: float = 5.0):
        self.root = root
        self.client = client
        self.lock = lock                       # the Store's lock: hot files are put and compared under it
        self.workers = workers
        self.retry_s = retry_s
        self.reconcile_s = reconcile_s
        self.connect_attempts = connect_attempts
        self.connect_pause_s = connect_pause_s
        self.index: dict[str, BlobInfo] = {}
        self.errors = 0
        self.last_error = ""
        self._state = threading.Lock()         # guards what follows; never held during a request or a write to disk
        self._journal_lock = threading.Lock()  # one journal write at a time
        self._known: dict[str, tuple[int, int]] = {}   # name -> (size, mtime_ns) of the folder's file as last put or confirmed
        self._sent: dict[str, str] = {}                # name -> MD5 sent, to check against what the service lists
        self._pending: set[str] = set()                # names the folder has and storage has not taken yet
        self._dead: dict[str, str] = {}                # names a delete is owed for (name -> when it was asked)
        self._cold_todo = 0
        self._synced = False                           # the hot files were brought in line once
        self._sized = False                            # the sizes of audio and video files were compared once
        self._hot_done = False                         # the pass over the small files ran to its end
        self._verified = False
        self._report: dict = {}
        self._healthy = False                          # the last thing asked of storage worked
        self._streak = 0
        self._down_until = 0.0
        self._fetching: dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ names

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def kind(self, path: Path) -> str:
        return classify(self._rel(path))

    def _walk(self, *kinds: str, top: Path | None = None) -> Iterable[tuple[str, Path]]:
        for here, _, files in os.walk(top or self.root):
            base = Path(here)
            for name in files:
                path = base / name
                rel = path.relative_to(self.root).as_posix()
                if classify(rel) in kinds and valid(rel):
                    yield rel, path

    def _has_local_hot(self) -> bool:
        return next(iter(self._walk(HOT)), None) is not None

    # ------------------------------------------------------------ start, stop, status

    def start(self, thread: bool = True) -> None:
        """Connects, restores an empty folder from the container, and starts the background sync. Raises BlobError
        when the folder has nothing to stand on and storage cannot be reached."""
        self._load_journal()
        needs_storage = (self.root / RESTORING).exists() or not self._has_local_hot()
        try:
            self._connect_with_retries()
        except BlobError as e:
            if needs_storage:
                raise BlobError("the data folder is empty or only half restored and storage cannot be reached, so Dojo does "
                                f"not start: a second, empty Record must never begin ({e})") from e
            log.error("storage cannot be reached; Dojo goes on with the files on the disk and keeps trying (%s)", e)
        else:
            if needs_storage:
                self._restore()
        if thread:
            self._thread = threading.Thread(target=self._run, name="dojo-blob", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=15)

    def status(self) -> dict:
        """For /healthz ("state" only) and the owner's diagnostics."""
        with self._state:
            waiting, owed, todo = len(self._pending), len(self._dead), self._cold_todo
            synced, objects, healthy = self._synced, len(self.index), self._healthy
        down = (not healthy) or time.monotonic() < self._down_until
        state = "degraded" if down else ("ok" if synced and not (waiting or owed or todo) else "migrating")
        return {"state": state, "objects": objects, "waiting": waiting, "deleting": owed, "audio_video_to_send": todo,
                "errors": self.errors, "last_error": self.last_error}

    def _unsettled(self) -> bool:
        with self._state:
            return (not self._healthy) or bool(self._pending or self._dead or self._cold_todo) or not self._synced

    # ------------------------------------------------------------ failures

    def _failed(self, error: Exception) -> None:
        with self._state:
            self.errors += 1
            self.last_error = f"{type(error).__name__}: {str(error)[:160]}"
            self._healthy = False
            self._streak += 1
            if self._streak >= 2:
                self._down_until = time.monotonic() + 20.0   # stop asking for a moment: every ask would wait for a timeout

    def _ok(self) -> None:
        with self._state:
            self._healthy = True
            self._streak = 0
            self._down_until = 0.0

    def _check_up(self) -> None:
        if time.monotonic() < self._down_until:
            raise BlobPaused("storage did not answer a moment ago; waiting before asking again")

    def _call(self, fn: Callable, *args, **kwargs):
        """One request to storage, with the bookkeeping: any failure but "no such blob" counts against storage (and, twice
        in a row, pauses asking for a moment); an answer that only says there is no such blob does not."""
        self._check_up()
        try:
            out = fn(*args, **kwargs)
        except BlobError as e:
            if e.status == 404:
                self._ok()
            else:
                self._failed(e)
            raise
        self._ok()
        return out

    # ------------------------------------------------------------ the Store's calls

    def wrote(self, path: Path, data: bytes | None = None) -> None:
        """A file was written in the folder: put it into the container too. Never raises: if storage cannot take
        it now, the name waits and the background thread puts it. A small file is put under the Store's lock, so it
        is put quickly or not at all; an audio or video file is put without it, with the usual patience."""
        rel = self._rel(path)
        kind = classify(rel)
        if kind == SKIP or not valid(rel):
            return
        try:
            if data is None:
                data = path.read_bytes()
            self._put(rel, data, path.stat().st_mtime_ns, path, fast=kind == HOT)
        except Exception as e:  # noqa: BLE001 - a write must never fail because storage did
            if not isinstance(e, BlobError):
                log.warning("could not put %s into storage: %s", rel, type(e).__name__)
            with self._state:
                self._pending.add(rel)
            self._wake.set()

    def _put(self, rel: str, data: bytes, mtime_ns: int, path: Path | None = None, fast: bool = False) -> None:
        md5 = md5_b64(data)
        self._call(self.client.put, rel, data, md5, mtime_ns, fast=fast)
        with self._state:
            self.index[rel] = BlobInfo(len(data), md5, mtime_ns, time.time())
            self._known[rel] = (len(data), mtime_ns)
            self._sent[rel] = md5
            self._pending.discard(rel)
            owed = self._dead.pop(rel, None) is not None
        if owed:
            self._save_journal()   # written again after this name: its old delete must not run now
        if path is not None and not path.exists():
            # An audio or video file is put outside the Store's lock, so it can be deleted while it is being put:
            # storage must not keep what the folder no longer has.
            with self._state:
                self._dead.setdefault(rel, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._save_journal()
            self.finish_delete([rel], fast=fast)

    def begin_delete(self, path: Path, tree: bool = False) -> list[str]:
        """Before a file (or a whole folder) leaves the data folder: write down that storage owes a delete.
        Returns the names, for `finish_delete`."""
        rel = self._rel(path)
        names: set[str] = set()
        with self._state:
            names.update(n for n in self.index if (n.startswith(rel + "/") if tree else n == rel) and classify(n) != SKIP)
            self._pending = {n for n in self._pending if not (n.startswith(rel + "/") if tree else n == rel)}
        if tree:
            names.update(r for r, _ in self._walk(HOT, COLD, top=path))
        elif classify(rel) != SKIP and valid(rel):
            names.add(rel)
        if names:
            when = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._state:
                for n in names:
                    self._dead.setdefault(n, when)
            self._save_journal()
        return sorted(names)

    def finish_delete(self, names: list[str], fast: bool = True) -> None:
        """After the folder's copy is gone: have storage delete its own. What fails stays owed, and is repeated.
        Called with the Store's lock held, so by default each delete is tried once and quickly."""
        for rel in names:
            try:
                self._call(self.client.delete, rel, fast=fast)
            except BlobError:
                self._wake.set()
                return
            with self._state:
                self._dead.pop(rel, None)
                self.index.pop(rel, None)
                self._known.pop(rel, None)
                self._sent.pop(rel, None)
        if names:
            self._save_journal()

    # ------------------------------------------------------------ audio and video: listing, fetching, evicting

    def names_in(self, folder: str) -> set[str]:
        """The audio and video files storage holds in this folder ("" = the data folder itself)."""
        prefix = folder + "/" if folder else ""
        with self._state:
            return {n[len(prefix):] for n in self.index
                    if n.startswith(prefix) and "/" not in n[len(prefix):] and classify(n) == COLD and valid(n) and n not in self._dead}

    def stats_in(self, folder: str, suffix: str) -> list[tuple[str, int, float]]:
        prefix = folder + "/" if folder else ""
        with self._state:
            return [(n[len(prefix):], i.size, i.modified) for n, i in self.index.items()
                    if n.startswith(prefix) and "/" not in n[len(prefix):] and n.endswith(suffix)
                    and classify(n) == COLD and valid(n) and n not in self._dead]

    def has(self, path: Path) -> bool:
        rel = self._rel(path)
        with self._state:
            return rel in self.index and rel not in self._dead and classify(rel) == COLD

    def fetch(self, path: Path) -> bool:
        """Brings a cold file from the container into the folder. False when storage does not have it; BlobError when
        storage has it and cannot be reached."""
        rel = self._rel(path)
        if not self.has(path):
            return False
        with self._state:
            lock = self._fetching.setdefault(rel, threading.Lock())
        with lock:
            if path.is_file():
                return True
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._call(self.client.get, rel, path)
            except BlobError as e:
                if e.status == 404:
                    with self._state:
                        self.index.pop(rel, None)
                    return False
                raise
            st = path.stat()
            with self._state:
                self._known[rel] = (st.st_size, st.st_mtime_ns)
        return True

    def safe_to_evict(self, path: Path) -> bool:
        """The folder's copy of a cold file may go only when storage holds a copy of the same size."""
        rel = self._rel(path)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        with self._state:
            have = self.index.get(rel)
            return bool(have and classify(rel) == COLD and rel not in self._pending and rel not in self._dead
                        and have.md5 and have.size == size)

    # ------------------------------------------------------------ the journal of deletes owed

    def _load_journal(self) -> None:
        try:
            doc = json.loads((self.root / JOURNAL).read_text("utf-8"))
        except (OSError, ValueError):
            return
        names = doc.get("names") if isinstance(doc, dict) else None
        if isinstance(names, dict):
            with self._state:
                self._dead = {str(k): str(v) for k, v in names.items() if valid(str(k))}

    def _save_journal(self) -> None:
        with self._journal_lock:
            with self._state:
                body = json.dumps({"names": dict(sorted(self._dead.items()))}, indent=0)
            tmp = self.root / f".{JOURNAL}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.root / JOURNAL)
            except OSError as e:
                log.error("could not write %s: %s", JOURNAL, type(e).__name__)
                tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------ connecting and restoring

    def _refresh(self) -> None:
        """Reads the container's listing again."""
        started = time.time()
        fresh = self.client.list()
        with self._state:
            self._take(fresh, started)
        self._ok()

    def _take(self, fresh: dict[str, BlobInfo], started: float) -> None:
        """Makes a listing the index. What this process put while the listing ran is newer than the listing: it stays.
        Call with `_state` held."""
        for rel, info in self.index.items():
            if info.modified >= started - 1 and rel not in self._dead:
                fresh[rel] = info
        self.index = fresh

    def _connect_with_retries(self) -> None:
        last: BlobError | None = None
        for attempt in range(self.connect_attempts):
            try:
                self._refresh()
                return
            except BlobError as e:
                self._failed(e)
                last = e
                if attempt + 1 < self.connect_attempts:
                    time.sleep(self.connect_pause_s)
        raise last  # type: ignore[misc]

    def _restore(self) -> None:
        """Fills the folder with every hot file the container holds that the folder lacks. Blocks: nothing is served
        before the Record and the documents are back."""
        marker = self.root / RESTORING
        marker.write_text("A restore from storage began here and is not finished. Delete this file only once it is.\n", "utf-8")
        self._apply_deletes()
        with self._state:
            want = [n for n in self.index if classify(n) == HOT and valid(n) and n not in self._dead]
        missing = [n for n in want if not (self.root / n).exists()]
        log.warning("restoring %d of %d files from storage into the data folder", len(missing), len(want))

        def one(rel: str) -> None:
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.client.get(rel, dest)

        with ThreadPoolExecutor(max_workers=max(self.workers, 8)) as pool:
            for future in [pool.submit(one, n) for n in missing]:
                future.result()   # a file that cannot be restored stops the start: a half restored Record must not run
        for rel in want:
            try:
                st = (self.root / rel).stat()
            except OSError:
                continue
            with self._state:
                self._known[rel] = (st.st_size, st.st_mtime_ns)
        marker.unlink(missing_ok=True)
        log.warning("restore finished")

    # ------------------------------------------------------------ the background thread

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001 - the thread must outlive any one failure
                log.exception("storage sync failed")
            self._wake.wait(self.retry_s if self._unsettled() else self.reconcile_s)
            self._wake.clear()

    def sync_once(self) -> None:
        """One pass: owed deletes, failed puts, then every file the folder has and storage lacks, the hot files first,
        and at the end a comparison of storage with the folder."""
        try:
            self._refresh()
        except BlobError as e:
            self._failed(e)
            return
        self._apply_deletes()
        put = self._retry_pending() + self._reconcile_hot() + self._migrate_cold()
        if put or not self._verified:
            self._verify()
        self._write_report()

    def _apply_deletes(self) -> None:
        with self._state:
            owed = sorted(self._dead)
        for rel in owed:
            with self.lock:
                with self._state:
                    if rel not in self._dead:
                        continue   # written again since
                try:
                    self._call(self.client.delete, rel, fast=True)   # under the Store's lock: once, and quickly
                except BlobError:
                    return
                with self._state:
                    self._dead.pop(rel, None)
                    self.index.pop(rel, None)
                    self._known.pop(rel, None)
                    self._sent.pop(rel, None)
        if owed:
            self._save_journal()

    def _retry_pending(self) -> int:
        """Puts the small files that failed to be put. (An audio or video file that failed is found by `_migrate_cold`,
        which puts what the folder has and storage lacks.) Returns how many were put."""
        with self._state:
            names = sorted(self._pending)
        put = 0
        for rel in names:
            path = self.root / rel
            if classify(rel) == COLD:
                if not path.exists():
                    with self._state:
                        self._pending.discard(rel)
                continue
            with self.lock:
                try:
                    data = path.read_bytes()
                    mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    with self._state:
                        self._pending.discard(rel)   # gone from the folder since: nothing to put
                    continue
                try:
                    self._put(rel, data, mtime_ns, fast=True)
                except BlobError:
                    return put
            put += 1
        return put

    def _reconcile_hot(self) -> int:
        """Every hot file in the folder that storage does not hold (or holds differently) is put. The folder wins.
        Returns how many were put."""
        put = 0
        for rel, path in self._walk(HOT):
            try:
                st = path.stat()
            except OSError:
                continue
            with self._state:
                if rel in self._dead or (self._known.get(rel) == (st.st_size, st.st_mtime_ns) and rel in self.index):
                    continue
            with self.lock:   # the lock every write takes: a write cannot slip in between the read and the put
                try:
                    data = path.read_bytes()
                    st = path.stat()
                except OSError:
                    continue
                md5 = md5_b64(data)
                with self._state:
                    have = self.index.get(rel)
                if have and have.size == len(data) and have.md5 == md5:
                    with self._state:
                        self._known[rel] = (st.st_size, st.st_mtime_ns)
                        self._sent.setdefault(rel, md5)
                    continue
                try:
                    self._put(rel, data, st.st_mtime_ns, fast=True)
                except BlobError:
                    return put
            put += 1
        self._hot_done = True
        return put

    def _migrate_cold(self) -> int:
        """Puts the audio and video files the folder has and storage lacks. Returns how many were put."""
        todo: list[tuple[str, Path]] = []
        clash = 0
        check = not self._sized
        for rel, path in self._walk(COLD):
            with self._state:
                have, dead = self.index.get(rel), rel in self._dead
            if dead:
                continue
            if have is None:
                todo.append((rel, path))
            elif check:
                try:
                    clash += path.stat().st_size != have.size   # never overwrite either side of a file that should not change
                except OSError:
                    pass
        self._sized = True
        with self._state:
            self._cold_todo = len(todo)
            self._synced = self._hot_done   # "ok" only once both kinds are known to be in line
        if clash:
            log.error("%d audio or video files differ in size from storage's copy; neither was changed", clash)
        if not todo:
            return 0
        log.warning("putting %d audio and video files into storage", len(todo))
        sent = [0]
        halt = threading.Event()

        def one(item: tuple[str, Path]) -> None:
            rel, path = item
            if halt.is_set() or self._stop.is_set():
                return
            try:
                self._put(rel, path.read_bytes(), path.stat().st_mtime_ns, path)
            except BlobError:
                halt.set()   # storage is not answering: the next pass tries again
                return
            except OSError:
                return
            with self._state:
                self._cold_todo = max(0, self._cold_todo - 1)
                sent[0] += 1
                if sent[0] % 500 == 0:
                    log.warning("put %d of %d audio and video files into storage", sent[0], len(todo))

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(one, todo))
        return sent[0]

    def _verify(self) -> None:
        """Lists the container again and compares every file in the folder with it: the size always, and the MD5 the
        service holds against the one that was sent."""
        started = time.time()
        try:
            fresh = self.client.list()
        except BlobError as e:
            self._failed(e)
            return
        self._ok()
        with self._state:
            self._take(fresh, started)
            sent = dict(self._sent)
            dead = set(self._dead)
        missing: list[str] = []
        size: list[str] = []
        md5: list[str] = []
        checked = 0
        for rel, path in self._walk(HOT, COLD):
            if rel in dead:
                continue
            try:
                local = path.stat().st_size
            except OSError:
                continue
            checked += 1
            have = fresh.get(rel)
            if have is None:
                missing.append(rel)
            elif have.size != local:
                size.append(rel)
            elif rel in sent and have.md5 and have.md5 != sent[rel]:
                md5.append(rel)
        self._verified = True
        self._report = {"checked": checked, "missing_count": len(missing), "missing": missing[:25],
                        "size_differs_count": len(size), "size_differs": size[:25],
                        "md5_differs_count": len(md5), "md5_differs": md5[:25]}
        if missing or size or md5:
            log.error("storage does not match the folder: %d missing, %d of another size, %d of another MD5",
                      len(missing), len(size), len(md5))
        else:
            log.warning("storage matches the folder: %d files checked (the size of each, the MD5 of each file sent)", checked)

    def _write_report(self) -> None:
        with self._state:
            body = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "objects": len(self.index),
                    "bytes": sum(i.size for i in self.index.values()), "waiting": len(self._pending),
                    "deleting": len(self._dead), "audio_video_to_send": self._cold_todo, "errors": self.errors,
                    "last_error": self.last_error, "verification": self._report}
        tmp = self.root / f".{REPORT}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_text(json.dumps(body, indent=1), "utf-8")
            os.replace(tmp, self.root / REPORT)
        except OSError:
            tmp.unlink(missing_ok=True)
