# ADR 0004 - The plan as a private calendar feed, without Microsoft Graph

- Status: accepted
- Date: 2026-09-30
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: Accepted by the Dojo lead under the owner's standing instruction to decide and build
  autonomously, 2026-09-30. The owner can veto it at any time by turning the feed off or by saying
  so. Turning the feed off makes every link answer 404 at once. Review triggers are listed below.
- Guardrails: EG-5 (sign-in is not permission to read Microsoft 365), EG-41 (personal project and
  corporate estate stay apart), EG-6 (credential exceptions), EG-12 (record consequential
  decisions), EG-26 (deletion must be real), EG-29 (no bearer credentials in URLs and logs), EG-43
  (dependencies), COMPONENT-MAP M-02 (Learning Orchestration proposes, the learner decides) and M-03
  (a missing event is never read favourably), and "Exception and change control"
- Numbering: 0003 is the DP-800 lab sandbox (`0003-dp800-lab-sandbox.md`), so this is 0004.

## The requirement

DESIGN.md section 1 says Dojo "plans all of that into your Outlook calendar around your meetings and
moves things when you miss them". Section 4: "Every session is a calendar event with a link that
opens exactly that activity." Section 7 describes screen 08, Plan: the week in the calendar, both
exams, the horizon to March and the sync status (mockup 08).

The owner's Outlook is his corporate mailbox. Reading his meetings or writing into his calendar
through Microsoft Graph would reach into the corporate estate. EG-41 keeps the personal project and
the corporate estate apart, and EG-5 makes any Microsoft 365 permission a separate decision, which
for a delegated calendar scope is the owner's (and his tenant's) to take. The lead has not taken it
for him. So Dojo must put the plan in front of the owner without touching his mailbox.

A calendar app that subscribes to a feed fetches it by URL, from its own servers, without the owner's
sign-in. So the feed needs a path that App Service authentication leaves open, and a credential that
fits in a URL, as the podcast feed does (ADR 0002). That changes identity, secret handling and the
cloud boundary, which is why it is written down here.

## The options

