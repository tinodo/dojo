"""Exam packages: the official skills measured, their blueprint weights and the sources that ground them.

The built-in packages are files in app/packages. Packages the owner adds on the Add an exam page
(app/newexam.py) are kept on the data share under packages/. A certification target is configuration,
not architecture (EG-45): an added package has exactly the built-in schema, is checked against it
whenever it is read, and every part of Dojo uses it the same way. A built-in package cannot be
replaced or removed.

Dojo can find more official pages for the skills of a built-in package (app/deeper.py, ADR 0008). They
are kept apart from the repository's file, as an overlay in the content store
(content/package-sources/<id>.json), and added after the package's own pages whenever the package is
loaded or the overlay changes. The package's own page stays the first source. An overlay that cannot be
read, or a page in it that does not pass the checks, is logged and left out: it never stops Dojo."""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from .core import Store, UserError, canonical, iso, sha256
from .sources import check_url, docs_url, normalize, same_page

log = logging.getLogger("dojo.packages")

EXPLAIN_VERBS = {
    "describe", "explain", "identify", "understand", "recognize", "differentiate", "distinguish", "compare",
    "define", "summarize", "interpret", "list", "outline", "discuss", "know", "determine",
}
PACKAGE_ID = re.compile(r"^[a-z0-9-]{2,40}$")     # the same shapes the API accepts (main.PKG, main.SKILL)
SKILL_ID = re.compile(r"^[a-z0-9.]{2,40}$")
EXAM_CODE = re.compile(r"^[A-Z]{2}-\d{3}$")
DOMAIN_ID = re.compile(r"^d\d{1,2}$")
MAX_SKILLS = 400
MAX_SOURCES = 3
OVERLAY = "package-sources"     # content/package-sources/<id>.json: pages Dojo found for a built-in package
ADDED_LICENSE = "CC-BY-4.0"
ADDED_PUBLISHERS = {"learn.microsoft.com": "Microsoft Learn", "docs.github.com": "GitHub Docs"}
ADDED_KEYS = ("url", "title", "publisher", "license", "use", "verified", "verified_at", "reason")


def skill_kind(text: str) -> tuple[str, str]:
    """The item template follows the skill's verb: explaining verbs get explanation items, doing
    verbs (configure, use, implement, ...) get a scenario with one changed condition."""
    m = re.match(r"[A-Za-z]+", text.strip())
    verb = m.group(0).lower() if m else ""
    return ("explanation" if verb in EXPLAIN_VERBS else "scenario"), verb


def _text(value: Any, what: str, low: int = 1, high: int = 600) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise ValueError(f"{what} must be text of {low} to {high} characters")
    return value


def _url(value: Any, what: str) -> str:
    try:
        return check_url(_text(value, what, 12, 600))
    except UserError as e:
        raise ValueError(f"{what} is not on an allowed host") from e


