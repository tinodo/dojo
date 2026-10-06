# Security policy

## Report a vulnerability privately

Please do not open a public issue, discussion or pull request for a security problem.

Use GitHub's private vulnerability reporting instead:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the problem, how to reproduce it, and what an attacker could do with it.

Only the maintainer sees the report. You will get an answer as soon as the maintainer can give one.
This is a personal project with one maintainer, so there is no fixed response time.

Never put secrets, access tokens, feed or calendar links, or anyone's personal data in a report. If you
found such data, say where it is and what kind of data it is, not the data itself.

## Supported versions

Only the `main` branch is supported. There are no releases and no back-ported fixes. A fix lands on
`main` and is deployed from there.

## Scope

In scope:

- The application code in `app/` and its static front end in `app/static/`.
- The infrastructure templates in `infra/` and the GitHub Actions workflows in `.github/workflows/`.
- The tools in `tools/`.
- Design flaws that let one learner see or change another learner's data, let anyone bypass sign-in,
  or let someone spend the operator's money without being admitted.

Out of scope:

- Problems in Azure, GitHub, Microsoft Entra ID or other services Dojo uses. Report those to their
  owners.
- Problems in third-party packages, unless Dojo uses them in an unsafe way. Report the package problem
  upstream.
- Someone else's deployment of Dojo. Its operator runs it, not this project.
- Findings that need the operator's own administrator access. The operator can read the stored data by
  design; `docs/privacy.md` says so.
- Denial of service by sending many requests, and social engineering.

Do not test against a deployment you do not run. Run Dojo yourself (see `docs/operations.md`), or on
your own computer with the stub AI (`DOJO_LOCAL=1`, see `README.md`).

## No bounty

This project has no bug bounty and pays no rewards. With your agreement, the fix will credit you.

## What helps

- The commit you tested.
- The route, the request and the response, with any token replaced by `<token>`.
- Whether team mode was on (`DOJO_TEAM=1`).
- What you expected to happen, and what happened.
