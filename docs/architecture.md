# Dojo architecture

This page explains how Dojo is built: its parts, how data flows between them, where it is stored and
the limits it runs within. For why each choice was made, see the [ADRs](adr/README.md). For running
your own copy, see [operations.md](operations.md). For what is stored about a person, see
[privacy.md](privacy.md).

## The big picture

```
 Browser (vanilla-JS single-page app, strict CSP)        Podcast app         Calendar app
        |  HTTPS, signed in through App Service              | /feed/<token>      | /cal/<token>
        v  authentication (Microsoft Entra ID)               v  (no sign-in)      v  (no sign-in)
 +--------------------------------------------------------------------------------------+
 | Azure App Service (Linux, B1): one gunicorn process with one Uvicorn worker           |
 |   FastAPI app (app/main.py): routes, security headers, route classes                 |
 |   Dojo core (app/learning.py) + rooms: exam, checks, plan, labs, podcast, play, team  |
 |   Background services: preparation, question pool, drift check, deeper lessons,     |
 |   delivered-meaning check, nudges, sweepers                                          |
 |   Store (app/core.py): JSON files on the persistent /home share                      |
 +--------------------------------------------------------------------------------------+
        |  managed identity (no keys)          |  managed identity       |  HTTPS, allow-listed
        v                                      v                         v
 Azure AI Foundry resource              Azure SQL (labs only,      learn.microsoft.com
  - author, gate, grader models          private endpoint,         docs.github.com
  - Speech: TTS, STT, batch avatar        free offer)              (official sources)
```

- One web app serves the page, the API and the two token feeds.
- All AI runs in one Azure AI Foundry resource in the operator's subscription. Dojo calls it with the
  app's user-assigned managed identity. Local (key) authentication is off.
- Everything Dojo keeps is a file on the web app's persistent `/home` share. There is no other database
  for learner data. The only database is the lab sandbox, which holds the tables learners create in
  labs and a small mirror of the deletion list.
- Dojo reads the web only from Microsoft Learn and GitHub Docs, and only pages it is allowed to read
  (see `app/sources.py`).

## How a lesson or a question is made

1. **Sources.** An exam package (`app/packages/*.json`, or an added one on the data share) lists every
   skill in the official study guide with the official pages that teach it. `app/sources.py` fetches
   those pages, keeps a dated snapshot and cuts out the excerpts that matter.
2. **Author.** The author model writes the lesson, the question, the hint or the answer, and must quote
   the excerpt for every claim.
3. **Quote check.** Dojo itself looks for every quote, word for word, in the stored page. A quote that is
   not there removes the claim, or the whole item.
4. **Gate.** A model from a second family reads what is left against the same excerpts and says whether
   each part holds. What does not hold is removed.
5. **Quality log.** Everything removed, withheld or failed is written to the quality log with the reason,
   and shown on the learner's quality page. It is never shown as content.
6. **Grading.** When the learner answers an open question, the grader (a third family) judges each rubric
   point separately and must quote the learner's own words for every point it marks as met. Dojo checks
   those quotes against the answer, piece by piece. Each judgement records the version of the judging
   rules that made it (`GRADING_VERSION`). One made with older rules can be judged again with today's,
   once, at the learner's request: a new `answer.rejudged` event points at the old judgement, which stays
   visible but no longer counts (ADR 0015).
7. **Record.** The answer, its conditions (help seen, time since teaching, spoken or typed, changed
   situation) and the judgement go into the learner's Record, an append-only, hash-chained event log.

What the learner sees about a skill is worked out from the Record each time (`app/evidence.py`): the
four pips (with help, on your own, later, in a new situation), every attempt with its conditions, and no
score. Multiple choice never fills a pip. A missing event is never read in the learner's favour. There
is no pass prediction and no mapping to Microsoft's scaled score.

## AI model roles

| Role | Default model (family) | Job |
|---|---|---|
| author | GPT-5.5 (OpenAI) | Writes lessons, items, hints, rehearsal questions, narration scripts and answers to questions. |
| gate | Grok 4.1 Fast reasoning (xAI) | Independently checks everything shown to the learner against the sources; gives live feedback on an answer. |
| grader | DeepSeek V4 Pro (DeepSeek) | Judges finished answers against the rubric, quoting the learner. |

