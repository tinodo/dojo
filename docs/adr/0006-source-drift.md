# ADR 0006 - When a source page changes, what was written from it is checked again

- Status: accepted
- Date: 2026-09-30
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: Accepted by the Dojo lead under the owner's standing instruction to decide and build
  autonomously, 2026-09-30; the owner can reject it at any time by saying so. Review triggers are
  listed below.
- Guardrails: EG-12 (record consequential decisions), EG-16 (exposure is not a Finding), EG-20
  (re-gate or withhold cached media after relevant source drift), EG-26 (deletion must be real),
  EG-34 (a new source version may trigger a new interpretation, not mutate the old event), EG-39 (only
  documentation is copied), EG-43 (no new dependency), COMPONENT-MAP M-06 (narrow revalidation, not
  another course), ADR 0002's review trigger (the podcast feed withholds the same lessons), and ADR 0005
  (documentation only; added exams)

## The requirement

DESIGN.md section 3 lists a Build job: "When a source page changes, the affected lessons and questions
are re-checked." Lessons, items and practice-exam questions are written from official pages, and every
quote is checked word for word against a dated snapshot of the page. Each one records the pages it came
from, with the hash of the snapshot (`source_refs`). Until now nothing looked at those pages again, so a
lesson could go on teaching words that its page no longer says.

Checking again is not free. It reads other people's websites. It can hold back a lesson the learner is
using. A new video costs money. And it touches the evidence rules: an answer given before the change
must stay as it was. That is why it is written down here.

## The options

What counts as a change:

1. **Any new hash.** Documentation sites change markup, links and whitespace often. Each of those would
   start a rewrite and cost model calls for nothing. Rejected.
2. **A change of words, as the quote check reads them** (chosen). A snapshot now also keeps the hash of
   its normalised text (`norm`: links reduced to their text, entities decoded, quotes and dashes made
   plain, formatting characters removed, whitespace collapsed, case folded). A new hash with the same
   `norm` is a formatting change. Studio lists it, and nothing else happens.
3. **A stored diff of every version.** More detail, but it keeps more copies of third-party pages and
   changes no decision. Rejected. A snapshot keeps the hashes and dates of up to 12 earlier versions,
   never their text.

What "still holds" means:

1. **Every recorded quote is still on the new page, word for word** (chosen). It is the same check
   (`quote_ok`) that let the quote in. It is exact and needs no model.
2. **The gate model judges every claim again.** It would catch a changed meaning around a quote that is
   still there. But it costs a model call per lesson and per change, and the rewrite goes through the
   gate anyway. Not now: see the review trigger.

What happens when a lesson no longer holds:

1. **Write it again and film it again, automatically.** Writing is cheap and already runs in the
   background. Filming costs about $1 per video minute at list price. Spending that without a person
   saying so is rejected.
2. **Write it again automatically; film it only when the owner presses a button** (chosen).

## The decision

**Detect.** `DriftCheck` in `app/drift.py` is a background thread, like the delivery check. It starts in
Azure, 15 minutes after the app, or locally with `DOJO_DRIFT=1`. It needs no model.

- It reads each cited page again at most once a day: every page a package lists for a skill, every page
  the newest lesson of a skill cites, and every page a lab cites. Only pages Dojo has read before (a page
  a lab cites is also read once to start from: see Labs), only on docs.github.com and
  learn.microsoft.com, and only through `Sources.recheck`. It follows redirects under ADR 0005's
  documentation rule, and stores a page only under its own address: see "Where a read may end".
- At most 400 reads a day, at least 3 minutes apart. A site that answers 429, 403 or 5xx is left alone
  for as long as its Retry-After asks: at least an hour, at most a day. The day's count is written
  before the read, so a restart does not reset it. (Until ADR 0008 this was 100 reads, 10 minutes
  apart: enough for the roughly 80 pages that the built-in exams and the labs cited, one page per
  skill. Deeper lessons let a skill cite up to three pages, up to about 300 for the built-in exams
  alone, and at 100 a day each page would be read only every three days, which would break the promise
  below. 400 reads a day, at most one every 3 minutes, keep it for up to 400 cited pages.)
