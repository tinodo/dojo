# ADR 0009 - Team Dojo: several learners in one Dojo, switched off by default

- Status: proposed (design only; no application code changed). Revised after the first design review (GPT
  "rework", Gemini "approve with fixes") and after the fix check. The operator-access question (G1) is
  settled by a lead decision, recorded under "Operator access and the residual risk".
  **The real-learner pilot is BLOCKED** until the accountable
  privacy owner outside the build team and, for colleagues in Germany or the Netherlands, the works
  councils accept the residual risk of operator access in writing (see "Operator access and the residual
  risk").
- Date: 2026-10-01 (revised the same day)
- Decision owner: the owner. Only the owner can switch team mode on. Before a real-learner pilot they name an
  accountable privacy owner outside the build team, who reviews `docs/privacy.md`.
- Decision: proposed by the Team Dojo builder. The lead may reject or change it in review. The review
  triggers are below.
- Guardrails: EG-5 (human identity is separate from permissions; admission policy; who can read the
  learner), EG-7 (least privilege reaches the learner record), EG-24 (ownership and isolation start in
  the data model; tests use two learners; no tiny-cohort exposure), EG-25 (disclosure is explicit),
  EG-26 (deletion is real), EG-29 (observability is not surveillance), EG-36 (an outside privacy and service
  owner reviews before real data reaches a model service), EG-38 (labs need an action boundary), EG-12
  (record consequential decisions)
- Law that shapes the design: BetrVG §87(1) no. 6 (Germany; BAG 1 ABR 7/15), WOR art. 27(1)(l) (the
  Netherlands), GDPR Recital 43. Dojo offers no per-member views and keeps as little as it can. It does
  **not** claim to be unsuitable to monitor: the operator keeps technical access to stored data. That
  residual risk is surfaced (EG-5), not relabelled, and it blocks the pilot until it is accepted.
- Vision: section II.6 "The social and emotional dimension" and "Learning with peers, without a performance
  dashboard": peers learn together, records stay private, and nothing feeds a manager.

## The requirement

The owner asked: "Check whether it is a proper multi-user application (can multiple users use the same app and
preserve state, progress, etc.)".

The honest answer today is no. Dojo admits one person:

- Easy Auth (`infra/main.bicep`, `authsettingsV2`) admits only the owner's object id, through
  `defaultAuthorizationPolicy.allowedPrincipals.identities`.
- `app/core.py` `current_learner()` returns 403 unless the token's tid and oid equal `DOJO_OWNER_TID` and
  `DOJO_OWNER_OID`.

Inside, most of the code already treats the learner as a key. Every route takes `Me = Depends(current_learner)`.
Per-learner data lives under `learners/<learner_key>/...`. The learner key is
`"l" + sha256("dojo-learner:" + tid + ":" + oid)[:31]`, so it follows EG-5: tenant plus object id, never
the email address.

The parts that are tied to one learner are:

- the background services that `create_app()` builds around one `owner` Learner;
- the podcast and calendar token mapping;
- one global `podcast/aired.json`;
- the DP-800 lab room, which has one shared runner user and shared schemas;
- the Studio, cost and Add an exam routes, which are open to whoever passes `current_learner`.

Appendix A lists every such place.

Team Dojo lets the owner admit colleagues. Each colleague keeps a private record, plan, podcast,
calendar, practice pool and labs. The owner keeps the content work, the money and the admin screens. It
ships switched off.

## The options

### Who operates the Dojo that colleagues use

1. **One shared Dojo, run by the owner.** Chosen, with the pilot blocked until the residual risk is accepted.
   One deployment, one bill, one place where content is built. The cost: the operator can technically read
   every member's stored data (see "Operator access and the residual risk").
2. **Each colleague runs their own Dojo in their own subscription** (the sovereignty-preserving option).
   Course packages are shareable files, so built exams can be handed over. Circles (ADR 0010) could later
   federate only opt-in aggregates between Dojos. No one could read another person's record, because no one
   operates another person's Dojo. Rejected **for now**: each colleague would need a subscription, Foundry
   model deployments, the lab database, Easy Auth, the deploy pipeline and someone to keep it running, and
   each Dojo would pay its own content-building costs. It becomes the plan if the privacy owner or a works
   council refuses operator access (see "Review triggers").

### Where admission happens

1. **Easy Auth allow-list only.** Every new member's object id goes into Bicep and needs a redeploy.
   Rejected. Each approval would need a deploy. Easy Auth also cannot show a calm "Ask for access" page.
   And the app has to know the members anyway, because roles and per-member settings live there.
2. **Entra "Assignment required? = Yes" on the enterprise app, with members assigned in Entra.** A strong
   outer gate, but an unassigned colleague is stopped by Entra and never reaches "Ask for access". Global
   Administrators are exempt, so it would not limit an owner who is one. Not the default. It stays optional
   hardening that the owner can turn on in the Entra admin center (see "Decisions for the owner").
3. **Easy Auth group or claim rules** (`allowedPrincipals.groups`, `jwtClaimChecks`). Rejected. How they
   behave at run time for guests and with group overage is not documented, so they cannot be a gate.
4. **Easy Auth admits the tenant's users, and the app admits members.** Chosen. The app registration stays
   single-tenant (AzureADMyOrg). Without `allowedPrincipals`, "all user and guest accounts in your
   directory can use your application" (Microsoft Learn). So only members and B2B guests of the sign-in tenant can
   sign in at all, and the operator controls that population by inviting guests. **The app's own allow-list (tid plus
   oid) is the real gate.** Dojo checks it on every request.

### Keeping learners apart

1. **A check in every route.** Fragile. One forgotten route leaks.
2. **Keep the storage keyed by learner, and enforce the few global objects one by one.** Chosen. The path
   comes from the signed-in learner (`learners/<key>/...`) and never from the request, so another learner's
   data is out of reach. The few global objects (jobs, tokens, `aired`, media for check questions, the lab
   database) each get an explicit check, listed in Appendix B. A route inventory test makes every route
   declare its class (see "Roles and route classes").

### The practice-exam pool

1. **One shared pool, with "seen" kept per learner.** Cheaper: one pool serves everyone. But peers talk
   about the questions they met. A question another learner has already discussed is not fresh for you,
   even when your record says you have not seen it. A practice-exam result would then claim more than its
   evidence (EG-32).
2. **A pool per learner.** Chosen. This is today's design, made to work for many learners. A practice-exam
   question is written for one learner and never shown to another. "Seen" stays per learner, so a seen
   question never counts as unseen, and fresh means fresh. The cost grows with the number of active
   learners. A global daily cap bounds it (see "Money").

### The lab database

1. **One database per learner.** Strongest separation, but many databases cost money, and the free offer
   covers only a few databases per subscription. Rejected because of cost.
2. **Shared schemas, with learners taking turns.** One learner's tables would be visible to the next.
   Rejected.
3. **A schema per learner and lab, with its own owner and its own runner user, in the one free-offer
   database.** Chosen. SQL Server permissions do the separating, not app code. See "Labs".

### Background services

1. **One set of services per learner.** Simple to reason about, but every service would multiply its cost
   and its threads. Rejected.
2. **Content work stays shared; learner work is per learner; "who comes first" looks across learners.**
   Chosen. See the table under "The decision".

## The decision

### The switch

- A new Bicep parameter, `teamMode bool = false`. `deploy.yml` does not pass it, so it stays false until
  the owner changes the default (or adds `teamMode=true`) in a reviewed PR.
  - Amended 5 October 2026: `deploy.yml` passes `teamMode` from the repository variable `DOJO_TEAM_MODE`.
    Only exactly `true` switches it on; unset or any other value deploys `false`. The template's default stays
    `false`, so a fork or a new copy stays owner-only, and switching is not a code change that a public
    repository would carry to every copy. Only the repository's owner or an admin can set the variable.
- `teamMode=false`: `allowedPrincipals.identities = [ownerObjectId]`, exactly as today. The app setting
  `DOJO_TEAM` is `0`.
- `teamMode=true`: `defaultAuthorizationPolicy.allowedPrincipals` is left out, so Easy Auth admits every
  member and guest of the tenant. `DOJO_TEAM` is `1`.
  - The app setting `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` is set to the sign-in tenant id
    (`tenant().tenantId`). Easy Auth then also checks the token's `tid` claim against it. This is an extra
    check; the app's own check stays.
  - Everything else in `authsettingsV2` stays the same: the issuer, the audiences, the excluded paths
    `/healthz`, `/feed/*` and `/cal/*` (since 5 Oct 2026 also `/static/sw.js` and `/static/icon-192.png`,
    ADR 0013), and the token store.
  - No `allowedPrincipals.groups` and no `jwtClaimChecks`.
- `Settings.team` reads `DOJO_TEAM`. With it off, `current_learner` behaves byte for byte as today
  (owner only, the same 403 text). The existing owner tests keep passing unchanged.
- A code constant `TEAM_READY` stays `False` until the last PR of this plan lands. While it is `False`, the
  app ignores `DOJO_TEAM=1`, logs one warning and stays owner-only. A half-built team mode can then never be
  switched on by accident.
- A new optional Bicep parameter `ownerLearnerKey` (default empty) feeds the app setting `DOJO_OWNER_KEY`.
  Empty means the key is derived from `DOJO_OWNER_TID` and `DOJO_OWNER_OID`, as today. It exists only for
  the owner's lost-account procedure (see "Lost accounts").

### Pausing and ending team mode

Turning team mode off must never destroy members' data or lock them out of export and delete. So there are
three different actions.

- **Pause** (Members page). Members keep signing in, but every operational route returns 403 `paused`, and
  background work for members stops. `/api/me`, export, "Delete my record" and Leave keep working. All data
  stays untouched. Resume undoes it. Nothing is deleted while paused. The Pause state is kept in
  `team/state.json` and mirrored in the lab database; Resume needs the database (see "Deletion that stays
  deleted").
- **End team mode** (Members page, with confirmation). A banner is not notice: a member who does not sign
  in never sees it, and Dojo holds no mail permission (EG-5), so it cannot send one. So:
  1. The owner chooses "End team mode". Dojo shows the list of every active and removed-in-grace member's
     email (and display name), with a "Copy addresses" button, and a suggested message: "Our Dojo stops
     serving members on <date>. Download your record before then; your data is deleted on that day."
  2. The owner tells every member **outside Dojo** (email or Teams).
  3. The owner confirms "I have told every member", and picks the end date. Dojo records the
     confirmation, its time and the date in `team/audit.jsonl`. **The 14-day clock starts only at this
     confirmation**: the end date must be at least 14 days after it.
  - From the confirmation, every member also sees a banner: "This Dojo stops serving members on <date>.
    Download your record before then. Your data is deleted on that day."
  - Until the date, members keep full use, including export and delete.
  - On the date, every member becomes `removed` (reason `team-ended`), the app stops admitting them, and
    the sweeper deletes their data that day (with the same steps as Leave, tombstones included). The 14
    days of notice replace the 30-day grace.
  - Only after that does the owner set `teamMode=false` in Bicep and deploy.
- **`teamMode=false` deployed while members still have data** (by mistake, or as an emergency stop). Easy
  Auth then admits only the owner, so members are locked out. The app treats this as a **suspension**: it
  deletes nothing, the sweeper skips members, and the owner's Studio shows a warning: "Team mode is off
  but N members still have data. Turn it back on, or turn it on and End team mode." The ADR states this
  plainly: a suspension locks members out of their own export until it is lifted, so it is for emergencies
  only.

The emergency stop for a suspected leak is **Pause**, not the Bicep switch, because Pause keeps export and
delete working.

### Identity and admission

- The owner is still the principal named by `DOJO_OWNER_TID` and `DOJO_OWNER_OID`. The owner's learner key
  does not change, so the owner's record, plan, podcast, calendar and labs stay where they are.
- In team mode, `current_learner` also requires `tid == DOJO_OWNER_TID`. That is the explicit tenant
  admission policy (EG-5): one tenant, the sign-in tenant.
- A B2B guest's token carries `tid` = the sign-in tenant (the resource tenant) and `oid` = the guest object
  there.
  That oid differs from the person's oid in their home tenant. So the learner key belongs to the guest
  object. If the guest object is deleted and the person is invited again, they get a new oid. Their old
  record is reached only through the owner's re-bind (see "Lost accounts"); otherwise they start fresh.
  The invite steps on the Members page say so.
- Membership is read on every request from `team/members.json`. Each row holds: tid, oid, learner key,
  lab surrogate (see "Labs"), Entra name, email, display name, role, status (pending, active or removed),
  `joined_at`, `removed_at` and the removal reason (left, removed, expired, team-ended), the notice
  acknowledgement, and the renewal month (see "Lost accounts"). It holds no activity date. It is cached in
  memory and reloaded when the file changes. Removing a member takes effect on the next request.
- **The learner key comes from the membership row**, not from the token, once a member is admitted. A new
  member's key is `learner_key(tid:oid)`, as today. If that key is in the tombstone list (the same guest
  object left before), it gets a generation suffix, `learner_key(tid:oid:2)`, so a returning member starts
  fresh and never collides with a deleted folder. Still keyed only to tid and oid (EG-5). The owner's key
  is `DOJO_OWNER_KEY` if set, else derived as today.
- A signed-in non-member gets 403 with the code `not_member`. The SPA shows "Ask for access".
  - `POST /api/access/request` records tid, oid, name, email and the time.
  - The email comes from the `email` claim, which guests have by default. `name` and `preferred_username`
    need the `profile` scope and may be missing. Dojo then shows the email, or "A colleague" if both are
    missing.
  - The email and name are for display only. They are never used to decide access or to key data (EG-5).
  - A person has at most one open request. The owner's page lists open requests.
  - Approve adds a member with the role `learner`. Decline marks the request declined.
  - Declined and open requests are deleted after 30 days.
- The owner cannot be removed or demoted, and cannot leave. There is one owner, and it is fixed by the
  settings, not by the member list.
