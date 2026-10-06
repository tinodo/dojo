# Dojo privacy and data-flow sheet (draft)

Status: draft for the outside privacy review that EG-36 requires before team mode is switched on
(ADR 0009): a privacy owner outside the build team and, where they apply (for example for colleagues in
Germany or the Netherlands), the works councils.
**The pilot is blocked** until they have read this sheet and accepted the residual operator-access risk
below in writing (a decision taken by the build lead under the owner's standing instruction). It describes Dojo as built at 99342a9 plus the Team Dojo design. Items marked
**(confirm)** are for the reviewer to check against the service terms in force.

## Who runs this Dojo

- **Operator:** the owner of this Dojo, who administers the hosting and the sign-in tenant. The operator
  runs the hosting, the keys and the AI services. Dojo admits learners only with the operator's approval.
- **Tenant and subscription:** Dojo runs in an Azure subscription and signs people in through one
  Microsoft Entra tenant (the sign-in tenant). The operator has administrator rights over that tenant and
  that subscription. Either set of rights gives full access to the stored data.
- **What that means:** the operator can technically read everything Dojo stores about every learner: the
  /home file share (through Kudu, SSH or a backup download) and the lab database. **Members cannot see
  when the operator does.** Dojo cannot log or show that access, and members cannot see the platform's
  activity log. The operator can also change the code that runs. This is operator access. It is not
  learner ownership, and Dojo does not call it that.
- **The operator's written commitment:** the operator does not read members' stored data. If a support
  case ever needs it, the operator asks the member first. A commitment is not a technical control; see
  "Residual risk".
- **Deployment:** GitHub Actions deploys from the main branch with a federated identity that is Contributor
  on the app's resource group only. Code that is merged and deployed runs with the app's managed identity,
  which can read all stored data.
- **Microsoft** hosts and processes the data as the cloud provider, under its online services terms.

## Who can sign in

- Only users of the sign-in tenant (its members, and B2B guests that the operator invites in the Entra
  admin center) can sign in. The app registration is single-tenant.
- With team mode off, only the owner is admitted.
- With team mode on, Easy Auth lets every member and guest of the tenant through, and checks the tenant id.
  Dojo's own allow-list (tenant id plus object id), kept by the owner, is the real gate.
- Sign-in asks for identity scopes only. Dojo has no permission to anyone's mail, files, chats or
  calendar.
- A learner is identified by the tenant id plus the object id of their account in the sign-in tenant (a
  one-way hash forms the folder name). It is never their email address.
- The email (always present for guests) and the name (present only if the token carries it) are shown to
  the owner on the access request and the Members page. They never decide access.
- A membership lasts 12 months. In the last 30 days the member is asked once at sign-in whether to keep
  it. Only that click renews it. If it is not renewed, it ends and the data is deleted 30 days later. Dojo
  stores only the renewal month, and the owner does not see it.

## What is stored, and where

| Data | Where | Kept until |
|---|---|---|
| Learner record: answers, how sure you were, your own marks on lesson self-checks, the recall log, hints used, grades and findings, disputes, rehearsals, spoken checks, practice exams, labs results, coach questions and answers, plan, exam dates, settings. The record is hash-chained per learner; no chain spans two learners | App Service /home file share in the operator's Azure region (Sweden Central by default), under the learner's own folder | The learner deletes it or leaves. A removed or expired member's data is deleted 30 days after removal. When team mode ends, on the announced date |
| Background job results (may contain a coach answer or feedback) | /home, `jobs/` | Removed at the first restart after 14 days; deleted at once on delete or leave. A job still running at that moment cannot write anything back |
| Podcast and calendar links (a hash of the secret, not the secret) and "on air" notes | The learner's folder | Delete or leave |
| Audio of spoken check questions | The learner's folder (team mode) | Delete or leave |
| Membership: tenant id, object id, name and email from sign-in, chosen display name, role, status (pending, active or removed), joined date, removal date and reason (left, removed, expired, team ended), notice acknowledgement, renewal month (hidden from the owner), a random lab name part. No activity date | /home, `team/members.json` | On leave, the name, email and display name are deleted at once. The bare row goes 40 days later |
| Access requests: tenant id, object id, name, email, time, and the "I used this Dojo before" mark | /home, `team/requests.json` | 30 days |
| Admin actions: approve, decline, remove, re-bind, pause, resume, "I have told every member", end; with the time and the display name concerned. No learner activity | /home, `team/audit.jsonl` | 12 months. When a member's data is deleted, their display name in it becomes "a former member" |
| Deletion list (tombstones): a one-way hash of each deleted learner's key and lab name part, and the deletion date; the membership status changes (a one-way hash of the learner key, status, reason, date) and a one-way hash of an old account replaced by a re-bind; a counter that shows whether /home was restored; the Pause state | /home, `team/tombstones.json` and `team/state.json`, and a copy in the lab database (schema `dojo_meta`: only hashes, statuses, reasons, dates and the counter) that only the app's admin connection can read; no learner's lab user has any permission on it | 40 days. Used only to delete again whatever a backup restore brings back |
| My allowance: how many model-backed actions I used today and yesterday, their cost at list price and the open reservations | The learner's own folder, `allowance.json`. Shown only to that learner ("Today's allowance" on their record page); the owner's page never reads it | Today and yesterday (older days are removed hourly); deleted on leave |
| Today's members' spend: one sum for all members, plus open reservations under random ids (each with its amount, the time it was reserved and what it has drawn so far). It enforces the daily cap | /home, `team/cap-today.json`. Holds no learner key, name, action or exam. Never shown while there are fewer than 5 members | Replaced by an empty file when the budget day ends (midnight UTC). Leaving does not lower today's sum |
| Today's practice-question batches for all members together: the day and one count. It enforces the members' daily limit of 24 batches | /home, `team/pool-budget.json`. Holds no learner key, name or exam. Not shown | Starts again at zero on the next budget day. Leaving does not lower it |
| The allowance values and the daily cap the owner set | /home, `team/budget.json`. No learner data | Kept until changed |
| "Reservation bound exceeded": the model role and call kind whose real cost was above its reservation, and the day | /home, `team/budget-flags.json`. No learner key, item or amount | Kept; it holds nothing about a person |
| Cost totals at list price, pooled | /home, `team/meter.json`. Holds no learner key, name or member count | Kept. The 5-member rule is applied **when it is written**: an exam has its own sum only while 5 or more members are enrolled in it; "members together" exists only with 5 or more active members; with fewer, everything (members, owner and content) is one sum, "Dojo in total" |
| Play (ADR 0010): the learner's Play settings, moment deletions and, if they joined circles, that opt-in ("Others can invite me", the notice version) and whether they joined the effort board ("just my points"). Points, weeks met and moments are worked out from the record and not stored | The learner's own folder, `social/play.json` | Leave Play, Delete my record or leaving Dojo deletes it at once; leaving circles deletes the opt-in |
| Play circles: name, members (learner keys and the dates they joined), open invitations, the goal, and the copies of moments or passes members shared. No points, no activity, no inviter | /home, `play/circles/<id>.json`. Shown only to that circle's members, by display name | Invitations 7 days; shared copies 4 weeks, or until withdrawn, or at once when the sharer's record no longer supports them; a member's entries at once when they leave the circle, Play, their record or Dojo, or are removed, or their membership ends; every circle at once when the owner switches circles off |
| Play effort board: this week's snapshot, each member's band (0-3) and the hash of their last Record event at publication (so their own points are read from the same Record), by learner key. No points, no names, no order | /home, `play/board.json`. A member sees their own points and band, and the display names in their own band only when at least 3 others share it | Replaced every Monday (no history); a member's entry at once when they leave the board, Play, their record or Dojo, or are removed, or their membership ends; deleted when the owner switches circles off |
| Play duels: the two learner keys, the exam (and domain), the 10 questions, the invitation time (for expiry only), whether it was accepted, each player's quick-practice id and, once both finished, the two numbers right. A decline removes the invited key and nothing else | /home, `play/duels/<id>.json`. Shown only to the two players; the answers are in each player's own quick practice, without the other's name | 7 days after the invitation; at once when either player leaves Play, their record or Dojo, or is removed, or their membership ends; every duel when the owner switches circles off |
| Lab tables a learner creates | Azure SQL Database in the same region, private endpoint, the learner's own schemas. The schema names use a random part stored by Dojo, never a directory id or the learner key, because every database user can list schema names | Reset (own schemas only, nothing else in the database), or deleted with the learner: on leave, or 30 days after removal, the lab schemas and the learner's database users are dropped at the next sync with the database. Each learner's SQL runs as their own database user, which can reach only their own schema for that lab. The owner (as operator) can read every schema. The database's automatic backups keep a copy for up to 7 days **(confirm the retention)** |
| The owner's random lab name part | /home, `team/owner.json`, written at the owner's first lab use. Not tied to any account id; if the file is lost Dojo reads it back from the owner's database user | Kept, so the owner's labs survive a lost account |
| Shared content: lessons, deep lessons, questions' sources, narration, films, podcast episodes, exam packages, and a frozen summary (title, skills, scoring) of a removed exam so old attempts still show | /home | Not personal; kept |
| Application logs | /home/LogFiles | 7 days or 35 MB. In Azure, Dojo logs only warnings and errors, in both modes. It writes **no line per request**. An unexpected error writes one line with the method, the route pattern (for example `POST /api/items/{item_id}/grade`) and the error type, never the concrete path, a learner key, an item, attempt, job or session id, a token, a name or an email. A filter removes any such id that slips through. The web server (gunicorn) is started without an access log |
| App Service automatic backups of /home | Taken hourly by the platform (Basic plan and higher); they include all of /home: every record, membership, request and meter file | 30 days. A backup larger than 30 GB cannot be restored |

The first sign-in notice says where data is stored in these words: "On this web app's own storage in the
operator's Azure region, in your own folder. Lab tables live in one Azure SQL database in the same region."
In Azure, App Service reports the region, and the notice and the About page name it, for example "in Azure
(Sweden Central)". A Dojo without a lab database says "This Dojo has no lab database." instead of the second
sentence.

The spoken audio of an answer is sent to Speech for transcription and is not stored by Dojo. In team mode,
Dojo first decodes the recording in memory on its own server (at most 2 MB and 200 seconds) and sends
Speech a 16 kHz mono copy it wrote itself; neither the upload nor the copy is written to disk.

## Which AI services see what

All models run in the operator's Azure AI Foundry resource, in the operator's Azure region (Sweden Central by default). Each is a **GlobalStandard**
deployment. **A GlobalStandard request may be processed in any Azure region, including outside the
EU/EEA**; data at rest stays in the resource's geography **(confirm for each model)**. The alternative is a
**Data Zone EU** deployment, which keeps processing inside the EU, where the model offers one. That is a
choice for the reviewer (see the end of this sheet); it may change the models or the price.

| Service | Model | What it reads |
|---|---|---|
| Author | gpt-5.5 (OpenAI, hosted by Azure) | Lessons and questions from public Microsoft Learn and GitHub Docs pages; the coach's answer to a learner's question (the question text); when it writes a new practice item for a learner, **the situations of that learner's earlier items** ("Already used"), so it does not repeat them |
| Gate and live feedback | grok-4-1-fast-reasoning (xAI, hosted by Azure) | Everything shown to a learner, before it is shown; live feedback on a learner's answer |
| Grader | DeepSeek-V4-Pro (DeepSeek, hosted by Azure) | A learner's finished answer, the question and the rubric |
| Speech | Azure AI Speech | Text to speech: lesson text and check questions. Speech to text: the learner's spoken answer (audio). Avatar: lesson text only |

- Dojo uses stateless chat completions. It uses no stored responses, threads, files, vector stores, batch,
  evaluations or fine-tuning.
- **What Dojo adds, and what it does not.** Dojo adds no name, email, tenant id, object id or learner key
  to a prompt. But **anything a learner types or says** (answers, questions to the coach, spoken answers,
  dispute reasons, lab SQL) goes to the models as written, and may contain anything, including names.
  The notice tells learners so.
- A learner's earlier items go only into prompts made for that same learner, never for another.
- Provider-side abuse monitoring may keep prompts and outputs for a limited time **(confirm the period and
  whether modified abuse monitoring applies)**.
- The models are hosted by Microsoft. No data goes to OpenAI, xAI or DeepSeek as companies **(confirm)**.
- Spending is capped before it happens: each learner has daily allowances, and members together have a
  daily cap (default USD 5 at list price). Before an action is recorded, Dojo sets aside the most its
  whole first round of model calls can cost (every call, with a retry), and sends no call that is not
  covered. A second try, or another batch of practice-exam questions, runs only if what was set aside still
  covers it; otherwise it is left out, and the learner is told. Money set aside before midnight UTC counts
  on the day it was set aside, never on the next. A learner who reaches an allowance, or arrives when the
  shared cap is used up or set aside for work running now, is told so calmly and nothing is recorded; the
  message does not say who used the shared budget. If judging an answer is cut short because the shared
  budget ran out, the answer stays saved and the learner can have it judged after midnight UTC. These
  rules hold only amounts and random ids in the shared files, never a learner key. The owner is metered
  like everyone, but never refused.
- Before a spoken answer is sent to Speech, Dojo decodes the recording on its own server and sends Speech a
  plain 16 kHz mono copy that it wrote itself. A recording that cannot be decoded, or that is longer than
  200 seconds, is refused and not sent. Neither the upload nor the copy is stored.

## What the owner's admin page shows

Dojo offers no per-member views and keeps as little as it can. It does **not** claim to be unsuitable to
monitor: the operator's technical access (above) remains.

- **Members list:** display name, Entra name and email (for admission only), role, status and joined date.
  Nothing else.
- **No activity or results:** no last-active day, no per-member cost, no per-member counts, no answers,
  records, readiness, plans, practice-exam results or labs. No renewal date.
- **Costs:** only as the pooled sums described above, under the 5-member rule applied when they are
  written. With fewer than 5 members there is only "Dojo in total".
- **Allowances:** the owner sets the values and the daily cap. Dojo enforces them per learner by itself.
  The owner is not told who reached an allowance, or when the cap was reached.
- **No export or API of another person's data**, for any role. The owner's "Download my record" is the
  owner's own record.
- **Honest limit:** the Azure bill and the AI resource's metrics show total use per day. With very few
  members, a sharp change can hint at one person's use. Dojo adds nothing to that.
- **Play:** no admin page shows anyone's Play data. The admin role sees only whether circles are switched
  on (see "Play: circles, the board and duels" below).
- **Peers** see nothing of each other in Team. Play's circles show only the display name the member chose,
  never the Entra name, and only what the section below lists.

## Play: circles, the board and duels (ADR 0010)

Play's personal layer (study points, weeks met, the moments shelf) is private to the learner and off until
they turn it on. Its social layer (circles, the effort board and duels) exists only in team mode, is off
until the owner switches it on, and then each member opts in on their own after a one-page notice in two
parts. These are its words.

**In the app**

- **What circles are for.** A circle is 3 to 12 people who chose each other, to study alongside each
  other: a shared weekly goal in study points, and a place to share a moment or a pass when you want to.
  Nothing in a circle ranks or compares anyone.
- **What is kept, and for how long.** In your own folder, until you leave: that you joined, and whether
  others can invite you. For each circle: its name, its members and the dates they joined, open
  invitations (7 days), the goal, and the copies members shared (4 weeks, or until withdrawn, or at once
  when your record no longer supports one). The bar is worked out from members' records at most once a day
  and is not stored.
- **Who sees what.** If you turn on "Others can invite me", people who opted in to circles see your
  display name in the list of people they can invite. Members of a circle see each other's display names,
  what each chose to share with that circle, the goal, and the bar. They do not see your points, study
  days, answers, results or moments, unless you share a moment. Nobody sees who was invited: an invitation
  you decline or let lapse is deleted, and the person who invited you is never told.
- **No admin view.** No page in Dojo shows anyone's Play data to the owner's admin role: not the Members
  page, not diagnostics, not Studio. The admin role sees only whether circles are switched on.
- **The person who runs this Dojo.** The operator may use Play as a learner. They cannot start a circle
  or invite anyone; they are in a circle only when a member invites them, and there they are shown as
  "runs this Dojo" and see what every member of that circle sees.
- **What the bar can reveal.** The bar shows only 0, 25, 50, 75 or 100% of the goal, only in circles of 5
  or more members, and changes at most once a day. The goal is at least 100 study points per member. It
  does not hide everything: if every other member of your circle pools their own numbers, they can learn
  that you studied substantially in a period. That is effort only, never results: no score, answer, skill
  or moment can be read from the bar. If you do not accept that, stay out of circles, or leave one at any
  time.
- **The effort board.** Off unless you join it, and shown only when at least 10 people have joined. Once a
  week, on Monday, it puts last week's study points (at most 400) into four bands: Getting going (0-99),
  Building (100-199), Steady (200-299) and Strong week (300-400). You see your own points and your band,
  and the display names of the others in your band only, in alphabetical order, and only when at least 3
  others share it; otherwise how many others do. Nobody sees another band, an order, a position or anyone
  else's points. It is a comparison of effort between colleagues, never of results. The person who runs
  this Dojo is never on it. Only this week's board is kept; leaving takes you off it at once.
- **Duels.** A duel is by invitation only, to someone who turned on "Others can invite me" and studies the
  same exam, so they see your display name in the duel list for the exams you both study. Both answer the
  same 10 new checked questions within 48 hours, in your own time, with no clock. Only the two of you see
  the two numbers right, once both have finished; each reviews only their own misses. There is no win, no
  loss and no record of results. An invitation you decline is not kept: to the person who invited you it
  looks exactly like one that lapsed. Your answers stay in your own record like any quick questions,
  without the other person's name. The duel is deleted 7 days after the invitation, or at once when either
  of you leaves. The person who runs this Dojo never invites anyone to a duel.
- **Leaving.** Leave a circle at any time; what you shared there goes with you. Turning circles off, or
  Leave Play, deletes your memberships, your shared copies, your invitations, your place on the board and
  your duels at once.

**Outside the app**

- **What the operator can technically access.** Outside the app, the operator is Global Administrator of
  the Microsoft tenant and Owner of the Azure subscription Dojo runs in. They can technically read
  everything Dojo stores, including Play's settings, the circle files, the board and the duels, and you cannot
  see when they do.
  Their written commitment: they do not read members' stored data, and if a support case ever needs it,
  they ask you first. A commitment is not a technical control.
- **Never for performance.** Dojo data is never used for performance reviews, staffing or HR decisions,
  and nothing is shared with managers.

**How the rules hold.** A shared moment reads, for example, "Ada relearned every skill in 'Use GitHub
Copilot responsibly' (GH-300) on 2 Oct. From Dojo's own questions. Not a certification."; a pass reads
"Ada reports passing GH-300.", never with a score or a date, whether or not a transcript listing was
found. The sharer sees exactly these words before sharing. The bar is computed from the days before today
(00:00 Europe/Berlin) of the members who were in the circle before Monday: a member counts only from the
Monday after they join, and the bar starts once 5 do, so a newcomer's first days are never shown. The goal is set on Monday and fixed
for the week; when a member leaves, the bar is hidden until the next Monday. Responses carry display names
and opaque ids, never a learner key, an Entra name or an email, and Play writes nothing per person to the
logs.

**The bar's remaining risk is a lead decision**, accepted as residual risk for the privacy reviewers (ADR
0010, section 4): only if every other member of a circle pools their own numbers can they learn that the
remaining member studied substantially in a period. Effort only, never results. Dojo promises no more.