- A page is gone after two "not there" answers at least 6 hours apart: not found (404 or 410), or moved
  to another page, in any mix. One is not enough.

**Where a read may end.** Candidates follow, citations do not. A cited address stores only its own page.
No page is ever stored under an address that names another page. Each read records where it ended after
redirects (`final`, ADR 0005).

- Two addresses name the same page (`same_page` in `app/sources.py`) when they have the same host, the
  same path and the same Learn `?view=`, in the form `docs_url` gives them. A first path segment that
  names a language (`de-de`, `zh-hant-tw`, `ja`) does not count, and neither does a trailing slash.
  Other query parameters are dropped, as `docs_url` drops them. Letter case counts: `/JSON-data-type`
  is another page. A GitHub Docs article address (`/api/article/body?pathname=...`) stands for the page
  it asks for.
- A read that ends at the same page counts as usual. In practice that is a redirect that adds or drops a
  trailing slash. Dojo reads documentation in English only (ADR 0005), so a redirect to the same page
  in another language is not followed. That read fails and is tried again the next day. It does not
  count towards "gone": the page did not move.
- A redirect anywhere else means the cited address has moved: to another documentation page, into
  training, or off the two sites. Nothing is stored under the cited address, and a page that is not
  documentation is not even requested. The read is written down as "moved", with where it led.
- A move counts as "not found" does. After the second one, 6 hours or more after the first, the page is
  gone, and its snapshot records where it went (`moved`). What was written from it needs re-check, as
  for any page that is gone: the lesson is held and written again from the skill's other pages (with
  none left, it waits), items and questions are held back, and nothing new is written from the page.
  Answers given earlier are still judged against the last good copy. Studio lists the change as "A
  source page moved", "moved to <path>" (the full address when it left the site). The lesson's notice
  says "moved to <path>" too; its list of sources and each quote from the page are marked "page moved".
  `/api/diag` counts `pages_gone` and, of those, `pages_moved`.
- When the address leads to its own page again, the next read stores it, and the page is neither gone
  nor moved.
- Citations do not follow. `Sources.get`, which reads a cited page when a lesson or question is written,
  follows the same rule. A page that now leads to another page gives its last good copy, marked stale;
  without one, the source is unavailable. This narrows ADR 0005 as it was first written, which followed
  a redirect from one documentation page to another and stored that page under the cited address. The
  cited page could then quietly become another page's text: quotes would be checked against the wrong
  page, and no check could see the move.
- Candidates follow. While Add an exam chooses a skill's pages (ADR 0005), a search hit or a study guide
  link is a candidate, not a citation yet. It is read with `Sources.candidate`, the only other reader.
  If the address leads to another documentation page, the candidate is the page it ends at: that page
  is stored under its own address, and if it passes the gate, it is cited there, never under the old
  address. Two candidates that end at the same page are one. A page on the other site is read again with
  that site's own reader, so its copy is what the check reads later. A candidate whose copy is marked
  gone is read again, never chosen from that copy. A redirect into training or off the two sites is
  refused, as for any read. Nothing is stored under the address asked for, so a citation still only ever
  holds its own page.

**Copies Dojo may use.** A snapshot is read as the page only when it was read under ADR 0005's rule,
from the cited page itself (`own_copy`): its `final` is documentation, and the same page. Everything that
reads or writes snapshots for the check respects this. `head` gives no version for any other copy, so
`compare`, `holds` and the classification have nothing to read from it, and `holds` checks the stored
copy again before it reads its text. `recheck` and `_keep` take history over only from such a copy.
`mark_gone` may mark any stored copy gone: a page that is not there is not there, whatever copy Dojo has.

- A copy stored before ADR 0005 has no `final`. One stored under ADR 0005's redirects may have another
  page's. Neither gives a version: `compare` says "same", `holds` finds no quote on it, and nothing is
  classified from it. It is read again in its turn, like any page that is due, and what was written from
  the page is judged from that read on. A version Dojo cannot place among the page's known versions is
  dated from that read. No history is taken over from such a copy.