**Why three families.** A model is poor at catching its own mistakes, and models trained the same way
tend to make the same ones. So the checker is never the writer's family, and the grader is neither:
it did not write the question and it did not approve it. On top of the models, Dojo's own code checks
every quote word for word. A model's answer is never treated as a verified fact on its own.

All three are Azure AI Foundry deployments named `author`, `gate` and `grader`
(`infra/main.bicep`). The model names and versions are Bicep parameters, passed to the app in
`DOJO_MODELS`. With `DOJO_LOCAL=1` and no real AI, `StubAI` in `app/ai.py` stands in: it is deterministic
and quotes the text it is given, so the real quote checks still run in tests.

## Modules in `app/`

Each module starts with a docstring that states what it does and the rules it keeps. In one line each:

| Module | What it does |
|---|---|
| `__init__.py` | Marks the package. |
| `ai.py` | Calls the three Foundry models (author, gate, grader) with the managed identity; `StubAI` for local work and tests. |
| `avatar.py` | Films presenter videos with Azure AI Speech batch avatar synthesis: one job per presenter per lesson, turns found again from the subtitle file. Frozen: video ids are content hashes of its output. |
| `background.py` | Team mode only: one coordinator that runs each background service's next small batch in a spare work slot. |
| `budget.py` | Team mode only: reserves the most each paid call can cost before it runs, enforces per-learner allowances and the members' daily cap, and keeps pooled cost sums. |
| `checks.py` | The 3-minute spoken check: up to three due skills, questions read aloud, answers spoken or typed. |
| `core.py` | Settings, the file `Store` (atomic writes, the hash-chained Record, deletion guards), sign-in helpers, work slots and background jobs. |
| `costs.py` | Estimates what media and models cost from the public Azure retail list prices; never guesses a missing price. |
| `deeper.py` | Deeper lessons (ADR 0008): finds up to two more official pages per built-in skill and schedules lessons to be written again from all of them. |
| `deeplink.py` | The links Dojo hands out to be opened later, `/?go=<route>` (ADRs 0004 and 0013), and the one rule for a route, which `app.js` and `sw.js` repeat. |
| `delivery.py` | The delivered-meaning check: transcribes Dojo's own narration and videos and compares them word by word with the approved text. |
| `drift.py` | The source-drift check (ADR 0006): reads cited pages again, finds changed words and holds back or rewrites what no longer holds. |
| `evidence.py` | Pure functions that turn the Record into what the learner sees per skill: attempts, conditions and pips. |
| `exam.py` | The timed practice exam: building it from a checked question pool, the server-side clock, locked sections, scoring and the background `PoolKeeper`. |
| `five.py` | The 5-minute session a nudge opens (ADR 0013): due recalls, one repair, then a stop. |
| `items.py` | The seven practice-exam item kinds (ADR 0012): how each looks, what an answer may be and how it scores. |
| `itemwriter.py` | Writes and checks the item kinds beyond single choice; every part of an answer key carries its own checked quote. |
| `know.py` | Know what you know (ADR 0011): confidence after an answer and the calibration view. |
| `labs.py` | DP-800 labs in Azure SQL: per-learner schemas and runner users, SQL run and checked in the database; a fake runner locally. |
| `learning.py` | The core `Dojo` class: lessons, narration, practice items, hints, grading, rehearsal, Ask, the quality log, export and delete. |
| `main.py` | The FastAPI app: wiring, startup, the request guard and security headers, and every route with its route class. |
| `meaning.py` | Pure word-error-rate comparison of approved text and a transcript, flagging numbers, names and negations. |
| `mirror.py` | Mirrors the deletion list, membership changes and Pause into the lab database, so a restore of either side cannot undo a deletion. |
| `newexam.py` | Add an exam (ADR 0005): reads the study guide, finds and checks pages per skill, prices the course and builds the package. |
| `packages.py` | Loads and validates exam packages (built-in, added, and the deeper-lessons page overlay). |
| `pagefinder.py` | Finds official pages that teach a skill: site search, the author picks, the gate must quote the page, Dojo finds the quote. |
| `plan.py` | The study plan inside the learner's free times, and its private iCalendar feed (ADR 0004). |
| `play.py` | Play's personal layer (ADR 0010): study points, weeks met and the moments shelf, all derived from the Record. |
| `play_board.py` | Play's effort board: weekly bands of effort with no order (team mode, opt-in). |
| `play_duel.py` | Play's duels by invitation: ten shared questions, accuracy only, deleted after seven days (team mode, opt-in). |
| `play_social.py` | Play's circles: a shared weekly goal in coarse bands, shared moments and passes (team mode, opt-in). |
| `podcast.py` | The private podcast feed per exam (ADR 0002): episodes, the token check and the "did you listen?" notes. |
| `prepare.py` | Background preparation: rewrites after drift, missing lessons, deeper-template upgrades, narration, Deeper Listen and approved filming. |
| `push.py` | Opt-in Web Push nudges (ADR 0013): at most one a day, only when something is due. |
| `readiness.py` | "Should you book the exam?": advice from the learner's own Record and practice exams, with every rule shown; never a prediction. |
| `relearn.py` | Successive relearning (ADR 0011): when a skill is asked again, derived from the Record. |
| `selftest.py` | The startup self-test of models, Speech and the sign-in credential exchange, reported by `/healthz`. |
| `sources.py` | Allow-listed fetching of official pages, dated snapshots, excerpts and word-for-word quote checks. |
| `speech.py` | Narration (text to speech) and transcription of spoken answers (fast transcription); audio of an answer is never stored. |
| `studyguide.py` | Reads an official study guide and certification page without a model or the network, checking every copied word. |
| `team.py` | Team mode (ADR 0009): members, access requests, roles, notice, leaving, removal, tombstones and the route classes. |