## Written commitments

These are stated in the first sign-in notice as well.

- The operator does not read members' stored data. If a support case needs it, the operator asks the
  member first.
- Dojo data is never used for performance reviews, staffing or HR decisions.
- Nothing is shared with managers.
- Social features are opt-in and off by default. Leaving a social feature deletes its data.
- Each learner's record has its own hash chain. No chain ever spans two learners.
- "Leave Dojo and delete my data" deletes the whole learner folder. Copies stay in automatic backups for up
  to 30 days.
- No one, the owner included, can export or call up another person's learning data in Dojo.
- Peers only ever see the display name the member chose.
- If team mode ends, members get at least 14 days' notice and can download or delete their data until
  then.

## Residual risk and works councils

In Germany (BetrVG §87(1) no. 6) a works council co-decides on any technical system that is objectively
suitable to monitor behaviour or performance, whatever the intent. The Federal Labour Court counts group
data when the pressure passes to individuals (BAG 1 ABR 7/15). In the Netherlands, WOR art. 27(1)(l)
covers systems "aimed at or suitable for" monitoring. Consent between colleagues and an operator inside the
same employer is weak (GDPR Recital 43).

Dojo does not rely on consent, and it does not claim to be unsuitable to monitor. What it offers:

- no per-member views and no per-member numbers anywhere outside the member's own folder;
- no activity trace in the logs;
- automatic enforcement of budgets, so no one needs individual numbers;
- the operator's written commitment.

