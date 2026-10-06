# ADR 0008 - Deeper lessons: more official pages per skill

- Status: accepted
- Date: 2026-10-01
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: accepted by the builder of the Deeper lessons feature under the owner's standing instruction
  to decide and build autonomously. The lead can reject or change it in review. Review triggers are below.
- Guardrails: EG-12 (record consequential decisions), EG-14 (a generated answer is not a verified Claim),
  EG-16 (exposure is not a Finding), EG-20 (re-gate or withhold cached media after relevant source drift),
  EG-37 (retrieved content is untrusted instruction), EG-39 (only documentation is copied), EG-43 (no new
  dependency), ADR 0005 (how a page becomes a skill's source) and ADR 0006 (what was written from a page
  is checked again when the page changes)

## The requirement

Measured on 29 September 2026: every lesson of the two built-in exams cites exactly one official page,
and half of them give fewer than 917 words to read. The exam tests the whole skill, and one page often
covers only part of it. Lessons should teach the whole skill from every official page that covers it,
found and checked by Dojo itself, with the checks every lesson already has: each point, misconception
and self-check question quotes its page word for word, and a second model judges every element.

That decides four things others must know: where pages found later live (the built-in packages are
files in git), what a search and a second lesson for every skill may cost and how fast they run, how a
lesson already written gives way to a deeper one without a gap, and how the drift check (ADR 0006)
keeps up with three times the pages.

## The options

1. **Add pages to the package files by hand.** 114 skills mapped by a person, a deploy for every change,
   and no check that a page teaches its skill. Rejected.
2. **Build the built-in exams again as added exams (ADR 0005).** Add an exam already maps up to three
   pages per skill, but an added exam's skills get new ids, so the owner's evidence and readiness credit
   would no longer attach to them. Rejected.
3. **Write longer lessons from the same page.** Longer is not deeper: the page still covers only part of
   the skill, and more points from it repeat it. Rejected.
4. **Search for more pages in the background with Add an exam's checks, keep them beside the package,
   write every new lesson on a deeper template from all of a skill's pages, and write the existing
   lessons again within a daily allowance** (chosen).

## The decision

**One way to find pages.** Add an exam's search, pick and check move from `app/newexam.py` into
`app/pagefinder.py` (`PageFinder`), which both features use. Add an exam behaves as before. For one group
of skills at a time, candidates come from the two sites' own search (Microsoft Learn and GitHub Docs).
The author picks by number, never more than the skill has room for (three pages in all). It is told which
page each skill cites already, never to pick that page again, and to prefer pages that teach the parts it
leaves out. The checker must copy a sentence from each picked page that shows the page teaches the skill,
and Dojo finds that sentence on the page word for word (`quote_ok`). Only documentation is read, never
training, not even through a redirect. A candidate that leads to another documentation page is judged
and cited under that page's own address (ADR 0005). A page the skill cites already, or one that ends at
the same page as another, is never added twice. Everything read from the web reaches a model only as
material, never as instruction (EG-37).

**Where the pages live.** What the search keeps is an overlay on the data share,
`content/package-sources/<exam>.json`, never in git. For each skill it holds:

