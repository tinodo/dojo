"""Studio for the owner (docs/adr/0019-studio-overview.md): how far each course is, and what Dojo did on its own.

Everything here is read from what Dojo already keeps. It writes no record of its own (the public price list that the
estimates read is kept, as it is for the cost page), calls no model and spends nothing. The words are chosen here, in
one place, so that a number never reaches the screen without saying what it counts."""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from datetime import timedelta
from typing import Any

from .core import iso, parse_iso, utcnow
from .newexam import BUILD, exam_blocker
from .pagecheck import KIND as PAGECHECK
from .prepare import DEEP_PER_DAY

log = logging.getLogger("dojo.studio")

CACHE_S = 60          # a reading is fresh this long; after that it is read again in the background
FEED_DAYS = 14        # the activity looks back this far; the job records and the meter keep about as much
FEED_EVENTS = 300     # and shows at most this many events, newest first
LEVELS = {"bad": 0, "warn": 1, "info": 2}
JOB_LABEL = {BUILD: "Building a course", "video": "Filming a lesson", "deck": "Drawing a lesson's diagrams",
             PAGECHECK: "Checking a course's pages"}
FILM_NOTE = "Filming costs money, so Dojo never films on its own: you approve each price."


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


class Studio:
    """The owner's overview of every course, and the feed of what Dojo did."""

    def __init__(self, dojo: Any, newexams: Any, checker: Any, costs: Any, preparer: Any, drift: Any, pool: Any,
                 budget: Any, owner: Any, pagecheck: Any = None):
        self.dojo, self.newexams, self.checker, self.costs = dojo, newexams, checker, costs
        self.preparer, self.drift, self.pool, self.budget, self.owner, self.pagecheck = preparer, drift, pool, budget, owner, pagecheck
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[float, Any]] = {}
        self._busy: set[str] = set()   # views being read again in the background
        self._kinds: dict[str, tuple[float, str]] = {}   # what Watch plays for a lesson with no film of its own, kept a few minutes

    def _cached(self, key: str, make: Any, fresh: bool = False) -> Any:
        """The last reading of a view. One that is older than CACHE_S is still what the page gets: it is read again in the
        background, so a visit never waits for the reading except the very first one. `fresh` reads it now."""
        with self._lock:
            hit = None if fresh else self._seen.get(key)
            if hit:
                if time.monotonic() - hit[0] >= CACHE_S and key not in self._busy:
                    self._busy.add(key)
                    threading.Thread(target=self._again, args=(key, make), name=f"studio-{key}", daemon=True).start()
                return hit[1]
        value = make()
        with self._lock:
            self._seen[key] = (time.monotonic(), value)
        return value

    def _again(self, key: str, make: Any) -> None:
        try:
            value = make()
            with self._lock:
                self._seen[key] = (time.monotonic(), value)
        except Exception:  # noqa: BLE001 - the last reading stays what the page gets
            log.exception("studio: could not read %s again", key)
        finally:
            with self._lock:
                self._busy.discard(key)

    def warm(self, delay: float = 90.0) -> None:
        """Read both views once, a little after startup, so the first visit does not wait for the first reading."""
        def run() -> None:
            time.sleep(delay)
            for view in (self.courses, self.activity):
                try:
                    view()
                except Exception:  # noqa: BLE001 - warming is only a courtesy
                    log.info("studio: the first reading failed")
        threading.Thread(target=run, name="studio-warm", daemon=True).start()

    # ------------------------------------------------------------ the courses

    def courses(self, fresh: bool = False) -> dict:
        return self._cached("courses", self._courses, fresh)

    def _courses(self) -> dict:
        try:
            ready = (self.pool.status().get("pool") or {})
        except Exception:  # noqa: BLE001 - the question pool is one row among many
            ready = {}
        try:
            media = set(os.listdir(self.dojo.store.path("media")))
        except OSError:
            media = set()
        newest: dict[tuple[Any, Any], str] = {}   # the newest lesson of each skill: read once, not for every skill
        for m in sorted(self.dojo.lesson_index(), key=lambda m: m.get("created") or ""):
            newest[(m.get("package"), m.get("skill"))] = m["id"]
        out = []
        for pid in self.dojo.exam_order(self.owner):
            try:
                out.append(self._course(pid, ready.get(pid), media, newest))
            except Exception:  # noqa: BLE001 - a course that cannot be read never blanks the page
                log.exception("studio: could not read the course %s", pid)
        return {"at": iso(), "courses": out}

    def _watch(self, lesson_id: str) -> str:
        """What Watch plays for a lesson that has no film of its own: "earlier" or "none" (what `film_kind` says, kept five minutes)."""
        hit = self._kinds.get(lesson_id)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        kind = self.dojo.film_kind(lesson_id)
        self._kinds[lesson_id] = (time.monotonic(), kind)
        return kind

    def _course(self, pid: str, questions: int | None, media: set[str], newest: dict) -> dict:
        dojo = self.dojo
        pkg = dojo.packages.get(pid)
        meta, skills, added = pkg["meta"], list(pkg["skills"].values()), dojo.packages.is_added(pid)
        total = len(skills)
        spread = Counter(len(s["sources"]) for s in skills)
        with_pages = total - spread.get(0, 0)
        lesson_of = {s["id"]: newest[(pid, s["id"])] for s in skills if s["sources"] and (pid, s["id"]) in newest}
        lessons = len(lesson_of)
        listen = deeper = diagrams = 0
        films: Counter = Counter()
        unfilmed: list[str] = []
        for lid in lesson_of.values():
            plan = self.checker.plan(lid)   # kept by the voice check: a lesson is read once
            audio = [a for a in plan["audio"] if a]
            listen += 1 if audio and all(a + ".mp3" in media for a in audio) else 0
            deeper += 1 if dojo.deep_ready(lid) else 0
            deck = dojo.deck(lid)
            diagrams += 1 if deck and deck.get("state") == "ready" else 0
            made = list(plan["films"].values())
            own = bool(made) and all(f + ".webm" in media and f + ".json" in media for f in made)
            kind = "own" if own else self._watch(lid)
            films[kind] += 1
            if kind != "own":
                unfilmed.append(lid)
        price = self._film_price(unfilmed)
        search = self._search(pid) if added else None
        check = self.pagecheck.view(pid) if self.pagecheck is not None and not added else None
        failed = self._failed(pid)
        rechecking = len(self.drift.rechecking(pid)) if self.drift is not None else 0
        blocker = exam_blocker(pkg)
        rows = [
            self._pages_row(total, with_pages, spread, search, check),
            self._row("lessons", "Lessons", lessons, with_pages, "The written lesson for each skill that has an official page."),
            self._row("listen", "Listen", listen, lessons, "Every slide read aloud, so a lesson can be heard instead of read."),
            self._row("deeper", "Deeper Listen", deeper, lessons,
                      f"Two voices talk the lesson through. Dojo writes up to {DEEP_PER_DAY} a day, so this fills up over time.", optional=True),
            self._row("present", "Present", lessons, lessons,
                      "The narrated slides. "
                      + (f"{_plural(diagrams, 'lesson')} also {'has' if diagrams == 1 else 'have'} diagrams drawn by a model. "
                         if diagrams else "No lesson has model-drawn diagrams yet. ")
                      + "They are drawn the first time a lesson is opened in Present, or ahead of time if you turn that on in Costs."),
            self._video_row(films["own"], films["earlier"], films["none"], lessons, price),
        ]
        practice = ("Written when you practise, so there is nothing to wait for."
                    + (f" {_plural(questions, 'question')} {'is' if questions == 1 else 'are'} kept ready in the background." if questions else ""))
        rows.append({"key": "practice", "label": "Practice questions", "have": None, "of": None, "state": "info", "note": practice})
        rows.append({"key": "exam", "label": "Timed practice exam", "have": None, "of": None,
                     "state": "ok" if blocker is None else "warn",
                     "note": "Available." if blocker is None else str(blocker)})
        ready = with_pages == total and lessons == with_pages and listen == lessons
        out = {"package": pid, "exam": meta["exam"], "title": meta["title"], "added": added, "version": meta.get("version"),
               "skills": total, "ready": ready, "rows": rows,
               "video": {"own": films["own"], "earlier": films["earlier"], "none": films["none"], "price": price},
               "search": search, "pagecheck": check}
        out["verdict"] = ("Everything needed to study this course is there." if ready
                          else f"{_plural(total - lessons, 'skill')} of {total} can't be studied from a lesson yet.")
        out["attention"] = self._attention(pid, out, with_pages, lessons, listen, deeper, failed, rechecking, price, search, check)
        return out

    @staticmethod
    def _row(key: str, label: str, have: int, of: int, note: str, optional: bool = False) -> dict:
        state = "ok" if have == of else ("todo" if optional else "warn")
        return {"key": key, "label": label, "have": have, "of": of, "state": state, "note": note}

    @staticmethod
    def _pages_row(total: int, with_pages: int, spread: Counter, search: dict | None, check: dict | None) -> dict:
        if search is None:
            shape = ", ".join(f"{spread[n]} cite{'s' if spread[n] == 1 else ''} {_plural(n, 'page')}" for n in (1, 2, 3) if spread.get(n))
            note = f"{shape}. The first page of each skill comes from the original list; Dojo's older search added the others. "
            if check and check["checked"]:
                note += (f"Two model families read them: {check['supported']} of {check['checked']} skills have a page both accepted, "
                         f"{check['weak']} only partly, {check['invalid']} refused."
                         + (f" {_plural(check['unchecked'], 'skill')} still to check." if check["unchecked"] else ""))
            else:
                note += "No page has been through the two-model page check that added exams get yet."
        else:
            note = (f"{search['covered']} skills were found by Dojo's own search: two model families both accepted each page and "
                    f"quoted it. {search['legacy']} keep pages from before that search: {search['supported']} confirmed by both models, "
                    f"{search['invalid']} refused by both, {search['unlooked']} not looked at yet.")
        return {"key": "pages", "label": "Official pages", "have": with_pages, "of": total,
                "state": "ok" if with_pages == total else "warn", "note": note}

    @staticmethod
    def _video_row(own: int, earlier: int, none: int, lessons: int, price: dict | None) -> dict:
        parts = [f"{own} have a film of the current lesson"]
        if earlier:
            parts.append(f"{earlier} play a film of an earlier version, with a note saying so")
        if none:
            parts.append(f"{none} have no film")
        note = "; ".join(parts) + ". " + FILM_NOTE
        if price and price.get("complete") and price.get("total") is not None:
            note += f" Filming the rest would cost about ${price['total']:,.2f} at list price."
        elif price:
            note += " The price list could not be read just now, so no price is shown."
        return {"key": "video", "label": "Watch", "have": own, "of": lessons, "state": "ok" if own == lessons else "todo", "note": note}

    def _film_price(self, unfilmed: list[str]) -> dict | None:
        if not unfilmed:
            return None
        try:
            est = self.costs.film_estimate([self.checker.plan(x) for x in unfilmed])
        except Exception:  # noqa: BLE001 - the price is a line in the overview, never a reason to lose it
            log.info("studio: the price of filming could not be worked out")
            return {"lessons": len(unfilmed), "videos": None, "total": None, "complete": False, "currency": "USD"}
        return {"lessons": est.get("lessons"), "videos": est.get("videos"), "total": est.get("total"),
                "complete": bool(est.get("complete")), "currency": est.get("currency") or "USD"}

    def _search(self, pid: str) -> dict | None:
        st = self.newexams.sourcer.status(pid)
        if not st:
            return None
        details = st.get("details") or []
        counts = st.get("counts") or {}
        verdicts = Counter((d.get("audit") or {}).get("verdict") for d in details if d.get("audit"))
        return {"covered": counts.get("covered", 0), "legacy": counts.get("legacy", 0),
                "unfinished": st.get("incomplete", 0), "complete": bool(st.get("complete")),
                "supported": verdicts.get("supported", 0), "invalid": verdicts.get("invalid", 0), "weak": verdicts.get("weak", 0),
                "unlooked": sum(1 for d in details if d["state"] == "legacy" and not d.get("audit"))}

    def _failed(self, pid: str) -> int:
        failed = getattr(self.preparer, "state", {}).get("failed") or {}
        return sum(1 for key in failed
                   if key.startswith(f"{pid}/") or key.startswith(f"audio:{pid}/") or key.startswith(f"deep:{pid}/"))

    def _attention(self, pid: str, course: dict, with_pages: int, lessons: int, listen: int, deeper: int, failed: int,
                   rechecking: int, price: dict | None, search: dict | None, check: dict | None = None) -> list[dict]:
        total, items = course["skills"], []

        def add(level: str, text: str, link: str | None = None, link_text: str | None = None) -> None:
            items.append({"level": level, "text": text, "link": link, "link_text": link_text})

        if with_pages < total:
            add("warn", f"{_plural(total - with_pages, 'skill')} {'has' if total - with_pages == 1 else 'have'} no official page yet, so no lesson. "
                        "Dojo is searching for them on its own.")
        if lessons < with_pages:
            add("warn", f"{_plural(with_pages - lessons, 'skill')} {'has a page' if with_pages - lessons == 1 else 'have pages'} but no lesson yet. "
                        "Dojo writes them one at a time.")
        if lessons and listen < lessons:
            add("info", f"{_plural(lessons - listen, 'lesson')} {'has' if lessons - listen == 1 else 'have'} no Listen audio yet. Dojo narrates them after writing.")
        if failed:
            add("warn", f"{_plural(failed, 'piece')} of work failed and {'is' if failed == 1 else 'are'} waiting to be tried again after 24 hours.",
                "#/quality/activity", "See Activity")
        if rechecking:
            add("info", f"{_plural(rechecking, 'lesson')} {'is' if rechecking == 1 else 'are'} being written again after an official page changed.")
        if search and search["invalid"]:
            add("info", f"{_plural(search['invalid'], 'skill')} cite{'s' if search['invalid'] == 1 else ''} a page that both models refuse. "
                        "Dojo looks again a day later and then finds a better page on its own, a few a day.")
        if search and not search["complete"]:
            add("info", f"Dojo is still finding official pages for {_plural(search['unfinished'], 'skill')}.")
        if lessons and deeper < lessons:
            add("info", f"{_plural(lessons - deeper, 'lesson')} {'waits' if lessons - deeper == 1 else 'wait'} for Deeper Listen.")
        if search is None and not (check and check["checked"]):
            add("info", "Every page of this course is from before Dojo's two-model page check. A lesson's sentences are still checked against its pages, "
                        "but nobody has asked whether a page covers its whole skill.")
        if check and check["invalid"]:
            add("warn", f"{_plural(check['invalid'], 'skill')} cite{'s' if check['invalid'] == 1 else ''} only pages that both models refuse. "
                        "Nothing was changed: the lessons stay as they are, and the list is under the pages.")
        if check and check["weak"]:
            add("info", f"{_plural(check['weak'], 'skill')} cite{'s' if check['weak'] == 1 else ''} pages that cover {'it' if check['weak'] == 1 else 'them'} only partly: "
                        "the two models did not agree, or neither was sure.")
        missing = course["video"]["earlier"] + course["video"]["none"]
        if missing:
            cost = (f" Filming them all would cost about ${price['total']:,.2f}." if price and price.get("complete") and price.get("total") is not None
                    else " The price list could not be read just now, so no price is shown." if price else "")
            where = f"#/add-exam/{pid}" if course["added"] else "#/quality/checks"
            add("info", f"{_plural(missing, 'lesson')} {'has' if missing == 1 else 'have'} no film of the current lesson.{cost} {FILM_NOTE}",
                where, "Approve filming")
        return sorted(items, key=lambda x: LEVELS[x["level"]])

    # ------------------------------------------------------------ what Dojo did

    def activity(self) -> dict:
        return self._cached("activity", self._activity)

    def _activity(self) -> dict:
        since = utcnow() - timedelta(days=FEED_DAYS)
        events: list[dict] = []
        for part in (self._lesson_events, self._deep_events, self._search_events, self._change_events, self._job_events,
                     self._problem_events):
            try:
                events += part(since)
            except Exception:  # noqa: BLE001 - one source that cannot be read never blanks the feed
                log.exception("studio: the activity could not read %s", part.__name__)
        events.sort(key=lambda e: e["at"], reverse=True)
        shown = events[:FEED_EVENTS]
        try:
            spend = self.budget.spend_days(FEED_DAYS) if self.budget is not None and self.budget.active else {}
        except Exception:  # noqa: BLE001 - the meter is only a figure beside the feed
            spend = {}
        # The events carry UTC times and the page groups them by the reader's own days; the meter counts UTC days.
        return {"at": iso(), "window_days": FEED_DAYS, "metered": bool(spend), "now": self._now(), "events": shown,
                "spend": [{"day": day, "usd": usd} for day, usd in sorted(spend.items(), reverse=True)],
                "cut": len(events) > len(shown)}

    def _exam(self, pid: str | None) -> str:
        try:
            return self.dojo.packages.get(pid)["meta"]["exam"]
        except Exception:  # noqa: BLE001 - an exam removed since
            return str(pid or "").upper()

    def _skill(self, pid: str | None, sid: str | None) -> str:
        try:
            return self.dojo.packages.skill(pid, sid)["text"]
        except Exception:  # noqa: BLE001
            return ""

    def _lesson_events(self, since: Any) -> list[dict]:
        out = []
        index = self.dojo.lesson_index()
        first: dict[tuple[Any, Any], str] = {}   # the oldest lesson of each skill: a later one was written again, after a changed page or a deeper template
        for m in sorted(index, key=lambda m: (m.get("created") or "", m["id"])):
            first.setdefault((m.get("package"), m.get("skill")), m["id"])
        for m in index:
            at = m.get("created")
            if not at or parse_iso(at) < since:
                continue
            again = bool(m.get("upgrade_of")) or first.get((m.get("package"), m.get("skill"))) != m["id"]
            out.append({"at": at, "kind": "lesson", "tone": "info", "course": self._exam(m.get("package")),
                        "title": f"Wrote the lesson “{m.get('title')}”" + (" again" if again else ""),
                        "detail": self._skill(m.get("package"), m.get("skill")), "link": f"#/lesson/{m['id']}"})
        return out

    def _deep_events(self, since: Any) -> list[dict]:
        out = []
        store = self.dojo.store
        for name in store.names("content", "deep"):
            try:
                if os.stat(store.path("content", "deep", name + ".json")).st_mtime < since.timestamp():
                    continue
            except OSError:
                continue
            d = self.dojo.deep(name)
            if not d:
                continue
            at = d.get("created")
            if not at or parse_iso(at) < since:
                continue
            try:
                title = self.dojo.lesson(name).get("title")
            except Exception:  # noqa: BLE001 - the lesson went with its exam
                continue
            ok = d.get("state") == "ready"
            out.append({"at": at, "kind": "deep", "tone": "info" if ok else "warn", "course": self._exam(d.get("package")),
                        "title": f"Wrote the Deeper Listen for “{title}”" if ok else f"The Deeper Listen for “{title}” did not pass its checks",
                        "detail": "", "link": f"#/lesson/{name}/listen"})
        return out

    def _search_events(self, since: Any) -> list[dict]:
        out = []
        for pid in self.dojo.packages.ids():
            if not self.dojo.packages.is_added(pid):
                continue
            for a in self.newexams.sourcer.activations(pid, since):
                partly = a["state"] != "covered"
                out.append({"at": a["at"], "kind": "pages", "tone": "warn" if partly else "info", "course": self._exam(pid),
                            "title": f"Found official pages for “{a['skill']}”",
                            "detail": f"{_plural(a['pages'], 'page')}" + (", covering it only partly" if partly else ""), "link": None})
        return out

    def _change_events(self, since: Any) -> list[dict]:
        if self.drift is None:
            return []
        said = {"gone": "is gone", "moved": "has moved"}
        return [{"at": c["at"], "kind": "page", "tone": "warn" if c["kind"] in said else "info", "course": "",
                 "title": f"An official page {said.get(c['kind'], 'changed')}: {c['title']}",
                 "detail": f"{_plural(c['lessons'], 'lesson')} {'was' if c['lessons'] == 1 else 'were'} written from it." if c["lessons"] else "",
                 "link": None} for c in self.drift.recent_changes(since)]

    def _job_events(self, since: Any) -> list[dict]:
        out = []
        store = self.dojo.store
        for name in store.names("jobs"):
            j = store.read("jobs", name)
            if not isinstance(j, dict) or j.get("learner") != self.owner.key or not j.get("created") or parse_iso(j["created"]) < since:
                continue
            kind = j.get("kind")
            failed = j.get("state") in ("failed", "interrupted", "abandoned")
            if kind not in JOB_LABEL and not failed:
                continue
            label = JOB_LABEL.get(kind) or f"A “{kind}” job"
            out.append({"at": j["created"], "kind": "job", "tone": "bad" if failed else "info", "course": "",
                        "title": f"{label}: {j.get('state')}", "detail": str(j.get("error") or j.get("step") or "")[:200], "link": None})
        return out

    def _problem_events(self, since: Any) -> list[dict]:
        out = []
        for e in self.dojo.quality_log(self.owner):
            if e.get("kind") not in ("preparation", "failure") or not e.get("at") or parse_iso(e["at"]) < since:
                continue
            out.append({"at": e["at"], "kind": "problem", "tone": "bad", "course": self._exam(e.get("package")) if e.get("package") else "",
                        "title": str(e.get("reason") or "Something did not work")[:200],
                        "detail": self._skill(e.get("package"), e.get("skill")) if e.get("package") else str(e.get("part") or ""), "link": None})
        return out

    def _now(self) -> list[dict]:
        """What is running at this moment, as one line each."""
        def skill_of(current: str | None) -> str:
            if not current or "/" not in current:
                return ""
            pid, sid = current.split(":")[-1].split("/", 1)
            return f"{self._exam(pid)} · {self._skill(pid, sid) or sid}"

        rows = []
        prep = getattr(self.preparer, "state", {}) or {}
        rows.append({"what": "Writing and narrating lessons",
                     "state": f"Working on {skill_of(prep.get('current'))}" if prep.get("running") and prep.get("current") else "Waiting for work"})
        try:
            search = self.newexams.sourcer.diag()
            rows.append({"what": "Finding official pages", "state": f"Searching in {self._exam(search['current'])}" if search.get("current")
                         else (search.get("note") or "Waiting for work")})
        except Exception:  # noqa: BLE001
            pass
        for what, source, idle in (("Reading official pages again", self.drift, "Waiting for the next page"),
                                   ("Checking what the voices said", self.checker, "Waiting for the next lesson")):
            try:
                st = source.status()
                rows.append({"what": what, "state": f"Working on {st.get('current')}" if st.get("running") and st.get("current") else (st.get("note") or idle)})
            except Exception:  # noqa: BLE001
                continue
        try:
            running = [j for j in self.dojo.jobs.for_learner(self.owner) if j.get("state") in ("queued", "running")]
            for j in running[:5]:
                rows.append({"what": JOB_LABEL.get(j.get("kind")) or f"A “{j.get('kind')}” job", "state": str(j.get("step") or j.get("state"))})
        except Exception:  # noqa: BLE001
            pass
        return rows
