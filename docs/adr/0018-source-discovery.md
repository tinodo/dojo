# ADR 0018 - Source discovery: every skill gets verified official pages

- Status: proposed
- Date: 2026-10-16
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feature/source-discovery`). The lead can reject or change it in review.
- Guardrails: EG-14 (every source is checked by a model and by Dojo, word for word), EG-37 (everything read from the
  web is data, never instruction), EG-39 (documentation only, checked at every redirect), ADR 0005 (add an exam),
  ADR 0006 (source drift), ADR 0008 (deeper lessons), ADR 0009 (every paid call reserves its maximum first).

## The requirement

The owner added DP-300 and saw "Only 81 of 94 skills have a verified official page", with a button and a price.
His rule: every study-guide skill is covered by official documentation; "not found" is a defect of Dojo, not a state
to show; no button, no price question; find the pages with a real web search and Learn's own search, not with a model
alone; change Dojo, never fix a course by hand; do not throw away the existing courses (more than 1,000 USD, mostly
narration and filming) without a measured reason.

## What was measured (15 October, DP-300: 94 skills, 81 with a page, 13 without)

- The owner's own web search found 36 pages for the 13 skills. Dojo's search reached 5 of them: it searched SQL Server
  and Azure SQL Database only (the products of the study guide's links), while the exam also covers Managed Instance,
  Arc, VMs and Azure Migrate, and it asked one query per skill.
- Today's checker accepted 15 of 35 of those documentation pages: it refused overview and concept pages, so even
  perfect retrieval left 3 of 13 skills without a page. (One of the owner's lists was wrong in meaning: "Monitor and
  troubleshoot deployments" is in the group "Automate deployment of database resources" and means ARM, Bicep and CLI
  deployments. The checker was right to refuse the Azure Monitor pages.)
- Hold-out recall on the 81 sourced skills: today's search 83%, unscoped deeper API search 72%, Learn's MCP server 41%,
  table-of-contents BM25 36%, the union 88%.
- A graded checker (`teaches` / `covers` / `no`, with exam, domain and group as context) on 39 positive and 36 negative
  pairs: the gate model alone accepted 34/39 and refused 35/36; the author model alone 39/39 and 36/36; DeepSeek, a
  third family, 25/39 and 34/36 with many quotes that are not on the page (not usable). **Both models must agree: 34/39
  positives accepted, 0/36 negatives accepted, 20/20 skills with an accepted page, 15/20 with a page both graded
  `teaches`.**
- Web search through Foundry's Responses API works on the `author` deployment with the app's own Entra token: no agent,
  no Bicep change. With `max_tool_calls: 1` a call takes about 5 s and 10,000 input tokens and returns about 11
  sources. The `gate` deployment (Grok) refuses every Responses request with the web search tool. The author
  deployment answered 429 (token rate limit) on back-to-back unrestricted calls.

## The decision

Models propose search phrases; search engines and the documentation's own tables of contents supply candidate pages;
Dojo fetches every page and two model families judge it; Dojo keeps the books.

- **Retrievers** (`app/sourcing/retrieve.py`, `inventory.py`): Learn's search API (narrowed to the exam's products and
  not), Learn's MCP server (optional), BM25 over the exam's inventory (the `toc.json` trees of the study guide's links
  and of the top hits of the global retrievers; never `/previous-versions/`), and web search (R4:
  `Foundry.web_search`, restricted to learn.microsoft.com or docs.github.com, addresses only). GitHub exams use GitHub
  Docs' own search and web search; a GitHub page-list inventory is not built yet.
- **Planner** (`plan.py`): one author call per group writes up to five phrases per skill; later rounds are told which
  phrases failed and which pages were refused, by category.
- **Verification** (`verify.py`): a page is accepted only when the gate model AND the author model each grade it
  `teaches` or `covers` and each quotes a sentence Dojo finds on the page word for word. The page's grade is the lower
  of the two. A quote that is not on the page gets one repair: the model names a numbered sentence taken from the page.
  A page one model accepts and the other refuses is read again by both with more of the page; still split means
  refused. Nothing is accepted by rank, by agreement of searches or by one model.
- **Choosing the pages a lesson cites** (`_rank`, `Verifier.rank_pages`): every page of a round's pool is judged (the
  first version stopped at three accepted pages, and the first real run showed what that costs: all 13 test skills were
  `covered` after round 1, but 9 of the 43 pages they cited were neighbours, such as the HA/DR checklists for "monitor"
  and "troubleshoot", an Azure Pipelines task for "monitor and troubleshoot deployments", or Kusto's row-level security
  for an Azure SQL skill). With two or more accepted pages, both models then read them side by side, in address order,
  and each orders them by how directly they explain what the skill's verb and subject ask and names the pages that only
  belong to the same area, and names the pages a lesson on the skill can be written from mainly (`direct`: they explain
  what the skill's verb asks about its subject itself). The orders are added; a page is dropped only when both models
  drop it, and is direct only when both call it direct; the best page is never dropped; nothing is ever added by this
  step. On those 13 skills with the owner's pages mixed in, both models put the troubleshooting guides first and dropped
  the checklists, and put ARM's deployment history and common deployment errors above the Azure Pipelines task. The
  second real run (before `direct` existed) still ended every one of the 13 skills `covered` in round 1, but for 4 of
  them (monitoring and troubleshooting an HA/DR solution, monitoring and troubleshooting deployments) the cited pages
  were neighbours and the pages the owner's search found were never retrieved: nothing told the search that the first
  pages were not the right ones. That is what `direct` is for.
- **Covered, partial, exhausted, blocked**: a skill is `covered` when a page it cites (the best three after that
  comparison) has both grades `teaches` and both models call it direct, or when both models, each listing the skill's
  obligations with a verified sentence for each, say the cited pages together cover it. Accepted pages that were not
  direct are told to the next round's planner like refused ones ("only the same area"), so the next phrases look for
  what was missing instead of the same neighbours. Otherwise it is `partial`: its pages are written only when
  round 4 is done (the lesson checker refuses what a page does not support) and the search goes on. `exhausted` means
  every step of the four rounds ran and nothing was accepted: it says that the configured retrieval is exhausted, never
  that the documentation lacks a page. A step that fails three times makes the skill `blocked` for the day; a blocked
  skill is never called exhausted. The build adds an exam when at least one skill has an accepted page; it writes the
  `covered` skills and leaves the rest to the background.
- **Rounds** (`ladder.py`): 1 phrases plus all retrievers, every page of the round's pool judged (at most 40, which is
  what the searches can add); 2 new phrases that know the refusals and the accepted pages that were only the same area,
  refusals that several searches agree on judged once more with more of the page; 3 every title of the inventory read by
  the gate model (R7); 4 web search with the exam's wording and no domain filter. Web search asks with the planner's
  phrase for the skill and with its phrases for a product or deployment option ("troubleshoot an HA/DR solution" finds
  Azure SQL's pages; "... SQL Server on Azure VMs Always On availability groups" finds the troubleshooting guides of
  Always On). Exhausted and partial skills come back three days in a row, then weekly, with new wording.
- **Running it**: stage 0 of the background preparation, taking every other turn so lessons for the pages already
  found are written between its steps. The build job runs rounds 1 and 2 with progress as before and hands over.
  Per exam: a lease file with an epoch (a process that lost it writes nothing more), a ledger of steps keyed by cycle,
  round and step with an input fingerprint, at most 4 skills at once, 4,000 model calls a day (a cap pauses the work,
  it never ends a skill), the author's web searches one at a time with back-off. Every document carries its owner and
  epoch; a write checks the lease, reads the document on disk (a newer owner's document is never written over) and
  checks the lease again afterwards. The removal of an exam revokes the lease, holds the package lock, and deletes what
  a step wrote meanwhile (only a document of that owner). The preparer sleeps only until the search is next due (15
  seconds to 30 minutes), so a step that failed is retried after its own pause.
  **Accepted residual risk:** the files are on shared storage with no compare-and-swap, and the package lock is held
  inside one process. Production runs one container with one worker (App Service B1, `gunicorn --workers 1`), so the
  removal of an exam and the search run in one process and the lock covers them; a second process exists only while a
  restart overlaps. In that overlap a process that stalls between its last disk read and its write can write one stale
  search document, and a removal that runs in the other process within the few microseconds between `_activate`'s
  read of the package and its write would be undone: the removed exam comes back and the owner removes it again.
  Neither can put a wrong page into a package: pages reach a package only through `_activate`, which checks the lease,
  reads the package again and writes only what two models accepted. Closing it needs an incarnation id and removal
  tombstones in the package catalog, which this change does not build.
- **Nothing that exists is changed**: only skills without sources are written. A skill that has sources keeps them,
  and with them its lesson, narration and film. The exceptions are a skill whose cited pages both models refuse (found
  by the audit: both models read every cited page and each gave it no grade, not a failed quote and not a split; judged
  again a day later, three a day, and then replaced, filmed or not, [ADR 0021](0021-refused-pages-filmed.md)) and a
  `partial` skill, which may gain pages up to three, only while its lesson has no film. Readiness is unchanged except
  that it cannot say "book" while a skill has no page.
- **Screens**: the repair button, its price question and its route are gone. The exam page says in one line how many
  skills Dojo is still finding pages for. Studio lists every skill with its state, its accepted pages (grade, quotes,
  retrievers), blocked steps and the audit, and says whether web search works (one cached self-test a day, never at
  start-up).
- **Documentation only (EG-39)**: tables of contents are read documentation-only (the address and every redirect must
  pass `docs_url`, and a table of contents a page names outside the documentation is never asked for); a page that
  leads to `/previous-versions/` is refused after the redirect, before any model reads it, and the accepted pages read
  again for the comparison and the set check go through the same check (a page that moved or is gone is no longer
  accepted); the exam's title, which comes from the study guide, stands inside `<data>` in every prompt like everything
  else read from the web.
- **Covered means cited**: a skill is `covered` only by pages its package cites (`_activate` says whether it does).
  A `partial` skill that was written with pages can gain pages up to three while its lesson has no film; when it cites
  three already, or its lesson has a film, it is `settled`: Studio shows it as partly covered, the exam page no longer
  counts it as still being searched, and it is not searched again. Studio marks which accepted pages the package cites.
- **Counting and attribution**: every attempt to ask a model, a repeated malformed answer and a quote repair included,
  is counted against the exam's 4,000 calls of the day (`counted.Counted` wraps the models of a run); the threads that
  judge pages, compare them and scan titles run in a copy of the caller's context (`core.carried`), so the calls of an
  add-exam build are attributed to the owner like the rest of the job (Python 3.13 threads start with an empty context).
  The background search, like the rest of Dojo's shared background work, has no learner: its calls are booked to content,
  and the owner is counted and never refused (ADR 0009). The study guide's words reach the web search inside `<data>`
  with their angle brackets neutralised, under a fixed instruction that says they are material.
- **When pages change, the lesson follows**: a skill whose package cites a page its lesson was not written from (a page
  added to a `partial` skill, or pages that replaced refused ones) is written again by Deeper lessons (ADR 0008,
  `DeeperLessons.why` = "pages"), within its daily allowance, from all the cited pages; the earlier lesson stays the one
  shown until the new one passes every check. Nothing in the search writes or deletes a lesson.
- **A build from an older Dojo**: every check a build keeps says under which rules it was made (`v`); a build that was
  stopped before this change and resumed after it checks those skills again, so only pages two models accepted under
  the present rules reach a package.
- **Privacy**: web search receives public text only; `docs/privacy.md` says that Grounding with Bing is outside the
  Data Protection Addendum.

## Measured on 16 October (the 13 DP-300 skills the old search left without a page, real models, real web search)

- All 13 skills were `covered`, 12 of them in round 1 and 1 in round 2: 265 model calls in 37 minutes. 8 of 13 skills cite
  a page of the locked positives; the other cited pages (21) are not in the locked sets, so the measurement does not judge
  them: both models accepted them and Dojo found each quote on the page, and they are read by hand on the live site. Of
  the 6 locked hard-negative pairs that both models accepted in this run (a page of the right product for another
  operation, or an overview of a wider topic), the comparison kept 5 out of the cited pages. The sixth, MI
  `database-copy-move-how-to` for "implement a migration between Azure SQL services", is cited because both models call
  it direct: a borderline (it is the page for moving a database between managed instances), reported and not tuned.
- Per page (`verify` mode, 42 locked positives, 80 locked hard negatives): 38 of 42 positives accepted and 6 of 80
  negatives accepted, 20 of 20 skills with an accepted positive. The 6 are wrong-operation or overview pages that both
  models grade `teaches` or `covers`; the comparison step is what keeps them out of a lesson, and the number is reported, not tuned.
- A second run on the code that was merged (the last review rounds had raised the number of pages judged in a round
  and changed the web search wording): again 13 of 13 `covered`, 317 model calls in 53 minutes, no hard negative cited
  (8 accepted ones, all kept out of the cited pages), 7 of 13 skills cite a locked positive, 21 cited pages unlabelled.
- Real cost of the search, per skill, in that run: author 64,900 input and 7,800 output tokens (web search, phrases,
  the reading of every page, the comparison), checker 88,700 and 20,700; about $0.6 a skill at list price, and
  `costs.SOURCING_CALLS` holds these numbers. The first run had measured 58,300 and 6,400, and 67,700 and 15,700.

## Evaluation and the merge gate

`tests/data/sourcing/` holds locked sets (DP-300 with 94 skills, adjudicated positives and hard negatives of the same
product and wrong operation, a GH-300 and an AZ-104 set, and set-completeness cases), each label read from the page and
given a third-family opinion. They are not tuned on. `tools/source_eval.py` runs them against the real models. It must
be run, and every accepted hard negative explained, before any change to the verifier prompts, `VERIFIER_VERSION`, or
the gate or author deployment, like the grader bench of ADR 0016.

## Not done, and why

- Nested bullets of a study guide stay as they are (noted, not taken as skills): they are details of their parent skill,
  and failing the build on them would block legitimate exams. DP-300 has none.
- Grading (`learning._judge` can keep a grader `met` without a gate) is outside this change: it has its own ADRs and bench.
- A GitHub Docs inventory, embeddings over page sections and facet decomposition of skills that name several products.
- Pages cited before this change are audited page by page and reported. Pages both models refuse, twice, are replaced by
  the pages the search accepted ([ADR 0021](0021-refused-pages-filmed.md)): at first they were kept while the lesson had a film,
  which left a lesson that teaches the wrong thing for good.

## Consequences

More model calls than the old build (about 5 to 8 per skill and round), done once, in the background. The result is
measured against locked sets instead of asserted. A skill the documentation covers thinly shows as `partial` in Studio
instead of disappearing.

## Review triggers

Reject or change if a hard negative is accepted in the real-model run, if the author deployment's token rate limit
starves lessons, or if Learn's MCP interface changes (it is optional).
