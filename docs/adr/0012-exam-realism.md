# ADR 0012 - The practice exam feels like the real one: item types, sections, clock and the open-book rule

- Status: proposed
- Date: 2026-10-01
- Decision owner: the Dojo lead, for the owner. The owner decides the open questions at the end.
- Guardrails: EG-14 (a generated answer is not a verified claim), EG-37 (retrieved content is untrusted),
  EG-39 (no real exam content: Dojo-written questions only), the Interaction-to-Evidence contract
  (multiple choice never fills a pip; no pass prediction; no 700-scale mapping; mode and permitted
  resources are recorded), DESIGN.md screens 06, 07 and 14, ADR 0006 (source drift).

## The requirement

Until now every practice-exam question was a single-choice item with four options. The real Microsoft
and GitHub exams look different: several item types, sections you cannot go back to, a clock, and on
associate and expert exams the Microsoft Learn site open beside the exam. A learner who only ever
practised single choice meets these for the first time on exam day. This ADR records what Dojo copies
from the real exam, what it does not, and why.

## What the official pages say (checked 2026-10-01)

From [Exam duration and exam experience](https://learn.microsoft.com/en-us/credentials/support/exam-duration-exam-experience)
(page updated 2026-06-04):

- Durations: Fundamentals 45 minutes, associate and expert role-based exams 100 minutes, 120 minutes
  when the exam may contain labs; "Most Microsoft Certification exams typically contain between 40-60
  questions."
- Question types: "we don't identify specific exam formats or question types before the exam". The
  exam sandbox shows: active screen, build list, case studies, drag and drop, hot area, multiple choice,
  labs, mark for review, the review screen, navigation and the timer. The sandbox is the interface
  reference (aka.ms/examdemo). Microsoft does not promise any mix.
- Breaks: "Once a break is launched, you will not be able to return to the questions that you viewed
  before the break." No break within the problem-solution question sets (the repeated-scenario yes/no
  items).
- Microsoft Learn: "You can access Microsoft Learn as you complete your associate or expert exam. NOTE
  that access to Learn is NOT available on Fundamentals exams or GitHub exams." Everything on
  learn.microsoft.com except Q&A, practice assessments and your profile. "Extra time has not been
  added." "The exam timer will continue as you explore Microsoft Learn content."

