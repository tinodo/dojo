## What this changes

<!-- One or two sentences in plain English. Link the issue, for example "Closes #12". -->

## Why

<!-- The problem it solves, or the decision it follows. For an architecture change, link the ADR. -->

## How it was checked

<!-- What you ran and what you looked at. Screenshots must not show anyone's data, tokens or links. -->

## Checklist

- [ ] Tests pass with the stub AI: `DOJO_LOCAL=1 python -m unittest discover -s tests -t .`
- [ ] New behaviour has a test; no test calls Azure or writes to a real Record.
- [ ] `python tools/secret_scan.py` passes.
- [ ] `node --check app/static/app.js` passes.
- [ ] Strict CSP kept: no inline scripts or styles, no `style="..."` or `on...=` attributes.
- [ ] Every new route has exactly one route class, or is added to the public list in `tests/test_team.py`; a new own-object path parameter is in `OWN_PARAMS` (`tests/test_team2.py`).
- [ ] Plain English in the UI, messages and docs.
- [ ] An ADR in `docs/adr/` (and a line in `docs/adr/README.md`) for an architecture change.
- [ ] Docs updated where affected (`README.md`, `docs/architecture.md`, `docs/operations.md`, `docs/privacy.md`, module docstrings).
- [ ] No new dependency without an agreed issue; if added, pinned and listed in `THIRD-PARTY-NOTICES.md`.
- [ ] No secrets, tokens, tenant/subscription/object IDs, emails or other personal data anywhere in the change.
