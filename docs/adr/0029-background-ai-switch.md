# ADR 0029 - One switch for the AI work Dojo does by itself

- Status: accepted (built, and switched off by the owner's order on the day it was built)
- Date: 2026-11-18
- Decision owner: the Dojo lead, for the owner
- Decision: the owner, 18 November 2026. After the added exams had cost more than he expected and Present and Studio did not work well for him, he
  told the lead to stop all the AI that Dojo does on a schedule: it is not worth it.
- Guardrails: ADR 0005 and 0018 (an added exam's pages and lessons are made in the background), ADR 0006 (a changed page holds its lessons back),
  ADR 0007 and 0008 (Deeper Listen and deeper lessons), ADR 0014 (decks), ADR 0020 (films are approved with a price), ADR 0009 (budgets),
  EG-8 and EG-10 (a change reaches Azure only through the pipeline).

## The requirement

Dojo makes much of its content without anyone pressing a button: it looks for the official pages of an added exam, writes and rewrites lessons,
narrates them, writes Deeper Listen, draws decks when the owner has turned that on, keeps a pool of practice questions topped up and checks what the voices
said. Each of these spends money on the same Azure resource, inside daily limits. The owner needs to be able to stop all of it at once, and to start it again
with the same one move, without taking Dojo apart. Until now every service had its own limit and none had an off.

## What runs by itself

| Service | Spends on | With the switch off |
|---|---|---|
| Preparation (`prepare.py`): the search for an added exam's pages, lessons, rewrites, deeper-lesson upgrades, narration, Deeper Listen, decks drawn ahead of time | models, speech, web search | Stops. Only the filming of lessons the owner approved goes on (avatar video, at the price he saw) |
| Question pool (`exam.py`, `PoolKeeper`) | models | Does not start |
| Deeper lessons search (`deeper.py`) | models, web search | Does not start |
| Delivered-meaning check (`delivery.py`) | speech (it transcribes Dojo's own audio and compares the words) | Does not start |
| Self-test (`selftest.py`) | three models and Speech, under a cent | Runs once at start, which `/healthz` and the deploy check need, and no longer every 6 hours |
| Source-drift check (`drift.py`) | no model | Keeps running |
| Price lookup, nudges, the sweepers, the lab database mirror | no model | Unchanged |

What the owner or a member asks for is not on a schedule and is not touched: Ask, a practice exam, a check, judging, Listen when it is played, and the deck of
a lesson when it is first opened in Present.

## Options

| Option | Verdict |
|---|---|
| Lower each service's daily limit | Rejected: seven limits to keep in step, and none of them is zero |
| A button in Studio | Rejected for now: Studio's pages are designed before they change (rule 13), and a button that a redeploy could undo is a poor off-switch |
| **One setting of the deployment, read once at start** | **Chosen:** the same kind of switch as team mode (ADR 0009): off or on at the next deploy, in the open, in a reviewed repository variable |
| Remove the services from the code | Rejected: it is a decision about this month, not about the product |

## The decision

**Add one setting, `DOJO_BACKGROUND`, that the deployment sets from the Bicep parameter `backgroundAi`. `deploy.yml` fills the parameter from the repository variable
`DOJO_BACKGROUND_AI`.**

- **On is what Dojo always did.** The variable unset, empty, `on` or `true` (any case) leaves everything as it was. Nothing else about the services changes.
- **Any other value is off.** `off` is the word to use. A typo such as `of` also switches it off, and the deploy log prints `background AI: false`. A typo in the
  other direction would keep spending, which is the worse mistake for a switch whose job is to stop spending. The app reads `DOJO_BACKGROUND` itself as off only when it is
  exactly `0`, which is what the template writes for false.
- **Off keeps one thing running:** the Preparer's thread files the films of lessons the owner approved at a price (ADR 0020). It skips the search for pages and
  every step that writes a lesson, narration, Deeper Listen or a deck. Its pause between steps is 30 minutes, and an approval wakes it. The pool, the deeper
  lessons search and the delivered-meaning check are not started. The scheduler of team mode gets the same list.
- **The self-test runs once.** `/healthz` reports it and the deploy's smoke test waits for it, so the first run stays. A failed run is repeated every 5 minutes until
  it passes, as before. After a pass it does not come back every 6 hours.
- **One warning at start** says that the switch is off. `/api/diag` shows `automatic` under the Preparer.

To switch off: `gh variable set DOJO_BACKGROUND_AI --body off --repo <github-owner>/<repo>`, then deploy (a push to `main`, or `gh workflow run deploy.yml`). To switch
on again: delete the variable, or set it to `on`, and deploy.

## Consequences

- **Cost:** the spend that nobody pressed for goes to zero. The self-test is the one exception: under a cent each time the app starts. Nothing else is paid for unless the owner or a
  member asks, or the owner approved films. The daily limits stay in the code for the day the switch is on again.
- **A lesson that a changed page holds back is not written again by itself** while the switch is off. Drift still reads the pages (no model) and still holds a lesson back
  when its page no longer supports it (ADR 0006); nothing replaces it until someone writes a new lesson for the skill or the switch is on. A held-back lesson stays held back.
- **An exam added while it is off** is still built when the owner adds it: that is his action, and the build checks the official pages once, with two models. Its lessons are
  not written ahead of time and the later rounds of the page search do not run; a lesson is written when someone asks for that skill. Its approved films, once it has lessons, are still made.
- **No film waits for a rewrite that will not come.** A lesson that is waiting to be written again on the deeper template is left out of the offer, and an approved film for it waits
  (ADR 0020, 0025). While the switch is off `Dojo.scheduled_rewrites()` is empty, so such a lesson is offered like any other and an approved film is made for the lesson as it
  is. A lesson that a changed page holds back, one written again in the last week, and one whose pages may still be replaced wait as before.
- **Practice exams still work.** Questions waiting in the pool are used first and the rest are written and checked when the exam is built (ADR 0009, "Money"), so a
  build with an empty pool takes longer. Nothing refills the pool by itself.
- **Studio says things that are not true while it is off.** The To do page says lessons, Listen and Deeper Listen make themselves, and the Diagrams card says
  "Waiting for its turn". Studio is not changed here. Its wording is designed first and goes in its own change. The diagrams switch can still be pressed; it is remembered and does nothing until
  the switch is on.
- **Present's deck is not part of this.** A deck is drawn when a lesson is first opened in Present, and it is the owner's action.

## How we know it worked

After the deploy that carries it: `/healthz` shows the new build; the web app's settings hold `DOJO_BACKGROUND=0`; the log holds the one warning and no new errors; the data
share receives no new lesson, narration, Deeper Listen, deck, search result or pool batch; the meter stays flat between the owner's own actions.

## Review triggers

The owner wants part of it back (the likely shape is one switch per service, or a monthly cap that stops everything at once); Studio is redesigned to say what is on and what is off;
the owner approves films while the switch is off and they do not start.
