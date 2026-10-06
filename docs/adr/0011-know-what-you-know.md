# ADR 0011 - Know what you know: confidence, calibration and successive relearning

- Status: proposed
- Date: 2026-10-02
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder of the "Know what you know" feature (branch `feature/know`). The lead
  can reject or change it in review. Review triggers are below.
- Guardrails: I2E contract (a self-report is never a Finding; multiple choice never fills a pip; a missing
  event is never read in the learner's favour; no pass prediction), vision INV-1 (self-reports stay
  self-reports) and section II.6 (shame is the dropout mechanism; no punished streaks), EG-12 (record
  consequential decisions), EG-24 (test with two synthetic learners), ADR 0004 (the Plan), ADR 0005 and
  ADR 0006 (skills with no page, or whose pages are gone, get no question)

## The requirement

Two techniques have the strongest evidence for long-term learning: practice testing and distributed
practice (Dunlosky et al. 2013, *Psychological Science in the Public Interest* 14(1)). Successive
relearning combines them. The learner retrieves a fact until it is right, then retrieves it again on later
days. It gave much better retention than learning in one session (Rawson, Dunlosky and colleagues, for
example doi 10.1037/xap0000146; Rawson and Dunlosky 2011, doi 10.1037/a0023956). Recall beats recognition
(Rowland 2014, doi 10.1037/a0037559; Adesope et al. 2017).

People also misjudge what they know. Asking how sure they were *after* they commit an answer, and showing
accuracy next to confidence, is a useful formative check. The errors made with confidence are the ones to
repair first (doi 10.7899/jce-12-018). Microsoft exams do not penalise wrong answers, so confidence must
never change a score.

Dojo already has the evidence pips, later checks and the 3-minute spoken check. It does not ask how sure
the learner was, and nothing brings a skill back after it has been shown later. This ADR records how Dojo
asks for confidence, shows it, and schedules recalls, and what none of it may do.

What we do not claim: a 2025 randomised trial of voluntary mobile flashcards found no significant
difference after six months, with heavy attrition. A separate drill that nobody opens does not help. So
recalls are woven into Today, the Plan and the spoken check, and they never become a wall of overdue work.

## The options

1. **A separate flashcard deck.** Easy to build, but it is a drill nobody opens (see above), it is
   recognition rather than recall, and its cards are not checked against the pages. Rejected.
2. **Ship FSRS now.** FSRS is the best open scheduler for flashcard histories. Its benchmark predicts recall
   of cards, not exam outcomes, and it needs review logs Dojo does not have yet. Rejected for now. The logs
   start here (see "Logs for a later scheduler").
3. **Fixed expanding intervals, derived from the Record, plus confidence on every committed answer.**
   Chosen.

## The decision

### 1. Confidence after committing an answer

- **Where.** Practice items, probes and checks (the item page), the 3-minute spoken check, the untimed
  question-by-question rehearsal, and the lesson's "Check yourself" questions on the Read tab.
- **When.** After the learner commits and before any feedback. On an item, the answer box locks when
  Submit is confirmed. Then "How sure were you?" appears with **Guessing**, **Fairly sure**, **Certain** and
  **Skip**. Only then is the answer sent. A spoken-check answer gets no feedback on the spot anyway.
- **Not in timed practice exams.** They stay exam-like.
- **Stored with the attempt, append-only.** One optional field, `confidence`, in the data of the existing
  `answer.submitted` and `mcq.answered` events: `"guess"`, `"fair"`, `"certain"`, or `null` when skipped.
  No new event type. Nothing earlier is rewritten. The hash chain is untouched.
- **What it never does.** It never fills a pip, never changes grading (the grader prompt does not see it),
  and never feeds readiness, the suggestions or the plan. `evidence.derive` does not read it. Tests prove
  that the same answers with and without confidence give the same evidence, grading prompt, readiness
  and exam scores.
