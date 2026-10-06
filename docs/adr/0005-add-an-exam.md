# ADR 0005 - Adding an exam: a package is data, and only a checked page grounds a skill

- Status: accepted
- Date: 2026-09-30
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: accepted by the builder of the Add an exam feature under the owner's standing instruction to
  decide and build autonomously. The lead can reject or change it in review. Review triggers are below.
- Guardrails: EG-45 (certification targets are configuration, not architecture), EG-37 (retrieved content
  is untrusted instruction), EG-14 (a generated answer is not a verified Claim), EG-39 (rights), EG-43
  (dependencies), EG-12 (record consequential decisions)

## The requirement

DESIGN.md section 1: "You name an exam. Dojo reads the official study guide, collects the matching
Microsoft Learn and GitHub Docs pages, and builds you a complete course." Screen 12: type any Microsoft
or GitHub exam code; Dojo finds the study guide, the weights and the date the blueprint changes.

Until now an exam was a JSON file in `app/packages`, written by hand and shipped with a deploy. Adding
one from the app decides three things others must know: where a package lives, what a model may decide
in it, and what may cost money without the owner saying yes.

## The options

1. **Keep packages in git.** Every new exam is a code change and a deploy. Rejected: EG-45.
2. **Let a model read the study guide and write the package.** Fast, but a model can reword a skill,
   drop one or invent a weight, and its answer is not a verified claim (EG-14). Rejected.
3. **Read the study guide with a parser, and use models only to find teaching pages, each one
   verified** (chosen).

## The decision

**Where a package lives.** An added package is `packages/<code>.json` on the data share, next to the
record, never in git. It has exactly the schema of the built-in files, and `app/packages.py` checks it
against that schema whenever it is read. A file that fails is skipped and logged, not half used. A
built-in package cannot be replaced or removed. Every feature reads an added package the same way it
reads a built-in one.

**Reading the study guide.** `app/studyguide.py` parses the page with the standard library, as
`app/sources.py` does. It reads the domains and their weight ranges, the groups, the skills, the date the
skills are measured as of, the change log and the retirement notice. It also reads the certification page
for the exam length and the official practice assessment. Every skill is the study guide's own text. If
anything does not parse cleanly, Dojo builds nothing. A retired exam is refused. A blueprint that starts
in the future is used, and labelled as such.

**Finding teaching pages.** For each skill, candidates come from the study guide's own documentation
links and from the sites' own search (Microsoft Learn and GitHub Docs). They are fetched only through
`app/sources.py`, whose host list stays closed. The author model picks up to three candidates by number;
it cannot add an address. The gate model must copy a full sentence from each picked page that shows the
page teaches the skill. Dojo then finds that sentence, word for word, on the page itself (`quote_ok`).
Only then does the page become one of the skill's sources.

Only documentation is read, never training (EG-39). Before anything is fetched, `docs_url` in
`app/sources.py` resolves dot segments, including encoded ones such as `%2e%2e`. It refuses a path that
leaves Learn's `/en-us/` documentation or GitHub Docs' `/en/`, and one that lands in `training`,
`credentials` or another non-documentation area. The same rule is checked again at every redirect,
before Dojo follows it. A page that ends up anywhere else is neither stored nor used.

Candidates follow, citations do not (`candidate` and `get` in `app/sources.py`). A candidate is an
address Dojo is still choosing: a search hit or a study guide link. If it leads to another documentation
page, for example an old Learn address that now leads to its replacement, Dojo judges the page it ends
at. That page is stored under its own final address and, if it passes, cited there, never under the old
one. Two candidates that end at the same page are one candidate. A candidate that ends on the other site
is read again with that site's own reader. A candidate whose stored copy is marked gone is read again,
never chosen from that copy. A redirect into training or anywhere else is still refused. A cited address
never follows: it stores only its own page, and a cited page that now leads elsewhere counts as moved
(ADR 0006).

A stored page records its final address, and an older stored copy without one is fetched again when a
skill needs it, not at startup. That older copy may hold a training page's text, so Dojo never uses it. If the
fetch fails, the source counts as unavailable, just as if Dojo had never read it. Only a copy whose
final address is documentation can serve as the last good copy. Lessons already built stay readable.

