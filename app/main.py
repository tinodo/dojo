"""The Dojo web app: wiring, the request guard, every route and startup.

- `create_app` builds one Dojo: the Store, the AI, Speech and avatar clients (real, or stubs with
  DOJO_LOCAL=1), the domain objects from the other modules, and the background services. Tests pass
  their own clients in.
- The guard middleware runs on every request. It refuses ambiguous paths (dot segments, backslashes,
  encoded separators). A changing /api call must carry `X-Dojo: 1` and come from Dojo's own origin. It adds
  the security headers: a strict Content-Security-Policy with no inline scripts or styles, no framing, the
  microphone for this site only (ADR 0001). In Azure it logs one scrubbed line per failure, never a line
  per request.
- Every signed-in route depends on one of the route classes of ADR 0009 (`Me`, `Exit`, `Admin`, `Who` and
  their seat variants); `team.ROUTE_CLASSES` and the tests keep that inventory complete. Only `/healthz`,
  `/`, `/static`, and the token-protected `/feed` and `/cal` routes are public.
- Startup (the lifespan) runs the self-test and starts the background services: preparation, the exam
  pool, deeper lessons, the delivery check, source drift, push nudges and Play's sweeper. In team mode the
  model work shares one scheduler that uses only spare job slots, and the team sweeper runs too. Locally
  they start only when asked (DOJO_PREPARE, DOJO_CHECK, DOJO_DRIFT, DOJO_PUSH); the first two also need
  DOJO_LOCAL_AI=real.
- Dojo runs as one process with one worker: its locks and job slots live in memory.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.routing import Match

from .ai import Foundry, StubAI
from .avatar import CONTENT_TYPE as AVATAR_CONTENT_TYPE
from .avatar import MEDIA_ID as VIDEO_ID
from .avatar import SUFFIX as AVATAR_SUFFIX
from .avatar import Avatar, StubAvatar
from .background import BackgroundScheduler
from .budget import Budget, Refused
from .checks import LIMIT as CHECK_LIMIT
from .checks import CheckRoom
from .core import (APP_DIR, Abandoned, Jobs, Learner, Settings, Store, Tokens, UserError, carried, iso, learner_key, load_settings, owner_key,
                   parse_iso, utcnow, where_stored)
from .costs import Costs
from .deeper import DeeperLessons
from .delivery import DeliveryCheck
from .drift import DriftCheck
from . import items as exam_items
from .exam import ExamRoom, PoolKeeper
from . import helpdoc
from .exam import open_book as exam_open_book
from .exam import spec as exam_spec
from .learning import Dojo
from .labs import LABS, FakeRunner, LabRoom, LabUser, SqlRunner, is_waking, sql_error_reason
from .mirror import Mirror
from .newexam import NewExams
from .packages import Packages
from .plan import Planner
from .play import PlayRoom
from .play_board import Board
from .play_duel import Duels
from .play_social import CIRCLE_MAX, PlaySocial
from .podcast import Podcast
from .prepare import Preparer
from .readiness import advice_log, advise, store_advice
from .selftest import SelfTest
from .sources import Sources
from .speech import (AUDIO_TYPES, MAX_AUDIO_BYTES, MEDIA_ID, NORMALIZE_MAX_BYTES, TRANSCRIBE_ERRORS, AudioRefused, Speech, StubSpeech,
                     normalize)
from .team import (NOTICE_VERSION, Seat, Team, current_active_learner, current_admin, current_admin_seat, current_exit_seat,
                   current_member_any, current_principal, notice_view, refuse)
from .five import FIVE_RECALLS, FiveMinutes
from .push import Nudges

# The web app manifest (ADR 0013): not every system knows this ending.
mimetypes.add_type("application/manifest+json", ".webmanifest")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dojo")

# What must never reach a log line in Azure (ADR 0009, "Logging"): learner keys, Dojo ids, tenant and
# object ids, emails and long tokens. A second line of defence: the code logs none of them on purpose.
_SCRUB = [
    (re.compile(r"\bl[0-9a-f]{31}\b"), "<learner>"),
    (re.compile(r"\b[a-z]+-[0-9a-f]{20}\b"), "<id>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<guid>"),
    (re.compile(r"[^\s@/\"'<>]+@[^\s@/\"'<>]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "<token>"),
]


def scrub(text: str) -> str:
    """Replaces learner keys, Dojo ids, GUIDs, emails and long tokens with placeholders."""
    for pattern, placeholder in _SCRUB:
        text = pattern.sub(placeholder, text)
    return text


class ScrubFilter(logging.Filter):
    """Scrubs every log line, and its stack trace, before a handler writes it."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = scrub(record.getMessage()), None
        except Exception:  # noqa: BLE001 - a log line that cannot be formatted is dropped, not shown raw
            return False
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


def quiet_logs() -> None:
    """In Azure: warnings and errors only, never a line per request, and every line scrubbed."""
    logging.getLogger().setLevel(logging.WARNING)
    log.setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, ScrubFilter) for f in handler.filters):
            handler.addFilter(ScrubFilter())

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
ID = r"^[a-z]+-[0-9a-f]{20}$"
PKG = r"^[a-z0-9-]{2,40}$"
SKILL = r"^[a-z0-9.]{2,40}$"
# In Azure without DOJO_LAB_SERVER and DOJO_LAB_DATABASE there is no lab database; every lab route says this.
LABS_OFF = "Labs are not set up on this Dojo."
# Play's social layer (ADR 0010): every route under these answers 404 unless circles are on.
SOCIAL_PATHS = ("/api/play/social", "/api/play/circles", "/api/play/shares", "/api/play/invitations",
                "/api/play/board", "/api/play/duels")


class LessonRequest(BaseModel):
    package: str = Field(pattern=PKG)
    skill: str = Field(pattern=SKILL)


class ViewedRequest(BaseModel):
    modality: Literal["read", "listen", "present", "watch"]
    narration: Literal["deep", "slides"] | None = None   # what Listen played (ADR 0007)


# How sure the learner was, asked after the answer is committed and before feedback (ADR 0011). "skip"
# is recorded as asked but not given; leaving the field out means it was not asked.
Confidence = Literal["guess", "fair", "certain", "skip"]


class SelfCheckRequest(BaseModel):
    index: int = Field(ge=0, le=20)
    confidence: Confidence | None = None
    matched: Literal["yes", "partly", "no"]


class ItemRequest(BaseModel):
    package: str = Field(pattern=PKG)
    skill: str = Field(pattern=SKILL)
    mode: Literal["probe", "practice", "check"]


class AnswerRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    elapsed_s: int = Field(default=0, ge=0, le=86400)
    input: Literal["typed", "spoken"] = "typed"
    edited_before_send: bool = False
    receipts: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=12)
    podcast_heard: bool | None = None  # the reply to the podcast question, when the item asked it
    confidence: Confidence | None = None


class DisputeRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class RehearsalRequest(BaseModel):
    package: str = Field(pattern=PKG)
    count: int = Field(default=10, ge=4, le=30)


class McqAnswer(BaseModel):
    index: int = Field(ge=0, le=100)
    choice: int = Field(ge=0, le=5)
    confidence: Confidence | None = None


class ExamRequest(BaseModel):
    package: str = Field(pattern=PKG)
    length: Literal["full", "short"] = "full"
    # "single" (the default, for a page loaded before ADR 0012) or "exam": the real exam's question types.
    style: Literal["single", "exam"] = "single"
    open_book: bool = False


class ExamAnswer(BaseModel):
    index: int = Field(ge=0, le=200)
    choice: int | None = Field(default=None, ge=0, le=5)
    # The answer to an item of ADR 0012 (app/items.py checks its shape), and the statement or case question.
    response: dict[str, Any] | None = None
    part: int | None = Field(default=None, ge=0, le=20)
    # The page's count of writes to this item; a request older than one already stored changes nothing.
    rev: int | None = Field(default=None, ge=1, le=1_000_000)


class ExamVisit(BaseModel):
    index: int = Field(ge=-1, le=200)


class ExamLearn(BaseModel):
    event: Literal["open", "away"]
    seconds: int = Field(default=0, ge=0, le=36000)


class ExamFlag(BaseModel):
    index: int = Field(ge=0, le=200)
    flagged: bool = True


class ExamSubmit(BaseModel):
    reason: Literal["submitted", "time"] = "submitted"
    podcast_heard: bool | None = None


class OfficialAssessment(BaseModel):
    package: str = Field(pattern=PKG)


class AskRequest(BaseModel):
    package: str = Field(pattern=PKG)
    skill: str = Field(pattern=SKILL)
    question: str = Field(min_length=3, max_length=1500)


class CheckRequest(BaseModel):
    package: str = Field(pattern=PKG)
    recall: bool = False   # a check made only of the recalls due today (ADR 0011)


class SettingsRequest(BaseModel):
    active_package: str | None = Field(default=None, pattern=PKG)
    exam_dates: dict[str, str | None] | None = None
    later_hours: int | None = Field(default=None, ge=4, le=168)
    enrolled: list[Annotated[str, Field(pattern=PKG)]] | None = Field(default=None, max_length=50)


class DeleteRequest(BaseModel):
    confirm: Literal["DELETE"]

class LabSql(BaseModel):
    sql: str = Field(min_length=1, max_length=8000)
    confirm_destructive: bool = False

class LabHint(BaseModel):
    index: int = Field(ge=0, le=1)


class RefilmRequest(BaseModel):
    lessons: list[Annotated[str, Field(pattern=ID)]] = Field(min_length=1, max_length=200)


# The plan's rules are checked in plan.check_rules, which says in plain words what is wrong.
class PlanWindow(BaseModel):
    label: str = Field(default="", max_length=80)
    days: list[int] = Field(max_length=7)
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)
    audio_only: bool = False


class PlanRules(BaseModel):
    day_minutes: list[int] = Field(max_length=7)
    week_minutes: int
    earliest: str = Field(max_length=5)
    reminder: int
    windows: list[PlanWindow] = Field(max_length=12)


class PlanMove(BaseModel):
    start: str = Field(max_length=16)


class PlanPin(BaseModel):
    pinned: bool


