# ADR 0015 - Judge again: a newer judgement of an earlier answer

- Status: proposed
- Date: 2026-10-05
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder-grader (branch `fix/judge-again`). The lead can reject or change it in
  review. Review triggers are below.
- Guardrails: the Interaction-to-Evidence contract (section 7: a Finding is disputed, superseded or
  withdrawn, never edited; evidence is reinterpreted in a new version and the old version is kept; a
  dispute is resolved by a named adjudicator and both are kept; section 8, cases 6 and 7), EG-13 (record
  the versions behind every finding; never silently rewrite historical evidence), EG-26 (supersession
  without falsifying the historical event; a dispute names the exact object and version; the original
  grader cannot count agreement with itself as an independent appeal), EG-34 (a new version may trigger a
  new interpretation, not mutate the old event), EG-35 (version the grader's instructions and the gate
  policy; tie a finding to the evaluator configuration used), ADR 0011 (calibration and recalls read
  judgements), ADR 0012 (what is closed during a timed exam).

## The requirement

On 2 October the owner answered a DP-800 check question on table design. His answer was a list: one line
per column, each with a type and "non-null" or "null". The grader quoted that list in fragments, one per
line. The quote check of that day wanted one run of consecutive words, so the points it quoted counted as
not met. The grader also marked a point met that was written as a prohibition, about an index the answer
never mentions, with an empty quote. The judgement was 0 of 4. The owner disputed it: he had given every
column a type and said whether it can be null.

PR #41 (3 October) fixed the quote check: a list-like answer may be quoted in exact fragments, a left-out
"no" or "not" is caught, and the checker reads stitched quotes against the whole answer. But nothing could
judge an old answer again. **Judge my answer now** (`regrade`) works only for an answer that is not judged
yet. A dispute takes a judgement out of the evidence; it does not give the answer a fair judgement.

## The options

1. **Leave old judgements alone; disputes are the only remedy.** Rejected. A dispute only removes the
   judgement, so the answer then shows nothing. An undisputed judgement from a known defect keeps
   counting.
2. **Judge every old answer again, automatically.** Rejected for now. It spends money on every old
   answer and changes the record without being asked. It is a review trigger below.
3. **Rewrite the old judgement in place.** Rejected. The Record is append-only and hash-chained; the
   contract (section 7) and EG-34 forbid it.
4. **Count the new judgement as a new attempt.** Rejected. One answer would count twice, and could give
   a second "on your own" or "later" for the same words.
5. **Chosen: judge again, once, when the learner asks, and only a judgement made with older judging
   rules. A new event points at the old one. The new judgement counts in place of the old one, and the
   old one stays visible.**

## The decision

### 1. A version for the judging rules

`GRADING_VERSION` in `app/learning.py` is the version of Dojo's judging rules: the grader's and the
checker's instructions, the check of the grader's quotes, and how a judgement is put together
(`Dojo._judge`). It is bumped whenever one of them changes what a judgement can say.

- Version 1: every judgement stored without a version. That is every judgement before this change, also
  those after PR #41.
- Version 2: today's rules. PR #41's quote check, and the rule of section 7 below: a point is met only by
  what the answer itself says. A fragment quoted with the comma that ends it reads as it does without the
  comma: what follows is the next clause, not a word that negates it.

Every new judgement stores its `version`, in the item's result and in its `answer.judged` event. A bump
also adds a line to `GRADING_CHANGES`, in the learner's words: the page of an answer that can be judged
again shows what changed since its judgement. A test checks that every version has its line.

### 2. Who can judge again, and when

- Only a judged answer whose judgement has an older version than today's.
- Only once per answer. A later version does not open it again (a review trigger below).
- Only when the learner asks: **Judge again with today's rules** on the judged answer. Dojo never does it
  by itself.
- Disputed or not.
- Not while a timed practice exam runs. It is feedback, which the exam closes (ADR 0012).
- In team mode it uses one graded answer from the daily allowance (`budget.action("answer")`), like
  **Judge my answer now**.
- If a model call fails or the budget runs out on the way, nothing changes. The earlier judgement
  stands, the learner is told so, and can ask again.

Route: `POST /api/items/{id}/judge-again`. It runs as a grading job. While one runs for the answer, another
is refused, and only one can ever replace the judgement.

### 3. What the Record keeps

