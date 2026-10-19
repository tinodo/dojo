# ADR 0024 - Studio's Checks tab is a few screens, and what the owner presses is at the top

- Status: proposed
- Date: 2026-10-19
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `fix/checks-page-height`). The lead can reject or change it in review.
- Guardrails: ADR 0019 and ADR 0020 (Studio is the owner's page, and he presses its buttons), ADR 0023 (the lab database switch).

## The requirement

The owner opened Studio's Checks tab to find the new Lab database switch and wrote that the page was "like 82 pages tall" and
that he had to find a button somewhere on it. He was right. The switch was drawn after the pipeline strip, the voice check, the
source changes, the deeper lessons and the official pages, and the page was long for two reasons that had nothing to do with it:

- The log of blocked text ("What was blocked, and why") is a panel that scrolls, but the blocks of the page never shrink
  (`.app > .main > * { flex-shrink: 0 }`), so it was as tall as its entries: up to 400 (`quality_log`).
- Source changes drew every change Dojo keeps (`CHANGES_KEPT`, 60) with its quotes and its lessons, and Film again drew every
  lesson that waits for a film.

## The decision

- **What the owner presses comes first.** The Lab database panel is the first block of the Checks tab.
- **A lesson that waits for a new source is named.** When every official page of a skill is gone or moved, its lesson waits and
  the owner updates the package's source. Studio only counted them, and named them only inside the change that found the page
  gone, which the cap now folds away and the 60 kept can lose. `summary()` gains one list, `stuck` (package, skill, lesson,
  title), and the tab names them above the history, five shown and the rest under one line.
- **A lab that needs a look has one action row**, made from `d.labs` (the labs flagged now) and drawn above the history. The
  change blocks only show its state. Before, every change that touched the lab had its own "I looked at it", and a change that
  was no longer one of the 60 kept took the button with it.
- **A long list shows its first rows and keeps the rest under "Show the other N ..."** (`capped`): source changes 4, lessons
  waiting for a film 5, films approved 3. A lesson whose last try failed is always shown, because the owner can approve it
  again. One row is never hidden behind a line of its own. The film button and its price come before the list, and what it
  approves is still every lesson, not the ones shown.
- **The log is a box** (`logbox`: `max-height: min(70vh, 640px)`, a region the keyboard can reach), on the owner's Checks tab and on
  a member's Studio log alike.
- **The rest stays open** while a block is drawn again after a press, and is closed again when the tab is opened anew.
- The cap is on the client side. The one addition to the API is `stuck`; nothing else in its payload changes.

## Options considered

- Every section of Checks collapsed under one line. Not now: it hides numbers the owner reads, and three lists were the cause.
- The switch on the Courses tab. Rejected: that tab is for making a course; an operational switch does not belong there.
- A cap on the server. Not now: the changes are already capped at 60. Measure `/api/studio/drift` first, then add a compact
  or lazy query rather than cutting the list silently.

## Consequences

The tab is a few screens. Three lists need a press to be read in full. What still grows with more courses is not capped: one
Deeper block for every exam and one summary for every added exam.

## The rule from here

What the owner must press on a Studio tab goes before its history. A list on a Studio tab that has no bound of its own gets a
cap and a "Show the other N". `tests/test_checks_page.py` draws the tab in node and counts the rows a person sees apart from the
ones behind a line.

## Review triggers

The Checks tab is long again; an exam is added and a block for every exam makes it long; the owner wants the history in full.