A skill that no page passes for stays in the package, so the blueprint stays whole. It has no sources.
Dojo does not teach it, ask about it or put it in an exam, and it counts as not shown. A practice exam
never plans such a skill. If no skill in a whole domain has a source, Dojo offers no practice exam for
that exam, and says which domain is missing. An exam without a weighted domain would not show how ready
the learner is, so Dojo gives no such exam, not even as a practice set. Quick questions and lessons for
the other skills still work.

**Untrusted pages.** Everything read from the web reaches a model only inside `<data>`, with its angle
brackets neutralised, under a system prompt that says it is material, never instruction (EG-37).

**Skill ids.** An added skill's id is its position plus a short hash of its words, for example
`d1.g2.s3.54ea9b`. The page shows only the position. If an exam is removed and added again after its
blueprint changed, old evidence never attaches to a different skill.

**Readiness belongs to a blueprint.** Every package carries a blueprint mark: a short hash of its domain
ids, their weights, and its skill ids and words (`blueprint` in `app/packages.py`). Each new practice
exam records the mark of the package it was written for. Readiness rule 2 counts an attempt only if its
mark matches the package's mark now, and only if every current domain had at least 5 questions in it. A
question on a skill the package no longer has counts for nothing. So an exam that is removed and added
again with changed skills starts again, and one added again unchanged keeps its credit. Attempts made
before the mark existed have none. On a built-in exam they count as the current blueprint, because
those blueprints have not changed since, so the owner's existing attempts keep their credit. On an added
exam they never count: a missing mark is never read in the learner's favour. If a built-in package's
domains, weights or skills are ever edited, its older marked attempts stop counting, as they should.
Unmarked attempts on a built-in exam cannot be told apart and would still count, but rule 2 reads only
the last 30 days, so that fades within a month.

**Money.** Before the owner presses "Build this course", the page shows the list-price estimate for
finding pages, writing and checking the lessons, and narration. Each model's input and output tokens
are priced from its own meters in the Azure Retail Prices API: the model's name without its pinned
version, for the deployment type the models run on (`modelSku` in `infra/main.bicep`, Global Standard).
A line with no list price, such as a model with no public meter, is never guessed. It says "no public
list price for <model>; not included", and the total and the words above "Build this course" say what
the total leaves out. Under each total (building, filming, and Studio's "What this costs"), the page says
when the list prices were read. Dojo reads the list again after a day. If that fails, it uses the last
list and says so in the same place: the prices could not be refreshed and may be out of date. This is
never only inside "How Dojo estimates this". After that, the existing Preparer writes and narrates the
lessons. For new lessons and narration the exams take turns, and the daily limits and the job slots are
unchanged, so a new exam cannot starve the others. Rewrites after a source page changes go the learner's
active exam first, within their own daily allowance (ADR 0006).
Presenter videos cost much more. They are never filmed from here on their own. The owner presses "Film
the lessons (about $X at list price)", and that approves exactly the lessons in that offer. Lessons
written later need a new approval. Filming runs one lesson at a time, only after the owner's jobs have
been quiet for a few minutes, and stops before presenter videos fill 90% of their storage budget.

**Removing an exam.** "Remove this exam" asks first. It deletes the package, its build, its lessons,
narration, videos and question pool. The record stays: it is append-only, and the removal is one more
event (`package.removed`), as the adding was (`package.added`).

## Consequences

- A new exam needs no code change and no deploy. The owner decides and pays, per exam, on one page.
- Some skills may have no verified source, most often conceptual GitHub skills that search matches
  poorly. They are visible as "no verified source" in the Course, never hidden and never faked.
- Without a stated exam length, Dojo offers no timed practice exam for that exam, only quick questions.
  The same is true while a whole domain has no verified source.
- Licences: the pages are GitHub Docs and Microsoft Learn documentation, taken as CC BY 4.0, as for
  the built-in exams. Training modules are linked, never copied (EG-39).

## Review triggers

- Microsoft Learn changes the structure of its study guides, and the parser starts to refuse them.
- Many skills end without a verified source.
- The list-price estimates drift far from the bill in Azure Cost Management.
- A second user is added: added exams are shared by the whole app today.
