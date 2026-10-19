# ADR 0021 - Pages both models refuse are replaced, filmed or not

- Status: proposed
- Date: 2026-10-19
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `fix/refused-filmed-pages`). The lead can reject or change it in review.
- Guardrails: ADR 0018 (the search finds the pages; a film is kept), ADR 0008 (a lesson is written again when its pages
  change), ADR 0005 (filming and every other paid step is the owner's decision, with the price shown), ADR 0020 (the Courses
  page offers the film).

## The requirement

Dojo's own audit of the pages DP-300 cited before the search existed found two skills whose pages both models refuse, twice,
a day apart, and whose lessons are filmed. One lesson, "Implement statistics maintenance tasks", teaches statistics
maintenance on SUSDB (the WSUS database) from a Configuration Manager page; the other, "Identify use cases for Azure SQL
Database in Microsoft Fabric", cites a documentation navigation page. The search had already accepted 13 and 14 proper pages
for them, but ADR 0018 keeps the pages of a skill whose lesson has a film: it set `replacement_waits`, nothing read it, and
Studio said "Dojo ... finds a better page on its own", which was false for exactly these skills. A film that teaches the wrong
thing is not worth keeping, and nothing in Dojo would ever have changed it.

## The decision

- **A skill whose cited pages both models refuse twice gets the pages the search accepted for it, filmed or not**
  (`Sourcer._activate`, the `replacing` branch). This is the one measured reason ADR 0018 lacked for changing what a film
  teaches from: both models read every cited page and gave each no grade, on two looks at least 20 hours apart, at most three
  skills a day for each exam (`REPLACE_PER_DAY`, unchanged). A `partial` skill that is not refused still gains pages only while
  its lesson has no film.
- **The pages are replaced only while the package still cites a refused page.** If none is cited any more (an earlier step
  wrote the new pages and stopped before it saved its book, or the pages changed since) nothing is written: no second
  generation, no overwrite of what the package cites now.
- **The new lesson is written by Deeper lessons**, as for any change of pages (`why` = "pages", ADR 0008), within the
  daily allowance, about 0.6 USD. The old lesson stays the one learners see until the new one passes every check.
- **Nothing is filmed.** The new lesson has no film, so it is in the Film offer of the Courses page with its price (ADR 0020).
  Pressing it is the owner's decision. The old film is not removed.
- **Watch does not play the old film on the new lesson** when the new lesson cites none of the pages the earlier version was
  written from (`Dojo._earlier_film`, per ancestor): that film teaches from pages that did not cover the skill. A lesson
  rewritten on the deeper template from the same pages and more keeps showing the earlier film with its note, as before. A
  lesson with no recorded pages is treated as sharing, so today's behaviour holds when nothing is known.
- **A rewrite is not rejected for being thinner than the lesson it replaces when it teaches from other pages**
  (`make_lesson`): the points of a lesson written from refused pages are no measure. Without this a correct new lesson with
  fewer checked points than the wrong one would be refused three times and given up, and the wrong lesson with its film would
  stay. The share of points that hold (75%) and two sections are still required.
- **A failed or given-up attempt at the old lesson is cleared when its pages are replaced** (`DeeperLessons.start_again`):
  attempts are kept for a lesson, not for the pages it is written from, so an earlier failure would otherwise make the new
  pages wait a day or never be tried.
- **Books held under the old rule are settled** (`Sourcer._resume_replacements`, called before any searching): a `partial`,
  `stuck` book with `replacing` and `replacement_waits`, accepted pages and a confirmed refusal on the audit of the present
  `VERIFIER_VERSION` gets its pages written, one skill a step, with files only and no model call. Its state becomes `covered`
  when the pages it cites now are what covers the skill (`_justified`), otherwise it stays `partial`. Two skills are in this
  state today; no new one can arise.
- **Studio tells the truth.** The attention line says what happens to a skill whose pages are refused (better pages, the
  lesson written again, a filmed lesson offered for filming again at its price, the old film not shown meanwhile), and the audit
  counts only skills that still cite the pages that were judged (`audit.current`): an audit of pages that were replaced since
  is history, no longer a number.

## Options considered

- Keep the film and the pages, and only tell the owner. Rejected: the owner cannot fix a page, and the lesson stays wrong.
- Replace the pages and keep showing the old film until the new one is made. Rejected: it teaches the wrong thing for as long
  as the owner does not press a button.
- Withhold the old lesson until the new one exists. Not done: the skill would have no lesson for the minutes the rewrite
  takes, and for days if the author fails; the lesson is already listed in Studio. See the review triggers.
- A durable ledger across exams for every activation, and binding the confirmations to model identifiers. Not built: the bound
  at confirmation (three a day for each exam, two looks a day apart) is the one that existed for unfilmed skills, and a change of
  `VERIFIER_VERSION` makes the audit start again, which ends any waiting. The catch-up adds no confirmation: it settles books
  already confirmed.
- Compare the pages a lesson quotes (`cited`) instead of the pages it was written from (`sources`) for Watch. Not done: a
  rewrite from the same pages that happens not to quote one of them would lose its film, which is a change to ADR 0008's rule
  that this decision does not need. A refused page can never be among the accepted pages the new lesson is written from, so
  the two comparisons agree here.

## Consequences

A replaced lesson costs one rewrite and its narration; the film it had is lost to Watch until the owner films the new lesson.
Studio shows how many lessons are in this state, as the skills that still cite refused pages, and each replacement is in the
activity feed.

## Review triggers

Change it if a verifier or model change makes the audit refuse many pages of filmed lessons (the count Studio shows would
rise; the three-a-day bound limits it, but each replacement drops a film), if a replacement lesson is repeatedly rejected, or
if a lesson with refused pages is shown to learners for days before its replacement exists (then withhold it).
