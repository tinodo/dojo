# ADR 0013 - Phone: install Dojo as an app, opt-in push nudges, and a 5-minute session

- Status: proposed
- Date: 2026-10-02
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder of the "Phone" feature (branch `feature/phone`). The lead can reject or
  change it in review. Review triggers are below.
- Guardrails: vision section II.6 (shame is the dropout mechanism; no punished streaks), the I2E contract
  (a nudge is not evidence and never changes a pip), EG-5 (identity is separate from connectors: Dojo
  still holds no Teams or Graph permission), EG-12 (record consequential decisions), EG-24 (isolation
  begins with the data model; tested with two synthetic learners), EG-43 (supply-chain gate for new
  packages), ADR 0004 (the Plan and its calendar feed), ADR 0009 (Team Dojo: learner routes and the
  Store), ADR 0011 (successive relearning and its daily cap)

## The requirement

Successive relearning (ADR 0011, live since #37) only works if the learner comes back on the days a
recall is due. The research notes say the same from two sides:

- `research/2026-10-01-learning-gaps.md` item 11: retrieval spread over days is the strongest lever Dojo
  has, and nothing reminds the learner when a recall is ready.
- `research/2026-10-01-multi-user-tech.md` item 7: an installable web app with Web Push reaches phones
  without a store app, without Teams and without a Graph permission; iPhone and iPad support it only for
  web apps added to the Home Screen (iOS and iPadOS 16.4 and later).

DESIGN.md screen 11 (the phone) promised "Teams nudges". They were never built because Dojo holds no
Teams or Graph permission (EG-5; the owner's open decision D). The phone page today offers the podcast feed and
the calendar feed. A short, calm nudge at a time the learner chose, only on days when something is due,
is the cheapest way to make the recalls happen.

## What the official pages say (checked 2026-10-02)

- WebKit: "Web Push for Web Apps on iOS and iPadOS" (https://webkit.org/blog/13878/): from iOS and iPadOS
  16.4 a web app that the user added to the Home Screen can ask for notification permission, and only in
  response to a direct user action such as a tap. A page open in Safari cannot.
- web.dev "Learn PWA" (https://web.dev/learn/pwa/) and MDN "Web app manifests"
  (https://developer.mozilla.org/docs/Web/Progressive_web_apps/Manifest): `name`, `icons`, `start_url`
  inside `scope`, `display: standalone`. A manifest behind a sign-in needs `crossorigin="use-credentials"`
  on the `<link rel="manifest">`, or the browser fetches it without cookies.
- MDN "Push API" (https://developer.mozilla.org/docs/Web/API/Push_API) and RFC 8030 (Web Push), RFC 8291
  (message encryption) and RFC 8292 (VAPID): the push service only ever sees an encrypted payload; a push
  service answers 404 or 410 when a subscription is gone.
- pywebpush (https://github.com/web-push-libs/pywebpush): the reference Python implementation of RFC 8291
  and RFC 8292 from the web-push-libs project.

## The options

1. **Teams nudges (screen 11 as designed).** Needs a Teams bot or a Graph permission for every learner.
   That is the owner's decision D and is still open. Not now.
2. **Email.** Needs Graph `Mail.Send` or an outside mail service, a permission or a new processor of
   personal data. Rejected.
3. **Calendar reminders only.** Already there (ADR 0004: the Plan's calendar feed has a reminder per
   session). A calendar entry cannot know whether a recall is due when the reminder fires, and a learner
   who does not subscribe the calendar gets nothing. Kept, not enough.
4. **A store app (Android, iOS).** A second code base, store accounts and review. Rejected.
5. **Installable web app with opt-in Web Push (chosen).** No Microsoft permission, no new service, no cost.
   Works on Android, Windows, macOS and Linux browsers, and on iPhone and iPad once Dojo is on the Home
   Screen.

## The decision

### 1. Installable web app (PWA)

- `app/static/manifest.webmanifest`: `id` and `start_url` `/`, `scope` `/`, `display: standalone`, name
  "Dojo", theme and background colours from the design tokens, icons rendered from `app/static/icon.svg`
  (the SVG itself with `sizes: any`, PNG 192 and 512, a maskable 512 with a safe margin, and a 180 px
  `apple-touch-icon` for iOS). It is linked from `index.html` with `crossorigin="use-credentials"`
  because the whole site is behind Easy Auth.
- **A service worker that caches nothing.** `app/static/sw.js` has no `fetch` handler and never stores
  anything in the Cache API; on `activate` it only deletes any cache that a later version might have left. It
  handles `push` (show the notification), `notificationclick` (focus Dojo or open it on `/?go=five`; a URL in a
  payload is accepted only as `/?go=<route>` on this site with a valid route, anything else opens
  `/?go=today`; amended 5 Oct 2026, see "What it says" below) and `pushsubscriptionchange`
  (the browser renewed the subscription: the worker sends the new one to `POST /api/push/devices`). Nothing private, and no API
  response, is ever stored by the worker. Offline use is not a goal: every Dojo page needs the server and
  a valid sign-in anyway, and a cached shell could show a page after the sign-in has ended or after a
  deploy. Chromium no longer needs a `fetch` handler for the install prompt.
- The worker is served from `/static/sw.js?v=<build>` and registered from `app.js` (strict CSP: no inline
  script) with `scope: "/"`. The middleware adds `Service-Worker-Allowed: /` and `Cache-Control: no-cache`
  to `sw.js` and the manifest, so a new build is picked up at the next visit. No new public route, no
  new mount: the route inventory from ADR 0009 stays as it is.
- `/healthz`, `/feed/*` and `/cal/*` are unchanged: a worker without a `fetch` handler never sees a
  request, and podcast and calendar apps never run the worker. Easy Auth settings are unchanged.
  **Amended 5 Oct 2026:** that was not enough. The browser checks the worker's script for a new version at
  every page load in its scope, outside any page, and loads a nudge's icon the same way. Without a session,
  Easy Auth answered those requests with its sign-in page and a new nonce cookie, which replaced the nonce
  of the sign-in under way: every sign-in failed ("invalid nonce") and started again, endlessly, once a
  session had ended. Easy Auth now leaves `/static/sw.js` and `/static/icon-192.png` open (static, public
  files), Dojo's 5-minute build check reads `/healthz` instead of `/api/me`, and every other request Dojo
  sends (all of `api()`, the exam check before offering a reload, the worker's subscription renewal) sends
  `X-Requested-With: XMLHttpRequest`, so Easy Auth answers 401 or an empty 403 instead of starting a
  sign-in. Dojo then shows "Your sign-in has ended" with **Reload**, which asks for `/?go=<the page you were
  on>` (see "What it says" below), and never starts a sign-in on its own.
  What is left: audio and video load through the browser's media player, which cannot send that header. If a
  sign-in ends while one is still loading, its next request can reset the nonce (the player then stops with
  an error), so a sign-in in another tab at that moment may need a second try. It cannot loop.
  Closing that too would need a worker `fetch` handler or open media addresses, both against this ADR.

### 2. Opt-in Web Push nudges (per learner, per device)

**Keys.** One VAPID key pair (P-256) for the whole app, made at first need (the first time any learner
opens the nudge settings) and stored on the data share at `push/vapid.json`, like the feed tokens: never
in git, never in a log, never in a response. The file is created exclusively (`O_EXCL`) and readable by
the owner only (0600, where the file system honours it), so a second creator, such as another worker
process, can never replace a key that devices were subscribed with: the loser of that race reads the
winner's key. A key file that is there but broken is never replaced silently (nudges answer 503 and the
error is logged). Only the public key is served (`GET /api/push`). Each device
remembers which public key it was subscribed with; if the key file is ever replaced, old devices are
dropped and the learner is asked to turn nudges on again.

**What the learner chooses.** A time of day (Berlin time, in 5-minute steps, default 08:00) and the days
(default Monday to Friday). Nudges are off until the learner taps "Turn on nudges on this device" and
allows notifications in the browser. Each device is separate; at most 5 devices per learner. A device is
known by `dev-` and the first 20 hex digits of SHA-256 of its push endpoint, so the page can tell which
entry is "this device" without the server ever sending an endpoint back.

**When a nudge is sent.** All of these must hold:

1. Today (Berlin) is one of the learner's days, and the time is between the chosen time and two hours
   later. The nudger looks every 5 minutes, so a nudge comes within a few minutes of the chosen time. A
   window that runs over midnight belongs to the chosen day: what is due (recalls and the Plan) is judged
   for that day.
2. No nudge was sent today: **at most one nudge a day**, for all of the learner's devices together. The
   day is reserved in `push.json` before the first message goes out, so a restart between a push service
   accepting it and Dojo writing the outcome can never send it twice. A send that fails loses that day's
   nudge; that is the accepted price of "at most once".
3. Something is due: recalls that the 5-minute session would ask now (due today and within the day's cap
   from ADR 0011), or a session in today's Plan that is still planned (not done, dropped or skipped). The
   nudger only reads; it never refreshes the Plan or writes a recall.
4. No timed practice exam is open (a nudge must not interrupt or help an exam), and the learner may use
   Dojo: the owner, or an active member who accepted the notice and is not paused (ADR 0009).

**What it says.** Calm, counts only, no skill names, no results, nothing about what was missed:
"2 recalls are ready, about 4 minutes." or "Your plan has 1 session today, about 25 minutes." The title is
"Dojo". There are no streaks, no "don't lose", "overdue", "behind" or "missed"; a day without a nudge,
or a nudge that is ignored, has no consequence anywhere in Dojo. Tapping it opens `/?go=five` (recalls) or
`/?go=today` (a planned session).

**Amended 5 Oct 2026:** a nudge used to carry `/#/five` or `/#/today`. Easy Auth loses what follows `#` when it
signs someone in: its sign-in page keeps the fragment in a cookie that script writes without `SameSite`, so the
browser treats it as `Lax`, and the return from Entra is a cross-site `form_post`, which does not carry it. A
nudge tapped after the session had ended therefore opened Today, whatever it named. Easy Auth does keep the
path and the query, so the payload's `url` is now `/?go=<route>` (`/?go=five`, `/?go=today`, and `/?go=phone`
for the test nudge), and when `app.js` starts it turns `?go=<route>` into `/#/<route>` and drops the query
(`openDeepLink`; ADR 0004 has the rule for a route and why a route cannot leave the app). The worker's
`safePath` accepts a payload `url` only as `/?go=<route>` with a route that follows that rule, or as the old
`/#/<route>`, which it rewrites to `/?go=<route>`, so a nudge sent before this build still opens its page;
anything else becomes `/?go=today`. It builds the address from the fixed text `/?go=` and a checked route on
its own origin, so it can never open another site. The in-app **Reload** after "Your sign-in has ended" does
the same: it asks for `/?go=<the page you were on>`, or for `/` when the address holds no valid route, and
Dojo still never starts a sign-in itself.

**How it is sent.** A daemon thread (`Nudges` in `app/push.py`) wakes every 5 minutes, walks the learners that have a
`push.json`, decides with a pure function, takes a job slot with a timeout (the slot discipline every
background job uses; if no slot is free it tries again on the next tick, the two-hour window leaves room)
and sends with `pywebpush`. Once it has the slot it looks at everything again (an open exam, the
membership and pause, the settings, what is due) right before it reserves the day and sends, and before
each device it looks again at the exam, the pause and the membership: if one changed, the remaining
devices get nothing (the day stays used; at most once). The app-wide store lock is not held across a push
POST (up to 10 seconds each), so the window left is a single POST. No model is called. A 404 or 410 from the push service removes that device at
once; a device that fails 5 times in a row is removed too. Every write goes through the Store under the
learner's key (`writing_as`), so a learner who leaves or is deleted mid-send leaves nothing behind. The
send function is injected, so tests never reach a real push service.

**Where it may send to (SSRF guard).** An endpoint is accepted only over https on a known push service:
`fcm.googleapis.com` (Chrome, Edge on Android), `updates.push.services.mozilla.com` and
`*.push.services.mozilla.com` (Firefox), `*.notify.windows.com` (Edge on Windows), `web.push.apple.com`
and `*.push.apple.com` (Safari). No other host, no port other than 443, no user name in the URL.
The keys must have the lengths RFC 8291 requires (65-byte P-256 point, 16-byte auth secret).

**Routes** (all learner routes in the ADR 0009 inventory, `Depends(current_active_learner)`):
`GET /api/push`, `PUT /api/push/settings`, `POST /api/push/devices`, `DELETE /api/push/devices/{id}`,
`DELETE /api/push` (off everywhere: deletes `push.json`), `POST /api/push/test` (one test nudge to this
device, at most once a minute).

### 3. The 5-minute session (`#/five`)

One entry point that the nudge opens and Today links to:

1. **Recalls first.** Up to 2 of today's due recalls (2 minutes each, ADR 0011), always within the day's
   cap: the session starts a recall check limited to 2 questions. More recalls stay on Today. If an
   ordinary check is open, the page says "You have a check open" and offers to go on with it, or to end
   it and start the 5 minutes; Dojo never ends it unasked.
2. **Then one repair** of a confident error, if there is one (the newest from "Know what you know"): read
   the lesson section it points to, then answer one new question without hints. The question is written
   and gated like any other check. The repair counts as done only once that question has been answered;
   if writing it fails, the page stays on the repair with a calm "try again in a moment".
3. **Then it stops**, with a clear end: "That is it for today." and a link back to Today. Nothing asks for
   more.

The step lives in the browser's session storage for that day only. If nothing is due the page says so and
offers Today. While a timed exam is open it says it opens after the exam. Answers go to the record exactly
as recalls and checks always have; the session itself is not evidence.

### 4. iPhone and iPad in the UI

The phone page (`#/phone`) shows, next to the podcast and calendar links:

- "Dojo as an app": an Install button where the browser offers one; otherwise the steps; "Installed" when
  Dojo already runs as an app.
- "Nudges": time, days, "Turn on nudges on this device", "Send a test nudge", "Turn off on this device",
  "Turn off everywhere", and the list of devices with their labels.
- "iPhone and iPad": iOS or iPadOS 16.4 or later; open Dojo in Safari; Share, then "Add to Home Screen";
  open Dojo from the Home Screen and sign in; open this page and tap "Turn on nudges"; tap Allow. Safari
  on its own cannot receive nudges.

## Privacy

- **Stored by Dojo, per learner** (`learners/<key>/push.json`): the chosen time and days; for each device
  its push endpoint, its two public encryption keys, a label from the browser ("Edge on Android"), when it
  was added, when the last nudge reached it and a failure count; the Berlin day and time of the last
  nudge. No content of any nudge is stored.
- **App-wide** (`push/vapid.json`): the VAPID key pair and the date it was made. Not personal data.
- **What leaves Dojo:** to the browser maker's push service (Google, Mozilla, Microsoft or Apple) the
  endpoint, the time and an end-to-end encrypted payload that the push service cannot read. The VAPID
  contact is the site's https address, not an e-mail address. The text appears on the device's lock screen,
  which is why it has counts only.
- **Retention:** until the learner turns a device or all nudges off, the push service reports the device
  gone, the device fails 5 times in a row, the learner deletes their record, or leaves the team (the whole
  `learners/<key>/` folder goes, ADR 0009). When a member is removed (by the owner, because the
  membership ran out, or because team mode ends) `push.json` is deleted at once, not after the 30-day
  grace period: the endpoints are capability URLs and a removed member gets no nudge anyway. A pause
  deletes nothing (as ADR 0009 promises); no nudge is sent while it lasts. Backups of the share follow the existing 30-day rule.
- **How to switch off:** "Turn off on this device", "Turn off everywhere" (deletes `push.json`), or the
  browser's or phone's notification settings (Dojo notices at the next send and removes the device).
- **Export:** the settings and device labels are part of the learner's data and appear in the export; the
  endpoint and keys are left out (they are only useful to Dojo).

## Dependencies (EG-43)

- New direct pins in `requirements.txt`: `pywebpush==2.5.0` (MPL-2.0, web-push-libs, current release),
  `py-vapid==1.9.4` (MPL-2.0, web-push-libs) and `http-ece==1.2.1` (MIT, web-push-libs; source
  distribution only, pure Python).
- Transitive, pulled in by pywebpush: `aiohttp 3.14.3` with `aiohappyeyeballs`, `aiosignal`, `attrs`,
  `frozenlist`, `multidict`, `propcache`, `yarl` (Apache-2.0 or MIT, maintained by aio-libs). Dojo does
  not use pywebpush's async path; aiohttp is accepted as an unused transitive dependency rather than
  vendoring a fork. `cryptography` and `requests` are already in the app.
- Why a library: RFC 8291 encryption and RFC 8292 signing are security code; the reference implementation
  is safer than our own. MPL-2.0 is file-level copyleft; Dojo uses the files unmodified, so nothing changes
  for Dojo's own code.

## Money

None beyond App Service. Push services are free, there are no model calls, and a nudge check every 5
minutes reads a few small files per learner.

## Defaults the owner may change

Default time 08:00, default days Monday to Friday, the two-hour window, 2 recalls in the 5-minute session,
5 devices per learner, 5 failures before a device is removed.

## Consequences

- Dojo can reach a learner's phone without Teams, Graph or a store app, and only when the learner asked.
- A nudge opened after the Easy Auth session expired goes through the sign-in first, and Easy Auth returns
  to the same `/?go=…` address, which the app turns into the page. (Until 5 Oct 2026 the nudge carried `/#/five`
  and the learner landed on Today: Easy Auth loses the `#` part, see "What it says".)
- Notification icons are fetched by the browser and sit behind Easy Auth like every static file; if the
  browser fetches them without the cookie the nudge shows without the Dojo icon. Easy Auth stays as it is.
- One more background thread; it is cheap and starts only in Azure or with `DOJO_PUSH=1` locally.
- The day claim is process-local: it is written to `push.json` under the Store lock, which is a lock
  inside one process, like every Store lock in Dojo. That is correct for the supported deployment, which
  runs exactly one process on one instance (ADR 0009, "Capacity"). Two processes could each claim the
  same day and send it twice; Dojo does not add an interprocess claim for a deployment it does not run.

## Review triggers

- If Dojo ever runs more than one process or instance, the day claim must become an atomic claim on
  shared storage (and the other Store locks need the same review).
- The owner decides D (a Teams or Graph permission): reconsider Teams nudges next to or instead of Web Push.
- Apple, Google, Microsoft or Mozilla change the rules for web push or install (for example EU changes to
  Home Screen web apps on iOS).
- Any learner says a nudge felt like pressure, or asks for more than one a day.
- Anyone proposes putting skill names, results or other content in the payload, or caching pages offline.
- Team mode grows past a handful of learners (the nudger walks all learners every 5 minutes).

## How to undo it

Remove the push routes and the nudger thread, ship a `sw.js` that calls `self.registration.unregister()`,
remove the manifest link, and delete `push/vapid.json` and every `learners/*/push.json`. The 5-minute
session is independent and can stay.