What remains: the operator can technically read stored data, and members cannot see when. **That
residual risk is the reviewer's to accept or refuse.** Until it is accepted in writing, team mode stays
off for real learners.

The build lead recorded this as a decision (ADR 0009, "Residual risk"): the product shows nothing per
member and keeps as little as possible; the operator's access is disclosed, not designed away; the pilot
starts only after written acceptance by the privacy owner and, for colleagues in Germany or the
Netherlands, the works councils; if they refuse, the self-run alternative below is the path.

**The alternative if it is refused:** each colleague runs their own Dojo in their own Azure subscription.
Then no one else can read their data. Course packages can be shared, and opt-in aggregates for small
groups ("circles") could be exchanged between Dojos later. It was set aside for now because each person
pays for and runs their own hosting, models and lab database, and the federation does not exist yet.

## The learner's controls

- **First sign-in notice:** the learner acknowledges it once. It says what this sheet says, including the
  operator's access and that anything typed or said goes to the models. Until it is acknowledged, only
  "Download my record", "Delete" and "Leave" work.
- **Export:** "Download my record" gives the full record as JSON.
- **Start over:** "Delete my record" deletes the learner's data and keeps the membership.
- **Leave:** "Leave Dojo and delete my data" first puts the learner on the deletion list, stops their
  queued and running jobs, and then deletes the whole learner folder (record and its hash chain, plan,
  links, "on air" notes, check audio, allowance counts, social data), their job results, their lab schemas
  and database users (dropped at the next sync with the lab database, at once if it is awake), and the
  name, email and display name in their membership. A job that was running
  cannot write anything back afterwards. Pooled cost sums stay; they hold no learner key.