class NewExamRequest(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    exam_date: str | None = Field(default=None, max_length=10)


class FilmRequest(BaseModel):
    confirm: Literal["FILM"]
    offer: str = Field(pattern=r"^[0-9a-f]{16}$")   # the priced set of lessons the owner saw


class RemoveExamRequest(BaseModel):
    confirm: Literal["REMOVE"]


# Team Dojo (ADR 0009)
class AccessRequest(BaseModel):
    used_before: bool = False   # "I used this Dojo before with an account that no longer works"


class NoticeAck(BaseModel):
    version: int = Field(ge=1, le=1000)


class DisplayName(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LeaveRequest(BaseModel):
    confirm: Literal["LEAVE"]


class RemoveMember(BaseModel):
    confirm: Literal["REMOVE"]


class RebindRequest(BaseModel):
    member: str = Field(pattern=ID)
    checked: bool


class EndTold(BaseModel):
    confirmed: bool


class ResumeRequest(BaseModel):
    without_database: bool = False   # the owner's logged override for a long outage of the lab database
    no_restore: bool = False         # "No /home restore has happened since the Pause", confirmed


class LabRepair(BaseModel):
    confirm: bool = False


class EndDate(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


# Play, the personal layer (ADR 0010).
class PlayJoin(BaseModel):
    include_record: bool = False
    days: int | None = Field(default=None, ge=3, le=6)


class PlaySettings(BaseModel):
    days: int | None = Field(default=None, ge=3, le=6)
    rhythm_on: bool | None = None
    show_hidden: bool | None = None


class PassReport(BaseModel):
    package: str = Field(pattern=PKG)
    earned: str = Field(min_length=10, max_length=10)


# Play, the social layer (ADR 0010). Every field is optional so that a route whose circle, invitation or
# shared copy is not the caller's answers 404, never a 422 that would tell it apart.
class SocialJoin(BaseModel):
    version: int | None = None
    invitable: bool = False


class SocialSettings(BaseModel):
    invitable: bool | None = None


class CircleNew(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    people: list[Annotated[str, Field(pattern=ID)]] | None = Field(default=None, max_length=CIRCLE_MAX)


class CircleInvite(BaseModel):
    people: list[Annotated[str, Field(pattern=ID)]] | None = Field(default=None, max_length=CIRCLE_MAX)


class CircleGoal(BaseModel):
    points: int | None = None


class CircleShare(BaseModel):
    moment: str | None = Field(default=None, pattern=ID)


class PlaySwitch(BaseModel):
    on: bool


class BoardSettings(BaseModel):
    mine_only: bool | None = None


class DuelNew(BaseModel):
    person: str | None = Field(default=None, pattern=ID)
    package: str | None = Field(default=None, pattern=PKG)
    domain: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,40}$")


class PushSettings(BaseModel):
    time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][05]$")
    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(min_length=1, max_length=7)


class PushDevice(BaseModel):
    endpoint: str = Field(min_length=20, max_length=2048)
    p256dh: str = Field(min_length=80, max_length=100)
    auth: str = Field(min_length=20, max_length=30)
    label: str = Field(default="", max_length=60)


class PushTest(BaseModel):
    device: str = Field(pattern=r"^dev-[0-9a-f]{20}$")


class BudgetSettings(BaseModel):
    allowances: dict[str, int] | None = None
    cap: float | None = None


class FiveStart(BaseModel):
    end_open: bool = False   # end an ordinary check that is open, then start the 5 minutes


def _credential(settings: Settings) -> Any:
    """The app's managed identity in Azure; locally, the developer's Azure CLI sign-in."""
    from azure.identity import AzureCliCredential, ManagedIdentityCredential
    if settings.in_azure:
        return ManagedIdentityCredential(client_id=settings.identity_client_id or None)
    return AzureCliCredential(tenant_id=settings.tenant_id or None)


def _unsafe_path(raw: bytes) -> bool:
    """Dot segments, backslashes and encoded dots, slashes, backslashes or percent signs can make the
    sign-in layer and the app disagree about which path was asked for, for example whether it lies under
    /feed/ or /cal/, which need no sign-in. Dojo never makes such paths, so they are refused everywhere."""
    low = raw.lower()
    return (b"\\" in raw or b"%2e" in low or b"%2f" in low or b"%5c" in low or b"%25" in low
            or any(part in (b".", b"..") for part in raw.split(b"/")))


def _bare(status: int, **headers: str) -> Response:
    """An empty answer that says nothing about why."""
    return Response(status_code=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                                "X-Robots-Tag": "noindex, nofollow", **headers})


def _check_id(value: str, pattern: str = ID) -> str:
    if not re.match(pattern, value):
        raise HTTPException(404, "Not found.")
    return value


def create_app(settings: Settings | None = None, ai: Any = None, speech: Any = None, http: Any = None,
               avatar: Any = None) -> FastAPI:
    """Builds the app with every part wired together. `ai`, `speech`, `avatar` and `http` (the client
    used to fetch sources and list prices) can be replaced in tests."""
    settings = settings or load_settings()
    if settings.in_azure:
        quiet_logs()
    store = Store(settings.data_dir)
    tokens = Tokens(_credential(settings)) if settings.real_ai else None
    if ai is None:
        ai = Foundry(settings, tokens) if settings.real_ai else StubAI()
    if speech is None:
        speech = Speech(settings, tokens, store) if settings.real_ai else StubSpeech(settings, None, store)
    if avatar is None:
        avatar = Avatar(settings, tokens, store) if settings.real_ai else StubAvatar(settings, None, store)
    packages = Packages(APP_DIR / "packages", store)
    sources = Sources(store, http=http)

    def job_failed(learner_key: str, kind: str, message: str) -> None:
        store.append_line("learners", learner_key, "quality.jsonl", value={"at": iso(), "kind": "failure", "part": kind, "text": "", "reason": message})

    jobs = Jobs(store, on_failure=job_failed)
    dojo = Dojo(settings, store, packages, sources, ai, speech, avatar, jobs)
    selftest = SelfTest(settings, store, ai, speech, avatar, tokens)
    owner_id = "local:dev" if settings.local else f"{settings.owner_tid}:{settings.owner_oid}"
    owner = Learner(owner_id, owner_key(settings), "Owner")
    preparer = Preparer(dojo, owner)
    checker = DeliveryCheck(dojo, owner)
    costs = Costs(dojo, checker, settings.speech_region, http=http, deployment=settings.model_sku)
    costs.deep_progress = preparer.deep_status   # Studio's Deeper Listen line (ADR 0007)
    drift = DriftCheck(dojo, owner, costs)
    podcast = Podcast(dojo, owner)
    podcast.migrate_aired()   # the owner's podcast notes move into the owner's folder, once (ADR 0009)
    room = ExamRoom(dojo, podcast)
    pool = PoolKeeper(room, owner)
    checks = CheckRoom(dojo)
    planner = Planner(dojo, owner)
    play = PlayRoom(dojo)
    newexams = NewExams(dojo, room, pool, podcast, checker, costs, preparer, owner)
    preparer.films = newexams  # files the filming the owner approved for an added exam
    deeper = DeeperLessons(dojo, owner, costs)
    deeper.busy = preparer._learner_busy   # searches only while the learner has nothing running
    preparer.deeper = deeper   # writes lessons again on the deeper template, within their own allowance
    lab_demo = not settings.in_azure
    lab_server, lab_database = os.environ.get("DOJO_LAB_SERVER", ""), os.environ.get("DOJO_LAB_DATABASE", "")
    # In Azure with both lab settings empty there is no lab database: labs are off, with no runner at all,
    # and every lab route says so. One setting without the other is a mistake, and SqlRunner refuses it.
    labs_on = lab_demo or bool(lab_server.strip() or lab_database.strip())
    labs = LabRoom(FakeRunner() if lab_demo else SqlRunner(lab_server, lab_database, _credential(settings)),
                   lab_demo, store) if labs_on else None
    planner.labs_on = labs_on
    dojo.pool_keeper = pool   # so deleting the record holds the background question pool still
    dojo.podcast = podcast    # so deleting the record also removes what the feed has listed
    dojo.drift = drift        # so lessons, items and questions a changed page no longer supports are held back
    dojo.deeper = deeper      # so Listen says when a lesson's Deeper Listen waits for it to be written again
    dojo.open_attempt = room.open_attempt   # so an exam is never left out while its attempt runs
    dojo.on_enrolled = pool.nudge           # a member's new exams get questions without a long wait
    team = Team(settings, store, jobs)   # members, admission and deletion that stays deleted (ADR 0009)
    jobs.owner_key = owner.key            # the owner may queue 6 jobs, a member 3
    dojo.owner_key = owner.key            # the owner studies every exam
    # The active members, read from members.json when asked: token lookups, enrollment counts and the
    # background work across learners. With team mode off this is always empty.
    dojo.members = podcast.links.members = planner.links.members = pool.members = team.active_learners
    team.on_purge.append(podcast.forget)   # a member's AIRED goes with their folder; this drops the cached copy
    # Labs (ADR 0009 PR 4): a member's lab schemas go once the deletion is mirrored in the lab database; the
    # local demo forgets their runs at once. With team mode on and a lab database, the deletion list, the
    # membership changes and Pause are mirrored there, and members wait for the startup reconcile. With labs
    # off there is no lab database: no schemas to drop and no mirror, so /home is the only copy.
    if labs is not None:
        team.on_purge.append(lambda l: labs.forget(team.lab_of(l.key)))
        team.on_synced.append(labs.reconcile)
        if settings.team and not lab_demo:
            team.attach_mirror(Mirror(labs.runner))
    # Budgets and metering (ADR 0009, "Money"): only in team mode. With it off nothing is reserved, no call
    # changes and no file is written, so the owner's Dojo and costs stay exactly as they were.
    budget = Budget(settings, store, owner.key, costs, members=team.active_learners, enrolled_counts=dojo.enrolled_counts)
    if budget.active:
        for client in (ai, speech, avatar):
            client.meter = budget
        # Before anything is served: reservations left open by the process that ended are settled.
        budget.recover([owner.key] + [r["key"] for r in team.rows() if r.get("key") and not r.get("deleted_at")])
    pool.budget = budget
    team.on_sweep.append(lambda: budget.prune([owner.key] + [m.key for m in team.active_learners()]))
    social = PlaySocial(settings, store, team, play)   # circles and the witness room (ADR 0010)
    board = Board(social)                  # the effort board: weekly bands, no order
    duels = Duels(social, dojo)            # duels by invitation
    play.social = social                   # Leave Play deletes the learner's circle entries too
    team.on_purge.append(social.forget)    # and so do Leave Dojo, removal and the sweeper
    team.on_removed.append(social.forget)  # at the moment a membership ends, not only at the purge
    store.on_event.append(social.record_changed)   # a shared copy goes when its support leaves the Record
    background = BackgroundScheduler(jobs)   # team mode: one coordinator, spare slots only (ADR 0009)
    five = FiveMinutes(dojo, checks)     # the 5-minute session the nudge opens (ADR 0013)

    def may_nudge(key: str) -> Learner | None:
        return owner if key == owner.key else team.active_member(key)

    nudges = Nudges(store, jobs.slots, due=five.nudge, may=may_nudge, busy=lambda l: bool(room.open_attempt(l)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        podcast.prune_soon()  # joined files that an earlier run left behind
        prepare_on = settings.real_ai and (settings.in_azure or os.environ.get("DOJO_PREPARE") == "1")
        check_on = settings.real_ai and (settings.in_azure or os.environ.get("DOJO_CHECK") == "1")
        # Reading the official pages again needs no model, so it also runs with the stub when asked.
        drift_on = settings.in_azure or os.environ.get("DOJO_DRIFT") == "1"
        if settings.real_ai:
            selftest.start()
        if settings.team:
            # One thread, a fixed rotation, one batch at a time, and only in a spare work slot. The same
            # first delays as the services' own loops.
            if prepare_on:
                background.add("prepare", preparer.step, 120, wait=lambda worked: 15 if worked else 1800,
                               wake=preparer._wake, service=preparer)
                background.add("pool", pool.step, 300, wait=lambda s: max(5, s), wake=pool.wake, service=pool)
            if drift_on:
                background.add("drift", drift.step, 900, service=drift)
            if prepare_on:
                background.add("deeper", deeper.search_step, 600, wait=lambda s: max(1, s), service=deeper)
            if check_on:
                background.add("delivery", checker.step, 600, service=checker)
            background.start()
        else:
            if prepare_on:
                preparer.start()
                pool.start()
                deeper.start()
            if check_on:
                checker.start()
            if drift_on:
                drift.start()
        if settings.team:
            team.start()
        social.start()   # Play's sweeper: lapsed invitations and old shared copies, in both modes (ADR 0010)
        # Nudges call no model, so they also run with the stub when asked.
        if settings.in_azure or os.environ.get("DOJO_PUSH") == "1":
            nudges.start()
        yield
        nudges.stop()
        podcast.close()

    app = FastAPI(title="Dojo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.dojo = dojo
    app.state.selftest = selftest
    app.state.preparer = preparer
    app.state.checker = checker
    app.state.costs = costs
    app.state.drift = drift
    app.state.deeper = deeper
    app.state.podcast = podcast
    app.state.exams = room
    app.state.pool = pool
    app.state.checks = checks
    app.state.plan = planner
    app.state.play = play
    app.state.social = social
    app.state.newexams = newexams
    app.state.labs = labs
    app.state.team = team
    app.state.budget = budget
    app.state.background = background
    app.state.five = five
    app.state.nudges = nudges
    index_html = (APP_DIR / "static" / "index.html").read_text("utf-8").replace("{{BUILD}}", settings.build[:12])

    site_host = (os.environ.get("WEBSITE_HOSTNAME") or "").lower()

    def origin_ok(request: Request) -> bool:
        """Defence in depth only: the X-Dojo header is what stops cross-site requests, because a browser
        will not send a custom header cross-site without a CORS preflight, and Dojo never approves one.
        Browsers send `Origin: null` in some same-site cases, and behind App Service the Host header can
        differ from the public name, so compare against every name this site answers to."""
        origin = (request.headers.get("origin") or "").strip().lower()
        if not origin or origin == "null":
            return True
        names = {n.strip().lower() for n in (request.headers.get("host", ""), request.headers.get("x-forwarded-host", ""),
                                              request.headers.get("x-original-host", ""), site_host) if n}
        return urlparse(origin).netloc in names

    def template(request: Request) -> str:
        """The route pattern a request matches, for log lines: never the concrete path, which carries ids."""
        for route in app.router.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return getattr(route, "path", "?")
        return "(no route)"

    async def _social_gate(request: Request) -> JSONResponse | None:
        """Play's social routes answer before any body is read (FastAPI parses a JSON body before any
        dependency runs, so a later check would let a malformed body answer 422): first the learner class's
        own refusal, read exactly as the route's check reads it (ADR 0009), then 404 while circles are off
        (ADR 0010, P-1). None lets the request through to its route."""
        try:
            await run_in_threadpool(team.active_seat, request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        except Exception:  # noqa: BLE001 - the route's own check answers, as on any other route
            return None
        return None if social.enabled() else JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        started = time.monotonic()
        path = request.url.path
        # The two routes that need no sign-in, because the apps that fetch them cannot sign in: the private
        # podcast feed (ADR 0002) and the private calendar feed (ADR 0004). The token in the path is the
        # credential, so their paths are never logged.
        feed = any(path == p or path.startswith(p + "/") for p in ("/feed", "/cal"))
        if _unsafe_path(request.scope.get("raw_path") or path.encode("utf-8")):
            log.warning("refused %s: dot segments, a backslash or an encoded character in the path", request.method)
            return _bare(400)
        if feed and request.method not in ("GET", "HEAD"):
            return _bare(405, Allow="GET, HEAD")
        if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
            if request.headers.get("x-dojo") != "1":
                log.warning("refused %s %s: no X-Dojo header", request.method, template(request))
                return JSONResponse({"detail": "Missing the Dojo request header."}, status_code=403)
            if not origin_ok(request):
                if settings.in_azure:
                    log.warning("refused %s %s: cross-site origin", request.method, template(request))
                else:
                    log.warning("refused %s %s: origin %r, host %r, forwarded %r", request.method, path, request.headers.get("origin"),
                                request.headers.get("host"), request.headers.get("x-forwarded-host"))
                return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        refused = await _social_gate(request) if any(path == p or path.startswith(p + "/") for p in SOCIAL_PATHS) else None
        if refused is not None:
            # The status of a social route never depends on its body: refusal or 404 come first.
            response = refused
        elif settings.in_azure:
            # One line per failure, naming the route pattern and the error type: never a line per request
            # (with a few members that would be an activity trace), never the path or an id.
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001
                log.error("500 on %s %s: %s", request.method, template(request), type(exc).__name__)
                response = JSONResponse({"detail": "Something went wrong."}, status_code=500)
        else:
            response = await call_next(request)
            if path.startswith("/api/"):
                # Play logs nothing per person (ADR 0010, P-13): which moment, circle, invitation or shared copy
                # a request named stays out of the log.
                logged = re.sub(r"/[a-z]+-[0-9a-f]{20}", "/-", path) if path.startswith("/api/play/") else path
                log.info("%s %s -> %s in %d ms", request.method, logged, response.status_code, (time.monotonic() - started) * 1000)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        # The microphone is on for this site only, so the learner can say an answer out loud.
        # Everything else stays off. See docs/adr/0001-microphone-for-spoken-answers.md.
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(), payment=(), usb=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if settings.in_azure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        # Media ids are content hashes and the route is owner-only, so a video may be cached privately;
        # everything else must not be.
        if request.url.path == "/" or (request.url.path.startswith("/api/") and not request.url.path.startswith("/api/media/")):
            response.headers["Cache-Control"] = "no-store"
        # The service worker and the manifest (ADR 0013): a new build is picked up at the next visit, and the
        # worker, served from /static, may control the whole site. It caches nothing.
        if path in ("/static/sw.js", "/static/manifest.webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
            if path == "/static/sw.js":
                response.headers["Service-Worker-Allowed"] = "/"
        if feed:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    @app.exception_handler(UserError)
    async def user_error(request: Request, exc: UserError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Refused)
    async def refused(request: Request, exc: Refused):
        # A reservation that did not fit: nothing was recorded and nothing was paid (ADR 0009, "Money").
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(Abandoned)
    async def abandoned(request: Request, exc: Abandoned):
        # The learner's data was deleted (or the record started over) while this request ran: it wrote
        # nothing more (ADR 0009, generation-checked writes).
        return JSONResponse({"detail": {"code": "deleted", "message": "Your data was deleted while this ran."}}, status_code=409)

    # ---------------------------------------------------------------- public

    @app.get("/healthz")
    def healthz() -> dict:
        """Open to anyone, so it says only whether the self-test passed and which build runs; the
        per-check results and their time are for the signed-in owner at /api/diag."""
        return selftest.public()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(index_html)

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    # ---------------------------------------------------------------- signed in: the five route classes
    # (ADR 0009). With team mode off every class is today's owner-only sign-in.

    Me = Depends(current_active_learner)        # learner: every operational route
    Exit = Depends(current_member_any)          # learner_exit: me, notice, export, delete, leave
    ExitSeat = Depends(current_exit_seat)
    Admin = Depends(current_admin)              # admin: the owner only
    AdminSeat = Depends(current_admin_seat)
    Who = Depends(current_principal)            # signed_in_non_member: any signed-in user of the tenant

    def _enrolled(learner: Learner, pid: str) -> None:
        """A package this learner is not enrolled in answers like one that does not exist (ADR 0009)."""
        dojo.check_enrolled(learner, pid)

    def _no_help_during_exam(learner: Learner, what: str) -> None:
        """A timed practice exam is taken without help, so while one is open Dojo shows nothing that
        teaches: no lesson, no audio or video, no quick questions, no hint, no feedback, no coach - not
        even one of its earlier answers, and not the export, which carries all of that in one file.
        Recording a lesson view is the one thing that is not refused: exposure that really happened is
        written down. Refusing is the honest half; the other half is in ExamRoom._conditions, which
        records the conditions as unknown if a teaching event lands inside the attempt anyway."""
        if room.open_attempt(learner):
            raise HTTPException(409, f"A timed practice exam is running. {what} until it ends.")

    def _only_this_exam(learner: Learner, aid: str) -> None:
        """An earlier exam's results page shows its answers and rationales, which is teaching. While an
        exam is open, the only exam you can open is that one."""
        running = room.open_attempt(learner)
        if running and running["id"] != aid:
            raise HTTPException(409, "A timed practice exam is running. Your earlier results are closed "
                                     "until it ends.")

    @app.get("/api/me")
    def me(seat: Seat = ExitSeat) -> dict:
        learner = seat.learner
        row = seat.row
        if seat.admin:
            od = team.owner_doc()
            if settings.team and seat.principal and seat.principal.name and od.get("entra_name") != seat.principal.name:
                # The operator's name for the notice members read ("who runs this Dojo"). Under the lock, so
                # the lab surrogate written beside it (ADR 0009, "Labs") is never lost to a concurrent write.
                with store.lock:
                    store.write("team", "owner", value={**team.owner_doc(), "entra_name": seat.principal.name})
            display = od.get("display_name") or ""
        else:
            display = row.get("display_name") or ""
        status = "active" if seat.admin else team.status_of(row)
        out = {"name": learner.name, "local": settings.local, "build": settings.build,
               "role": "admin" if seat.admin else "learner", "team": settings.team, "labs": labs is not None,
               "display_name": display,
               "status": status, "removed_at": None if seat.admin else (row.get("removed_at") or None),
               "notice": {"version": NOTICE_VERSION, "required": not seat.admin,
                          "acknowledged": seat.admin or (row.get("notice") or {}).get("version") == NOTICE_VERSION},
               "banner": team.banner(seat), "renew_by": None if seat.admin else team.renew_by(row),
               "enrolled": dojo.enrolled(learner) if (status == "active" or seat.admin) else [],
               "packages": [{"id": p["meta"]["id"], "exam": p["meta"]["exam"], "title": p["meta"]["title"],
                             "added": bool(p["meta"].get("added"))} for p in packages.by_id.values()]}
        if status == "active" or seat.admin:
            out["profile"] = dojo.profile(learner)
        return out

    # ---------------------------------------------------------------- Team Dojo (ADR 0009)

    @app.get("/api/access")
    def access_status(seat: Seat = Who) -> dict:
        """Whether this signed-in person is a member, and the state of their request if not."""
        return team.access(seat)

    @app.post("/api/access/request")
    def access_request(body: AccessRequest, seat: Seat = Who) -> dict:
        if not settings.team:
            raise HTTPException(409, "This Dojo has no members.")
        return team.request_access(seat, body.used_before)

    @app.get("/api/notice")
    def notice(seat: Seat = ExitSeat) -> dict:
        return notice_view(team, dojo.models("author", "gate", "grader"), labs=labs is not None)

    @app.post("/api/notice/ack")
    def notice_ack(body: NoticeAck, seat: Seat = ExitSeat) -> dict:
        return team.acknowledge(seat, body.version)

    @app.get("/api/help")
    def help_guide(learner: Learner = Exit) -> dict:
        """The user guide, docs/user-guide.md, rendered and escaped. It holds nothing personal, so a member
        behind the notice, a pause or an ended membership can read it too."""
        try:
            return helpdoc.guide()
        except OSError:
            raise HTTPException(503, "The user guide is missing from this build.")

    @app.put("/api/me/display-name")
    def display_name(body: DisplayName, seat: Seat = ExitSeat) -> dict:
        if not seat.admin and seat.row.get("status") != "active":
            raise refuse(403, "removed", "Your access to this Dojo ended.")
        return team.set_display_name(seat, body.name)

    @app.post("/api/me/renew")
    def renew(seat: Seat = ExitSeat) -> dict:
        return team.renew(seat)

    @app.post("/api/team/leave")
    def leave(body: LeaveRequest, seat: Seat = ExitSeat) -> dict:
        if seat.admin:
            raise HTTPException(409, "The owner cannot leave this Dojo.")
        return team.leave(seat)

    @app.get("/api/team/members")
    def team_members(learner: Learner = Admin) -> dict:
        return team.members_view()

    @app.get("/api/team/budget")
    def team_budget(learner: Learner = Admin) -> dict:
        """The allowances and the cap, and cost totals under the 5-member rule. Never a member's number."""
        return budget.admin_view()

    @app.put("/api/team/budget")
    def team_budget_set(body: BudgetSettings, learner: Learner = Admin) -> dict:
        return budget.set_limits(body.allowances, body.cap)

    @app.get("/api/me/allowance")
    def my_allowance(learner: Learner = Me) -> dict:
        """What is left of today's allowances: the learner's own, seen only by them."""
        return budget.left_today(learner)

    @app.post("/api/team/requests/{req_id}/approve")
    def team_approve(req_id: str, learner: Learner = Admin) -> dict:
        return team.approve(_check_id(req_id))

    @app.post("/api/team/requests/{req_id}/decline")
    def team_decline(req_id: str, learner: Learner = Admin) -> dict:
        return team.decline(_check_id(req_id))

    @app.post("/api/team/requests/{req_id}/rebind")
    def team_rebind(req_id: str, body: RebindRequest, learner: Learner = Admin) -> dict:
        return team.rebind(_check_id(req_id), _check_id(body.member), body.checked)

    @app.post("/api/team/members/{member_id}/remove")
    def team_remove(member_id: str, body: RemoveMember, learner: Learner = Admin) -> dict:
        if member_id == "owner":
            raise HTTPException(409, "The owner cannot be removed.")
        return team.remove(_check_id(member_id))

    @app.post("/api/team/pause")
    def team_pause(learner: Learner = Admin) -> dict:
        return team.pause(True)

    @app.post("/api/team/resume")
    def team_resume(body: ResumeRequest | None = None, learner: Learner = Admin) -> dict:
        body = body or ResumeRequest()
        return team.pause(False, body.without_database, body.no_restore)

    @app.get("/api/team/end")
    def team_end_view(learner: Learner = Admin) -> dict:
        return team.end_emails()

    @app.post("/api/team/end/told")
    def team_end_told(body: EndTold, learner: Learner = Admin) -> dict:
        return team.end_told(body.confirmed)

    @app.post("/api/team/end")
    def team_end(body: EndDate, learner: Learner = Admin) -> dict:
        return team.end_on(body.date)

    @app.post("/api/team/end/cancel")
    def team_end_cancel(learner: Learner = Admin) -> dict:
        return team.end_cancel()

    @app.get("/api/settings")
    def get_settings(learner: Learner = Me) -> dict:
        return dojo.profile(learner)

    @app.put("/api/settings")
    def put_settings(body: SettingsRequest, learner: Learner = Me) -> dict:
        return dojo.save_profile(learner, body.model_dump())

    # ---------------------------------------------------------------- the plan (Plan page, Today)
    # A plan change is the learner's own data (plan.json), never an event in the Record.

    @app.get("/api/plan")
    def plan_view(week: str | None = None, learner: Learner = Me) -> dict:
        return planner.view(learner, week)

    @app.get("/api/plan/today")
    def plan_today(learner: Learner = Me) -> dict:
        return planner.today(learner)

    @app.put("/api/plan/rules")
    def plan_rules(body: PlanRules, week: str | None = None, learner: Learner = Me) -> dict:
        planner.set_rules(learner, body.model_dump())
        return planner.view(learner, week)

    @app.post("/api/plan/sessions/{session_id}/move")
    def plan_move(session_id: str, body: PlanMove, week: str | None = None, learner: Learner = Me) -> dict:
        planner.move(learner, _check_id(session_id), body.start)
        return planner.view(learner, week)

    @app.post("/api/plan/sessions/{session_id}/skip")
    def plan_skip(session_id: str, week: str | None = None, learner: Learner = Me) -> dict:
        planner.skip(learner, _check_id(session_id))
        return planner.view(learner, week)

    @app.post("/api/plan/sessions/{session_id}/pin")
    def plan_pin(session_id: str, body: PlanPin, week: str | None = None, learner: Learner = Me) -> dict:
        planner.pin(learner, _check_id(session_id), body.pinned)
        return planner.view(learner, week)

    # ---------------------------------------------------------------- Play, the personal layer (ADR 0010)
    # Private to the learner. Points, the rhythm and moments are derived from the Record on each read;
    # only the learner's choices are stored (learners/<key>/social/play.json). Nothing here is logged per person.

    @app.get("/api/play")
    def play_view(learner: Learner = Me) -> dict:
        return play.view(learner)

    @app.post("/api/play/join")
    def play_join(body: PlayJoin, learner: Learner = Me) -> dict:
        return play.join(learner, body.include_record, body.days)

    @app.put("/api/play/settings")
    def play_settings(body: PlaySettings, learner: Learner = Me) -> dict:
        return play.update(learner, body.days, body.rhythm_on, body.show_hidden)

    @app.delete("/api/play/moments/{moment}")
    def play_hide(moment: str, learner: Learner = Me) -> dict:
        return play.hide(learner, _check_id(moment))

    @app.post("/api/play/leave")
    def play_leave(learner: Learner = Me) -> dict:
        return play.leave(learner)

    @app.post("/api/play/offer/dismiss")
    def play_dismiss(learner: Learner = Me) -> dict:
        return play.dismiss_offer(learner)

    @app.post("/api/passes")
    def report_pass(body: PassReport, learner: Learner = Me) -> dict:
        _enrolled(learner, body.package)
        return play.report_pass(learner, body.package, body.earned)

    # ---------------------------------------------------------------- Play, the social layer (ADR 0010)
    # Circles, the witness room and pass celebrations. Every route answers 404 unless team mode is on and the
    # owner switched circles on; each acts only on the caller's own circles, invitations and shared copies.

    @app.get("/api/play/social")
    def social_view(learner: Learner = Me) -> dict:
        return social.view(learner)

    @app.post("/api/play/social/join")
    def social_join(body: SocialJoin, learner: Learner = Me) -> dict:
        return social.opt_in(learner, body.version, body.invitable)

    @app.put("/api/play/social")
    def social_settings(body: SocialSettings, learner: Learner = Me) -> dict:
        social.gate()
        if body.invitable is None:
            raise HTTPException(400, "Nothing to change.")
        return social.set_invitable(learner, body.invitable)

    @app.post("/api/play/social/leave")
    def social_leave(learner: Learner = Me) -> dict:
        return social.leave_social(learner)

    @app.post("/api/play/circles")
    def circle_new(body: CircleNew, learner: Learner = Me) -> dict:
        return social.create(learner, body.name, body.people)

    @app.post("/api/play/circles/{circle}/invite")
    def circle_invite(circle: str, body: CircleInvite, learner: Learner = Me) -> dict:
        social.gate()
        return social.invite(learner, _check_id(circle), body.people)

    @app.post("/api/play/circles/{circle}/leave")
    def circle_leave(circle: str, learner: Learner = Me) -> dict:
        social.gate()
        return social.leave_circle(learner, _check_id(circle))

    @app.put("/api/play/circles/{circle}/goal")
    def circle_goal(circle: str, body: CircleGoal, learner: Learner = Me) -> dict:
        social.gate()
        return social.set_goal(learner, _check_id(circle), body.points)

    @app.post("/api/play/circles/{circle}/preview")
    def circle_preview(circle: str, body: CircleShare, learner: Learner = Me) -> dict:
        social.gate()
        return social.preview(learner, _check_id(circle), body.moment)

    @app.post("/api/play/circles/{circle}/share")
    def circle_share(circle: str, body: CircleShare, learner: Learner = Me) -> dict:
        social.gate()
        return social.share(learner, _check_id(circle), body.moment)

    @app.delete("/api/play/shares/{share}")
    def share_withdraw(share: str, learner: Learner = Me) -> dict:
        social.gate()
        return social.withdraw(learner, _check_id(share))

    @app.post("/api/play/invitations/{invite}/accept")
    def invitation_accept(invite: str, learner: Learner = Me) -> dict:
        social.gate()
        return social.accept(learner, _check_id(invite))

    @app.post("/api/play/invitations/{invite}/decline")
    def invitation_decline(invite: str, learner: Learner = Me) -> dict:
        social.gate()
        return social.decline(learner, _check_id(invite))

    # The effort board (ADR 0010 section 5): the caller's own place on it, never anyone else's.

    @app.get("/api/play/board")
    def board_view(learner: Learner = Me) -> dict:
        return board.view(learner)

    @app.post("/api/play/board/join")
    def board_join(learner: Learner = Me) -> dict:
        return board.join(learner)

    @app.post("/api/play/board/leave")
    def board_leave(learner: Learner = Me) -> dict:
        return board.leave(learner)

    @app.put("/api/play/board")
    def board_settings(body: BoardSettings, learner: Learner = Me) -> dict:
        social.gate()
        if body.mine_only is None:
            raise HTTPException(400, "Nothing to change.")
        return board.set_mine_only(learner, body.mine_only)

    # Duels (ADR 0010 section 6): each route acts only on a duel the caller plays in.

    @app.get("/api/play/duels")
    def duels_view(learner: Learner = Me) -> dict:
        return duels.view(learner)

    @app.post("/api/play/duels")
    def duel_new(body: DuelNew, learner: Learner = Me) -> dict:
        social.gate()
        return duels.invite(learner, body.person, body.package, body.domain)

    @app.post("/api/play/duels/{duel}/accept")
    def duel_accept(duel: str, learner: Learner = Me) -> dict:
        social.gate()
        return duels.accept(learner, _check_id(duel))

    @app.post("/api/play/duels/{duel}/decline")
    def duel_decline(duel: str, learner: Learner = Me) -> dict:
        social.gate()
        return duels.decline(learner, _check_id(duel))

    @app.post("/api/play/duels/{duel}/open")
    def duel_open(duel: str, learner: Learner = Me) -> dict:
        social.gate()
        _no_help_during_exam(learner, "Quick questions show their reasons straight away, so they are closed")
        return duels.open(learner, _check_id(duel))

    @app.get("/api/team/play")
    def team_play(learner: Learner = Admin) -> dict:
        """The owner's admin role sees only whether circles are on (ADR 0010, P-6)."""
        return social.switch_view()

    @app.put("/api/team/play")
    def team_play_set(body: PlaySwitch, learner: Learner = Admin) -> dict:
        return social.set_switch(body.on)

    @app.get("/api/today/{pid}")
    def today(pid: str, learner: Learner = Me) -> dict:
        _enrolled(learner, pid)
        return dojo.today(learner, pid)

    @app.get("/api/packages/{pid}")
    def package(pid: str, learner: Learner = Me) -> dict:
        _enrolled(learner, pid)
        return dojo.package_view(learner, pid)

    @app.get("/api/skills/{pid}/{sid}")
    def skill(pid: str, sid: str, learner: Learner = Me) -> dict:
        _enrolled(learner, pid)
        view = dojo.skill_view(learner, pid, sid)
        view["labs"] = [{"at": e["at"], **e["data"]} for e in store.events(learner.key)
                        if e["type"] == "lab.checked" and e["data"].get("skill") == sid
                        and e["data"].get("package") == pid][-20:]
        return view

    def _labs_on() -> None:
        if labs is None:
            raise HTTPException(404, LABS_OFF)

    @app.get("/api/labs")
    def lab_list(learner: Learner = Me) -> list[dict]:
        _labs_on()
        _no_help_during_exam(learner, "Labs are closed")
        return [{"id": lab, "title": spec["title"], "skill": spec["skill"]} for lab, spec in LABS.items()]

    def _lab(lab: str, learner: Learner) -> LabUser | None:
        """The learner's own lab schemas (ADR 0009, "Labs"): None is the owner, who keeps today's schema
        names; a member works in schemas named by a random surrogate kept in their membership row."""
        _labs_on()
        _no_help_during_exam(learner, "Labs are closed")
        if lab not in LABS:
            raise HTTPException(404, "Lab not found.")
        _enrolled(learner, "dp-800")
        return None if learner.key == owner.key else LabUser(team.lab_surrogate(learner.key))

    @app.get("/api/labs/{lab}")
    def lab_detail(lab: str, learner: Learner = Me) -> dict:
        who = _lab(lab, learner)
        store.append_event(learner.key, "lab.opened", {"package": "dp-800", "skill": LABS[lab]["skill"], "lab": lab})
        return {**labs.detail(lab, who, shared=settings.team), "drift": drift.lab_state(lab)}

    @app.post("/api/labs/{lab}/run")
    def lab_run(lab: str, body: LabSql, learner: Learner = Me) -> dict:
        who = _lab(lab, learner)
        if re.search(r"\b(DROP|TRUNCATE|DELETE|ALTER)\b", body.sql, re.IGNORECASE) and not body.confirm_destructive:
            raise HTTPException(409, "This SQL may remove or change lab objects. Confirm the action before running it.")
        try:
            with budget.action(learner, "lab", "dp-800", amount=0.0):
                result = labs.operation(lab, "run", who, body.sql)
        except UserError:
            raise
        except Exception as exc:
            log.warning("lab run unavailable: %s; %s", type(exc).__name__, sql_error_reason(exc))
            raise HTTPException(503, "Waking the database, about a minute. Try again shortly."
                                if is_waking(exc) else "The database is unavailable. Try again.") from exc
        store.append_event(learner.key, "lab.ran", {"package": "dp-800", "skill": LABS[lab]["skill"],
                           "lab": lab, "local_demo": lab_demo})
        return result

    @app.post("/api/labs/{lab}/hint")
    def lab_hint(lab: str, body: LabHint, learner: Learner = Me) -> dict:
        _lab(lab, learner)
        if body.index >= len(LABS[lab]["hints"]):
            raise HTTPException(404, "Hint not found.")
        store.append_event(learner.key, "lab.hint", {"package": "dp-800", "skill": LABS[lab]["skill"],
                           "lab": lab, "index": body.index})
        return {"hint": LABS[lab]["hints"][body.index]}

    @app.post("/api/labs/{lab}/check")
    def lab_check(lab: str, learner: Learner = Me) -> dict:
        who = _lab(lab, learner)
        try:
            result = labs.operation(lab, "check", who)
        except UserError:
            raise
        except Exception as exc:
            log.warning("lab check unavailable: %s; %s", type(exc).__name__, sql_error_reason(exc))
            raise HTTPException(503, "Waking the database, about a minute. Try again shortly."
                                if is_waking(exc) else "The database is unavailable. Try again.") from exc
        if not lab_demo:
            events = store.events(learner.key)
            previous = next((i for i in range(len(events) - 1, -1, -1) if events[i]["type"] == "lab.reset"
                             and events[i]["data"].get("lab") == lab), -1)
            current = [e for e in events[previous + 1:] if e["data"].get("lab") == lab]
            recent_lesson = next((e for e in reversed(events) if e["type"] == "lesson.viewed"
                                  and e["data"].get("skill") == LABS[lab]["skill"]
                                  and e["data"].get("package") == "dp-800"), None)
            minutes = max(0, int((utcnow() - parse_iso(recent_lesson["at"])).total_seconds() / 60) - 30) if recent_lesson else None
            hints = sum(e["type"] == "lab.hint" for e in current)
            attempts = sum(e["type"] == "lab.ran" for e in current)
            asked = sum(e["type"] == "ask.answered" and e["data"].get("skill") == LABS[lab]["skill"]
                        and e["data"].get("package") == "dp-800" for e in events[previous + 1:])
            store.append_event(learner.key, "lab.checked", {"package": "dp-800", "skill": LABS[lab]["skill"],
                               "lab": lab, "passed": result["passed"], "hints_shown": hints,
                               "coach_answers": asked, "runs": attempts, "minutes_since_lesson": minutes,
                               "evidence": "practice only; no pips"})
            result["conditions"] = {"hints_shown": hints, "coach_answers": asked, "runs": attempts,
                                    "minutes_since_lesson": minutes, "pips": False}
        return result

    @app.post("/api/labs/{lab}/reset")
    def lab_reset(lab: str, learner: Learner = Me) -> dict:
        who = _lab(lab, learner)
        try:
            result = labs.operation(lab, "reset", who)
        except UserError:
            raise
        except Exception as exc:
            log.warning("lab reset unavailable: %s; %s", type(exc).__name__, sql_error_reason(exc))
            if is_waking(exc):
                raise HTTPException(503, "Waking the database, about a minute. No reset was recorded.") from exc
            from mssql_python import Error as SqlDriverError
            if isinstance(exc, SqlDriverError):
                raise HTTPException(422, f"Reset failed: {str(exc)[:500]}") from exc
            raise HTTPException(503, "The database is unavailable. No reset was recorded.") from exc
        store.append_event(learner.key, "lab.reset", {"package": "dp-800", "skill": LABS[lab]["skill"], "lab": lab,
                           "local_demo": lab_demo})
        return result

    @app.post("/api/lessons")
    def new_lesson(body: LessonRequest, learner: Learner = Me) -> dict:
        packages.skill(body.package, body.skill)
        _enrolled(learner, body.package)
        _no_help_during_exam(learner, "Lessons are closed")
        if learner.key != owner.key and dojo.lessons_for(body.package, body.skill):
            # A lesson is shared: writing it again replaces it for everyone, so that is the owner's (ADR 0009).
            raise HTTPException(409, "This skill already has a lesson. Open it from the skill page.")
        with budget.action(learner, "lesson", body.package):
            return dojo.jobs.start(learner, "lesson", dojo.make_lesson, learner, body.package, body.skill)

    @app.post("/api/lessons/{lesson_id}/rewrite")
    def rewrite_lesson(lesson_id: str, learner: Learner = Admin) -> dict:
        """The owner asks for this skill's lesson to be written again. It becomes the newest lesson for
        everyone, so it is admin-only; the first lesson of a skill is POST /api/lessons."""
        _no_help_during_exam(learner, "Lessons are closed")
        meta = next((m for m in dojo.lesson_index() if m["id"] == _check_id(lesson_id)), None)
        if meta is None:
            raise HTTPException(404, "Lesson not found.")
        return dojo.jobs.start(learner, "lesson", dojo.make_lesson, learner, meta["package"], meta["skill"])

    @app.get("/api/lessons/{lesson_id}")
    def get_lesson(lesson_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Lessons are closed")
        return dojo.lesson_view(_check_id(lesson_id))

    @app.post("/api/lessons/{lesson_id}/viewed")
    def lesson_viewed(lesson_id: str, body: ViewedRequest, learner: Learner = Me) -> dict:
        # Not closed during an exam, on purpose: a lesson tab that was already open can start playing
        # after the clock does. Refusing the event would lose real exposure and leave the attempt
        # looking like exam conditions. Dojo records it, and ExamRoom._conditions then makes the
        # attempt's conditions unknown.
        return dojo.lesson_viewed(learner, _check_id(lesson_id), body.modality, narration=body.narration)

    @app.post("/api/lessons/{lesson_id}/audio")
    def lesson_audio(lesson_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Lessons are closed")
        lesson = dojo.lesson(_check_id(lesson_id))
        with budget.action(learner, "audio", (lesson or {}).get("package")):
            return dojo.jobs.start(learner, "audio", dojo.lesson_audio, learner, lesson_id)

    @app.post("/api/lessons/{lesson_id}/video")
    def lesson_video(lesson_id: str, learner: Learner = Admin) -> dict:
        """Film every slide of this lesson. Asking again while it runs returns the same job."""
        _no_help_during_exam(learner, "Lessons are closed")
        dojo.lesson(_check_id(lesson_id))
        running = dojo.jobs.existing(learner, "video", lesson_id)
        return running or dojo.jobs.start(learner, "video", dojo.lesson_video, learner, lesson_id, ref=lesson_id)

    @app.get("/api/media/{media_id}")
    def media(media_id: str, learner: Learner = Me) -> Response:
        """Narration audio and slide videos. FileResponse answers Range requests with 206, which is
        what a browser needs to seek in a video."""
        _no_help_during_exam(learner, "Lesson audio and video are closed")
        if MEDIA_ID.match(media_id):
            name, media_type = media_id + ".mp3", "audio/mpeg"
        elif VIDEO_ID.match(media_id):
            name, media_type = media_id + AVATAR_SUFFIX, AVATAR_CONTENT_TYPE
        else:
            raise HTTPException(404, "Not found.")
        # A learner's own check-question audio first (ADR 0009), then the shared narration and video.
        own = dojo.store.path("learners", learner.key, "media", name)
        path = own if own.is_file() else dojo.store.path("media", name)
        if not path.is_file():
            raise HTTPException(404, "Not found.")
        if media_type == AVATAR_CONTENT_TYPE:
            dojo.avatar.touch(media_id)  # videos are evicted least-recently-served first
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/items")
    def new_item(body: ItemRequest, learner: Learner = Me) -> dict:
        packages.skill(body.package, body.skill)
        _enrolled(learner, body.package)
        _no_help_during_exam(learner, "New items are closed")
        with budget.action(learner, "item", body.package):
            return dojo.jobs.start(learner, "item", dojo.make_item, learner, body.package, body.skill, body.mode)

    @app.get("/api/items/{item_id}")
    def get_item(item_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Your earlier items, with their feedback, are closed")
        item_id = _check_id(item_id)
        return {**dojo.item_view(learner, item_id), "podcast_question": podcast.question(learner, item_id)}

    @app.post("/api/items/{item_id}/hint")
    def hint(item_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Hints are closed")
        item_id = _check_id(item_id)
        with budget.action(learner, "hint", dojo.item(learner, item_id).get("package"), amount=0.0):
            return dojo.hint(learner, item_id)

    @app.post("/api/items/{item_id}/answer")
    def answer(item_id: str, body: AnswerRequest, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Dojo holds back feedback on your answers")
        item_id = _check_id(item_id)
        dojo.refuse_withheld(learner, item_id)  # held back after a source change: refused before anything is recorded
        # Reserved before anything is recorded: an answer is never stored and then left unjudged for lack
        # of money (ADR 0009, "Money"). The item's exam goes with it, for the pooled sums.
        with budget.action(learner, "answer", dojo.item(learner, item_id).get("package")):
            podcast.declare(learner, item_id, body.podcast_heard)  # the reply goes in first; an answer that needs one is refused without it
            return dojo.submit(learner, item_id, body.text, body.elapsed_s, body.input,
                               body.edited_before_send, body.receipts, body.confidence)

    @app.post("/api/items/{item_id}/grade")
    def regrade(item_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Dojo holds back feedback on your answers")
        item_id = _check_id(item_id)
        with budget.action(learner, "answer", dojo.item(learner, item_id).get("package")):
            return dojo.regrade(learner, item_id)

    @app.post("/api/items/{item_id}/judge-again")
    def judge_again(item_id: str, learner: Learner = Me) -> dict:
        # Once, on the learner's request, for a judgement made with older judging rules (ADR 0015).
        _no_help_during_exam(learner, "Dojo holds back feedback on your answers")
        item_id = _check_id(item_id)
        with budget.action(learner, "answer", dojo.item(learner, item_id).get("package")):
            return dojo.judge_again(learner, item_id)

    @app.post("/api/items/{item_id}/dispute")
    def dispute(item_id: str, body: DisputeRequest, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Dojo holds back feedback on your answers")
        return dojo.dispute(learner, _check_id(item_id), body.reason)

    @app.post("/api/rehearsals")
    def new_rehearsal(body: RehearsalRequest, learner: Learner = Me) -> dict:
        packages.get(body.package)
        _enrolled(learner, body.package)
        _no_help_during_exam(learner, "Quick questions show their reasons straight away, so they are closed")
        with budget.action(learner, "rehearsal", body.package, batches=max(1, -(-body.count // 4))):
            return dojo.jobs.start(learner, "rehearsal", dojo.make_rehearsal, learner, body.package, body.count)

    @app.get("/api/rehearsals/{rid}")
    def get_rehearsal(rid: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Quick questions show their reasons straight away, so they are closed")
        return dojo.rehearsal_view(learner, _check_id(rid))

    @app.get("/api/rehearsals")
    def list_rehearsals(package: str, learner: Learner = Me) -> list:
        packages.get(package)
        _enrolled(learner, package)
        return dojo.rehearsals_for(learner, package)

    @app.post("/api/rehearsals/{rid}/answer")
    def answer_mcq(rid: str, body: McqAnswer, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "A rehearsal shows its rationale straight away, so it is closed")
        return dojo.answer_mcq(learner, _check_id(rid), body.index, body.choice, body.confidence)

    @app.post("/api/ask")
    def ask(body: AskRequest, learner: Learner = Me) -> dict:
        packages.skill(body.package, body.skill)
        _enrolled(learner, body.package)
        _no_help_during_exam(learner, "The coach is closed")
        with budget.action(learner, "ask", body.package):
            return dojo.jobs.start(learner, "ask", dojo.ask, learner, body.package, body.skill, body.question)

    # ---------------------------------------------------------------- the 3-minute check

    @app.get("/api/checks")
    def check_plan(package: str, recall: bool = False, learner: Learner = Me) -> dict:
        """Which skills a check would ask about now, and why. Nothing is written yet."""
        packages.get(package)
        _enrolled(learner, package)
        _no_help_during_exam(learner, "The 3-minute check is closed")
        return checks.plan(learner, package, recall)

    @app.post("/api/checks")
    def new_check(body: CheckRequest, learner: Learner = Me) -> dict:
        """Start a check: the questions are written in the background, one job slot at a time."""
        packages.get(body.package)
        _enrolled(learner, body.package)
        _no_help_during_exam(learner, "The 3-minute check is closed")
        with budget.action(learner, "check", body.package, batches=CHECK_LIMIT):
            return checks.create(learner, body.package, body.recall)

    # ---------------------------------------------------------------- know what you know (ADR 0011)

    @app.get("/api/calibration")
    def calibration(package: str, learner: Learner = Me) -> dict:
        """How sure you were next to how often you were right. It links to lessons and shows earlier
        questions, so it is closed while a timed exam runs."""
        packages.get(package)
        _enrolled(learner, package)
        _no_help_during_exam(learner, "Your earlier answers and the lessons are closed")
        return dojo.calibration(learner, package)

    @app.post("/api/lessons/{lesson_id}/selfcheck")
    def lesson_selfcheck(lesson_id: str, body: SelfCheckRequest, learner: Learner = Me) -> dict:
        """The learner's own mark on a "Check yourself" question: a self-report, never evidence."""
        _no_help_during_exam(learner, "Lessons are closed")
        return dojo.selfcheck(learner, _check_id(lesson_id), body.index, body.confidence, body.matched)

    @app.get("/api/checks/{cid}")
    def get_check(cid: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "The 3-minute check is closed")
        view = checks.view(learner, _check_id(cid))
        for q in view["questions"]:
            # A check answer goes through the answer route, which refuses it without a reply when the podcast
            # question applies. So each question still to answer carries it, as the answer form does.
            q["podcast_question"] = podcast.question(learner, q["item"]) if q["state"] == "ready" and not q["answered"] else None
        return view

    @app.post("/api/checks/{cid}/end")
    def end_check(cid: str, learner: Learner = Me) -> dict:
        """Close the session. The answers were already sent one by one; this only stops it coming back."""
        return checks.end(learner, _check_id(cid))

    # ---------------------------------------------------------------- the phone (ADR 0013)
    # Nudges carry no teaching, so their settings stay open during a timed exam; the nudger itself sends
    # nothing while one runs. The 5-minute session opens lessons and checks, so it is closed then.

    @app.get("/api/five")
    def five_view(learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "The 5-minute session is closed")
        return five.view(learner)

    @app.post("/api/five/start")
    def five_start(body: FiveStart | None = None, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "The 5-minute session is closed")
        pid = five.package(learner)
        with budget.action(learner, "check", pid, batches=FIVE_RECALLS):
            return five.start(learner, end_open=bool(body and body.end_open), pid=pid)

    @app.get("/api/push")
    def push_view(learner: Learner = Me) -> dict:
        return nudges.view(learner)

    @app.put("/api/push/settings")
    def push_settings(body: PushSettings, learner: Learner = Me) -> dict:
        return nudges.save_settings(learner, body.time, body.days)

    @app.post("/api/push/devices")
    def push_device_add(body: PushDevice, learner: Learner = Me) -> dict:
        return nudges.add_device(learner, body.endpoint, body.p256dh, body.auth, body.label)

    @app.delete("/api/push/devices/{device_id}")
    def push_device_remove(device_id: str, learner: Learner = Me) -> dict:
        return nudges.remove_device(learner, _check_id(device_id, r"^dev-[0-9a-f]{20}$"))

    @app.delete("/api/push")
    def push_off(learner: Learner = Me) -> dict:
        return nudges.off(learner)

    @app.post("/api/push/test")
    def push_test(body: PushTest, learner: Learner = Me) -> dict:
        return nudges.test(learner, body.device)

    @app.post("/api/transcribe")
    async def transcribe(request: Request, item: str = "ask", learner: Learner = Me) -> dict:
        """Write down what the learner just said. This answers inside the request instead of starting a
        job, so it is never queued behind filming. Dojo keeps neither the audio nor the text: the words
        go straight back to the browser, where the learner reads them and decides what to send. The
        receipt is the one thing Dojo remembers, and only so one answer can say a recording was really
        written down for it. `item` says which answer the recording is for, or "ask" for the coach box."""
        answer_for = item if item == "ask" else _check_id(item)
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type not in AUDIO_TYPES:
            raise HTTPException(415, TRANSCRIBE_ERRORS[415])
        declared = request.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise HTTPException(413, TRANSCRIBE_ERRORS[413])
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > MAX_AUDIO_BYTES:  # stop reading a huge body instead of buffering it
                raise HTTPException(413, TRANSCRIBE_ERRORS[413])
        if not budget.active:   # team mode off: today's path, unchanged
            heard = await run_in_threadpool(dojo.speech.transcribe, bytes(audio), content_type)
            return {**heard, "receipt": dojo.receipts.issue(learner.key, answer_for)}
        # Team mode: Dojo decodes the recording itself and sends Speech its own WAV, so the reservation is
        # for a length Dojo counted, never for one the upload claims (ADR 0009, "Money").
        if len(audio) > NORMALIZE_MAX_BYTES:
            raise HTTPException(413, TRANSCRIBE_ERRORS[413])
        try:
            wav, seconds = await run_in_threadpool(normalize, bytes(audio), content_type)
        except AudioRefused as e:
            raise HTTPException(e.status, str(e)) from None

        def heard_for_this_learner() -> dict:
            amount = (seconds + 1.0) * budget.meter_rate("transcription")
            with budget.action(learner, "transcribe", amount=amount):
                return dojo.speech.transcribe(wav, "audio/wav", seconds=seconds)

        heard = await run_in_threadpool(carried(heard_for_this_learner))
        return {**heard, "receipt": dojo.receipts.issue(learner.key, answer_for)}

    # ---------------------------------------------------------------- practice exams

    @app.post("/api/exams")
    def new_exam(body: ExamRequest, learner: Learner = Me) -> dict:
        packages.get(body.package)
        _enrolled(learner, body.package)
        if room.open_attempt(learner):
            raise HTTPException(409, "A timed practice exam is running. Finish it, or let its clock run "
                                     "out, before starting another.")
        if body.package in room.closing:   # answered before anything is reserved; create checks again under the lock
            raise HTTPException(409, "This exam was removed from this Dojo, so this practice exam cannot run.")
        # The build's first round at most, after what the learner's pool already holds (ADR 0009, "Money").
        first = budget.calls_amount(room.first_round(learner, body.package, body.length, body.style)) if budget.active else None
        with budget.action(learner, "exam", body.package, amount=first):
            doc = room.create(learner, body.package, body.length, body.style, body.open_book)
            job = dojo.jobs.start(learner, "exam", room.build, learner, doc["id"], ref=doc["id"])
        return {**job, "attempt": doc["id"], "wanted": doc["wanted"], "minutes": doc["minutes"], "spec": doc["spec"]}

    @app.get("/api/exams")
    def list_exams(package: str, learner: Learner = Me) -> dict:
        pkg = packages.get(package)
        _enrolled(learner, package)
        # One attempt is open at a time, and it closes everything that teaches whichever package it
        # belongs to, so the page is told which exam is running rather than only this package's.
        open_doc = room.open_attempt(learner)
        try:
            lengths, untimed = {length: exam_spec(pkg, length) for length in ("full", "short")}, None
        except UserError as e:
            lengths, untimed = None, str(e)   # an added exam with no stated duration, or a domain with no checked source
        # The exam format's mix, in questions per kind: a yes/no series counts its statements, a case study its questions.
        mixes = {k: {kind: n * exam_items.UNIT_SIZE.get(kind, 1) for kind, n in exam_items.mix(v["questions"]).items() if n}
                 for k, v in (lengths or {}).items()}
        return {"package": package, "attempts": room.list_for(learner, package),
                "running": {"id": open_doc["id"], "package": open_doc["package"]} if open_doc else None,
                "lengths": lengths, "untimed": untimed, "pool": room.pool_size(learner, package),
                "open_book": exam_open_book(pkg), "kind_labels": exam_items.LABELS, "mix": mixes}

    @app.get("/api/exams/{aid}")
    def get_exam(aid: str, learner: Learner = Me) -> dict:
        _only_this_exam(learner, _check_id(aid))
        return room.view(learner, _check_id(aid))

    @app.post("/api/exams/{aid}/start")
    def start_exam(aid: str, learner: Learner = Me) -> dict:
        return room.start(learner, _check_id(aid))

    @app.post("/api/exams/{aid}/answer")
    def answer_exam(aid: str, body: ExamAnswer, learner: Learner = Me) -> dict:
        if body.response is not None and len(json.dumps(body.response)) > 2000:
            raise HTTPException(400, "That answer is too long.")
        return room.answer(learner, _check_id(aid), body.index, body.choice, body.response, body.part, body.rev)

    @app.post("/api/exams/{aid}/visit")
    def visit_exam(aid: str, body: ExamVisit, learner: Learner = Me) -> dict:
        return room.visit(learner, _check_id(aid), body.index)

    @app.post("/api/exams/{aid}/learn")
    def learn_exam(aid: str, body: ExamLearn, learner: Learner = Me) -> dict:
        return room.learn(learner, _check_id(aid), body.event, body.seconds)

    @app.post("/api/exams/{aid}/flag")
    def flag_exam(aid: str, body: ExamFlag, learner: Learner = Me) -> dict:
        return room.flag(learner, _check_id(aid), body.index, body.flagged)

    @app.post("/api/exams/{aid}/submit")
    def submit_exam(aid: str, body: ExamSubmit | None = None, learner: Learner = Me) -> dict:
        body = body or ExamSubmit()
        return room.submit(learner, _check_id(aid), body.reason, body.podcast_heard)

    @app.post("/api/exams/{aid}/rationales")
    def reveal_exam(aid: str, learner: Learner = Me) -> dict:
        _only_this_exam(learner, _check_id(aid))
        return room.reveal(learner, _check_id(aid))

    @app.get("/api/readiness")
    def readiness(package: str, learner: Learner = Me) -> dict:
        """Read-only: a GET must never write to the Record. The page uses the POST below, because what
        it shows and what is kept have to be the same advice."""
        packages.get(package)
        _enrolled(learner, package)
        return {**advise(dojo, room, learner, package), "history": advice_log(dojo, learner, package)[:5]}

    @app.post("/api/readiness")
    def keep_readiness(body: ExamRequest, learner: Learner = Me) -> dict:
        """One calculation: it is stored and then returned, so the advice on the screen is the advice in
        the Record. If the storing fails the page shows the error, never unrecorded advice."""
        packages.get(body.package)
        _enrolled(learner, body.package)
        advice = advise(dojo, room, learner, body.package)
        kept = store_advice(dojo, learner, advice)
        return {**advice, "kept": kept, "history": advice_log(dojo, learner, body.package)[:5]}

    @app.post("/api/readiness/official")
    def official_taken(body: OfficialAssessment, learner: Learner = Me) -> dict:
        pkg = packages.get(body.package)
        _enrolled(learner, body.package)
        official = pkg["meta"].get("official_practice_assessment")
        if not official:
            raise HTTPException(409, "Dojo has no verified official practice assessment for this exam.")
        store.append_event(learner.key, "official_assessment.taken",
                           {"package": body.package, "url": official.get("url"), "assessment_id": official.get("assessment_id")})
        return {"package": body.package, "taken": True}

    @app.get("/api/ask/{ask_id}")
    def get_ask(ask_id: str, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "Your earlier answers from the coach are closed")
        return dojo.ask_view(learner, _check_id(ask_id))

    @app.get("/api/asks")
    def list_asks(package: str, learner: Learner = Me) -> list:
        packages.get(package)
        _enrolled(learner, package)
        return dojo.asks_for(learner, package)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, learner: Learner = Me) -> dict:
        return dojo.jobs.get(learner, _check_id(job_id))

    @app.get("/api/quality")
    def quality(learner: Learner = Me) -> list:
        # A blocked sentence is still lesson text about the exam's subject: closed like the lesson itself.
        _no_help_during_exam(learner, "What was blocked is closed")
        return dojo.quality_log(learner)

    @app.get("/api/studio/voices")
    def studio_voices(learner: Learner = Admin) -> dict:
        # The worst slides quote approved lesson words, which teach.
        _no_help_during_exam(learner, "What the voices said is closed")
        return checker.summary()

    @app.get("/api/studio/costs")
    def studio_costs(learner: Learner = Admin) -> dict:
        # "Reservation bound exceeded": a call cost more than Dojo reserved for it (role and call kind only).
        view = costs.view()
        return {**view, "budget_flags": budget.flags()} if budget.active else view

    @app.get("/api/studio/drift")
    def studio_drift(learner: Learner = Admin) -> dict:
        # The removed quotes are words from the official pages that lessons taught.
        _no_help_during_exam(learner, "Source changes are closed")
        return drift.summary()

    @app.post("/api/studio/drift/refilm")
    def studio_refilm(body: RefilmRequest, learner: Learner = Admin) -> dict:
        """The owner's go-ahead to spend money on filming these changed lessons again."""
        _no_help_during_exam(learner, "Filming is closed")
        return drift.approve(body.lessons)

    @app.post("/api/studio/drift/labs/{lab}/looked")
    def studio_lab_looked(lab: str, learner: Learner = Admin) -> dict:
        """The owner looked at a lab whose source page changed: watch it from the pages as they are now."""
        _no_help_during_exam(learner, "Source changes are closed")
        return drift.lab_looked(lab)

    @app.get("/api/studio/deeper")
    def studio_deeper(learner: Learner = Admin) -> dict:
        """More official pages per skill and lessons written again on the deeper template (ADR 0008)."""
        return deeper.view()

    @app.get("/api/record/export")
    def export(learner: Learner = Exit) -> JSONResponse:
        # The export carries lessons, coach answers, item feedback and rehearsal rationales. All of
        # that teaches, so it is closed while an exam runs, like every other way of reading it.
        if room.open_attempt(learner):
            raise HTTPException(409, "Export is closed while a timed exam runs. Finish or end the exam first.")
        return JSONResponse(dojo.export(learner), headers={"Content-Disposition": 'attachment; filename="dojo-record.json"'})

    @app.get("/api/record/chain")
    def chain(learner: Learner = Me) -> dict:
        return dojo.store.verify_chain(learner.key)

    @app.post("/api/record/delete")
    def delete(body: DeleteRequest, learner: Learner = Exit) -> dict:
        # Play's circle entries are not in the learner's folder (ADR 0010, P-4): they go first, so no shared
        # copy outlives its Record, and again after, in case something was added meanwhile.
        social.forget(learner)
        out = dojo.delete_all(learner)
        social.forget(learner)
        return out

    @app.get("/api/diag")
    def diag(learner: Learner = Admin) -> dict:
        return {"selftest": selftest.result, "build": settings.build, "models": dojo.models("author", "gate", "grader"),
                "preparation": preparer.status(), "video_storage": dojo.avatar.usage(), "question_pool": pool.status(),
                "delivery_check": checker.status(), "drift_check": drift.status(), "exam_builds": newexams.diag(),
                "deeper_lessons": deeper.diag(),
                "labs": {"on": True, "mode": "local demo" if lab_demo else "private Azure SQL",
                         "probe": "Use POST /api/diag/lab to connect; never run at startup.",
                         "repair": "POST /api/diag/lab/repair {confirm: true} cleans dbo and escaped history tables."}
                if labs is not None else {"on": False, "mode": "off: " + LABS_OFF},
                "team_sync": team.sync_view() if settings.team else None,
                "deeper_listen": costs.deep_line()}

    @app.post("/api/diag/lab")
    def lab_probe(learner: Learner = Admin) -> dict:
        if labs is None:
            return {"ok": False, "mode": "off", "message": LABS_OFF}
        try:
            return labs.probe()
        except Exception as exc:
            log.warning("lab probe failed: %s", type(exc).__name__)
            return {"ok": False, "message": "Private database is unavailable or waking. Retry in a minute."}

    @app.post("/api/diag/lab/repair")
    def lab_repair(body: LabRepair, learner: Learner = Admin) -> dict:
        """The old dbo clean-up, for the owner only: tables a runner can no longer create, but history
        tables that escaped before the guard, or objects left from before ADR 0009."""
        if not body.confirm:
            raise HTTPException(400, "Confirm the repair: it drops every table in dbo.")
        _labs_on()
        try:
            return labs.repair()
        except UserError:
            raise
        except Exception as exc:
            log.warning("lab repair failed: %s; %s", type(exc).__name__, sql_error_reason(exc))
            raise HTTPException(503, "The database is unavailable. Try again in a minute.") from exc

    @app.post("/api/diag/run")
    def diag_run(learner: Learner = Admin) -> dict:
        """The owner asked for this, so the slow avatar probe and the speech round trip run too."""
        return selftest.run(avatar=True)

    @app.get("/api/about")
    def about(seat: Seat = Who) -> dict:
        storage = f"Your record is stored on this web app's own persistent storage {where_stored()}, in the operator's Azure subscription."
        if settings.team:
            od = team.owner_doc()
            operator = od.get("display_name") or od.get("entra_name") or "the owner of this Dojo"
            storage += (f" This Dojo is run by {operator}, who as its operator can technically read everything stored in it. "
                        "See the notice for what that means.")
        return {
            "build": settings.build,
            "models": dojo.models("author", "gate", "grader"),
            "packages": [p["meta"] for p in packages.by_id.values()],
            "storage": storage,
        }

    # ---------------------------------------------------------------- adding an exam (screen 12)

    @app.get("/api/exam-packages")
    def exam_list(learner: Learner = Admin) -> dict:
        """The exams the owner added, and builds that run or stopped."""
        return {"exams": newexams.overview()}

    @app.get("/api/exam-lookup")
    def exam_lookup(code: str = "", fresh: bool = False, learner: Learner = Admin) -> dict:
        """What Dojo finds for an exam code: the study guide, the weights, the dates and the price."""
        return newexams.lookup(code, fresh)

    @app.post("/api/exam-packages")
    def exam_add(body: NewExamRequest, learner: Learner = Admin) -> dict:
        """The owner confirmed "Build this course": check the official pages and build the package."""
        return newexams.start_build(learner, body.code, body.exam_date)

    @app.get("/api/exam-packages/{pid}")
    def exam_progress(pid: str, learner: Learner = Admin) -> dict:
        return newexams.status(learner, _check_id(pid, PKG))

    @app.post("/api/exam-packages/{pid}/film")
    def exam_film(pid: str, body: FilmRequest, learner: Learner = Admin) -> dict:
        """The owner approved filming at the price shown. Only that set of lessons is filmed."""
        return newexams.approve_film(learner, _check_id(pid, PKG), body.offer)

    @app.post("/api/exam-packages/{pid}/remove")
    def exam_remove(pid: str, body: RemoveExamRequest, learner: Learner = Admin) -> dict:
        return newexams.remove(learner, _check_id(pid, PKG))

    # ---------------------------------------------------------------- the private podcast feed

    def public_base(request: Request) -> str:
        """Where a podcast app reaches Dojo. Behind App Service the app itself is spoken to over plain HTTP."""
        return f"https://{site_host}" if site_host else str(request.base_url).rstrip("/")

    @app.get("/api/feed")
    def feed_status(learner: Learner = Me) -> dict:
        return podcast.status(learner, dojo.enrolled(learner))

    @app.post("/api/feed/link")
    def feed_link(request: Request, learner: Learner = Me) -> dict:
        """The link is shown once, in this answer. Dojo keeps only a hash of it. Not while an exam runs:
        the exam decides at its start and at its end whether an episode could be on the phone, and the
        feed stays paused until it closes."""
        _no_help_during_exam(learner, "A podcast link cannot be made")
        token = podcast.links.create(learner=learner)
        podcast.warm()
        return podcast.link_view(token, public_base(request), learner, dojo.enrolled(learner))

    @app.post("/api/feed/link/new")
    def feed_link_new(request: Request, learner: Learner = Me) -> dict:
        _no_help_during_exam(learner, "A new podcast link cannot be made")
        token = podcast.links.create(replace=True, learner=learner)
        podcast.warm()
        return podcast.link_view(token, public_base(request), learner, dojo.enrolled(learner))

    @app.delete("/api/feed/link")
    def feed_link_off(learner: Learner = Me) -> dict:
        """Turning the feed off is never refused, not even during an exam. Episodes already on the phone
        still play, so Dojo keeps asking about them."""
        podcast.links.delete(learner)
        return podcast.status(learner, dojo.enrolled(learner))

    # Not signed in: App Service authentication leaves /feed/* open, because podcast apps cannot sign in.
    # The token in the path is the credential. Its hash is compared with the owner's (always) and every
    # active member's, and the match decides whose feed this is (ADR 0009); nothing on these routes reads
    # X-MS-CLIENT-PRINCIPAL. Only lesson audio and package metadata are served, and only for the exams that
    # learner is enrolled in. These routes keep no log of requests. Their only writes are the joined lesson
    # audio under podcast/, and once per lesson that the feed listed or served it (that learner's AIRED),
    # so the signed-in side knows what to ask about.
    # While a timed practice exam runs, the feed answers like a wrong link: Dojo shows nothing that teaches
    # during an exam, and the exam asks about what was already on the phone.

    def _feed_owner(token: str, pid: str | None = None) -> Learner | None:
        learner = podcast.links.learner_for(token)
        if learner is None or room.open_attempt(learner):
            return None
        if pid is not None and pid not in dojo.enrolled(learner):
            return None
        return learner

    @app.api_route("/feed/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def feed(token: str, name: str, request: Request) -> Response:
        m = re.match(r"^([a-z0-9-]{2,40})\.(xml|png)$", name)
        learner = _feed_owner(token, m.group(1)) if m and m.group(1) in packages.by_id else None
        if learner is None:
            return _bare(404)
        pid, kind = m.groups()
        if kind == "png":
            cover = APP_DIR / "static" / "podcast" / f"{pid}.png"
            if not cover.is_file():
                return _bare(404)
            return FileResponse(cover, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
        with store.writing_as(learner.key):
            body = podcast.feed_xml(pid, token, public_base(request), learner)
        return Response(body, media_type="application/rss+xml; charset=utf-8", headers={"Cache-Control": "private, no-cache"})

    @app.api_route("/feed/{token}/ep/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def feed_episode(token: str, name: str) -> Response:
        m = re.match(r"^(lesson-[0-9a-f]{20})(-deep)?\.mp3$", name)
        learner = _feed_owner(token) if m else None
        path = None
        if learner is not None:
            with store.writing_as(learner.key):
                path = podcast.episode_file(m.group(1), deep=bool(m.group(2)), learner=learner,
                                            packages=set(dojo.enrolled(learner)))
        if path is None:
            return _bare(404)
        return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.api_route("/feed", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/feed/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def feed_unknown(rest: str = "") -> Response:
        return _bare(404)

    # ---------------------------------------------------------------- the private calendar feed
    # See docs/adr/0004-private-calendar-feed.md. Dojo never reads or writes the learner's calendar: the
    # calendar app subscribes to this feed by URL and fetches it every few hours.

    @app.get("/api/calendar")
    def calendar_status(learner: Learner = Me) -> dict:
        return planner.links.status(learner)

    @app.post("/api/calendar/link")
    def calendar_link(request: Request, learner: Learner = Me) -> dict:
        """The link is shown once, in this answer. Dojo keeps only a hash of it. Unlike the podcast link it
        can be made during a timed exam: the calendar carries no teaching, only times and names."""
        return planner.link_view(planner.links.create(learner=learner), public_base(request), learner)

    @app.post("/api/calendar/link/new")
    def calendar_link_new(request: Request, learner: Learner = Me) -> dict:
        return planner.link_view(planner.links.create(replace=True, learner=learner), public_base(request), learner)

    @app.delete("/api/calendar/link")
    def calendar_link_off(learner: Learner = Me) -> dict:
        planner.links.delete(learner)
        return planner.links.status(learner)

    # Not signed in: App Service authentication leaves /cal/* open, because calendar apps cannot sign in.
    # The token in the path is the credential. Its hash is compared with the owner's (always) and every
    # active member's, and the match decides whose plan this is (ADR 0009); nothing on these routes reads
    # X-MS-CLIENT-PRINCIPAL. The feed carries that learner's planned sessions: exam, activity, skill, length
    # and a link into Dojo, nothing from the Record. It keeps no log of requests. A GET brings the plan up
    # to date (plan.json) and notes when the feed was last fetched (a time, nothing else); a HEAD writes
    # nothing.

    @app.api_route("/cal/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def calendar_feed(token: str, name: str, request: Request) -> Response:
        body = planner.feed(token, public_base(request), fetch=request.method == "GET") if name == "dojo.ics" else None
        if body is None:
            return _bare(404)
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "private, no-cache", "Content-Disposition": 'inline; filename="dojo.ics"'})

    @app.api_route("/cal", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/cal/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def calendar_unknown(rest: str = "") -> Response:
        return _bare(404)

    return app


app = create_app()
