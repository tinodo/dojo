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
  belong to the same area. The orders are added; a page is dropped only when both models drop it; the best page is never
  dropped; nothing is ever added by this step. On those 13 skills with the owner's pages mixed in, both models put the
  troubleshooting guides first and dropped the checklists, and put ARM's deployment history and common deployment
  errors above the Azure Pipelines task.
- **Covered, partial, exhausted, blocked**: a skill is `covered` when a page it cites (the best three after that
  comparison) has both grades `teaches`, or when both models, each listing the skill's obligations with a verified
  sentence for each, say the cited pages together cover it. Otherwise it is `partial`: its pages are written only when
  round 4 is done (the lesson checker refuses what a page does not support) and the search goes on. `exhausted` means
  every step of the four rounds ran and nothing was accepted: it says that the configured retrieval is exhausted, never
  that the documentation lacks a page. A step that fails three times makes the skill `blocked` for the day; a blocked
  skill is never called exhausted. The build adds an exam when at least one skill has an accepted page; it writes the
  `covered` skills and leaves the rest to the background.
- **Rounds** (`ladder.py`): 1 phrases plus all retrievers, the best 20 pages of the pool judged; 2 new phrases that know
  the refusals, the next 20, refusals that several searches agree on judged once more with more of the page; 3 every
  title of the inventory read by the gate model (R7); 4 web search with the exam's wording and no domain filter.
  Exhausted and partial skills come back three days in a row, then weekly, with new wording.
- **Running it**: stage 0 of the background preparation, taking every other turn so lessons for the pages already
  found are written between its steps. The build job runs rounds 1 and 2 with progress as before and hands over.
  Per exam: a lease file with an epoch (a process that lost it writes nothing more; the lease is checked again after
  every write, and the removal of an exam revokes it, holds the package lock, and deletes what a step wrote meanwhile),
  a ledger of steps keyed by cycle, round and step with an input fingerprint, at most 4 skills at once, 4,000 model
  calls a day (a cap pauses the work, it never ends a skill), the author's web searches one at a time with back-off.
  The preparer sleeps only until the search is next due (15 seconds to 30 minutes), so a step that failed is retried
  after its own pause. One residual window is left and accepted: a process that stalls for longer than the lease's ten
  minutes between its check and its write can still write once; the check after the write then stops it.
- **Nothing that exists is changed**: only skills without sources are written. A skill that has sources keeps them,
  and with them its lesson, narration and film. The exceptions are a skill whose cited pages both models refuse (found
  by the audit: both models read every cited page and each gave it no grade, not a failed quote and not a split; judged
  again a day later, three a day, and replaced only while its lesson has no film) and a `partial` skill, which may gain
  pages up to three, again only while its lesson has no film. Readiness is unchanged except that it cannot say "book" while a skill has
  no page.
- **Screens**: the repair button, its price question and its route are gone. The exam page says in one line how many
  skills Dojo is still finding pages for. Studio lists every skill with its state, its accepted pages (grade, quotes,
  retrievers), blocked steps and the audit, and says whether web search works (one cached self-test a day, never at
  start-up).
- **Documentation only (EG-39)**: tables of contents are read documentation-only (the address and every redirect must
  pass `docs_url`, and a table of contents a page names outside the documentation is never asked for); a page that
  leads to `/previous-versions/` is refused after the redirect, before any model reads it; the exam's title, which comes
  from the study guide, stands inside `<data>` in every prompt like everything else read from the web.
- **Privacy**: web search receives public text only; `docs/privacy.md` says that Grounding with Bing is outside the
  Data Protection Addendum.

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
- Pages cited before this change are audited page by page and reported; refused pages are not removed from a skill that
  has a lesson, because that changes the lesson's source set.

## Consequences

More model calls than the old build (about 5 to 8 per skill and round), done once, in the background. The result is
measured against locked sets instead of asserted. A skill the documentation covers thinly shows as `partial` in Studio
instead of disappearing.

## Review triggers

Reject or change if a hard negative is accepted in the real-model run, if the author deployment's token rate limit
starves lessons, or if Learn's MCP interface changes (it is optional).