- **What remains after delete or leave:**
  - **copies in App Service's automatic backups of /home, for up to 30 days.** Dojo cannot remove one
    learner from those backups. If /home is restored from a backup, Dojo notices (a counter kept in both
    places is behind) and deletes the learner again at its next start, before admitting any member, from
    the deletion list (kept 40 days, in two places so either restore leaves one). The owner's runbook
    requires pausing Dojo before any restore. **Residual risk:** if /home were restored without pausing
    while the lab database is unreachable, a member who left could see their own deleted data again, and a
    removed member could sign in again, until the database answers; then the removal is applied and the
    data deleted again. Removals are kept in the database for this reason, so a restore made while the
    database is reachable cannot undo them;
  - lab tables in the database's automatic backups, for up to 7 days;
  - log lines, for up to 7 days (they carry no learner ids);
  - provider-side abuse-monitoring copies, if any, for that service's period.
- **Removal by the owner:** access ends on the next request. The removed person can still download or
  delete their record for 30 days. Then it is deleted, and backups keep it for up to 30 days more.
- **Pause:** the owner can pause the Dojo for members. Nothing is deleted. Members can still download,
  delete and leave.
- **End of team mode:** Dojo cannot send mail, so the owner tells every member directly (email or Teams).
  Dojo gives the owner the list of members' emails for that. The 14 days of notice start only when the
  owner confirms in Dojo that every member has been told (logged). Members also see a banner, keep full
  use, export and delete until the date. On the date their data is deleted, as for Leave.
