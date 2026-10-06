"""Add an exam (DESIGN section 1 and screen 12; EG-45, EG-37, EG-39, EG-14).

The owner types an exam code. Dojo reads the official study guide on Microsoft Learn without a model
(app/studyguide.py): the domains and their weights, the groups and skills, the date the skills are
measured as of, the certification page and its official practice assessment.

Building the course then looks, for every skill, for up to three official documentation pages that
teach it (app/pagefinder.py, shared with Deeper lessons, ADR 0008). The sites' own search proposes pages,
the author model picks the likeliest, and the checker
model (gate) must show that each page teaches the skill with a sentence copied word for word from the
page. Dojo finds that sentence on the page itself before it keeps the page. A skill no page passed for
stays in the package without a source: Dojo does not teach it or ask about it, and it counts as not
shown. A model's answer is never a verified claim on its own (EG-14).

Everything read from the web is untrusted (EG-37). It reaches a model only inside <data>, with its
angle brackets neutralised, under a system prompt that says it is material and never instruction. A
model can only point at pages Dojo found, by number; it cannot add an address.

The package has exactly the built-in schema (app/packages.py) and is kept on the data share. The
existing background preparation (app/prepare.py) then writes and narrates its lessons. Filming costs
far more, so it never starts from here on its own: the owner approves a priced set of lessons first."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import HTTPException

from .ai import AIError
from .core import Abandoned, Learner, UserError, iso, sha256, utcnow
from .exam import blocker as exam_blocker
from .pagefinder import MAX_SOURCES, PageFinder, github_hits, groundable, label, learn_hits, learn_search  # noqa: F401
from .sources import normalize
from .studyguide import (LEARN, after, cert_slug, exam_url, guide_url, issuer, normalize_code, parse_cert_page,
                         parse_guide, practice_for, resolve)

log = logging.getLogger("dojo.newexam")

LOOKUP_CACHE_S = 900
SUPPORT_URL = LEARN + "/en-us/credentials/support/exam-duration-exam-experience"
COUNT_NOTE = ("Microsoft Learn does not publish how many questions {code} has. Its support page says only that most "
              "Microsoft exams contain between 40 and 60 questions and that the number varies per exam.")
GUIDE_LINKS = 12       # documentation links from the study guide offered to every group
ERROR_SHARE = 0.1      # more skills than this whose check could not run stop the build, to be resumed
BUILD = "exam-build"


def skill_key(domain: str, group: str, text: str) -> str:
    return sha256(f"{normalize(domain)}|{normalize(group)}|{normalize(text)}")[:16]


def skill_id(i: int, j: int, k: int, text: str) -> str:
    """Position plus a short hash of the words, so evidence recorded against a skill never attaches to
    a different skill after the exam is removed and added again with a changed blueprint."""
    return f"d{i}.g{j}.s{k}.{sha256(normalize(text))[:6]}"


def _same_page(final: str, wanted: str) -> bool:
    return urlparse(final).path.rstrip("/").lower() == urlparse(wanted).path.rstrip("/").lower()


def day_text(value: Any) -> str:
    """'2026-06-30' as 'June 30, 2026' for messages; anything else as it is."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value or "")
    return f"{d:%B} {d.day}, {d.year}"