- A page that is gone stays gone, whichever copy it has.
- Studio counts pages whose copy is not checked yet, and `/api/diag` has `pages_unchecked`.

**Added exams.** An exam added under ADR 0005 is checked like a built-in one: its lessons, items and
practice-exam questions, with the same limits. `cited()` and the Preparer go over every exam Dojo has.
Added exams have no labs. Removing an added exam removes what the check wrote down for it: its lessons
leave the changes found, a change to a page nothing cites any more is dropped, its films leave the list,
and a page nothing cites is no longer read. The owner's film approvals stay, as a record of money
approved. The learner's Record keeps every event.

**A hold is not a missing source.** A held lesson, item or question is held for now; the skill keeps its
official sources. What decides whether Dojo gives a timed practice exam (`untestable`, `blocker` and
`plan_skills` in `app/exam.py`) and the blueprint mark (ADR 0005) read the package's sources, never the
check. So a hold never makes a built-in exam look unsourced: the exam is not refused, every domain keeps
its share of the questions, and the mark does not change. A skill whose only page is gone still gets its
share. No new question can be written from that page, so those questions are lost, with the reason in
the quality log, and the exam comes out shorter (`dropped`). Readiness still asks for 5 scored questions
in each domain, so a shorter exam is never read in the learner's favour.

**The plan.** The plan (ADR 0004) links a practice, a check or a later check to the skill's page, where
a new question is written from the page as it is now. So a held item is never planned and never blocks
a session. Two things would lead nowhere, so they are not planned while they last:

- A skill whose every cited page is gone, moved included (`DriftCheck.gone_skills`). Nothing new can be
  written for it, no lesson and no question. It gets no session, and the 3-minute check does not pick it
  (`checks.pick`), as for a skill with no verified source (ADR 0005). Its lab stays planned: the database
  checks a lab, not a page. When a page is back, the next plan has the skill again.
- The Lesson or Listen of a held lesson. It is out of the podcast feed and is being written again.
  Practice and checks for the skill stay planned. Once the Preparer has written the new lesson, the plan
  has its Lesson or Listen.

A session already planned that one of these makes lead nowhere leaves the plan, even one the learner
pinned or moved, like a session of a removed exam (ADR 0004): for a skill whose every page is gone, every
session but its lab; for a held lesson, its Lesson and Listen. That is `plan.unavailable`, fed by
`Planner.needs` from the same `gone_skills` and held lessons that decide what is planned, and applied in
`plan.reconcile` before its other rules, missed or not. The removal is kept with the plan's moves, with
why, and "Moved for you" shows one line: "Removed", with "Its official pages are gone or moved" or "This
lesson is being written again". The calendar feed stops listing the session, as it does for every
session that leaves the plan. What was done stays done. A session of the learner's that is only no
longer asked for keeps its time, as ADR 0004 says. When a page answers again, or the new lesson is
there, the step is planned again as a new session, and its "Removed" line goes.

Unlike a timed exam, which keeps the held skill's share, the plan only leaves out what cannot be done
now. The plan is the learner's own data, never evidence: none of this writes to the Record.

**Classify.** When the words of a page change, every recorded quote of each lesson (the newest per
skill), item and practice-exam question that cites it is looked for in the new text.

- *Still holds*: every quote is there. Studio lists it. Nothing else happens.
- *Needs re-check*: a quote is gone, or the page is gone.
- Verdicts are not stored. They are worked out from the snapshots when asked, and cached per page
  version. A page that changes back makes a lesson hold again, and a later change is dated from when the
  words last moved away. A restart cannot leave a stale verdict behind. Sorting out a change fetches
  nothing, so a restart after a read finishes the job without reading the page again.

**Act on "needs re-check".**

