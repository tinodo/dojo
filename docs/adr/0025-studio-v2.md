# ADR 0025 - Studio is organised by what the owner asks, and money is shown as it is

- Status: proposed
- Date: 2026-10-19
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feat/studio-v2`). The lead can reject or change it in review.
- Guardrails: ADR 0019, 0020 and 0024 (Studio is the owner's page; he presses its buttons), ADR 0021 (refused pages), ADR 0023 (the lab
  database switch), `docs/ui-principles.md` (how a page for the owner is made).

## The requirement

The owner opened Studio's Courses tab and read "Videos 0 of 73 [Film 72 lessons · about $137.96] 1 lesson wait for a rewrite (a source
page changed)". He asked what it meant, whether it was safe to film or money down the drain if the lessons changed, what he had to do,
how the Courses tab related to the Checks tab, and what the Costs tab's numbers were: AI use, per month, caused by users, or the web
app and the database. He said the page was "a bunch of letters" with "no design and no logical flow". He was right on every count:

- Four tabs named for what Dojo had built (Courses, Activity, Checks, Costs), not for what he asks. Money buttons lived in three places.
- Words from Dojo's insides on the face of the page: held, template, withheld, rewrites, WER.
- Numbers without a unit, a period or a verdict. "Costs" showed list-price estimates and no bill at all.
- Nothing said whether a film was safe to make. A lesson that was rewritten after it was filmed made the film out of date, and the page
  offered films for lessons that were about to be rewritten (ADR 0020 named that as a gap).

Two independent design reviews of the first spec found more: a "Film again" card would have duplicated the per-course offer (the offer
already is "filmed before and rewritten" plus "never filmed"), a verdict that forecast ("more are due") cannot be computed from the data,
"nothing is paid until you press a button" was not true (lessons, audio and the daily checks cost money on their own), and a page that
says "everything is working" must not say so when a check could not be read.

## The decision

**Five pages, each the answer to a question.** `To do` (what needs me, and what is optional), `Courses` (is each course ready to study),
`Money` (what did Azure charge), `Health` (is everything working) and `Activity` (what did Dojo do). The tabs are links with the current
page marked (`aria-current`), not an ARIA tab list. The heading and the tabs are drawn at once and each page fills in under them; a read
that fails says so on its own. The old addresses keep working: `#/quality/checks` opens Health with the blocked sentences open,
`#/quality/costs` opens Money.