From [Exam scoring and score reports](https://learn.microsoft.com/en-us/credentials/certifications/exam-scoring-reports)
(page updated 2025-05-27):

- Scores run from 1 to 1,000 and 700 passes; a passing score "may not equal 70% of the points".
- "When answering most multi-part questions, you'll receive one point for each correctly answered
  component. You can earn all, some, or none of the points possible for that question. If a question is
  worth more than one point, it will be noted in the question."
- "There's no penalty for guessing... No points are deducted for incorrect answers."

From the certification pages:

- [DP-800](https://learn.microsoft.com/en-us/credentials/certifications/developing-ai-enabled-database-solutions/):
  "Microsoft Certified: SQL AI Developer Associate", shown with the associate badge; "You will have 120
  minutes"; proctored; "may have interactive components". **DP-800 is an associate role-based exam, so
  the real exam allows Microsoft Learn.**
- [GH-300](https://learn.microsoft.com/en-us/credentials/certifications/github-copilot/) (and the other
  GitHub exams, GH-100/200/500/900): 100 minutes, proctored, interactive components, Pearson VUE, its own
  sandbox (GHCertDemo.starttest.com). **GitHub exams are closed-book.**

Not stated in words on any page: that a case-study section cannot be entered again once you leave it.
The sandbox's own instruction screens say so, and the page's break and problem-solution rules point the
same way. Dojo adopts it as the stricter reading.

## Decision

### 1. Item types

Dojo writes seven kinds. Each one is Dojo's own question, written from the skill's official pages:

| Kind | What the learner does | Questions | Points |
|---|---|---|---|
| `single` (as before) | choose one of four | 1 | 1 |
| `multi` "Choose two" | choose N of five; the page stops a choice beyond N | 1 | N, one per correct choice |
| `sequence` (build list) | put the steps that belong in order; a step that does not belong stays out | 1 | 1, all or nothing |
| `match` (drag and drop) | give each target its value; a value may be used once, more than once or not at all | 1 | one per target |
| `hotarea` | choose a value in each drop-down placed in a statement or code sample | 1 | one per drop-down |
| `yesno` series | one scenario, three proposed solutions; Yes or No for each, one at a time | 3 | one per statement |
| `case` study | a scenario with exhibit tabs and four questions (single or "choose two") | 4 | per question, by its kind |

Not built: labs and active screen. Both need a live environment or a simulated product screen that
Dojo cannot verify against a page word for word. The DP-800 lab sandbox (ADR 0003) stays where it is.

**The mix.** Microsoft promises no mix, so Dojo picks one that lets a learner meet every type the
sandbox shows without making the exam a puzzle book. Single choice stays the majority because the
sandbox and the score page both treat it as the common case. Per question of the exam:

- multiple answers 10%, sequence 6%, matching 6%, hot area 6% (each at least one in a short exam)
- one yes/no series of 3 statements when the exam has at least 12 questions
- one case study of 4 questions when the exam has at least 30 questions (it needs reading time; a short
  exam stays a sampler)
- the rest single choice: 29 of 50 in a full exam (58%), 7 of 15 in a short one.

Skills are still planned by blueprint weight (`plan_skills`); a series or a case study takes its slots
from one domain, so the domain shares hold.

**Old clients and old attempts.** `POST /api/exams` keeps single choice when no `style` is sent, so a
page loaded before the deploy only gets questions it can draw. The new page sends `style: "exam"`.
Attempts and pool questions without a `kind` are single choice and render and score exactly as before.

### 2. Every item stays Dojo-written and verified

Every part of an answer key carries its own quote, and every quote must appear word for word in the
excerpt both models saw and in the stored page (`quote_ok`, as for single choice):

- `multi`: one quote for each correct option;
- `sequence`: one quote for each step in the correct order (the gate also checks that the quotes
  support the order, not only the steps);
- `match`: one quote for each target and its value;
- `hotarea`: one quote for each drop-down's correct value;
- `yesno`: one quote for each statement, whether its answer is Yes or No;
- `case`: one quote per key component of each question, by that question's kind. The exhibits are a
  fictional company written by Dojo; a product fact in them must be supported like any other.

An item where any quote fails, or where the gate model does not say it holds, is dropped and logged in
the quality log, never shown. The gate sees every key component with its quote and is asked whether
each holds; one that does not drops the whole item. Exposure and freshness stay per learner: a series
or a case study is one exposure unit (its key is the scenario), seen when the exam starts, and a
question whose quotes left their page (ADR 0006) is held back or withdrawn exactly as before - the drift
check now reads every quote of an item, across every page it cites.

### 3. Exam session

- **Duration**: the stated duration from the certification page first (as now). Only when the page
  states none and Dojo knows the level from the certification page does the exam-duration page's
  typical time apply: 45 minutes for fundamentals, 100 for associate and expert (Dojo cannot know whether
  labs appear, and it writes none). With neither, there is still no timed exam: a missing fact is not
  filled in.
- **Clock** visible all the time, kept by the server (as now); it keeps running in Microsoft Learn.
- **Flags and the review screen** as now, for the main section.
- **Sections and locks** (enforced by the server, not only the page): the main section can be reviewed
  until the end. A yes/no series and a case study are sections of their own. The learner chooses when to
  enter one; once they leave it - by going anywhere else - it is locked: no answer, no flag. Each yes/no
  statement is locked as soon as it is answered. The page warns before every lock. Microsoft publishes
  no fixed section order, so Dojo does not invent one.
- **Scoring**: one point per correctly answered component, no points taken off for a wrong one, the
  points shown in the question, and partial credit exactly where the score page allows it (multi-part
  questions). A sequence is all or nothing: the score page does not say how a build list is scored, and
  "partly in order" is not a component.

### 4. Evidence rules

- New selected-response types never fill a pip. `exam.answered` stays rehearsal-only in
  `app/evidence.py`; its `correct` means full points, and it now carries `kind`, `points` and
  `max_points`.
- Results stay a per-domain diagnosis: points earned of points possible per domain, the misses by skill,
  where to go next. No pass prediction, no 700-scale mapping; the score page's own words ("may not equal
  70%") are why.
- Readiness rule 2 reads points through the same scoring function, so an old single-choice attempt
  scores exactly as before.

### 5. Open book: Microsoft Learn allowed

Only for Microsoft associate and expert exams, because only those allow it. The level comes from
`exam_format.level` in the package: DP-800 is `associate`; an added exam gets it from the badge or the
certification name on its certification page. GitHub, fundamentals, Office specialist and unknown levels
are always closed-book, and the page says why in one sentence.

With "Microsoft Learn allowed", a button opens learn.microsoft.com in a separate window (Dojo cannot embed
it). The clock keeps running, as in the real exam. Dojo counts each lookup (a press of the button) and
the time the exam page is out of view (Page Visibility API), and the results say so: "6 minutes in Learn
for 3 lookups". The attempt records the mode as "open book, Microsoft Learn allowed", the counts, and
that what was read there is unknown to Dojo. Because the real exam allows the same, an open-book attempt
still counts as exam conditions for readiness rule 2; a closed-book attempt on an associate exam is
stricter than the real one and counts too.

### 6. Accessible and phone-friendly

Every type works with the keyboard and a screen reader: checkboxes for "choose N" (with a live count),
selects for matching and hot areas, move up and move down buttons for a sequence (no drag needed),
radio groups for yes/no, a tab list for exhibits. Everything fits at 390 px. No inline styles or scripts
(the strict CSP stays).

### 7. Cost

New types use the same author (gpt-5.5) and gate (grok-4-1-fast-reasoning) models. At the list prices of
2026-10-01 (gpt-5.5 short context $5 input and $30 output per million tokens; Grok 4.1 $0.20 and $0.50),
with the token assumptions in `app/costs.py` (`ITEM_CALLS`) and 1.25 rounds because dropped items are
paid for too:

- 100 single-choice questions: about $5.22.
- 100 questions in the exam mix: about $5.75, so **about $0.53 more per 100 questions (+10%)**.
- Per 100 items of one kind: multi or sequence $6.80, matching or hot area $7.56, yes/no $3.70 (per
  statement), case study $6.09 (per question).

The pool keeps its pace and limits: the same target of 24 questions per exam, now shared over the kinds
by the mix, one author and one gate call per batch, at most 12 batches a day. Studio shows the mix and
the estimate.

### 8. Multi-user

All new state lives in the attempt under `learners/<learner_key>/exams/...` and the pool under
`learners/<learner_key>/pool/...`; every new function takes the `Learner`. Nothing new is bound to the
single owner.

## Consequences

- A full exam takes longer to write: series and case studies are written one per call.
- A learner can still see the same official facts tested in several types; exposure is per unit.
- The diagnosis becomes points-based. For single-choice attempts nothing changes.

## Review triggers

- Microsoft publishes a mix, an item-type change, or new scoring rules on either page.
- The Learn rule changes (for example Learn on fundamentals or GitHub exams).
- Drop rates for a kind stay above 50% in the quality log for a week: the prompt or the kind needs work.

## For the owner

- Is the mix right (58% single choice, one series, one case study in a full exam)?
- Should an open-book attempt count for readiness rule 2 on associate exams, as decided here?
- Labs and active screen are not built; say if you want a separate ADR for them.