- The lesson leaves the podcast feed (`Podcast._plan`, its episode file and the skill's episode link),
  as ADR 0002's review trigger asks.
- The course map, the skill page and every lesson tab show: "A source page changed on <date>. Dojo is
  re-checking this lesson." The lesson stays readable, with the quotes that are gone marked, so the
  learner is not left with nothing while it is rewritten. That is EG-20's "historical view, clearly
  marked". Viewing it records exposure, as before; exposure is never evidence.
- The Preparer writes the skill's lesson again before any other: author, gate, then narration, the
  ordinary path, from the page as it is now.
- A rewrite that cannot work is not tried. When every page the skill cites is gone, moved included
  (`DriftCheck.gone_skills`), nothing can be written from them, so the Preparer skips the skill
  (`Preparer.rewrites`): it uses none of the day's rewrites and calls no model. The lesson stays held
  and marked. In Studio it shows as "waiting", with "All its official pages are gone or moved; update
  the package's source.", and it is counted apart from the lessons waiting for a rewrite. Once one of its
  pages answers again, or its package cites another page, the skill is back in the queue in its usual
  place. So a site that moves many pages at once cannot use up the day's rewrites and starve the lessons
  that can be written again. The check can mark a skill's last page gone after the Preparer picked the
  skill and before the rewrite is counted. So the count looks again (`DriftCheck.charge_rewrite`): when
  every page the skill cites is gone by then, it counts nothing, and nothing is written. The look and
  the count hold the lock the check holds while it marks a page gone, so no page can go in between.
- Rewrites have a daily allowance: at most 10 a day (`DAILY_REWRITES`), counted per UTC day in
  `content/drift-state`, like the reads. A rewrite is counted before it starts, so one that fails still
  counts and a restart does not hand out a new allowance. Rewrites go in one fixed order
  (`Preparer.rewrites`): the learner's active exam first, then the other exams' lessons together, by
  most exam weight (ties by exam id, then skill id). The rest wait for the next day in the same order.
  Rewrites do not take turns between exams, and they do not move the turn that new lessons and
  narration take (ADR 0005), so the day's allowance never alternates between exams. Studio and
  `/api/diag` show it: "3 of 10 rewrites used today; 12 waiting. 2 lessons wait without using one: all
  their official pages are gone or moved; update the package's source." A skill with no lesson yet is
  prepared as before, and the learner's own "Write the lesson" is not limited: it is one lesson, asked
  for by a person.
- Nothing is filmed on its own. A rewritten lesson whose earlier version was filmed has no video yet:
  Watch says so and offers Present, Read and Listen. Studio shows "Re-film N changed lessons (about $X
  at list price)". The estimate uses the pace of Dojo's own videos (14 characters a second before there
  are any) and the list prices behind "What this costs". Under the button, as under every total (ADR
  0005), Studio says when those list prices were read, or that they could not be refreshed and may be
  out of date. Pressing it, and confirming, records the
  approval and files the films through the ordinary video job, one at a time, only while the owner has
  no job running. A film cut off by a restart is filed once more. The "Film this lesson" button on
  Watch stays: it is also an explicit action by the owner.
- An item not answered yet whose quote is gone is held back. Its hints and answers are refused (409)
  before anything is recorded, it is not issued again, and a 3-minute check shows it as skipped, with
  the reason.
- A quick-practice question not answered yet whose quote is gone is held back the same way. Answering it
  is refused (409) with the same notice, before anything is saved or recorded, and it shows no answer
  key. It is not counted in the result. A question answered before the change stays as it was. The
  export leaves out its answer key, rationale and quote too, with the notice in their place; its stem,
  options and source stay, and so does everything of an answered question.
- A practice-exam question whose quote is gone leaves the pool. An exam that has not started drops such
  questions when it starts, and its clock is worked out again; an exam with none left cannot start.
- A running exam is left alone: no answer is refused while it runs. When it closes, every question is
  checked against its page once more (`question_ok`). A question whose quote is gone is withdrawn:
  - It is not scored, and it is not counted in the totals or in the domain diagnosis.
  - The answer given stays visible. The answer key, rationale and quote are not shown, on the results
    page or in the export, as they may no longer be what the page says. The source link stays.
  - The results say: "N questions were withdrawn because their official source changed during the
    exam." When the exam closed because time ran out, the results say that a scored question left open
    counts as unanswered; a withdrawn question is not scored, so it is not a miss.
  - The Record gets no `exam.answered` event for it. `exam.closed` counts the scored questions only, and
    says how many were withdrawn.
  - Opening the results records `exam.rationales_shown`, which starts the teaching clock of the skills it
    lists. It lists only the skills of scored questions: a withdrawn question shows nothing, so it teaches
    nothing. When every question was withdrawn, the moment is still recorded, with no skills.
