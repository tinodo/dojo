"""The delivered-meaning check (EG-19): a passed script does not guarantee that the audio says the same
thing, so Dojo writes down what its own voices actually said and compares it with the approved words.

Listen: every slide's MP3 of a lesson is transcribed and compared with that slide's approved narration.
Deeper Listen (ADR 0007): every part's MP3 of a lesson's Deeper Listen, the same way, against that part's
approved lines. Watch, a smaller sample: each presenter's video of a lesson is transcribed as a whole, the heard words
are split into that presenter's turns by the stored start and end of every turn, and each turn is
compared with its approved words. Only a video with its turn timing and within the checker's size
limit is picked; one too large to send is counted in Studio and diag instead of failing every day.
Media files are read outside the store's lock, as playback reads them. The comparison is in `meaning`.

It is built like the Preparer: one lesson per step, never while the learner has a job queued or
running (it also stops between two slides when the learner starts something), and it holds one of
the same three job slots the learner's work uses. A failure is written to the content quality log and
the lesson is left alone for RETRY_HOURS. It spends at most LISTEN_PER_DAY Listen lessons,
DEEP_PER_DAY Deeper Listen lessons and WATCH_PER_DAY Watch lessons a day; the count is written to the
share before the work is paid for, so a restart does not hand it a fresh allowance.

Everything it writes is content-quality data about shared study material, under content/ on the data
share: the latest result per lesson (a new check replaces it), the day's count, the transcription
seconds it has used and its failures. It never writes into a learner's record. The heard words are
kept only in the latest result of each lesson, for the owner's Studio; the application log gets
lesson ids and counts, never what was heard."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from .avatar import PRESENTERS, lesson_turns, media_id_for
from .core import Learner, UserError, iso, parse_iso, utcnow
from .meaning import by_turn, compare, median
from .speech import MAX_CHECK_BYTES

log = logging.getLogger("dojo.delivery")

LISTEN_PER_DAY = 10   # lessons whose Listen audio is written down and compared, per day
WATCH_PER_DAY = 1     # lessons whose presenter videos are compared, per day: a sample
DEEP_PER_DAY = 10     # lessons whose Deeper Listen audio is written down and compared, per day
DAILY = {"listen": LISTEN_PER_DAY, "watch": WATCH_PER_DAY, "deep": DEEP_PER_DAY}
KIND_NAME = {"listen": "Listen", "watch": "Watch", "deep": "Deeper Listen"}
RETRY_HOURS = 24
WORK_WAIT_S = 20
BUSY_WAIT_S = 60
IDLE_WAIT_S = 1800
SLOT_WAIT_S = 5
WORST_SHOWN = 8
NOTE = ("Transcription itself can be wrong. A difference here means the words Speech wrote down are not the approved "
        "words, not that the voice said them wrong: listen to that slide or part before changing anything. Differences "
        "that touch a number, a name or a negation are listed first, because they can change what you are told.")


class DeliveryCheck:
    """The voice check: transcribes Dojo's own narration and videos and compares what was said with the
    approved text, word by word."""
    def __init__(self, dojo: Any, owner: Learner):
        self.dojo = dojo
        self.owner = owner
        self.state: dict = {"running": False, "current": None, "last": None, "note": "",
                            "checked": {"listen": 0, "watch": 0, "deep": 0}}
        self._started = False
        self._plans: dict[str, dict] = {}    # lesson id -> what it says and where; lessons never change once written
        self._results: dict[str, dict] | None = None
        self._one = threading.Lock()         # one check at a time
        self.scheduled = False               # team mode: run by the BackgroundScheduler in a spare slot (ADR 0009)

    # ------------------------------------------------------------ loop

    def start(self, delay: int = 600) -> None:
        if self._started:
            return
        self._started = True
        self.state["note"] = f"the first check starts {max(1, delay // 60)} minutes after the app"
        threading.Thread(target=self._loop, args=(delay,), name="delivery-check", daemon=True).start()

    def _loop(self, delay: int) -> None:
        time.sleep(delay)
        while True:
            try:
                wait = self.step()
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("delivery check step crashed")
                wait = IDLE_WAIT_S
            time.sleep(wait)

    def _learner_busy(self) -> bool:
        if self.scheduled:   # team mode: the BackgroundScheduler gave this step a spare slot (ADR 0009)
            return False
        return any(j.get("state") in ("queued", "running") for j in self.dojo.jobs.for_learner(self.owner))

    # ------------------------------------------------------------ what it writes down (content only)

    def _doc(self) -> dict:
        doc = self.dojo.store.read("content", "delivery-state", default=None) or {}
        day = iso()[:10]
        if doc.get("day") != day:
            doc["day"], doc["today"] = day, {kind: 0 for kind in DAILY}
        doc.setdefault("seconds", {kind: 0.0 for kind in DAILY})
        doc.setdefault("failed", {})
        for kind in DAILY:   # a state written before a kind was added
            doc["today"].setdefault(kind, 0)
            doc["seconds"].setdefault(kind, 0.0)
        return doc

    def _save(self, doc: dict) -> None:
        doc["updated"] = iso()
        self.dojo.store.write("content", "delivery-state", value=doc)

    def _charge(self, kind: str) -> bool:
        """Count the lesson before paying for it, so a crash or a restart cannot check it twice for free."""
        with self.dojo.store.lock:
            doc = self._doc()
            if doc["today"].get(kind, 0) >= DAILY[kind]:
                return False
            doc["today"][kind] = doc["today"].get(kind, 0) + 1
            self._save(doc)
            return True

    def _spent(self, kind: str, seconds: float) -> None:
        with self.dojo.store.lock:
            doc = self._doc()
            doc["seconds"][kind] = round(float(doc["seconds"].get(kind) or 0) + float(seconds or 0), 3)
            self._save(doc)

    def _mark(self, key: str, failed: bool) -> None:
        with self.dojo.store.lock:
            doc = self._doc()
            if failed:
                doc["failed"][key] = iso()
            elif doc["failed"].pop(key, None) is None:
                return
            self._save(doc)

    @staticmethod
    def _waiting(key: str, doc: dict, now: Any) -> bool:
        """A failure is left alone for a day, so one bad lesson does not block the queue."""
        failed_at = doc["failed"].get(key)
        return bool(failed_at and now - parse_iso(failed_at) < timedelta(hours=RETRY_HOURS))

    def results(self) -> dict[str, dict]:
        if self._results is None:
            found = {}
            for name in self.dojo.store.names("content", "delivery"):
                doc = self.dojo.store.read("content", "delivery", name)
                if isinstance(doc, dict):
                    found[name] = doc
            self._results = found
        return self._results

    def _keep(self, meta: dict, kind: str, value: dict) -> None:
        with self.dojo.store.lock:
            doc = self.dojo.store.read("content", "delivery", meta["id"], default=None) or {"lesson": meta["id"]}
            doc.update(package=meta.get("package"), skill=meta.get("skill"), title=meta.get("title"))
            doc[kind] = value
            self.dojo.store.write("content", "delivery", meta["id"], value=doc)
        self.results()[meta["id"]] = doc

    # ------------------------------------------------------------ what to check next

    def order(self) -> list[dict]:
        """Every lesson, the owner's active exam first, then the exams most members study (ADR 0009), then
        by exam weight, the newest lesson first."""
        pids = self.dojo.exam_order(self.owner)
        rank = {pid: k for k, pid in enumerate(pids)}
        share = {(pid, s["id"]): s["share"] for pid in pids for s in self.dojo.packages.get(pid)["skills"].values()}
        metas = [m for m in self.dojo.lesson_index() if m.get("package") in rank]
        metas.sort(key=lambda m: m.get("created") or "", reverse=True)
        metas.sort(key=lambda m: (rank[m["package"]], -share.get((m["package"], m.get("skill")), 0), m.get("skill") or ""))
        return metas

    def plan(self, lesson_id: str) -> dict:
        """What a lesson's voices are meant to say and where its audio and videos are (also read by the
        cost view): per slide the Listen media id, the approved words and their characters; per
        presenter the turns and the video's media id; per slide which turns it holds."""
        plan = self._plans.get(lesson_id)
        if plan is None:
            slides = self.dojo.lesson(lesson_id).get("slides", [])
            lines = [self.dojo._slide_lines(sl) for sl in slides]
            per_speaker, layout = lesson_turns(lines)
            texts = [[str(x.get("text") or "").strip() for x in ls] for ls in lines]
            plan = {"audio": [self.dojo._slide_media(sl) for sl in slides],
                    "approved": [" ".join(t for t in ts if t) for ts in texts],
                    "chars": [sum(len(t) for t in ts) for ts in texts],
                    "turns": per_speaker, "layout": layout,
                    "films": {sp: media_id_for(sp, turns) for sp, turns in per_speaker.items()}}
            self._plans[lesson_id] = plan
        return plan

    def next_listen(self, doc: dict, now: Any) -> tuple[dict, dict] | None:
        """The first lesson whose narration is all there and has not been checked in this version."""
        for meta in self.order():
            if self._waiting(f"listen:{meta['id']}", doc, now):
                continue
            plan = self.plan(meta["id"])
            done = (self.results().get(meta["id"]) or {}).get("listen") or {}
            if not plan["audio"] or done.get("audio") == plan["audio"]:
                continue
            if all(self.dojo.speech.exists(a) for a in plan["audio"]):
                return meta, plan
        return None

    def deep_plan(self, lesson_id: str) -> dict:
        """What a lesson's Deeper Listen is meant to say (ADR 0007): per part its media id, its title and
        its approved words. Not kept: a Deeper Listen can be written again."""
        d = self.dojo.deep(lesson_id) or {}
        segments = d.get("deep_narration") or []
        texts = [[str(x.get("text") or "").strip() for x in seg["lines"]] for seg in segments]
        return {"audio": self.dojo._deep_media(d), "titles": [seg.get("title") or "" for seg in segments],
                "approved": [" ".join(t for t in ts if t) for ts in texts]}

    def next_deep(self, doc: dict, now: Any) -> tuple[dict, dict] | None:
        """The first lesson whose Deeper Listen plays and has not been checked in this version."""
        for meta in self.order():
            if self._waiting(f"deep:{meta['id']}", doc, now) or not self.dojo.deep_ready(meta["id"]):
                continue
            plan = self.deep_plan(meta["id"])
            done = (self.results().get(meta["id"]) or {}).get("deep") or {}
            if plan["audio"] and done.get("audio") != plan["audio"]:
                return meta, plan
        return None

    def next_watch(self, doc: dict, now: Any) -> tuple[dict, dict, dict] | None:
        """The first lesson with a presenter filmed whose videos have not been checked in this version.
        Only a video that can be checked counts: one with its turn timing and small enough to send, so
        a video that would fail every time never spends the day's one Watch check."""
        sizes = self._video_sizes()
        if not sizes:
            return None
        for meta in self.order():
            if self._waiting(f"watch:{meta['id']}", doc, now):
                continue
            plan = self.plan(meta["id"])
            done = (self.results().get(meta["id"]) or {}).get("watch") or {}
            filmed = {sp: mid for sp, mid in plan["films"].items() if mid in sizes}
            if not filmed or done.get("media") == filmed:
                continue
            present = {sp: mid for sp, mid in filmed.items()
                       if sizes[mid] <= MAX_CHECK_BYTES and self.dojo.avatar.timings(mid)}
            if present and done.get("media") != present:
                return meta, plan, present
        return None

    def _video_sizes(self) -> dict[str, int]:
        return {name[:-len(".webm")]: size for name, size, _ in self.dojo.avatar.videos()}

    def _too_large(self) -> int:
        return sum(1 for size in self._video_sizes().values() if size > MAX_CHECK_BYTES)

    def _media(self, name: str) -> bytes | None:
        """One of Dojo's own media files, read outside the store's lock as playback reads it: a video
        can be tens of megabytes, and media files are written whole and never changed."""
        path = self.dojo.store.path("media", name)
        try:
            if path.stat().st_size > MAX_CHECK_BYTES:
                raise UserError(f"{name} is larger than {MAX_CHECK_BYTES // (1024 * 1024)} MB")
            return path.read_bytes()
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------ one step

    def step(self) -> int:
        """Check one lesson. Returns how many seconds to wait before the next step."""
        with self._one:
            return self._step()

    def _step(self) -> int:
        if self._learner_busy():
            self.state.update(note="your own work comes first")
            return BUSY_WAIT_S
        doc, now = self._doc(), utcnow()
        kind, target = None, None
        pick = {"watch": self.next_watch, "listen": self.next_listen, "deep": self.next_deep}
        for kind in ("watch", "listen", "deep"):
            if doc["today"].get(kind, 0) < DAILY[kind]:
                target = pick[kind](doc, now)
                if target:
                    break
        if not target:
            spent = [k for k in DAILY if doc["today"].get(k, 0) >= DAILY[k]]
            note = ("nothing new to check" if not spent else "today's checks are done" if len(spent) == len(DAILY)
                    else "; ".join(f"{KIND_NAME[k]}: {'done for today' if k in spent else 'nothing new'}" for k in DAILY))
            self.state.update(running=False, current=None, note=note)
            return IDLE_WAIT_S
        slots = None if self.scheduled else self.dojo.jobs.slots   # scheduled: the scheduler holds the slot
        if slots is not None and not slots.acquire(timeout=SLOT_WAIT_S):
            self.state.update(note="the work slots are busy")
            return BUSY_WAIT_S
        meta = target[0]
        key = f"{kind}:{meta['id']}"
        try:
            if not self._charge(kind):
                self.state.update(note="today's checks are done")
                return IDLE_WAIT_S
            self.state.update(running=True, current=key, note="")
            check = {"listen": self._listen, "watch": self._watch, "deep": self._deep}[kind]
            finished = check(*target)
            if finished:
                self.state["checked"][kind] += 1
                self._mark(key, failed=False)
            else:
                self.state["note"] = "stopped for your own work"
        except UserError as e:
            self._fail(kind, meta, str(e))
        except Exception as e:  # noqa: BLE001 - a broken check waits a day like any other failure
            log.warning("delivery check %s failed: %s", key, type(e).__name__)
            self._fail(kind, meta, f"the check itself failed ({type(e).__name__})")
        finally:
            if slots is not None:
                slots.release()
            self.state.update(running=False, current=None, last=iso())
        return WORK_WAIT_S

    def _fail(self, kind: str, meta: dict, reason: str) -> None:
        self._mark(f"{kind}:{meta['id']}", failed=True)
        what = {"listen": "Listen audio", "watch": "Watch video", "deep": "Deeper Listen audio"}[kind]
        self.dojo.quality(None, "delivery", {
            "package": meta.get("package"), "skill": meta.get("skill"), "lesson": meta["id"],
            "part": KIND_NAME[kind], "text": "",
            "reason": f"Checking what the {what} of this lesson says did not work: {reason.rstrip('.')}. "
                      f"It is tried again after {RETRY_HOURS} hours."})

    def _listen(self, meta: dict, plan: dict) -> bool:
        slides, seconds = [], 0.0
        for k, (media_id, approved) in enumerate(zip(plan["audio"], plan["approved"])):
            if k and self._learner_busy():
                return False
            audio = self._media(media_id + ".mp3")
            if not audio:
                raise UserError(f"the audio of slide {k + 1} is gone")
            heard = self.dojo.speech.transcribe_words(audio, "audio/mpeg")
            self._spent("listen", heard["seconds"])
            seconds += heard["seconds"]
            slides.append({"slide": k + 1, "seconds": heard["seconds"], **compare(approved, heard["text"])})
        value = {"checked": iso(), "audio": plan["audio"], "seconds": round(seconds, 3), **_totals(slides), "slides": slides}
        self._keep(meta, "listen", value)
        log.info("delivery check: listen %s, %d slides, %d of %d words differ", meta["id"], len(slides),
                 value["errors"], value["words"])
        return True

    def _deep(self, meta: dict, plan: dict) -> bool:
        parts, seconds = [], 0.0
        for k, (media_id, title, approved) in enumerate(zip(plan["audio"], plan["titles"], plan["approved"])):
            if k and self._learner_busy():
                return False
            audio = self._media(media_id + ".mp3")
            if not audio:
                raise UserError(f"the audio of Deeper Listen part {k + 1} is gone")
            heard = self.dojo.speech.transcribe_words(audio, "audio/mpeg")
            self._spent("deep", heard["seconds"])
            seconds += heard["seconds"]
            parts.append({"part": k + 1, "title": title, "seconds": heard["seconds"], **compare(approved, heard["text"])})
        value = {"checked": iso(), "audio": plan["audio"], "seconds": round(seconds, 3), **_totals(parts), "parts": parts}
        self._keep(meta, "deep", value)
        log.info("delivery check: deep %s, %d parts, %d of %d words differ", meta["id"], len(parts),
                 value["errors"], value["words"])
        return True

    def _watch(self, meta: dict, plan: dict, present: dict) -> bool:
        per_turn: dict[tuple[str, int], dict] = {}
        presenters, seconds = {}, 0.0
        for n, speaker in enumerate(sp for sp in PRESENTERS if sp in present):
            if n and self._learner_busy():
                return False
            media_id = present[speaker]
            spans = self.dojo.avatar.timings(media_id)
            data = self._media(media_id + ".webm")
            if not spans or not data:
                raise UserError(f"the {speaker}'s video is gone")
            heard = self.dojo.speech.transcribe_words(data, "audio/webm")
            self._spent("watch", heard["seconds"])
            seconds += heard["seconds"]
            said, outside = by_turn(heard["words"], spans)
            turns = plan["turns"][speaker]
            for pos, texts in enumerate(turns):
                per_turn[(speaker, pos)] = compare(" ".join(texts), said[pos] if pos < len(said) else "")
            presenters[speaker] = {"media": media_id, "seconds": heard["seconds"], "turns": len(turns),
                                   "timed_turns": len(spans), "words_outside_turns": outside}
        turns = [{"slide": k + 1, "speaker": speaker, "turn": pos + 1, **per_turn[(speaker, pos)]}
                 for k, row in enumerate(plan["layout"]) for speaker, pos in row if (speaker, pos) in per_turn]
        value = {"checked": iso(), "media": present, "presenters": presenters, "seconds": round(seconds, 3),
                 **_totals(turns), "turns": turns}
        self._keep(meta, "watch", value)
        log.info("delivery check: watch %s, %d turns, %d of %d words differ", meta["id"], len(turns),
                 value["errors"], value["words"])
        return True

    # ------------------------------------------------------------ what Studio and diag show

    def summary(self) -> dict:
        results = list(self.results().values())
        doc = self._doc()
        parts = {}
        worst = []
        for kind, rows in (("listen", "slides"), ("watch", "turns"), ("deep", "parts")):
            checked = [r for r in results if isinstance(r.get(kind), dict)]
            units = [(r, u) for r in checked for u in r[kind].get(rows, [])]
            words = sum(u["words"] for _, u in units)
            errors = sum(u["errors"] for _, u in units)
            parts[kind] = {"lessons": len(checked), rows: len(units),
                           "median_wer": median([u["wer"] for _, u in units]),
                           "wer": round(errors / words, 4) if words else None,
                           "flagged": sum(1 for _, u in units if u["flags"]),
                           "minutes": round(float(doc["seconds"].get(kind) or 0) / 60, 1)}
            for r, u in units:
                if u["errors"]:
                    worst.append({"kind": kind, "lesson": r["lesson"], "title": r.get("title"), "package": r.get("package"),
                                  "skill": r.get("skill"), "checked": r[kind].get("checked"), "slide": u.get("slide"),
                                  "part": u.get("part"), "part_title": u.get("title"),
                                  "speaker": u.get("speaker"), "wer": u["wer"], "words": u["words"], "errors": u["errors"],
                                  "flags": u["flags"], "diffs": u["diffs"], "approved": u["approved"], "heard": u["heard"]})
        worst.sort(key=lambda w: (not w["flags"], -w["wer"], -w["errors"]))
        parts["watch"]["too_large"] = self._too_large()
        return {"lessons": len(self.dojo.lesson_index()), **parts, "worst": worst[:WORST_SHOWN],
                "daily": DAILY, "today": doc["today"], "retry_hours": RETRY_HOURS, "note": NOTE,
                "status": {"started": self._started, **{k: self.state[k] for k in ("running", "current", "last", "note")}}}

    def seconds(self) -> dict:
        """Transcription used by the check so far, for the cost view."""
        return {kind: float(v or 0) for kind, v in self._doc()["seconds"].items()}

    def status(self) -> dict:
        doc, now = self._doc(), utcnow()
        results = self.results().values()
        return {"started": self._started, "running": self.state["running"], "current": self.state["current"],
                "last": self.state["last"], "note": self.state["note"], "daily_limit": DAILY, "checked_today": doc["today"],
                "checked_since_start": self.state["checked"],
                "lessons_checked": {kind: sum(1 for r in results if isinstance(r.get(kind), dict)) for kind in DAILY},
                "waiting_after_failure": sum(1 for key in doc["failed"] if self._waiting(key, doc, now)),
                "videos_too_large": self._too_large(), "retry_hours": RETRY_HOURS,
                "transcribed_minutes": {k: round(float(v or 0) / 60, 1) for k, v in doc["seconds"].items()}}


def _totals(units: list[dict]) -> dict:
    words = sum(u["words"] for u in units)
    errors = sum(u["errors"] for u in units)
    return {"words": words, "errors": errors, "wer": round(errors / words, 4) if words else 0.0,
            "median_wer": median([u["wer"] for u in units]), "flagged": sum(1 for u in units if u["flags"])}
