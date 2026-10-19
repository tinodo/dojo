# ADR 0022 - The course picker says what a page is about

- Status: proposed
- Date: 2026-10-19
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `fix/course-picker-scope`). The lead can reject or change it in review.
- Guardrails: ADR 0009 (the exam an owner picks is the one shared work goes to first), ADR 0019 and ADR 0020 (Studio is the
  owner's page for every course).

## The requirement

The owner wrote: "At the top of the page there is a course selector. Yet this appears NOT to be viable for Studio; I see all
courses in one table. This was confusing. Not sure how that top course selector behaves." He was right. The picker at the top
(and the exam list in the side bar) chooses the exam Today, Course, Practice and Record are about. Studio, Plan and Play show
every exam together: none of their pages reads the exam picked (`/api/studio/*`, `/api/quality`, `/api/plan`, `/api/play`), so
choosing another exam there changed nothing on the page. One thing did follow it: Studio's course cards were ordered with the
active exam first, through a view that is kept for a few minutes, so the order changed late, if at all. The picker also showed
a course on pages that are not about one.

## The decision

- **The pages about every exam say so.** `ALL_COURSES_PAGE` names them: Studio and its four tabs (the same route is the
  members' "Studio log"), Plan and Play. On them the header picker reads "ALL / All courses / not tied to one course", the
  badge drawn as an outline, and its first choice is "All courses", already chosen. A page that shows every exam has to be in
  that list; any other page keeps the exam picked in the picker, as before.
- **Choosing a course there opens it.** These pages have no page for one course, so a choice in the picker, or a press on a
  course in the side bar, makes that course the exam picked and goes to its Today, the one already picked too. The choices read
  "Open DP-800 · ..." and the picker is described to a screen reader, not only by a tooltip.
- **The side bar keeps marking the exam picked on those pages.** It stays the exam Today, Course, Practice and Record open and
  the one Dojo prepares first (ADR 0009; Studio's Deeper lessons text says "your active exam first"). Hiding it would hide a
  preference that is real.
- **Studio's cards no longer follow the exam picked.** The Courses tab lists the courses in the order of `exam_order` without
  its first rule (`active_first=False`): by how many members study them, then catalog order. The work Dojo does in the
  background keeps the active exam first.
- **A skill or a lesson of one of the owner's exams moves the picker to that exam** (`followExam`, as a link to a check or a
  practice exam already did), so a link from Studio or Plan into another exam no longer leaves the previous exam in the picker
  and in the "Course" crumb. A member's exam they do not study is left alone.
- **A choice never takes the owner off the page he has gone on to,** and the writes of the exam kept go one after another
  (`keepExam`): a slow answer cannot jump to Today after a click elsewhere, and two quick choices keep the last one.

## Options considered

- Filter Studio and Plan by the picker. Rejected: only two tabs of Studio have a course in them; Checks, Costs, Plan and Play
  are the same whatever is picked, so a filter would work on some pages and not on others, which is the confusion again.
- Disable or hide the picker on those pages. Rejected: a dead control, and no way to open a course from there except the side bar.
- A line of text under Studio's title. Rejected: the owner has said the pages carry too much text, and the picker would still
  show a course.
- A second selector inside Studio. Rejected: two selectors on one page.
- Remove the mark in the side bar on those pages. Rejected: the exam picked is a real preference (the order of rewrites,
  checks and question pools follow it), not only the order of Studio's cards.
- Open the course's Course page rather than Today. Not done: Today is where a switch from any page without a version for one
  exam already goes.

## Consequences

Opening a lesson or a skill of another exam now makes that exam the exam picked, which Dojo keeps, so the next visit and the
order of the background work follow it. Pages that show every exam and are added later have to be added to `ALL_COURSES_PAGE`;
`tests/test_picker.py` runs the shell of `app.js` in node (the picker, the side bar, the switch, the order of writes, a late
answer) and checks that route, picker and views call each other. Not covered, and as before: Five Minutes picks an exam on the
server that may not be the one picked, and a timed practice exam does not hide the picker.

## Review triggers

Change it if a Studio tab becomes per course (then the filter is the better design for that tab), if the owner wants the active
course first on Studio again (then say so on its card and keep it out of a cached view), if the picker is wrong on a page that
is not in the list, or if opening a lesson changing the exam picked surprises the owner.
