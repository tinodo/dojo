# ADR 0002 - A private podcast feed that podcast apps reach without signing in

- Status: accepted
- Date: 2026-09-28
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: Accepted by the Dojo lead under the owner's standing instruction to decide and build
  autonomously, 2026-09-28; the owner can reject it at any time by turning the feed off or by saying
  so. Turning the feed off makes every link answer 404 at once. Review triggers are listed below.
- Guardrails: EG-6 (credential exceptions), EG-12 (record consequential decisions), EG-16 (exposure
  is not a Finding), EG-20 (what consumption does not prove), EG-26 (deletion must be real), EG-29
  (no bearer credentials in URLs and logs), EG-43 (dependencies), COMPONENT-MAP M-03 (a missing event
  is never read favourably), INTERACTION-TO-EVIDENCE-CONTRACT (unknown, never "none"), and "Exception
  and change control"

## The requirement

DESIGN.md section 4 puts the phone in the product as "a private podcast feed in any podcast app"
(mockup 11). Section 9 lists "offline podcast downloads" as not designed yet. This ADR designs it.
The lessons already have narration: every slide has its own MP3.

Everything in Dojo sits behind App Service authentication (Entra sign-in), except `/healthz`. A
podcast app cannot sign in with Entra. It fetches a feed URL and the episode URLs in it, from the
phone and sometimes from its maker's servers. So the feed needs paths that App Service
authentication leaves open, and a credential that fits in a URL. That changes identity, secret
handling and the cloud boundary, which is why it is written down here.

## The options

1. **No feed; download buttons in the signed-in app.** Nothing new is open. But files copied to a
   phone by hand are not a podcast: no updates, no order, and the phone must sign in to download.
   Rejected.
2. **The feed behind sign-in.** Podcast apps do not do Entra sign-in. Some send a user name and
   password (HTTP Basic), which would need a second password system. Rejected.
3. **Short-lived signed links per episode.** Podcast apps fetch the feed and its episodes days or
   weeks later, so expiring links break the subscription. Rejected in favour of a link the owner
   can renew at will.
4. **A private feed at a secret URL** (chosen). This is how private podcast feeds usually work. The
   secret is a long random token in the path, and only the feed, its cover and its episodes live
   there.

For the evidence, a download could be recorded as a listen. An earlier version of this design did
that: every episode download wrote a "heard, extent unknown" view into the Record. It was dropped,
because it let requests that are not signed in write into real learner data. Dojo now asks the
learner instead, in the signed-in app (see "Exposure" below).

## The decision