- the skill's words when it was searched;
- when it was last tried and last searched, and how many candidates there were;
- the pages added. Each page has its address, title, publisher, licence (CC-BY-4.0) and `use: ground`.
  It also has the sentence the checker copied (with the checker, the date and the page's hash), when it
  was checked (`verified_at`), and the checker's reason.

`app/packages.py` merges the overlay into the built-in package when Dojo starts and whenever the overlay
changes. The package's own page stays the first source, and the pages found follow it. Every page is
checked again as it is merged: it must be official documentation in its canonical form, name its
publisher and licence, and have a copied sentence. No page appears twice, and a skill has at most three
pages. A page that fails is left out and logged. An overlay that cannot be read, or is not for that
exam, is logged and ignored, and the package is used as the repository has it: startup never fails
because of it. If a skill's words change in the repository, the pages found for its old words are
dropped. `by_id` is replaced, never changed in place, as adding and removing an exam do. Added exams
are not searched: they got their pages when they were added.

**What the pages change, and what they do not.** Sources are not part of the blueprint mark (ADR 0005),
so readiness credit survives. Every part of Dojo reads the merged package: lessons, items, checks, Ask,
practice exams, the podcast, the plan and the drift check. The skill page lists every page the skill
cites, and says which pages Dojo found and when it checked them. An answer is judged only from the pages
its item was written from, at the size it was written with, from the copies Dojo stores of them (Review,
round 1). So a page found later changes nothing for an item written before it.

**Pacing the search.** The search runs on its own thread, and only with real models: in Azure, or
locally with `DOJO_PREPARE=1`. It starts 10 minutes after startup. It searches one group of skills at a
time, at least 10 minutes apart, and only while the learner has no job queued or running (the
Preparer's rule). The active exam goes first, then the groups that weigh most.

A search is written down before any search or model call. So a crash or a restart cannot repeat it for
free: it is tried again a day later, like a search that failed. A skill where nothing new was found
keeps its one page and is searched again no sooner than 30 days later. The limit of three pages counts
the pages that are there, except the package's own page, which always keeps its place. A skill with two
pages that are there is not searched again unless a page goes, and a skill with three is never
searched. When a page a search added goes (ADR 0006), the skill is searched once straight away for a
page to take its place, and then every 30 days while the gone page stays listed (Review, round 1). The
first pass over the two built-in exams is 25 groups, about four hours of quiet time.

**A deeper template (2) for every new lesson**, for built-in and added exams alike (`LESSON_TEMPLATE`
and `_lesson_prompt` in `app/learning.py`). A lesson has:

- 4-6 sections of 3-5 points;
- 2-4 misconceptions;
- a worked example whose steps say what to do;
- 4-5 self-check questions;
- 6-9 slides.

With more than one page, the author is told to teach the whole skill from all of them, and to cite every
page that covers the skill at least once. Every point, misconception and self-check question keeps its
word-for-word quote, checked by `quote_ok`. The gate judges every element, exactly as before, and what
fails is removed and logged.

A lesson records `template: 2` and three lists of pages:

- `sources`: the pages it read;
- `cited`: the pages its kept elements quote;
- `offered`: the pages the skill cited when it was written.

The newest lesson of a skill is the one the learner sees.

**How much of each page the models see.** Pages are cut to the parts most related to the skill. These
sizes were measured with the real prompts on page-sized text (4 characters is about one token):

| Pages x characters each | Lesson prompt | Checking 110 elements (3 calls) |
| --- | --- | --- |
| 1 x 18,000 (as before) | about 5,050 tokens | about 20,100 tokens |
| 2 x 12,000 (as before) | about 6,600 | about 24,700 |
| 3 x 10,000 (chosen) | about 8,100 | about 29,200 |
| 3 x 12,000 | about 9,600 | about 33,700 |

The chosen sizes (`GROUNDING_CHARS`) are 18,000 characters for one page, 12,000 each for two and
10,000 each for three. A median page, about 15,000 characters, still fits whole when it is the only one.
Three pages make the lesson prompt about a quarter longer than two do; 12,000 characters each would make
it nearly half as long again.

The same sizes ground items, checks and Ask. Practice-exam questions are written for several skills in
one call, so they get 6,000, 4,000 and 3,000 characters per page (`MCQ_CHARS`); before, every page got
6,000 whatever the number. An item written from three pages reads about 3,000 more tokens than one
written from one page, about $0.015 at list price.

**Writing the existing lessons again.** The Preparer (`app/prepare.py`) does this one lesson at a time,
in this order:

1. the drift check's rewrites;
2. missing lessons;
3. upgrades;
4. narration.

A skill's newest lesson is upgraded in either of two cases:

- it is on template 1, or has no template;
- its package now cites a page, not gone, that the skill did not cite when the lesson was written.

The active exam goes first, then exam weight. A built-in skill waits for its search, so its lesson is
written once, from all its pages; a skill that already has three pages does not wait. A skill of an
added exam never waits.

- **Its own allowance.** At most 20 upgrades a UTC day, counted in `content/deeper-state` so a restart
  does not reset the count. Upgrades never use the drift check's 10 rewrites a day, and the drift check
  never uses theirs.
- **Held skills.** A skill the drift check holds, or whose every page is gone, is left to the drift
  check, and nothing is counted. This is decided under the drift check's own lock, so a page that goes
  at that moment cannot slip through.
- **Written down first.** Each attempt is counted and written down before any model is called: the
  day's count, the attempts at that lesson and when, and the work counted for "spent so far". A crash,
  an error or a restart gets no free retry.
- **No gap.** The lesson the learner sees stays the earlier one until the new one passes every check.
  The new one must hold as well as a first lesson must: three quarters of what was written kept, and at
  least two sections. It must also keep at least as many checked points as the lesson it replaces.
  Otherwise nothing is written and the earlier lesson stays.
- **Unreadable pages.** If a page the skill cites cannot be read just now, and is not gone, the upgrade
  waits for it. The attempt counts, but no model is called.
- **Failures.** A failed attempt keeps the earlier lesson and is tried again no sooner than 24 hours
  later. After 3 failed attempts it is given up, and Studio shows it with the reason. A newer lesson
  starts again.
- **Narration.** The new lesson is narrated for Listen and Present as soon as it is written, like any
  new lesson. If that fails, the narration pass fills it in later. The voice check (`app/delivery.py`)
  then transcribes the new narration once, as it does for every new narration.
- **Deeper Listen (ADR 0007).** Its pass skips a lesson that will be written again: one queued now,
  or waiting for its skill's search, a failed attempt's wait or the next day's allowance. One given up
  is not skipped. So the whole-lesson narration is written, and paid for, once: for the new lesson.
  Listen says so for such a lesson, and Studio's Deeper Listen line counts them. An upgrade retires the
  Deeper Listen of the lesson it replaces, as a rewrite after a page change does: it no longer plays,
  and its record, with what it cost, stays. A lesson that already played one before this feature reached
  it gets a second one for its new lesson, and the estimate to finish counts those.

**Watch.** A presenter video is never filmed on its own. Watch keeps the video of the earlier lesson
and says plainly that it was filmed from an earlier version: the lesson was written again on a given
date to teach the whole skill from more pages, and Read, Listen and Present give the new one. The slides
and sources shown beside the video are the earlier version's. If a page change no longer supports that
earlier version, the video is not shown (EG-20). The new version is filmed only through Studio's
existing "Film again" button, which shows the list price first (ADR 0006).

**The drift check keeps up.** Pages found are read again exactly like cited pages: snapshots, the
verified copy, a page that moves counts as gone, a lost quote holds the lesson, and a skill whose every
page is gone waits. With up to three pages per skill, the built-in exams can cite about 300 pages
instead of about 80. At 100 reads a day each page would be read only every three days, which breaks ADR
0006's promise of about a day. So the budget is now 400 reads a day, at most one every 3 minutes, and
ADR 0006 says so. A page found that went gone and was replaced leaves the overlay, and the drift check
forgets what it wrote down for it once nothing cites it (Review, round 1).

**Studio and diagnostics.** Studio's "Deeper lessons" card and the `deeper_lessons` block of
`/api/diag` show, per exam:

- the skills with two or more pages;
- the lessons on template 2;
- what waits: to write again, waiting for its search, or given up;
- today's allowance and the last failures;
- what was spent so far and what is left, at list price, with the price age and the assumptions shown
  as the other estimates show them (`deeper_estimate` in `app/costs.py`).

**The Record.** Nothing changes in it. Pages found and lessons written again change what Dojo teaches,
never what it has recorded. Reading or hearing a lesson is exposure, and never evidence (EG-16).

## Money

These figures use list prices (Global Standard in swedencentral):

- gpt-5.5: $5 per million input tokens and $30 per million output tokens;
- Grok 4.1 Fast: $0.20 and $0.50;
- the neural voice: $15 per million characters;
- transcription for the voice check: $0.36 an hour.

The token counts are assumptions, because Dojo does not count model tokens yet:

- **Searching, per skill.** The author's pick takes 900 input and 800 output tokens, and the checker
  3,500 and 1,000. That is about $0.03 a skill, even when nothing new is found.
- **A lesson on template 2.** Writing takes 10,000 input and 10,000 output tokens: three page excerpts
  in, and about 20 points, 5 questions and 8 slides out, with the author's reasoning, which is billed as
  output. Checking takes 31,000 and 9,000: 110 elements, in three calls that each see the pages. Both
  count 1.5 times on average, because a draft that fails is written again. Slide narration counts 3,000
  characters until three lessons are narrated, then the average so far, and the voice check transcribes
  it once, at 14 characters a second. That is about $0.61 a lesson. An attempt that fails is counted at
  its writing and checking, about $0.54.
- **The two built-in exams, 114 skills: about $73.** Searching is $3.39, writing $59.85, checking
  $1.83, narration $5.13 and its voice check $2.44. At 20 lessons a day this is spread over about six
  days, about $12 a day; the searches come on top, mostly on the first day.
- **Add an exam** uses the same writing and checking figures, so a course for an added exam costs about
  $0.15 a skill more than before (20 skills: $12.31 instead of $9.38). Its estimate is otherwise
  unchanged.
- **Not in this total:** Deeper Listen's whole-lesson narration (ADR 0007) and filming again. Each is
  priced and capped by its own feature. A deeper lesson leaves Deeper Listen's cost about where ADR 0007
  put it. Measured with the real prompts, a template-2 lesson of about 20 points on three pages gives
  Deeper Listen a writing prompt of about 9,900 tokens and a check of about 19,300 (a template-1 lesson
  on one page: 6,100 and 13,200), against ADR 0007's assumed 10,000 and 23,000; its outline keeps it at
  about 1,000 words whatever the lesson's length. The one exception is counted in the estimate to
  finish: a lesson that already plays a Deeper Listen when it is written again needs a second one,
  about $0.49 each by ADR 0007's assumptions. Deeper Listen waits for every lesson that will be written
  again, so this is only the lessons it narrated before this feature reached them: at most 20 a day
  since ADR 0007 went live.

## Defaults the owner may change

- In `app/deeper.py`:
  - `DAILY_UPGRADES`: 20 a day;
  - `UPGRADE_WAIT_H`: 24 hours;
  - `UPGRADE_TRIES`: 3;
  - `SEARCH_GAP_S`: 10 minutes;
  - `SEARCH_AGAIN_DAYS`: 30;
  - `SEARCH_RETRY_H`: 24 hours.
- In `app/packages.py`: `MAX_SOURCES`, 3 pages per skill.
- In `app/learning.py`: `GROUNDING_CHARS` and `MCQ_CHARS`.
- In `app/drift.py`: `DAILY_FETCHES`, 400, and `FETCH_GAP_S`, 3 minutes.

## Review, round 1

The first review of the pull request (#32) found two faults. Both are fixed:

1. **An answer could be judged against the wrong page.** When the package no longer cited any of the
   pages an item was written from (a page Dojo found was removed, or replaced, before the answer was
   judged), grading fell back to every page the skill cites now. So the grader could see the
   repository's page instead of the page the rubric came from. Now an answer is judged only from its
   item's own pages (`Dojo.item_grounding`). Each keeps the number the item gave it and is read from the
   copy Dojo stores (`Sources.stored`), without reading the page again, whether it is gone, moved or no
   longer cited. Each item now records the size each page was cut to (`excerpt_chars`), so the grader
   sees the excerpts the item was written from; an item written before this feature records none and
   is cut as Dojo cut pages then: 18,000 characters for one page, 12,000 each for more. When Dojo holds
   no copy it may use of any of the item's pages, the answer waits to be judged and the job says why; no
   other page stands in. Judging an answer is the only place Dojo grades with pages: practice-exam and
   quick-practice questions are scored by their stored key, and the drift check judges a question by
   its own page.
2. **A page found that went gone was never replaced while two pages remained.** A three-page skill
   whose third page went gone was not searched again, because a skill with two pages was not searched.
   Now a skill with a page a search added that is gone gets one search for a replacement straight away,
   without the 30-day wait (only the day's wait after any try). The limit of three pages counts the
   pages that are there: the package's own page keeps its place, gone or not, and a page a search added
   keeps its place while it is there. Each page the search finds takes the place of one gone page that
   a search added, the one gone longest first, and that page leaves the overlay. The drift check
   forgets what it wrote down for it (its reads, and the changes found on it) once nothing cites it, as
   for the pages of a removed exam; while the newest lesson still cites it, that lesson stays held and
   is written again first. The copy Dojo stores of the page stays, so an answer to an item written from
   it is still judged from that copy (fix 1). A gone page that no search replaces stays listed, the
   drift check still reads it in case it comes back, and the skill is searched again 30 days later. A
   lesson waits to be written again until the replacement search has run, so it is written, and paid
   for, once. Studio and diagnostics count the skills waiting for such a search (`replacing`).

   Before this fix, a search that finished also dropped every gone page a search had added, even when it
   found nothing to take its place. Now a gone page leaves only when a page takes its place.

## Consequences

- Lessons teach from up to three pages, and are longer. A deeper lesson costs about a third more than an
  earlier one. A deeper lesson the gate cannot keep as well as the old one is not shown.
- For about a week the built-in exams have lessons on both templates, and Studio shows how far along it
  is.
- The two documentation sites get a few requests per skill searched: one or two searches and the pages
  picked. They also get up to about 230 more page reads a day from the drift check.
- An item answered after its skill got more pages is still judged from its own pages, from the copies
  Dojo stores of them, even once the package no longer cites one.
- Some skills will keep one page, because the sites' search finds nothing better or the checker rejects
  what it finds. Studio shows this; it is never hidden.

## Review triggers

- The gate keeps rejecting deeper lessons (Studio's failures), or the kept share falls well below the
  earlier template's.
- The list-price estimates drift far from the bill in Azure Cost Management.
- A site complains about or blocks Dojo's searches or reads.
- Pages found turn out to teach something other than the skill: raise the checker's bar, or let the
  owner remove a page.
- At the latest after the exams, in March 2027.

## How to undo it

1. In `app/main.py`, remove `deeper.start()` from the lifespan, and remove `preparer.deeper = deeper`
   and `dojo.deeper = deeper`. There are then no more searches and no more upgrades, and Deeper Listen
   no longer waits for one.
2. Delete `content/package-sources/` and `content/deeper-state` on the data share. The built-in
   packages are then the repository's again. Lessons already written stay, and the newest is still
   shown.
3. To go back to the earlier template, set `LESSON_TEMPLATE` to 1 and restore the earlier
   `_lesson_prompt`. Lessons on template 2 stay valid and are shown until they are written again.
4. Once fewer pages are cited, set `DAILY_FETCHES` back to 100 and `FETCH_GAP_S` to 600 in
   `app/drift.py`.
5. Nothing in the Record needs undoing.

Tests:

- `tests/test_deeper.py`, with a mocked web and stub models: `OverlayTests`, `SearchTests`,
  `ReplacementTests`, `TemplateTests`, `CostTests`, `StudioTests`, `UpgradeTests`,
  `FoundPageDriftTests`, `ReplacedPageTests`, `GradingTests`, `EarlierFilmTests` and
  `DeeperListenTests`.
- Add an exam's own tests in `tests/test_newexam.py`, unchanged except for the estimate's figures.
