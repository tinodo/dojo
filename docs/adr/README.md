# Architecture decision records

Each ADR records one decision that shapes Dojo: the problem, the options, what was chosen and why, and
what would make us look again. They are written in plain English for the operator, for reviewers and
for contributors.

The status is the one written in the ADR. Every decision below is built and running on `main`. Several
were recorded as "proposed" while their design was reviewed and were built afterwards; their text was not
re-labelled. Team mode (0009) and Play's social layer (0010) are built but switched off by default.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-microphone-for-spoken-answers.md) | The browser may use the microphone on Dojo's own site only, so a learner can say an answer out loud. Speech is sent once to Azure AI Speech and not stored. | Accepted |
| [0002](0002-private-podcast-feed.md) | A private podcast feed per exam that podcast apps fetch without signing in. A long random token in the path is the credential; Dojo keeps only its hash. | Accepted |
| [0003](0003-dp800-lab-sandbox.md) | DP-800 labs run in a private, free-offer Azure SQL database behind a private endpoint, checked in the database itself. | Proposed (built) |
| [0004](0004-private-calendar-feed.md) | The study plan is published as a private iCalendar feed that Outlook subscribes to by URL. Dojo never reads or writes anyone's calendar. | Accepted |
| [0005](0005-add-an-exam.md) | Adding an exam: an exam package is data, read from the official study guide, and a page grounds a skill only after a checked quote proves it teaches that skill. | Accepted |
| [0006](0006-source-drift.md) | When an official page changes, every lesson and question written from it is checked again word for word, and what no longer holds is held back and rewritten. | Accepted |
| [0007](0007-deeper-listen.md) | Deeper Listen: a gated two-voice audio lesson of about six minutes that teaches the whole lesson, written only from checked content. | Accepted |
| [0008](0008-deeper-lessons.md) | Deeper lessons: up to two more official pages per skill, found and checked in the background, and lessons written again from all of them. | Accepted |
| [0009](0009-team-dojo.md) | Team Dojo: several learners in one Dojo, with owner approval, per-learner isolation, budgets and deletion that stays deleted. Off by default; needs a privacy and works-council review before use. | Proposed (built, off) |
| [0010](0010-play.md) | Play: mastery moments, a weekly rhythm and study points that never depend on being right; circles, an effort board and duels in team mode only, with no ranking. | Proposed (built; social layer off) |
| [0011](0011-know-what-you-know.md) | Know what you know: "How sure were you?" after an answer, a calibration view, and successive relearning at growing intervals. A self-report never fills a pip. | Proposed (built) |
| [0012](0012-exam-realism.md) | The practice exam feels like the real one: seven item kinds, sections that lock, a server-side clock, and Microsoft Learn allowed where the real exam allows it. | Proposed (built) |
| [0013](0013-phone.md) | Phone: an installable web app, opt-in push nudges (at most one a day) and a 5-minute session. | Proposed (built) |
| [0014](0014-present-deck.md) | Present is a real deck: built from the checked lesson at no cost, with SVG visuals from one gated author call per lesson, synced to the slide narration. No AI images. | Proposed |
| [0015](0015-judge-again.md) | Judge again: an answer judged with older judging rules can be judged again with today's, once, at the learner's request. A new event points at the old judgement, which stays visible; a dispute stays with the judgement it was filed against. | Proposed (built) |
| [0016](0016-grader-bench-and-referee.md) | The grader bench measures judgements against answers with known results (version 2: 97.9%); judging rules version 3 adds a referee, a third model that decides the points the checker disagrees about (version 3: 432 of 432). | Proposed (built) |

## Writing a new ADR

1. Take the next free number (look in this folder; do not reuse one).
2. Name the file `NNNN-short-title.md` and start with the title, status, date, decision owner and the
   guardrails it touches, as the existing ADRs do.
3. Describe the problem, the options you considered, the decision, its consequences and its review
   triggers.
4. Add a line to the table above in the same pull request.