**What is open without sign-in.** `infra/main.bicep` adds `/feed/*` to
`globalValidation.excludedPaths`, next to `/healthz`. Microsoft Learn documents wildcard paths in
that list: the file-based configuration reference shows
`"excludedPaths": ["/path1", "/path2", "/path3/subpath/*"]`
(https://learn.microsoft.com/azure/app-service/configure-authentication-file-based). Under `/feed/`
the app answers only:

- `GET` or `HEAD /feed/<token>/<exam>.xml`: the feed for one exam (RSS 2.0 with the itunes namespace);
- `GET` or `HEAD /feed/<token>/<exam>.png`: its cover art, a static file;
- `GET` or `HEAD /feed/<token>/ep/<lesson id>.mp3`: one episode, with byte ranges.

A wrong token, no token or any other path under `/feed` gets 404 with an empty body. Any other
method gets 405. While a timed practice exam runs, the right token gets the same empty 404 (see
"Exposure during a timed practice exam" below).

**What they contain, and what they write.** Lesson content and package metadata only: exam and
skill names and ids in study-guide order, slide titles, the joined narration, the lesson's sources
with their licences, and when each lesson was written. No learner data: no name, no Record, no
evidence, no misses, no progress. The feed routes never call `current_learner` and never read
`X-MS-CLIENT-PRINCIPAL`. A valid token maps to the configured owner explicitly, and to nothing else.
The one thing they read about the owner is whether a timed practice exam is running.
The feed routes write nothing into any learner's data: no event, no request log, no counter. They
write two things under `podcast/` on the data share, outside the Record: the joined episode audio,
which is lesson content, and, once per lesson before it is first listed or served, a note that the
feed has offered it (`podcast/aired.json`, see "Exposure" below).

**The token.**

- 32 random bytes from `secrets.token_urlsafe` (256 bits, 43 URL-safe characters).
- Made only when the owner presses "Get my podcast link". It is shown in that answer only, and the
  page keeps it in memory until it is closed or reloaded.
- The server stores only a SHA-256 hash (of `dojo-feed:` + token) and the time it was made, in
  `learners/<owner>/feed.json` on the data share. The token itself is stored nowhere.
- Checked with `hmac.compare_digest`. A hash is compared even when there is no link.
- "Make a new link" replaces the hash, so the old link dies at once. "Turn the feed off" and
  "Delete my record" remove it. What the feed has listed or served (`podcast/aired.json`, see
  "Exposure" below) is kept apart from the link: a new link and turning the feed off keep it; only
  "Delete my record" removes it.
- The QR code is drawn on the server (segno, SVG) and returned inside the signed-in
  `POST /api/feed/link` answer, so the token never appears in any other URL.

**The path guard.** App Service authentication decides by path whether to ask for sign-in, and the
app routes by path. The two must never disagree about whether a path lies under `/feed/`. So the
guard middleware refuses (400), on every route, any raw path with a `.` or `..` segment, a
backslash, or an encoded dot, slash, backslash or percent sign (`%2e`, `%2f`, `%5c`, `%25`). Dojo
never makes such paths. `/feed/../api/me` and `/feed/%2e%2e/api/me` never reach the API, and `/api`
still needs the principal header that only App Service authentication sets. Learn: "External
requests aren't allowed to set these headers, so they're present only if App Service sets them"
(https://learn.microsoft.com/azure/app-service/configure-authentication-user-identities).

**Caching and indexing.** Feed answers carry `Cache-Control: private` (`no-cache` for the XML, one
day for audio and cover) and `X-Robots-Tag: noindex, nofollow`. The feed says
`<itunes:block>Yes</itunes:block>`, which asks Apple Podcasts not to list it. These are requests,
not controls; the token is the control.

**Logging (EG-29).** The token is a bearer credential, so it must not land in logs.

- The app's own request log writes `/api` paths only. Feed requests are not logged, and a refused
  path is logged without the path.
- gunicorn writes no access log. The start command (`appCommandLine`) has no `--access-logfile`,
  there is no `GUNICORN_CMD_ARGS` and no gunicorn config file, so `accesslog` stays unset. The
  Uvicorn worker gives uvicorn's access logger gunicorn's empty access handlers, so nothing is
  written.
- App Service on Linux has no web server (request) logging; Learn lists it as Windows only
  (https://learn.microsoft.com/azure/app-service/troubleshoot-diagnostic-logs). The HTTP log table
  (`AppServiceHTTPLogs`) is filled only through diagnostic settings, and Dojo has none.
- On Linux, `httpLogs.fileSystem` keeps the container's console output instead. The Azure CLI sets
  it for `az webapp log config --docker-container-logging filesystem` ("gathering STDOUT and
  STDERR output from container"), which is how Learn turns on container logging for Python apps
  (https://learn.microsoft.com/azure/app-service/configure-language-python). Dojo's own log lives
  there, so the switch stays on (7 days, 35 MB). That output is Dojo's log and gunicorn's error
  log, and neither holds a feed URL. A test fails if the Bicep gains an access log,
  `GUNICORN_CMD_ARGS` or diagnostic settings, and another fails if a feed request logs the token.
- Run locally, plain uvicorn prints an access log to the console, feed paths and tokens included,
  so the README's local command passes `--no-access-log`. This log is local only; production runs
  gunicorn, as above.
- The App Service authentication module logs every request URL, at Information, into
  `/home/LogFiles/Application/diagnostics-<date>.txt` ("Request starting HTTP/1.1 GET
  …/feed/<token>/gh-300.xml"). This was not known when the feed was accepted; it was found after the
  first deployment (29 Sep 2026), before any link had been made, with made-up tokens only. So the
  site's application log level (`applicationLogs.fileSystem.level` in `infra/main.bicep`) is
  `Warning`: the module's warnings and errors stay (on 28–29 Sep: start-up port notices and
  cross-site request warnings, none with a request path), its request lines do not. A test fails if
  the level goes back below `Warning`. Whether the platform's front ends keep request URLs is not
  known. The log files sit on the same share as the Record and the lessons; whoever can read them
  can already read everything a link opens. Treat the link as known to the platform operator.

**Exposure: asked, in the signed-in app (M-03, I2E).** Dojo cannot see what a podcast app downloads
or plays; the feed notes only which lessons it has offered (below). Left there, a check an hour after listening could count as
"later". Episodes already on the phone still play after the feed is turned off, so turning it off
does not end this. The answer form asks, when it matters:

- When: the feed has listed or served the skill's episode at some time, even if the feed is off now;
  the item is an unanswered probe or check; and the answer could count as "later" (from 60 minutes
  before "later" is reached, so the usual wait for judging is covered), or, when nothing has taught
  the skill yet, as "in a new situation" (an item with a changed condition).
- "Listed or served" (`podcast/aired.json`, `Podcast._aired`): before the feed lists an episode, or
  serves its file, Dojo notes the lesson, by exam and skill, with the time it first did. One note per
  lesson, written once; one feed request writes all its new notes at once. If the note cannot be
  written, the episode is left out of that answer, so nothing reaches a phone that Dojo would not ask
  about. A downloaded episode stays on the phone when a newer lesson or new narration takes its
  place in the feed and when the feed is turned off, so the note outlives all of these; only "Delete
  my record" removes it. A skill whose episode the feed never listed or served is on no phone, so it
  is not asked about. The note names lessons and times, never who fetched them, from where, or with
  which app.
- What: "Did you listen to this lesson in a podcast app since <the last teaching, in plain
  words>?" Yes or No, and a reply is required. The answer route refuses an answer without one
  (409); the form then shows the question and keeps the typed answer. The 3-minute check asks it
  too: its answers go through the same route, so `GET /api/checks/<id>` gives each question still to
  answer its `podcast_question`, and the check page shows it above "Send and go on".
- What is recorded: the reply, in the signed-in answer request, before the answer, as
  `podcast.declared {package, skill, item, lesson, heard, since, asked}`. "Yes" is teaching at
  that moment, extent unknown, so that answer counts as right after teaching; the Record lists it
  as "You said you listened in a podcast app", marked "declared · not evidence". "No" leaves
  "later" standing, and the attempt lists "Declared: no podcast listening since the last teaching".
  Anything but an explicit "no" counts as "yes". A declared answer's time since teaching is measured
  to the declaration, just before sending, and never comes out longer than it would without one.
- It is exposure, never a Finding (EG-16). Every episode's notes say what listening does not prove
  and point to the check in Dojo (EG-20).
- Nothing else records a podcast listen. `POST /api/lessons/<id>/viewed` refuses the `podcast` way
  of viewing (422). An earlier version accepted it, though its page never sent it. A view of that
  kind already in a Record still counts as teaching, the reading that never favours the learner, and
  the skill page says an earlier version of Dojo recorded it.

**Exposure during a timed practice exam.** A practice exam claims "exam conditions" only when Dojo
can see that nothing taught during it (`ExamRoom._conditions`), and readiness rule 2 counts only
such exams. A podcast app is outside that view, so:

- While an exam runs, every feed URL answers like a wrong link, just as the app closes lessons and
  media during an exam. No podcast link can be made or renewed while it runs; turning the feed off
  always works. So the feed's state during an exam is known from its start and its end.
- An exam asks before it closes when, at its start or its end, an episode could be on the phone
  (`Podcast.could_be_on_phone`): the feed is on, or it has listed or served any episode, by the
  rule above. So an exam started after the feed was
  turned off still asks. The question: "Did you listen to a lesson in a podcast app while this exam
  was open?" Yes or No. The brief says so before the start. Finishing early without a reply is
  refused (409). When time runs out, the page asks first and sends the reply with the time-up.
- The reply is recorded in the signed-in submit request, before the exam closes, as
  `podcast.declared {package, attempt, heard, asked}`. It names no skill, so it does not move any
  teaching clock; a later check asks its own question. Only "No" lets the exam claim exam
  conditions, and the result then says "You said you did not listen to a lesson in a podcast app
  while it was open." "Yes", or no reply at all (a closed tab, a deadline that passed first), records
  the conditions as unknown.

**Episodes.** One per skill: the latest lesson, and only if every slide has narration. The slide
MP3s share one encoding (24 kHz, 96 kbit/s, mono), so their frames are joined, with any tags and
the encoder's info frame removed; the duration is bytes × 8 / 96,000 seconds. The joined file is
made on first request and kept under `podcast/` on the data share, keyed by the ordered slide media
ids, so new narration makes a new file. Files no current episode uses are deleted once none has used
them for 10 minutes, so a download handed one just before a newer lesson or new narration took its
place can still open it. Dojo looks for them in the background, 30 seconds after the last new file
was made (so a run of new files is followed by one clean-up), when the app starts and when a link is
made.

## What is kept, and what is not

- Kept by Dojo: the token's hash and creation time (`feed.json`); which lessons the feed has listed
  or served, and when it first did (`podcast/aired.json`, removed only with the record); the joined
  episode files (lesson audio, no learner data); the learner's own podcast replies in the Record,
  which is append-only, written by signed-in answer requests.
- Not kept: the token, and anything else about feed requests: no address, no podcast app name, no
  count of downloads or plays. The one trace of a request is when the feed first offered each lesson.
- Out of Dojo's reach: the podcast app, and possibly its servers, keep the feed URL, the episode
  files and their own logs. Dojo cannot delete those. The page says so, and says that a new link
  stops new downloads (EG-26).

## Consequences

- Part of Dojo is now reachable by anyone with the link. A 256-bit token cannot be guessed, but a
  leaked link (a shared phone, a backup, a screenshot, a podcast service's servers) works until
  the owner makes a new one. It has no expiry, because an expiring link breaks the subscription.
- Whoever has the link can download the lesson audio and read the feed, nothing more. They can
  neither read nor write the Record or any other learner data.
- Once the feed has offered an episode, some probe and check answers need one more click, and so
  does finishing a practice exam. That goes on after the feed is turned off, until the record is deleted.
- Residual risks:
  - Dojo relies on the learner's word. A "no" after listening makes an answer look later than it
    was, or an exam look like exam conditions; the Record says that it was declared.
  - Dojo keeps asking after the feed is turned off, even once the episodes are gone from the
    phone, because it cannot see that. It errs towards asking: a lesson narrated only after it left
    the feed is asked about too. The confirmation before turning the feed off, and the panel once
    it is off, say that Dojo keeps asking.
  - "Later" is measured when an answer is judged, for every attempt, not only here. Judging takes
    seconds, but a failed judgement retried much later can carry an answer past "later". An answer
    sent more than 60 minutes before "later" and judged after it is not asked about. Measuring at
    submission would close this for all answers; that is a separate change.
  - Two sends of the same answer at the same moment can record the reply twice. The later one
    counts, and no order of the two makes the answer look later than one reply would.
- One new dependency, segno 1.6.6, for the QR code: pure Python, BSD-3-Clause, no dependencies of
  its own, pinned in `requirements.txt` (EG-43). The standard library has no QR encoder.
- Storage: the joined episodes repeat the slide narration on the data share, about 0.7 MB per
  minute (roughly 5 MB a lesson, 0.6 GB if all 114 skills have one). Replaced files are deleted.

## Guardrail exception

| Field | Value |
| --- | --- |
| Guardrails | EG-6 (a bearer credential where no identity flow is possible) and EG-29 (a credential in a URL path): a bearer capability URL for non-personal content |
| Reason | Podcast apps cannot sign in with Entra ID |
| Alternatives | Options 1 to 3 above, and recording downloads as listens (dropped) |
| Risk | Low. A leaked link lets its holder download lesson audio and read the feed until the link is renewed. It opens no learner data. The only thing its requests write is when the feed first offered a lesson, so at worst Dojo asks the podcast question about lessons the owner never downloaded, never the other way round |
| Affected users and data | The owner only; lesson content and package metadata. No real learner data |
| Scope | `GET` and `HEAD` of the owner's feeds, covers and episodes |
| Owner | The owner, the only user; decided by the Dojo lead |
| Protected storage | The hash only, on the web app's own `/home` share |
| Rotation and expiry | No automatic expiry; "Make a new link" at any time; the review triggers below |
| Incident response | Make a new link or turn the feed off; both work at once. Copies in podcast apps cannot be recalled |
| Mitigation | 256-bit token, hash-only storage, constant-time check, 404 without detail, path guard, no feed logging, no learner data in the feed, and nothing written from it but which lessons it has offered (`podcast/aired.json`, outside the Record) |
| Deterministic evidence | `tests/test_podcast.py`: `LinkTests`, `FeedTests` (among them `test_the_feed_writes_nothing_into_any_learners_data`, `test_the_feed_holds_no_learner_data`, `test_the_token_is_never_written_to_a_log`), `OpenFeedSignInTests`, `PodcastQuestionTests`, `AiredRecordTests` and `ReplacedEpisodeTests`; the podcast declaration tests in `tests/test_units.py` (`EvidenceTests`); `tests/test_exam.py`: `PodcastDuringExamTests`, `PodcastAfterTheFeedIsOffTests` and `ConditionsTests.test_with_the_podcast_feed_on_only_an_explicit_no_claims_them` |
| Decision | Accepted by the Dojo lead under the owner's standing instruction to decide and build autonomously, 2026-09-28; the owner can reject it at any time by turning the feed off or by saying so. Turning the feed off makes every link answer 404 at once. Review triggers as listed below |

**Who decides.** The exception involves no real learner data: the feed serves lesson content and
writes only which lessons it has offered, and the podcast question belongs to the signed-in app. So "Exception and change
control" lets a solo project owner decide it, with recorded alternatives, risk, deterministic
evidence and an explicit later review trigger. That is an owner decision, not independent human
review.

## Review trigger

- Anyone other than the owner gets access to Dojo.
- The link may have leaked: make a new link first, then review.
- The feed is to serve anything personal, or to write anything into learner data. That is no
  longer a low-risk exception, and needs independent review.
- Any change that could log request URLs: a gunicorn access log, diagnostic settings for App
  Service HTTP logs, Application Insights request telemetry, a proxy or CDN in front of the app, or
  the site's application log level below `Warning`.
- A change in how App Service authentication matches `excludedPaths`, or a move away from it.
- Dojo starts withholding lessons after source drift (EG-20): the feed must withhold the same
  episodes (`Podcast._plan`). Done with ADR 0006, 2026-09-30.
- At the latest after the exams, in March 2027.

## How to undo it

1. Press "Turn the feed off", or delete `learners/<owner>/feed.json`: every feed URL answers 404
   at once. Dojo keeps asking the podcast question about episodes that may already be on the phone,
   until step 3.
2. Remove `'/feed/*'` from `excludedPaths` in `infra/main.bicep` and deploy: App Service
   authentication asks for sign-in on `/feed` again.
3. Remove the feed routes from `app/main.py` and `app/podcast.py`, the podcast question
   (`Podcast.question` and `declare`, `Podcast._aired`, `_put_on_air` and `could_be_on_phone`,
   `podcast_heard`, `podcastQuestion` in `app/static/app.js`), the exam's podcast question
   (`ExamRoom._asks_podcast`, `_podcast_possible` and `_declare`, the podcast lines in
   `ExamRoom._conditions`, `podcastAsk` and `EXAM_PODCAST_RULE` in `app/static/app.js`), the refusal
   of feed links during an exam, the "On your phone" panel, page and styles, `app/static/podcast/`,
   `tests/test_podcast.py`, `PodcastDuringExamTests`, `PodcastAfterTheFeedIsOffTests` and segno.
   Delete `podcast/` (with `aired.json`) on the data share.
4. Keep the path guard; it protects the rest of the app too.
5. Podcast replies already in the Record stay, and the evidence rules keep reading them. The Record
   is append-only, and they are the learner's own statements. Exams already closed keep the
   conditions they were given.