- Invites stay manual. The operator (as an administrator who may invite guests) invites the colleague as a
  guest in the Entra admin center. The Members page shows the steps, and the Dojo URL to give as the redirect URL.
  - The guest accepts a consent page when they redeem the invitation.
  - Microsoft does not document how long it takes until they can sign in, so the page does not promise a
    time.
  - Dojo holds no Microsoft Graph permission. Sign-in asks for identity scopes only.

### Lost accounts and orphaned memberships

A re-invited guest gets a new oid, so a new learner key, and their old record would be stranded. The owner
may also be a guest. Dojo handles both without rewriting any hash chain, because the record is keyed by the
folder, and the chain does not contain the key (`Store.append_event` chains `prev` hashes only).

- **A member's re-bind (operator-mediated).**
  1. The new identity signs in and is a non-member. "Ask for access" has a box: "I used this Dojo before
     with an account that no longer works." The request records that.
  2. The owner sees the request marked "used Dojo before". The owner confirms **out of band** that it is
     the same person (a Teams call or a message to the old email). Dojo shows the old member rows that can
     still be re-bound: active, or removed or expired less than 30 days ago. It shows only their names and
     emails, never their data.
  3. The owner picks the old row and confirms "I checked with the person directly". Dojo writes the new
     tid and oid into **the old row**. The learner key, the lab surrogate, the tokens and the folder stay
     as they are. Nothing is copied or moved, and the record's chain is untouched. The old oid is kept in
     the row as a hash only, so a stale token for it cannot match.
  4. The re-bind is written to `team/audit.jsonl`, the admin action log (approve, decline, remove,
     re-bind, pause, resume, "I have told every member", end). It holds the action, the time, and the display name concerned. It holds no
     learner activity. When a member's data is deleted, their display name in the log is replaced by
     "a former member". Lines older than 12 months are pruned.
  5. If the old data was already deleted, there is nothing to re-bind, and the person starts fresh.
- **The owner's own lost account.**
  1. Another administrator of the sign-in tenant re-invites the owner. The owner should keep a second
     administrator (or a break-glass account) in the tenant, because a guest cannot re-invite themselves.
  2. The owner reads their new guest oid in the Entra admin center.
  3. In a reviewed PR, the owner sets `ownerObjectId` to the new oid and `ownerLearnerKey` to the old key. The old
     key is `"l" + sha256("dojo-learner:" + tid + ":" + old oid)[:31]`, and the old oid is still in the
     current Bicep parameters.
  4. GitHub Actions deploys it with its federated identity. That does not depend on the owner's guest account.
  5. The owner signs in. Their record, plan, podcast and calendar links and lab schemas all work, because the key
     did not change.

  PR 1 tests this on the local stub: a changed owner oid with `DOJO_OWNER_KEY` set reaches the same
  record, and the chain still verifies.
- **Orphaned memberships, without tracking activity.** A membership is valid for 12 months. In the last 30
  days, the member sees "Keep my membership for another 12 months?" at sign-in. Only that click renews it.
  Dojo stores the renewal **month** (not a day, not a sign-in time), and the owner's page does not show it.
  A membership that is not renewed expires: it becomes `removed` (reason `expired`), and its data is
  deleted 30 days later, as for any removal. The owner's membership does not expire.

### Display name and the seam for Play

- A member chooses their **display name**, the only name peers will ever see. Until they choose, it is
  "Member" plus a short number. It is never the Entra name. The Entra name and email are shown to the
  owner only, on the request and the Members list, for admission.
- **The seam for Play (ADR 0010, a separate builder).** Team provides only:
  - members and their status;
  - roles;
  - the display name;
  - an opt-in storage folder under each learner, `learners/<key>/social/`. It is empty in Team, and it is
    deleted with the learner's folder.

  The opt-in flags, their defaults, and "circles" (small opt-in groups) belong to Play and are designed
  there. Team ships no social feature and no flag.

### Roles and route classes

There are two roles: `admin` (the owner only) and `learner`. Every route belongs to exactly one of five
classes, each with its own dependency. The route inventory test fails if a route has no class.

| Class | Dependency | Who passes | Routes |
|---|---|---|---|
| `public` | none | anyone (Easy Auth excluded paths) | `/healthz`, `/feed/{token}/*`, `/cal/{token}` (token is the credential), static files |
| `signed_in_non_member` | `current_principal` | any signed-in tenant user, member or not | `GET /api/access` (my request status), `POST /api/access/request` |
| `learner_exit` | `current_member_any` | the owner, active members, members waiting for the notice, removed members within 30 days, members during Pause or End team mode | `/api/me`, the notice and its acknowledgement, `/api/record/export`, `POST /api/record/delete`, `POST /api/team/leave`, membership renewal |
| `learner` | `current_active_learner` | the owner, and active members who acknowledged the notice, while team is not paused | every operational route: items, lessons, exams, checks, rehearsals, asks, plan, podcast and calendar settings, labs, settings, quality |
| `admin` | `current_admin` | the owner only | see below |

A removed member therefore gets 403 `removed` on every operational route, and can still reach their own
export, delete and leave. That is the only thing the exit class allows.

- The `admin` class covers:
  - `/api/diag`, `/api/diag/lab`, `/api/diag/run`;
  - `/api/studio/*` (voices, costs, drift, drift refilm, drift lab "looked", deeper);
  - `/api/exam-packages*` and `/api/exam-lookup` (Add an exam, film approvals);
  - `/api/lessons/{id}/video` (filming costs money);
  - `/api/team/*` except leave;
  - `POST /api/lessons/{id}/rewrite`.
- **First lessons and rewrites are separate routes.** `POST /api/lessons` (learner) makes the first lesson
  of a skill that has none, and returns 409 if a lesson exists. `POST /api/lessons/{id}/rewrite` (admin)
  rewrites an existing one. A rewrite becomes the newest lesson for everyone and invalidates its podcast
  and film, so only the admin rewrites. Today both go through `POST /api/lessons`; the SPA's "Fresh lesson" button (`app.js` L821) moves to the
    new route and is shown to the admin only.
- `/api/quality` stays open to learners, scoped to their own lines plus the shared content lines. This is
  a deliberate exception to "quality is admin-only". "Nothing dropped silently" means a learner must be
  able to see what was removed from their own answers. The admin never sees another learner's lines.
- `/api/about` stays open to every signed-in user. Its storage text names the operator (see "Privacy").
- The SPA hides what a learner cannot use: Studio, Add an exam, the film and rewrite buttons, the
  diagnostics on About, and Members. The server enforces the classes anyway. Appendix A lists the SPA
  places.

### Operator access and the residual risk

The law sets the bar, and Dojo cannot fully meet it on its own.

- **Germany:** BetrVG §87(1) no. 6, as the Federal Labour Court reads it, covers any system that is
  *objectively suitable* to monitor behaviour or performance. Intent does not matter. Group data counts
  when the pressure passes to individuals (BAG 1 ABR 7/15).