def validate(raw: Any) -> dict:
    """Check an added package against the schema the built-in packages use, so that every feature
    can read it unchanged. Raises ValueError with the first thing that is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("a package is a JSON object")
    if not isinstance(raw.get("id"), str) or not PACKAGE_ID.match(raw["id"]):
        raise ValueError("the package id must be lower-case letters, digits and hyphens")
    if not isinstance(raw.get("exam"), str) or not EXAM_CODE.match(raw["exam"]) or raw["id"] != raw["exam"].lower():
        raise ValueError("the exam code must look like AZ-104 and match the package id")
    _text(raw.get("title"), "the title", 3, 200)
    if raw.get("issuer") not in ("Microsoft", "GitHub") or raw.get("kind") != "certification":
        raise ValueError("the issuer must be Microsoft or GitHub and the kind a certification")
    _text(raw.get("version"), "the version", 4, 40)
    source = raw.get("source")
    if not isinstance(source, dict):
        raise ValueError("the package must name its study guide")
    _text(source.get("name"), "the study guide's title", 3, 300)
    _url(source.get("url"), "the study guide's address")
    practice = raw.get("official_practice_assessment")
    if practice is not None:
        if not isinstance(practice, dict):
            raise ValueError("the practice assessment must be an object or null")
        _url(practice.get("url"), "the practice assessment's address")
    fmt = raw.get("exam_format")
    if fmt is not None and not isinstance(fmt, dict):
        raise ValueError("the exam format must be an object or null")
    if isinstance(fmt, dict) and fmt.get("duration_minutes") is not None:
        minutes = fmt["duration_minutes"]
        if not isinstance(minutes, int) or not 15 <= minutes <= 600:
            raise ValueError("the exam duration must be between 15 and 600 minutes")
    if isinstance(fmt, dict) and fmt.get("level") not in (None, "fundamentals", "associate", "expert"):
        raise ValueError("the certification level must be fundamentals, associate, expert or null")
    if raw.get("exam_page") is not None:
        _url(raw["exam_page"], "the exam page")
    if not isinstance(raw.get("notes", []), list) or not all(isinstance(n, str) for n in raw.get("notes", [])):
        raise ValueError("the notes must be a list of text")
    domains = raw.get("domains")
    if not isinstance(domains, list) or not domains or len(domains) > 20:
        raise ValueError("a package needs 1 to 20 domains")
    seen: set[str] = set()
    domain_ids: set[str] = set()
    for d in domains:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not DOMAIN_ID.match(d["id"]) or d["id"] in domain_ids:
            raise ValueError("every domain needs its own id such as d1")
        domain_ids.add(d["id"])
        _text(d.get("title"), "a domain title", 3, 300)
        lo, hi = d.get("weight_min"), d.get("weight_max")
        if not all(isinstance(w, int) and not isinstance(w, bool) for w in (lo, hi)) or not 0 <= lo <= hi <= 100:
            raise ValueError(f"the weight of {d['title']!r} must be a range between 0 and 100")
        groups = d.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError(f"{d['title']!r} has no groups")
        for g in groups:
            if not isinstance(g, dict):
                raise ValueError("a group is an object")
            _text(g.get("title"), "a group title", 3, 300)
            skills = g.get("skills")
            if not isinstance(skills, list) or not skills:
                raise ValueError(f"the group {g['title']!r} has no skills")
            for s in skills:
                if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not SKILL_ID.match(s["id"]) or s["id"] in seen:
                    raise ValueError("every skill needs its own id of lower-case letters, digits and dots")
                seen.add(s["id"])
                _text(s.get("text"), "a skill", 3, 600)
                refs = s.get("sources", [])
                if not isinstance(refs, list) or len(refs) > MAX_SOURCES:
                    raise ValueError(f"a skill has at most {MAX_SOURCES} sources")
                for ref in refs:
                    if not isinstance(ref, dict) or ref.get("use") != "ground":
                        raise ValueError("every source of an added skill grounds it")
                    _url(ref.get("url"), "a source address")
                    _text(ref.get("title"), "a source title", 1, 300)
                if not isinstance(s.get("learn", []), list):
                    raise ValueError("learn must be a list")
    if not 3 <= len(seen) <= MAX_SKILLS:
        raise ValueError(f"a package has 3 to {MAX_SKILLS} skills")
    return raw


def blueprint(raw: dict) -> str:
    """A short mark of what the exam measures: its domains, their weights and their skills. A practice
    exam keeps the mark of the blueprint it followed, and readiness counts it only while the package
    has that blueprint (app/readiness.py). Titles, groups and sources do not change what is measured."""
    return sha256(canonical([[d["id"], d["weight_min"], d["weight_max"],
                              [[s["id"], normalize(s["text"])] for g in d["groups"] for s in g["skills"]]]
                             for d in raw["domains"]]))[:16]


def added_ref(ref: Any) -> dict:
    """One page an overlay adds to a skill, checked as strictly as a page Add an exam keeps: official
    documentation in its canonical form, its publisher and license named, and the sentence the checker
    copied from it. Returns the fields a source keeps; raises ValueError."""
    if not isinstance(ref, dict) or ref.get("use") != "ground":
        raise ValueError("an added page must ground the skill")
    url = _url(ref.get("url"), "an added page's address")
    if docs_url(url) != url:
        raise ValueError(f"{url} is not an official documentation page in its canonical form")
    _text(ref.get("title"), "an added page's title", 1, 300)
    if ref.get("license") != ADDED_LICENSE or ref.get("publisher") != ADDED_PUBLISHERS.get(urlparse(url).hostname):
        raise ValueError(f"{url} does not name its publisher and the {ADDED_LICENSE} license")
    v = ref.get("verified")
    if not isinstance(v, dict) or not isinstance(v.get("quote"), str) or not v["quote"].strip():
        raise ValueError(f"{url} has no sentence the checker copied from it")
    return {k: ref[k] for k in ADDED_KEYS if k in ref}


def merge_sources(raw: dict, overlay: Any) -> tuple[dict, list[str]]:
    """A built-in package with the pages Dojo found for its skills added after the package's own pages
    (ADR 0008). `raw` is not changed. A page that fails `added_ref`, repeats a page the skill cites
    already, or would give the skill more than MAX_SOURCES pages is left out, and so is every page found
    for a skill whose words changed since; the second value says what was left out and why. Raises
    ValueError when the overlay as a whole is not one for this package."""
    if not isinstance(overlay, dict) or overlay.get("package") != raw["id"] or not isinstance(overlay.get("skills"), dict):
        raise ValueError("it is not a list of pages found for this exam")
    out = copy.deepcopy(raw)
    skills = {s["id"]: s for d in out["domains"] for g in d["groups"] for s in g["skills"]}
    skipped = [f"{sid}: there is no such skill" for sid in overlay["skills"] if sid not in skills]
    for sid, s in skills.items():
        entry = overlay["skills"].get(sid)
        found = entry.get("sources") if isinstance(entry, dict) else None
        if not found:
            continue
        if not isinstance(found, list):
            skipped.append(f"{sid}: its pages are not a list")
            continue
        if normalize(str(entry.get("skill_text") or "")) != normalize(s["text"]):
            skipped.append(f"{sid}: the skill's words changed after its pages were found")
            continue
        refs = list(s.get("sources", []))
        for ref in found:
            try:
                ref = added_ref(ref)
            except ValueError as e:
                skipped.append(f"{sid}: {e}")
                continue
            if any(isinstance(r, dict) and same_page(ref["url"], r.get("url")) for r in refs):
                skipped.append(f"{sid}: {ref['url']} is cited already")
            elif len(refs) >= MAX_SOURCES:
                skipped.append(f"{sid}: a skill has at most {MAX_SOURCES} pages")
            else:
                refs.append(ref)
        s["sources"] = refs
    return out, skipped


class Packages:
    """Every exam Dojo knows. `by_id` is replaced, never changed in place, when an exam is added or
    removed, or when the pages found for a built-in exam change, so code that is looping over it is never
    disturbed."""

    def __init__(self, folder: Path, store: Store | None = None):
        self.store = store
        self._lock = threading.Lock()
        self._builtin_raw: dict[str, dict] = {}   # the built-in packages as the repository has them
        found: dict[str, dict] = {}
        for f in sorted(folder.glob("*.json")):
            raw = json.loads(f.read_text("utf-8"))
            self._builtin_raw[raw["id"]] = raw
            found[raw["id"]] = self._index(self._overlaid(raw), builtin=True)
        self.builtin = frozenset(found)
        for name in store.names("packages") if store is not None else []:
            try:
                raw = validate(store.read("packages", name))
                if raw["id"] != name or raw["id"] in found:
                    raise ValueError("its id does not match its file, or a built-in package has that id")
                found[raw["id"]] = self._index(raw)
            except (ValueError, KeyError, TypeError) as e:
                log.warning("skipping the added exam package %s: %s", name, e)
        self.by_id: dict[str, dict] = found

    def _overlaid(self, raw: dict) -> dict:
        """A built-in package with the pages Dojo found for it (ADR 0008). An overlay that cannot be read is
        logged and ignored, and the package is used as the repository has it."""
        if self.store is None:
            return raw
        try:
            doc = self.store.read("content", OVERLAY, raw["id"])
            if doc is None:
                return raw
            merged, skipped = merge_sources(raw, doc)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("ignoring the pages found for the exam package %s: %s", raw["id"], e)
            return raw
        if skipped:
            log.warning("left out %d of the pages found for the exam package %s, for example %s",
                        len(skipped), raw["id"], skipped[0])
        return merged

    def builtin_raw(self, package_id: str) -> dict:
        """A built-in package as the repository has it, without the pages Dojo found."""
        return self._builtin_raw[package_id]

    def overlay(self, package_id: str) -> dict | None:
        """The pages Dojo found for a built-in package, as stored, or None. Not checked: see `set_overlay`."""
        if self.store is None or package_id not in self._builtin_raw:
            return None
        try:
            doc = self.store.read("content", OVERLAY, package_id)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def set_overlay(self, package_id: str, doc: dict) -> dict:
        """Keep the pages Dojo found for a built-in package's skills and cite them from now on (ADR 0008).
        The overlay must be one for this package (`merge_sources`); a page in it that fails the checks is
        left out of the package but stays in the file. `by_id` is replaced, never changed in place."""
        if self.store is None:
            raise UserError("This Dojo has nowhere to keep the pages it found.")
        with self._lock:
            if package_id not in self._builtin_raw:
                raise UserError("Only a built-in exam gets pages this way: an added exam has its pages from the start.")
            merged, skipped = merge_sources(self._builtin_raw[package_id], doc)
            self.store.write("content", OVERLAY, package_id, value=doc)
            indexed = self._index(merged, builtin=True)
            self.by_id = {**self.by_id, package_id: indexed}
        if skipped:
            log.warning("left out %d of the pages found for the exam package %s, for example %s",
                        len(skipped), package_id, skipped[0])
        return indexed

    @staticmethod
    def _index(raw: dict, builtin: bool = False) -> dict:
        skills: dict[str, dict] = {}
        for d in raw["domains"]:
            count = sum(len(g["skills"]) for g in d["groups"]) or 1
            middle = (d["weight_min"] + d["weight_max"]) / 2
            for g in d["groups"]:
                for s in g["skills"]:
                    kind, verb = skill_kind(s["text"])
                    skills[s["id"]] = {
                        "id": s["id"], "text": s["text"], "domain": d["id"], "domain_title": d["title"],
                        "domain_weight": f'{d["weight_min"]}-{d["weight_max"]}%', "group": g["title"],
                        "weight": middle / count, "kind": kind, "verb": verb,
                        "sources": [x for x in s.get("sources", []) if x.get("use") == "ground"],
                        "learn": s.get("learn", []),
                    }
        total = sum(s["weight"] for s in skills.values()) or 1.0
        for s in skills.values():
            s["share"] = s["weight"] / total
        meta = {k: raw.get(k) for k in ("id", "exam", "title", "issuer", "kind", "version", "source", "passing_score",
                                        "official_practice_assessment", "exam_page", "exam_format", "notes", "added")}
        meta.update(blueprint=blueprint(raw), builtin=builtin)
        domains = [{"id": d["id"], "title": d["title"], "weight_min": d["weight_min"], "weight_max": d["weight_max"],
                    "groups": [{"title": g["title"], "skills": [s["id"] for s in g["skills"]]} for g in d["groups"]]}
                   for d in raw["domains"]]
        return {"meta": meta, "domains": domains, "skills": skills}

    def ids(self) -> list[str]:
        return list(self.by_id)

    def get(self, package_id: str) -> dict:
        p = self.by_id.get(package_id)
        if not p:
            raise HTTPException(404, "No such exam package.")
        return p

    def skill(self, package_id: str, skill_id: str) -> dict:
        s = self.get(package_id)["skills"].get(skill_id)
        if not s:
            raise HTTPException(404, "No such skill in this exam.")
        return s

    def is_added(self, package_id: str) -> bool:
        return package_id in self.by_id and package_id not in self.builtin

    def add(self, raw: dict) -> dict:
        """Keep a new package on the data share and start using it. It must pass `validate`."""
        if self.store is None:
            raise UserError("This Dojo has nowhere to keep an added exam.")
        try:
            validate(raw)
        except ValueError as e:
            raise UserError(f"Dojo did not keep the exam package: {e}.") from e
        with self._lock:
            if raw["id"] in self.builtin:
                raise UserError(f"{raw['exam']} is built into Dojo already.")
            indexed = self._index(raw)
            self.store.write("packages", raw["id"], value=raw)
            self.by_id = {**self.by_id, raw["id"]: indexed}
        return indexed

    def remove(self, package_id: str) -> None:
        """Removes an added exam. A frozen copy is kept in `packages-retired/` so past attempts still show
        its title and skill names, marked as removed (ADR 0009, "Enrollment")."""
        with self._lock:
            if package_id in self.builtin:
                raise UserError("A built-in exam cannot be removed.")
            if package_id not in self.by_id:
                raise HTTPException(404, "No such exam package.")
            if self.store is not None:
                raw = self.store.read("packages", package_id, default=None)
                if isinstance(raw, dict):
                    frozen = {**raw, "retired": iso()}
                    self.store.write("packages-retired", package_id, value=frozen)
                    # Kept by version as well: if the same exam is added again and removed again, an attempt
                    # still finds the very version it was written from.
                    self.store.write("packages-retired", package_id, _version(raw), value=frozen)
                self.store.delete("packages", package_id)
            self.by_id = {k: v for k, v in self.by_id.items() if k != package_id}

    def version(self, package_id: str) -> str | None:
        """Which version of an added exam this is, so an attempt can be shown with the package it was written
        from even after the exam was removed and added again. None for a built-in exam: it is never removed."""
        if self.store is None or package_id in self.builtin or package_id not in self.by_id:
            return None
        raw = self.store.read("packages", package_id, default=None)
        return _version(raw) if isinstance(raw, dict) else None

    def retired(self, package_id: str, version: str | None = None) -> dict | None:
        """The frozen copy of a removed exam, indexed like a live one, its name marked "(removed)". With a
        version, that version's copy, also while a newer version of the same exam is live."""
        if self.store is None or (version is None and package_id in self.by_id):
            return None
        try:
            if version is not None:
                raw = validate(self.store.read("packages-retired", package_id, version, default=None))
            else:
                raw = validate(self.store.read("packages-retired", package_id, default=None))
        except (ValueError, KeyError, TypeError):
            return None
        pkg = self._index(raw)
        pkg["meta"].update(exam=f"{raw['exam']} (removed)", retired=True)
        return pkg

    def get_any(self, package_id: str, version: str | None = None) -> dict:
        """For history only, never for new work: the version an attempt was written from if it is known
        (live, or its frozen copy), else the live exam, else the frozen copy of a removed one."""
        if version is not None and self.version(package_id) != version:
            frozen = self.retired(package_id, version)
            if frozen is not None:
                return frozen
        return self.by_id.get(package_id) or self.retired(package_id) or self.get(package_id)


def _version(raw: dict) -> str:
    """A short fingerprint of an added exam's package as kept on the data share."""
    return sha256(canonical(raw))[:16]
