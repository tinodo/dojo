# ADR 0027 - The project site is built from the repository and holds nothing private

- Status: accepted
- Date: 2026-10-29
- Decision owner: the Dojo lead, for the owner
- Decision: asked for by the owner on 29 October 2026; the design was read by two reviewers from two model families in two rounds before anything was built.
- Guardrails: EG-9 (GitHub holds engineering assets, not learner data), EG-29 (no surveillance: the site has no analytics), EG-33 (checks protect
  consequential boundaries), EG-39 (source terms: only the domains of a study guide are shown, never its skill statements), EG-41 (a personal project,
  kept apart from any corporate estate), EG-43 (no new dependency; every action pinned), EG-44 (a Content-Security-Policy, least privilege);
  `docs/ui-principles.md` (rules 11 and 12 apply to the site's look and words).

## The requirement

The owner, 29 October 2026: "I also see that the project on Github does not have a github pages part. Can you create (and maintain) that as well?"

Dojo is open source and public, and a person who finds the repository sees only a README. A visitor has three questions: what is Dojo and is it for me,
how does it teach and show what I know, and how do I run it and what does it cost. The README answers them for a developer who reads Markdown on GitHub.
A site can answer them for someone who does not, in the look of the app. "Maintain" is the harder half: a site that is written once says what Dojo
was, not what it is.

## The decision

**A small site on GitHub Pages, at https://tinodo.github.io/dojo/, that is built from the repository on every push to `main`.**

- **Four pages and a 404.** Home (what Dojo is, what you do in it, what the record shows, how a lesson is made, the exams, how to run your own, where Dojo
  stops), Exams (the domains and weights of each study guide), Documentation (the documents of the repository, read on GitHub) and Decisions (the ADRs, read
  on GitHub). English, like the app, the README and the documents. No JavaScript, no cookies, no analytics, nothing loaded from another site.
- **The look of the app.** The same colours and fonts (the fonts are copied from `app/static/fonts` at build time with their licence texts, never stored
  twice). Rules 11 and 12 of `docs/ui-principles.md` apply: no bar down the side of a box, one box for everything, no wording that vouches for Dojo, plain words.
- **A generator, `website/build.py`, standard library only.** It reads the templates and the stylesheet in `website/`, the exam package files (named fields
  only), `docs/adr`, the fonts and the icon of the app, four constants read from the code (pages per skill, hours for "later", how often a page is read
  again, the Python version) and the commit. It writes `_site/` (ignored by Git). A placeholder that is unknown, or a link to a file that does not exist,
  fails the build.
- **Generated, so it cannot drift:** the exam list and its numbers, the Decisions table (the number, the ADR's own title, its status), the four constants and
  the footer (which commit built the site). Written by hand: the sentences in `website/templates/`, and one line for each document in
  `website/content/docs.json`. A test fails when a document is missing there, and when a decision title holds a word from the insides of Dojo (then
  `website/content/decisions.json` gives a plainer one).
- **Publishing, `.github/workflows/pages.yml`.** On a push to `main` (and by hand): the site tests, the build, and GitHub's own Pages actions, every one pinned
  to a full commit hash (the repository requires it). The build job can only read; the deploy job alone may write Pages. Pages was switched on once, before the
  first run, with `gh api -X POST repos/tinodo/dojo/pages -f build_type=workflow`, and the `github-pages` environment is limited to `main`.
- **The app is not restarted for a site-only change.** `deploy.yml` ignores a push that touches only `website/` and the two site workflows. Any other change,
  including the README, the documents and the site tests, deploys the app as before. `ci.yml`, and its required check `test`, are not filtered.
- **A weekly check of the outside addresses, `.github/workflows/site-links.yml`.** The exam pages and study guides on Microsoft Learn move now and then, and
  nothing in the repository would notice. `website/check_links.py` fetches each address on learn.microsoft.com and docs.github.com once a week and fails on a
  connection error, on any answer that is not 2xx (403, 429 and 5xx only say the other side is busy and are reported, not failed), and when an address now
  ends at a different page.

**What the site holds:** what the repository already makes public (the README's story, the documents, the ADRs, the exam names and domain weights, the
numbers in the code), and one real sentence from GitHub Docs with its address and licence, as an illustration of a claim and its quote.

**What the site never holds:** the live app's address, tenant, subscription or object ids, resource names, email addresses, text from the live Record or from
lessons, screenshots of the live app, the skill statements of the study guides (they are Microsoft's, EG-39), analytics. `tests/test_site.py` looks for each
of these in every file the build writes (a fixed list of paths), and reuses the patterns of `tools/secret_scan.py`.

**Keeping it true:** AGENTS.md rule 15 (a change that alters what a learner or a host sees updates the site in the same pull request), a line in the pull
request checklist, the generated parts, the tests, and the weekly link check.

## Options considered

1. **A Jekyll theme through GitHub's own build.** No code to write, but the look is not Dojo's, the numbers would be typed by hand, and nothing would test it.
2. **Render the documents and ADRs as pages.** Richer, but it needs a Markdown renderer (a new dependency, or one written here) and a copy of each document
   that ages. GitHub already renders them well. Not for now; the Documentation and Decisions pages link to them.
3. **A separate repository or a `gh-pages` branch.** Splits the source of truth, so the site and the app could disagree.
4. **Hand-written HTML only.** Simple, but the exams, the decisions and the numbers would go out of date the first time one changed.
5. **Nothing.** The README stays the front door. The owner asked for a site.

## Consequences

- There is a public web surface, with the repository's name on it. It is read-only and static, and it holds nothing that is not already public.
- Every push to `main` runs a one-minute site build in addition to the app deploy. A site-only change runs only that.
- A new exam package, a new ADR or a changed constant changes the site without anyone touching `website/`. A new document, or a decision title with a word
  from the insides of Dojo, makes a test fail until a line is added: that is the intended reminder.
- Screenshots of the app are not in this version. When they are added they come from the local app with made-up data, never from the live one.

## Review triggers

A custom domain, a request for analytics or a contact form, screenshots, rendering the documents, a fifth page, a change in how GitHub Pages is published,
or a visitor who cannot find what they came for.