- **The Netherlands:** WOR art. 27(1)(l) covers systems "gericht op of geschikt voor" ("aimed at or
  suitable for") monitoring.
- **Consent:** consent between colleagues, and to an operator in the same employer, is weak (GDPR
  Recital 43).

**What Dojo cannot claim.** Removing admin screens does not make Dojo unsuitable to monitor. The operator
keeps technical access to every learner folder on /home and every lab table, through their administrator
rights:

- over the sign-in tenant;
- over the Azure subscription Dojo runs in.

With either, the operator can read the /home share (Kudu, SSH, a backup download) and the lab database. That access
is **not visible to members**: Dojo cannot log or show it, and members cannot see the platform's own
activity log. A self-built Dojo also lets the operator change the code. This is organisational access. It
is not learner ownership, and Dojo never calls it that (EG-5). The conflict is surfaced here, not
resolved.

**What Dojo does instead: no per-member views, and as little data as it can.**

- **The owner sees no per-member activity or results in the app.** That means no last-active day, no cost
  per member, no counts per member, no answers, record, readiness, plan, practice-exam results or labs.
  This replaces the first brief's "last active day" and "AI cost per member".
- **No export or API of another person's data, for any role.** The owner's export is the owner's own
  record. No admin route returns another learner's learning data. A test lists every admin route's
  response fields against an allow-list.
- **Costs are shown only as totals**, and the 5-member rule is applied **when the meter is written**, not
  only on screen (see "Money"). With fewer than 5 members, only "Dojo in total" exists.
  - Honest limit: the owner can see the Azure bill and the AI resource's metrics in the portal. With few
    members, they hint at how much the members used together. Dojo does not break it down further.
- **Budgets are enforced automatically per learner.** A learner's own counts live in their own folder.
  Only they see them. The owner sets the allowance values and never needs anyone's numbers. The owner is
  not told when a member reaches an allowance or the cap.
- **Logs carry no activity trace** (see "Logging").
- **Background work may read a learner's own data to serve that learner.** For example, the pool keeper
  checks the date of the learner's latest record entry. It never stores or shows it elsewhere.
- **Written commitments by the operator**, in the notice and in `docs/privacy.md`:
  - The operator does not read members' stored data. If a support case ever needs it, he asks the member
    first.
  - Dojo data is never used for performance reviews, staffing or HR.
  - Nothing is shared with managers.
  - Social features are opt-in and off by default, and leaving them deletes their data.
  - The hash chain is per learner, never across learners.
  - "Leave Dojo and delete my data" deletes the whole learner folder. Backups keep copies for up to 30
    days.

**The pilot stays blocked.** A commitment is not a control. Team mode is not switched on for real
learners until the accountable privacy owner outside the build team and, for colleagues in Germany or the
Netherlands, the works councils have read `docs/privacy.md` and accepted this residual risk in writing.
If they refuse it, the sovereignty-preserving option (each colleague runs their own Dojo) becomes the
plan.

**Residual risk: the constraint, as decided by the lead (2026-10-01).** The fix check found that no
amount of design inside one shared Dojo removes operator access. The lead settled this by decision, under
the owner's standing instruction, not by more design. The constraint first given to the reviewers ("the owner
must not be able to see per-member activity") was stated too strongly. It now reads:

1. **The product** shows nothing per member, to anyone, and keeps as little as possible (the list
   above).
2. **The operator's technical access** (administrator of the tenant and the subscription; unseen by members) is
   **disclosed**: in the first sign-in notice, in `docs/privacy.md`, and to the reviewers.
3. **The pilot starts only if** the outside privacy owner and, for colleagues in Germany or the
   Netherlands, the works councils **accept that access in writing**.
4. **If they refuse,** the self-run alternative (each colleague runs their own Dojo) becomes the path. The
   review trigger for it stays.

This ADR therefore does not claim that operator access is solved. It claims only that it is disclosed,
minimised in the product, and gated by an outside decision.

### The admin's Members page

The Members page shows, for each member:

- the display name;
- the Entra name and email (for admission only);
- the role;
- the status (pending, active or removed);
- the date they joined.

It shows nothing else about a member: no activity, cost, counts or learning data. The page also holds:

- the open requests, with the "used Dojo before" mark and the re-bind action;
- the allowance values and the global cap;
- cost totals that follow the 5-member rule;
- the invite steps;
- Pause and Resume, and End team mode.

Removing a member asks for confirmation. The removed member's data is kept for 30 days and then deleted
automatically.

During those 30 days, a removed member who signs in sees "Your access ended on <date>." Only the
`learner_exit` routes work, so they can still:

- download their record;
- delete everything now.

They can do nothing else. Background work skips removed members.

### Privacy, honestly

- **The first sign-in notice** (for members, not the owner) says in plain words:
  - who runs this Dojo (the owner, by name);
  - that the operator, as Global Administrator of the tenant and Owner of the subscription, can
    technically read everything stored, that members cannot see when he does, and his written commitment
    not to;
  - where the data is stored (Sweden Central, on the web app's /home share; lab tables in one Azure SQL
    database);
  - which AI models read the answers, and that anything typed or said in an answer or a question goes to
    them;
  - how long data is kept, including that deleted data can stay in App Service's automatic backups for up
    to 30 days;
  - how to export;
  - how to leave and delete everything;
  - the written commitments under "Operator access and the residual risk": the operator does not read
    members' data; never for performance reviews, staffing or HR; nothing to managers; social features
    opt-in, off by default, and deleted when left; one hash chain per learner;
  - what the owner sees of them: display name, Entra name, email, role, status and joined date, nothing
    more.

  The member acknowledges it once. The acknowledgement records the notice version and the time. A new
  version asks again. Until the member acknowledges, every `learner` route returns 403 `notice`; the
  `learner_exit` routes still work.
- **"Leave Dojo and delete my data"** (members only) does all of the following, in this order:
  1. writes the learner key's hash and the lab surrogate's hash to the tombstone list (see "Deletion that
     stays deleted");
  2. cancels the learner's queued jobs and marks their running jobs as abandoned;
  3. deletes the whole learner folder `learners/<key>/` (record and hash chain, items, plan, exams, pool,
     rehearsals, asks, checks, profile, quality lines, tokens, `aired` notes, check-question audio, own
     allowance counts, the `social/` folder);
  4. deletes their job documents;
  5. drops their lab schemas and users;
  6. sets the membership to `removed` (reason `left`, `removed_at` now) and deletes the row's name,
     email and display name. The bare row (status, dates, hashes) goes when the tombstone expires.

  Cost totals for all members together and per exam stay, because they were never per member.

  The screen says plainly what remains afterwards:
  - copies in App Service's automatic backups of /home, for up to 30 days;
  - lab tables in the database's backups;
  - log lines, for up to 7 days.

  Dojo cannot delete single files from those backups.

  "Delete my record" (start over) stays as today and keeps the membership. It bumps the learner's
  generation instead of writing a tombstone (see below).
- **`docs/privacy.md`** is the one-page data-flow sheet for the outside reviewer. Dojo never calls the
  operator's access "learner ownership" (EG-5).

### Deletion that stays deleted

Two things could bring deleted data back: a job that finishes after the delete, and a restore of /home
from a backup.

- **Late writes.** `Store` gets a per-learner **generation** and a **tombstone set**.
  - A job, a background batch and a synchronous route capture the learner's generation when they start
    (in the same `contextvars` context as the cost attribution).
  - Every write under `learners/<key>/` (`write`, `append`, `append_event`), and every job-document write,
    checks under the store lock that the key is not tombstoned and that the generation still matches. If
    not, the write is dropped and the job ends as `abandoned`. A late job can never recreate a deleted
    folder.
  - "Delete my record" bumps the generation; Leave and the sweeper tombstone the key. The PoolKeeper's
    existing `stale()` check stays and becomes per learner.
  - Model calls already in flight finish and are paid. Their cost goes to the pooled totals only.
- **Backup restores.** `team/tombstones.json` lists, for every deleted learner, `sha256(learner key)`,
  `sha256(lab surrogate)` and the deletion date. The same rows are mirrored in the lab database, in a
  table `dojo_meta.tombstones` that only the admin connection can read or write. App Service restores do
  not touch the database, and database restores do not touch /home, so one copy survives either restore.
  - **A restore counter.** Both places also hold a **tombstone epoch**: a number that goes up by one with
    every change to the tombstone list, the Pause state or a membership status (below), written to the
    file and the database in the same step. A /home restore brings back an older file, so its epoch is
    **behind** the database's. A database restore makes the database's epoch behind the file's.
  - **Membership status is mirrored too, so a restore cannot bring back a revoked member.** Restoring
    /home from before a removal would bring back an `active` row in `team/members.json`. So every status
    change is written, under the same epoch step, to `dojo_meta.membership` next to the tombstones:
    - each row holds `sha256(learner key)`, the status (`active` or `removed`), the reason (left, removed,
      expired, team-ended, rebound), the date, and the epoch of the change. No tid, oid, name or email;
    - every revocation (left, removed, expired, team-ended) writes a row; so does every change back to
      `active` (approve, a re-bind of a removed row within its 30 days), so the latest row always wins and
      a legitimate reinstatement is not undone;
    - a re-bind also writes `sha256(tid:old oid)` to `dojo_meta.retired_principals`, so a restored row that
      still names the old guest object cannot sign in with it.
  - **Reconciling memberships** happens before any member is admitted, at startup and on Resume, in the
    same step as the tombstones:
    - if the file's epoch is behind, every row in `team/members.json` whose key hash has a newer
      `removed` row in the database becomes `removed`, with that reason and date, so the grace period and
      the sweeper count from the original date (past 30 days means deleted now). A row whose principal
      hash is retired becomes `removed` (reason `rebound`), and Studio asks the owner to repeat the
      re-bind when the new identity asks again;
    - a member approved after the backup has no row in the restored file. The database cannot recreate it
      (it holds only hashes), so that person asks for access again; their folder did not exist in the
      backup either;
    - if the database's epoch is behind, the file's statuses are written back.
  - **The Pause state is mirrored too** (`team/state.json` and `dojo_meta.state`). At startup Dojo is
    paused if either copy says so. Leaving Pause (Resume) **requires a successful reconcile with the
    database**; Resume is refused while the database is unreachable.
  - **At startup, Dojo waits up to about 90 seconds for the lab database.** A serverless database wakes
    from auto-pause in about a minute. During the wait the owner can use Dojo; members get 503 "Dojo is
    starting, try again in a minute" with `Retry-After: 60`. The same gate holds everything a member owns
    that is reached without a sign-in or done for them in the background: the members' podcast feed and
    calendar links answer 404 (the owner's links keep working), and background work and nudges see no
    member (`active_learners`, `active_member`), because a restored `members.json` may still list a member
    the database knows is revoked, or miss a Pause (review fix). The reconcile runs on its own thread
    (`team-mirror`), so the start itself never waits for the database; a timer opens the gate after 90
    seconds whatever happens. While the gate is shut the thread tries again every 10 seconds.
    - **Reachable:** Dojo reconciles. If the file's epoch is behind (a /home restore), it first purges
      every tombstoned key from the database list (folders, job documents, member rows), and only then
      admits any member. If the database's epoch is behind (a database restore), it drops any lab schema
      whose surrogate is tombstoned and writes the file's list back. Then it writes the union and the
      higher epoch to both places.
    - **Unreachable after the wait** (an outage, or the free monthly allowance is used up): Dojo admits
      members using the file list and file statuses, which are correct unless /home was just restored. Studio shows the
      owner "Tombstones not synced". A background task retries every 5 minutes and reconciles as soon as
      the database answers, purging if the epoch shows a restore.
  - **Restoring /home is an owner action with a runbook:** Pause first and wait until the Members page
    shows "Pause recorded in the lab database"; restore; start; wait for "Tombstones synced with the lab
    database"; then Resume (which itself needs the database: it reconciles first and answers 409
    `needs_database` if the database does not answer).
  - **When the database stays away for long** (the free monthly allowance is used up until the next
    month), Resume would be blocked for weeks. So the Members page offers "Resume without the database"
    behind a second confirmation: "No restore of /home has happened since the Pause." The request must
    carry both `without_database` and `no_restore` (400 otherwise). It is logged in `team/audit.jsonl`
    with that sentence, and the background retry still reconciles as soon as the database answers.
  - **Documented residual risk:** a /home restore done without Pause **while** the database is
    unreachable. Then members are admitted from the restored file until the database comes back: data
    deleted after that backup can be visible to its own learner (only to them: folders stay keyed per
    learner), and a member removed after that backup can sign in again, for that time. It takes two faults at once: a restore against the runbook, and a
    database outage. The background retry purges it as soon as the database answers.
  - Tombstones are kept for 40 days: longer than the 30-day backup retention, so no automatic backup can
    bring the data back after the tombstone expires.
  - Locally (no database) only the file exists. That is fine: the local stub has no backups. With team
    mode off the mirror is not attached at all: no member exists to gate, and the start does not touch
    the database.
  - **The `dojo_meta` schema** (made by the admin connection, owned by `dbo`, no grant to any runner) has
    four tables: `state` (epoch, paused), `tombstones` (key hash,
    lab hash, date), `membership` (key hash, status, reason, date, epoch) and `retired_principals`
    (principal hash, date). Only 64-character hex hashes, statuses, reasons, dates and numbers; every
    value is validated before it is written or read. Rows older than 40 days are dropped at the next sync.

### Enrollment

- Each learner keeps `enrolled: [package ids]` in their own profile.
- A member chooses exams from the catalog (built-in plus added) after the notice, and can change the choice
  later.
- The owner's profile has no `enrolled` key today. A missing key means "all packages" for the owner and
  "none yet" for a member. That is the owner's migration: nothing is written.
- Package-scoped learner routes refuse a package the learner is not enrolled in with 404. These include
  items, practice exams, checks, rehearsals, the plan, the podcast feed list and the calendar.
  `PUT /api/settings` refuses an `active_package` that is not enrolled.
- Added exams are shared content. Only the admin adds or removes them.
- **Removing an exam** is refused (409) while any member has an open practice-exam attempt for it, and the
  page says how many attempts are open (a count, no names). Once none is open:
  - the exam is dropped from every enrollment, and every member's pool for it is deleted;
  - a frozen copy of what history needs to render is kept in `packages-retired/<pid>.json`: the title,
    the skills and their names, and the exam's scoring rules. Past attempts, items, readiness snapshots
    and record events then still show "AZ-104 (removed)" with their skill names, instead of breaking;
  - the lessons and media stay as shared content, as today.

### Every per-learner object is checked

Appendix B lists every learner-scoped object, its routes, and how ownership is checked. In short:

- The storage path comes from the signed-in learner. Ids from the request are checked against a pattern
  and looked up only inside that learner's folder. A miss is a 404, never a 403, so ids are not
  confirmed.
- Jobs live in one global folder. `Jobs.get` already compares `job.learner`. Every other reader goes
  through `for_learner`.
- Feed and calendar tokens: `/feed/{token}` and `/cal/{token}` compare the token's hash with the stored
  hash of **the owner, always, plus every active member**, in constant time and all of them every time.
  The owner is read from the settings, not from `members.json`, so the owner's feed keeps working with
  team mode off, while paused, and if `members.json` is unreadable. The match decides the learner. A
  removed member's tokens stop at once. No new index file is needed, so the owner's existing tokens keep
  working with no migration.
- Check-question audio is generated for one learner. In team mode it is stored under
  `learners/<key>/media/`. `/api/media/{id}` serves the learner's own file first, then the shared narration.
  The owner's existing check audio in the shared folder is content only and stays.
- Spoken-answer receipts stay in memory, keyed by learner, with a limit per learner (20) instead of one
  global limit. Then one learner cannot push out another learner's receipts.

### Background services for many learners

| Service | Today | Decision |
|---|---|---|
| `Preparer` (lessons ahead) | owner's active exam first; "busy" = owner has a job | **Shared content work.** Order: the owner's active exam, then the other exams by the number of members enrolled in them. Runs only in a spare slot (see "Spare slots" below). Lessons are made as content (no learner). |
| `DeliveryCheck` | owner's active exam; owner busy | Shared. Same order and the same spare-slot rule. |
| `DriftCheck` | owner's items and pool for Studio counts; owner busy; films filed as owner | Shared. Holding back is already judged per item when it is shown, so it works for every learner. Studio counts stay **the owner's own items**, labelled so. Counts across members would expose a tiny cohort (EG-24). Films are filed as admin work. Spare-slot rule. |
| `DeeperLessons` | owner's active exam; `busy = preparer._learner_busy` | Shared. Same order and the same spare-slot rule. |
| `NewExams` (Add an exam) | owner | Admin only. `remove` follows "Enrollment": refused while an attempt is open; frozen copy kept. |
| `PoolKeeper` | owner's pool, owner's budget, owner's pause; calls `top_up` on its own thread | **Per learner, scheduled across learners.** Round-robin over enrolled exams of members who have a record entry in the last 14 days (read from each learner's own folder, not stored centrally). It skips a learner whose own job is queued or running. Spare-slot rule. **Around every batch** it sets the cost attribution and the generation to the learner whose pool it fills, and it **reserves** the batch's maximum cost against that learner's allowance and the global cap before the batch (see "Money"). A failed reservation skips that learner until the next day. A learner's deletion pauses only that learner. |
| `Podcast` | one owner token map; global `aired.json` | **Feed XML is shared content.** Tokens, the "on air" notes, `forget`, `could_be_on_phone` and the podcast question are **per learner** (`learners/<key>/aired.json`). |
| `Planner` and `/cal/*` | owner token map; `status()` of the owner in every learner's plan | **Per learner.** Each learner has their own calendar token, link status and sessions, over their enrolled exams only. |
| `LabRoom` | one runner, shared schemas | **Per learner and lab, enforced by SQL Server.** See "Labs". |
| `Jobs` | 3 shared slots; 6 jobs per learner; first in, first out | Same 3 slots. A member may have at most **3** jobs queued or running (the owner keeps 6). The queue is **fair**: a free slot goes to the oldest job of the learner who was served least recently, so one learner's three jobs cannot hold the others back. |

**Spare slots, not "nobody is busy".** Today background work waits until the owner has no job at all. With
30 members, "any learner busy" would be true almost all day, and content work would starve. So:

- **One background coordinator.** In team mode the services no longer run their own loops against the
  slots. One `BackgroundScheduler` thread owns all background work and calls each service's `step()`
  (one small batch) in a fixed rotation: Preparer, PoolKeeper, Drift, Deeper, Delivery. The services
  keep their own logic for *what* to do next; the scheduler alone decides *when*.
- **At most one background batch at a time.** The scheduler takes the `Jobs` lock, checks that at least
  **two of the three** slots are free, takes **one** of them, and releases the lock, all in one step, so
  no other check can slip in between. It then runs the service's batch, and gives the slot back when the
  batch ends. Interactive work therefore always has at least two slots, and a background batch never
  makes a learner wait for the first one.
- **The rotation is real.** After a batch (or a service that has nothing to do), the turn passes to the
  next service. A service with nothing to do costs only its check. If fewer than two slots are free, the
  scheduler waits for a slot to be released (a condition variable on `Jobs`), and the same service keeps
  its turn, so no service is skipped because another was unlucky.
- A background batch is kept small (one lesson, one pool batch), so a slot comes back within minutes.
- Interactive jobs never wait for background work beyond the one held slot.
- Two tests in PR 2: with two interactive jobs running, no background batch starts; with five services
  that all have work, ten scheduler turns run each service twice, and at no moment do two background
  batches run at once.

With team mode off there is one learner, and every row behaves as today: background work keeps waiting
until the owner has no job at all. The spare-slot rule applies only in team mode.

### The owner's live data

- The record (the hash-chained events) is never rewritten or moved.
- `podcast/aired.json` is the only global document that is really per learner. The startup migration is
  idempotent:
  1. If `learners/<owner>/aired.json` is missing, copy the old file there, read it back and compare.
  2. If both exist (a crash after the copy), merge them: the union of lessons, keeping the earliest time.
     Merging only adds notes, so it can only add podcast questions, never remove one (never in the
     learner's favour).
  3. Then rename the old file to `podcast/aired.json.migrated`.
  4. If any step fails, Dojo keeps reading the old file for the owner and logs a warning.

  It is tested on the local stub: first run, second run, a crash between the steps, and an unreadable old
  file.
- Feed and calendar tokens stay in the owner's own documents. Lookup by hash finds them without migration.
- Enrollment needs no migration (a missing key means "all" for the owner).
- The owner's legacy lab schemas `lab_vector`, `lab_search` and `lab_hybrid` keep their names and tables.
  Only their runner user changes (see "Labs").
- The migration runs whether team mode is on or off, so the owner moves to the new layout before anyone
  else arrives.

### Labs

DP-800 learners run arbitrary SQL. SQL Server, not app code, keeps them apart. This extends ADR 0003.

- **Per learner and lab:**
  - schema `lab_<lab>_<s12>`, where `s12` is the learner's **lab surrogate**: 12 random hex characters
    made with `secrets.token_hex(6)` at the member's first lab operation (so rows from before PR 4 get
    one too), and stored only in their row in `members.json` (field `lab`). It is not derived from the learner key, the tid or the oid. Schema and user names can
    show up in catalog views (`sys.schemas`, `sys.database_principals`) depending on metadata visibility,
    so a name must never point back to a person. A re-bind keeps the surrogate.
  - schema owner `<schema>_owner`, a user without login, so one learner's objects never ownership-chain
    into another learner's;
  - runner `lr_<s12>_<lab>`, a user without login, with `DEFAULT_SCHEMA` set to that schema.
- The runner gets `ALTER, SELECT, INSERT, UPDATE, DELETE` on its own schema and `CREATE TABLE` in the
  database. It gets nothing on any other schema, `dbo` included: no `CONTROL`, `EXECUTE`, `IMPERSONATE`,
  `VIEW DEFINITION` or external endpoint.
- Learner SQL runs as `EXECUTE AS USER = N'lr_<s12>_<lab>' WITH NO REVERT`, on a new connection without
  pooling, as today.
- **The owner's surrogate and legacy names.** The owner is not a row in `members.json` (he is fixed by the
  settings). His surrogate lives in `team/owner.json` (`{"lab_surrogate": "<s12>"}`), written once, under
  the store lock, at his first lab operation after PR 4 (or the lab probe), in either mode; not at
  startup, so a start never wakes the database. It is keyed to nothing: not the tid, oid or learner key.
  - The owner keeps today's schema names `lab_vector`, `lab_search` and `lab_hybrid` and his tables in
    them. The LABS table maps "owner" to these legacy names; members get `lab_<lab>_<s12>`. Only his
    runners are new: `lr_<s12>_<lab>`, each with `DEFAULT_SCHEMA` set to the legacy schema. The shared
    `lab_runner` loses all grants and is dropped once nothing uses it.
  - **Owner-account recovery** (see "Lost accounts") changes the oid but keeps the learner key through
    `DOJO_OWNER_KEY`. `team/owner.json` does not depend on either, so the owner's runners, schemas and
    tables keep working with no step.
  - **If `team/owner.json` is lost** (an old /home restore), Dojo recovers the surrogate at the next lab
    operation from the database: the runner whose default schema is `lab_vector` names it. Only if no such runner exists does
    it draw a new surrogate and create new runners for the legacy schemas. The tables are never touched.
  - Tests in PR 4: a changed owner oid with `DOJO_OWNER_KEY` set runs, checks and resets the same legacy
    schemas with the same runner; a missing `team/owner.json` is recovered from the database to the same
    surrogate; the reconciler never drops a legacy schema.
