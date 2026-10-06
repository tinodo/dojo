"""A private podcast feed per exam: one episode per skill, made from the latest lesson's Deeper Listen once it is
ready (ADR 0007), else from its narrated slides.

App Service authentication does not guard /feed/*, so the long random token in the path is the only
credential. Dojo keeps a hash of it, never the token itself. The feed reads lessons, their narration audio
and the package metadata, nothing from the learner's record, and it writes nothing about the learner. Its
only writes are the joined lesson audio and, once per lesson, that the feed has listed or served it (AIRED).

Dojo cannot see what a podcast app plays. So, on the signed-in side only, the answer form asks before an
answer that could count as "later" whether the learner listened (question and declare, at the end), and a
timed practice exam asks at its end whether the learner listened during it (app/exam.py). A downloaded
episode still plays after a newer lesson or new narration takes its place and after the feed is turned
off, so both keep asking about every lesson the feed has ever listed or served, until the record is deleted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from datetime import timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape as xml_escape

import segno
from fastapi import HTTPException

from .core import APP_DIR, Abandoned, Learner, Store, iso, parse_iso, utcnow
from .deeplink import go_link

log = logging.getLogger("dojo.podcast")

TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
EPISODE_KEY = re.compile(r"^e[0-9a-f]{39}$")
JOIN_VERSION = "1"
BUILD_BUDGET_S = 12.0
LICENCES = {"CC-BY-4.0": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/")}
_NO_LINK = hashlib.sha256(b"dojo-feed:no link").hexdigest()
# "Later" is measured when an answer is judged, which can be a while after it is sent, so Dojo starts
# asking this long before an answer would reach "later".
ASK_EARLY_MIN = 60
ANSWER_IT_FIRST = "Answer the podcast question first: did you listen to this lesson in a podcast app?"
# Every lesson the feed has listed or served, by exam and skill, with when it first did:
# {package: {skill: {lesson id: time}}}. It names lessons, not requests: one entry per lesson, written once,
# before the feed first lists or serves it. New links, turning the feed off and new narration keep it; only
# deleting the record removes it (Podcast.forget).
AIRED = "aired"
# Joined files that no current episode uses are looked for this long after the last new one was made.
PRUNE_AFTER_S = 30.0
# Such a file is removed once no episode has used it for this long, so a download that was handed the file
# just before a newer lesson or new narration took its place can still open it.
UNUSED_GRACE_S = 600.0


# ---------------------------------------------------------------- joining MP3 files

class AudioError(ValueError):
    """The bytes are not plain MPEG audio Layer III frames of one constant encoding."""


_KBPS_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_KBPS_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_HZ = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _skip_id3v2(data: bytes) -> int:
    pos = 0
    while data[pos:pos + 3] == b"ID3":
        head = data[pos:pos + 10]
        if len(head) < 10 or any(b & 0x80 for b in head[6:10]):
            raise AudioError("a damaged ID3 tag")
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        pos += 10 + size + (10 if head[5] & 0x10 else 0)
    return pos


def _frame(header: int) -> tuple[tuple[int, int, int, int], int, int]:
    """The encoding (version, kbit/s, Hz, channel mode), byte size and sample count of one frame."""
    if header >> 21 != 0x7FF:
        raise AudioError("no frame where one should start")
    version, layer = (header >> 19) & 3, (header >> 17) & 3
    rate, hz_index = (header >> 12) & 15, (header >> 10) & 3
    if version == 1 or layer != 1 or rate in (0, 15) or hz_index == 3:
        raise AudioError("not an MPEG audio Layer III frame")
    kbps = (_KBPS_V1 if version == 3 else _KBPS_V2)[rate]
    hz = _HZ[version][hz_index]
    size = (144 if version == 3 else 72) * kbps * 1000 // hz + ((header >> 9) & 1)
    return (version, kbps, hz, (header >> 6) & 3), size, (1152 if version == 3 else 576)


def _is_info_frame(frame: bytes, version: int, mode: int, crc: bool) -> bool:
    """A Xing, Info or VBRI frame describes the file it heads; after joining it would describe the wrong file."""
    side = (17 if mode == 3 else 32) if version == 3 else (9 if mode == 3 else 17)
    at = 4 + (2 if crc else 0) + side
    return frame[at:at + 4] in (b"Xing", b"Info") or frame[36:40] == b"VBRI"


def _audio_span(data: bytes) -> tuple[tuple[int, int, int, int], int, int, int, int]:
    """Encoding, first and end byte, frame count and samples per frame of the audio frames in one file."""
    pos, end = _skip_id3v2(data), len(data)
    fmt, first, frames, samples = None, pos, 0, 0
    while end - pos >= 4:
        if end - pos == 128 and data[pos:pos + 3] == b"TAG":
            break
        header = int.from_bytes(data[pos:pos + 4], "big")
        f, size, samples = _frame(header)
        if pos + size > end:
            break  # a cut-off last frame is left out
        if fmt is None:
            fmt = f
            if _is_info_frame(data[pos:pos + size], f[0], f[3], not (header >> 16) & 1):
                pos += size
                first = pos
                continue
        elif f != fmt:
            raise AudioError("the encoding changes inside the file")
        frames += 1
        pos += size
    if not frames or fmt is None:
        raise AudioError("no audio frames")
    return fmt, first, pos, frames, samples


def join_mp3(parts: list[bytes]) -> tuple[bytes, float]:
    """One MP3 made of the audio frames of each part, in order, and its length in seconds.

    MP3 frames are independent, so frames of the same constant encoding can follow each other. Tags and
    a leading Info frame are dropped. Parts with different encodings raise AudioError.
    """
    if not parts:
        raise AudioError("nothing to join")
    out, fmt0, frames, samples = bytearray(), None, 0, 0
    for data in parts:
        fmt, first, stop, n, samples = _audio_span(data)
        if fmt0 is None:
            fmt0 = fmt
        elif fmt != fmt0:
            raise AudioError("the parts are encoded differently")
        out += data[first:stop]
        frames += n
    return bytes(out), frames * samples / fmt0[2]


def episode_key(media_ids: list[str]) -> str:
    """Names the joined file after the slide audio it is made of, so new narration makes a new file."""
    return "e" + hashlib.sha256(f"{JOIN_VERSION}:{','.join(media_ids)}".encode("ascii")).hexdigest()[:39]


# ---------------------------------------------------------------- the secret link

def token_hash(token: str) -> str:
    return hashlib.sha256(("dojo-feed:" + token).encode("utf-8")).hexdigest()


def _link_doc(store: Store, key: str) -> dict | None:
    try:
        doc = store.read("learners", key, "feed")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("hash"), str) else None


def find_by_token(digest: str, well_formed: bool, candidates: list[Learner], stored: Callable[[str], dict | None],
                  no_link: str) -> Learner | None:
    """The learner whose link this is, or None. The hash is compared with every candidate's stored hash,
    each in constant time and all of them every time, so timing tells nothing about who has a link."""
    found, seen = None, set()
    for c in candidates:
        if c.key in seen:
            continue
        seen.add(c.key)
        doc = stored(c.key)
        same = hmac.compare_digest(digest, doc["hash"] if doc else no_link)
        if same and well_formed and doc and found is None:
            found = c
    return found


def members_of(members: Callable[[], list[Learner]]) -> list[Learner]:
    """The active members, or none when they cannot be read: the owner's link never depends on them."""
    try:
        return list(members())
    except Exception:  # noqa: BLE001 - members.json unreadable: only the owner is looked up
        log.warning("members could not be read for a link lookup")
        return []