The front end is `app/static/`: `index.html`, `app.js` (the single-page app, no framework and no build
step), `app.css`, the icon sprite `icons.svg`, self-hosted fonts in `fonts/`, the web app manifest and
`sw.js`, a service worker that caches nothing (it exists so the app can be installed and receive push
messages).

## Request handling and sign-in

- **App Service authentication (Easy Auth)** signs every user in with Microsoft Entra ID before a
  request reaches Dojo. It passes the verified identity in the `X-MS-CLIENT-PRINCIPAL` header. There is
  no client secret: the sign-in app registration trusts the web app's managed identity through a
  federated identity credential.
- **Owner-only (the default).** Easy Auth admits only the owner's object ID, and Dojo checks the tenant ID
  and object ID again on every request (`app/core.py`, `app/team.py`).
- **Team mode.** Easy Auth admits every user of the tenant, and Dojo's own allow-list (the owner plus the
  members the owner approved) is the gate.
- **Route classes.** Every route is in exactly one class: `public` (`/healthz`, `/`, `/feed/*`, `/cal/*`),
  `signed_in_non_member` (ask for access), `learner_exit` (notice, export, delete, leave), `learner`
  (everything else a learner does) or `admin` (the owner). A test fails if a route has no class.
- **Request guard** (`app/main.py`). It refuses paths with dot segments, backslashes or encoded separators
  (so Easy Auth and the app always agree which path was asked for). It requires the `X-Dojo: 1` header and
  a same-site origin on every API call that changes something. It sets a strict Content Security Policy
  (no inline scripts or styles), HSTS, `X-Frame-Options: DENY`, and a Permissions Policy that allows only
  the microphone, on this site only.
- **Open paths.** `/healthz` returns only `{"status": "ok" | "degraded", "build": "<commit-sha>"}`; the
  per-check results and their time are at `/api/diag` for the signed-in owner. `/feed/*` and `/cal/*` answer only GET and HEAD,
  and the long random token in the path is the credential; Dojo keeps only a hash of it. Because request
  URLs would carry the token, the site's application log level stays at Warning and Dojo writes no line
  per request in Azure. Easy Auth also leaves `/static/sw.js` and `/static/icon-192.png` open: the browser
  fetches them outside any page, and Easy Auth's sign-in answer to either would set a new nonce cookie that
  breaks a sign-in under way (ADR 0013).

## Background services and their limits

All work shares three work slots (`Slots` in `app/core.py`). Interactive jobs queue for a slot in a fair
order: whoever was served least recently goes first. The owner may have six jobs queued or running, a
member three. Job documents are kept 14 days; after a restart, unfinished jobs are marked interrupted.