- **Losing the account:** the record belongs to the person's account in the sign-in tenant. If that
  account is deleted and the person is invited again, they get a new account. They tick "I used this Dojo before"
  when asking for access. The owner checks with them directly (a call or a message to the old address),
  and only then binds the old record to the new account. Nothing is copied and the record is not
  rewritten. The re-bind is logged in the admin action log. This works while the old data still exists
  (up to 30 days after removal). Export first if in doubt.

## For the reviewer to decide

1. **Whether the residual operator access is acceptable** (administrator rights over the tenant and the
   subscription; members cannot see when they are used; a written commitment, not a control), how it is
   stated to colleagues, or whether the self-run alternative is required.
2. For colleagues in Germany or the Netherlands: the works council's view on the same point.
3. The model processing geography: GlobalStandard (may process outside the EU/EEA) versus Data Zone EU
   deployments where the models offer them.
4. The abuse-monitoring retention for each model.
5. The retention periods above, including the 30-day App Service backups, the 40-day deletion list, the
   SQL backup window, the 12-month admin action log and logs.
6. Whether an access request (name and email before approval) may be kept for 30 days.
7. The re-bind: whether an out-of-band check by the owner is enough to give an old record to a new
   account.
8. The /home size: above 30 GB the automatic backups cannot be restored. Custom backups (10 GB limit, a
   storage account with a SAS) are not used.
9. The 5-member threshold for cost sums, and the honest limit of the Azure bill.
10. The restore residual risk: a /home restore without pausing, during a lab-database outage, can show a
    member who left their own deleted data, and let a removed member sign in, until the database answers.
11. Play's circles (ADR 0010): whether the bar's remaining risk is acceptable (if every other member of
    a circle pools their own numbers, they can learn that the remaining member studied substantially in a
    period; effort only, never results), and whether circles may be switched on at all. They stay off until
    this sheet, with its Play section, is accepted in writing.