class FeedLinks:
    """Each learner's one podcast link. Only a SHA-256 of its token is stored, next to that learner's
    profile. A token is looked up by its hash among the owner (always, from the settings) and the active
    members (ADR 0009), so no index file is needed and the owner's link keeps working as it was."""

    def __init__(self, store: Store, owner: Learner):
        self.store = store
        self.owner = owner
        self.members: Callable[[], list[Learner]] = lambda: []

    def _doc(self, learner: Learner | None = None) -> dict | None:
        return _link_doc(self.store, (learner or self.owner).key)

    def active(self, learner: Learner | None = None) -> bool:
        return self._doc(learner) is not None

    def status(self, learner: Learner | None = None) -> dict:
        doc = self._doc(learner)
        return {"active": doc is not None, "created": doc.get("created") if doc else None}

    def create(self, replace: bool = False, learner: Learner | None = None) -> str:
        """A new token. It is returned once and never stored; making a new one ends the old one at once."""
        learner = learner or self.owner
        with self.store.lock:
            if self._doc(learner) and not replace:
                raise HTTPException(409, "You already have a podcast link. Dojo cannot show it again; make a new link instead.")
            token = secrets.token_urlsafe(32)
            self.store.write("learners", learner.key, "feed", value={"hash": token_hash(token), "created": iso()})
        return token

    def delete(self, learner: Learner | None = None) -> None:
        """Turns the feed off: every link answers 404 at once. Episodes already downloaded still play, so what
        the feed listed is kept (AIRED), and Dojo keeps asking about it."""
        self.store.delete("learners", (learner or self.owner).key, "feed")

    def learner_for(self, token: str) -> Learner | None:
        """Whose link this token is: the owner's or an active member's, else None."""
        well_formed = bool(TOKEN.match(token or ""))
        return find_by_token(token_hash(token if well_formed else ""), well_formed,
                             [self.owner] + members_of(self.members), lambda key: _link_doc(self.store, key), _NO_LINK)

    owner_for = learner_for


