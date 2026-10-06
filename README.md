# Dojo

Dojo is a personal AI exam coach for Microsoft and GitHub certification exams. It teaches each skill from
the official documentation, asks you open questions about it, judges your answers point by point, and
keeps an honest record of what you have shown you can do.

The first exams are **GH-300 (GitHub Copilot)** and **DP-800 (Developing AI-Enabled Database Solutions)**.
You can add any other Microsoft or GitHub exam that has an official study guide.

> **Dojo is not an official Microsoft product and is not affiliated with or endorsed by the Microsoft or GitHub certification programs.**
> Its questions are written by Dojo from public, official documentation. They are **never real exam
> questions**, and Dojo never uses leaked or "dump" content.

**Read next:** the [user guide](docs/user-guide.md) · the [architecture](docs/architecture.md) ·
[running your own Dojo](docs/operations.md) · the [decision records](docs/adr/README.md) ·
[privacy](docs/privacy.md) · [security](SECURITY.md)

## What Dojo is, and what it is not

Dojo **is**:

- a coach for one learner (or, optionally, a small team) preparing for a certification exam;
- grounded only in official sources: Microsoft Learn and GitHub Docs;
- strict about evidence: it tells you what you have shown, under which conditions, and nothing more;
- a small web app you host yourself on Azure, or run on your laptop with a stub AI.

Dojo **is not**:

- a source of real exam questions, or a "brain dump";
- a pass predictor: it never gives a score, a probability of passing or a "you are ready" number;
- an official training product, or a replacement for Microsoft Learn;
- a multi-tenant service: it is built for one owner on one small App Service.

## Features

Each feature links to the decision record (ADR) that explains it.

- **Skills from the study guide.** Every skill of the exam, word for word from the official study guide,
  with its domain weight.
- **Add an exam.** Type an exam code. Dojo reads the official study guide, shows what building the course
  would cost at list price, and builds it when you agree ([ADR 0005](docs/adr/0005-add-an-exam.md)).
- **Learn.** A lesson per skill, written from the official pages it cites. Read it, listen to it as a
  two-voice conversation, have it presented as narrated slides, or watch it as an avatar video.
  - **Deeper lessons** use up to three official pages per skill ([ADR 0008](docs/adr/0008-deeper-lessons.md)).
  - **Deeper Listen** is a six-minute audio conversation that teaches the whole lesson
    ([ADR 0007](docs/adr/0007-deeper-listen.md)).
- **Practice.** New open-answer questions every time. Hints are there when you want them, and every hint
  you open is recorded as help.
- **Spoken answers.** Say your answer out loud instead of typing it ([ADR 0001](docs/adr/0001-microphone-for-spoken-answers.md)).
- **Ask.** Ask the coach about a skill. It answers only from that skill's sources and shows the exact words
  it relied on.
- **Practice exams.** Questions in the real exam's formats, spread by domain weight, with sections and a
  clock like the real exam ([ADR 0012](docs/adr/0012-exam-realism.md)).
- **Know what you know.** "How sure were you?" after an answer, a calibration view, and reviews at growing
  intervals ([ADR 0011](docs/adr/0011-know-what-you-know.md)).
- **Labs for DP-800.** Small SQL exercises in a private Azure SQL sandbox, checked by the database itself
  ([ADR 0003](docs/adr/0003-dp800-lab-sandbox.md)).
- **Plan.** Study sessions planned into the free times you set, also as a private calendar feed for
  Outlook ([ADR 0004](docs/adr/0004-private-calendar-feed.md)).
- **Podcast.** A private podcast feed per exam, one episode per skill, for any podcast app
  ([ADR 0002](docs/adr/0002-private-podcast-feed.md)).
- **Phone.** Install Dojo on your phone, get at most one nudge a day if you ask for it, and do a 5-minute
  session ([ADR 0013](docs/adr/0013-phone.md)).
- **Play.** Mastery moments, a weekly rhythm and study points that reward effort, never luck
  ([ADR 0010](docs/adr/0010-play.md)).
- **Record.** An append-only, hash-chained log of everything you did. You can export it or delete it.
- **When a source changes.** Dojo reads every cited page again about once a day. When the words change, it
  checks every quote again and rewrites what no longer holds ([ADR 0006](docs/adr/0006-source-drift.md)).
- **Quality log and Studio.** Everything that was removed, held back or failed, and why. What each kind of
  work costs at Azure list price.
- **Team mode** (off by default). Several colleagues in one Dojo, each with their own isolated record
  ([ADR 0009](docs/adr/0009-team-dojo.md)).

## How Dojo keeps the AI honest