1. **Microsoft Graph: read free/busy, write events into the work calendar.** This is what the design
   first describes. Dojo would see meetings and write events directly. It needs a delegated
   calendar permission on the corporate mailbox (to write, `Calendars.ReadWrite`, which also reads
   every event's details). That is the corporate estate (EG-41, EG-5), and only the owner can decide
   it. **Rejected until the owner decides.** His one-line decision: *"Dojo may read when I am busy
   and write its sessions into my work calendar through Microsoft Graph (delegated calendar
   permission): yes or no."* Teams nudges (DESIGN.md section 4) need the same kind of decision and
   are replaced for now by the events' reminders.
2. **A file to import (.ics download).** Outlook imports a snapshot and never updates it
   ("When you import an .ics file, your calendar doesn't refresh the imported events",
   https://support.microsoft.com/en-us/outlook/import-or-subscribe-to-a-calendar-in-outlook-com-or-outlook-on-the-web).
   A session moved after a miss would never reach the calendar. Rejected.
3. **Invitations by email.** Dojo would have to send mail (Graph again, or a mail service with its
   own credential), and every move would be another mail in the corporate inbox. Rejected.
4. **The feed behind sign-in.** Calendar apps fetch a subscribed calendar from their servers and
   cannot do Entra sign-in. Rejected.
5. **A private feed at a secret URL** (chosen). This is how calendars are published for
   subscription ("Add calendar", "Subscribe from web" in Outlook), and it is the podcast feed's
   design (ADR 0002). The owner decides whether and where to subscribe; Dojo never reaches into
   the calendar.

## The decision

**Planning without the calendar.** Dojo plans only inside free times and limits the owner sets on
the Plan page, never around his meetings, because it cannot see them. The defaults follow the
storyline: at most 60 minutes on weekdays and 2 hours on Saturday, Sunday off, nothing before 08:00,
at most 7 hours a week, a reminder 10 minutes before, and free times at 08:30 (the commute, audio
only), 12:30, 18:00 and Saturday 10:00. An audio-only time takes only listening. What a session asks
for comes from the Record: the next steps on Today, later checks once they come due (20 hours after
the last teaching), a 3-minute spoken check a day per exam while untested skills are left, a
practice exam a week in the last four weeks before each exam date, and a DP-800 lab a week (ADR
0003). A lab is the first one not yet passed, takes 20 minutes and needs a desk, so it never goes
into an audio-only time. A lab checked in a week, passed or not, is that week's lab, and one not
passed comes again the next week. Lab work restarts a skill's teaching clock, so Dojo plans no lab
for a skill that waits for its later check. Dojo places sessions two weeks ahead, and nothing of an
exam on its exam day or after it. The plan is a proposal (M-02): the owner moves, skips or pins any
session, or changes the rules. What he moved or pinned keeps its time unless it is missed, or it leads
nowhere now: its exam was removed, or a source change took what it needs (below).

Free times are Berlin wall-clock times, and sessions are kept as moments. A free time runs from the
first moment the clock reaches its start to the first moment it reaches its end, and a session must
lie inside it as moments. On the night the clocks go forward, Dojo plans nothing at a time that does
not exist, and a session that runs over the change ends an hour later on the clock. In the hour that
repeats in autumn, a time means its first moment, so a session starts there only the first time
round, but it may run on into the second: 45 minutes from the first 02:30 end at the second 02:15,
inside a free time to 03:00. No two sessions overlap, also over a change or over midnight. A session
the owner puts late in the evening may run on past midnight. It holds its time on both days, and its
minutes count in the day and the week they fall in, on the Plan page too, where it is listed on the
day it starts. Moving a session to a time that does not exist is refused.

**Added exams.** An exam added later (ADR 0005) is planned like the built-in ones. A skill of it with
no verified source gets no session, because Dojo does not teach it or ask about it. So a 3-minute
check is planned only while a skill it can ask about is untried, the same rule the check uses to pick
its skills. Removing an added exam removes its planned sessions, pinned ones too. What was done stays,
and still counts in that week's time.

**Source changes.** A hold from the source-drift check (ADR 0006) is planned around in the same way,
for as long as it lasts. A skill whose every cited page is gone gets no session, and the 3-minute check
does not pick it, as nothing new can be written for it. Its lab stays planned. A held lesson gets no
Lesson or Listen while it is written again; the new lesson is planned once it is there. Such a session
planned before the hold leaves the plan and the calendar, pinned or moved too, like a session of a
removed exam: for a skill whose every page is gone, every session but its lab; for a held lesson, its
Lesson and Listen. "Moved for you" says "Removed", and why. What was done stays. When the page is back,
or the new lesson is there, the step is planned again.

**Done, read from the Record.** A session is done only when the Record holds a matching event from
after it was planned (for example `lesson.viewed` for a lesson, `answer.submitted` without help for a
check, `exam.started` for a practice exam, `lab.checked` for that lab, passed or not; opening a lab,
running SQL or a hint is not enough). A missing event is never read as done (M-03). A later check is
done only by an answer made as one, read from the same attempt fields as the evidence
(`app/evidence.py`): not disputed, without help, and at least 20 hours after the skill's last
teaching before that answer. Like a check or a lab, it is done when it was sat, whether every point
was met or not; the evidence decides whether another one is needed. Its own feedback comes after it
and does not undo it; an answer not judged yet does not count. A session
that ends without one moves to the next free slot, or waits if none is left, and "Moved for you"
says so. A missed 3-minute check is not carried over, because one is planned every day. Sessions
within the next 24 hours keep their time while they are still needed, still fit the rules and are not
under a session the owner put there. Later ones are placed again as the Record changes, keeping their
calendar event. A later check whose skill the owner worked on since (a lesson, a question, a lab)
moves to when it comes due again, even inside those 24 hours, because a check before then would not
be a later check.

**Where the plan lives.** `learners/<owner>/plan.json`: the rules, the sessions, Dojo's moves of the
last 14 days, and the steps the owner skipped, until the next day. It is the learner's own data, like
the profile, and never an event in the Record: a plan is not evidence. It is in the export and is
removed by "Delete my record".

**What is open without sign-in.** `infra/main.bicep` adds `/cal/*` to
`globalValidation.excludedPaths`, next to `/healthz` and `/feed/*` (wildcards are documented in
https://learn.microsoft.com/azure/app-service/configure-authentication-file-based). Under `/cal/`
the app answers only `GET` or `HEAD /cal/<token>/dojo.ics`: the plan as an iCalendar feed (RFC
5545). A wrong token, no token or any other path under `/cal` gets 404 with an empty body. Any other
method gets 405. The routes never call `current_learner` and never read `X-MS-CLIENT-PRINCIPAL`; a
valid token maps to the configured owner explicitly, and to nothing else.

**What the feed contains.** One event per session, from 7 days back to the end of the plan. Each has:

- the title: exam, activity and the skill's name from the public study guide, for example
  "GH-300 · Lesson · Describe risks and limitations of Generative AI tools";
- its start and end as moments in UTC (`DTSTART:…Z`, `DTEND:…Z`), which the calendar app shows in
  its own time zone. A Berlin wall-clock time would be ambiguous in the hour that repeats in autumn,
  so the feed needs no `VTIMEZONE`;
- a description with the length and the link, and "Move or skip it on Dojo's Plan page.";
- the link (`URL`) into the signed-in app, to exactly that activity: `https://<host>/?go=lesson/<id>`,
  `/?go=lesson/<id>/listen`, `/?go=skill/<exam>/<skill>`, `/?go=check/for/<exam>`, `/?go=recall/for/<exam>`,
  `/?go=rehearsal/for/<exam>` or `/?go=lab/<lab>` (amended 5 Oct 2026; they were `#/lesson/<id>` and so on, see
  "Links into Dojo and sign-in"). It carries no token;
- `CLASS:PRIVATE`, and a reminder (`VALARM`, `ACTION:DISPLAY`) the set number of minutes before,
  on sessions still to come;
- a stable `UID` (the session's id), with `SEQUENCE` and `LAST-MODIFIED` raised on every change, so
  a calendar app updates the event in place when Dojo moves it, instead of adding a second one.

Lines are folded at 75 octets and end in CRLF, and text is escaped, as RFC 5545 requires. The feed
is written with the standard library.

**What the feed does not contain.** No name, no answers, no results, points or pips, no evidence,
no readiness, no "why this, now", no "missed" or "done". The calendar sits in a corporate mailbox,
so its text is the least a plan can say and still be a plan. Skipped sessions leave the feed. A
session done before its planned time leaves it too: that time is free again.

**What the feed writes.** A `GET` brings the plan up to date, exactly as opening the Plan page does
(`plan.json`), and notes when the feed was fetched (`calendar.json`, at most every 5 minutes). That
note is a time and nothing else: no address, no app name, no count. A `HEAD` writes nothing.
Nothing on these routes writes an event into the Record. A fetch cannot move, skip or pin anything;
only time and the Record move the plan. The feed runs under the store lock, so turning the link off
or deleting the record never lands in the middle of a fetch. If bringing the plan up to date fails,
the feed serves the plan as it was and logs the failure without the path.

**The token.** The same design as the podcast link (`podcast.FeedLinks`, ADR 0002):

- 32 random bytes from `secrets.token_urlsafe` (256 bits, 43 URL-safe characters).
- Made only when the owner presses "Get my calendar link". It is shown in that answer only, with a
  QR code for a phone. The page keeps it in memory until it is closed or reloaded.
- The server stores only a SHA-256 hash (of `dojo-cal:` + token), when it was made and when a
  calendar app last fetched it, in `learners/<owner>/calendar.json`. The token itself is stored
  nowhere. The prefix differs from the podcast's, so a podcast token never opens the calendar.
- Checked with `hmac.compare_digest`. A hash is compared even when there is no link or the token has
  the wrong shape.
- "Make a new link" replaces the hash, so the old link dies at once. "Turn the feed off" and "Delete
  my record" remove it. The calendar link and the podcast link are separate: turning one off
  leaves the other.
- The QR code is drawn on the server with segno, which Dojo already uses, inside the signed-in
  `POST /api/calendar/link` answer.

**The path guard.** `/cal` joins `/feed` in the guard middleware. It refuses (400), on every route,
any raw path with a `.` or `..` segment, a backslash, or an encoded dot, slash, backslash or percent
sign. So `/cal/../api/me` and `/cal/%2e%2e/api/me` never reach the API, and App Service
authentication and the app never disagree about whether a path lies under `/cal/`.

**Caching and indexing.** Feed answers carry `Cache-Control: private, no-cache` and
`X-Robots-Tag: noindex, nofollow`. The feed asks to be fetched every hour (`REFRESH-INTERVAL`,
`X-PUBLISHED-TTL`). Calendar apps decide for themselves: Outlook says updates "should happen
approximately every 3 hours" and "can take more than 24 hours" (support page above). The Plan page
says "every few hours" and shows when the feed was last fetched.

**Logging (EG-29).** The token is a bearer credential, so it must not land in logs. Everything in
ADR 0002's logging section holds for `/cal` as it does for `/feed`:

- The app's own request log writes `/api` paths only. Calendar feed requests are not logged, and a
  refused path is logged without the path.
- gunicorn writes no access log, and App Service on Linux has no web server request log. Dojo has
  no diagnostic settings.
- The site's application log level stays `Warning`, so the App Service authentication module does
  not write its "Request starting" lines, which would carry `/cal/<token>/dojo.ics`. The test
  `test_only_healthz_and_feed_are_open_and_nothing_logs_request_urls` pins the open paths, now with
  `/cal/*`, and fails if the level goes below `Warning`.
- Run locally, plain uvicorn prints an access log with tokens, so the README's command passes
  `--no-access-log`.

**Links into Dojo and sign-in.** An event's link opens the signed-in app. When the owner is not
signed in, App Service authentication sends him to sign in first. The part after `#` never reaches
the server, so `infra/main.bicep` sets `login.preserveUrlFragmentsForLogins` to `true` (documented
in the file-based configuration reference above, and as "Preserve URL fragments" in
https://learn.microsoft.com/azure/app-service/configure-authentication-customize-sign-in-out).
The link then still opens that activity after sign-in.

**Amended 5 Oct 2026:** that last sentence was wrong. App Service authentication keeps the path and the
query of the address it signs the owner in for, and loses what follows `#`. Its sign-in page keeps the fragment
in a `PreLoginUrlFragment` cookie that script writes without `SameSite`, so the browser treats it as `Lax`, and
the return from Entra is a cross-site `form_post`, which does not carry a `Lax` cookie. Seen live: signing in
from `/#/help` landed on `/` with `preserveUrlFragmentsForLogins` on, while a request without a session for
`/?go=lesson/abc-0123456789abcdef0123/present` put `state=redir=/?go=lesson/abc-0123456789abcdef0123/present`
in the authorize URL, so signing in lands on that exact address.

So an event's link is now `https://<host>/?go=<route>`, the route as before without the `#/` (for example
`/?go=skill/gh-300/d1.g1.s1`), and when the app starts it turns `?go=<route>` into `/#/<route>` and drops the
query (`app/deeplink.py`; `openDeepLink` in `app/static/app.js`). The server ignores `go`. A route is one to
six parts of lowercase letters and digits that may be joined by a single `.`, `_` or `-`, at most 120
characters. It is decoded once, and it only ever goes after the fixed `/#/` in `history.replaceState`, so it can
never be an address or take the page to another origin; anything else is dropped and the start page (Today)
opens. `preserveUrlFragmentsForLogins` stays on, but nothing relies on it: a `#/…` link still opens its page for
a signed-in owner. Dojo does not raise `SEQUENCE` for this change, so a calendar app that keeps an event it
already has may keep the old `#/…` link; removing the Dojo calendar there and subscribing again gives every
event the new one.
Evidence: `tests/test_deeplink.py`. ADR 0013 has the same change for nudges and for **Reload**.

**Exposure.** Who can see the plan, and what they learn from it:

- Outlook on the web fetches a subscribed calendar from Microsoft's servers and keeps its events in
  the mailbox. So the owner's employer's Microsoft 365 holds the event text and the subscription
  URL. Treat the link as known to the mailbox's operators. That is why the text is minimal and
  every event is marked private; the private mark is a request, not a control.
- The plan follows the Record, so its shape says a little. A session that moves after its end was
  not done; one that leaves the calendar before its time was done early. The kind of session hints
  at progress: practice usually follows a miss, a later check follows an answer without help, and a
  lab that comes back the next week was not passed. The events never say so and carry no result. A
  title that names only "Dojo" is a small change if the owner wants it.
- Anyone who has the link can read the plan until the owner makes a new one. It opens nothing else:
  no Record, no lessons, no results.
- Unlike the podcast feed, the calendar feed stays on during a timed practice exam, and a link can
  be made then. It carries no teaching, only names and times, so it cannot change an exam's
  conditions.

## What is kept, and what is not

- Kept by Dojo: the token's hash, when it was made and when a calendar app last fetched the feed
  (`calendar.json`; one time, overwritten); the plan (`plan.json`), which is the learner's own data,
  in the export and removed with the record. Sessions that ended more than 35 days ago, and Dojo's
  moves older than 14 days, are dropped from it.
- Not kept: the token, and anything else about feed requests: no address, no app name, no count.
- Out of Dojo's reach: the calendar app keeps the events and the subscription URL. Dojo cannot
  delete them. Turning the feed off stops updates, and the events stay until the owner removes the
  Dojo calendar there. The Plan page says so (EG-26).

## Consequences

- A second part of Dojo is reachable by anyone with a link: the plan. A 256-bit token cannot be
  guessed, but a leaked link works until the owner makes a new one. It has no expiry, because an
  expiring link breaks the subscription.
- Dojo cannot see meetings. A session can clash with one. The owner sets his free times around his
  usual week, and moves or skips a session that clashes.
- A change reaches the calendar at its next fetch, hours later. The Plan page is always current,
  and it says when the calendar last fetched.
- Whether a reminder rings is up to the calendar app. Some apps drop the reminders of a
  subscribed calendar. Dojo cannot see whether one rang.
- A leaked link lets its holder read the plan and makes "last fetched" unreliable, since any fetch
  sets it. It cannot change what is planned.
- Listening in a podcast app counts as done only when the owner says so at the next check
  (`podcast.declared`, ADR 0002). Dojo cannot see a podcast app.
- No new dependency (EG-43). The standard library writes the iCalendar text. Dojo works out Berlin
  time with its own EU summer-time rule, and a test checks it against the time zone database. CI
  and App Service run Linux, which has one; Windows has none without the `tzdata` package, so there
  the test skips.

## Guardrail exception

| Field | Value |
| --- | --- |
| Guardrails | EG-6 (a bearer credential where no identity flow is possible) and EG-29 (a credential in a URL path): a bearer capability URL for the owner's study plan |
| Reason | Calendar apps, Outlook on the web among them, fetch subscribed calendars without sign-in. Writing into the work calendar through Graph is the owner's decision (EG-5, EG-41) and is not taken |
| Alternatives | Options 1 to 4 above |
| Risk | Low. A leaked link lets its holder read the planned sessions (exam, activity, skill name, time, length, link) until the link is renewed. It opens no Record, no result and no lesson. Its requests write only what opening the Plan page writes, and the time of the last fetch |
| Affected users and data | The owner only: his study plan, made of times and public study-guide names. No evidence |
| Scope | `GET` and `HEAD` of `/cal/<token>/dojo.ics` |
| Owner | The owner, the only user; decided by the Dojo lead |
| Protected storage | The hash only, on the web app's own `/home` share |
| Rotation and expiry | No automatic expiry; "Make a new link" at any time; the review triggers below |
| Incident response | Make a new link or turn the feed off; both work at once. Events already in a calendar stay until the Dojo calendar is removed there |
| Mitigation | 256-bit token, hash-only storage, constant-time check, 404 without detail, path guard, no request logging, minimal event text marked private, `noindex` |
| Deterministic evidence | `tests/test_plan.py`: `IcsTests`, `FeedContentTests` (among them `test_an_event_says_what_how_long_and_where_and_nothing_else` and `test_the_same_session_is_the_same_event_so_outlook_updates_it`), `CalendarLinkTests`, `CalendarFeedRouteTests` (among them `test_a_get_writes_only_the_plan_and_when_it_was_fetched_and_a_head_nothing` and `test_the_token_is_never_written_to_a_log`), `CalendarLinkLifeTests`, `CalendarSignInTests`, and `PlannerTests.test_the_planner_writes_no_evidence`; `tests/test_podcast.py`: `test_only_healthz_and_feed_are_open_and_nothing_logs_request_urls`; `tests/test_deeplink.py` (amended 5 Oct 2026): the rule for a route, run in node against `app.js` and `sw.js`, and the `/?go=` links |
| Decision | Accepted by the Dojo lead under the owner's standing instruction to decide and build autonomously, 2026-09-30; the owner can veto it at any time by turning the feed off or by saying so. Review triggers as listed below |

**Who decides.** The feed serves the owner's own plan to the owner's own calendar, and writes
nothing into the Record. So "Exception and change control" lets a solo project owner decide it,
with recorded alternatives, risk, deterministic evidence and an explicit later review trigger. That
is an owner decision, not independent human review. The Graph option is not decided here; it stays
the owner's.

## Review trigger

- The owner decides on Graph (option 1), either way.
- Anyone other than the owner gets access to Dojo.
- The link may have leaked: make a new link first, then review.
- The feed is to carry more than names, times and links: results, reasons, evidence or the owner's
  words. That is no longer a low-risk exception, and needs independent review.
- Any change that could log request URLs: a gunicorn access log, diagnostic settings for App
  Service HTTP logs, Application Insights request telemetry, a proxy or CDN in front of the app, or
  the site's application log level below `Warning`.
- A change in how App Service authentication matches `excludedPaths`, or in what it keeps of an address across
  sign-in (event links rely on the query surviving it, and on nothing after `#`), or a move away from it.
- At the latest after the exams, in March 2027.

## How to undo it

1. Press "Turn the feed off" on the Plan page, or delete `learners/<owner>/calendar.json`: every
   link answers 404 at once. Remove the Dojo calendar in Outlook to remove its events there.
2. Remove `'/cal/*'` from `excludedPaths` in `infra/main.bicep` (and from the pinned test) and
   deploy: App Service authentication asks for sign-in on `/cal` again.
3. Remove the `/cal` and `/api/calendar` routes from `app/main.py`; `CalendarLinks`, `write_ics`,
   `calendar_ics`, `link_view` and `feed` from `app/plan.py`; the calendar panel and chip in
   `app/static/app.js` and their styles; and `IcsTests`, `FeedContentTests`, `CalendarLinkTests`,
   `CalendarFeedRouteTests`, `CalendarLinkLifeTests` and `CalendarSignInTests`. The Plan page and the
   planner work without the feed.
4. Keep the path guard; it protects the rest of the app too. Keep `app/deeplink.py` and the `?go=` handling
   in `app.js` and `sw.js`: nudges and **Reload** use them too (ADR 0013). `preserveUrlFragmentsForLogins`
   can stay: it is harmless, and since the amendment of 5 Oct 2026 nothing relies on it.
5. The plan (`plan.json`) is the learner's own data and nothing in the Record refers to it. Keep it
   for the Plan page, or delete it: that loses only the plan and its rules.