# ---------------------------------------------------------------- episodes and the feed

_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _text(value: Any) -> str:
    return xml_escape(_NOT_XML.sub("", str(value)))


def _attr(value: Any) -> str:
    return xml_escape(_NOT_XML.sub("", str(value)), {'"': "&quot;"})


def _cdata(html: str) -> str:
    return "<![CDATA[" + _NOT_XML.sub("", html).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _web(url: Any) -> str | None:
    return url if isinstance(url, str) and re.match(r"^https?://[^\s\"'<>]+$", url) else None


def _rfc2822(value: str) -> str | None:
    try:
        return format_datetime(parse_iso(value).astimezone(timezone.utc), usegmt=True)
    except (TypeError, ValueError):
        return None


def qr_data_uri(url: str) -> str:
    return segno.make_qr(url, error="m").svg_data_uri(scale=5, border=3, dark="#2B2622", light="#FFFFFF")


class Podcast:
    """The private podcast feed (ADR 0002): one feed per exam behind a secret link, one episode per skill
    from the lesson's audio. It notes which lessons it offered, never who fetched them."""
    def __init__(self, dojo: Any, owner: Learner):
        self.dojo = dojo
        self.store: Store = dojo.store
        self.links = FeedLinks(dojo.store, owner)
        self._lessons: dict[str, dict] = {}   # lessons never change once written
        self._have: set[str] = set()
        self._built: dict[str, dict] = {}
        self._bad: set[str] = set()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._pending: dict[str, list[str]] = {}
        self._worker: threading.Thread | None = None
        self._on_air: dict[str, dict] = {}    # AIRED per learner key, read once
        self._legacy = False                  # the owner's AIRED is still podcast/aired.json (migrate_aired)
        self._pruner: threading.Timer | None = None
        self._unused: dict[str, float] = {}   # joined files no current episode uses: since when, by the monotonic clock
        self.prune_after_s = PRUNE_AFTER_S
        self.unused_grace_s = UNUSED_GRACE_S

    # ---- lessons

    def _deeper(self, info: dict) -> dict:
        """The lesson as the feed plays it: its Deeper Listen once that is ready (ADR 0007), else its slides.
        Its media, and so its episode key, change with it: the joined file of the slides goes after the grace
        time, and the episode gets a new guid, so a podcast app fetches it again."""
        if not self.dojo.deep_ready(info["id"]):
            return info
        d = self.dojo.deep(info["id"])
        return {**info, "deep": True, "created": d.get("created") or info["created"],
                "titles": [str(seg.get("title") or "") for seg in d["deep_narration"]], "media": self.dojo._deep_media(d)}

    def _lesson(self, lesson_id: str) -> dict | None:
        info = self._lessons.get(lesson_id)
        if info is None:
            try:
                doc = self.dojo.lesson(lesson_id)
            except HTTPException:
                return None
            info = {"id": doc["id"], "package": doc.get("package"), "skill": doc.get("skill"), "created": doc.get("created") or "",
                    "titles": [str(sl.get("title") or "") for sl in doc.get("slides", [])],
                    "media": self.dojo.slide_media(doc), "sources": doc.get("sources") or []}
            self._lessons[lesson_id] = info
        return self._deeper(info)

    def _withheld(self, lesson_id: str) -> bool:
        """A lesson that a changed source page no longer supports stays out of the feed until the skill's
        lesson is written again and checked (ADR 0002's review trigger, ADR 0006)."""
        drift = getattr(self.dojo, "drift", None)
        return bool(drift and drift.withheld(lesson_id))

    @staticmethod
    def _order(pkg: dict) -> list[str]:
        """The skills that get lessons, in study-guide order. A skill with no verified source gets no lesson."""
        return [sid for d in pkg["domains"] for g in d["groups"] for sid in g["skills"] if pkg["skills"][sid].get("sources")]

    def _plan(self, pid: str) -> list[dict]:
        """Every skill that gets a lesson, in study-guide order, with its latest lesson if it has one and
        that lesson is not held back after a source change."""
        rows = []
        for sid in self._order(self.dojo.packages.get(pid)):
            latest = self.dojo.lessons_for(pid, sid)
            info = self._lesson(latest[0]["id"]) if latest and not self._withheld(latest[0]["id"]) else None
            rows.append({"n": len(rows) + 1, "skill": sid, "lesson": info,
                         "key": episode_key(info["media"]) if info and info["media"] else None})
        return rows

    def _narrated(self, info: dict) -> bool:
        """Whether every slide has its audio. Audio files are never taken away, so a yes is remembered."""
        for m in info["media"]:
            if m not in self._have:
                if not self.dojo.speech.exists(m):
                    return False
                self._have.add(m)
        return bool(info["media"])

    # ---- what the feed has listed or served (AIRED), per learner (ADR 0009)

    def _air_parts(self, learner: Learner) -> tuple[str, ...]:
        if learner.key == self.links.owner.key and self._legacy:
            return ("podcast", AIRED)   # the move into the owner's folder failed: the old file is still read
        return ("learners", learner.key, AIRED)

    def _air(self, learner: Learner) -> dict:
        with self.store.lock:
            if learner.key not in self._on_air:
                doc = self.store.read(*self._air_parts(learner))
                self._on_air[learner.key] = doc if isinstance(doc, dict) else {}
            return self._on_air[learner.key]

    def _aired(self, learner: Learner, pid: Any, sid: Any) -> dict[str, str]:
        """The lessons of one skill that this learner's feed has listed or served, with when it first did."""
        skills = self._air(learner).get(pid)
        lessons = skills.get(sid) if isinstance(skills, dict) else None
        return lessons if isinstance(lessons, dict) else {}

    def _put_on_air(self, learner: Learner, pid: str, lessons: list[tuple[str, str]]) -> bool:
        """Notes that this learner's feed lists or serves these lessons, given as (skill, lesson id), before
        it does, in one write. If that cannot be noted, they are left out: Dojo would not know to ask about
        them. Nothing is noted for a learner whose data was deleted meanwhile (Abandoned)."""
        with self.store.lock:
            air = self._air(learner)
            skills = {sid: dict(seen) for sid, seen in air[pid].items() if isinstance(seen, dict)} if isinstance(air.get(pid), dict) else {}
            now, new_ones = iso(), False
            for sid, lesson_id in lessons:
                seen = skills.setdefault(sid, {})
                if lesson_id not in seen:
                    seen[lesson_id] = now
                    new_ones = True
            if not new_ones:
                return True
            new = {**air, pid: skills}
            try:
                self.store.write(*self._air_parts(learner), value=new)
            except (OSError, Abandoned) as e:
                log.warning("podcast episode left out: Dojo could not note that the feed lists it: %s", type(e).__name__)
                return False
            self._on_air[learner.key] = new
            return True

    def forget(self, learner: Learner) -> None:
        """Deleting a record also removes what that learner's feed has listed or served. The owner's old
        file and the copy the move left behind go too."""
        with self.store.lock:
            self.store.delete("learners", learner.key, AIRED)
            if learner.key == self.links.owner.key:
                self.store.delete("podcast", AIRED)
                self.store.delete_file("podcast", AIRED + ".json.migrated")
                self._legacy = False
            self._on_air.pop(learner.key, None)

    def could_be_on_phone(self, learner: Learner) -> bool:
        """Whether a podcast app could hold one of this learner's episodes: the feed is on, so one can be
        downloaded at any moment, or the feed has listed or served one, which plays on after it leaves."""
        return self.links.active(learner) or any(self._aired(learner, pid, sid) for pid, skills in self._air(learner).items()
                                                 if isinstance(skills, dict) for sid in skills)

    def notes(self, learner: Learner) -> dict:
        """This learner's AIRED, for their export."""
        with self.store.lock:
            return json.loads(json.dumps(self._air(learner)))

    @staticmethod
    def _merged(a: dict, b: dict) -> dict:
        """The union of two AIRED documents, each lesson with the earlier of its two times. It only adds
        notes, so it can only add podcast questions, never take one away."""
        out: dict = {}
        for doc in (a, b):
            for pid, skills in doc.items():
                if not isinstance(skills, dict):
                    continue
                for sid, seen in skills.items():
                    if not isinstance(seen, dict):
                        continue
                    have = out.setdefault(pid, {}).setdefault(sid, {})
                    for lesson, at in seen.items():
                        have[lesson] = min(str(at), have[lesson]) if lesson in have else str(at)
        return out

    def migrate_aired(self) -> str:
        """Moves the owner's notes from podcast/aired.json into the owner's folder (ADR 0009, "The owner's
        live data"). Safe to run on every start: copy and read back; if a copy is already there (a crash
        after copying), merge; then rename the old file. If any step fails, the old file stays the owner's
        and a warning is logged. Says what it did."""
        owner = self.links.owner
        old = self.store.path("podcast", AIRED + ".json")
        with self.store.lock:
            if not old.exists():
                self._legacy = False
                return "nothing to move"
            try:
                before = json.loads(old.read_text("utf-8"))
                if not isinstance(before, dict):
                    raise ValueError("not a document of aired lessons")
                there = self.store.read("learners", owner.key, AIRED)
                target = before if there is None else self._merged(there if isinstance(there, dict) else {}, before)
                if there != target:
                    self.store.write("learners", owner.key, AIRED, value=target)
                    if self.store.read("learners", owner.key, AIRED) != target:
                        raise OSError("the copy reads back differently")
                old.replace(old.with_name(AIRED + ".json.migrated"))
            except (OSError, ValueError, Abandoned) as e:
                log.warning("the podcast notes stay in podcast/aired.json for now: %s", type(e).__name__)
                self._legacy = True
                self._on_air.pop(owner.key, None)
                return "kept the old file"
            self._legacy = False
            self._on_air.pop(owner.key, None)
            return "merged" if there is not None else "moved"

    # ---- joined episode files, built on first use and kept on the data share

    def _key_lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _ready(self, key: str) -> dict | None:
        meta = self._built.get(key) or self.store.read("podcast", key)
        if not (isinstance(meta, dict) and isinstance(meta.get("bytes"), int)):
            return None
        try:
            size = self.store.path("podcast", key + ".mp3").stat().st_size
        except OSError:
            size = -1
        if size != meta["bytes"]:
            self._built.pop(key, None)
            return None
        self._built[key] = meta
        return meta

    def _build(self, key: str, media: list[str]) -> dict | None:
        with self._key_lock(key):
            meta = self._ready(key)
            if meta or key in self._bad:
                return meta
            try:
                parts = [self.store.read_bytes("media", m + ".mp3") for m in media]
                if any(p is None for p in parts):
                    return None
                audio, seconds = join_mp3(parts)  # type: ignore[arg-type]
                meta = {"bytes": len(audio), "seconds": round(seconds, 3), "media": media, "built": iso()}
                self.store.write_bytes("podcast", key + ".mp3", data=audio)
                self.store.write("podcast", key, value=meta)
            except AudioError as e:
                self._bad.add(key)
                log.warning("podcast episode %s left out: %s", key[:10], e)
                return None
            except OSError as e:
                log.warning("podcast episode %s not built yet: %s", key[:10], e)
                return None
            self._built[key] = meta
        self.prune_soon()  # the new file may take the place of an older one
        return meta

    def _queue(self, jobs: list[tuple[str, list[str]]]) -> None:
        with self._guard:
            self._pending.update(jobs)
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, name="dojo-podcast", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._guard:
                if not self._pending:
                    self._worker = None
                    break
                key = next(iter(self._pending))
                media = self._pending.pop(key)
            try:
                self._build(key, media)
            except Exception:  # noqa: BLE001 - a background build must never stop the others
                log.exception("podcast episode %s failed", key[:10])

    def warm(self) -> None:
        """Builds every episode that can be built, in the background, and removes joined files no episode uses."""
        self.prune_soon()
        threading.Thread(target=self._warm, name="dojo-podcast-warm", daemon=True).start()

    def _warm(self) -> None:
        try:
            jobs = [(row["key"], row["lesson"]["media"]) for pid in self.dojo.packages.ids() for row in self._plan(pid)
                    if row["key"] and not self._ready(row["key"]) and self._narrated(row["lesson"])]
        except Exception:  # noqa: BLE001 - warming up is only a head start
            log.exception("podcast warm-up failed")
            return
        if jobs:
            self._queue(jobs)

    def prune_soon(self, delay: float | None = None) -> None:
        """Prunes in the background, prune_after_s after the last call (or `delay`, if longer), so a run of new
        episodes is followed by one prune. Called when a new joined file is made, when the app starts, when a
        link is made, and by a prune that left files still inside their grace time."""
        wait = self.prune_after_s if delay is None else max(delay, self.prune_after_s)
        with self._guard:
            if self._pruner is not None:
                self._pruner.cancel()
            self._pruner = threading.Timer(wait, self._prune_quietly)
            self._pruner.daemon = True
            self._pruner.start()

    def _prune_quietly(self) -> None:
        try:
            self.prune()
        except Exception:  # noqa: BLE001 - tidying up can wait for the next new episode
            log.exception("podcast prune failed")

    def close(self) -> None:
        with self._guard:
            if self._pruner is not None:
                self._pruner.cancel()
                self._pruner = None

    def prune(self) -> None:
        """Removes joined files that no current episode uses, such as those of replaced lessons or narration,
        once they have been unused for unused_grace_s: a download that was handed such a file just before it
        was replaced can still open it. The files are listed before the current episodes are worked out, so a
        file made in between stays. A file still inside its grace time gets another prune when it is up."""
        names = {n.rsplit(".", 1)[0] for n, _, _ in self.store.file_stats("podcast", suffix=".mp3")} | set(self.store.names("podcast"))
        names = {key for key in names if EPISODE_KEY.match(key)}
        if not names:
            return
        keep = {row["key"] for pid in self.dojo.packages.ids() for row in self._plan(pid) if row["key"]}
        now, again = time.monotonic(), None
        with self._guard:
            for key in [k for k in self._unused if k in keep or k not in names]:
                del self._unused[key]
            unused = {key: self._unused.setdefault(key, now) for key in names - keep}
        for key, since in unused.items():
            left = since + self.unused_grace_s - now
            if left > 0:
                again = left if again is None else min(again, left)
                continue
            with self._key_lock(key):
                self._built.pop(key, None)
                try:
                    self.store.delete_file("podcast", key + ".mp3")
                    self.store.delete("podcast", key)
                except OSError:
                    pass
            with self._guard:
                self._unused.pop(key, None)
        if again is not None:
            self.prune_soon(again)

    def episodes(self, pid: str, budget_s: float = BUILD_BUDGET_S, learner: Learner | None = None) -> tuple[list[dict], int]:
        """The ready episodes in study-guide order, and how many skills have no ready episode yet.

        Only a lesson whose slides are all narrated becomes an episode. Episodes not built within the time
        budget are built in the background and counted as not ready in this answer. Each episode listed is
        in the AIRED of the learner whose feed this is (the owner's by default) before this answer goes out.
        """
        learner = learner or self.links.owner
        started, ready, later = time.monotonic(), [], []
        rows = self._plan(pid)
        for row in rows:
            key, info = row["key"], row["lesson"]
            if not key:
                continue
            meta = self._ready(key)
            if meta is None and self._narrated(info):
                if time.monotonic() - started < budget_s:
                    meta = self._build(key, info["media"])
                else:
                    later.append((key, info["media"]))
            if meta:
                ready.append((row, meta))
        if later:
            self._queue(later)
        if ready and not self._put_on_air(learner, pid, [(row["skill"], row["lesson"]["id"]) for row, _ in ready]):
            ready = []
        out = [{**row, "bytes": meta["bytes"], "seconds": meta["seconds"]} for row, meta in ready]
        return out, len(rows) - len(out)

    def _in_feed(self, row: dict) -> bool:
        """Whether this skill's latest lesson is an episode, or will be once its joined file is built."""
        return bool(row["key"]) and row["key"] not in self._bad and bool(self._ready(row["key"]) or self._narrated(row["lesson"]))

    def counts(self, pid: str) -> dict:
        rows = self._plan(pid)
        return {"ready": sum(1 for r in rows if self._in_feed(r)), "total": len(rows)}

    def episode_file(self, lesson_id: str, deep: bool = False, learner: Learner | None = None,
                     packages: set[str] | None = None) -> Path | None:
        """The joined file of a lesson that is a current episode, built now if needed. The lesson is in the
        AIRED of the learner whose feed asks (the owner's by default) before its file is served. `deep` asks
        for its Deeper Listen episode, which replaces the slides one. `packages` limits it to the exams
        that learner is enrolled in."""
        learner = learner or self.links.owner
        info = self._lesson(lesson_id)
        pid, sid = (info or {}).get("package"), (info or {}).get("skill")
        if not info or not info["media"] or bool(info.get("deep")) != deep or pid not in self.dojo.packages.by_id or sid not in self.dojo.packages.get(pid)["skills"]:
            return None
        if packages is not None and pid not in packages:
            return None
        latest = self.dojo.lessons_for(pid, sid)
        if not latest or latest[0]["id"] != lesson_id or self._withheld(lesson_id):
            return None
        key = episode_key(info["media"])
        meta = self._ready(key) or self._build(key, info["media"])
        if not meta or not self._put_on_air(learner, pid, [(sid, lesson_id)]):
            return None
        return self.store.path("podcast", key + ".mp3")

    # ---- what the learner and the podcast app see

    def feed_urls(self, token: str, base: str, packages: list[str] | None = None) -> list[dict]:
        return [{"package": pid, "exam": p["meta"]["exam"], "title": p["meta"]["title"], "url": f"{base}/feed/{token}/{pid}.xml"}
                for pid, p in self.dojo.packages.by_id.items() if packages is None or pid in packages]

    def link_view(self, token: str, base: str, learner: Learner | None = None, packages: list[str] | None = None) -> dict:
        feeds = self.feed_urls(token, base, packages)
        for f in feeds:
            f["qr"] = qr_data_uri(f["url"])
            f.update(self.counts(f["package"]))
        return {**self.links.status(learner), "feeds": feeds}

    def status(self, learner: Learner | None = None, packages: list[str] | None = None) -> dict:
        learner = learner or self.links.owner
        return {**self.links.status(learner), "on_phone": self.could_be_on_phone(learner),
                "packages": [{"package": pid, "exam": p["meta"]["exam"], **self.counts(pid)} for pid, p in self.dojo.packages.by_id.items()
                             if packages is None or pid in packages]}

    def feed_xml(self, pid: str, token: str, base: str, learner: Learner | None = None) -> bytes:
        pkg = self.dojo.packages.get(pid)
        meta = pkg["meta"]
        episodes, missing = self.episodes(pid, learner=learner)
        total = len(episodes) + missing
        feed = f"{base}/feed/{token}"
        title = f"Dojo · {meta['exam']} {meta['title']}"
        home = base + "/"
        image = f"{feed}/{pid}.png"
        if missing == 0:
            readiness = f"All {total} episodes are ready."
        else:
            readiness = (f"{len(episodes)} of {total} episodes {'is' if len(episodes) == 1 else 'are'} ready; "
                         f"{missing} {'is' if missing == 1 else 'are'} not ready yet and will appear when their narration is made.")
        about = (f"Dojo's lessons for the {meta['exam']} {meta['title']} exam, one episode per skill in study-guide order. "
                 f"The voices are AI-generated. {readiness} Listening is not evidence: check each skill in Dojo afterwards. "
                 "Dojo is an independent tool, not endorsed by Microsoft or GitHub.")
        newest = _rfc2822(max((e["lesson"]["created"] for e in episodes), default=""))
        x = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
             'xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">',
             "<channel>",
             f"<title>{_text(title)}</title>",
             f"<link>{_text(home)}</link>",
             f'<atom:link href="{_attr(feed + "/" + pid + ".xml")}" rel="self" type="application/rss+xml"/>',
             f"<description>{_text(about)}</description>",
             "<language>en-us</language>"]
        if newest:
            x.append(f"<lastBuildDate>{newest}</lastBuildDate>")
        if (APP_DIR / "static" / "podcast" / f"{pid}.png").is_file():
            # An exam the owner added has no cover of its own; a podcast app shows its own placeholder.
            x += [f'<itunes:image href="{_attr(image)}"/>',
                  f"<image><url>{_text(image)}</url><title>{_text(title)}</title><link>{_text(home)}</link></image>"]
        x += ["<itunes:author>Dojo</itunes:author>",
              '<itunes:category text="Education"><itunes:category text="Courses"/></itunes:category>',
              "<itunes:explicit>false</itunes:explicit>",
              "<itunes:type>serial</itunes:type>",
              "<itunes:block>Yes</itunes:block>"]
        for e in episodes:
            info, sid = e["lesson"], e["skill"]
            skill = pkg["skills"][sid]
            page = go_link(f"skill/{pid}/{sid}", base)   # /?go=, because Easy Auth loses a "#" route at sign-in
            notes = self._notes(info, skill, sid, meta, page)
            name = info["id"] + ("-deep" if info.get("deep") else "")   # a new guid and file for Deeper Listen
            x += ["<item>",
                  f"<title>{_text(skill['text'])} ({_text(sid)})</title>",
                  f"<itunes:episode>{e['n']}</itunes:episode>",
                  "<itunes:episodeType>full</itunes:episodeType>",
                  f'<guid isPermaLink="false">{_text(name)}</guid>']
            when = _rfc2822(info["created"])
            if when:
                x.append(f"<pubDate>{when}</pubDate>")
            x += [f"<link>{_text(page)}</link>",
                  f'<enclosure url="{_attr(feed + "/ep/" + name + ".mp3")}" length="{e["bytes"]}" type="audio/mpeg"/>',
                  f"<itunes:duration>{max(1, round(e['seconds']))}</itunes:duration>",
                  f"<description>{_cdata(notes)}</description>",
                  f"<content:encoded>{_cdata(notes)}</content:encoded>",
                  "</item>"]
        x += ["</channel>", "</rss>", ""]
        return "\n".join(x).encode("utf-8")

    @staticmethod
    def _notes(info: dict, skill: dict, sid: str, meta: dict, check_url: str) -> str:
        """Episode notes: what is in it, who made it, where it comes from, and what listening does not prove."""
        out = [f"<p>{escape(skill['text'])} ({escape(sid)}), from the {escape(meta['exam'])} study guide: {escape(skill['domain_title'])}.</p>"]
        titles = [t for t in info["titles"] if t.strip()]
        if titles:
            out.append("<p>In this episode:</p><ol>" + "".join(f"<li>{escape(t)}</li>" for t in titles) + "</ol>")
        if info.get("deep"):
            out.append("<p>AI-generated voices. Dojo wrote this conversation from its checked lesson and the official sources "
                       "below, and had a second AI model check every line against them. The self-check is in Dojo.</p>")
        else:
            out.append("<p>AI-generated voices. Dojo wrote this lesson from the official sources below and had a second AI model "
                       "check it against them.</p>")
        sources = []
        for s in info["sources"]:
            url = _web(s.get("url"))
            name = escape(str(s.get("title") or s.get("url") or "Source"))
            linked = f'<a href="{escape(url)}">{name}</a>' if url else name
            publisher = escape(str(s.get("publisher") or "publisher not stated"))
            label, licence_url = LICENCES.get(s.get("license"), (s.get("license") or "licence not stated", None))
            licence = f'<a href="{escape(licence_url)}">{escape(label)}</a>' if licence_url else escape(label)
            sources.append(f"<li>{linked}, {publisher}, licensed {licence}</li>")
        if sources:
            out.append("<p>Adapted, with changes, from:</p><ul>" + "".join(sources) + "</ul>")
        out.append("<p>Listening is not evidence. It does not show that you can do this on your own, later, or in a new situation. "
                   f'<a href="{escape(check_url)}">Check this skill in Dojo</a>.</p>')
        out.append("<p>Dojo is an independent tool, not endorsed by Microsoft or GitHub.</p>")
        return "".join(out)

    # ---- the signed-in side: nothing here is reachable from /feed

    def question(self, learner: Learner, item_id: str) -> dict | None:
        """What to ask before this item's answer is sent, or None. Dojo cannot see a podcast app, so it asks
        about every skill whose episode the feed has ever listed or served (AIRED). A downloaded episode stays on
        the phone when a newer lesson or new narration takes its place and when the feed is turned off, so this
        holds whether or not the feed is on now and whatever the lesson's narration is now. It asks before an
        answer that could count as "later": ASK_EARLY_MIN before "later" is reached, or, with no teaching at
        all, on an item with a changed condition. A skill the feed never listed is on no phone: not asked."""
        it = self.dojo.item(learner, item_id)
        if it.get("answer") or it.get("mode") not in ("probe", "check"):
            return None
        pid, sid = it.get("package"), it.get("skill")
        aired = self._aired(learner, pid, sid)
        if not aired:
            return None
        pkg, profile, ev = self.dojo.evidence(learner, pid)
        if sid not in ev:
            return None
        since = ev[sid]["last_teaching"]
        if since:
            if utcnow() - parse_iso(since) < timedelta(hours=profile["later_hours"]) - timedelta(minutes=ASK_EARLY_MIN):
                return None
        elif not it.get("changed_condition"):
            return None
        lessons = self.dojo.lessons_for(pid, sid)
        on = self.links.active(learner)
        episode = None
        if on and lessons:
            info = None if self._withheld(lessons[0]["id"]) else self._lesson(lessons[0]["id"])
            order = self._order(pkg)
            row = {"n": order.index(sid) + 1 if sid in order else 0, "lesson": info,
                   "key": episode_key(info["media"]) if info and info["media"] else None}
            episode = row["n"] if row["n"] and self._in_feed(row) else None
        # The lesson asked about is the newest one the feed has listed or served, in the feed now or earlier.
        lesson = next((x["id"] for x in lessons if x["id"] in aired), None) or max(aired, key=lambda k: str(aired[k]))
        return {"since": since, "lesson": lesson, "episode": episode, "feed_on": on}

    def declare(self, learner: Learner, item_id: str, heard: bool | None) -> None:
        """Records what the learner said, before the answer itself is recorded. When the question applies,
        an answer without a reply is refused, so a listen Dojo did not ask about cannot count as "later"."""
        with self.store.lock:
            it = self.dojo.item(learner, item_id)
            if it.get("answer"):
                return  # the answer itself is refused next: each item is answered once
            asked = self.question(learner, item_id)
            if heard is None:
                if asked:
                    raise HTTPException(409, ANSWER_IT_FIRST)
                return
            pid, sid = it["package"], it["skill"]
            if asked is None:
                lessons = self.dojo.lessons_for(pid, sid)
                asked = {"since": self.dojo.evidence(learner, pid)[2][sid]["last_teaching"],
                         "lesson": lessons[0]["id"] if lessons else None, "asked": False}
            self.store.append_event(learner.key, "podcast.declared", {
                "package": pid, "skill": sid, "item": item_id, "lesson": asked["lesson"], "heard": bool(heard),
                "since": asked["since"], "asked": asked.get("asked", True)})
