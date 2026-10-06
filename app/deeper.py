"""Deeper lessons (ADR 0008): more official pages for the skills of the built-in exams, and lessons
written again on the deeper template.

Every built-in skill was taught from one official page, while the exam tests the whole skill. In the
background, and only while the learner has nothing running (the Preparer's rule), Dojo searches the two
documentation sites for up to two more pages per skill, one group of skills at a time, SEARCH_GAP_S
apart. It uses the search, the picking and the checks of Add an exam (app/pagefinder.py): the author picks
from what the sites' own search found, the checker must copy a sentence from each page that shows it
teaches the skill, Dojo finds that sentence on the page itself, and only documentation is read, never
training, not even through a redirect. A page the skill cites already is never added again. What it finds
is kept apart from the repository's file, as an overlay (app/packages.py) that every part of Dojo reads as
the package's pages, the drift check included. A search that finds nothing new leaves the skill with its
one page and is tried again no sooner than SEARCH_AGAIN_DAYS later; one that failed, a day later. Added
exams are not searched: they got their pages when they were added.

Every new lesson is written on the deeper template (LESSON_TEMPLATE, app/learning.py). A skill whose
newest lesson is on the earlier template, or was not written from every page its package now cites, gets
a new lesson from the Preparer (app/prepare.py): after the drift check's rewrites and after missing
lessons, before narration, the active exam first and then by exam weight, at most DAILY_UPGRADES a day.
That allowance is its own, counted per UTC day in the content store so that a restart does not reset it,
and the drift check's rewrites never use it. A built-in skill waits for its search first, so its lesson is
written once, from all its pages. Each attempt is written down before any model is called. The lesson the
learner sees stays the earlier one until the new one passes every check (Dojo.make_lesson); a failed
attempt is tried again no sooner than UPGRADE_WAIT_H later, and after UPGRADE_TRIES failures it is given
up, which Studio shows. A skill whose lesson the drift check holds, or whose every page is gone, is left
to the drift check and uses none of this allowance. Deeper Listen (ADR 0007) is not written for a lesson
that will be written again (`to_write_again`): the new lesson gets it, so it is paid for once. A lesson
that already plays one loses it when it is replaced, as with a drift rewrite (Dojo._replaced).

Nothing here is evidence or touches the Record. Reading or hearing a lesson is exposure, and exposure
never fills a pip."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from .core import Learner, iso, parse_iso, utcnow
from .learning import LESSON_TEMPLATE
from .packages import MAX_SOURCES
from .pagefinder import PageFinder
from .sources import normalize, same_page

log = logging.getLogger("dojo.deeper")

DAILY_UPGRADES = 20        # lessons written again on the deeper template per UTC day, at most
UPGRADE_WAIT_H = 24        # a failed upgrade is tried again no sooner than this
UPGRADE_TRIES = 3          # and is given up after this many attempts that failed
SEARCH_GAP_S = 600         # at least this long between the searches for two groups of skills
SEARCH_AGAIN_DAYS = 30     # a skill a search found nothing new for is searched again no sooner than this
SEARCH_RETRY_H = 24        # a search that failed, or was cut short by a restart, is tried again after this
BUSY_WAIT_S = 60           # the learner has something running: look again after this
IDLE_WAIT_S = 3600         # every skill has been searched: look again after this
START_DELAY_S = 600        # after startup, the first search waits this long
FAILURES_KEPT = 20
STATE = "deeper-state"     # content/deeper-state.json
RECORD_NOTE = ("Nothing here is evidence. Pages found and lessons written again change what Dojo teaches, never "
               "the Record: reading or hearing a lesson is exposure and never fills a pip.")


def _when(value: Any) -> datetime | None:
    try:
        return parse_iso(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


class DeeperLessons:
    """The search for more pages (its own background thread) and the upgrade queue the Preparer works
    through. `busy` is the Preparer's rule for when the learner has something running."""

    def __init__(self, dojo: Any, owner: Learner, costs: Any = None):
        self.dojo = dojo
        self.owner = owner
        self.costs = costs
        self.finder = PageFinder(dojo)
        self.busy: Callable[[], bool] = lambda: False
        self.now: Callable[[], datetime] = utcnow
        self.state: dict = {"current": None, "note": "", "last": None}
        self._started = False
        self._one = threading.Lock()        # one search at a time
        self._overlays = threading.Lock()   # an overlay is read, changed and written by one thread at a time

    def start(self, delay: int = START_DELAY_S) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, args=(delay,), name="deeper", daemon=True).start()

    def _loop(self, delay: int) -> None:
        time.sleep(delay)
        while True:
            try:
                wait = self.search_step()
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("the search for more official pages crashed")
                wait = SEARCH_GAP_S
            time.sleep(max(1, wait))

    # ------------------------------------------------------------ what it writes down (content only)

    def _doc(self) -> dict:
        try:
            doc = self.dojo.store.read("content", STATE, default=None)
        except ValueError:
            doc = None
        doc = doc if isinstance(doc, dict) else {}
        day = iso(self.now())[:10]
        if doc.get("day") != day:
            doc["day"], doc["upgrades"] = day, 0
        doc["upgrades"] = int(doc.get("upgrades") or 0)
        for key, kind in (("skills", dict), ("spent", dict), ("failures", list)):
            if not isinstance(doc.get(key), kind):
                doc[key] = kind()
        return doc

    def _save(self, doc: dict) -> None:
        doc["updated"] = iso(self.now())
        self.dojo.store.write("content", STATE, value=doc)

    @staticmethod
    def _spent(doc: dict, pid: str) -> dict:
        spent = doc["spent"].get(pid)
        spent = spent if isinstance(spent, dict) else {}
        doc["spent"][pid] = {k: int(spent.get(k) or 0) for k in ("searched", "attempts", "written")}
        return doc["spent"][pid]

    def _failed(self, doc: dict, pid: str, sid: str, what: str, reason: str) -> None:
        doc["failures"].append({"at": iso(self.now()), "package": pid, "skill": sid, "what": what, "reason": str(reason)[:300]})
        doc["failures"] = doc["failures"][-FAILURES_KEPT:]

    # ------------------------------------------------------------ the pages a skill cites

    def _gone(self, url: Any) -> bool:
        return bool((self.dojo.sources.head(url) or {}).get("gone")) if isinstance(url, str) else True

    def _live(self, s: dict) -> list[str]:
        """The pages a skill cites that are not gone."""
        return [r["url"] for r in s.get("sources") or [] if isinstance(r, dict) and r.get("url") and not self._gone(r["url"])]

    def _found(self, s: dict, e: dict | None) -> tuple[list[str], list[str], list[str]]:
        """A skill's pages in three lists: the package's own, the pages a search added that are there, and
        the pages a search added that are gone (ADR 0006: not found, or moved, at two reads 6 hours apart)."""
        added = {r["url"] for r in (e or {}).get("sources") or [] if isinstance(r, dict) and isinstance(r.get("url"), str)}
        cited = [r["url"] for r in s.get("sources") or [] if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"]]
        found = [u for u in cited if u in added]
        return [u for u in cited if u not in added], [u for u in found if not self._gone(u)], [u for u in found if self._gone(u)]

    def _room(self, s: dict, e: dict | None) -> int:
        """How many pages a search may add for a skill. The package's own pages keep their places, gone or
        not (the package lists them). A page a search added keeps its place while it is there; once it is
        gone, its place goes to the page a search finds to replace it."""
        own, live, _ = self._found(s, e)
        return MAX_SOURCES - len(own) - len(live)

    def _to_replace(self, s: dict, e: dict | None) -> list[str]:
        """The pages a search added that are gone, and that no search has looked for a replacement for yet."""
        if not e:
            return []
        looked = e.get("gone_searched")
        looked = {u for u in looked if isinstance(u, str)} if isinstance(looked, list) else set()
        return [u for u in self._found(s, e)[2] if u not in looked]

    def _search_first(self, overlay: dict, s: dict) -> bool:
        """Whether a built-in skill's search comes before its lesson is written again: it has room for a page,
        and no search for it, as its words are now, has finished, or a page a search added is gone and no
        search has looked for a replacement yet. The new lesson is then written, and paid for, once."""
        e = self._entry(overlay, s)
        return self._room(s, e) > 0 and (not (e and _when(e.get("searched"))) or bool(self._to_replace(s, e)))

    def overlay(self, pid: str) -> dict:
        """The pages found for a built-in exam, as stored; an empty one when there are none or the stored one
        is not for this exam (the next search replaces it)."""
        doc = self.dojo.packages.overlay(pid)
        if not isinstance(doc, dict) or doc.get("package") != pid or not isinstance(doc.get("skills"), dict):
            doc = {"package": pid, "version": 1, "skills": {}}
        return doc

    @staticmethod
    def _entry(overlay: dict, s: dict) -> dict | None:
        """What the overlay says about a skill, unless the skill's words changed since."""
        e = overlay["skills"].get(s["id"])
        if not isinstance(e, dict) or normalize(str(e.get("skill_text") or "")) != normalize(s["text"]):
            return None
        return e

    def _searched(self, overlay: dict, s: dict) -> bool:
        """Whether a search for this skill, as its words are now, has ever finished."""
        e = self._entry(overlay, s)
        return bool(e and _when(e.get("searched")))

    def _search_due(self, overlay: dict, s: dict, now: datetime) -> bool:
        e = self._entry(overlay, s)
        if self._room(s, e) <= 0:
            return False
        if e is None:
            return True
        tried, searched = _when(e.get("tried")), _when(e.get("searched"))
        if tried and now - tried < timedelta(hours=SEARCH_RETRY_H):
            return False
        if not searched or (tried and tried > searched) or self._to_replace(s, e):
            return True   # it failed, a restart cut it short, or a page it added is gone: one search for a replacement
        if len(self._live(s)) >= 2 and not self._found(s, e)[2]:
            return False
        return now - searched >= timedelta(days=SEARCH_AGAIN_DAYS)

    def _active(self) -> str:
        return self.dojo.profile(self.owner)["active_package"]

    def _builtins(self) -> list[str]:
        """The built-in exams, the owner's active exam first, then those most members study (ADR 0009)."""
        return [pid for pid in self.dojo.exam_order(self.owner) if pid in self.dojo.packages.builtin]

    # ------------------------------------------------------------ searching

    def search_targets(self) -> list[tuple[str, dict, dict, list[str]]]:
        """The groups of skills of the built-in exams with a skill due for a search: the active exam first,
        then the groups with the most exam weight. A group is searched together, as Add an exam does."""
        now = self.now()
        out = []
        for pid in self._builtins():
            pkg = self.dojo.packages.by_id.get(pid)
            if not pkg:
                continue
            overlay = self.overlay(pid)
            groups = []
            for d in pkg["domains"]:
                for g in d["groups"]:
                    due = [sid for sid in g["skills"] if self._search_due(overlay, pkg["skills"][sid], now)]
                    if due:
                        groups.append((-max(pkg["skills"][sid]["share"] for sid in due), len(groups), d, g, due))
            out += [(pid, d, g, due) for _, _, d, g, due in sorted(groups, key=lambda x: x[:2])]
        return out

    def search_step(self) -> int:
        """Search for one group of skills when it is time. Returns the seconds until the next look."""
        with self._one:
            if self.busy():
                self.state["note"] = "Waiting: the learner has something running."
                return BUSY_WAIT_S
            now = self.now()
            last = _when(self._doc().get("last_search"))
            if last and (now - last).total_seconds() < SEARCH_GAP_S:
                return max(1, int(SEARCH_GAP_S - (now - last).total_seconds()))
            targets = self.search_targets()
            if not targets:
                self.state["note"] = "Every skill of the built-in exams has been searched."
                return IDLE_WAIT_S
            pid, d, g, sids = targets[0]
            self.search_group(pid, d, g, sids)
            return SEARCH_GAP_S

    def search_group(self, pid: str, domain: dict, group: dict, sids: list[str]) -> dict[str, dict]:
        """Find more pages for some skills of one group of a built-in exam and keep what passes the checks.
        The search is written down before any search or model call, so a crash or a restart cannot repeat
        it for free: the skills wait SEARCH_RETRY_H before they are tried again."""
        pkg = self.dojo.packages.get(pid)
        stamp = iso(self.now())
        with self._overlays:
            overlay = self.overlay(pid)
            for sid in sids:
                s = pkg["skills"][sid]
                entry = self._entry(overlay, s) or {"skill_text": s["text"], "sources": []}
                entry["tried"] = stamp
                overlay["skills"][sid] = entry
            self._write(pid, overlay)
        with self.dojo.store.lock:
            doc = self._doc()
            self._spent(doc, pid)["searched"] += len(sids)
            doc["last_search"] = stamp
            self._save(doc)
        own = {s["id"]: s for d in self.dojo.packages.builtin_raw(pid)["domains"] for g in d["groups"] for s in g["skills"]}
        known, limits = {}, {}
        for sid in sids:
            entry = overlay["skills"][sid]
            mine = [r for r in own[sid].get("sources") or [] if isinstance(r, dict) and r.get("url")]
            added = [r for r in entry.get("sources") or [] if isinstance(r, dict) and isinstance(r.get("url"), str)]
            known[sid] = [{"url": r["url"], "title": r.get("title") or r["url"]} for r in mine + added]   # gone ones too
            limits[sid] = max(0, self._room(pkg["skills"][sid], entry))
        self.state.update(current=f"{pid}: {group['title']}", note="")
        log.info("deeper lessons: searching for more pages for %d skills of %s / %s", len(sids), pid, group["title"])
        try:
            found = self.finder.match_group({"code": pkg["meta"]["exam"], "title": pkg["meta"]["title"]},
                                            {"title": domain["title"]}, {"title": group["title"]},
                                            [(sid, pkg["skills"][sid]["text"]) for sid in sids], [],
                                            known=known, limits=limits, reasons=True)
        except Exception as e:  # noqa: BLE001 - written down, tried again a day later
            log.exception("deeper lessons: the search for %s / %s failed", pid, group["title"])
            found = {sid: {"sources": [], "error": f"The search failed ({type(e).__name__})."} for sid in sids}
        finally:
            self.state.update(current=None, last=iso(self.now()))
        self._keep(pid, sids, found)
        return found

    def _keep(self, pid: str, sids: list[str], found: dict[str, dict]) -> None:
        now = iso(self.now())
        failures, replaced = [], []
        gone_at = lambda r: str((self.dojo.sources.head(r["url"]) or {}).get("gone") or "")  # noqa: E731
        with self._overlays:
            overlay = self.overlay(pid)
            pkg = self.dojo.packages.get(pid)
            own = {s["id"]: s for d in self.dojo.packages.builtin_raw(pid)["domains"] for g in d["groups"] for s in g["skills"]}
            for sid in sids:
                s = pkg["skills"][sid]
                entry = self._entry(overlay, s) or {"skill_text": s["text"], "sources": [], "tried": now}
                result = found.get(sid) or {"sources": [], "error": "The search did not answer for this skill."}
                if result.get("error"):
                    entry["error"] = str(result["error"])[:300]
                    failures.append((sid, entry["error"]))
                else:
                    refs = [dict(r) for r in own[sid].get("sources") or [] if isinstance(r, dict) and r.get("url")]
                    old = [r for r in entry.get("sources") or [] if isinstance(r, dict) and isinstance(r.get("url"), str)]
                    gone = sorted((r for r in old if self._gone(r["url"])), key=gone_at)
                    new = [{**r, "verified_at": now} for r in result.get("sources") or []]
                    kept = []
                    for r in [r for r in old if not any(r is x for x in gone)] + new:
                        if len(refs) + len(kept) < MAX_SOURCES and not any(same_page(r["url"], x["url"]) for x in refs + kept + gone):
                            kept.append(r)
                    # Each page found takes the place of a page a search added that is gone, the longest gone
                    # first; that page leaves the package. A gone page nothing replaces stays listed, and the
                    # drift check still reads it: it may come back.
                    out = gone[:sum(1 for r in kept if any(r is x for x in new))]
                    left = gone[len(out):]
                    replaced += [r["url"] for r in out]
                    entry.update(sources=kept + left, searched=now, error=None, found=len(new),
                                 candidates=int(result.get("candidates") or 0), gone_searched=[r["url"] for r in left])
                overlay["skills"][sid] = entry
            self._write(pid, overlay)
        if replaced:
            # What the drift check wrote down for a replaced page goes once nothing cites it (ADR 0008). Its
            # stored copy stays: an answer to an item written from it is still judged from that copy.
            drift = getattr(self.dojo, "drift", None)
            if drift is not None:
                drift.forget_pages(replaced)
            log.info("deeper lessons: %d page(s) that went gone replaced in %s", len(replaced), pid)
        if failures:
            with self.dojo.store.lock:
                doc = self._doc()
                for sid, reason in failures:
                    self._failed(doc, pid, sid, "search", reason)
                self._save(doc)
        log.info("deeper lessons: %d of %d skills searched, %d failed", len(sids) - len(failures), len(sids), len(failures))

    def _write(self, pid: str, overlay: dict) -> None:
        overlay.update(package=pid, version=1, updated=iso(self.now()))
        self.dojo.packages.set_overlay(pid, overlay)

    # ------------------------------------------------------------ upgrading lessons

    def _newest(self) -> dict[tuple, dict]:
        """The newest lesson of every skill (what `lessons_for` shows first)."""
        out: dict[tuple, dict] = {}
        for m in self.dojo.lesson_index():
            k = (m.get("package"), m.get("skill"))
            if k not in out or (m.get("created") or "") > (out[k].get("created") or ""):
                out[k] = m
        return out

    def why(self, s: dict, meta: dict) -> str | None:
        """Why a skill's newest lesson should be written again: "template" when it is on an earlier template,
        "pages" when its package cites a page, not gone, that the skill did not cite when the lesson was
        written. A page that could not be read then is not a reason: that lesson is not written again daily."""
        if int(meta.get("template") or 1) < LESSON_TEMPLATE:
            return "template"
        offered = meta.get("offered") or []
        if any(not any(same_page(url, u) for u in offered) for url in self._live(s)):
            return "pages"
        return None

    def _held(self, pid: str) -> set[str]:
        drift = getattr(self.dojo, "drift", None)
        return (drift.rechecking(pid) | drift.gone_skills(pid)) if drift else set()

    @staticmethod
    def _attempt(doc: dict, pid: str, sid: str, lesson_id: str) -> dict | None:
        """The attempts at upgrading this lesson, if any (a newer lesson starts again)."""
        a = doc["skills"].get(f"{pid}/{sid}")
        return a if isinstance(a, dict) and a.get("lesson") == lesson_id else None

    def _resting(self, a: dict | None, now: datetime) -> bool:
        if not a:
            return False
        if a.get("given_up"):
            return True
        last = _when(a.get("last"))
        return bool(last and now - last < timedelta(hours=UPGRADE_WAIT_H))

    def upgrades(self, doc: dict | None = None, by: dict | None = None) -> list[tuple[str, str, str]]:
        """(exam, skill, lesson) for every lesson to write again now, in the order the Preparer writes them:
        the active exam first, then by exam weight. Left out: a skill the drift check holds or finds gone,
        a built-in skill whose search comes first (`_search_first`), and one resting after a failed attempt
        or given up."""
        doc = self._doc() if doc is None else doc
        by = self._newest() if by is None else by
        now = self.now()
        active = self._active()
        counts = self.dojo.enrolled_counts()
        queue = []
        for pid in self.dojo.packages.ids():
            pkg = self.dojo.packages.by_id.get(pid)
            if not pkg:
                continue
            overlay = self.overlay(pid) if pid in self.dojo.packages.builtin else None
            held = None
            for s in pkg["skills"].values():
                meta = by.get((pid, s["id"]))
                if not meta or not s["sources"] or not self.why(s, meta):
                    continue
                if overlay is not None and self._search_first(overlay, s):
                    continue
                if self._resting(self._attempt(doc, pid, s["id"], meta["id"]), now):
                    continue
                held = self._held(pid) if held is None else held
                if s["id"] in held:
                    continue
                queue.append((pid != active, -counts.get(pid, 0), -s["share"], pid, s["id"], meta["id"]))
        return [(pid, sid, lid) for *_, pid, sid, lid in sorted(queue)]

    def to_write_again(self, doc: dict | None = None, by: dict | None = None) -> set[str]:
        """The newest lessons that will be written again on the deeper template: queued now, or waiting for
        their skill's search, a failed attempt's wait or the next day's allowance; not those given up.
        Deeper Listen (ADR 0007) waits for them, so it is written, and paid for, once: for the new lesson."""
        doc = self._doc() if doc is None else doc
        by = self._newest() if by is None else by
        out = set()
        for (pid, sid), meta in by.items():
            pkg = self.dojo.packages.by_id.get(pid)
            s = pkg["skills"].get(sid) if pkg else None
            if not s or not s["sources"] or not self.why(s, meta):
                continue
            if not (self._attempt(doc, pid, sid, meta["id"]) or {}).get("given_up"):
                out.add(meta["id"])
        return out

    def upgrades_left(self) -> int:
        return max(0, DAILY_UPGRADES - self._doc()["upgrades"])

    def next_upgrade(self) -> tuple[str, str, str] | None:
        """The next lesson to write again, while today's allowance lasts."""
        if self.upgrades_left() <= 0:
            return None
        queue = self.upgrades()
        return queue[0] if queue else None

    def charge_upgrade(self, pid: str, sid: str, lesson_id: str) -> bool:
        """Count an upgrade and write it down before any model is called, so a crash, an error or a restart
        gets no free retry and the wait, the attempts and the money spent survive. False, and nothing
        counted, when today's allowance is used up or the drift check holds the skill by now."""
        def charge() -> bool:
            with self.dojo.store.lock:
                doc = self._doc()
                if doc["upgrades"] >= DAILY_UPGRADES:
                    return False
                key = f"{pid}/{sid}"
                a = self._attempt(doc, pid, sid, lesson_id) or {"lesson": lesson_id, "tries": 0}
                if int(a.get("tries") or 0) >= UPGRADE_TRIES:
                    # Every attempt so far stopped without an answer (a crash or a restart): given up as well.
                    a.update(outcome="failed", given_up=iso(self.now()),
                             error=a.get("error") or "The attempts stopped before they finished (Dojo restarted or crashed).")
                    doc["skills"][key] = a
                    self._failed(doc, pid, sid, "upgrade", a["error"])
                    self._save(doc)
                    return False
                a.update(tries=int(a.get("tries") or 0) + 1, last=iso(self.now()), outcome="running", error=None)
                doc["skills"][key] = a
                doc["upgrades"] += 1
                doc["last_upgrade"] = {"skill": key, "at": a["last"]}
                self._spent(doc, pid)["attempts"] += 1
                self._save(doc)
                return True

        drift = getattr(self.dojo, "drift", None)
        return drift.unless_held(pid, sid, charge) if drift else charge()

    def upgraded(self, pid: str, sid: str, old_id: str, new_id: str) -> None:
        """The new lesson passed every check and is now the one the learner sees."""
        with self.dojo.store.lock:
            doc = self._doc()
            a = self._attempt(doc, pid, sid, old_id) or {}
            doc["skills"][f"{pid}/{sid}"] = {"lesson": new_id, "tries": 0, "last": a.get("last") or iso(self.now()),
                                             "outcome": "done", "replaced": old_id, "tries_needed": int(a.get("tries") or 1)}
            self._spent(doc, pid)["written"] += 1
            self._save(doc)
        log.info("deeper lessons: %s/%s written again on template %d", pid, sid, LESSON_TEMPLATE)

    def failed(self, pid: str, sid: str, lesson_id: str, reason: str) -> None:
        """The new lesson did not pass: the earlier one stays. Given up after UPGRADE_TRIES attempts."""
        with self.dojo.store.lock:
            doc = self._doc()
            a = self._attempt(doc, pid, sid, lesson_id) or {"lesson": lesson_id, "tries": 1, "last": iso(self.now())}
            a.update(outcome="failed", error=str(reason)[:300])
            if int(a.get("tries") or 0) >= UPGRADE_TRIES:
                a["given_up"] = iso(self.now())
            doc["skills"][f"{pid}/{sid}"] = a
            self._failed(doc, pid, sid, "upgrade", reason)
            self._save(doc)
        log.info("deeper lessons: writing %s/%s again failed: %s", pid, sid, reason)

    # ------------------------------------------------------------ what Studio and the diagnostics show

    def _estimate(self, searches: int, lessons: int, narrated: int | None = None, deep_again: int = 0) -> dict | None:
        return self.costs.deeper_estimate(searches, lessons, narrated, deep_again) if self.costs is not None else None

    def _exams(self, doc: dict) -> list[dict]:
        now = self.now()
        by = self._newest()
        queue = self.upgrades(doc, by)
        rows = []
        for pid in self.dojo.packages.ids():
            pkg = self.dojo.packages.by_id.get(pid)
            if not pkg:
                continue
            builtin = pid in self.dojo.packages.builtin
            overlay = self.overlay(pid) if builtin else None
            skills = list(pkg["skills"].values())
            newest = [(s, by[(pid, s["id"])]) for s in skills if s["sources"] and (pid, s["id"]) in by]
            behind = [(s, m) for s, m in newest if self.why(s, m)]
            given_up = [s["id"] for s, m in behind if (self._attempt(doc, pid, s["id"], m["id"]) or {}).get("given_up")]
            # A lesson that plays a Deeper Listen now gets a new one once it is written again (ADR 0007).
            deep_again = sum(1 for s, m in behind if s["id"] not in given_up and self.dojo.deep_written(m["id"]))
            to_search = [s for s in skills if builtin and self._search_first(overlay, s)]
            spent = self._spent(doc, pid)
            rows.append({
                "package": pid, "exam": pkg["meta"]["exam"], "title": pkg["meta"]["title"], "builtin": builtin,
                "skills": len(skills), "deep": sum(1 for s in skills if len(self._live(s)) >= 2),
                "searched": sum(1 for s in skills if builtin and self._searched(overlay, s)), "to_search": len(to_search),
                "replacing": sum(1 for s in to_search if self._to_replace(s, self._entry(overlay, s))),
                "lessons": len(newest), "deeper": len(newest) - sum(1 for s, m in newest if int(m.get("template") or 1) < LESSON_TEMPLATE),
                "behind": len(behind), "waiting": sum(1 for q in queue if q[0] == pid), "given_up": len(given_up),
                "searching_first": sum(1 for s, m in behind if builtin and self._search_first(overlay, s)),
                "spent": spent, "_left": (len(to_search), len(behind) - len(given_up), deep_again),
                "next_search": None if not builtin else min(
                    (iso(t) for t in (self._next_search(overlay, s, now) for s in skills) if t), default=None),
            })
        return rows

    def _next_search(self, overlay: dict, s: dict, now: datetime) -> datetime | None:
        e = self._entry(overlay, s)
        if e is None or self._room(s, e) <= 0 or self._search_due(overlay, s, now):
            return None
        tried, searched = _when(e.get("tried")), _when(e.get("searched"))
        if tried and (not searched or tried > searched or self._to_replace(s, e)):
            return tried + timedelta(hours=SEARCH_RETRY_H)
        if not searched or (len(self._live(s)) >= 2 and not self._found(s, e)[2]):
            return None
        return searched + timedelta(days=SEARCH_AGAIN_DAYS)

    def view(self) -> dict:
        """Studio's Deeper lessons card."""
        doc = self._doc()
        rows = self._exams(doc)
        for row in rows:
            searches, lessons, again = row.pop("_left")
            spent = row["spent"]
            row["spent_estimate"] = self._total(self._estimate(spent["searched"], spent["attempts"], spent["written"]))
            row["to_finish"] = self._total(self._estimate(searches, lessons, deep_again=again))
            row["deep_again"] = again
        left = [(r["to_search"], r["behind"] - r["given_up"], r["deep_again"]) for r in rows]
        spent = [r["spent"] for r in rows]
        return {
            "template": LESSON_TEMPLATE, "record": RECORD_NOTE, "started": self._started,
            "limits": {"per_day": DAILY_UPGRADES, "tries": UPGRADE_TRIES, "wait_hours": UPGRADE_WAIT_H,
                       "search_gap_minutes": SEARCH_GAP_S // 60, "search_again_days": SEARCH_AGAIN_DAYS,
                       "pages_per_skill": MAX_SOURCES},
            "today": {"day": doc["day"], "used": doc["upgrades"], "left": max(0, DAILY_UPGRADES - doc["upgrades"]),
                      "last": doc.get("last_upgrade")},
            "search": {"current": self.state["current"], "note": self.state["note"], "last": doc.get("last_search")},
            "exams": rows,
            "estimate": self._estimate(sum(a for a, _, _ in left), sum(b for _, b, _ in left), deep_again=sum(c for _, _, c in left)),
            "spent": self._estimate(sum(s["searched"] for s in spent), sum(s["attempts"] for s in spent),
                                    sum(s["written"] for s in spent)),
            "failures": list(reversed(doc["failures"])),
        }

    @staticmethod
    def _total(estimate: dict | None) -> dict | None:
        return {"total": estimate["total"], "complete": estimate["complete"]} if estimate else None

    def diag(self) -> dict:
        v = self.view()
        return {"started": v["started"], "template": v["template"], "per_day": v["limits"]["per_day"],
                "used_today": v["today"]["used"], "left_today": v["today"]["left"], "last_search": v["search"]["last"],
                "exams": [{k: r[k] for k in ("package", "skills", "deep", "searched", "to_search", "replacing", "lessons", "deeper",
                                             "behind", "waiting", "given_up", "deep_again", "spent", "spent_estimate", "to_finish")}
                          for r in v["exams"]],
                "estimate_usd": (v["estimate"] or {}).get("total"), "spent_usd": (v["spent"] or {}).get("total"),
                "failures": v["failures"][:5]}