- **Official sources only.** Lessons, questions and answers come only from Microsoft Learn and GitHub Docs
  pages that the exam's skills cite. Training material and other sites are never read.
- **Quotes checked word for word.** Every claim carries a quote from its source page. Dojo itself, not a
  model, checks that the quote is on the page. When a page changes later, Dojo checks again.
- **An independent gate.** Three model families each have one job:

  | Role | Job |
  |---|---|
  | author | writes lessons, questions, hints and answers to Ask |
  | gate | checks independently everything the learner will see against the sources |
  | grader | judges answers point by point, quoting the learner's own words |

  A different model family checks the author's work, so one model's blind spot is less likely to pass.
  What fails a check is removed and logged in the quality log. It is never shown. The defaults are in
  [`infra/main.bicep`](infra/main.bicep); see [architecture.md](docs/architecture.md#ai-model-roles).
- **Evidence, not scores.** For each skill Dojo shows four pips: shown *with help*, *on your own*, *later*
  (again after time has passed) and *in a new situation*. Every attempt is shown with its conditions. A
  multiple-choice answer never fills a pip, because a right answer can be a guess.
- **No pass prediction.** Dojo gives advice on whether to book the exam, with every rule and its current
  value, so you can disagree with the reasoning. It never maps anything to Microsoft's scaled score.

## Status

- Dojo is used by its owner, one learner, on one Azure App Service.
- **Team mode exists but is off by default.** Do not switch it on until a privacy owner (and, where they
  apply, works councils) has reviewed and accepted [docs/privacy.md](docs/privacy.md). The operator of a
  Dojo can technically read everything it stores. See
  [switching team mode on safely](docs/operations.md#switching-team-mode-on-safely).
- The project is maintained by one person in spare time. Expect slow answers to issues.

## Quick start on your laptop

You need Python 3.12 and Git. No Azure account is needed: `DOJO_LOCAL=1` uses a stub AI, stub speech and a
fake signed-in owner.

```powershell
git clone https://github.com/tinodo/dojo.git
cd dojo
python -m venv .venv
.venv\Scripts\Activate.ps1          # on macOS or Linux: source .venv/bin/activate
pip install -r requirements.txt
$env:DOJO_LOCAL = "1"               # on macOS or Linux: export DOJO_LOCAL=1
python -m uvicorn app.main:app --port 8000 --no-access-log
```

Open <http://localhost:8000>. The stub AI is a stand-in made for the tests: it echoes source text, so the
content reads oddly, but you can click through the app. Data goes to `.dojo-data/` in the repository. To use real models from your
laptop, see [local development](docs/operations.md#local-development-and-tests).

## Running the tests

```powershell
$env:DOJO_LOCAL = "1"
python -m unittest discover -s tests -t .
python tools/secret_scan.py
node --check app/static/app.js
```

CI runs the same checks on every pull request.

## Hosting your own Dojo on Azure

Dojo deploys with Bicep through GitHub Actions. A one-time bootstrap script creates the identities, the
sign-in app registration and the repository secrets that hold their IDs; after that every push to `main`
deploys. No passwords or keys are stored: the pipeline and the app sign in with managed identities and
OpenID Connect.

Read [docs/operations.md](docs/operations.md) for the steps, every setting, backups and troubleshooting.

## Costs

You pay normal Azure prices for what you use: a B1 App Service plan, the AI models, Speech, avatar video
and (for labs) about USD 23 a month for the SQL sandbox's network and security add-ons. Dojo's Studio page
shows its own estimates at Azure **list price**; your bill is in Azure Cost Management. Background work has
daily limits, and avatar videos are only filmed after you approve their price. See
[costs and cost controls](docs/operations.md#costs-and-cost-controls).

## Limitations

- Dojo is only as good as the official documentation. A skill without a usable official page is not
  taught.
- Questions are written by AI and checked by AI and by quote matching. A wrong question can still get
  through. You can dispute a judgement, and the quality log shows what was removed.
- It runs as one process on one instance. It is not built to scale out.
- Labs exist only for DP-800.
- The interface and content are in English.
- Speech, avatar video and the models are Azure services. The real thing does not run without Azure.

## Security and privacy

- Report a vulnerability privately: see [SECURITY.md](SECURITY.md). Do not open a public issue.
- What Dojo stores, who can see it and how to delete it: [docs/privacy.md](docs/privacy.md).

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and open an issue before a larger
change. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## License and notices

Dojo is released under the [MIT License](LICENSE). Fonts, Python packages and quoted documentation
come with their own terms: see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Microsoft, Azure, Microsoft Learn, GitHub and GitHub Copilot are trademarks of the Microsoft group of
companies. Their use here only describes what the exams and services are.