- **Lab text** (instructions, starter SQL, hints) uses unqualified table names, so the runner's default
  schema decides where they go. The lab panel shows the learner's schema name for anyone who wants to
  qualify names. The verify, reset and seed SQL become templates with `{schema}`. The schema name comes
  from the member row and the LABS table, never from a request.
- **Reset touches only that learner's schema.** Today `SqlRunner.reset` (`labs.py` L189-235) also turns off
  system versioning for any table whose history sits in `dbo`, and drops **every table in `dbo`**. With one
  learner that cleaned up stray tables; with many it would be one learner's reset reaching the whole
  database. So:
  - a member's reset turns off system versioning only for temporal tables in their schema (and history
    tables in their schema), then drops only objects in their schema. The `hs.name='dbo'` clause and the
    `DROP TABLE [dbo].*` block are gone from member operations;
  - runners cannot create objects in `dbo` (no permission), so there is nothing there to clean;
  - the old `dbo` clean-up survives only as an admin repair, `POST /api/diag/lab/repair` with
    `{"confirm": true}` (400 without it), behind a confirmation on the About page ("Clean dbo and escaped
    history"). It keeps every lab schema, the owner's and the members'.
- **The cross-schema history guard** (the DDL trigger) already works per schema, because it compares the
  table's schema with its history table's schema.
- **One lab operation at a time for all learners.** The existing global lock stays. It also stops one
  learner's `##global` temp table from being seen by another session, because each batch runs on its own
  connection, which closes before the next batch starts.
- **Collisions:** 12 random hex characters give 48 bits, plenty for 30 learners. Admission draws again if
  the surrogate is already in use or tombstoned.
- **When a member's data is deleted** (Leave, removal past the 30 days, the end of team mode), the purge
  tombstones `sha256(surrogate)` and wakes the mirror sync. After each successful sync the **lab
  reconciler** empties and drops every `lab_(vector|search|hybrid)_[0-9a-f]{12}` schema, its owner and its
  runner **only when the surrogate is tombstoned**. During the 30 days after a removal the schemas stay,
  like the folder. A schema or runner it does not recognise at all (no member, the owner or a tombstone
  accounts for it; for example after a /home restore) is reported in Studio, never dropped. It never
  touches the owner's legacy names, which do not match the pattern.
  - The reconciler runs only after a mirror sync (startup, the retries, and each change that kicks the
    sync), never on a timer of its own, so it never wakes the serverless database by itself. If the
    database is away, the schemas are dropped at the next successful sync; the learner's data in them was
    already unreachable, because their runner is only ever used for their own requests.
- **Cost:** still one free-offer serverless database. The free monthly vCore-seconds are shared by all
  learners. When they run out, the database pauses until the next month (`AutoPause`). That is a capacity
  limit, stated on the Labs page in team mode.
- **Per-learner lab runs per day** count toward the budgets (default 150). That stops one learner from
  using up everyone's free vCore-seconds.

### Money

- **Metering.** `ai.Foundry.chat` reads `usage.prompt_tokens` and `usage.completion_tokens` from each
  response. Today they are ignored. Each call is attributed to a learner key, an action and a package.
  - The attribution travels in a `contextvars` variable. It is set in `Jobs._run` from the job's learner
    and kind, and around the synchronous model calls in routes such as transcribe.
  - **The package travels with it** (review fix). Every route that reserves names the exam the action is
    for: the answer, regrade and hint routes read the item first and pass its package; the five-minute
    start picks its exam (`Five.package`) before it reserves and then starts exactly that exam. A job
    carries the package of the hold it adopts (or of the spend it was started under) into its `Spend`,
    so its calls are metered against that exam, not against "members, other exams".
  - Thread pools inside a job submit through `contextvars.copy_context().run` (`core.carried`). Python
    threads (3.12, the version CI and App Service run) do not inherit the context otherwise.
  - A call with no attribution is booked to `content`. In team mode it also logs one warning with the
    call kind (once per kind), and the route test below fails if any learner-triggered path reaches a
    model or speech stub without a reservation.
  - **The PoolKeeper runs on its own thread** and calls `ExamRoom.top_up` directly, outside `Jobs._run`.
    So it sets the attribution (learner, action `pool`, package) and the generation itself, around every
    batch, and clears them after. Its thread pools copy the context like any other.
  - Speech is metered by characters (TTS) and audio seconds (STT), the avatar by video seconds. All
    prices come from `app/costs.py` (`Costs.model_price` and the speech meters), at list price.
  - **When no list price can be read** (the price list was never fetched, or a model has no meter),
    `app/budget.py` uses ceilings set above every list price of the deployed models and meters (USD 10
    per million input tokens, USD 60 per million output tokens, and the speech ceilings), so a
    reservation is never below what a call can cost. Such reservations are large: a first lesson would
    set aside about USD 22, a graded answer about USD 7 and an Ask about USD 5.4, and none fits the default cap, so team mode
    needs a readable price list (Studio already fetches it). With the local stub and no price list, a fixed pretend price (`STUB`: USD 1.25
    and USD 10 per million tokens) applies instead, so budgets behave as with a typical model; no money
    is spent there.
- **Storage, split so that no per-member number exists outside the member's own folder:**
  - `learners/<key>/allowance.json` holds that learner's per-action counts, cost and open reservations for
    today and yesterday only. It enforces the allowances. It is deleted with the folder, and only the
    learner sees it.
  - `team/meter.json` holds pooled sums only, per day and month, and **the 5-member rule is applied when
    it is written**, not only on screen:
    - an exam gets its own bucket only while **5 or more members are enrolled** in it at write time
      (enrollment, not activity, so no activity is needed to decide). Spend on any other exam goes into
      "members, other exams";
    - "members together" and "members, other exams" are written only while there are 5 or more active
      members;
    - with fewer than 5 active members, **everything** (members, owner, content) is written into one
      bucket, "Dojo in total". Separate owner and content buckets would let the members' total be found by
      subtraction;
    - an exam bucket that existed stays as it is when the exam later drops below 5; new spend goes into
      "other exams" from then on.

    No learner key, name or member count is written there (EG-29). The owner's own spend is in the owner's
    own `allowance.json`, which he sees as any learner sees theirs.
  - `team/cap-today.json` enforces the members' daily cap, and **survives Leave**. It holds only the budget
    day, the members' settled spend for that day as one sum, and the open reservations, each under a
    random reservation id with its amount, the time it was reserved and what its calls have drawn so far.
    No learner key, name, action or exam. Leave and removal do
    **not** lower today's sum: a member who spends and then leaves cannot free the cap for a second run.
    When the budget day ends (midnight UTC), the file is replaced by an empty one for the new day. It is
    used only to enforce the cap and is never shown while there are fewer than 5 active members (with 5
    or more, the owner sees the same sum as "members together" in the meter).
  - `team/budget.json` holds the allowance values and the cap the owner set (no learner key), and
    `team/budget-flags.json` the "Reservation bound exceeded" flag by role and call kind only (no ids).
    The per-day counts in `allowance.json` older than yesterday are pruned by the hourly sweep.
- **Per-learner daily allowances** (members only; the owner is metered but not limited). Each is a count
  per UTC day:

  | Action | Default |
  |---|---|
  | Ask the coach | 20 |
  | Graded answers (submit and regrade) | 25 |
  | Hints | 40 |
  | New practice items | 25 |
  | Spoken checks | 4 |
  | Rehearsals | 3 |
  | Practice exams started | 2 |
  | Transcriptions | 60 |
  | First lesson for a skill without one | 3 |
  | Lesson audio | 5 |
  | Lab runs | 150 |
  | Practice-exam pool batches made for them | 6 |

- **Pool batches (PR 2, kept in PR 3).** The pool keeper counts batches: 6 a day per member, and at most
  24 a day for all members together, counted in `team/pool-budget.json` (`{day, batches}` only, no learner
  key; it starts again at zero on the next budget day). The owner keeps his own 12. The shared count is
  written before the learner's, so a crash over-counts, never under-counts. PR 3 keeps these counters as
  the batch allowance and **adds** a dollar reservation per batch: before any model call, the batch
  reserves its bounded maximum (the writer's batch of that question kind, below) against the learner it
  serves and, for a member, against the members' daily cap (the reservation does not count a second
  batch). **Money first, then the counters, in one critical section** under the store lock
  (`PoolKeeper._pay`): the dollars are reserved, and only if they fit are the batch counters written; if
  writing the counters refuses or fails, the hold is released at once. So a refusal for money never uses up
  a batch of the day. A batch that does not fit waits; the pool keeper looks again on its next round.

- **A global daily cap** for members' metered spend: default USD 5 at list price. Content work keeps its
  own existing caps (ADR 0005, 0007, 0008).
- **The cap is preventive: reserve before spending.** A `Budget` object owns one lock. Every paid path
  reserves before it calls the service, and settles after.
  1. **A bounded maximum per call, provably conservative.** The bound must never be below what the call
     can cost, so it uses no estimate that could be low.
     - **Output:** in team mode every model call sets `max_completion_tokens` from a table per role and
       call kind (for example the grader's verdict, the author's item), set generously above today's
       longest observed answers. For the reasoning models it bounds reasoning and visible tokens together.
     - **Input:** the UTF-8 **byte** count of everything sent (system, user and assistant messages, the
       JSON schema of a structured answer), plus a fixed allowance per message and per request for the
       chat template's special tokens (16 per message, 64 per request, set above what the providers
       document). The models' tokenizers are byte-level BPE (or byte-fallback), in which every
       non-special token covers at least one byte, so there can never be more input tokens than bytes
       plus template tokens; the replay test below checks this for each model. Nothing is estimated "per
       character".
     - **Price:** input bound times the input price, plus `max_completion_tokens` times the output price,
       at list price from `app/costs.py`.
     - **Proof in tests and in production.** The tests check the bound's arithmetic against what is
       really sent (every byte of the messages and the response format, plus the template allowances),
       and that every call carries `max_completion_tokens` and settles from the reported usage. No
       recorded live responses exist yet, so the replay of recorded responses planned here is replaced,
       for PR 3, by the production check: settlement compares every call, and if a call ever costs more
       than its reservation, the excess is booked (even past the cap, because it was spent), a warning
       names the role and call kind (no ids), and Studio shows "Reservation bound exceeded"; the table
       entry for that call kind is then raised in a PR. The canary records responses for the replay once
       team mode is switched on. A call with no `usage` in its answer costs its whole bound.
     - **The action's reservation is the workflow's worst case** (revised after the PR 3 review). The
       route reserves, before anything is recorded, **every model call the workflow's first round can
       make**: batches x calls x tries (`ACTION_CALLS` in `app/budget.py`). It cannot know their input yet,
       so each call is reserved at a pre-input bound above the largest prompt that the excerpt sizes allow:
       48,000 tokens, 64,000 for the grader's verdict and the gate's judgement, 80,000 for the lesson gate
       (40 elements of up to 1,400 characters). Each call then draws its exact bound (its real input bound
       plus its output bound) from that hold. The worst cases:
       - a graded answer: the grader twice (the second time with the quotes that did not verify) and the
         gate once, each with one paid retry;
       - a new practice item, and each question of a spoken check: one round of `make_item` (author and
         gate, each with a retry); a check reserves one round per question, plus its speech;
       - a first lesson: one round of `make_lesson` (author, and the gate over at most 171 elements in 5
         chunks of 40, each with a retry);
       - an Ask: the author's answer and the gate over its points (at most 8 points and the "not covered"
         sentence, one chunk), each with a retry. (The first version reserved the answer only; the test that
         compares every action's real calls with its reservation found the gate);
       - a practice exam: **its whole first round** (`ExamRoom.first_round`): the questions the plan needs
         that the pool cannot give, cut into the batches the writers really use (4 single-choice questions
         a batch, one case a batch, and so on), each batch author and gate with a retry. A short
         single-choice exam with an empty pool reserves its four batches, not one;
       - a rehearsal and a pool batch: one writer batch of their kind.
       At the stub's pretend prices this sets aside USD 0.80 for an Ask, 1.04 for a graded answer, 0.80
       for an item, 0.90 for a one-question check (2.50 for three), 0.80 per single-choice batch, 0.96 per
       case batch and 3.36 for a first lesson.
     - **Second tries are paid only from what is left, never on credit.** `make_item` writing again,
       `make_lesson` rewriting what did not hold, a practice exam's second round, and every writer batch
       claim their own worst case from what is left of the action's hold (`Budget.claim`), under the lock.
       A first round of a check or lesson question is kept for it (`owed`), so a second try of one
       question never takes the money of a question still to be written. If a claim does not fit, that
       try or batch is **left out**, as when it fails, and says so ("Not tried a second time: today's
       budget for this action could not cover another try"; a practice exam left shorter says "Left out:
       today's budget for this action could not cover another batch of questions"). The action itself is
       never stopped half-way for lack of money with ordinary inputs.
     - **The one top-up that remains**, and what it does. Only a call past both bounds - a prompt longer
       than the pre-input bound (a page in a script of several bytes per character) or a transport retry
       the service charged - needs more than its share. Its hold is then topped up against the cap under
       the same lock. If that does not fit, the call is refused before it is sent and the action ends the
       way a model failure ends it. For a graded answer that is **resumable**: the answer stays saved and
       the learner reads "Your answer is saved. Today's shared budget ran out while it was being judged, so
       judging waits: press "Judge my answer now" after midnight UTC." Nothing else is kept half-made: an
       item, a check, a lesson or a practice exam that could not be finished is not stored, and the
       learner reads that the shared budget starts again at midnight UTC.
     - **Speech to text: Dojo decodes the audio and sends its own copy.** No duration, rate or size field
       that the client controls is trusted: not a WAV header's byte rate, and not a WebM, Ogg or MP4
       duration field. In team mode, every learner audio upload (spoken answers and spoken checks) goes
       through `speech.normalize()`:
       1. The upload (at most 2 MB in team mode) is opened with **PyAV** (FFmpeg's libraries, bundled in
          the wheel). The container format is forced from the allowed content type (`webm`, `ogg`, `mp4`,
          `mp3`, `wav`), and only the first audio stream is read. A file with no audio stream, a video
          stream, or a codec outside the allow-list (Opus, Vorbis, AAC, MP3, PCM) is refused.
       2. It is decoded and resampled to **16 kHz mono 16-bit PCM**. Dojo counts the samples it actually
          got. Decoding stops as soon as the count passes 200 s, so a crafted file cannot make Dojo hold
          more than 200 s of PCM (6.4 MB).
       3. Anything that cannot be decoded is refused with 415 ("Dojo could not read this recording; try
          again"); more than 200 s is refused with 413. Both happen before any paid call.
       4. Dojo writes a WAV file itself from those samples and sends **that** to Speech, as `audio/wav`.
          Its length is exactly (bytes - 44) / 32,000 seconds, known before the call.
       5. The reservation is for that exact length, rounded up by one second, at list price. Settlement
          still reads the service's `durationMilliseconds`; a cost above the reservation raises the
          Studio warning (no ids).
       The original upload is not stored, as today. The decode runs in the worker's thread pool. With
       team mode off, today's path stays unchanged: the owner's recording goes to Speech as it came, and
       `av` is never imported.
     - **The new dependency (EG-43).** `av` (PyAV), pinned in `requirements.txt` to `av==18.1.0`. The
       package feed has `av-18.1.0-cp311-abi3-manylinux_2_28_x86_64.whl`, a stable-ABI wheel that installs
       on CPython 3.12, the version CI and the App Service build use. It is added because the standard
       library cannot decode Opus, Vorbis, AAC or MP3.
       - Licence: PyAV is BSD-3-Clause; its wheels bundle FFmpeg (8.1.2 in 18.1.0) built under the LGPL,
         used unmodified as shared libraries. That fits Dojo's use.
       - Risk: FFmpeg parses untrusted input. The design limits that surface: forced container format,
         an allow-list of codecs, a 2 MB input, the 200 s stop, one decode at a time (a lock), a 10 s
         wall-clock limit per decode, and the existing per-learner transcription allowance. Dependabot
         alerts cover the pin; a known exploitable FFmpeg issue in the bundled build blocks the release
         until a fixed wheel is pinned.
       - Size: the wheel is about 34 MB.
     - **The cost of decoding on B1** (one vCPU, 1.75 GB). Decoding and resampling 199 s of Opus took
       about 1 s on the development laptop; the test fails above 3 s, a margin for slower CI runners and
       B1's single core (the canary measures it in Azure). Memory peaks at about 10 MB per decode. With
       one decode at a time and at most 60 transcriptions per learner per day, a busy day of 30 members
       adds minutes of CPU, not hours. The decode runs in the worker's thread pool, so it does not block
       other requests.
     - **Text to speech:** by characters, known before the call; the reservation is the bound and the
       settlement. **Avatar:** by video seconds (admin only; the owner is metered, never refused).
  2. **A bounded maximum per action.** Each learner action (a graded answer, an Ask, a check, a rehearsal
     turn, a practice-exam start, a pool batch, a first lesson) has a fixed list of calls for its first
     round, so its maximum is the sum of its calls' maximums, each counted with **one** retry of the
     structured answer (`ask_json` asks twice), as listed above. `Foundry.chat` retries up to four times
     today; a transport retry is only paid when the service answered 200 with an empty answer. A further
     paid retry needs its own reservation, and is refused like any other.
  3. **Reserve, atomically.** Under the `Budget` lock, Dojo checks the learner's action count, the
     learner's open reservations plus spend, and the members' spend plus open reservations for today
     (from `team/cap-today.json`, below), against the allowance and the cap. If all fit, it writes the
     reservation to the learner's `allowance.json` and to `team/cap-today.json`, **before** the call and
     before anything is recorded. The cap file is written first, so a crash between the two over-counts.
  4. **Settle.** After the call, the real usage (`usage.prompt_tokens`, `usage.completion_tokens`, speech
     units) replaces the reservation. Unused reservation is released when the action's last call or job
     ends (before the job's final state is written). An action that failed before any call ran gives its
     count back, whether the route ran it or a job adopted its hold (review fix): `Jobs._work` settles the
     hold as failed on every failure path (an error, a refusal, a job cancelled while it waited for a
     slot), and a failed or voided hold with no call gives the count back.
  5. **Crash safety** (review fix). Each open entry in `team/cap-today.json` records when it was reserved
     and what its calls have drawn so far (written with each call, before it is sent). After a crash or a
     restart no hold of the old process can still run (one worker process), so at startup `Budget.recover`
     settles every open entry of today by what it had drawn (the money that may really have been spent
     stays spent; the unused rest of the reservation is freed) and clears the members' open reservations
     in their `allowance.json` the same way. While running, an open entry that no hold of this process
     owns and that is older than the longest call (480 s) plus 15 minutes is settled the same way the
     next time the cap is read, so a leftover never ties up the cap for the rest of the day. Entries
     written before this change (an amount only) count as fully drawn: that errs on the side of the cap.
  6. **Midnight.** A hold belongs to the budget day it was reserved in, and is settled against that day
     only: when a hold reserved before midnight settles after it, its spend is spread over the parts it
     reserved (earliest first), and only the parts of the current day are written to the new day's
     `team/cap-today.json`. The new day starts clean: an old day's hold is never added to it. Money spent
     after midnight beyond what the old day reserved (a top-up) becomes a new part of the new day, reserved
     against the new day's cap like any other.
  7. **Every paid path goes through it:** chat, transcription, speech, avatar, retries, background work
     (pool batches reserve against the learner they serve; content work reserves against its own existing
     caps), and the owner (reserved and metered, but not refused).
- **A failed reservation is a calm refusal.** For a learner action, it is a 429 before anything is
  recorded, in one of four words:
  - the allowance: "You have used today's allowance for graded answers. It starts again at midnight UTC.";
  - the cap, spent: "Dojo has used today's shared budget. It starts again at midnight UTC.";
  - the cap, set aside: when the money already spent leaves room but the open reservations of work
    running now fill the cap, "Today's shared budget is set aside for work that is running now. Try again
    in a few minutes; what that work does not use comes back when it ends." Worst-case reservations are
    large against the default USD 5 cap, so only a few member actions run at the same time; most of each
    reservation comes back when the action ends;
  - too large: an action whose worst case alone is above the cap (a long practice exam with an empty pool,
    or a first lesson at a low cap) is refused with a sentence that says so and that the owner can raise
    the cap, instead of a "try later" that would never succeed.

  Because the action's worst case is reserved before the answer is stored, an answer is not left unjudged
  for lack of money with ordinary inputs; the one exception (the top-up above) is resumable and says that
  it waits until after midnight UTC. **The saved answer says why, plainly** (review fix): the item view
  carries `grade_waits` while the learner's allowance or the shared budget cannot pay for judging ("Your
  answer is saved. Today's shared budget is used, so judging waits until after midnight UTC, when "Judge my
  answer now" opens again.", or the allowance or "too large" wording), and the "Judge my answer now" button
  stays disabled until the next budget day or until the owner raises the cap. A judging job that failed
  shows its own reason, not "the studio log says why"; when only reservations of running work fill the
  cap it says to try again in a few minutes. A pool batch that cannot reserve waits and is tried again on the pool
  keeper's next round. The cap cannot overshoot: every call is sent only on money already reserved; only a
  call that costs more than its own bound (the flagged case above) can push the settled sum past it.
- **Sizing the cap.** Honest worst cases make the default USD 5 cap tight: at typical list prices
  (USD 1.25 and USD 10 per million tokens) it holds about four graded answers or one first lesson at the
  same time. That is the price of a preventive cap. The owner can raise it on the Members page; the
  Members page also shows what each action sets aside (`set_aside`), so the cap can be sized from it.
- Learners see their own remaining allowances. The admin sets the allowance values and the cap on the
  Members page. The admin sees cost totals only under the 5-member rule, and is not told who reached an
  allowance or when the cap was reached.

### Logging

Today `app/main.py` sets the root logger to INFO (L43) and the `guard` middleware logs **every** `/api`
request: method, path, status and milliseconds (L334). On App Service that goes to the container log on
/home/LogFiles (7 days or 35 MB). With a few members, that is a timestamped activity trace per person,
because the paths carry item, attempt and job ids. The refusal warnings (L326, L329) also log the path.

The decision, in Azure (when `WEBSITE_SITE_NAME` is set), in both modes:

- The `dojo` logger and the root logger run at **WARNING**. The per-request INFO line is not written at
  all in Azure. Locally it stays, for development.
- A warning or error names the **route template** (for example `POST /api/items/{item_id}/grade`), the
  status and the exception type. It never contains the concrete path, a learner key, an item, attempt,
  job or session id, a token, a name or an email.
- A logging filter on the root handler replaces anything that still matches a learner key
  (`l[0-9a-f]{31}`), a Dojo id pattern or a long token with a placeholder, as a second line of defence.
- gunicorn keeps no access log (ADR 0002), unchanged.
- PR 1 tests it: with `WEBSITE_SITE_NAME` set, a run of learner requests writes no log line; a forced 500
  writes one line with the route template and none of the ids.

`docs/privacy.md` describes exactly this.

### Capacity

- Supported: **up to 30 members** on one App Service B1 instance, in one process, with the one `RLock`
  over the /home JSON files.
- **One instance and one worker only. Scale-out is not supported.** /home is a network share, and file
  locks on it are not reliable across instances. Two instances, or two worker processes, would also break
  the in-process lock, the in-memory receipts and caches, and the job slots.
- **/home size.** App Service automatic backups cannot be restored when the backup is larger than 30 GB.
  Each member adds records, check audio and job results, and shared media grows with the content. The
  owner's Studio should show the /home size and warn at 20 GB. This is follow-up work, outside the four
  PRs. It is listed in the review triggers.
- Known slow spots: `Jobs._free` and `Jobs.for_learner` read every job document, and jobs are deleted after
  14 days. The token lookup reads one small document per member. All of these are linear and fine at 30.
- Above 30 members, or with a second instance, this ADR must be reviewed (see "Review triggers").

### Facts verified by the lead's research (Microsoft Learn)

1. **Easy Auth.** With `allowedPrincipals` removed from a single-tenant registration, every member and
   guest of the tenant passes Easy Auth. So in team mode, the app's allow-list (tid plus oid) is the real
   gate. `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` adds a check of the `tid` claim. The runtime behaviour of
   `allowedPrincipals.groups` and `jwtClaimChecks` for guests and with group overage is not documented, so
   Dojo does not use them.
   - "Assignment required? = Yes" exists, and Global Administrators are exempt from it. It is optional
     hardening only.
   - PR 1's Bicep test checks that `teamMode=true` produces no `allowedPrincipals`. After the first deploy
     with team mode on, the owner checks in the portal that the identity list is gone.
2. **Guest claims.**
   - `tid` is the sign-in tenant.
   - `oid` is the guest object in the sign-in tenant, not the home tenant's oid.
   - `email` is present by default; it is for display only.
   - `name` and `preferred_username` need the `profile` scope and may be absent.
3. **Invitations.** These stay manual, in the Entra admin center. The guest accepts a consent page on
   redemption. The time until they can sign in is not documented.
4. **Backups.** App Service automatic backups:
   - run hourly on Basic and higher;
   - include all of /home on Linux;
   - keep each backup for 30 days;
   - cannot be restored above 30 GB. Custom backups have a 10 GB limit and need a storage account with a
     SAS.

   So deleted data can stay in the automatic backups for up to 30 days. `docs/privacy.md` says so, and the
   notice says so.
5. **Concurrency.** /home is a network share. File locks on it are unreliable across instances. Dojo stays
   on one instance and one worker; scale-out is unsupported.

Still to keep in a test: Easy Auth replaces a client-supplied `X-MS-CLIENT-PRINCIPAL` on signed-in
paths. `current_learner` is never used on the excluded paths (`/feed`, `/cal`, `/healthz`). The route
inventory test keeps it that way.

## The plan: four reviewable PRs

Each PR keeps team mode off (`TEAM_READY = False`) until the last one. The full test suite passes after
each PR. The new tests use the existing `principal(tid, oid)` helper, so they act as signed-in learners.

### PR 1 - The switch, admission, roles, logging and leaving

The changes:

- `Settings.team`, `TEAM_READY`, the Bicep parameters `teamMode` and `ownerLearnerKey`, and the app
  settings `DOJO_TEAM`, `DOJO_OWNER_KEY` and (team mode only) `WEBSITE_AUTH_AAD_ALLOWED_TENANTS`.
- `team/members.json` (with `removed_at`, reason, lab surrogate, renewal month), `team/requests.json`,
  `team/audit.jsonl`, `team/tombstones.json`.
- The five route classes and their dependencies: `current_principal`, `current_member_any`,
  `current_active_learner`, `current_admin`; `public` needs none.
- `/api/access*` (with "used Dojo before"), `/api/team/*` (members, requests, approve, decline, remove,
  re-bind, pause, resume, end team mode), the notice and its acknowledgement, the display name, membership
  renewal, the empty `social/` folder for Play, Leave, the exit page for removed members, and the sweeper.
- The store's generation and tombstone checks; job cancellation on leave; the startup re-purge from
  `team/tombstones.json`.
- First lessons and rewrites on separate routes.
- Logging at WARNING in Azure, no per-request lines, the scrubbing filter.
- `/api/me` returns `role`, `team`, `enrolled`, `notice`, `display_name`, and the banner for Pause or End
  team mode.
- The SPA: role-aware sidebar, Ask for access, Notice, Members, Leave, the banners, renewal.

The tests:

- **Admission:** a non-member gets `not_member` and can make one request; approve, then access; decline;
  remove, then refused on the next request; the owner cannot be removed or leave; another tenant is refused
  even with team mode on; with team mode off everything behaves exactly as today (the existing owner tests
  are unchanged).
- **Route classes:** a route inventory test lists every route in the app and fails if one has no class.
  For every admin route, a learner gets 403 and the admin gets through. For every `learner` route, a
  removed member gets 403 `removed`, a paused member 403 `paused`, a member before the notice 403
  `notice`; the `learner_exit` routes work for all three. A signed-in non-member reaches only the
  `signed_in_non_member` routes. A new notice version asks again.
- **Lessons:** a learner's `POST /api/lessons` on a skill that has a lesson gets 409; the rewrite route is
  admin-only.
- **Leave:** after leaving, the learner's folder `learners/<key>` is gone, with their jobs and membership
  details; the other learner's data is untouched; the removed-member exit page allows export and delete
  only.
- **Deletion during a running job:** a job blocks on a stub model; the learner leaves; the stub is
  released; the job ends `abandoned`, no folder `learners/<key>` is recreated and no job document remains.
  The same for "Delete my record" (generation) and for a PoolKeeper batch.
- **Restore re-purge:** on the local stub, a copied-back `learners/<key>` folder and member row whose key
  is tombstoned are deleted at the next start; a returning guest object gets a new key with a generation
  suffix.
- **Re-bind:** a new identity's request marked "used Dojo before", re-bound by the admin to an old row,
  reaches the old record, and the chain still verifies; the old oid no longer signs in; the audit log has
  one line with no learner activity. Re-binding to a deleted member is refused.
- **Owner's lost account:** with a changed owner oid and `DOJO_OWNER_KEY` set to the old key, the owner
  reaches the same record, tokens and plan; the chain verifies.
- **Expiry:** a membership not renewed after 12 months becomes `removed` (`expired`); renewal stores a
  month only; the owner's page never shows it.
- **Pause and End team mode:** paused members get 403 `paused` on learner routes and can still export and
  delete; End team mode shows the members' emails and does nothing until the owner confirms "I have told
  every member"; that confirmation is one line in `team/audit.jsonl`; the end date must be at least 14
  days after the confirmation, not after the first click; before the date members keep full use; on the
  date they are removed and their data deleted; with `DOJO_TEAM=0` and members present, nothing is deleted
  and the owner sees the warning.
- **No per-member views:**
  - Every admin route's response is checked against a field allow-list. Members return only display name,
    Entra name, email, role, status and joined date.
  - No admin route takes a learner key or id that returns that learner's data.
  - The owner's export holds only the owner's record.
  - `team/members.json` holds no activity date.
- **Logging:** with `WEBSITE_SITE_NAME` set, a run of learner requests writes no log line; a forced 500
  writes one line with the route template and no id, key, path or email.
- **Display name:** peers' views (none yet) and `/api/me` use the chosen display name, never the Entra
  name.
- **Bicep:** `az bicep build` with `teamMode` false gives `allowedPrincipals` with the owner, and no
  `WEBSITE_AUTH_AAD_ALLOWED_TENANTS`, exactly as today. With true, it gives no `allowedPrincipals`, no
  `groups` and no `jwtClaimChecks`, plus `DOJO_TEAM=1` and `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` = the
  tenant id.
- **Guest claims:** a principal with `email` but without `name` and `preferred_username` can ask for
  access and is shown by email. Changing the email claim never changes the learner key or the membership.

### PR 2 - Per-learner tokens, enrollment and the background services

The changes:

- Feed and calendar tokens looked up by hash over the owner (always) plus active members; the `aired`
  notes per learner, with the migration; `podcast.status`, `question`, `forget` and `could_be_on_phone`
  per learner; calendar `links.status()` per learner.
- Enrollment and the 404 for packages a learner is not enrolled in; the plan over enrolled exams only.
- Check-question audio per learner; receipts limited per learner.
- The per-member job cap and the fair queue; the single `BackgroundScheduler` with the spare-slot rule (team mode only).
- The Preparer, delivery, deeper and drift order across learners, in rotation.
- The PoolKeeper round-robin, with attribution and generation set around every batch (the reservation
  itself comes in PR 3).
- Removing an exam: refused while an attempt is open; `packages-retired/<pid>.json`.

The tests:

- **Two-learner isolation, table-driven over the route inventory.** Learner A creates every kind of object
  (item, answer, hint, rehearsal, ask, check, practice exam, plan session, calendar token, feed token, lab
  run, export). Learner B then calls every learner route with A's ids and gets 404, never A's data.
  B's lists, exports, readiness, plan, podcast status and calendar status never contain A's objects.
- **Tokens:** A's feed token serves A's feed, and its fetch notes `aired` only for A; B's podcast question
  is not affected. Same for the calendar. A removed member's token returns 404. The owner's token works
  with team mode off, while paused, and with an unreadable `members.json`.
- **Migration:** on the local stub, first run, second run (no change), a crash between copy and rename
  (merge), and an unreadable old file. The owner's existing tokens keep working.
- **Background services with two learners:** with B holding two slots, background work does not start;
  with one slot busy, it starts and takes one; three learners' queued jobs are served in turn (fairness);
  the scheduler never runs two background batches at once, and with five services that all have work, ten
  turns run each twice (the rotation is real); a service waiting for a slot keeps its turn;
  the pool keeper serves both enrolled learners in turn, skips a learner with their own job running,
  pauses only the learner who deleted, and its model calls are attributed to the learner whose pool it
  fills. With team mode off, today's "owner busy" tests pass unchanged.
- **Removing an exam:** refused with 409 while A has an open attempt; after A submits, both pools are
  deleted and both enrollments dropped; A's past attempt still renders with the frozen title and skills.
- **Enrollment:** routes for a package B is not enrolled in give 404.

### PR 3 - Budgets and metering

The changes:

- `app/budget.py` (new): the `Budget` object with per-call and per-action maximums, atomic reservations
  under the store lock against the learner's allowance and the members' cap, top-ups, settlement, crash
  safety, and the calm 429 refusals before anything is recorded. The owner is metered, never refused.
- Token usage from `Foundry.chat`; in team mode `max_completion_tokens` per role and call kind (with the
  fallback to `max_tokens` for a model that asks for it) and the byte-based input bound; a call that does
  not fit is never sent; a failed connection costs nothing; no usage costs the whole bound.
- Attribution through `contextvars` in jobs (a job adopts its route's hold and releases it before writing
  its final state), thread pools (`core.carried`), synchronous routes and the PoolKeeper; text to speech,
  transcription and avatar metering.
- Route holds on every paid learner route: lessons, lesson audio, items, hints, answers and regrades,
  rehearsals, Ask, checks, the five-minute start, practice exams, transcriptions and lab runs (a count
  only).
- The PyAV decode-and-rewrite step (`speech.normalize`), the `av==18.1.0` pin, and the 2 MB answer upload
  limit (team mode).
- `learners/<key>/allowance.json`, `team/cap-today.json` (today's members' sum and open reservations
  only), the pooled `team/meter.json` with the 5-member rule applied at write time, `team/budget.json` and
  `team/budget-flags.json`; the hourly sweep prunes old days.
- `GET`/`PUT /api/team/budget` (admin) and `GET /api/me/allowance`. The Members page shows the allowance
  values, the cap and the cost totals; a learner sees their own "Today's allowance" on the record page.
  Studio's cost page shows "Reservation bound exceeded" in team mode only.
- With team mode off, nothing of this runs: the clients are not metered, no file is written, the owner's
  recording goes to Speech as it came, and `/api/studio/costs` answers exactly as before.

The tests (`tests/test_team3.py`):

- **Bounds:** the input bound is the UTF-8 bytes of everything sent plus 16 per message and 64 per
  request, and above the text's own byte count; the output table; stub prices only without real models,
  ceilings when the list price cannot be read; list prices from `app/costs.py` when they can.
- **The metered client** (a mock transport): every call carries its output bound and settles from
  `usage`; team off sends exactly today's request; an empty 200 twice reserves again for each paid call;
  `max_tokens` fallback; no usage costs the bound; usage above the bound is booked and flagged; a call
  that does not fit the cap is never sent; a failed connection costs nothing.
- **The allowance and the cap:** a member is refused at the allowance, the owner never (past the
  allowance and past the cap); **preventive under concurrency**: 30 simultaneous reservations from three
  members against the USD 5 cap, of USD 0.60 each, give exactly 8 holds and 22 shared-budget refusals,
  and the sum never passes the cap; a reservation left open by a crash still counts in a new process;
  Leave does not lower today's sum, and after A spends USD 0.80 of a USD 1 cap and leaves, B's USD 0.30
  is refused and USD 0.15 fits; `team/cap-today.json` holds only `day`, `spent` and `open` (no learner
  key; each open entry only its amount, time and what it drew); a failure before any call gives the count
  back; old days are pruned and the cap starts each day
  empty; the meter keeps pooled sums under the 5-member rule at write time (with 4 members only "Dojo in
  total"; with 5 enrolled in one exam that exam has its own bucket; another exam lands in "members,
  other exams").
- **Every paid path reserves:** with `Budget.reserve` patched to refuse, every learner route that reaches
  a model or speech stub answers 429, the stubs are never called, and the learner's folder is unchanged.
  At the allowance a second answer is refused and nothing is recorded.
- **Attribution:** A's calls (including the item writer's thread pool) land on A's allowance only; a pool
  batch is reserved for the learner whose pool it fills, and at a zero cap a member's batch waits while
  the owner's runs; an open check asked for again is not counted twice.
- **Totals only:** the budget routes are admin-only; with fewer than 5 members only totals are shown.
- **Audio is decoded, never trusted:** WebM/Opus, Ogg/Opus, MP4/AAC, MP3 and WAV fixtures are written
  again as 16 kHz mono PCM whose length is exactly (bytes - 44) / 32,000 s. A WAV whose byte rate or
  sample rate lies, a WebM whose duration says 1 ms, and an MP4 whose durations say less (FFmpeg then
  decodes less, and Dojo sends exactly what it counted) are measured by their samples. A 230 s WAV whose
  byte rate claims a tenth of that, a 300 s WebM whose duration says 5 s, and a 300 s Ogg get 413. A
  truncated file, random bytes, an empty upload, a file with a video stream, FLAC and ALAC, and a
  disallowed content type get 415. None of them reaches the Speech stub or reserves anything; Speech
  gets Dojo's WAV and the reservation is (seconds + 1) at the transcription price. 199 s decode in under
  3 s.
- **Team mode off:** nothing is reserved, metered or written; `/api/studio/costs` has no new field; the
  owner's recording reaches Speech as it came, without decoding; the allowance view says team mode is
  off.
- **Midnight (review fix):** a hold reserved before midnight and settled after it adds nothing to the new
  day: with a USD 0.15 cap, USD 0.14 reserved at 23:59 and settled at 00:01 leaves the new day's sum at 0,
  so a new USD 0.14 fits and the cap file never passes 0.15; a top-up after midnight is a new part of the
  new day and is checked against the new day's cap.
- **Worst case (review fix):** the graded answer reserves the grader twice and the gate, each with a
  retry; a short single-choice practice exam with an empty pool reserves at least four writer batches
  (at least 15 questions) before the attempt exists; a lesson's and an item's whole first round runs from
  the route's reservation without a top-up; a second try that would take the money kept for a question
  still to be written is not run, and that question's first round still runs; a writer batch that does not
  fit is left out and never tops up; the owner is never left out; a worst case above the cap gets the "too
  large" refusal with its amount; grading cut short raises the "saved, judging waits" sentence; open
  reservations that fill the cap give the "set aside" refusal and spent money the "used" refusal.
- **Pool money before counters (review fix):** a pool batch refused for money leaves the member's and the
  shared batch counters unchanged; a counter refusal after the dollars fit gives the reservation back.
- **Crash and restart (review fix):** at startup, what a crash left open is settled by what it drew (a
  hold that drew USD 0.30 of 1.20 leaves 0.30 spent and frees the rest, and the learner's open entries are
  cleared); an open entry that no live hold owns expires after 480 s + 15 minutes and keeps what it drew,
  while a live hold's entry never expires.
- **Package attribution (review fix):** answer, regrade and hint spend lands on the item's exam; the
  five-minute start reserves and starts the same exam; a job carries its action's exam.
- **Counts back for jobs (review fix):** a job-backed action (Ask) whose job fails before any call gives
  its count back.
- **Plain waiting (review fix):** a saved answer whose judging was refused shows the reason and the
  disabled button; at the allowance it shows the allowance wording; the owner never sees it.
- **Every action reserves the calls it makes (review fix):** every `ACTION_CALLS` task name is a real
  system prompt's `TASK:`; a run of a lesson, item, answer, Ask, rehearsal, check and practice exam
  records each call's role and task under its hold, and each is in that action's reservation (speech for
  a check is reserved by characters).
- `tests/test_team2.py` waits for the check fill thread too, because it holds the check's reservation, and
  its isolation tests raise the cap, because two learners' worst cases do not fit USD 5 together.

### PR 4 - Lab isolation in the database

The changes:

- Schemas, owners and runners per learner and lab, named by the random lab surrogate; lab text with
  unqualified names; the verify, reset and seed templates.
- Member resets that touch only their own schema; the `dbo` clean-up moved to an admin repair.
- The owner's legacy schemas with his new runners; the shared `lab_runner` retired.
- Cleanup when a member leaves; the reconciler (tombstoned surrogates only; unknown schemas reported).
- The tombstone mirror in `dojo_meta.tombstones`, readable and writable only by the admin connection.
- The owner's surrogate in `team/owner.json`, recovered from the database if lost.
- The tombstone epoch and the mirrored Pause state; the 90-second startup wait; the background retry;
  Resume only after a reconcile.
- The membership mirror `dojo_meta.membership` (every status change, revocations included) and
  `dojo_meta.retired_principals`, reconciled before admitting anyone at startup and on Resume.
- `TEAM_READY = True`.

The tests (AGENTS.md rule 11):

- On a dedicated LocalDB instance (`sqllocaldb create dojo-team4`, then stopped and deleted with its
  `.mdf` and `_log.ldf` files in the user profile folder), with plain tables because LocalDB has no vector
  type: A's runner cannot SELECT, INSERT, ALTER, CREATE TABLE in, or see the metadata of B's schema; a
  trigger or view in A's schema cannot reach B's table (no ownership chain); a history table in another
  schema is rolled back; after A leaves, A's schemas and users are gone and B's are untouched; the
  reconciler never drops the owner's legacy schemas or an unknown schema.
- **Reset scope:** with a table in `dbo` (made by the admin connection) and B's temporal table, A's reset
  leaves both untouched and empties only A's schema. A's runner cannot create a table in `dbo`.
- **Names:** no schema or user name contains any part of a learner key, tid or oid; a re-bind keeps the
  schema.
- **Tombstone mirror:** no per-learner runner can read `dojo_meta.tombstones`; after deleting
  `team/tombstones.json` on the stub, the next start restores it from the database and re-purges.
- **Restore detection:** with the file's epoch set behind the database's (a simulated /home restore that
  brings back a deleted folder and member row), the folder is purged before the first member request is
  admitted. With the database unreachable (a short test timeout in place of 90 s), members are admitted
  from the file, Studio shows "Tombstones not synced", and when the database answers the background retry
  purges. Resume is refused while the database is unreachable; a Pause written to the database survives a
  restored `team/state.json` that says "not paused".
- **Restored memberships** (PR 4, because the mirror lives in the database): for each reason (left,
  removed, expired, team-ended), a member is revoked, then `team/members.json` is put back to a copy from
  before the revocation (epoch behind); at the next start that member gets 403 `removed`, never a learner
  route, and the grace period counts from the original date (a copy older than 30 days after removal
  means the data is deleted at that start). The same on Resume after a restore made while paused. A
  member removed and then re-bound within the grace stays active after a restore. A restored row naming a
  re-bound member's old oid cannot sign in. A member approved after the backup must ask again. With the
  database unreachable, the file statuses apply and Studio shows "Tombstones not synced"; when it
  answers, the revocation is applied before the next member request.
- **The owner's labs:** with a changed owner oid and `DOJO_OWNER_KEY` set, the owner runs, checks and
  resets the same legacy schemas through the same runner; a deleted `team/owner.json` is recovered from the
  database to the same surrogate; the reconciler never drops a legacy schema.
- The FakeRunner tests are updated for unqualified names.

**As built (PR 4).** What runs on the first start in production, with no manual step:

- **Team mode off (today).** The start does not contact the database. The first lab operation of the
  owner (or "Check lab connection now") runs the migration once per process, in this order:
  1. the owner's surrogate: `team/owner.json`; else the database (the runner `lr_<s12>_vector` whose
     default schema is `lab_vector`); else a new one. It is written to `team/owner.json`;
  2. the legacy schemas `lab_vector`, `lab_search`, `lab_hybrid` and their owners, the `lab_guard` user and
     the history guard trigger, each only if missing (as before);
  3. `lr_<s12>_vector`, `lr_<s12>_search`, `lr_<s12>_hybrid` WITHOUT LOGIN, each with `DEFAULT_SCHEMA` set
     to its legacy schema, `CREATE TABLE` in the database and `ALTER, SELECT, INSERT, UPDATE, DELETE` on
     that schema only;
  4. `lab_runner`'s grants on the legacy schemas are revoked, and `lab_runner` is dropped if it owns no
     schema or object.

  No table is created, altered or dropped: the owner's lab tables and progress stay as they are. Every
  step is guarded with `IF ... IS NULL` or a catalog check, so a second run, a second instance or a crash
  half-way is harmless. Rolling the app back is safe: the old code recreates `lab_runner` and its grants
  at its own first lab operation, and the new runners it does not know are inert.
- **Team mode on with a lab database.** Before the first member is admitted, the mirror makes
  `dojo_meta` if missing, reconciles (see "Deletion that stays deleted"), and the lab reconciler drops
  tombstoned member schemas. Members get 503 until then, and their feed and calendar links 404, or until
  the 90-second timer opens the gate on the file ("Tombstones not synced"; retried every 5 minutes). The
  owner, and the owner's links, are never gated.
- **Results.** On LocalDB 17 (`dojo-team4`): a member's runner cannot SELECT, INSERT, CREATE TABLE or
  ALTER in another member's schema, the owner's legacy schema, `dbo` or `dojo_meta`, cannot create a view,
  procedure or schema and cannot `EXECUTE AS`; another member's tables do not appear in its `sys.tables`.
  **Schema names do appear in `sys.schemas` to every user**, which is why surrogates are random. A member
  reset leaves `dbo` and the other member's temporal table untouched; Leave drops only that member's
  schemas and users; the migration keeps the owner's tables and is idempotent; a lost `team/owner.json`
  is recovered to the same surrogate.

## Consequences

- With the switch off, nothing changes for the owner's learning. Two things change on purpose: `aired.json`
  moves inside the owner's own folder, and in Azure the log runs at WARNING with no per-request lines.
- With the switch on (after all four PRs, the written acceptance of the residual risk by the privacy owner
  and, where needed, the works councils, and the owner's decision), colleagues invited as B2B guests can ask
  for access and learn privately from each other. The owner pays and sets the limits.
- **The operator can technically read every member's data**, as Global Administrator and as subscription
  Owner, and members cannot see when he does. Dojo says so plainly, shows no member's data in the app,
  and records his written commitment. This is operator access, not learner ownership. It is a residual
  risk that only the reviewers can accept; Dojo does not claim to remove it.
- The owner cannot see who is active or what anyone spends. Budgets work without it. A question like "who
  uses Dojo?" is answered only by the member list. This gives up cost transparency per person, on
  purpose.
- Spend is bounded before it happens: no learner and no background batch can push the day past the cap
  (only a call that costs more than its own bound could, and that is flagged in Studio). The price is
  that a few calls are refused that would have fitted, because reservations use the maximum, and that
  team mode needs a readable price list, because the ceilings used without one are high.
- Each active member adds practice-exam pool cost. The cap bounds it.
- Labs share one free database. A busy month can pause it for everyone.
- Shared content (lessons, deep lessons, podcast episodes, items' sources, media, packages) is improved
  for all members by the owner's background work, in spare slots only.
- A lost guest account no longer strands a record, but a re-bind relies on the owner's out-of-band check.
  A mistaken re-bind would show one person another's record. The re-bind is therefore logged, limited to
  rows whose data still exists, and needs an explicit confirmation.
- A /home restore done against the runbook (without Pause) during a lab-database outage can show a left
  member their own deleted data until the database answers. Two faults at once; accepted and documented.
- A member's spend stays in today's cap after they leave, so the cap cannot be reset by leaving.
- Ending team mode needs the owner to tell members outside Dojo, because Dojo holds no mail permission.
- Dojo gains one dependency, PyAV (with FFmpeg inside), only to decode learners' audio in team mode. It
  parses untrusted input, so it is fenced (format and codec allow-lists, size, length and time limits) and
  watched by Dependabot.

## Decisions for the owner

1. **The pilot is BLOCKED until the residual risk is accepted in writing.** Before switching team mode on,
   the owner names the accountable privacy and service owner outside the build team (EG-36), who reviews
   `docs/privacy.md`; checks with their organisation's privacy team; and, for colleagues in Germany or the Netherlands,
   with their works councils (BetrVG §87(1) no. 6; WOR art. 27(1)(l)). They must accept operator access
   as described. If they refuse it, the plan becomes "each colleague runs their own Dojo". Switch on only
   after all four PRs and that acceptance.
2. The admission policy: one tenant (the sign-in tenant), invited guests only, owner approval in Dojo. Optionally
   also set "Assignment required? = Yes" in the Entra admin center and assign each member. Unassigned
   colleagues then cannot reach "Ask for access". Global Administrators are exempt.
3. The operator statement in the notice and the sheet: the operator administers the sign-in tenant and the
   Azure subscription; either lets them read all stored data, unseen by members; Microsoft processes it as the service provider under its terms. Plus their written commitment
   not to read members' data.
4. Keeping a second administrator (or a break-glass account) in the sign-in tenant, so the owner's own
   lost guest account can be re-invited.
5. The budget defaults, the global daily cap and the 5-member threshold for cost totals.
6. The model deployment type: GlobalStandard (today) or Data Zone EU where the models offer it (see
   `docs/privacy.md`).
7. Whether 30 members is enough.

## Review triggers

- More than 30 members, a second App Service instance, or more than one worker process.
- Sign-in from a second tenant or with personal accounts (a new admission policy under EG-5).
- **The privacy owner or a works council refuses operator access.** Then team mode stays off, and the
  sovereignty-preserving option (each colleague runs their own Dojo in their own subscription, with
  shareable course packages and, later, opt-in federated aggregates for circles) gets its own ADR.
- Any feature that shows one learner's progress or activity to another, or to the owner (Play, circles,
  study groups). It needs its own ADR under EG-24 and EG-25 (ADR 0010 for Play), and a fresh works-council
  and privacy check.
- Any request to show the owner per-member activity, cost or results. The answer stays no unless the
  works-council position changes.
- The labs' free vCore-seconds running out two months in a row.
- A model deployment type or region change (the privacy sheet changes).
- A change to App Service backups (retention, custom backups) or /home nearing 30 GB. Above 30 GB the
  automatic backups cannot be restored.

## How to undo it

Never by flipping the Bicep switch first. See "Pausing and ending team mode":

1. **Pause**, if something is wrong now. Members keep export and delete; nothing is deleted.
2. **End team mode**: the owner tells every member outside Dojo (email or Teams), confirms "I have told
   every member", and picks a date at least 14 days after that confirmation. Members keep full use and
   also see a banner.
   On the date their access ends and their data is deleted (copies stay in the automatic backups for up
   to 30 days more; the tombstones re-purge any restore).
3. Then set `teamMode` back to false and deploy. Easy Auth again admits only the owner.

If `teamMode=false` is deployed while members still have data, Dojo treats it as a suspension: it
deletes nothing, and the owner is warned. The per-learner layout (the owner's `aired`, the lab runners)
stays, because it is also correct for one learner.

## Appendix A - The audit: what assumes one learner

How to read this: the place, then what breaks or leaks with two members, then what it needs. The line
numbers are at 99342a9.

### Identity, settings and wiring

| Place | With two members | Needs (PR) |
|---|---|---|
| `infra/main.bicep` `auth` (`allowedPrincipals.identities = [ownerObjectId]`) | A second person cannot sign in. | `teamMode` parameter (1) |
| `infra/main.bicep` app settings | No `DOJO_TEAM`; no tenant check in Easy Auth beyond the issuer. | `DOJO_TEAM`, and `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` in team mode (1) |
| `core.py` `Settings` | No team switch. | `team` field (1) |
| `core.py` `current_learner` (L331-357) | 403 for anyone but the owner. No role, no membership, no email. The key is derived from the token, so a lost guest account strands the record. | Five route classes; key from the member row; `DOJO_OWNER_KEY` (1) |
| `core.py` `Store` write paths, `append_event` (L280) | Nothing stops a late job writing under a deleted learner; a /home restore brings deleted folders back. | Generation and tombstone checks; startup re-purge (1); database mirror (4) |
| `main.py` (L43, L334, L326, L329) | Root logger at INFO; every `/api` request logged with its concrete path; refusals log the path. A per-member activity trace on /home/LogFiles. | WARNING in Azure, no per-request lines, route templates only, scrubbing filter (1) |
| `main.py` `create_app` (L236-260): `owner = Learner(...)` passed to Preparer, DeliveryCheck, DriftCheck, Podcast, PoolKeeper, Planner, NewExams and DeeperLessons; `deeper.busy = preparer._learner_busy` | Every background service thinks only of the owner. | See the services table (2) |
| `main.py` `about` | The storage text speaks to one learner. | Text names the operator (1) |
| `main.py` `me` | No role or enrollment. | `role`, `team`, `enrolled`, `notice`, `display_name` (1) |

### Routes that must be admin-only (all take plain `Me` today)

| Route | With two members | Needs (PR) |
|---|---|---|
| `/api/diag`, `/api/diag/lab`, `/api/diag/run` | Any member runs self-tests that spend money and see the configuration. | `Admin` (1) |
| `/api/studio/voices`, `/api/studio/costs` | A member sees the owner's spend and voices. | `Admin` (1) |
| `/api/studio/drift`, `/drift/refilm`, `/drift/labs/{lab}/looked` | A member triggers paid refilming and sees the owner's item counts. | `Admin` (1) |
| `/api/studio/deeper` | A member sees and steers paid content work. | `Admin` (1) |
| `/api/exam-packages*`, `/api/exam-lookup` | A member adds or removes exams (spends money; deletes pools). | `Admin` (1) |
| `/api/exam-packages/{pid}/film` | A member approves paid filming. | `Admin` (1) |
| `/api/lessons/{id}/video` | A member starts paid avatar filming. | `Admin` (1) |
| `POST /api/lessons` on a skill that has a lesson | A member's rewrite becomes everyone's lesson and voids its podcast and film. | Split: `POST /api/lessons` first lesson only (409 otherwise); `POST /api/lessons/{id}/rewrite` admin (1) |

### Per-learner pieces that are owner-bound

| Place | With two members | Needs (PR) |
|---|---|---|
| `podcast.py` `FeedLinks.owner_for` (L165-202) | Only the owner's token maps to a learner. Another member's feed is impossible, or would show the owner's. | Hash lookup across members (2) |
| `podcast.py` AIRED (L314-364), global `podcast/aired.json` | Lessons that reach A's phone count as aired for B, so B gets podcast questions for lessons B never heard. | `learners/<key>/aired.json`, migration (2) |
| `podcast.py` `forget`, `could_be_on_phone`, `question`, `status` (L566, L661) | Do nothing, or return None, for non-owners. B is never asked the podcast question and sees the owner's feed status. | Per learner (2) |
| `plan.py` `CalendarLinks` (L759-805), `/cal/*`, `/api/calendar*` | Owner-only token map. | Hash lookup across members (2) |
| `plan.py` `view`, `today`, `links.status()` | Every learner's plan shows the owner's calendar link status (a leak). | Per learner (2) |
| `plan.py` plan building (L864, L1197) | Plans over every package, not the learner's. | Enrolled exams only (2) |
| `exam.py` `PoolKeeper` (L751-930) | Only the owner's pool is kept full; `pool-budget` under the owner; `paused()` ignores others; `_busy` sees only owner jobs; `_next_package` uses the owner's active exam. It calls `top_up` (L718) on its own thread, outside `Jobs._run`, so its model calls carry no attribution. | Round-robin over members; attribution, generation and a reservation around every batch (2, 3) |
| `exam.py` `ExamRoom.pool`, seen keys (L116-127, L228) | Already per learner. | Keep; test (2) |
| `drift.py` `_owner_items`, `_owner_pool` (L714-736) | Studio counts only the owner's. Correct for privacy; must be labelled so. | Label (2) |
| `drift.py` `_owner_busy`, films filed as the owner (L1010-1050) | Drift work does not wait for members' jobs; with 30 members "anyone busy" would starve it. | Spare-slot rule, rotation (2) |
| `prepare.py` `order`, `rewrites` (L92-107) | Priority is only the owner's active exam. | Order across learners (2) |
| `prepare.py` `_learner_busy` (L117), `FILM_QUIET_S` (L394) | Waits only for the owner's jobs; "anyone busy" would starve it. | Spare-slot rule, rotation (2) |
| `prepare.py` lessons made with `self.owner` (L214, L249) | Background lessons are attributed to the owner. | Content attribution (2, 3) |
| `delivery.py` `_learner_busy` (L86), `active_package` (L159) | As above. | As above (2) |
| `deeper.py` `_active` (L209) | As above. | As above (2) |
| `newexam.py` `remove` (L645) | Deletes only the owner's pool for the exam. Members keep orphan pools and enrollments. Nothing stops removal during an open attempt, and history loses its package. | Refuse while an attempt is open; every member; `packages-retired/<pid>.json` (2) |
| `newexam.py` `start_build` (L266, L345) | Records `package.added` and the exam date in the owner's record and profile. | Keep (admin only) (1) |
| `checks.py` `_say` (L311) | A member's check-question audio lands in the shared `media/` and survives their deletion. | Per-learner media (2) |
| `learning.py` `Receipts` (L415) | One global limit; one learner can push out another's receipts. They are then recorded as unconfirmed (not in anyone's favour). | Per-learner limit (2) |
| `core.py` `Jobs.start` (6 per learner), `slots` (3) | One member can fill the shared queue; first in, first out. | Member cap 3, fair queue, spare slots for background (2) |
| `core.py` `Jobs._recover`, `_free`, `for_learner` | Read every job document; linear in jobs. | Accept at 30 members (none) |
| `ai.py` `Foundry.chat` (L71) | Ignores `usage`; sets no `max_completion_tokens`, so a call has no bounded cost; retries up to 4 times. No allowance or cap can be enforced before spending. | Per-call maximum, reservations, settlement, metering into the learner's own allowance and pooled sums (3) |
| `costs.py` `usage`, `view` (L323-396) | Owner-wide estimates only. | Pooled totals under the 5-member rule; never per member (3) |
| `labs.py` `LabRoom.prepare` (L275-316), `SqlRunner.run` (`lab_runner`), LABS schemas | Every learner shares `lab_vector`, `lab_search` and `lab_hybrid` and one runner: A reads, changes and drops B's tables. Schema names are visible to every database user in `sys.schemas`. | Per learner and lab schemas and runners, named by a random surrogate stored in the member row, never derived from directory ids (4) |
| `labs.py` `SqlRunner.reset` (L189-235) | Turns off versioning for tables whose history is in `dbo` and drops every `dbo` table: one learner's reset reaches the whole database. | Member reset limited to their own schema; `dbo` clean-up as admin repair only (4) |
| `labs.py` LABS text, FakeRunner checks (`lab_vector.products`) | Qualified names point at the owner's schema. | Unqualified names, templates (4) |
| `learning.py` `delete_all`, `export` (L1935-1968) | Correct per learner, but they do not cover membership, `aired`, check media, labs or jobs. | Leave deletes the whole folder and covers them (1, 2, 4) |
| `learning.py` `lessons_for` (L831-872) | The newest lesson wins for everyone. | Admin-only rewrite (1) |
| `learning.py` item generation (prompt L62, "Already used" list L1289) | Sends the author the learner's earlier private item situations. Stays within one learner, but it is learner data going to a model. | Keep; stated in `docs/privacy.md` (1) |
| `main.py` logging (L43 `basicConfig(INFO)`, request line L334, refusal warnings L326-329) | Every API request is logged with method, path, status and time: with few members, a timestamped activity trace on /home/LogFiles. Warnings carry raw paths. | WARNING in Azure, no request lines, route templates, scrubbing filter (1) |

### Already learner-keyed (keep, and test with two learners)

Items, answers and hints (`learning._item_path`, L1203); rehearsals (L1636-1777); asks (L1781); profile
and exam dates (L1842); the quality log split (L572, L1931); practice exams and pools (`learners/<key>/exams`,
`pool`, `seen`); checks (`checks._path`); the plan and sessions; the hash-chained events; feed and calendar
token documents; advice; readiness; export and delete; `Jobs.get`; receipts.

### Shared content (stays shared)

`content/lessons`, `content/deep`, the content lines of `content/quality.jsonl`, `content/delivery*`,
`content/prices`, `content/package-sources` (overlays), the drift and deeper state documents, `sources/`,
`media/` (narration and films), `packages/`, `package-builds/`, the podcast episode files, and
`selftest-probe`.

### The SPA (`app/static/app.js`)

| Place | With two members | Needs (PR) |
|---|---|---|
| `renderSide` (L365-390) | Shows Studio, "Add an exam" and every package under "Your exams". | Role-aware; enrolled exams only (1, 2) |
| `fillExamSelect` (L429-436) | Offers "Add an exam…" and every package. | Admin-only option; enrolled exams (1, 2) |
| routes (L5070): `#/add-exam`, `#/quality` (Studio) | A member can open them; the server would then refuse. | Hide; show "for the owner" (1) |
| Studio (L3118-3240): costs, voices, drift, deeper, refilm | Admin screens. | Admin only (1) |
| Watch, "Film this lesson" (L1305-1315) | Paid action. | Admin only (1) |
| About (L3514) calls `/api/diag` and `/api/diag/lab` | Calls admin routes. | Admin only; members see the privacy text (1) |
| Record delete text (L3110) | Talks about one owner. | Add "Leave Dojo and delete my data" for members (1) |
| Podcast panel (~L1050), calendar (~L4047) | Must show the learner's own status. | Per learner (2) |
| Add an exam screens (L4512-4945) | Admin only. | Admin only (1) |
| Skill view, "Fresh lesson" (L821) | A member's rewrite would replace everyone's lesson. | Admin only, on the new rewrite route (1) |
| New screens | None exist. | Ask for access (with "used Dojo before"), Notice, enrollment picker, Members (with re-bind, Pause, End team mode), exit page, Pause and End banners, renewal prompt, "left today" (1, 2, 3) |

## Appendix B - How each learner-scoped object is checked (IDOR)

| Object | Routes | Ownership check |
|---|---|---|
| Items, answers, hints, grades, disputes | `/api/items*`, `/answer`, `/grade`, `/hint`, `/dispute` | Path `learners/<me>/items/<id>`; id pattern-checked; missing means 404 |
| Spoken-answer receipts | `/api/transcribe`, `/answer` | In memory, keyed by my learner key and the item id |
| Jobs | `/api/jobs/{id}` and every poll | Global folder; `Jobs.get` compares `job.learner`; others use `for_learner` |
| Practice exams, pool, seen, rationales | `/api/exams*` | Path `learners/<me>/exams`, `pool`, `seen`; package must be enrolled |
| Rehearsals | `/api/rehearsals*` | Path `learners/<me>/rehearsals` |
| Spoken checks and their audio | `/api/checks*`, `/api/media/{id}` | Path `checks._path(me)`; audio under `learners/<me>/media` |
| Asks | `/api/ask*`, `/api/asks` | Path `learners/<me>/asks` |
| Profile, enrollment, exam dates | `/api/me`, `/api/settings` (GET, PUT) | Path `learners/<me>/profile` |
| Display name | `/api/me` | Only I set mine; peers will see only this name, never the Entra name |
| Opt-in storage for Play | none in Team | Path `learners/<me>/social/`; Play's routes must use the same rule |
| Plan and sessions | `/api/plan*`, `/api/plan/today`, `/api/today/{pid}`, `/api/plan/sessions/{id}/*`, `/api/plan/rules` | Path `learners/<me>/plan`; session id looked up only in my plan; enrolled exams only |
| Lesson views | `/api/lessons/{id}/viewed` | The lesson is shared; the `lesson.viewed` event goes only into my record |
| Calendar token and status | `/api/calendar`, `/api/calendar/link*`, `/cal/{token}` | Token doc under `learners/<me>`; `/cal` matches the hash against the owner (always) and every active member, in constant time |
| Podcast token, status, `aired`, question | `/api/feed`, `/api/feed/link*`, `/feed/{token}/*` | Token doc under `learners/<me>`; hash match over the owner (always) and active members decides the learner; `aired` under that learner only |
| Record (events), readiness, advice | `/api/record/chain`, `/api/readiness` (GET, POST), `/api/readiness/official`, `/api/skills/{pid}/{sid}`, `/api/packages/{pid}` | Path `learners/<me>/events`; the chain is never rewritten; package must be enrolled |
| Export | `/api/record/export` (`learner_exit`) | Built only from `learners/<me>`; works for removed members within 30 days and during Pause or End team mode |
| Delete and leave | `POST /api/record/delete`, `POST /api/team/leave` (`learner_exit`) | Only `me`; the owner cannot leave; tombstone or generation first, so late jobs cannot write back |
| Quality lines | `/api/quality` | My lines plus the content lines; never another learner's |
| Lab runs and lab tables | `/api/labs`, `/api/labs/{lab}`, `/run`, `/check`, `/hint`, `/reset` | SQL Server: my runner user has rights only on my schemas; check and reset run as admin against the schema named by my lab surrogate from my member row, never from the request; reset touches only my schema; lab events go into my record |
| Allowances and cost | `/api/me` (my "left today"), `/api/team/members` | My counts live in my folder and are shown only to me; the admin sees pooled totals under the 5-member rule only |
| Another person's data, for any role | none | No route exports or returns another person's learning data; the admin export is the admin's own record |
| Lessons asked for | `POST /api/lessons` (first lesson only, 409 otherwise), `/api/lessons/{id}/audio`; `POST /api/lessons/{id}/rewrite` is admin | Shared content; both learner routes reserve against my allowance |
| Membership and requests | `/api/access*` (`signed_in_non_member`), `/api/team/*` (admin) | My request is found by the token's tid and oid only; re-bind is an admin action after an out-of-band check, logged in `team/audit.jsonl` |
| Shared content | lessons, deep lessons, media, packages, episodes | Shared by design; package-scoped views need enrollment |