| Service | Module | Runs | Main limits |
|---|---|---|---|
| Self-test | `selftest.py` | At startup with real AI | Models, Speech both ways, sign-in exchange. The avatar is probed only on request. |
| Preparation | `prepare.py` | Real AI, in Azure or with `DOJO_PREPARE=1` | One lesson at a time; waits while the learner has work running. Drift rewrites first (10 a day), then missing lessons, deeper-template upgrades (20 a day), narration, Deeper Listen (20 a day, 2 attempts a lesson, a week apart), then approved filming (stops at 90% of the video budget). |
| Question pool | `exam.py` (`PoolKeeper`) | As preparation | Keeps about 24 checked questions per exam; at most 12 model batches a day (team mode: 6 per member, 24 for all members). |
| Deeper lessons search | `deeper.py` | As preparation | One group of skills at a time, at least 10 minutes apart; a skill with nothing new is searched again after 30 days. |
| Delivered-meaning check | `delivery.py` | Real AI, in Azure or with `DOJO_CHECK=1` | Per day: 10 Listen lessons, 10 Deeper Listen lessons, 1 Watch lesson. |
| Source-drift check | `drift.py` | In Azure or with `DOJO_DRIFT=1` (no model) | Each cited page at most once a day; at most 400 reads a day, at least 3 minutes apart; backs off when a site asks. |
| Price lookup | `costs.py` | On demand | Azure Retail Prices API at most once a day, kept on the share. |
| Avatar pacer | `avatar.py` | When filming | Batch avatar allows 2 create calls a minute on S0; one create every 31 seconds. |
| Nudges | `push.py` | In Azure or with `DOJO_PUSH=1` | Looks every 5 minutes; at most one nudge a day per learner, up to 5 devices. |
| Team sweeper | `team.py` | Team mode | Hourly: expiries, deletions after the grace period, the lab-database mirror. |
| Play sweeper | `play_social.py` | Always | Lapsed invitations and old shared copies. |

Daily counters are written to the share before the work is paid for, so a restart never hands a service
a fresh allowance. In team mode the services stop running their own loops, and `background.py` runs one
batch at a time, only while two slots stay free for learners.

## Speech and avatar