- Readiness rule 2 ("Should you book it?") counts scored questions only. When withdrawal leaves a domain
  with fewer than 5 questions, the attempt does not count for rule 2, and its result and the readiness
  page say why. Each question records, when the exam is built, whether it was new (`new`), and a start
  that drops questions reads that again with the count. A withdrawn question takes one off the new
  questions only if it was new. In an attempt built before questions recorded this, each withdrawn
  question is taken to have been new: a missing fact is never read in the learner's favour.

**The Record is never written by the check** (EG-34). An answer given before the change keeps its
result. The skill page adds a line to that attempt: the page changed on <date>, after this answer; take
a new check to show the skill against the page as it is now; studying the lesson again does not renew
the proof (M-06).

**Surface.** Studio's "Source changes" lists each change: the page, the date, the quotes no longer on
it, the lessons and labs it touched and what became of them, and how many items and practice-exam
questions are held back. Formatting-only changes are listed apart. `/api/diag` has a `drift_check`
block, like `delivery_check`.

**Labs.** The DP-800 labs (`app/labs.py`) cite Learn pages too. A lab has no quotes to look for, and
its steps are not words on the page, so no check can say that a lab still holds. A change can only
flag the lab for a person to look at.

- A page a lab cites that Dojo never read is read once, to start from, within the same limits and
  after the pages due again. The version Dojo first sees is what the lab is compared with. That first
  read flags nothing.
- From then on, a change in the page's words, or the page going away, flags every lab that cites it.
  A change in formatting only does not. The lab page says: "A source page changed on <date>. Dojo
  flagged this lab for a look: check its steps against the official docs." Its "Official docs" list
  marks the page. Studio lists the lab under the change, with an "I looked at it" button, and
  `/api/diag` counts flagged labs (`labs_flagged`).
- Pressing "I looked at it" (not during a practice exam) makes the pages as they are now the new
  starting point. The next change flags the lab again.
- The lab stays open. Holding it back like a lesson was rejected: a lab is practice and fills no pip,
  its checker tests the database and not the page, and only a person can tell whether its steps still
  work. Nothing is rewritten, filmed or recorded for a lab.

## What is kept, and what is not

- Snapshots (`sources/`): as before, plus the normalised hash, when this version was first seen,
  whether the page is gone and, when it moved, where to (`moved`), and the hashes and dates of up to 12
  earlier versions. Never earlier text. Where each read ended (`final`) is ADR 0005's.
- `content/drift-state`: the day's read count and rewrite count, the last rewrite, when each page was
  read and what came back (with where a moved page led), which sites asked Dojo to slow down, the changes
  found (page, kind, date, lessons and labs touched), the owner's film approvals (time, lessons,
  estimate) and the film jobs filed for them, and for each lab the hashes of the page versions it is
  compared with and when the owner last looked. Content only. What a change means for the learner's own
  items and questions is counted when Studio shows it, so deleting the record leaves nothing of them
  here (EG-26).
- The Record: untouched.

## Consequences

- About one extra request per cited page per day to two documentation sites, never more than 400 a day.
- Dojo notices a change within about a day, as long as no more than 400 pages are cited (Studio's
  source-drift card shows how many are). Beyond that, each page is read less often than daily. The date
  shown is when Dojo first saw the new version, as a UTC day; the site may have changed up to a day
  before.
- A lesson that lost a quote leaves the feed until its rewrite passes the gate. A podcast app may keep
  an episode it already downloaded.
- Model spend: one rewrite per lesson that lost a quote, the same as preparing a new one, and never more
  than 10 a day. A lesson whose every page is gone costs nothing until a page is back or its package
  cites another page. Video spend only after the button.
