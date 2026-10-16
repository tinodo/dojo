# ADR 0019 - Studio: is each course complete, and what did Dojo do on its own

- Status: proposed
- Date: 2026-10-16
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feature/studio-overview`). The lead can reject or change it in review.
- Guardrails: EG-24 (nothing about another learner is shown), ADR 0005 (filming and every other paid step is the owner's
  decision, with the price shown), ADR 0009 (the Studio routes are `Admin`), ADR 0018 (the search for official pages).

## The requirement

The owner asked: does Dojo have everything I need to pass? All skills, all lessons, all audio, all videos? What is
"374 meaning at risk"? What is Studio for? He wanted a page per course, and a page that shows what Dojo did and when.

## What was wrong

Studio was one long page ordered by the pipeline that builds a lesson (sources, author, gate, you, grader, record). It
answered "how is a lesson checked" and "what did the checker block", and it did so well. It did not answer "is the course
complete": the numbers that say so (lessons, audio, Deeper Listen, films, pages) were spread over five cards and the
exam page, one course at a time, and some were not shown anywhere (how many lessons have a film of their current version).
The tile "Meaning at risk" counted audio pieces where the voice check heard a name or a number differently, which says
nothing about the exam content; the name read as a warning about the course. Nothing showed what Dojo did on its own
since the owner last looked.

## The decision

Studio has four tabs, the first one new. Everything is read from what Dojo already keeps; nothing is written, no model is
called, nothing is spent.

- **Courses** (`GET /api/studio/courses`, `app/studio.py`): one card per course, original or added. Rows: official pages,
  lessons, Listen, Deeper Listen, Present, Watch, practice questions, the timed practice exam. Each row says in words what
  its number counts. "Ready to study" means every skill has an official page, a lesson and its Listen audio, and nothing
  more: it is not a pass prediction. Watch counts three kinds of lesson: a film of the current lesson, the film of an
  earlier version (what Watch plays, with a note, after a lesson was written again), and none; the price of filming the rest
  is shown from the same estimate the exam page uses, and filming stays the owner's decision. "What to know" lists what is
  missing or waiting, with a link to where it is decided. An original course says that no page has been through the
  two-model page check (ADR 0018); an added course says how many pages the search found, and how the audit of the older
  pages stands.
- **Activity** (`GET /api/studio/activity`): what Dojo is doing now, and what it did in the last 14 days, newest first,
  grouped by the reader's own days: lessons written (and written again), official pages found, Deeper Listen, source pages
  that changed, jobs of the owner (building, filming, drawing, and anything that failed) and what failed to prepare. The
  metered spend of each UTC day is shown beside it (team mode). Only the owner's own jobs are listed.
- **Checks**: what Studio was: the pipeline, what the voices said, source changes and the films to approve, deeper lessons,
  the search for pages, and everything the checker blocked. The tile now reads "Worth a listen", counts what differs by
  kind (name or term, number, negation) and says that the check transcribes the audio again, so it says nothing about
  whether the lesson text is right.
- **Costs**: the list-price estimates and the exams added. It was the slowest part of the page and is now read only when
  opened.

Two views read every lesson and every record, so they are kept 60 seconds and read again in the background: a visit gets
the last reading at once and the very first visit after a restart waits. They are also read once, 90 seconds after
startup in Azure. `Store.names` lists a folder with `scandir`, which knows each entry's kind from the listing: listing the
342 lesson files for every skill took 25 to 40 seconds of stat calls on Windows (the same call made Studio's cost view take
a minute), and is now about a second.

## Not decided here

- A bulk way to approve filming for the original courses (their lessons offer it one by one, and in Studio's "Film again"
  for lessons whose film is of an earlier version).
- Running the two-model page check on the original courses: it is a report that costs a few dollars and changes nothing,
  and is the next change.

## Consequences

The owner can see in one place which course is complete, what is missing, what finishing it costs and what Dojo did today.
The numbers are Dojo's own checks on its own files, not a prediction of the exam. The Studio routes stay `Admin`; a
timed exam closes them like the rest of Studio.

## Review triggers

Change it if a row counts something the learner would not call "there", if the activity shows anything about another
learner, or if the first reading after a restart takes long enough to need the page to say so.