- **Lesson self-check (Read tab).** "Answer in your head, then say how sure you are" opens the answer.
  Then "Did your answer match?" (**Yes**, **Partly**, **No**). Both answers are a self-report. They go to
  `learners/<key>/selfchecks.jsonl`, not the Record, because the learner marks them and no one judges
  them (vision INV-1). They appear in the calibration view as their own line ("you marked these
  yourself"), never mixed with judged answers. The Watch warm-up and pause are unchanged.

### 2. Calibration view: "Know what you know"

- **Where.** The Record (screen 09) gets a "Know what you know" panel: one row for the exam, one per
  domain, and the selected skill's own row in the skill detail. Readiness (screen 14) gets one line that
  links there and says it is not used in the advice.
- **The numbers.** For each confidence level: how many answers were right out of how many. Right means
  every rubric point met for an open answer, or the correct option for a rehearsal question. Disputed
  judgements count neither way. Skipped confidence is left out.
- **Too few answers.** A level shows a rate only with **at least 5 answers**. Below that it says "too few
  answers yet" and the count. Five is the smallest count at which one answer moves the rate by no more
  than 20 points. Even so, the rate is a mirror, not a measurement, and the page says so.
- **Confident errors.** Wrong answers marked Certain (first) or Fairly sure, newest first, at most 10,
  under the heading "Sure, but not right yet: worth another look". An error leaves the list once a later
  answer on the same skill is right: any right answer counts here, also one whose confidence was skipped,
  as skipping keeps an answer out of the rates only. A disputed answer counts neither way. Each row links to the lesson section that teaches the point that
  was missed, and has a "Try a new question" button.
  - The section is the one whose quotes share the most words with the quotes of the rubric points that
    were not met, or the rehearsal question's quote. When nothing matches, the link opens the lesson.
- **Words.** Kind, never ranking the learner: "Sure, but not right yet". No red, no score, no streak.

### 3. Successive relearning

All of it is derived from the Record each time, by a pure function (`app/relearn.py`). There is no stored
schedule to drift out of step with the events. The intervals are **scheduling**: when Dojo asks again.
They are never evidence. A recall answer is an ordinary check item. It fills a pip only under the existing
rules, exactly as any other check does (I2E A5 stays intact).

- **The ladder.** 1, 3, 7, 16, 35 days, counted in calendar days in the Plan's zone (Europe/Berlin).
  The server says whether a recall is due today in that zone; the browser never works it out from its
  own clock.
  - 1 day: the first recall comes on the next day. This matches the "later" condition (20 hours or more
    after teaching) and the first relearning session in the successive relearning studies.
  - Then roughly ×2.2 each step. That is the expanding pattern Leitner boxes and SM-2 style schedulers
    (default ease 2.5) use, and close to the early intervals FSRS gives at 90% target retention.
  - Cepeda et al. 2008 (doi 10.1111/j.1467-9280.2008.02209.x): the best gap is about 10 to 20 percent of
    how long you must remember. For an exam 4 to 10 weeks away, gaps of 7 to 16 days are in range.
  - The ladder stops growing at 35 days. Exam preparation is rarely longer than that, so a longer gap would
    let a skill drift before the exam.
- **Success.** A judged open answer with no help (probe or check), every point met, not disputed.
- **Miss.** Any judged open answer (probe, practice or check) with a point not met. A disputed miss still
  counts as a miss for scheduling. A dispute never moves a skill further away (a missing or disputed
  event is never read in the learner's favour). It only makes the skill come back sooner.
- **Rules, in order of time:**
  1. Before the first success, a skill has no schedule. Today and the Plan already lead it through teaching
     and practice.
  2. The first success starts the schedule: step 0, due the next day.
  3. A success on or after the due day moves one step up the ladder. The next recall is due that many days
     later. At the top it stays at 35 days.
  4. A success before the due day is kept in the Record. It does not move the schedule. So at most one
     step a day, and cramming does not count as spacing.
  5. A miss, once the schedule has started, resets it: step 0, due the next day, with a pointer to the
     lesson section to repair it first.
- **Relearned.** Three on-time successes (rule 3) on three different days since the last miss. With the
  ladder that takes at least 11 days: day 1, day 4 and day 11 after the first success. Three relearning
  sessions gave most of the benefit in Rawson and Dunlosky 2011. A skill stays on the ladder after that,
  every 35 days at most, so it does not drift away before the exam.
  - The first time a skill is relearned is a lasting milestone, shown in the Record as "Relearned on
    <date>". A later miss puts the skill back on the ladder. The milestone stays, because it happened.
- **Which skills are due.** Every skill whose due day is today or earlier. Skills with no verified page,
  or whose pages are gone (ADR 0005, 0006), are left out while that lasts. A skill still waiting for its
  first later check is left out too: that later check is its first recall, it is already in Today, the
  Plan and the spoken check, and it moves the ladder like any other success. So nothing is asked twice.
  (A skill never taught in Dojo has no later check to wait for, so it goes straight on the ladder.) Overdue is simply due. It is
  never counted, never coloured as late and never mentioned as missed.
- **Order.** Oldest due day first, then the heaviest in the exam blueprint.
- **Cap.** About 2 minutes a recall and 10 minutes a day: at most **5 recalls a day per learner**, across
  all exams. What is over the cap is described neutrally: "More are waiting. They come over the next
  days." A backlog never becomes a wall.
  - The cap is also the day's budget for writing recall questions, so it counts questions, not only
    answers. A recall question takes its place when its check starts: the check is saved under the store
    lock before any writer runs. It keeps that place whether it is answered, still being written, or ends
    unanswered. Only a question that ended before any writer had it frees its place, as nothing was spent
    on it. A recall answer sent today to a question of an earlier day takes a place too. So ending a
    recall check unanswered and starting another never writes more than five recall questions a day.
  - While a check with recall questions is still open, Today keeps showing those recalls, so the learner
    goes back to that check to answer them.
- **The question.** A fresh check item written for that skill with the existing writer and checker
  (`make_item`). Every item is new and checked against the situations already used, so the same question
  never comes twice, let alone twice in a row. It is a recall format: the learner says or types the
  answer, and it is judged against the official pages. Multiple choice never counts for the schedule.

### 4. Woven into the day

- **Today.** When recalls are due, a "Due recalls" card comes first, above the next step: the skills (up
  to the cap), about how long it takes, and a "Start recalls" button. It starts a spoken check made of
  the due recalls. When a skill was missed, its row says "Look at this first" with the lesson section.
- **Spoken check.** The 3-minute check asks due recalls first (up to what is left of the cap), then skills
  due for their later check, then untested skills, heaviest first.
- **Plan.** A "Recall" session for each day with recalls due in the next 14 days, sized at 2 minutes a
  recall, the same minutes Today shows for the same recalls (`relearn.minutes`), and capped like Today,
  less what today's checks already took. The places go in Today's order: oldest due day first, then the
  heaviest in the blueprint. On the board it keeps whole 5-minute blocks, like the 3-minute
  check. A recall that can no longer come before its exam day is dropped before the day's places are
  handed out, so it never pushes another exam's recall later. Its link opens the recall check. It is
  done by a recall answer in that exam. Like the daily 3-minute check, a missed Recall session is not
  carried over: tomorrow's is sized from what is due then.
- **No punishment for missed days.** No streak, no "you missed N days", no growing red number. Overdue
  items are due, oldest and heaviest first, within the same cap.

### 5. Logs for a later scheduler

Every judged open answer appends one line to `learners/<key>/recall_log.jsonl`:
`at`, `learner` (the learner key), `package`, `skill`, `item`, `mode`, `recall` (the item was written as a
recall), `result` (`"met"`, `"partly"`, `"missed"`), `met`, `total`, `unaided`, `confidence`,
`days_since_previous` (days since the previous judged open answer on that skill, or null), `step_before`,
`step_after` and `due_before` (the schedule around the answer).

That is what FSRS needs: a review time, the card (the skill), the result and a rating. A later change can
replace the fixed ladder with FSRS like this:

1. Rating: missed = Again, met with Guessing = Hard, met with Fairly sure or no confidence = Good, met
   with Certain = Easy. Partly met counts as Again, as here.
2. Fit FSRS parameters per learner from the log, once there are enough reviews (FSRS needs a few hundred).
   Until then use the published defaults.
3. Replace only `relearn.next_due`. The rules for success, miss, the cap, the order and "relearned" stay.
   The log stays the same. A dispute that is upheld later is read from the Record, not the log.
4. Validate against Dojo's own later checks, not the flashcard benchmark. Its target is recall of cards,
   not passing an exam.

The log is a derived copy for that use. The schedule is always derived from the Record, so a lost or
partial log line changes nothing Dojo shows.

### 6. Multi-user ready

All new state is under the learner: `learners/<key>/selfchecks.jsonl` and
`learners/<key>/recall_log.jsonl`. Everything else is derived from that learner's Record. Every new
function takes the `Learner`. No new singleton, owner binding or background service. The Plan uses the
learner it is given. The export includes both files, and deleting the record removes them with the rest
of `learners/<key>/`.

With Team Dojo (ADR 0009), every new route is a `learner` route (`current_active_learner`). Both files
and the recall reservations (the learner's open checks under `learners/<key>/checks/`) are written
through the Store, so they get its generation-checked writes and tombstones. Leaving or deleting removes
them, and a late write from work that began before is refused.

## Money

- **Confidence and calibration:** no model calls. Counting only.
- **Recalls:** each is one check item written and judged on the existing path, exactly like one 3-minute
  check question: the author and gate calls to write it (up to two rounds), one Speech synthesis of the
  question, and the grader and gate calls to judge the answer. There is no new kind of call.
- **Bound:** at most 5 recalls a day per learner. Most recalls take the place of a later check or a
  3-minute check question the learner would have answered anyway, because those already ask the same
  skills. Dojo does not count model tokens yet (app/costs.py), so the bill shows it in Azure Cost
  Management. The worst case is five extra check questions a day per learner.

## Defaults the owner may change

- The ladder (1, 3, 7, 16, 35 days), the cap (5 a day, 2 minutes each), the minimum for a rate (5 answers),
  and the three-days rule for "relearned". All are constants in `app/relearn.py` and `app/know.py`.

## Consequences

- Confidence is asked once per committed answer, and Skip is always there. One more tap per answer is
  the cost. Tests check it never reaches grading, evidence, readiness or exams.
- Today may lead with recalls instead of new teaching. That is the point, and the cap keeps it short.
- The Plan gains one session kind, "Recall".
- The Record gains a self-report file and a derived log. Both are in the export and deleted with the
  record.

## Review triggers

- Learners skip confidence most of the time: the question is in the way, so drop it for that flow.
- Recalls are mostly met for months: the ladder is too short. Consider FSRS with the logs.
- Recall check items are often skipped because they could not be written: the cap and the Plan
  overstate what is possible.
- A second learner joins (feature/team): check the cap and the Plan work per learner as designed.

## How to undo it

Remove the confidence step in the browser. The field stays in old events and is ignored. Remove the
Recall card, session kind and pick. The schedule has no stored state, so nothing needs migrating. Keep or
delete `recall_log.jsonl` and `selfchecks.jsonl` per learner.