**To do.** Cards, one for each decision, in three groups by need and not by money:
- *Needs you*, costs nothing: a lab to look at (one row, "I looked at it"), lessons whose official pages are all gone (named, with where
  to act: a change to the course's source, not a button here), an exam build that stopped.
- *Optional · costs money*: one **Videos** card with a row for each course, one **Diagrams** card, one **Page check** card with a row for
  each course not checked. A row says what is offered, what was left out and why, how much of the price replaces a video the owner already
  has (Watch plays an earlier version's film of a rewritten lesson, with a note) and how much has none, and carries the one button with
  its price. After the cards, **Review all** asks once with a box for each line and a total that follows the ticks. It says that each
  selection starts on its own (one that is refused does not stop the others, and the others are charged), and when the total is more than
  Azure charged this month so far.
- Nothing to decide says so in one line.

**A film is offered only for a lesson that is steady.** The server leaves a lesson out of every film offer, with its reason, when
(a) a changed page holds it back and it is being rewritten (ADR 0006), (b) it is about to be rewritten on the deeper template
(`DeeperLessons.to_write_again`, the gap ADR 0020 named), (c) it replaced an earlier lesson of its skill less than `QUIET_DAYS` (7) ago
for any reason, or (d) for an added exam, its skill's pages may still be replaced (the search is still adding pages, an audit has not
looked yet, or both models refused them: ADR 0018, ADR 0021). A lesson written for the first time is steady at once unless (d) holds.
The rule is applied where the offer is made, in both paths (built-in courses: `DriftCheck.film_split`; added exams:
`NewExams._lessons` and `_queue`), so the token, the price and the approval always describe the same lessons. The filers skip an approved
lesson that has since been scheduled for a rewrite (and, for an added exam, one whose skill's pages are unsettled again); the older
"film again" endpoint follows the same rule. When the list of lessons about to be rewritten cannot be read, nothing is offered and nothing
is filed (the round is tried again a minute later); an approval is refused in plain words; the Courses reading keeps its other parts and
the To do page says the films could not be read; Stop and the exam page work as before. The lesson page's **Film this lesson** stays: it is the owner choosing one lesson, and it says when the lesson is likely to be
replaced. The server refuses it (409) for a lesson that is not the newest of its skill or that a changed page holds back. The exam page
has no button that spends any more: it says how many written lessons are not filmed, how many are left out and why, and links to the To
do page. Its older endpoints (`POST /api/exam-packages/{pid}/film`, `POST /api/studio/drift/refilm`) are still there with no button and
read the same offer, so a lesson that is left out is refused there too. The page says nothing about what will happen: it says what was
left out and when it comes back. No verdict, no forecast.

**Money asks first, in a sheet.** Every button that spends opens a dialog (`moneySheet`) that names the lines, the price, what was left
out, what a rewrite later would do, and the room the videos need. Cancel has the focus, so Enter never spends; Escape and a click beside
the box cancel. The lesson page asks the same way. `window.confirm` is not used to spend money. A film is approved at the price the page
showed: the token names the lessons and that price, so a changed price is another offer. **Diagrams and page checks are approved with the
word and the sheet only**: no token names them, because each is one batch (about $0.28 a lesson for diagrams, $2 to $3 for a course's
page check) and the server does not compare the price on the sheet with the price of the moment. A price that moved between the sheet and
the press is not refused; this is accepted and stays on the review triggers.

**The Money tab shows what Azure charged** for Dojo's resource group this month, before tax, read from the Cost Management query API
(`app/bill.py`). The app's identity gets the built-in role **Cost Management Reader** on the resource group, granted once through
`infra/bootstrap/app-roles.bicep` (the deploy pipeline cannot assign roles). One daily query grouped by service and meter follows its
continuation to the end (a total is never shown from part of the lines), one more asks for last month; the answer is kept six hours in
memory and on the share, one reading at a time, a "wait" from Azure is remembered across restarts, and the browser has no way to force
one. Lines are grouped by what they pay for in the owner's words, each part marked **Pay per use**, **Always on**, **Partly always on** or
**Free offer**. It shows what is always on a month, the quietest complete day, the busiest days, last month, and says that the last day
is still arriving, and the least the month can come to (what is spent plus what is always on for the days left). A link opens Azure's
budgets for the resource group, because Dojo cannot set a limit. Last month's total is read apart: if that read fails, this month's is
kept, and it is shown only when it is in one currency; after a month ends it is labelled "Last month, until this month is read". Without
the role it says what is missing; with an old reading it says since when; on the first day of a month it says nothing is reported yet
and never "$0". Dojo's own estimates (what a course would cost at Azure's published prices) are below it, open
only when there is no bill, and labelled as estimates. The Activity page's "What it cost" is relabelled Dojo's own count, not the bill.

**Health cannot lie.** Each check is a closed section with a chip and one plain line. "Everything Dojo checked is working" is said only
when every check could be read; a check that could not be read is named apart and is not a thing to look at; a check that stopped
(the daily page read older than 36 hours) is a thing to look at; slides worth a listen are information, not a to-do; a storage cap is
said as a cap ("the last 400 are kept"). Builds that stopped, failed films and a failed monitoring try count as things to look at, and
the top line says when the checks were read. The source-change section is called "Source changes". The lab database switch is one
visible line above the sections, never inside a closed one.

**The server adds**: `film_split` and `film_offer` (the rule), `Dojo.rewritten_recently` and `scheduled_rewrites`,
`Sourcer.unsettled`, `DriftCheck.owner_todo`, `todo`, `quiet_days` and `quiet_last` in `/api/studio/courses`, `/api/studio/bill`.

## Options considered

- A verdict (safe, careful, wait) from the share of lessons rewritten lately. Rejected: it forecast, tiny courses made percentages
  absurd, and a one-off upgrade wave inflated it. The offer itself is now the answer.
- A "Film again" card. Rejected: the offer already contains those lessons.
- "Make everything" as the first button. Kept, but as **Review all** after the cards, with a box for each line.
- Reading the bill with the owner's token in the browser. Rejected: the app's own identity reads it, the owner's page only shows it.
- Splitting the bill per course or per person. Not possible: Azure bills the resource group.

## Consequences

The first screen says what to do. A course with a recent wave of rewrites offers fewer lessons for a week, which is the point; the card
says which and until when. The filming of a brand-new course waits for the pages of its skills to settle only where the search or an
audit has not finished (`unsettled`). The Money tab needs the role: until the owner applies it once, it says "Dojo has no read access to
Azure Cost Management yet" and shows the estimates. Reading the bill costs two small API calls every six hours at most.

## The rule from here

`docs/ui-principles.md`. In short: the page answers one question of the owner; a card has one main action with its price; a number says
what it counts or is not shown; no word from Dojo's insides on the face of a page; every state is looked at with the owner's amount of
data before it is built. `tests/test_studio_v2.py` runs the pages in node and reads the words off the tree.

## Review triggers

The owner asks again what a number or a button means; the quiet period turns out to be too short or too long (7 days); the Cost
Management API changes its answer or throttles; an exam is added and a row for every exam makes the To do page long; the price of a
batch of diagrams or of a page check moves a lot between the sheet and the press; a second reviewer finds a sentence that is not true.