- A new event, `answer.rejudged`. It has the same fields as `answer.judged` (for each point: met, and the
  learner's words quoted; met, total, unverified points, whether feedback was shown, the version). It
  adds `replaces`: the old judgement's time, met, total and version, whether it was disputed, and the id
  and hash of the `answer.judged` event that recorded it.
- No earlier event is changed, and the chain still checks out.
- The item keeps the old result in `history`, with the dispute filed against it, and when and why it was
  replaced. The export carries both, and both events.
- The Studio log gets one "judged again" line with both results. The recall log gets a new line marked
  `rejudged`.

### 4. What counts

Evidence (`app/evidence.py`) reads the new judgement in place of the old one.

- The attempt keeps its place and its conditions. It was answered when it was answered, in its mode,
  with its hints and its time since teaching. So "on your own" and "later" depend on how the answer was
  given, not on when it was judged.
- Met, total, all met, unverified points and feedback shown come from the new judgement. The attempt
  lists the earlier judgement, and its conditions say it no longer counts.
- The new judgement teaches when it is made, so "later" waits again from then on.
- Everything that reads attempts follows: the pips and readiness, calibration and confident errors
  (ADR 0011), recalls and their schedule, the Plan, Play's moments, the skill page and the export.
- The new judgement can be higher or lower; both count the same way. A dispute still never upgrades
  anything. Judging again is not an upgrade of the old judgement: it is a new judgement under newer
  rules, with its version and models on record.
- Play earns no study points for it: points count answers, and this is the same answer. A moment the new
  judgement makes keeps the answer's date.

### 5. What happens to a dispute

The dispute stays on the Record, unchanged, attached to the judgement it was filed against. That
judgement no longer counts. The new judgement counts, and can be disputed on its own.

Why:

- A dispute names the exact object and version (EG-26). The owner's dispute is about the judgement of
  2 October, version 1. It cannot be about a judgement that did not exist when he filed it, so it does
  not carry over.
- Judging again is not an adjudication. The contract wants a reviewer other than the evaluator
  (section 8, case 6), and EG-26 says the original grader cannot count agreement with itself as an
  independent appeal. The new judgement uses the same grader and checker models. So the dispute is not
  marked upheld or rejected. It stays open, because Dojo has no adjudicator yet.
- The old judgement is superseded, never edited (section 7, and case 7). It stays visible, marked as
  replaced, with the reason and the date, and the learner sees that the result changed.
- Carrying the dispute over was considered and rejected. It would hide a judgement the learner has not
  objected to, and a dispute of one judgement would silence another.

Each `dispute.filed` event now also names the judgement it is about (`judged_at` and `version`).
Evidence attaches a dispute to the judgement that counted when it was filed, by its place in the Record,
so disputes filed before this change attach correctly too.

### 6. The pages

- A judged answer with an older judgement shows a panel, "Judged with older rules". It names the version,
  says what changed since (from `GRADING_CHANGES`), that judging again happens once, that the result can be higher
  or lower, and that the earlier judgement stays visible. With a dispute, it says the dispute stays on
  record and that judging again is not a review of it. A confirmation comes first.
- After judging again: a "judged again" chip, and the judgement says when it was judged again. The
  earlier judgement, its points and the dispute are shown folded. Each judgement names the version of
  the rules that made it.
- Your Record: the attempt says "Judged again on (date) with newer judging rules, at your request", and
  its conditions name the earlier judgement. The Integrity panel says an answer judged again keeps its
  earlier judgement visible, and only the new one counts.

### 7. Rubric points phrased as prohibitions

Part of version 2. The grader's instructions say a point is met only by what the answer itself says: not
mentioning something never meets a point, and a "Do not ..." point is met only when the answer states the
right choice or rejects the wrong one. The item writer is told to write every rubric point as what a
correct answer states or does. A new rubric point that starts as a prohibition ("Do not", "Avoid",
"Never", "Must not" and the like, `rubric_prohibition`) goes back to the writer with the reason, so its
second round rewrites it. Items already written are not changed; their points are judged by the new
grader rule.

## Money

Judging again costs what judging an answer costs: one grader call (two when its quotes fail the check)
and one checker call. The disputed DP-800 answer was judged twice with the real models, once fresh and
once through **Judge again**. Both gave 2 of 4: the two points the dispute was about were met, the other
two were not. Each run cost about 1.2 US cents at list price. The grader read about
4,750 tokens and wrote 600 to 650. The checker read about 4,850, most of them cached, and wrote about 80,
plus 1,450 to 1,750 reasoning tokens. In team mode it is one graded answer from the daily allowance.

## Consequences

- A judgement made under a known defect can be replaced on request, and the history shows both.
- The learner chooses which answers to have judged again, and will likely pick the ones that seem too
  low. That is limited: only judgements from older rules, once each, the new result counts whether it is
  higher or lower, it cannot be undone, and the old one stays visible. Old judgements that were too
  generous stay unless the learner asks.
- Every judgement made before this change is version 1, also those after PR #41. Each can be judged
  again once, on request.

## Review triggers

- Dojo gets an adjudicator for disputes, someone other than the grader: disputes of replaced judgements
  could then be resolved.
- Readiness leans on many version-1 judgements: consider a re-evaluation of all of them that the owner
  starts, with its cost shown first (the contract's section 8, case 7).
- A later version fixes another defect: decide whether an answer already judged again may be judged once
  more.
- Team mode is in use: check whether judging again is used to try for a better mark.

## How to undo it

Remove the button and the route. Keep reading `answer.rejudged` in evidence: the events stay in the
Record, and if evidence ignored them, the old judgements would silently count again.