- **Narration.** Each lesson is a two-voice conversation: the guide (Andrew's voice, the Harry avatar)
  explains and the coach (Ava's voice, the Meg avatar) asks. Text to speech uses Azure AI Speech neural
  voices. Audio files are named by a hash of what they say.
- **Deeper Listen** (ADR 0007) is a longer, gated audio lesson of about six minutes that replaces the
  slide narration in Listen and in the podcast once it is ready.
- **Spoken answers** (ADR 0001) go once to fast transcription and are thrown away. In team mode Dojo
  first decodes the recording in memory (PyAV, at most 2 MB and 200 seconds) and sends a 16 kHz mono
  copy it wrote itself.
- **Presenter videos** are filmed only on an explicit request that shows the list price. Each lesson is
  two batch jobs, one per presenter, because of the 2-creates-a-minute quota. Videos are transparent
  WebM, about 8.2 MB a minute; they are capped at 3 GB in total and the least recently watched is
  deleted first. Lessons and audio are never deleted to make room.

## Podcast and calendar feeds

- **Podcast** (ADR 0002): `/feed/<token>/<exam>.xml` serves one episode per skill, made from
  the lesson's Deeper Listen or its narrated slides. The feed reads no learner data. It notes which
  lessons it has listed or served, so Dojo can ask "did you listen in a podcast app?" before an answer
  that could count as later. Off until the learner turns it on. An episode's notes link back to its skill.
- **Calendar** (ADR 0004): `/cal/<token>/<name>` serves the plan as iCalendar with a reminder and
  a link per session. It never reads or writes anyone's calendar, and says nothing from the Record.
- **Links back into Dojo** (ADRs 0004 and 0013): a calendar event, an episode's notes and a nudge link to
  `/?go=<route>`, not `/#/<route>`. Easy Auth loses what follows `#` when it signs someone in and keeps the
  query, so the app turns `/?go=<route>` into `/#/<route>` when it starts (`app/deeplink.py`).

## Labs (DP-800)

- One free-offer serverless Azure SQL database (auto-pause after 60 minutes) with public access off,
  reached only through VNet integration and a private endpoint. Entra-only authentication; the app's
  managed identity is the server's Entra administrator.
- Each learner has their own schema per lab and their own runner user without a login. Their SQL runs on
  a fresh connection as that runner (`EXECUTE AS USER ... WITH NO REVERT`), so SQL Server, not app code,
  keeps learners apart. Names come from a random surrogate, never from a learner key or directory ID. A
  database trigger keeps temporal history tables inside the lab schema.
- A check runs in the database. Lab events count as teaching and as help, never as evidence pips.
- Locally (`DOJO_LOCAL=1`) a clearly marked fake runner is used and no SQL connection is made.
- In Azure with `DOJO_LAB_SERVER` and `DOJO_LAB_DATABASE` both empty, labs are off: there is no runner,
  every lab route answers 404 "Labs are not set up on this Dojo.", the Plan schedules no labs, and team
  mode has no lab-database mirror.

## Team mode (ADR 0009)

Off by default (`DOJO_TEAM` unset, Bicep `teamMode=false`). While it is off, every member, circle, board
and duel route answers 404 and Dojo behaves as owner-only. When it is on:

- Members ask for access; the owner approves on the Members page. Membership is read on every request,
  so a removal takes effect on the next one.
- Every learner has their own folder and their own hash-chained Record; no chain spans two learners.
- Writes for a learner are generation-checked: work that began before a learner started over or left
  writes nothing (`Abandoned`). Deleted learners leave a tombstone (a hash) for 40 days, mirrored in the
  lab database, so a backup restore cannot bring their data back.
- Every paid call reserves its maximum cost first. Members have daily allowances and a shared daily cap
  (default USD 5 at list price). The owner is metered but never refused.
- The owner never sees per-member activity, results or costs. Cost sums are pooled and shown per group
  only with five or more members.
- At most 30 members are supported on one B1 instance.

Switching it on needs a privacy review first; see [operations.md](operations.md#switching-team-mode-on-safely).

## Play (ADR 0010)

- **Personal layer**, off until the learner turns it on: study points (never depend on being right or on
  a schedule), "weeks met" (never goes down; no streak), and a moments shelf derived from the Record.
- **Social layer**, team mode only and off until the owner switches it on, then opt-in per member:
  circles of 3 to 12 people with a shared goal shown in 25% bands, an effort board in four unordered
  bands, and duels by invitation. No ranking, no results shown to others.

## Storage layout on the data share

The data folder is `DOJO_DATA_DIR`: `/home/dojo-data` in Azure, `.dojo-data/` in the repository folder
locally. Names are checked against a safe pattern before any file is touched. JSON documents are written
to a temporary file and renamed, so a reader never sees half a document.

```
learners/<key>/            one folder per learner; <key> is a one-way hash of tenant id + object id
  events.jsonl             the Record: append-only, hash-chained events
  quality.jsonl            what was removed or failed for this learner, and why
  items/ rehearsals/ asks/ exams/ checks/ pool/ seen/ advice/   own items, attempts, pools, advice
  plan.json calendar.json feed.json aired.json profile.json allowance.json push.json
  selfchecks.jsonl recall_log.jsonl
  social/play.json         Play settings and opt-ins
  media/                   audio of spoken check questions (team mode)
content/                   shared study material, not personal
  lessons/ deep/           lessons and Deeper Listen scripts
  package-sources/         extra official pages found for built-in skills (ADR 0008)
  delivery/ drift-state.json deeper-state.json deep-state.json prices.json quality.jsonl
sources/                   dated snapshots of official pages
media/                     narration MP3 and presenter WebM files, named by content hash, with timing files
podcast/                   joined podcast episodes
packages/                  exams added in the app (never in git); package-builds/ packages-retired/
jobs/                      background job state, kept 14 days
push/vapid.json            the app's Web Push key pair (never in git, logs or responses)
team/                      team mode: members.json, requests, audit.jsonl, tombstones, state, owner,
                           budget, cap-today, pool-budget, meter, budget-flags, play
play/                      circles/, board.json, duels/ (team mode, social layer)
```

The B1 plan's `/home` share is 10 GB. Presenter videos are capped at 3 GB of it. App Service takes
automatic backups of `/home` (see [operations.md](operations.md#backups-and-restore)).

## Concurrency limits

- **One instance, one worker.** gunicorn runs a single Uvicorn worker (`infra/main.bicep`). The `Store`
  serialises file access with one in-process lock, and background services keep state in memory.
  Running a second worker or scaling out to a second instance would break those guarantees. Do not do
  it.
- **Three work slots** for model-backed work (interactive and background together), with a fair queue.
- **Long requests.** gunicorn's timeout is 600 seconds; model calls time out after 480 seconds.
- **Always On** is enabled, so background services keep running.
