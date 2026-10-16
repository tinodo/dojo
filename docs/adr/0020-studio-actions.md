# ADR 0020 - Studio: one button for each thing that is still to be made

- Status: proposed
- Date: 2026-10-16
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feature/studio-actions`). The lead can reject or change it in review.
- Guardrails: ADR 0005 (filming and every other paid step is the owner's decision, with the price shown), ADR 0009 (the
  Studio routes are `Admin` and closed during a timed exam), ADR 0014 (diagrams drawn ahead of time are one switch for all
  courses), ADR 0019 (Studio's Courses page).

## The requirement

The owner opened Studio's Courses page, saw what was missing, and could not tell what to press to get it made. The page said
it in sentences. The buttons were on three other pages: the exam page for an exam he added, Checks > "Film again" for lessons
whose film is of an earlier version, and Costs > "Draw ahead of time" for the diagrams. The 18 lessons of the original courses
that were never filmed had no bulk button at all.

## The decision

The Courses page is the page to press buttons on, and it is short.

- **Still to make** (top): what is left over all courses, with its price: the videos (presenter films) and the diagrams for
  Present. One button, **Make everything**, asks once, with the price of each course in the question, and then approves each
  course's films and turns the diagrams on. A course that is refused (its lessons or its price changed, or the films would not
  fit) does not stop the others; the answer says what happened to each (`POST /api/studio/make`).
- **A card for each course** is a table with one line for each thing a course is made of: official pages (only while some are
  missing), lessons, Listen, Deeper Listen, diagrams, videos, and on an original course the optional page check. Each line has a
  bar (`n of N`) and at the end either "done", what Dojo does by itself (lessons, Listen and Deeper Listen), or a button with
  its price on it. What each number counts stays, in words, under **Details**, closed.
- **Videos.** One offer for each course: every newest lesson without a film of its own, whether an earlier version was filmed
  or it never was, except a lesson a changed page holds back (it is written again first), a lesson with no narration, and
  lessons already approved. The price is `Costs.film_estimate` over the presenters whose video is not stored yet (a lesson with
  one of its two films costs one). The button's token names the lessons and the price (`sha256` of both), so a set or a price
  that changed since the page was drawn is another offer: the server recomputes both when the button is pressed and refuses
  (409) if the token differs. A price that could not be worked out completely has no token and no button: the owner is never
  asked to approve a price he was not shown. The lessons that are not offered are counted with the reason ("3 wait for a
  rewrite"), so no number is left unexplained.
- **Who keeps the films.** An exam the owner added keeps them in its build document (`NewExams`), a built-in course in the
  drift check's state (`DriftCheck`, which already files approved films one at a time). A lesson belongs to exactly one of them:
  the drift check no longer offers the lessons of an added exam. `POST /api/studio/film/{pid}` takes the token and hands the
  approval to whichever keeps that course (`NewExams.approve_film`, `DriftCheck.approve_offer`); the old buttons keep working.
- **Filing.** Both keep filing one film at a time through the ordinary video job, and both now start it atomically for a lesson
  (`Jobs.start(busy_message=...)`), so two callers cannot film the same lesson at once. The drift check checks again, when it
  is a lesson's turn, that the lesson is still the newest of its skill and not held back, and drops the approval otherwise: a
  lesson written again after the approval is not filmed, and the new one waits for an approval of its own. The Preparer does the
  same for an added exam (`NewExams.next_film`). The drift check also waits while the presenter videos are 90% full, as the
  Preparer does.
- **Stop.** "Stop the rest" forgets the films that were approved and have not started (`POST /api/studio/film/{pid}/stop`).
  It takes the same lock as the filing of a film (the drift check) so a film is either stopped or started. A film being made
  finishes; the page says so.
- **Room.** Presenter videos are capped at 6 GB (was 3 GB). The share is 10 GB; the data folder held 3.4 GB, of which
  1.95 GB were videos, and filming every lesson of the three courses needs about 2.9 GB of current films while the 231 videos of
  earlier lesson versions stay (they are what Watch plays, with a note, until a lesson has its own film). At 3 GB that would
  have paused filming at 90% before the end, or evicted films that had just been made. An offer is shown without a button
  ("no room: needs X GB, Y GB free") and refused by the server when its films, at the stored average size plus 20%, and the films
  already approved but not made yet, would not fit under 90% of the budget.
- **Diagrams** stay one switch for all courses (ADR 0014): they are drawn ahead of time, up to 20 a day, the active exam first,
  and the switch stays on for lessons written later. The page and the confirmation say so, with the price of all lessons that
  have none; the daily allowance bounds the spend. "Stop drawing" turns it off.
- **Pace.** The line of a course that is being filmed says how many lessons are left and about how long that takes: the
  middle of the owner's finished film jobs (three minutes until there are five) plus the wait the filing keeps between films
  (one minute for the drift check, three for the Preparer). While anything is being made the page reads the cached view again
  every 30 seconds and stops when it is left. A reading made on the spot after a button is never replaced by an older background
  reading (`Studio._gen`).
- **The lesson page's "Film this lesson"** shows what it costs now (`GET /api/lessons/{id}` carries `film_price` for the
  owner, from the presenters not stored yet). It used to show only the time.
- A course's headline says one of: "Everything is made", "You can study this now. Still missing: ...", or how many skills
  cannot be studied yet. "Ready to study" keeps its meaning (every skill has an official page, a lesson and its Listen audio);
  "Everything made" adds Deeper Listen, diagrams and videos for every lesson. Neither is a pass prediction.

## Options considered

- A "film everything" button that skips the confirmation. Rejected: ADR 0005.
- One server-side queue for both kinds of course. Better in the end, but it moves the approval state of the original courses
  and breaks what the drift check's lesson notes read. The two keepers stay, and a lesson is queued by one.
- Cleaning up the videos of superseded lesson versions, so that room is counted on current films only. Not done: Watch plays
  the film of an earlier version until the lesson has its own, so only the videos of lessons that have a film of their own are
  free to remove, and a wrong cleanup deletes money. See below.
- Raising the plan's storage instead of the videos' budget. The right answer if many more courses are added; a change of the
  deployment, not of this page.

## Not decided here

- Reference-aware cleanup of videos that no lesson plays any more. At 6 GB the presenter videos hold about 4.5 GB when every
  lesson of the three courses is filmed. `Avatar.evict` still removes the least recently served video when a new one does not
  fit, so a full budget degrades by removing old films, not by refusing.
- A lesson that will be written again on the deeper template (`DeeperLessons.to_write_again`, ADR 0008) is still offered, as it
  was by the older buttons: a film made for it would be for a lesson nobody is taught afterwards. None is waiting today, and a
  film approved for a lesson that was already written again is not made. If the template changes or many pages are replaced,
  the offer should leave those lessons out until they are written again.
- The older film buttons (**Film this lesson**, **Film again** in Checks, the exam page's **Film the lessons**) still approve by lesson
  ids and do not check the room at the press; both keepers pause at the filing, while presenter videos are 90% full, and the lesson
  page shows its price. Binding all of them to a priced offer would be a change to those pages, not to this one.
- Stopping a film that is already being made, and a second film queue that runs two films at once.
- Whether the page check of an original course should be able to replace a page. It only reports.

## Consequences

The owner opens Studio, sees what is missing with its price, and presses one button for a course, or one for everything. Nothing
that costs money starts without a confirmation that names the price, and the server holds the price and the set of lessons to
what was shown. The estimates are list prices; the bill can differ.

## Review triggers

Change it if a number on the page cannot be explained by the lines under it, if a film is made for a lesson nobody is taught, if
presenter videos pass 80% of their budget, or if the pace shown is wrong by more than half for a day.