- Limits:
  - Only quotes are checked, not claims. A quote still on the page holds, even if a new sentence next
    to it changes what it means.
  - Ask answers are not checked again.
  - A question whose quote goes while a practice exam runs is still shown and can be answered. It is
    withdrawn only when the exam closes, so the time spent on it is lost.
  - A lab is compared with the version of its page that Dojo first saw. A change made between the
    lab's writing and that first read is not noticed.
  - A lab page that is never found has no version to compare with. It flags nothing; it is tried again
    6 hours later and then once a day, and is watched from the first version Dojo can read.
  - A page renamed by a redirect is gone to Dojo, even when the new page says the same. Its lessons are
    held until they can be written again from the skill's other pages. A skill whose only page moved
    stays held, using none of the day's rewrites, until its package cites the new page: a citation never
    follows on its own. Only choosing pages again follows it, as Add an exam does for candidates.
  - A cited page that now leads only to another language is never read, and never counts as gone.
    Nothing new is written from it (ADR 0005 reads English only), what was written from it is judged
    against the last good copy, and the page is tried again each day.

## Review trigger

- A lesson or question that holds by its quotes turns out to teach what its page no longer says: add a
  gate re-judgement of claims.
- A site complains or blocks Dojo: lower `DAILY_FETCHES` or raise `FETCH_GAP_S`.
- Lessons keep waiting for a rewrite day after day: raise `DAILY_REWRITES`.
- Cited pages move often (a documentation site renames a section): consider checking the quotes on the
  page an address now leads to, and offering it to the owner as the skill's new source.
- The owner wants films made without pressing the button: that needs a spending limit, never open-ended
  filming.
- Anyone other than the owner gets access to Dojo.
- At the latest after the exams, in March 2027.

## How to undo it

1. Remove `drift.start()` from the lifespan in `app/main.py`: no page is read again. Pages Dojo refreshes
   for other reasons still count, so also remove `dojo.drift = drift` to stop every hold-back and notice
   at once.
2. Delete `content/drift-state` on the data share: the reads, changes and approvals are forgotten. The
   extra snapshot fields do no harm and can stay.
3. Remove `app/drift.py`, its routes (`/api/studio/drift`, `/api/studio/drift/refilm`,
   `/api/studio/drift/labs/{lab}/looked`), the `drift` field of `/api/labs/{lab}`, the diag block, the
   hooks in `app/learning.py`, `app/exam.py`, `app/readiness.py`, `app/checks.py`, `app/plan.py`,
   `app/podcast.py`, `app/prepare.py` and `app/newexam.py`, the drift parts of `app/static/app.js` and
   `app/static/app.css`, and `tests/test_drift.py`. A closed exam keeps its `withdrawn` list; without the
   hooks it would be scored in full again, so keep the lines in `app/readiness.py` and `app/exam.py` that
   read it.
4. The same-page rule in `app/sources.py` (`same_page`, `own_copy`) can stay: on its own it only keeps
   another page's text out of a cited address. To go back to ADR 0005's first redirects, use `verified`
   where it uses `own_copy`, and drop its `same_page` checks in `_get`, `_keep` and `recheck`;
   `candidate` then reads as `get` does.
5. Nothing in the Record needs undoing: the check never wrote to it.

Tests: `tests/test_drift.py` (`SetupTests`, `FormattingTests`, `RemovedQuoteTests`, `VersionTests`,
`GoneTests`, `MovedTests`, `SamePageTests`, `PoliteTests`, `RestartTests`, `PoolAndCheckTests`,
`FilmTests`, `LabTests`, `QuickPracticeTests`, `ExamWithdrawalTests`, `RewriteAllowanceTests`,
`RewriteOrderTests`, `GoneRewriteTests`, `UncheckedCopyTests`, `HeldIsNotUnsourcedTests`, `PlanTests`) and
`tests/test_newexam.py` (`AddedExamDriftTests`, `RenamedPageTests`, and the redirect cases in
`DocumentationOnlyTests` and `UncheckedSnapshotTests`), with a mocked web, and the removal rule itself in
`tests/test_plan.py` (`ReconcileTests`).