class NewExams:
    """Finding, building, filming and removing the exams the owner adds."""

    def __init__(self, dojo: Any, room: Any, pool_keeper: Any, podcast: Any, checker: Any, costs: Any,
                 preparer: Any, owner: Learner):
        self.dojo = dojo
        self.packages = dojo.packages
        self.sources = dojo.sources
        self.store = dojo.store
        self.jobs = dojo.jobs
        self.room = room
        self.pool_keeper = pool_keeper
        self.podcast = podcast
        self.checker = checker
        self.costs = costs
        self.preparer = preparer
        self.owner = owner
        self.finder = PageFinder(dojo)
        self._lookups: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()   # build documents are read, changed and written one at a time

    # ------------------------------------------------------------ the build document

    def _doc(self, pid: str) -> dict | None:
        return self.store.read("package-builds", pid, default=None)

    def _change(self, pid: str, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            doc = self._doc(pid) or {"package": pid}
            fn(doc)
            self.store.write("package-builds", pid, value=doc)
            return doc

    def _build_job(self, pid: str) -> dict | None:
        return self.jobs.existing(self.owner, BUILD, pid)

    # ------------------------------------------------------------ finding the study guide

    def lookup(self, raw: str, fresh: bool = False) -> dict:
        code = normalize_code(raw)
        pid = code.lower()
        if pid in self.packages.by_id:
            meta = self.packages.get(pid)["meta"]
            builtin = pid in self.packages.builtin
            return {"code": code, "package": pid, "state": "added", "builtin": builtin, "title": meta["title"],
                    "message": f"{code} is built into Dojo already." if builtin
                    else f"{code} is in Dojo already. You added it on {day_text(meta.get('added'))}."}
        if self._build_job(pid):
            return {"code": code, "package": pid, "state": "building",
                    "message": f"Dojo is building the course for {code} now."}
        found = self._found(code, fresh)
        out = {k: v for k, v in found.items() if not k.startswith("_")}
        if out["state"] == "found":
            # Priced on every request, not kept with the study guide: it says how old its prices are now.
            out["estimate"] = self.costs.course_estimate(out["skills"])
        doc = self._doc(pid)
        if doc and out["state"] == "found":
            # An earlier build stopped: building again keeps every page already checked.
            out["resume"] = {"checked": doc.get("checked") or 0, "skills": doc.get("skills"), "error": doc.get("error")}
        return out

    def _found(self, code: str, fresh: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._lookups.get(code)
        if hit and not fresh and now - hit[0] < LOOKUP_CACHE_S:
            return hit[1]
        found = self._find(code)
        with self._lock:
            self._lookups[code] = (now, found)
            for old in sorted(self._lookups, key=lambda c: self._lookups[c][0])[:-40]:
                self._lookups.pop(old, None)
        return found

    def _find(self, code: str) -> dict:
        today = utcnow().date()
        url = guide_url(code)
        base = {"code": code, "package": code.lower(), "issuer": issuer(code), "guide_url": url}
        try:
            final, page = self.sources.page(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                return {**base, "state": "missing",
                        "message": f"Microsoft Learn has no study guide for {code}. Check the code. Exams without a "
                                   "study guide, and codes that are not exams, cannot be added."}
            return {**base, "state": "unreadable",
                    "message": f"Microsoft Learn answered HTTP {e.response.status_code} for the study guide. Try again later."}
        except (httpx.HTTPError, UserError):
            return {**base, "state": "unreadable",
                    "message": "Dojo could not reach Microsoft Learn to read the study guide. Try again later."}
        if not _same_page(final, url):
            return {**base, "state": "missing",
                    "message": f"Microsoft Learn has no study guide for {code}: its address leads to another page."}
        guide = parse_guide(page, code, url, today)
        if guide["problems"]:
            return {**base, "state": "unparsed", "problems": guide["problems"][:5],
                    "message": f"Dojo found a study guide for {code} but could not read its skills measured "
                               "reliably, so it will not build a course from it."}
        retired = guide["retirement"]
        if retired and retired["past"]:
            return {**base, "state": "retired", "title": guide["title"], "retirement": retired,
                    "message": f"{code} was retired on {day_text(retired['date'])}. Dojo does not build courses for retired exams."}
        cert, cert_notes = self._cert(code, guide)
        practice, practice_notes = self._practice(code, guide, cert, today.isoformat())
        change = guide["change_log"] or {}
        training = [link for row in guide["resources"] for link in row["links"]
                    if urlparse(link["url"]).path.startswith("/en-us/training/")][:4]
        return {
            **base, "state": "found", "title": guide["title"], "name": guide["name"],
            "message": f"Dojo found the study guide for {code}: {guide['title']}.",
            "guide": {"url": url, "as_of": guide["as_of"], "as_of_text": guide["as_of_text"],
                      "forthcoming": after(guide["as_of"], today), "changed": change.get("changed"),
                      "changes": len(change.get("rows") or []), "retrieved": today.isoformat()},
            "domains": [{"title": d["title"], "weight_min": d["weight_min"], "weight_max": d["weight_max"],
                         "groups": len(d["groups"]), "skills": sum(len(g["skills"]) for g in d["groups"])}
                        for d in guide["domains"]],
            "skills": guide["skills"], "groups": guide["groups"], "passing_score": guide["passing_score"],
            "retirement": retired,
            "cert": {"url": cert["url"], "name": cert["name"], "duration_minutes": cert["duration_minutes"],
                     "update": cert["update"], "update_quote": cert["update_quote"]} if cert else None,
            "practice": {"title": practice["title"], "url": practice["url"]} if practice else None,
            "training": [{"url": x["url"], "title": label(x["title"], 120)} for x in training],
            "notes": guide["notes"] + cert_notes + practice_notes,
            "_guide": guide, "_cert": cert, "_practice": practice,
        }

    def _cert(self, code: str, guide: dict) -> tuple[dict | None, list[str]]:
        """The certification page that names the exam: the study guide's own link, then the exam's
        address (Learn redirects it to the certification), then Learn's search for credentials."""
        tried: list[str] = []

        def read(link: str) -> dict | None:
            if link in tried or len(tried) >= 6:
                return None
            tried.append(link)
            try:
                final, page = self.sources.page(link)
            except (httpx.HTTPError, UserError):
                return None
            final = final.split("#")[0].split("?")[0]
            if not cert_slug(final):
                return None
            cert = parse_cert_page(page, final, code)
            return cert if cert["mentions_code"] else None

        for link in guide["cert_links"][:3] + [exam_url(code)]:
            cert = read(link)
            if cert:
                return cert, []
        try:
            answer = self.sources.data(learn_search(f"{code} {guide['title']}", 5, "Credential"))
        except (httpx.HTTPError, UserError):
            answer = {}
        for row in (answer.get("results") if isinstance(answer, dict) else None) or []:
            link = resolve(str(row.get("url") or ""), LEARN) if isinstance(row, dict) else None
            if link and cert_slug(link.split("?")[0]):
                cert = read(link.split("?")[0])
                if cert:
                    return cert, []
        return None, [f"Dojo found no certification page on Microsoft Learn that names {code}, so it records no exam "
                      "duration and no practice assessment from one."]

    def _practice(self, code: str, guide: dict, cert: dict | None, today: str) -> tuple[dict | None, list[str]]:
        """The official practice assessment, linked only when its address names this exam or its
        certification and the page opens."""
        offers = ([(cert["practice"]["url"], cert["url"])] if cert and cert.get("practice") else [])
        offers += [(link, guide["url"]) for link in guide["practice_links"]]
        notes: list[str] = []
        for link, where in offers:
            if not practice_for(link, code, cert["slug"] if cert else None):
                notes.append(f"The practice assessment linked on {where} does not name {code} or its certification, "
                             f"so Dojo does not link it: {link}")
                continue
            try:
                self.sources.page(link)
            except (httpx.HTTPError, UserError):
                notes.append(f"The practice assessment {link} did not open when Dojo checked it, so Dojo does not link it.")
                continue
            ids = parse_qs(urlparse(link).query).get("assessmentId") or [""]
            return {"title": f"Practice Assessment for {code}: {guide['title']}", "url": link,
                    "assessment_id": re.sub(r"[^0-9A-Za-z-]", "", ids[0])[:20] or None, "found_on": where,
                    "checked": today}, notes
        if not offers:
            notes.append(f"Neither the study guide nor the certification page links an official practice assessment for {code}.")
        return None, notes

    # ------------------------------------------------------------ building the package

    def start_build(self, learner: Learner, raw: str, exam_date: str | None = None) -> dict:
        code = normalize_code(raw)
        pid = code.lower()
        if pid in self.packages.by_id:
            raise HTTPException(409, f"{code} is in Dojo already.")
        if exam_date:
            try:
                exam_date = date.fromisoformat(exam_date).isoformat()
            except ValueError:
                raise HTTPException(400, "Exam dates must look like 2027-03-01.") from None
        running = self._build_job(pid)
        if running:
            return {"package": pid, "job": running}
        found = self._found(code)
        if found["state"] != "found":
            raise UserError(found["message"])

        def begin(doc: dict) -> None:
            doc.update(package=pid, code=code, title=found["title"], state="matching", started=iso(), finished=None,
                       error=None, exam_date=exam_date or doc.get("exam_date"), skills=found["skills"])
            doc.setdefault("matches", {})
        with self._lock:
            running = self._build_job(pid)
            if running:
                return {"package": pid, "job": running}
            self._change(pid, begin)
            job = self.jobs.start(learner, BUILD, self._build, learner, code, ref=pid)
            self._change(pid, lambda d: d.update(job=job["id"]))
        return {"package": pid, "job": job}

    def _build(self, progress: Callable[[str], None], learner: Learner, code: str) -> dict:
        pid = code.lower()
        try:
            return self._build_steps(progress, learner, code, pid)
        except Exception as e:
            reason = str(e) if isinstance(e, (UserError, AIError)) else f"{type(e).__name__}: {str(e)[:300]}"
            self._change(pid, lambda d: d.update(state="failed", error=reason, finished=iso()))
            raise

    def _build_steps(self, progress: Callable[[str], None], learner: Learner, code: str, pid: str) -> dict:
        progress("Reading the study guide again")
        found = self._find(code)
        if found["state"] != "found":
            raise UserError(found["message"])
        guide = found["_guide"]
        matches: dict[str, dict] = dict((self._doc(pid) or {}).get("matches") or {})
        # Pages the study guide itself links to may teach any skill; they are offered to every group.
        offered = [x for x in ({"url": groundable(link["url"]), "title": label(link["title"]), "summary": "Linked from the study guide."}
                               for row in guide["resources"] for link in row["links"]) if x["url"]]
        offered = list({x["url"]: x for x in offered}.values())[:GUIDE_LINKS]
        work = [(d, g) for d in guide["domains"] for g in d["groups"]]
        total = guide["skills"]
        for n, (d, g) in enumerate(work, 1):
            keys = [skill_key(d["title"], g["title"], t) for t in g["skills"]]
            pending = [(k, t) for k, t in zip(keys, g["skills"]) if k not in matches or matches[k].get("error")]
            if not pending:
                continue
            done = self._counts(guide, matches)["checked"]
            progress(f"Checking official pages: {done} of {total} skills done (group {n} of {len(work)})")
            matches.update(self._match_group(found, d, g, pending, offered))
            self._change(pid, lambda doc: doc.update(matches=matches, **self._counts(guide, matches)))
        failed = [matches[k] for k in self._keys(guide) if k in matches and matches[k].get("error")]
        if len(failed) > max(1, ERROR_SHARE * total):
            raise UserError(f"The checks could not run for {len(failed)} of {total} skills (last problem: "
                            f"{failed[-1]['error']}). The pages already checked are kept: press Build again to continue.")
        if not self._counts(guide, matches)["sourced"]:
            # Something is wrong with the checks rather than with every page: start again from nothing.
            self._change(pid, lambda doc: doc.update(matches={}, checked=0, sourced=0, errors=0))
            raise UserError(f"No official page passed the check for any of the {total} skills, so Dojo did not add "
                            f"{code}. Press Build again to check every skill again.")
        progress("Putting the course together")
        pkg = self._assemble(found, matches)
        self.packages.add(pkg)
        sourced = sum(1 for d in pkg["domains"] for g in d["groups"] for s in g["skills"] if s["sources"])
        doc = self._change(pid, lambda doc: doc.update(state="added", finished=iso(), error=None,
                                                       **self._counts(guide, matches)))
        self.store.append_event(learner.key, "package.added", {
            "package": pid, "exam": code, "version": pkg["version"], "study_guide": pkg["source"]["url"],
            "skills": total, "sourced": sourced})
        if doc.get("exam_date"):
            self.dojo.save_profile(learner, {"exam_dates": {pid: doc["exam_date"]}})
        with self._lock:
            self._lookups.pop(code, None)
        self.preparer.wake()
        self.pool_keeper.nudge(learner)   # the owner studies the new exam: its pool is looked at now
        return {"package": pid, "skills": total, "sourced": sourced}

    @staticmethod
    def _keys(guide: dict) -> list[str]:
        return [skill_key(d["title"], g["title"], t) for d in guide["domains"] for g in d["groups"] for t in g["skills"]]

    @classmethod
    def _counts(cls, guide: dict, matches: dict) -> dict:
        keys = set(cls._keys(guide))
        mine = [matches[k] for k in keys if k in matches]
        return {"skills": len(keys), "checked": sum(1 for m in mine if not m.get("error")),
                "sourced": sum(1 for m in mine if m.get("sources")), "errors": sum(1 for m in mine if m.get("error"))}

    # Searching, picking and checking pages lives in app/pagefinder.py, shared with Deeper lessons (ADR 0008).

    def _search(self, code: str, domain: dict, group: dict, text: str) -> tuple[list[dict], str | None]:
        return self.finder.search(code, domain, group, text)

    def _read(self, url: str) -> dict | None:
        return self.finder.read(url)

    def _match_group(self, found: dict, domain: dict, group: dict, pending: list[tuple[str, str]],
                     offered: list[dict]) -> dict[str, dict]:
        return self.finder.match_group(found, domain, group, pending, offered)

    def _pick_prompt(self, found: dict, domain: dict, group: dict, pending: list[tuple[str, str]], cands: list[dict]) -> str:
        return self.finder.pick_prompt(found, domain, group, pending, cands)

    def _verify(self, found: dict, domain: dict, group: dict, text: str, batch: list[dict], snaps: dict) -> list[dict]:
        return self.finder.verify(found, domain, group, text, batch, snaps)

    def _assemble(self, found: dict, matches: dict) -> dict:
        guide, cert, practice = found["_guide"], found["_cert"], found["_practice"]
        code, today = found["code"], utcnow().date().isoformat()
        domains = []
        for i, d in enumerate(guide["domains"], 1):
            groups = []
            for j, g in enumerate(d["groups"], 1):
                skills = []
                for k, text in enumerate(g["skills"], 1):
                    m = matches.get(skill_key(d["title"], g["title"], text)) or {}
                    skills.append({"id": skill_id(i, j, k, text), "text": text,
                                   "sources": (m.get("sources") or [])[:MAX_SOURCES], "learn": []})
                groups.append({"title": g["title"], "skills": skills})
            domains.append({"id": f"d{i}", "title": d["title"], "weight_min": d["weight_min"],
                            "weight_max": d["weight_max"], "groups": groups})
        skills = [s for d in domains for g in d["groups"] for s in g["skills"]]
        unsourced = sum(1 for s in skills if not s["sources"])
        notes = list(found["notes"])
        if found["guide"]["forthcoming"]:
            notes.append(f"The study guide lists the skills measured as of {guide['as_of']}, which is after {today}. "
                         "Until then the live exam may still follow the earlier skills; the study guide's change log "
                         "shows what changes.")
        if not guide["as_of"]:
            notes.append(f"The study guide does not say from which date these skills are measured. Dojo read it on {today}.")
        if unsourced:
            notes.append(f"For {unsourced} of {len(skills)} skills no official page passed the check, so Dojo does not "
                         "teach them or ask about them. They count as not shown.")
        timed = bool(cert and cert["duration_minutes"])
        return {
            "id": code.lower(), "exam": code, "title": guide["title"], "issuer": issuer(code), "kind": "certification",
            "version": guide["as_of"] or f"retrieved-{today}",
            "source": {"name": guide["name"], "url": guide["url"], "skills_as_of": guide["as_of"], "retrieved": today,
                       "git_commit_id": guide["meta"].get("git_commit_id")},
            "passing_score": guide["passing_score"],
            "official_practice_assessment": practice,
            "exam_page": cert["url"] if cert else None,
            "exam_format": {"duration_minutes": cert["duration_minutes"] if timed else None,
                            "duration_quote": cert["duration_quote"] if timed else None,
                            "duration_source": cert["url"] if timed else None, "question_count": None,
                            "question_count_note": COUNT_NOTE.format(code=code), "question_count_source": SUPPORT_URL,
                            # The certification level decides the open-book rule and the typical duration (ADR 0012).
                            "level": cert.get("level") if cert and issuer(code) == "Microsoft" else None,
                            "level_quote": cert.get("level_quote") if cert and issuer(code) == "Microsoft" else None,
                            "level_source": cert["url"] if cert and cert.get("level") and issuer(code) == "Microsoft" else None,
                            "checked": today},
            "notes": notes,
            "added": iso(),
            "domains": domains,
        }

    # ------------------------------------------------------------ progress, filming and cost

    def _media(self) -> set[str]:
        try:
            return set(os.listdir(self.store.path("media")))
        except OSError:
            return set()

    def _lessons(self, pid: str, media: set[str]) -> dict:
        """How far the course is: lessons written, narrated and filmed, and the lessons that could be filmed."""
        pkg = self.packages.get(pid)
        teachable = [s["id"] for s in pkg["skills"].values() if s["sources"]]
        ready = narrated = filmed = 0
        unfilmed: list[str] = []
        for sid in teachable:
            lessons = self.dojo.lessons_for(pid, sid)
            if not lessons:
                continue
            ready += 1
            lesson = lessons[0]["id"]
            plan = self.checker.plan(lesson)
            audio = [a for a in plan["audio"] if a]
            if audio and all(a + ".mp3" in media for a in audio):
                narrated += 1
            films = list(plan["films"].values())
            if films and all(f + ".webm" in media and f + ".json" in media for f in films):
                filmed += 1
            elif films:
                unfilmed.append(lesson)
        return {"skills": len(pkg["skills"]), "teachable": len(teachable), "unsourced": len(pkg["skills"]) - len(teachable),
                "ready": ready, "narrated": narrated, "filmed": filmed, "unfilmed": unfilmed}

    def _settle(self, doc: dict) -> None:
        """Filming jobs that ended: done, or failed and waiting for a new approval."""
        film = doc.get("film") or {}
        for lesson, rec in list((film.get("filed") or {}).items()):
            if rec.get("done"):
                continue
            try:
                job = self.store.read("jobs", rec["job"])
            except ValueError:
                job = None
            state = (job or {}).get("state")
            if state in ("queued", "running"):
                continue
            if state == "done":
                rec["done"] = True
            else:
                film.setdefault("failed", {})[lesson] = {"at": iso(), "error": (job or {}).get("error") or "The filming job was lost."}
                del film["filed"][lesson]

    @staticmethod
    def _offer_token(lessons: list[str]) -> str:
        return sha256("|".join(sorted(lessons)))[:16]

    @staticmethod
    def _queue(film: dict, unfilmed: list[str]) -> tuple[list[str], list[str]]:
        """Approved lessons still waiting to be filmed, and the lessons the owner could approve now:
        ready, not filmed, not waiting and not being filmed. A lesson whose filming failed, or whose
        video was cleared away later, needs a new approval."""
        filed, failed = film.get("filed") or {}, film.get("failed") or {}
        busy = {k for k, r in filed.items() if not r.get("done")}
        waiting = [x for x in film.get("approved") or [] if x in unfilmed and x not in filed and x not in failed]
        offer = [x for x in unfilmed if x not in waiting and x not in busy]
        return waiting, offer

    def _film_view(self, doc: dict, lessons: dict) -> dict:
        film = doc.get("film") or {}
        waiting, offer = self._queue(film, lessons["unfilmed"])
        usage = self.dojo.avatar.usage()
        return {
            "approved": len(film.get("approved") or []), "waiting": len(waiting),
            "filming": sum(1 for r in (film.get("filed") or {}).values() if not r.get("done")),
            "failed": [{"lesson": k, **v} for k, v in (film.get("failed") or {}).items()][:10],
            "offer": {"lessons": len(offer), "token": self._offer_token(offer),
                      "estimate": self.costs.film_estimate([self.checker.plan(x) for x in offer])} if offer else None,
            "approvals": (film.get("approvals") or [])[-5:],
            "storage": {"bytes": usage["bytes"], "budget_bytes": usage["budget_bytes"]},
            "note": self.preparer.state.get("film_note") or "",
        }

    def _settled(self, pid: str) -> dict | None:
        """The build document with finished filming jobs moved on, written back only when that changed it."""
        with self._lock:
            doc = self._doc(pid)
            if doc and (doc.get("film") or {}).get("filed"):
                before = repr(doc["film"])
                self._settle(doc)
                if repr(doc["film"]) != before:
                    self.store.write("package-builds", pid, value=doc)
            return doc

    def _state(self, pid: str, added: bool, doc: dict) -> tuple[str, dict | None]:
        job = self._build_job(pid)
        state = "added" if added else doc.get("state") or "stopped"
        if state == "matching" and not job:
            state = "stopped"   # the job went with a restart; building again continues from the pages checked
        return state, job

    def _ready(self, pid: str) -> dict:
        """Lessons written so far, of the skills that can be taught: cheap enough for every list."""
        pkg = self.packages.by_id.get(pid) or {"skills": {}}   # removed a moment ago: nothing to count
        teachable = {s["id"] for s in pkg["skills"].values() if s["sources"]}
        return {"ready": len(teachable & self.dojo.lesson_skills(pid)), "teachable": len(teachable)}

    def overview(self) -> list[dict]:
        """The exams the owner added, and the builds that run or stopped, newest first."""
        pids = set(self.store.names("package-builds")) | {pid for pid in self.packages.ids() if self.packages.is_added(pid)}
        out = []
        for pid in pids:
            added, doc = self.packages.is_added(pid), self._doc(pid) or {}
            state, _ = self._state(pid, added, doc)
            row = {"package": pid, "exam": doc.get("code") or pid.upper(), "title": doc.get("title"), "state": state,
                   "added": added, "skills": doc.get("skills"), "checked": doc.get("checked"), "started": doc.get("started")}
            if added:
                meta = self.packages.get(pid)["meta"]
                row.update(exam=meta["exam"], title=meta["title"], started=row["started"] or meta.get("added"),
                           lessons=self._ready(pid))
            out.append(row)
        return sorted(out, key=lambda r: r["started"] or "", reverse=True)

    def status(self, learner: Learner, pid: str) -> dict:
        added = self.packages.is_added(pid)
        doc = self._settled(pid)
        if not added and not doc:
            raise HTTPException(404, "Dojo is not building or keeping an added exam with that code.")
        doc = doc or {"package": pid}
        state, job = self._state(pid, added, doc)
        out: dict = {
            "package": pid, "exam": doc.get("code") or pid.upper(), "title": doc.get("title"), "state": state, "added": added,
            "job": job,
            "matching": {k: doc.get(k) for k in ("skills", "checked", "sourced", "errors", "started", "finished", "error")},
            "exam_date": self.dojo.profile(learner)["exam_dates"].get(pid) if added else doc.get("exam_date"),
            "preparing": {"running": bool(getattr(self.preparer, "_started", False)), "current": self.preparer.state.get("current")},
            "removable": added or state in ("failed", "stopped"),
        }
        if not added:
            return out
        pkg = self.packages.get(pid)
        meta = pkg["meta"]
        lessons = self._lessons(pid, self._media())
        out.update(
            title=meta["title"], issuer=meta["issuer"], source=meta["source"], exam_page=meta.get("exam_page"),
            practice=meta.get("official_practice_assessment"), passing_score=meta.get("passing_score"),
            duration_minutes=(meta.get("exam_format") or {}).get("duration_minutes"), notes=meta.get("notes") or [],
            added_at=meta.get("added"), version=meta["version"], no_exam=exam_blocker(pkg),
            lessons={k: v for k, v in lessons.items() if k != "unfilmed"}, film=self._film_view(doc, lessons))
        return out

    def approve_film(self, learner: Learner, pid: str, token: str) -> dict:
        """The owner's explicit approval of the spend shown on the page: exactly those lessons."""
        if not self.packages.is_added(pid):
            raise HTTPException(404, "No such added exam.")
        with self._lock:
            doc = self._doc(pid) or {"package": pid}
            self._settle(doc)
            lessons = self._lessons(pid, self._media())
            film = doc.setdefault("film", {})
            _, offer = self._queue(film, lessons["unfilmed"])
            if not offer:
                raise UserError("There is nothing new to film: every ready lesson is filmed or already waiting.")
            if token != self._offer_token(offer):
                raise HTTPException(409, "The lessons to film changed since the page was drawn. Look at the new price and press again.")
            estimate = self.costs.film_estimate([self.checker.plan(x) for x in offer])
            film["approved"] = list(dict.fromkeys((film.get("approved") or []) + offer))
            for x in offer:
                film.setdefault("failed", {}).pop(x, None)
                film.setdefault("filed", {}).pop(x, None)
            film.setdefault("approvals", []).append({"at": iso(), "lessons": len(offer), "total": estimate.get("total"),
                                                     "currency": estimate.get("currency")})
            self.store.write("package-builds", pid, value=doc)
        self.preparer.wake()
        return self.status(learner, pid)

    def next_film(self) -> str | None:
        """The next lesson the owner approved for filming that is still not filmed, or None."""
        for pid in self.packages.ids():
            if not self.packages.is_added(pid):
                continue
            doc = self._settled(pid)
            film = (doc or {}).get("film") or {}
            if not film.get("approved"):
                continue
            present = {m["id"] for m in self.dojo.lesson_index() if m.get("package") == pid}
            for lesson in film["approved"]:
                if lesson in (film.get("filed") or {}) or lesson in (film.get("failed") or {}) or lesson not in present:
                    continue
                films = list(self.checker.plan(lesson)["films"].values())
                if films and not all(self.dojo.avatar.exists(f) for f in films):
                    return lesson
        return None

    def filed(self, lesson: str, job_id: str) -> None:
        pkg = (next((m for m in self.dojo.lesson_index() if m["id"] == lesson), None) or {}).get("package")
        if pkg:
            self._change(pkg, lambda d: d.setdefault("film", {}).setdefault("filed", {}).update({lesson: {"job": job_id, "at": iso()}}))

    # ------------------------------------------------------------ removing an exam

    def remove(self, learner: Learner, pid: str) -> dict:
        """Remove an added exam and what was prepared for it: the package, its lessons with their audio and
        video, their delivery checks, what the source-drift check wrote down for them, the question pool and
        the build notes. The learner's record stays:
        every event, item, attempt and piece of advice is kept (the record is append-only)."""
        if pid in self.packages.builtin:
            raise UserError("A built-in exam cannot be removed.")
        added = self.packages.is_added(pid)
        doc = self._doc(pid)
        if not added and not doc:
            raise HTTPException(404, "No such added exam.")
        # The check for running attempts and the mark that closes the exam to new starts are one step under
        # the store lock, which ExamRoom.start holds too: no attempt can open in between (ADR 0009).
        with self.store.lock:
            if pid in self.room.closing:
                raise HTTPException(409, "This exam is being removed already.")
            everyone = self._learners(learner)
            running = sum(1 for who in everyone if self.room.open_attempt(who, pid))
            if running == 1 and self.room.open_attempt(learner, pid):
                raise HTTPException(409, "A timed practice exam for this exam is running. Finish or end it first.")
            if running:
                # A count, no names (ADR 0009): the owner learns only that the exam is in use.
                raise HTTPException(409, f"{running} timed practice exam{'s' if running > 1 else ''} for this exam "
                                         f"{'are' if running > 1 else 'is'} running. Try again when they have ended.")
            self.room.closing.add(pid)
        try:
            if added:
                # Attempts from before attempts kept their package version are bound to the version that is
                # retired now, so a changed exam added later under the same id never changes how they read.
                # No attempt of this exam can be written meanwhile: it is marked closing.
                with self.store.lock:
                    self.room.bind_versions(everyone, pid, self.packages.version(pid))
            code = (self.packages.get(pid)["meta"]["exam"] if added else (doc or {}).get("code")) or pid.upper()
            with self.pool_keeper.paused(learner):
                with self.jobs.exclusive(learner):
                    if added:
                        self.packages.remove(pid)
                    removed = self._forget_content(pid)
                    for who in everyone:
                        self._unenroll(who, pid)
                    self.store.delete("package-builds", pid)
        finally:
            with self.store.lock:
                self.room.closing.discard(pid)   # removed: no longer in the catalog; failed: open again
        self._forget_drift(pid)
        self.podcast.prune_soon()
        with self._lock:
            self._lookups.pop(code, None)
        if added:
            self.store.append_event(learner.key, "package.removed", {"package": pid, "exam": code, **removed})
        return {"removed": pid, "exam": code, **removed}

    def forget(self, pid: str) -> None:
        """The exam went while a lesson for it was being written or narrated: that lesson goes too."""
        if pid not in self.packages.by_id:
            self._forget_content(pid)
            self._forget_drift(pid)

    def _learners(self, owner: Learner) -> list[Learner]:
        """Every learner with a folder: the owner first, then the members, whatever their status, so no
        pool or enrollment of the removed exam is left behind."""
        folder = self.store.path("learners")
        keys = sorted(x.name for x in folder.iterdir() if x.is_dir()) if folder.is_dir() else []
        keys = [k for k in keys if k != owner.key and not self.store.tombstoned(k)]
        return [owner] + [Learner("", k, "") for k in keys]

    def _unenroll(self, who: Learner, pid: str) -> None:
        """The removed exam's pool goes, and it leaves the learner's enrollment (ADR 0009, "Enrollment")."""
        try:
            with self.store.lock:
                self.store.delete("learners", who.key, "pool", pid)
                profile = self.store.read("learners", who.key, "profile", default=None)
                if isinstance(profile, dict) and isinstance(profile.get("enrolled"), list) and pid in profile["enrolled"]:
                    profile["enrolled"] = [p for p in profile["enrolled"] if p != pid]
                    self.store.write("learners", who.key, "profile", value=profile)
        except (Abandoned, ValueError) as e:   # a learner leaving right now: their folder goes anyway
            log.info("removing an exam skipped a learner folder: %s", type(e).__name__)

    def _forget_drift(self, pid: str) -> None:
        """What the source-drift check wrote down for the exam's lessons (ADR 0006)."""
        drift = getattr(self.dojo, "drift", None)
        if drift is not None:
            drift.forget(pid)

    def _forget_content(self, pid: str) -> dict:
        index = self.dojo.lesson_index()
        mine = [m["id"] for m in index if m.get("package") == pid]
        keep_audio: set[str] = set()
        keep_films: set[str] = set()
        for m in index:
            if m.get("package") != pid:
                plan = self.checker.plan(m["id"])
                keep_audio.update(a for a in plan["audio"] if a)
                keep_films.update(plan["films"].values())
        audio = videos = 0
        for lesson in mine:
            plan = self.checker.plan(lesson)
            for a in {a for a in plan["audio"] if a} - keep_audio:
                if self.store.exists("media", a + ".mp3"):
                    self.store.delete_file("media", a + ".mp3")
                    audio += 1
            for f in set(plan["films"].values()) - keep_films:
                if self.store.exists("media", f + ".webm"):
                    videos += 1
                self.store.delete_file("media", f + ".webm")
                self.store.delete_file("media", f + ".json")
            self.store.delete("content", "delivery", lesson)
            self.dojo.drop_lesson(lesson)
            self.checker._plans.pop(lesson, None)
            if self.checker._results is not None:
                self.checker._results.pop(lesson, None)
        return {"lessons": len(mine), "audio": audio, "videos": videos}

    # ------------------------------------------------------------ what /api/diag shows

    def diag(self) -> list[dict]:
        out = []
        for name in self.store.names("package-builds"):
            doc = self._doc(name) or {}
            job = self._build_job(name)
            film = doc.get("film") or {}
            added = self.packages.is_added(name)
            out.append({"package": name, "state": doc.get("state"), "added": added,
                        "skills": doc.get("skills"), "checked": doc.get("checked"), "sourced": doc.get("sourced"),
                        "errors": doc.get("errors"), "job": job["state"] if job else None, "error": doc.get("error"),
                        "lessons": self._ready(name) if added else None,
                        "film_approved": len(film.get("approved") or []), "film_failed": len(film.get("failed") or {})})
        return out
