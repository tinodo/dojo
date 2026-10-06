"""The source-drift check (DESIGN section 3, ADR 0006): when an official page that a lesson or a question
was written from changes, what was written from it is checked again, word for word.

Every lesson, item and practice-exam question records the pages it was written from and the hash of each
page's text at that moment (`source_refs`). Each cited page is read again through `Sources` at most once
a day: only the two allowed sites, at most DAILY_FETCHES reads a day, at least FETCH_GAP_S apart, and a
site that asks Dojo to slow down (429, 403 or a 5xx, with its Retry-After) is left alone for as long as
it asks, up to a day. The day's count is written to the share before a page is read, so a restart does
not hand the check a fresh allowance.

A new hash alone is not a reason to act. When only the formatting changed (the text the quote check
reads is the same), nothing is re-checked and Studio lists it as a formatting change. When words
changed, every quote of every lesson, item and pool question that cites the page is looked for again in
the new text:
- still holds: every quote is still on the page word for word. Written down; nothing else happens;
- needs re-check: a quote is gone, or the page is gone: not found, or moved to another page, twice,
  GONE_AFTER_HOURS apart. The lesson is held back from the podcast feed and carries a dated notice, and
  the Preparer writes the skill's lesson again (author, then gate, then narration), at most
  DAILY_REWRITES lessons a day; the rest wait their turn. A skill whose every cited page is gone cannot
  be written again: it waits, using none of the day's rewrites, until a page is back or its package
  cites another page (`gone_skills`). An item or question is held back: it is not answered, not issued
  again, not in the practice-exam pool, not in a three-minute check and not answered in a quick
  practice. A practice exam that is running keeps its questions; when it closes, one whose quote is gone
  is withdrawn: it is not scored and not counted.

Verdicts are worked out from the stored snapshots whenever they are asked for, and cached per page
version, so they cannot drift out of step with the pages: a page that changes back makes a lesson hold
again. Only a copy Dojo may use counts (`Sources.head`): read from a checked documentation page, and
from the cited page itself. Any other stored copy is read again in its turn and judged from then on.
What this module writes is content data in content/drift-state: when each page was read, the
changes found and the lessons they touched, the owner's approvals to film and the film jobs filed for
them. What a change means for the learner's own items and practice-exam questions is counted when it is
shown, so deleting the record leaves nothing of them here.

Filming costs money, so nothing is filmed on its own. A lesson written again whose earlier version was
filmed waits until the owner presses "Re-film" in Studio; its films are then filed one at a time
through the ordinary video job. The learner's Record is never written here: an answer given before a
change stays as it was (EG-34), and the skill page says that the source changed after it.

The hands-on labs cite official pages too, but they have no quotes to look for. A lab page is read once
to start from, and from then on a change in its words, or the page going away, flags the lab for a
person to look at. The flag stays until the owner says in Studio that they looked; the lab stays open,
as a lab is practice and fills no pip."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import HTTPException

from . import items as exam_items
from .avatar import lesson_turns, media_id_for
from .core import Learner, UserError, iso, one_line, parse_iso, sha256, utcnow
from .costs import LABEL as PRICE_LABEL
from .labs import LABS
from .sources import check_url, docs_url

log = logging.getLogger("dojo.drift")

DAILY_FETCHES = 400        # pages read again per day, at most (ADR 0008: about three pages per skill)
DAILY_REWRITES = 10        # lessons written again per day because a page changed (author and gate calls), at most
FETCH_GAP_S = 180          # at least this long between two reads, so a day's reads are spread out
RECHECK_HOURS = 24         # a page is read again at most once a day
MISSING_RETRY_HOURS = 6    # a page that was not found (or moved) is read again this much later
GONE_AFTER_HOURS = 6       # and counts as gone after a second such read at least this long after the first
HOST_WAIT_S = 3600         # a site that asks Dojo to slow down is left alone this long, or as long as it asks
HOST_WAIT_MAX_S = 86400    # but never longer than a day
IDLE_WAIT_S = 1800
FILM_WAIT_S = 60           # how often an approved film's job is looked at
FILM_TRIES = 2             # a film cut off by a restart is filed once more
CHANGES_KEPT = 60
QUOTES_SHOWN = 5
CHARS_PER_SECOND = 14.0    # a presenter's pace, until Dojo has filmed enough to measure it
STATE = "drift-state"

NOTICE = "A source page changed on {date}. Dojo is re-checking this lesson."
HOLDS = "A source page changed on {date}. Every quote in this lesson is still on the page, word for word."
REWRITTEN = "A source page changed on {date}, so this lesson was written again from the page as it is now and checked again."
ITEM_NOTICE = ("A source page changed on {date} and no longer has the words this question was written from. Dojo holds it "
               "back until a new one is written and checked: it cannot be answered, and it is not used again.")
ATTEMPT_NOTICE = ("A source page this question was written from changed on {date}{after}. Your answer and its result stay in "
                  "your record as they were. To show this skill against the page as it is now, take a new check: studying "
                  "the lesson again does not renew the proof.")
FILM_WAITING = ("The video of the earlier version no longer matches this lesson, so it is not shown. Filming costs money, "
                "so Dojo waits for your go-ahead, here or in Studio. Until then, use Present, Read or Listen.")
FILM_REMAKING = "The video is being remade. Until then, use Present, Read or Listen."
RECORD_NOTE = ("Your record is never rewritten. An answer given before a page changed stays as it was; the skill page "
               "says that the source changed after it.")
LAB_NOTICE = "A source page changed on {date}. Dojo flagged this lab for a look: check its steps against the official docs."
PAGES_GONE = "All its official pages are gone or moved; update the package's source."
PUBLISHERS = {"learn.microsoft.com": "Microsoft Learn", "docs.github.com": "GitHub Docs"}


def _day(ts: str | None) -> str:
    """A date as people write it: 24 Sep 2026."""
    if not ts:
        return ""
    try:
        dt = parse_iso(ts)
    except ValueError:
        return ""
    return f"{dt.day} {dt.strftime('%b %Y')}"


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _allowed(url: Any) -> bool:
    try:
        check_url(url)
    except (UserError, TypeError, ValueError, AttributeError):
        return False
    return True


def lesson_quotes(doc: dict) -> list[tuple[Any, str]]:
    """(source number, quote) of every point, misconception and self-check answer of a lesson."""
    out = []
    for sec in doc.get("sections") or []:
        for p in (sec or {}).get("points") or []:
            out.append((p.get("source"), p.get("quote")))
    for part in ("misconceptions", "self_check"):
        for m in doc.get(part) or []:
            out.append((m.get("source"), m.get("quote")))
    return out


def written_from(refs: Any, quotes: list[tuple[Any, str]]) -> dict[str, dict]:
    """Per cited page: the version (hash) it was written from and the quotes taken from it."""
    by_n = {r.get("n"): r for r in refs or [] if isinstance(r, dict) and r.get("url")}
    out = {r["url"]: {"sha": r.get("sha256"), "quotes": []} for r in by_n.values()}
    for n, quote in quotes:
        ref = by_n.get(n)
        if ref and quote and quote not in out[ref["url"]]["quotes"]:
            out[ref["url"]]["quotes"].append(quote)
    return out


class DriftCheck:
    """Source drift (ADR 0006): reads every cited page again within a daily budget and, when the words
    change, checks every quote that cites the page again."""
    def __init__(self, dojo: Any, owner: Learner, costs: Any = None):
        self.dojo = dojo
        self.sources = dojo.sources
        self.owner = owner
        self.costs = costs
        self.now = utcnow   # the clock: tests move it
        self.state: dict = {"running": False, "current": None, "last": None, "note": "", "read": 0}
        self._started = False
        self._one = threading.Lock()     # one step at a time
        self._films = threading.Lock()   # filing film jobs, one caller at a time
        self._marks = threading.Lock()   # marking a page gone and counting a rewrite never cross
        self._film_thread: threading.Thread | None = None
        self._written: dict[str, dict] = {}   # lesson id -> written_from(); a lesson never changes
        self._items: dict[str, dict] = {}     # item id -> written_from() of its rubric
        self._plans: dict[str, dict] = {}     # lesson id -> what filming it takes
        self._verdicts: dict[str, tuple] = {}  # key -> (page versions, verdict)
        self._titles: dict[str, dict] = {}    # url -> its title and publisher, from the packages
        self.labs: dict = LABS                # the hands-on labs and the pages they cite

    # ------------------------------------------------------------ loop

    def start(self, delay: int = 900) -> None:
        if self._started:
            return
        self._started = True
        self.state["note"] = f"the first check starts {max(1, delay // 60)} minutes after the app"
        threading.Thread(target=self._loop, args=(delay,), name="drift-check", daemon=True).start()

    def _loop(self, delay: int) -> None:
        time.sleep(delay)
        while True:
            try:
                wait = self.step()
            except Exception:  # noqa: BLE001 - never let the background loop die
                log.exception("drift check step crashed")
                wait = IDLE_WAIT_S
            time.sleep(wait)

    # ------------------------------------------------------------ what it writes down (content only)

    def _doc(self) -> dict:
        try:
            doc = self.dojo.store.read("content", STATE, default=None)
        except ValueError:
            doc = None
        doc = doc if isinstance(doc, dict) else {}
        day = iso(self.now())[:10]
        if doc.get("day") != day:
            doc["day"], doc["read"], doc["rewrites"] = day, 0, 0
        for key, kind in (("pages", dict), ("hosts", dict), ("changes", list), ("films", dict), ("approvals", list),
                          ("labs", dict), ("replaced", dict)):
            if not isinstance(doc.get(key), kind):
                doc[key] = kind()
        doc["read"] = int(doc.get("read") or 0)
        doc["rewrites"] = int(doc.get("rewrites") or 0)
        return doc

    def _save(self, doc: dict) -> None:
        doc["updated"] = iso(self.now())
        self.dojo.store.write("content", STATE, value=doc)

    def _charge(self, url: str, now: Any) -> bool:
        """Count the read and set the page's next turn before reading it, so a crash or a restart
        cannot read it again for free."""
        with self.dojo.store.lock:
            doc = self._doc()
            if doc["read"] >= DAILY_FETCHES:
                return False
            doc["read"] += 1
            doc["last_fetch"] = iso(now)
            doc["pages"].setdefault(url, {}).update(checked=iso(now), outcome="reading",
                                                    next=iso(now + timedelta(hours=RECHECK_HOURS)))
            self._save(doc)
            return True

    def rewrites_left(self) -> int:
        """How many more lessons the Preparer may write again today because a page changed."""
        return max(0, DAILY_REWRITES - self._doc()["rewrites"])

    def unless_held(self, pid: str, sid: str, charge: Callable[[], bool]) -> bool:
        """Run `charge`, which counts other work about to be written for a skill (a deeper lesson, ADR 0008),
        unless this check holds the skill's lesson or finds every page it cites gone: that skill is the
        check's to write again. Under the lock that marks a page gone, as `charge_rewrite` is. False, and
        `charge` not run, when held."""
        with self._marks:
            if sid in self.gone_skills(pid) or sid in self.rechecking(pid):
                return False
            return charge()

    def charge_rewrite(self, pid: str, sid: str) -> bool:
        """Count a rewrite before it starts, as a read is counted: a crash or a restart gets no free retry,
        and one that fails still counts. False, and nothing counted, when the day's allowance is used up,
        or when every page the skill cites is gone by now: the check may have marked the last one gone
        after the Preparer picked the skill. A page is marked gone under the same lock (`_record`), so the
        look and the count cannot let a page go in between."""
        with self._marks:
            if sid in self.gone_skills(pid):
                return False
            with self.dojo.store.lock:
                doc = self._doc()
                if doc["rewrites"] >= DAILY_REWRITES:
                    return False
                doc["rewrites"] += 1
                doc["last_rewrite"] = {"skill": f"{pid}/{sid}", "at": iso(self.now())}
                self._save(doc)
                return True

    def _rewrites(self, doc: dict, waiting: int, for_pages: int) -> dict:
        return {"per_day": DAILY_REWRITES, "used_today": doc["rewrites"], "left_today": max(0, DAILY_REWRITES - doc["rewrites"]),
                "waiting": waiting, "waiting_for_pages": for_pages, "last": doc.get("last_rewrite")}

    def _record(self, url: str, result: dict, now: Any) -> None:
        """Write down what a read found. A page that is not there, not found or moved to another page, is
        gone after two such reads at least GONE_AFTER_HOURS apart; the last one says whether it moved."""
        outcome, gone_since = result.get("outcome"), None
        with self.dojo.store.lock:
            doc = self._doc()
            page = doc["pages"].setdefault(url, {})
            page.update(outcome=outcome, status=result.get("status"), note=result.get("reason") or "")
            if outcome in ("missing", "moved"):
                page["misses"] = int(page.get("misses") or 0) + 1
                first = page.setdefault("missing_since", iso(now))
                if outcome == "moved":
                    page["moved_to"] = result.get("to")
                else:
                    page.pop("moved_to", None)
                if page["misses"] >= 2 and now - parse_iso(first) >= timedelta(hours=GONE_AFTER_HOURS):
                    gone_since = first
                else:
                    page["next"] = iso(now + timedelta(hours=MISSING_RETRY_HOURS))
            elif outcome == "limited":
                asked = float(result.get("retry_after") or 0)
                until = iso(now + timedelta(seconds=min(HOST_WAIT_MAX_S, max(HOST_WAIT_S, asked))))
                doc["hosts"][_host(url)] = until
                page["next"] = until
            elif outcome in ("same", "changed"):
                page.pop("misses", None)
                page.pop("missing_since", None)
                page.pop("moved_to", None)
                if outcome == "changed":
                    page["changed"] = iso(now)
            self._save(doc)
        if gone_since:
            to = result.get("to") if outcome == "moved" else None
            with self._marks:   # never in the middle of counting a rewrite (charge_rewrite)
                self.sources.mark_gone(url, gone_since, to=to)
            log.info("drift check: a cited page is gone (%s since %s)", "moved" if to else "not found", gone_since)

    def forget(self, pid: str) -> None:
        """An added exam was removed: what the check wrote down for it goes too. Its lessons leave the changes
        found, its films waiting or made leave the list, and a page nothing cites any more is no longer
        read. Stored pages stay, as another exam may cite them. Only content is written."""
        with self.dojo.store.lock:
            doc = self._doc()
            cited = set(self.cited())
            for c in doc["changes"]:
                c["lessons"] = [x for x in c.get("lessons") or [] if x.get("package") != pid]
            doc["changes"] = [c for c in doc["changes"] if c.get("url") in cited]
            doc["films"] = {k: f for k, f in doc["films"].items() if f.get("package") != pid}
            doc["pages"] = {u: p for u, p in doc["pages"].items() if u in cited}
            self._save(doc)
        for cache in (self._written, self._plans, self._verdicts, self._titles):
            cache.clear()
        log.info("drift check: forgot what it wrote down for the removed exam %s", one_line(pid))

    def forget_pages(self, urls: list[str]) -> None:
        """Pages Dojo had found for a skill that went gone and that a search replaced (ADR 0008): no package
        lists them any more. What the check wrote down for such a page goes once nothing cites it, as for
        the pages of a removed exam (`forget`): its reads, and the changes found on it. While the newest
        lesson of a skill still cites it, it stays, so that lesson is still held and written again; the
        next look (`_tidy`) finishes then. The stored copy of the page stays: an answer to an item written
        from it is still judged from that copy (Dojo.item_grounding)."""
        with self.dojo.store.lock:
            doc = self._doc()
            for url in urls:
                doc["replaced"][url] = iso(self.now())
            self._save(doc)
        self._tidy()

    def _tidy(self, cited: list[str] | None = None) -> None:
        """Forget the replaced pages nothing cites any more."""
        if not self._doc()["replaced"]:
            return
        cited = set(self.cited() if cited is None else cited)
        with self.dojo.store.lock:
            doc = self._doc()
            done = [url for url in doc["replaced"] if url not in cited]
            if not done:
                return
            for url in done:
                doc["replaced"].pop(url, None)
                doc["pages"].pop(url, None)
            doc["changes"] = [c for c in doc["changes"] if c.get("url") not in done]
            self._save(doc)
        for url in done:
            self._titles.pop(url, None)
        log.info("drift check: forgot what it wrote down for %d replaced page(s) nothing cites any more", len(done))

    # ------------------------------------------------------------ what is cited, and what is due

    def cited(self, by: dict | None = None) -> list[str]:
        """Every page a package lists for a skill, every page the newest lesson of a skill cites, and every
        page a lab cites."""
        by = self._by_skill() if by is None else by
        urls: dict[str, None] = {}
        for pid in self.dojo.packages.ids():
            for s in self.dojo.packages.get(pid)["skills"].values():
                for ref in s.get("sources") or []:
                    if isinstance(ref, dict) and ref.get("url"):
                        urls.setdefault(ref["url"], None)
                        self._titles.setdefault(ref["url"], {"title": ref.get("title") or ref["url"],
                                                             "publisher": ref.get("publisher", "")})
            for meta in self._latest(pid, by).values():
                for url in self._lesson_written(meta["id"]):
                    urls.setdefault(url, None)
        for spec in self.labs.values():
            for ref in spec.get("sources") or []:
                if isinstance(ref, dict) and ref.get("url"):
                    urls.setdefault(ref["url"], None)
                    self._titles.setdefault(ref["url"], {"title": ref.get("title") or ref["url"],
                                                         "publisher": PUBLISHERS.get(_host(ref["url"]), "")})
        return [u for u in urls if _allowed(u) and docs_url(u)]

    @staticmethod
    def _lab_urls(spec: dict) -> list[str]:
        return [r["url"] for r in spec.get("sources") or [] if isinstance(r, dict) and _allowed(r.get("url")) and docs_url(r["url"])]

    def _mark(self, url: str) -> str | None:
        """The page's stored version: its hash, marked when the page is gone (or moved); None if never read,
        or if its copy is not one Dojo may use (`Sources.head`)."""
        head = self.sources.head(url)
        if head is None:
            return None
        if head.get("gone"):
            return f'gone:{head["sha256"] or ""}'
        return head["sha256"] if head["verified"] else None

    @staticmethod
    def _host_waits(doc: dict, host: str, now: Any) -> bool:
        until = doc["hosts"].get(host)
        return bool(until and parse_iso(until) > now)

    def _due(self, doc: dict, now: Any) -> str | None:
        """The page whose turn came first: read a day after its snapshot, and not before its own next turn.
        A page a lab cites that Dojo never read is read once, to start from, after the pages due again."""
        first = {u for spec in self.labs.values() for u in self._lab_urls(spec)}
        best, best_at = None, None
        for url in self.cited():
            head = self.sources.head(url)
            if (head is None and url not in first) or self._host_waits(doc, _host(url), now):
                continue   # never read: nothing was written from it
            at = parse_iso(head["fetched_at"]) + timedelta(hours=RECHECK_HOURS) if head else now
            nxt = (doc["pages"].get(url) or {}).get("next")
            if nxt:
                at = max(at, parse_iso(nxt))
            if at <= now and (best_at is None or at < best_at):
                best, best_at = url, at
        return best

    # ------------------------------------------------------------ one step

    def step(self) -> int:
        """Read one page again when one is due, and sort out what changed. Returns the seconds to wait."""
        with self._one:
            return self._step()

    def _step(self) -> int:
        self._classify()
        self._films_soon()
        now, doc = self.now(), self._doc()
        if doc["read"] >= DAILY_FETCHES:
            self.state.update(running=False, current=None, note="today's reads are done")
            return IDLE_WAIT_S
        last = doc.get("last_fetch")
        if last:
            waited = (now - parse_iso(last)).total_seconds()
            if waited < FETCH_GAP_S:
                self.state.update(note="waiting between two reads")
                return max(1, int(FETCH_GAP_S - waited))
        url = self._due(doc, now)
        if not url:
            waiting = [h for h in doc["hosts"] if self._host_waits(doc, h, now)]
            self.state.update(running=False, current=None,
                              note=f"waiting, as asked by {', '.join(sorted(waiting))}" if waiting else "every page was read in the last day")
            return IDLE_WAIT_S
        if not self._charge(url, now):
            self.state.update(note="today's reads are done")
            return IDLE_WAIT_S
        self.state.update(running=True, current=url, note="")
        try:
            result = self.sources.recheck(url)
            self.state["read"] += 1
            self._record(url, result, now)
            log.info("drift check: read a cited page again: %s", result.get("outcome"))
        finally:
            self.state.update(running=False, current=None, last=iso(now))
        self._classify()
        return FETCH_GAP_S

    # ------------------------------------------------------------ verdicts, worked out from the snapshots

    def _lesson_written(self, lesson_id: str) -> dict[str, dict]:
        w = self._written.get(lesson_id)
        if w is None:
            try:
                doc = self.dojo.lesson(lesson_id)
            except HTTPException:
                return {}
            w = self._written[lesson_id] = written_from(doc.get("sources"), lesson_quotes(doc))
            for ref in doc.get("sources") or []:
                if isinstance(ref, dict) and ref.get("url"):
                    self._titles.setdefault(ref["url"], {"title": ref.get("title") or ref["url"],
                                                         "publisher": ref.get("publisher", "")})
        return w

    def _item_written(self, it: dict) -> dict[str, dict]:
        key = str(it.get("id") or "")
        w = self._items.get(key)
        if w is None:
            rubric = [(r.get("source"), r.get("quote")) for r in it.get("rubric") or [] if isinstance(r, dict)]
            w = written_from(it.get("sources"), rubric)
            if key:
                self._items[key] = w
        return w

    @staticmethod
    def _question_written(q: dict) -> tuple[str, dict[str, dict]]:
        if q.get("quotes") or q.get("kind") == "case":
            # An exam item of ADR 0012: every part of its key rests on its own quote, maybe on another page.
            written: dict[str, dict] = {}
            for x in exam_items.quotes(q):
                url = x["source"].get("url")
                if url:
                    w = written.setdefault(url, {"sha": x["source"].get("sha256"), "quotes": []})
                    if x["quote"] not in w["quotes"]:
                        w["quotes"].append(x["quote"])
            if not written:
                return "", {}
            body = "|".join(f'{u}|{w["sha"]}|{"|".join(w["quotes"])}' for u, w in sorted(written.items()))
            return "q:" + sha256(body)[:32], written
        ref = q.get("source") if isinstance(q.get("source"), dict) else {}
        if not ref.get("url"):
            return "", {}
        key = "q:" + sha256(f'{ref["url"]}|{ref.get("sha256")}|{q.get("quote") or ""}')[:32]
        return key, {ref["url"]: {"sha": ref.get("sha256"), "quotes": [q["quote"]] if q.get("quote") else []}}

    def _judge_url(self, url: str, w: dict) -> dict | None:
        """What the stored page means for what was written from one version of it: None when the words
        are as they were (the formatting may differ), else "holds" or "recheck"."""
        c = self.sources.compare(url, w.get("sha"))
        if c["state"] == "gone":
            return {"state": "recheck", "since": c["since"], "gone": True, "removed": []}
        if c["state"] != "changed":
            return None
        removed = [q for q in w["quotes"] if not self.sources.holds(url, q)]
        return {"state": "recheck" if removed else "holds", "since": c["since"], "gone": False, "removed": removed}

    def _judge(self, key: str, written: dict[str, dict]) -> dict | None:
        marks = tuple((url, self._mark(url)) for url in sorted(written))
        cached = self._verdicts.get(key)
        if cached and cached[0] == marks:
            return cached[1]
        pages = {url: v for url in sorted(written) if (v := self._judge_url(url, written[url]))}
        verdict = None
        if pages:
            state = "recheck" if any(v["state"] == "recheck" for v in pages.values()) else "holds"
            counted = [v["since"] for v in pages.values() if v["state"] == state and v["since"]]
            verdict = {"state": state, "since": min(counted) if counted else None,
                       "gone": [url for url, v in pages.items() if v["gone"]],
                       "removed": [{"url": url, "quote": q} for url, v in pages.items() for q in v["removed"]],
                       "pages": sorted(pages)}
        if len(self._verdicts) > 20000:
            self._verdicts.clear()
        self._verdicts[key] = (marks, verdict)
        return verdict

    def lesson_state(self, lesson_id: str) -> dict | None:
        """None while every page the lesson cites has its words as they were; else "holds" (every quote
        is still on the page) or "recheck" (a quote or a whole page is gone)."""
        w = self._lesson_written(lesson_id)
        return self._judge("lesson:" + lesson_id, w) if w else None

    def withheld(self, lesson_id: str) -> bool:
        """A lesson that needs re-checking: kept out of the podcast feed and written again."""
        return (self.lesson_state(lesson_id) or {}).get("state") == "recheck"

    def item_state(self, it: dict) -> dict | None:
        w = self._item_written(it)
        return self._judge("item:" + str(it.get("id")), w) if w else None

    def item_withheld(self, it: dict) -> bool:
        """An item not answered yet whose key quotes are gone is not answered, and not issued again."""
        return not it.get("answer") and (self.item_state(it) or {}).get("state") == "recheck"

    def question_note(self, q: dict) -> dict | None:
        """For a practice-exam or quick-practice question whose quote is gone from its page: the dated
        notice that holds it back. None while the question holds."""
        key, w = self._question_written(q)
        v = self._judge(key, w) if w else None
        if not v or v["state"] != "recheck":
            return None
        date = _day(v["since"])
        return {"state": "withheld", "since": v["since"], "date": date, "notice": ITEM_NOTICE.format(date=date)}

    def question_ok(self, q: dict) -> bool:
        """False for a practice-exam or quick-practice question whose quote is gone from its page."""
        return self.question_note(q) is None

    def _by_skill(self) -> dict[tuple, list[dict]]:
        """Every lesson per (package, skill), newest first as `lessons_for` orders them. Built once per
        call from one listing of the lessons folder, which is slow on the share."""
        out: dict[tuple, list[dict]] = {}
        for m in self.dojo.lesson_index():
            out.setdefault((m.get("package"), m.get("skill")), []).append(m)
        for v in out.values():
            v.sort(key=lambda d: d.get("created") or "", reverse=True)
        return out

    def _latest(self, pid: str, by: dict | None = None) -> dict[str, dict]:
        """The newest lesson of every skill of a package."""
        by = self._by_skill() if by is None else by
        return {sid: v[0] for (p, sid), v in by.items() if p == pid and v}

    def rechecking(self, pid: str) -> set[str]:
        """The skills of a package whose newest lesson needs re-checking: the Preparer writes them again."""
        return {sid for sid, m in self._latest(pid).items() if self.withheld(m["id"])}

    def gone_skills(self, pid: str) -> set[str]:
        """The skills of a package whose every cited page is gone, moved included. Nothing new can be
        written for them, no lesson and no question, until one of their pages is back. They keep their
        sources and their share of the exam: this is a hold, not a missing source."""
        gone: dict[str, bool] = {}
        out: set[str] = set()
        pkg = self.dojo.packages.by_id.get(pid)   # an exam removed a moment ago has none
        for sid, s in (pkg["skills"] if pkg else {}).items():
            urls = [r["url"] for r in s.get("sources") or [] if isinstance(r, dict) and r.get("url")]
            for u in urls:
                if u not in gone:
                    gone[u] = bool((self.sources.head(u) or {}).get("gone"))
            if urls and all(gone[u] for u in urls):
                out.add(sid)
        return out

    def skill_states(self, pid: str) -> dict[str, dict]:
        """What the course map shows per skill."""
        out = {}
        for sid, m in self._latest(pid).items():
            v = self.lesson_state(m["id"])
            if v and v["state"] == "recheck":
                out[sid] = {"state": "recheck", "date": _day(v["since"]), "since": v["since"], "notice": NOTICE.format(date=_day(v["since"]))}
        return out

    @staticmethod
    def _meta(lesson_id: str, by: dict) -> dict | None:
        return next((m for v in by.values() for m in v if m["id"] == lesson_id), None)

    @staticmethod
    def _previous(meta: dict, by: dict) -> dict | None:
        """The lesson written for the same skill just before this one."""
        lessons = by.get((meta.get("package"), meta.get("skill"))) or []
        ids = [x["id"] for x in lessons]
        k = ids.index(meta["id"]) if meta["id"] in ids else -1
        return lessons[k + 1] if 0 <= k < len(lessons) - 1 else None

    def _rewrite_of(self, meta: dict, by: dict) -> dict | None:
        """The lesson this one was written to replace: the one before it no longer held when it was written."""
        prev = self._previous(meta, by)
        v = self.lesson_state(prev["id"]) if prev else None
        if not v or v["state"] != "recheck":
            return None
        return prev if (meta.get("created") or "") >= (v.get("since") or "") else None

    def lesson_note(self, lesson_id: str) -> dict | None:
        """What a lesson's pages say about a source change: it is being re-checked, it still holds, or it
        was written again because the one before it no longer held."""
        v = self.lesson_state(lesson_id)
        by = self._by_skill()
        meta = self._meta(lesson_id, by)
        if v and v["state"] == "recheck":
            date = _day(v["since"])
            latest = self._latest(meta["package"], by).get(meta["skill"]) if meta else None
            newer = latest["id"] if latest and latest["id"] != lesson_id and not self.withheld(latest["id"]) else None
            return {"state": "recheck", "since": v["since"], "date": date, "notice": NOTICE.format(date=date),
                    "gone": [self._page(u) for u in v["gone"]], "removed": [r["quote"] for r in v["removed"]],
                    "pages": [self._page(u) for u in v["pages"]], "replaced_by": newer}
        prev = self._rewrite_of(meta, by) if meta else None
        if prev:
            pv = self.lesson_state(prev["id"]) or {}
            date = _day(pv.get("since"))
            return {"state": "rewritten", "since": pv.get("since"), "date": date, "notice": REWRITTEN.format(date=date),
                    "replaces": prev["id"], "film": self._film_note(lesson_id, prev["id"])}
        if v:
            date = _day(v["since"])
            return {"state": "holds", "since": v["since"], "date": date, "notice": HOLDS.format(date=date),
                    "pages": [self._page(u) for u in v["pages"]]}
        return None

    def item_note(self, it: dict) -> dict | None:
        """For an item a changed page no longer supports: held back when it is not answered yet; when it
        is, a line for the record saying the page changed, and whether that was after the answer."""
        v = self.item_state(it)
        if not v or v["state"] != "recheck":
            return None
        date = _day(v["since"])
        answer = it.get("answer") if isinstance(it.get("answer"), dict) else None
        if not answer:
            return {"state": "withheld", "since": v["since"], "date": date, "notice": ITEM_NOTICE.format(date=date)}
        # An item that needs re-checking refuses answers, so one given in the very second the change was
        # first seen came in before it: "after" holds for it too.
        after = bool(v["since"] and (answer.get("at") or "") <= v["since"])
        return {"state": "changed", "since": v["since"], "date": date, "after_answer": after,
                "notice": ATTEMPT_NOTICE.format(date=date, after=", after this answer" if after else "")}

    def lab_state(self, lab: str, doc: dict | None = None) -> dict | None:
        """A lab whose page changed its words, or went away, since Dojo started watching it for the lab or
        the owner last looked at the lab: flagged for a look. None otherwise. A lab has no quotes to look
        for, so only a person can say whether its steps still match."""
        spec = self.labs.get(lab)
        if not spec:
            return None
        doc = self._doc() if doc is None else doc
        watched = (doc["labs"].get(lab) or {}).get("pages") or {}
        pages, gone, sinces = [], [], []
        for url in self._lab_urls(spec):
            mark = watched.get(url)
            if not mark or mark == self._mark(url):
                continue
            c = self.sources.compare(url, mark.removeprefix("gone:"))
            if c["state"] in ("changed", "gone"):
                pages.append(url)
                if c["state"] == "gone":
                    gone.append(url)
                if c["since"]:
                    sinces.append(c["since"])
        if not pages:
            return None
        since = min(sinces) if sinces else None
        date = _day(since)
        return {"state": "recheck", "since": since, "date": date, "notice": LAB_NOTICE.format(date=date),
                "pages": [self._page(u) for u in pages], "gone": [self._page(u) for u in gone]}

    def lab_looked(self, lab: str) -> dict:
        """The owner looked at a flagged lab against its pages as they are now: the lab is watched from these
        versions on. Only content is written; the Record is not."""
        spec = self.labs.get(lab)
        if not spec:
            raise HTTPException(404, "Lab not found.")
        with self.dojo.store.lock:
            doc = self._doc()
            if not self.lab_state(lab, doc):
                raise HTTPException(409, "This lab is not flagged for a look.")
            watch = doc["labs"].setdefault(lab, {})
            watch["pages"] = {**(watch.get("pages") or {}),
                              **{url: mark for url in self._lab_urls(spec) if (mark := self._mark(url))}}
            watch["looked"] = iso(self.now())
            self._save(doc)
        log.info("drift check: the owner looked at lab %s after its page changed", one_line(lab))
        return self.summary()

    def _lab_now(self, entry: dict, url: str, doc: dict) -> dict:
        state = self.lab_state(entry["id"], doc)
        if state and any(p["url"] == url for p in state["pages"]):
            return {**entry, "now": "flagged"}
        looked = (doc["labs"].get(entry["id"]) or {}).get("looked")
        return {**entry, "now": "looked" if looked else "clear", "looked": looked}

    def flagged_labs(self, doc: dict | None = None) -> list[dict]:
        doc = self._doc() if doc is None else doc
        return [{"id": lab, "title": spec.get("title"), "skill": spec.get("skill"), "date": v["date"]}
                for lab, spec in self.labs.items() if (v := self.lab_state(lab, doc))]

    def _page(self, url: str) -> dict:
        if not self._titles:
            self.cited()
        known = self._titles.get(url) or {}
        out = {"url": url, "title": known.get("title") or url, "publisher": known.get("publisher", "")}
        head = self.sources.head(url) or {}
        if head.get("gone") and head.get("moved"):
            out["moved"] = head["moved"]
        return out

    # ------------------------------------------------------------ sorting out what a change touched

    def _owner_items(self) -> list[dict]:
        """The owner's items for the exams Dojo has now: those of a removed exam stay in the record, untouched."""
        path = ("learners", self.owner.key, "items")
        live = set(self.dojo.packages.ids())
        out = []
        for name in self.dojo.store.names(*path):
            try:
                doc = self.dojo.store.read(*path, name)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("package") in live:
                out.append(doc)
        return out

    def _owner_pool(self) -> list[dict]:
        out = []
        for pid in self.dojo.packages.ids():
            try:
                doc = self.dojo.store.read("learners", self.owner.key, "pool", pid, default=None)
            except ValueError:
                doc = None
            out += [q for q in (doc or {}).get("items") or [] if isinstance(q, dict)]
        return out

    def _classify(self) -> int:
        """Look at every cited page whose stored version is new since the last look, and write down what
        the change touched. Nothing is fetched here, so a step cut off after its read is finished by the
        next one without reading again."""
        doc = self._doc()
        by = self._by_skill()
        cited = self.cited(by)
        if doc["replaced"]:
            self._tidy(cited)
        todo = []
        for url in cited:
            mark = self._mark(url)
            if mark is not None and (doc["pages"].get(url) or {}).get("classified") != mark:
                todo.append((url, mark, (doc["pages"].get(url) or {}).get("classified")))
        start = self._lab_starts(doc)
        if not todo and not start:
            return 0
        entries = []
        if todo:
            items, pool = self._owner_items(), self._owner_pool()
            entries = [e for url, mark, before in todo if (e := self._review(url, mark, before, items, pool, by))]
        with self.dojo.store.lock:
            doc = self._doc()
            for url, mark, _ in todo:
                doc["pages"].setdefault(url, {})["classified"] = mark
            for lab, pages in start.items():
                watch = doc["labs"].setdefault(lab, {})
                watch.setdefault("since", iso(self.now()))
                watch["pages"] = {**pages, **(watch.get("pages") or {})}
            at = {c.get("id"): k for k, c in enumerate(doc["changes"])}
            for e in entries:
                if e["id"] in at:
                    doc["changes"][at[e["id"]]] = e
                else:
                    doc["changes"].append(e)
            doc["changes"] = doc["changes"][-CHANGES_KEPT:]
            self._save(doc)
        return len(entries)

    def _lab_starts(self, doc: dict) -> dict[str, dict[str, str]]:
        """Pages a lab cites that Dojo has read but does not watch for that lab yet, at their version now:
        what the lab is compared with from here on."""
        out: dict[str, dict[str, str]] = {}
        for lab, spec in self.labs.items():
            watched = (doc["labs"].get(lab) or {}).get("pages") or {}
            for url in self._lab_urls(spec):
                if url not in watched and (mark := self._mark(url)):
                    out.setdefault(lab, {})[url] = mark
        return out

    def _touched(self, url: str, items: list[dict], pool: list[dict]) -> tuple[dict, dict, list[dict]]:
        """The owner's items and practice-exam questions written from an earlier version of a page, counted
        by what the page means for them now, with the verdicts behind the counts."""
        counts, in_pool, verdicts = {"holds": 0, "withheld": 0, "answered": 0}, {"holds": 0, "withheld": 0}, []
        for it in items:
            w = self._item_written(it).get(url)
            v = self._judge_url(url, w) if w else None
            if v:
                verdicts.append(v)
                counts["holds" if v["state"] == "holds" else "answered" if it.get("answer") else "withheld"] += 1
        for q in pool:
            _, w = self._question_written(q)
            v = self._judge_url(url, w[url]) if url in w else None
            if v:
                verdicts.append(v)
                in_pool["holds" if v["state"] == "holds" else "withheld"] += 1
        return counts, in_pool, verdicts

    def _review(self, url: str, mark: str, before: str | None, items: list[dict], pool: list[dict], by: dict) -> dict | None:
        """A change as it is written down: the page, the kind of change, when, and the lessons it touched.
        Only content: what it meant for the learner's own items is counted afresh when it is shown (`_now`),
        so a deleted record leaves nothing behind here."""
        head = self.sources.head(url) or {}
        lessons, sinces = [], []
        for pid in self.dojo.packages.ids():
            for sid, meta in sorted(self._latest(pid, by).items()):
                w = self._lesson_written(meta["id"]).get(url)
                v = self._judge_url(url, w) if w else None
                if not v:
                    continue
                sinces.append(v["since"])
                lessons.append({"id": meta["id"], "title": meta.get("title"), "package": pid, "skill": sid, "state": v["state"],
                                "filmed": v["state"] == "recheck" and self._any_filmed(meta["id"])})
        counts, in_pool, verdicts = self._touched(url, items, pool)
        sinces += [v["since"] for v in verdicts]
        labs = [{"id": lab, "title": spec.get("title"), "skill": spec.get("skill")}
                for lab, spec in self.labs.items() if url in self._lab_urls(spec)]
        prev = before.split(":", 1)[1] if before and before.startswith("gone:") else before
        if head.get("gone"):
            kind, since = ("moved" if head.get("moved") else "gone"), head["gone"]
        elif prev:
            c = self.sources.compare(url, prev)
            if c["state"] == "same":
                return None   # the page is back as it was: the verdicts follow by themselves
            kind, since = ("formatting" if c["state"] == "formatting" else "changed"), c["since"]
            if kind == "formatting":
                lessons, labs = [], []
        elif lessons or verdicts:
            kind, since = "changed", min((s for s in sinces if s), default=head.get("first_seen"))
        else:
            return None   # the first look at this page, and nothing written from it has changed
        log.info("drift check: %s page; lessons: %d re-check, %d hold; items held back: %d; pool questions held back: %d; labs: %d",
                 kind, sum(1 for x in lessons if x["state"] == "recheck"), sum(1 for x in lessons if x["state"] == "holds"),
                 0 if kind == "formatting" else counts["withheld"], 0 if kind == "formatting" else in_pool["withheld"], len(labs))
        return {"id": sha256(f"{url}|{mark}")[:16], "url": url, **{k: v for k, v in self._page(url).items() if k != "url"},
                "kind": kind, "since": since, "seen": iso(self.now()), "lessons": lessons, "labs": labs}

    def _now(self, c: dict, doc: dict, by: dict, items: list[dict], pool: list[dict], gone: dict[str, set[str]]) -> dict:
        """A change as it stands now: what became of its lessons and labs, the quotes no longer on the page,
        and the owner's items and practice-exam questions that cite it."""
        url = c["url"]
        verdicts = []
        for x in c.get("lessons") or []:
            w = self._lesson_written(x["id"]).get(url)
            v = self._judge_url(url, w) if w else None
            if v:
                verdicts.append(v)
        counts, in_pool, theirs = self._touched(url, items, pool)
        removed = list(dict.fromkeys(q for v in verdicts + theirs for q in v["removed"]))
        live = set(self.dojo.packages.ids())
        return {**{k: v for k, v in c.items() if k not in ("removed", "removed_total", "items", "pool")},
                "date": _day(c.get("since")), "removed": removed[:QUOTES_SHOWN], "removed_total": len(removed),
                "items": counts, "pool": in_pool,
                "lessons": [self._lesson_now(x, doc, by, gone) for x in c.get("lessons") or [] if x.get("package") in live],
                "labs": [self._lab_now(x, url, doc) for x in c.get("labs") or [] if x.get("id") in self.labs]}

    # ------------------------------------------------------------ filming again, only when the owner says so

    def _film_plan(self, lesson_id: str) -> dict:
        plan = self._plans.get(lesson_id)
        if plan is None:
            try:
                slides = self.dojo.lesson(lesson_id).get("slides", [])
            except HTTPException:
                return {"films": {}, "chars": 0}
            per_speaker, _ = lesson_turns([self.dojo._slide_lines(sl) for sl in slides])
            plan = self._plans[lesson_id] = {
                "films": {sp: media_id_for(sp, turns) for sp, turns in per_speaker.items()},
                "chars": sum(len(t) for turns in per_speaker.values() for turn in turns for t in turn)}
        return plan

    def _filmed(self, lesson_id: str) -> bool:
        films = self._film_plan(lesson_id)["films"]
        return bool(films) and all(self.dojo.avatar.exists(m) for m in films.values())

    def _any_filmed(self, lesson_id: str) -> bool:
        return any(self.dojo.avatar.exists(m) for m in self._film_plan(lesson_id)["films"].values())

    def _was_filmed(self, lesson_id: str, doc: dict) -> bool:
        """Whether a lesson had a video: one is still stored, or one was when the change was found."""
        if self._any_filmed(lesson_id):
            return True
        return any(x.get("id") == lesson_id and x.get("filmed") for c in doc["changes"] for x in c.get("lessons") or [])

    def _film_note(self, lesson_id: str, old_id: str, doc: dict | None = None) -> dict | None:
        doc = doc or self._doc()
        if not self._was_filmed(old_id, doc):
            return None
        if self._filmed(lesson_id):
            return {"state": "done", "text": ""}
        film = doc["films"].get(lesson_id) or {}
        if film.get("state") in ("approved", "filed"):
            return {"state": "remaking", "text": FILM_REMAKING}
        return {"state": "waiting", "text": FILM_WAITING, "error": film.get("error") if film.get("state") == "failed" else None}

    def _replaced(self, meta: dict, by: dict, doc: dict) -> tuple[dict, str] | None:
        """The filmed lesson this one replaced, and why: "changed" when it was written again because a page
        changed, "deeper" when it was written again on the deeper template (ADR 0008), following those back
        to the last version that was filmed. None when no earlier version was filmed."""
        prev = self._rewrite_of(meta, by)
        if prev:
            return (prev, "changed") if self._was_filmed(prev["id"], doc) else None
        seen = {meta["id"]}
        while isinstance(meta.get("upgrade_of"), str) and meta["upgrade_of"] not in seen and len(seen) <= 5:
            seen.add(meta["upgrade_of"])
            meta = self._meta(meta["upgrade_of"], by)
            if not meta:
                return None
            if self._was_filmed(meta["id"], doc):
                return meta, "deeper"
        return None

    def film_state(self, lesson_id: str) -> str | None:
        """Where the owner's approval to film this lesson again stands: approved, filed, done, failed or None."""
        return (self._doc()["films"].get(lesson_id) or {}).get("state")

    def refilm_candidates(self, doc: dict | None = None, by: dict | None = None) -> list[dict]:
        """Lessons written again, after a page changed or on the deeper template, whose earlier version was
        filmed, that have no video of their own and are not already approved: what the Studio button would
        film."""
        doc = doc or self._doc()
        by = self._by_skill() if by is None else by
        out = []
        for pid in self.dojo.packages.ids():
            for sid, meta in sorted(self._latest(pid, by).items()):
                if self.withheld(meta["id"]):
                    continue
                found = self._replaced(meta, by, doc)
                if not found or self._filmed(meta["id"]):
                    continue
                prev, why = found
                film = doc["films"].get(meta["id"]) or {}
                plan = self._film_plan(meta["id"])
                if film.get("state") in ("approved", "filed") or not plan["films"]:
                    continue
                out.append({"id": meta["id"], "title": meta.get("title"), "package": pid, "skill": sid,
                            "replaces": prev["id"], "why": why, "characters": plan["chars"],
                            "error": film.get("error") if film.get("state") == "failed" else None})
        return out

    def estimate(self, lessons: list[dict]) -> dict:
        """What filming these lessons costs at list price: the video by the second and the presenters'
        voices by the character. The length is worked out from the narration at the pace Dojo's own
        videos show, or CHARS_PER_SECOND before there are any."""
        chars = sum(int(x.get("characters") or 0) for x in lessons)
        if not chars:
            return {"lessons": len(lessons), "characters": 0, "minutes": 0.0, "usd": 0.0, "voice_priced": True,
                    "pace": "assumed", "label": PRICE_LABEL, "fetched": None, "stale": False}
        usage = (self.costs.usage() if self.costs else None) or {}
        measured = bool(usage.get("video_seconds") and usage.get("watch_characters"))
        pace = usage["watch_characters"] / usage["video_seconds"] if measured else CHARS_PER_SECOND
        seconds = chars / pace if pace > 0 else 0.0
        prices = (self.costs.prices() if self.costs else None) or {}
        meters = prices.get("meters") or {}
        video, voice = meters.get("video"), meters.get("voice")
        usd = None
        if video:
            usd = seconds * float(video["per_second"]) + (chars * float(voice["per_character"]) if voice else 0.0)
        # When the list prices were read, and whether a refresh failed, as every other total says (ADR 0005).
        return {"lessons": len(lessons), "characters": chars, "minutes": round(seconds / 60, 1),
                "usd": None if usd is None else round(usd, 2), "voice_priced": bool(voice),
                "pace": "measured" if measured else "assumed", "label": PRICE_LABEL,
                "fetched": prices.get("fetched"), "stale": bool(prices.get("stale"))}

    def approve(self, lesson_ids: list[str]) -> dict:
        """The owner pressed the button: film these lessons, each once, one at a time, through the
        ordinary video job. Only lessons still waiting count."""
        waiting = {x["id"]: x for x in self.refilm_candidates()}
        chosen = [x for x in dict.fromkeys(lesson_ids or []) if x in waiting]
        if not chosen:
            raise HTTPException(409, "No changed lesson is waiting to be filmed again.")
        est = self.estimate([waiting[x] for x in chosen])
        now = iso(self.now())
        with self.dojo.store.lock:
            doc = self._doc()
            for x in chosen:
                c = waiting[x]
                doc["films"][x] = {"state": "approved", "approved": now, "title": c["title"], "package": c["package"],
                                   "skill": c["skill"], "replaces": c["replaces"], "job": None, "tries": 0, "error": None}
            doc["approvals"] = (doc["approvals"] + [{"at": now, "lessons": chosen, "usd": est["usd"],
                                                     "minutes": est["minutes"]}])[-20:]
            self._save(doc)
        log.info("drift check: the owner approved filming %d lesson(s) again", len(chosen))
        self._file_films()
        self._films_soon()
        return self.summary()

    def _set_film(self, lesson_id: str, **fields: Any) -> None:
        with self.dojo.store.lock:
            doc = self._doc()
            doc["films"].setdefault(lesson_id, {}).update(fields)
            self._save(doc)

    def _job(self, job_id: str | None) -> dict | None:
        if not job_id:
            return None
        try:
            job = self.dojo.store.read("jobs", job_id)
        except ValueError:
            return None
        return job if isinstance(job, dict) else None

    def _owner_busy(self) -> bool:
        return any(j.get("state") in ("queued", "running") for j in self.dojo.jobs.for_learner(self.owner))

    def _settle_films(self) -> None:
        for lesson_id, film in list(self._doc()["films"].items()):
            state = film.get("state")
            if state == "approved" and self._filmed(lesson_id):
                self._set_film(lesson_id, state="done", finished=iso(self.now()), error=None)
            elif state == "filed":
                job = self._job(film.get("job"))
                js = (job or {}).get("state")
                if js in ("queued", "running"):
                    continue
                if js == "done":
                    self._set_film(lesson_id, state="done", finished=iso(self.now()), error=None)
                elif js == "interrupted" and int(film.get("tries") or 0) < FILM_TRIES:
                    self._set_film(lesson_id, state="approved", job=None)
                else:
                    self._set_film(lesson_id, state="failed", error=(job or {}).get("error") or "The film job was not found.")

    def _file_films(self) -> bool:
        """Note which film jobs finished, then file the next approved film when none of ours is running
        and the owner has nothing running. True while approved films are left."""
        with self._films:
            self._settle_films()
            films = self._doc()["films"]
            waiting = sorted((f.get("approved") or "", k) for k, f in films.items() if f.get("state") == "approved")
            if waiting and not any(f.get("state") == "filed" for f in films.values()) and not self._owner_busy():
                lesson_id = waiting[0][1]
                try:
                    self.dojo.lesson(lesson_id)
                    job = (self.dojo.jobs.existing(self.owner, "video", lesson_id)
                           or self.dojo.jobs.start(self.owner, "video", self.dojo.lesson_video, self.owner, lesson_id, ref=lesson_id))
                except HTTPException as e:
                    if e.status_code != 429:
                        self._set_film(lesson_id, state="failed", error=str(e.detail))
                else:
                    self._set_film(lesson_id, state="filed", job=job["id"], filed=iso(self.now()),
                                   tries=int(films[lesson_id].get("tries") or 0) + 1)
                    log.info("drift check: filming lesson %s again, as approved", lesson_id)
            return any(f.get("state") in ("approved", "filed") for f in self._doc()["films"].values())

    def _films_soon(self) -> None:
        """Keep approved films moving in a thread of their own: a film takes minutes."""
        if not any(f.get("state") in ("approved", "filed") for f in self._doc()["films"].values()):
            return
        with self._films:
            if self._film_thread and self._film_thread.is_alive():
                return
            self._film_thread = threading.Thread(target=self._film_loop, name="drift-films", daemon=True)
            self._film_thread.start()

    def _film_loop(self) -> None:
        while True:
            time.sleep(FILM_WAIT_S)
            try:
                if not self._file_films():
                    return
            except Exception:  # noqa: BLE001 - try again on the next round
                log.exception("drift check: filing a film failed")

    # ------------------------------------------------------------ what Studio and diag show

    def _lesson_now(self, entry: dict, doc: dict, by: dict, gone: dict[str, set[str]]) -> dict:
        out = dict(entry)
        if not self.withheld(entry["id"]):
            out["now"] = "holds"
            return out
        latest = self._latest(entry["package"], by).get(entry["skill"])
        if latest and latest["id"] != entry["id"] and not self.withheld(latest["id"]):
            out.update(now="rewritten", by=latest["id"], by_title=latest.get("title"),
                       film=self._film_note(latest["id"], entry["id"], doc))
        elif entry["skill"] in self._gone_memo(entry["package"], gone):
            out.update(now="waiting", why=PAGES_GONE)
        else:
            out["now"] = "rechecking"
        return out

    def _gone_memo(self, pid: str, memo: dict[str, set[str]]) -> set[str]:
        """`gone_skills`, worked out once per Studio or diag view."""
        if pid not in memo:
            memo[pid] = self.gone_skills(pid)
        return memo[pid]

    def _films_view(self, doc: dict) -> list[dict]:
        rows = sorted(doc["films"].items(), key=lambda kv: kv[1].get("approved") or "", reverse=True)[:12]
        return [{"id": k, "title": f.get("title"), "package": f.get("package"), "skill": f.get("skill"),
                 "state": f.get("state"), "approved": f.get("approved"), "finished": f.get("finished"),
                 "error": f.get("error")} for k, f in rows]

    def _held(self, by: dict, gone: dict[str, set[str]]) -> tuple[int, int]:
        """The skills whose newest lesson is held back: how many wait for a rewrite, and how many wait for
        a page instead, as every page they cite is gone. Those use no rewrite (`Preparer.rewrites`)."""
        rewrite = pages = 0
        for pid in self.dojo.packages.ids():
            held = {sid for sid, m in self._latest(pid, by).items() if self.withheld(m["id"])}
            stuck = held & self._gone_memo(pid, gone) if held else set()
            rewrite, pages = rewrite + len(held - stuck), pages + len(stuck)
        return rewrite, pages

    @staticmethod
    def _counts(heads: list[dict | None]) -> dict[str, int]:
        """The cited pages Dojo has a copy of that it may use; those whose copy it may not use (stored before
        the checks on where a read ended, or read from another page), read again in turn; and those gone,
        moved to another page included."""
        return {"known": sum(1 for h in heads if h and h["verified"]),
                "unchecked": sum(1 for h in heads if h and not h["verified"] and not h.get("gone")),
                "gone": sum(1 for h in heads if h and h.get("gone")),
                "moved": sum(1 for h in heads if h and h.get("gone") and h.get("moved"))}

    def summary(self) -> dict:
        """Studio: the pages read, the changes found and what they touched, and the films to approve."""
        self._films_soon()
        doc, now, by = self._doc(), self.now(), self._by_skill()
        cited = self.cited(by)
        heads = [self.sources.head(u) for u in cited]
        changes = [c for c in reversed(doc["changes"]) if c.get("kind") != "formatting"]
        items, pool = (self._owner_items(), self._owner_pool()) if changes else ([], [])
        gone: dict[str, set[str]] = {}
        changes = [self._now(c, doc, by, items, pool, gone) for c in changes]
        formatting = [{"url": c["url"], "title": c.get("title"), "date": _day(c.get("since"))}
                      for c in reversed(doc["changes"]) if c.get("kind") == "formatting"][:8]
        cands = self.refilm_candidates(doc, by)
        rewrite, for_pages = self._held(by, gone)
        return {
            "record": RECORD_NOTE, "started": self._started,
            "limits": {"per_day": DAILY_FETCHES, "gap_minutes": FETCH_GAP_S // 60, "every_hours": RECHECK_HOURS,
                       "gone_after_hours": GONE_AFTER_HOURS, "rewrites_per_day": DAILY_REWRITES},
            "pages": {"cited": len(cited), **self._counts(heads),
                      "read_today": doc["read"], "last_read": doc.get("last_fetch"),
                      "waiting": sorted(h for h in doc["hosts"] if self._host_waits(doc, h, now))},
            "status": {k: self.state[k] for k in ("running", "current", "last", "note")},
            "rechecking": rewrite + for_pages, "rewrites": self._rewrites(doc, rewrite, for_pages),
            "labs": self.flagged_labs(doc),
            "changes": changes, "formatting": formatting,
            "refilm": {"lessons": cands, "estimate": self.estimate(cands), "films": self._films_view(doc),
                       "approvals": list(reversed(doc["approvals"]))[:5]},
        }

    def status(self) -> dict:
        """For /api/diag, like the delivery check's block."""
        doc, now, by = self._doc(), self.now(), self._by_skill()
        cited = self.cited(by)
        counts = self._counts([self.sources.head(u) for u in cited])
        films: dict[str, int] = {}
        for f in doc["films"].values():
            films[f.get("state") or "?"] = films.get(f.get("state") or "?", 0) + 1
        rewrite, for_pages = self._held(by, {})
        return {"started": self._started, "running": self.state["running"], "current": self.state["current"],
                "last": self.state["last"], "note": self.state["note"], "daily_limit": DAILY_FETCHES,
                "gap_seconds": FETCH_GAP_S, "read_today": doc["read"], "read_since_start": self.state["read"],
                "last_read": doc.get("last_fetch"), "pages_cited": len(cited), **{f"pages_{k}": v for k, v in counts.items()},
                "hosts_waiting": {h: u for h, u in doc["hosts"].items() if self._host_waits(doc, h, now)},
                "changes": len(doc["changes"]), "lessons_rechecking": rewrite + for_pages,
                "rewrites": self._rewrites(doc, rewrite, for_pages),
                "labs_flagged": len(self.flagged_labs(doc)), "films": films}
