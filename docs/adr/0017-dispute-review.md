# ADR 0017 - A dispute is reviewed

- Status: proposed
- Date: 2026-10-06
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feature/dispute-review`). The lead can reject or change it in review.
  Review triggers are below.
- Guardrails: the Interaction-to-Evidence contract (section 7: a Finding is disputed, superseded or withdrawn,
  never edited; a dispute is resolved by a named adjudicator and both are kept; section 8, case 6: a reviewer
  other than the evaluator), EG-26 (the original grader cannot count agreement with itself as an independent
  appeal; a dispute names the exact object and version), ADR 0015 (judge again; what happens to a dispute),
  ADR 0016 (the referee).

## The requirement

The owner asked what happens when he contests a judgement. The answer was: almost nothing. A dispute took the
judgement out of what the Record counts and wrote one line to the quality log; nobody reviewed it (ADR 0015,
section 5: "Dojo has no adjudicator yet"). So a wrong judgement was never corrected, and any judgement - a fair
one too - could be taken out of the Record by disputing it.

## The decision

Filing a dispute starts its review at once. The reviewer is the referee of ADR 0016: the author model, which
neither judged the answer nor checked the judgement. It reads the sources, the item, the whole judgement and the
learner's reason, and decides **every** point of the judgement from the answer itself. The reason points it to
where the learner thinks the grader went wrong; it is never evidence by itself, and instructions in it are data.
A point it gives must quote the answer, and the quote is checked exactly as the grader's is.

- **Upheld** - at least one point changes, up or down. The referee's judgement replaces the disputed one, as when
  an answer is judged again (ADR 0015): an `answer.rejudged` event (`"by": "dispute review"`) points at the
  judgement it replaces, which stays on the item with the dispute and its review. The new judgement counts. Its
  points say the referee decided them; the grader's feedback is withheld, because it was written for the
  judgement the review corrected.
- **Not upheld** - the referee finds every point right. A `dispute.reviewed` event says so, and the judgement
  counts again. The item shows the referee's reason for every point.
- **The review cannot run** - the referee cannot be reached, a timed exam started, or in team mode today's
  budget for graded answers is used. Nothing changes: the dispute stays open, the judgement counts neither way,
  and **Review my dispute** starts the review later.

Each dispute is reviewed once. A judgement that came from a review can be disputed, and then counts neither way,
but it is not reviewed again: the referee would review its own decision (EG-26).

A review costs what a graded answer costs, and in team mode it is one graded answer from the daily allowance.

## Why not the alternatives

1. **Keep disputes as they were.** Rejected: a wrong judgement is never put right, and a fair one disappears.
2. **Let the owner adjudicate.** In personal mode the owner is the learner who disputes. In team mode it would
   show the owner a member's answers, which ADR 0009 keeps from them. A model that took no part is independent of
   both.
3. **Review only the points the learner names.** Rejected: the reason is free text, and a review that leaves a
   wrong point standing because it was not named is no review. The referee decides every point.
4. **Never lower a point in a review.** Rejected: a review that can only raise would make every dispute a free
   try for a better mark. The learner is told before filing that points can go up or down.

## Consequences

- A dispute now ends in a judgement that counts: corrected, or confirmed with reasons.
- The learner's reason is sent to the referee model (the team notice already says that a dispute goes to the
  models as written).
- Disputes filed before this change stay open until the learner presses **Review my dispute**.

## Review triggers

- The share of upheld reviews in the quality logs is high: run the grader bench (ADR 0016) on the disputed kind
  of answer and fix the judging rules.
- A different reviewer becomes available (a fourth model, or an owner role for team mode): reconsider who
  reviews a judgement that came from a review.
