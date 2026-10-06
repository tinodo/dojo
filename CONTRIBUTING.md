# Contributing to Dojo

Thank you for wanting to help. Dojo is a personal project with one maintainer. It is open so others can
learn from it, run their own copy and suggest improvements. Please read this page before you open an
issue or a pull request.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Small fixes** (a typo, a clear bug with a test): open a pull request.
- **Larger changes** (a new feature, a new dependency, a change to how evidence, grading, sign-in,
  storage or costs work): open an issue first and describe what you want to change and why. Wait for
  the maintainer's answer before you write a lot of code. Some ideas do not fit Dojo's rules (below),
  and it is better to find that out early.
- **Security problems**: never in a public issue. See [SECURITY.md](SECURITY.md).
- **Never paste secrets or personal data** into an issue, a pull request, a commit, a test or a log
  excerpt. That includes access tokens, feed and calendar links, tenant, subscription and object IDs,
  email addresses and anything from a learner's Record. Use placeholders such as `<tenant-id>`.

## Dojo's rules that a change must keep

These are design rules, not style preferences. A pull request that breaks one is not merged.

- **Official sources only.** Lessons and questions are written from Microsoft Learn and GitHub Docs
  pages. Dojo never uses real exam content, and never claims that a question is from an exam.
- **Quotes are checked word for word.** Every claim a lesson or answer key makes carries a quote that
  Dojo finds on the cited page. What fails is removed and logged, never shown.
- **An independent gate.** A model from a different family checks everything shown to the learner
  before it is shown. Grading quotes the learner's own words.
- **Evidence rules.** The Record is append-only and hash-chained. Multiple choice never fills a pip. A
  missing event is never read in the learner's favour. There is no pass prediction and no mapping to
  Microsoft's 700-point scale.
- **Team-era rules.** Every route belongs to exactly one route class (see the route inventory below).
  All learner state goes through the `Store` under `learners/<key>/`. In team mode every paid call
  reserves its maximum cost first. The owner never sees per-member activity.
- **Privacy in logs.** In Azure Dojo logs warnings and errors only, never one line per request, and
  never a path, id, token, name or email.

[docs/architecture.md](docs/architecture.md) explains how the pieces fit, and the
[ADRs](docs/adr/README.md) explain why.

## Pull request checklist

Copy this list into your pull request (the template does it for you) and tick what applies.

- [ ] **Tests pass** on your computer with the stub AI:
      `DOJO_LOCAL=1 python -m unittest discover -s tests -t .`
      (PowerShell: `$env:DOJO_LOCAL="1"; python -m unittest discover -s tests -t .`).
      New behaviour has a test. Tests never call Azure and never write to a real Record.
- [ ] **Secret scan passes:** `python tools/secret_scan.py`.
- [ ] **The front end parses:** `node --check app/static/app.js`.
- [ ] **Strict Content Security Policy kept.** No inline `<script>` or `<style>`, no `style="..."`
      attributes, no `on...=` handlers, no `eval`. Styles go in `app/static/app.css`; setting styles
      from JavaScript through the CSSOM (for example `el.style.width = ...`) is fine.
- [ ] **Route inventory.** Every new API route uses exactly one route-class dependency (`Me`,
      `Exit`/`ExitSeat`, `Admin`/`AdminSeat` or `Who` in `app/main.py`), or is added to the short list
      of public routes in `tests/test_team.py`. A new path parameter that names a learner's own object
      is added to `OWN_PARAMS` in `tests/test_team2.py`. The tests fail until this is done.
- [ ] **Plain English** in the user interface, in messages and in documentation: short sentences, no
      jargon where a plain word works.
- [ ] **An ADR for an architecture change.** A new kind of data, a new external service, a new open
      route, a change to evidence, grading, sign-in or money needs a new file in `docs/adr/` (take the
      next free number) and a line in `docs/adr/README.md`.
- [ ] **Documentation updated** where the change affects it: `README.md`, `docs/architecture.md`,
      `docs/operations.md`, `docs/privacy.md` or the module docstring.
- [ ] **No new dependency** without an agreed issue. If one is agreed: pin the exact version in
      `requirements.txt` and add it to `THIRD-PARTY-NOTICES.md`.
- [ ] **No secrets or personal data** anywhere in the change.

## How CI works

- `.github/workflows/ci.yml` runs on every pull request and on every push to a branch other than
  `main`. It installs `requirements.txt` on Python 3.12, runs the full test suite with `DOJO_LOCAL=1`,
  runs `tools/secret_scan.py`, and checks that both Bicep templates compile. It has read-only
  permissions and no secrets.
- `.github/workflows/deploy.yml` runs only on a push to `main` (that is, after a merge) in the
  maintainer's repository. It runs the tests again, deploys the infrastructure and the app to Azure
  with OpenID Connect, and waits for a smoke test. Pull requests from forks never deploy.
- Dependabot opens weekly pull requests for Python packages and GitHub Actions. They go through the
  same checks and reviews.

## Reviews

Every pull request needs a review before it is merged, and CI must be green. The maintainer merges.
Expect questions: a review checks the rules above as well as the code.

## Style

- Python: follow the code around your change. Type hints, small functions, plain names.
- Comments only where the code needs explaining. Each module starts with a docstring that says what it
  does and the rules it keeps; update it when those change.
- JavaScript: vanilla JS in `app/static/app.js`, no build step and no framework.
- Commit messages: a short first line in plain English, then what changed and why.

## Licence of contributions

By contributing you agree that your contribution is licensed under the project's licence (see
[LICENSE](LICENSE)).
