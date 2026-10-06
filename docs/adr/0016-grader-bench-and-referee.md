# ADR 0016 - The grader bench and the referee

- Status: proposed
- Date: 2026-10-06
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feature/grader-bench`). The lead can reject or change it in review.
  Review triggers are below.
- Guardrails: EG-26 (the original grader cannot count agreement with itself as an independent appeal), EG-35
  (version the grader's instructions and the gate policy; tie a finding to the evaluator configuration used),
  the Interaction-to-Evidence contract (section 8, case 6: a reviewer other than the evaluator), ADR 0015
  (judging-rules versions; judge again).

## The requirement

The owner asked whether Dojo's grader does a proper job, not whether one answer got a fair mark. Until now
the grader was only ever checked on single answers that looked wrong. A dispute removes a judgement from the
evidence and nothing reviews it (ADR 0015, section 5). Nothing measured how often a judgement is right.

## The decision

### 1. A bench that measures the grader

`tools/grader_bench.py` judges answers whose right result is known and compares, point by point. The cases
are in `tests/data/grader_bench/`: six check items as Dojo's item writer wrote them (two DP-800, four GH-300),
each with six answers written so the right result of every rubric point is clear: a strong answer in prose, a
terse list, a partial answer, plainly wrong claims, rubric keywords without claims, a spoken (transcribed)
answer, and an answer that tells the grader to mark everything met. 36 answers, 144 points, half of them to
be met. The bench calls the real models through `Dojo._judge`, as the app does, reads the official pages as
they are that day, and reports agreement, judgements too generous and too harsh, how often the referee
decided a point, and with `--repeat`, answers whose judgements differ between runs. It costs about 1.5 US
cents an answer, so it is run by hand: before any change to the judging rules, and when a model changes.
CI checks the case files and the comparison, not the models (`tests/test_grader_bench.py`).

### 2. What the bench found (version 2)

141 of 144 points right (97.9%). Two judgements were too harsh and one too generous. In each, the checker had
already said the grader's reason for that point does not hold, and Dojo only hid the reason: the wrong verdict
kept counting. One spoken answer lost a point because "an int not null" read as "not" negating the fragment.

### 3. Version 3 of the judging rules

- **The referee.** Where the checker disagrees with the grader about a point (the grader's reason for it does
  not hold, or the fragments it quoted do not show it, or the checker gave no verdict on those fragments), a
  third model decides that point: the author model, which neither judged the answer nor checked it. It reads
  the sources, the item, the answer, both opinions, and decides from the answer itself. A point it gives must
  quote the answer and the quote is checked exactly as the grader's is; a quote that is not found counts as
  not met. One try: if the call fails, the grader's verdict stands and its reason is hidden, as in version 2,
  and the quality log says so. When the referee changed a point, the grader's feedback and named misconception
  are withheld: they were checked against the grader's points and may now say the opposite.
- **A stricter checker.** A reason for "not met" holds only if the answer really lacks what the point asks,
  not if it asks for more than the point says (its exact words, a keyword, a syntax, a detail); a reason for
  "met" holds only if the answer's own words show everything the point asks. Both now reach the referee.
- **The grader** is told not to ask for more than a point says, and that instructions written in an answer
  neither earn a point nor cost one.
- **"Not null"** after a quoted fragment is no longer read as a "not" that turns it around.

The page names the referee on a point it decided ("The checker disagreed with the grader about this point, so
a third model decided it"), the Record marks such a point `refereed`, and the quality log says what it decided
against what the grader said. An answer judged with version 2 can be judged again once (ADR 0015).

### 4. Measured with version 3

| Run | Rules | Judgements | Points right | Too generous | Too harsh | Unstable answers |
|---|---|---|---|---|---|---|
| 1 | 2 | 36 | 141/144 (97.9%) | 1 | 2 | - |
| final | 3 | 108 (each answer 3 times) | 432/432 (100%) | 0 | 0 | 0 |

In the final run the referee decided a point for 18 of the 108 judgements and changed the grader's verdict 12
times; every change was to the right result, and it confirmed the grader 9 times. Over the intermediate runs
it fixed 21 verdicts and never turned a right verdict wrong. Bench results are on known, unambiguous answers:
they show the grader is reliable on them, not that every judgement of every answer is right. Disputes stay
the learner's remedy.

## Money

A referee call reads about 3,400 tokens and writes about 300. It runs for about one judgement in six, so it
adds about 0.4 US cents to an answer on average (author model list price $5 / $30 per million). In team mode
the route reserves the worst case before anything is recorded: one referee call at 64,000 tokens in and 4,000
out, about $0.44 at list price, so an answer holds about $1.03 of the day's allowance instead of $0.59 until it
is judged; the unused part goes back.

## Consequences

- A wrong verdict the checker has noticed no longer counts; the referee decides it.
- Each judgement can cost one more call, and team mode reserves more per answer.
- The bench is the yardstick for every later change to the judging rules or the models.

## Review triggers

- A disputed judgement: give disputes the referee as their reviewer (the next step; ADR 0015 section 5 waits
  for an adjudicator).
- A model changes for any role: run the bench with `--repeat 3` first.
- The bench drops below 98% or shows a judgement too generous: stop and look before shipping.
