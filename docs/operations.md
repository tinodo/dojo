# Running Dojo: self-hosting and operations

This guide is for the operator: the person who hosts a Dojo, deploys it and keeps it running. Read
[architecture.md](architecture.md) first if you want to know how the parts fit together. The learner's guide
is [user-guide.md](user-guide.md).

Dojo is built for one owner on one small Azure App Service. Everything below assumes that shape. The
examples use placeholders such as `<tenant-id>`, `<subscription-id>` and `<app-name>`. Replace them with your
own values. Never commit real identifiers or secrets.

## Contents

1. [What you need](#what-you-need)
2. [Make the repository yours](#make-the-repository-yours)
3. [Bootstrap (once)](#bootstrap-once)
4. [GitHub secrets](#github-secrets)
5. [The deploy pipeline](#the-deploy-pipeline)
6. [Configuration reference](#configuration-reference)
7. [Backups and restore](#backups-and-restore)
8. [Costs and cost controls](#costs-and-cost-controls)
9. [Switching team mode on safely](#switching-team-mode-on-safely)
10. [Upgrading](#upgrading)
11. [Troubleshooting](#troubleshooting)
12. [Local development and tests](#local-development-and-tests)

## What you need

- An Azure subscription. You need rights to create resource groups and role assignments in it
  (Owner, or Contributor plus User Access Administrator).
- A Microsoft Entra tenant where you may create an app registration and assign users to it. The sign-in
  app lives here. It is usually the subscription's tenant.
- Model quota in your region for the three models in `infra/main.bicep` (by default `gpt-5.5`,
  `grok-4-1-fast-reasoning` and `DeepSeek-V4-Pro`, all as GlobalStandard deployments). Check the
  Azure AI Foundry model catalog for availability. The default region is Sweden Central.
- Your own copy of this repository on GitHub (a fork, or a new repository with this code).
- On your machine: PowerShell 7, the Azure CLI with Bicep (`az bicep install`), the GitHub CLI (`gh`,
  signed in with admin rights on your repository), Git, and Python 3.12.

## Make the repository yours

A few names are fixed in the files. Change them before the first deploy:

| File | What to change |
|---|---|
| `infra/main.bicep` | `param name`: your app name. It becomes the host name `<app-name>.azurewebsites.net`, so it must be globally unique. Also the `tags` (for example `owner`). |
| `.github/workflows/deploy.yml` | `env.RESOURCE_GROUP` and `env.SITE`: your resource group and app name. |
| `infra/bootstrap/main.bicep` | The `tags`. The GitHub subject is not in the file: `bootstrap.ps1` derives it from `-Repo` (see below). |
| `infra/bootstrap/bootstrap.ps1` | `-Tenant` and `-Subscription` are required and have no defaults. Also pass your own `-Repo` and `-AppName` (or change their defaults); `-Location` defaults to `swedencentral`. |

The resource group names (`rg-dojo-identity-swc`, `rg-dojo-swc`) and identity names (`id-dojo-deploy`,
`id-dojo-app`) are defaults in the Bicep files. You can keep them or change them in all places at once.

**The GitHub subject.** The deploy identity trusts only tokens for pushes to `main` of your repository.
`bootstrap.ps1` asks GitHub which subject format the repository uses (`gh api
repos/<owner>/<repo>/actions/oidc/customization/sub`) and builds the subject from it. Repositories created
after 15 July 2026, or opted in, use the immutable format with numeric IDs:
`repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/main`. Older repositories keep
`repo:<owner>/<repo>:ref:refs/heads/main` unless they opt in (GitHub docs, "OpenID Connect"); opting in is
safer, because a repository re-created under the same name then never inherits your trust. A custom subject
template is not supported: the bootstrap stops. With the immutable format, a re-created repository has a new
ID, so it needs the bootstrap (or a new federated credential) again. A wrong subject makes
`azure/login` fail in the deploy workflow with a "no matching federated identity" error.

## Bootstrap (once)

`infra/bootstrap/bootstrap.ps1` sets up everything the pipeline cannot set up for itself. Run it once, as the
person who will own the Dojo:

```powershell
az login --tenant <tenant-id>
gh auth login
pwsh infra/bootstrap/bootstrap.ps1 -Tenant <tenant-id> -Subscription <subscription-id> `
    -Location swedencentral -Repo <github-owner>/<repo> -AppName <app-name>
```

The script talks to Azure and Microsoft Graph over REST with tokens for the tenant you name. It never changes
your Azure CLI default subscription. It refuses to touch a resource group that exists but is not tagged
`project=dojo`. Running it again is safe: it reuses what it made.

It creates:

1. **Two resource groups.** One for the identities and one for the app.
2. **Two user-assigned managed identities.**
   - The *deploy identity* has a federated credential for your repository's `main` branch. It has
     Contributor on the app resource group and Managed Identity Operator on the app identity.
   - The *app identity* is what the running app uses. It has Azure AI User, Cognitive Services OpenAI User
     and Cognitive Services Speech User on the app resource group.
3. **The same three AI roles for you**, the signed-in person, so you can run Dojo against the real models
   from your laptop.
4. **A sign-in app registration** named after the app. It is single tenant, issues ID tokens, and has the
   redirect URI `https://<app-name>.azurewebsites.net/.auth/login/aad/callback`. It has no client secret:
   a federated credential lets the app identity prove itself instead. User assignment is required, and only
   you are assigned.
5. **GitHub repository secrets** for the pipeline (next section). It never prints their values.

The bootstrap does not create the app, the models or the database. The first run of the deploy workflow does
that from `infra/main.bicep`.

## GitHub secrets

The deploy workflow reads these **repository secrets** (Settings → Secrets and variables → Actions →
Secrets). The bootstrap sets them with `gh secret set` and removes any older repository variables with the
same names. They are identifiers, not credentials: the pipeline signs in to Azure with OpenID Connect and
holds no password or key. GitHub masks secrets in the logs of new workflow runs, and no workflow step
prints them. GitHub does not pass secrets to workflows that run for pull requests from forks; only
`deploy.yml` needs them, and it runs only on `main`.

**Moving to secrets protects future runs only.** If these IDs were ever repository variables, or appeared in
a commit, they stay in the Git history, in pull request refs and in the logs of the runs before the change.
Before you make such a repository public, choose one of these:

- **Publish a fresh repository** from a reviewed snapshot of the files: no old history, no pull request
  refs and no run logs. This is the clean way.
- **Keep the repository**: delete the old workflow runs and their logs, and accept that the Git history
  still holds whatever was committed. Check that history first (for example with `git log -p`).

To set one by hand: `gh secret set AZURE_TENANT_ID --repo <github-owner>/<repo>`, then paste the value.

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | `<deploy-identity-client-id>`: the deploy identity |
| `AZURE_TENANT_ID` | `<tenant-id>` |
| `AZURE_SUBSCRIPTION_ID` | `<subscription-id>` |
| `DOJO_AUTH_CLIENT_ID` | `<sign-in-app-client-id>`: the sign-in app registration |
| `DOJO_OWNER_OID` | `<owner-object-id>`: your user object ID in the tenant |

The deploy workflow passes `authClientId` and `ownerObjectId` to Bicep from these secrets, and `teamMode` from
one **repository variable** (Settings → Secrets and variables → Actions → Variables), `DOJO_TEAM_MODE`: exactly
`true` (lower case) switches team mode on at the next deploy; unset or any other value leaves it off, the
template's default. It is a switch, not an identifier, so it is a variable, and the deploy log says whether team
mode is on. Read
[Switching team mode on safely](#switching-team-mode-on-safely) before you set it. Other Bicep parameters, such as
`ownerLearnerKey`, keep their defaults in `infra/main.bicep`. To change one, change its default in a reviewed
pull request, or add it to both `--parameters` lines in `deploy.yml`.

## The deploy pipeline

Two workflows live in `.github/workflows`:

- **`ci.yml`** runs on pull requests and on pushes to branches other than `main`. It runs the unit tests with
  `DOJO_LOCAL=1`, the secret scan, and `bicep build` of both templates. It has read-only rights.
- **`deploy.yml`** runs on every push to `main` and on manual dispatch. In order:
  1. Unit tests and the secret scan. A failure stops the deploy.
  2. Sign in to Azure as the deploy identity (OIDC).
  3. `what-if`, then an incremental deployment of `infra/main.bicep` to the app resource group. Actions logs of
     a public repository are public, so each `az` step keeps its output in files and `tools/public_log.py`
     prints only how many changes of each kind the preview found, how many lines of warnings there were, and
     on a failure the error codes. Never resource names, ids or messages: see
     [When a deploy fails](#when-a-deploy-fails).
  4. Zip `app/`, `requirements.txt`, `docs/user-guide.md`, `LICENSE`, `THIRD-PARTY-NOTICES.md` and a `build.txt`
     that holds the commit SHA.
  5. `az webapp deploy --type zip --track-status false`, and once more after 90 seconds if it fails. App Service
     installs the requirements during the deploy (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`). This step may fail;
     the smoke test decides (see [Troubleshooting](#troubleshooting)).
  6. **Smoke test.** Poll `https://<app-name>.azurewebsites.net/healthz` every 20 seconds for up to 20 minutes.
     It passes only when `/healthz` reports the new build SHA and status `ok`.

The self-test (`app/selftest.py`) runs at startup. It checks the data share, each model role, speech out
(text to speech), speech in (transcription) and the sign-in credential exchange. `/healthz` is open to
anyone, so it shows only `{"status": "ok", "build": "<commit-sha>"}`. The status is `ok` only after a
finished run in which every check passed; otherwise it is `degraded` (also while the first run is pending).
It always answers HTTP 200, so App Service's health check sees a running app. Each check's result, details
and time are at `/api/diag`, for the signed-in owner. After a pass it runs again every 6 hours;
after a failure, every 5 minutes.

### When a deploy fails

The log names the step that failed and its error codes, such as `AuthorizationFailed`, `Conflict` or
`BCP035`. The messages stay out of the public log; read them where only you can:

- **Preview (what-if):** run it yourself, signed in with the Azure CLI:
  `az deployment group what-if --resource-group rg-dojo-swc --template-file infra/main.bicep --parameters authClientId=<auth-client-id> ownerObjectId=<owner-object-id>`.
- **Infrastructure deployment:** the deployment `dojo-<run number>` in the app resource group (Azure portal,
  Deployments), or `az deployment group show --resource-group rg-dojo-swc --name dojo-<run number> --query properties.error`.
- **App deployment:** the web app's Deployment Center, Logs, in the Azure portal.
- **Warnings:** `az bicep build --file infra/main.bicep` shows Bicep's. CI builds both templates on every pull
  request.

## Configuration reference

The app reads its settings from environment variables (App Service app settings in Azure). `infra/main.bicep`
sets all of them; you do not set them by hand in Azure. The code is `load_settings` in `app/core.py`, plus a
few switches in `app/main.py`.

### Dojo settings

| Setting | Meaning |
|---|---|
| `DOJO_DATA_DIR` | Where Dojo keeps its files. Azure: `/home/dojo-data` (the persistent share). Default locally: `.dojo-data` in the repository. |
| `DOJO_AI_ENDPOINT` | The OpenAI-compatible endpoint of the AI Services account, `https://<ai-name>.services.ai.azure.com/openai/v1`. |
| `DOJO_AI_RESOURCE_ID` | The Azure resource ID of the AI Services account. Speech uses it for Entra sign-in to the regional speech endpoints. |
| `DOJO_SPEECH_ENDPOINT` | The custom-domain endpoint for Speech, `https://<ai-name>.cognitiveservices.azure.com`. |
| `DOJO_SPEECH_REGION` | The Speech region. Also used to look up list prices. Default `swedencentral`. |
| `DOJO_MODELS` | JSON naming the model behind each role, as `name@version`: `{"author": "...", "gate": "...", "grader": "..."}`. Shown in the app and stored with each item. |
| `DOJO_MODEL_SKU` | The deployment type of every model, used for prices. Default `GlobalStandard`. |
| `DOJO_OWNER_TID` | The owner's tenant ID. With `DOJO_OWNER_OID` it decides who the owner is. |
| `DOJO_OWNER_OID` | The owner's user object ID. |
| `DOJO_AUTH_CLIENT_ID` | The sign-in app registration's client ID. The self-test uses it to prove the credential exchange works. |
| `DOJO_TEAM` | `1` switches team mode on (ADR 0009). Default off. See below before you use it. |
| `DOJO_OWNER_KEY` | Only for the lost-account procedure. Must be `l` followed by 31 hex digits. Normally empty. |
| `DOJO_LAB_SERVER`, `DOJO_LAB_DATABASE` | The Azure SQL server and database for labs (ADR 0003). In Azure, both empty means labs are off: every lab route answers 404 "Labs are not set up on this Dojo.", the Plan schedules no lab sessions, `/api/diag` shows labs off, and in team mode there is no lab-database mirror of the deletion list (`/home` is the only copy). Only one of the two set stops the app at startup. Locally a fake runner is used either way. |

### Platform settings set by Bicep

| Setting | Meaning |
|---|---|
| `AZURE_CLIENT_ID` | The app identity. The app signs in to the models, Speech and SQL with it. |
| `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` | Tells App Service authentication (Easy Auth) to use the app identity instead of a client secret. |
| `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` | Team mode only: the tenants whose accounts may sign in. |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | Keeps `/home` as persistent storage. Dojo's data lives there. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | Installs `requirements.txt` during the zip deploy. |

App Service sets `WEBSITE_SITE_NAME` and `WEBSITE_HOSTNAME` itself. Dojo uses `WEBSITE_SITE_NAME` to know it
runs in Azure.

### Local-only switches

These have no effect in Azure.

| Setting | Meaning |
|---|---|
| `DOJO_LOCAL=1` | Run on a laptop with a stub AI and a fake signed-in owner. No Azure needed. |
| `DOJO_LOCAL_AI=real` | With `DOJO_LOCAL=1`: use the real models and Speech, signed in as you through the Azure CLI. Needs the endpoint settings above. |
| `DOJO_TENANT_ID` | The tenant the Azure CLI credential asks for tokens in. |
| `DOJO_PREPARE`, `DOJO_CHECK`, `DOJO_DRIFT`, `DOJO_PUSH` | `1` starts that background work locally: preparation (with the question pool and deeper lessons), the delivery check, source drift, push nudges. `DOJO_PREPARE` and `DOJO_CHECK` also need `DOJO_LOCAL_AI=real`. In Azure they always run. |

### Fixed choices in `infra/main.bicep`

- **App Service plan B1 (Linux), Python 3.12**, Always On, health check on `/healthz`.
- **gunicorn with one worker and one thread** (`UvicornWorker`, timeout 600 s). Dojo's file locks and job
  slots assume one process. Do not scale out or add workers.
- **Easy Auth** requires sign-in on every path except `/healthz`, `/feed/*` and `/cal/*` (those use secret
  tokens in the URL), and two static files the browser fetches outside any page: the service worker's script
  `/static/sw.js` and the nudge icon `/static/icon-192.png`. Without a session, Easy Auth answers with its
  sign-in page and a new nonce cookie; for a background request that cookie replaces the nonce of a sign-in
  under way, which then fails ("invalid nonce" in the application log) and starts again, endlessly. So no
  request that Dojo's pages make on their own may get that answer: the 5-minute build check reads `/healthz`,
  and every other call sends `X-Requested-With: XMLHttpRequest`, for which Easy Auth answers 401 or an empty
  403 instead. Dojo then says the sign-in has ended and offers **Reload**; it never starts a sign-in itself.
  Easy Auth keeps the path and the query of the address it signs you in for, but not what follows `#`: its
  sign-in page keeps that in a cookie which the cross-site return from Entra does not send, even with
  `preserveUrlFragmentsForLogins` on (seen on 5 Oct 2026; the setting stays on). So a link that Dojo hands out
  to be opened later (a calendar event, a nudge, an episode's notes) is `/?go=<route>` and not `/#/<route>`.
  The app turns it into `/#/<route>` when it starts (`app/deeplink.py`, ADRs 0004 and 0013), and **Reload**
  asks for `/?go=<the page you were on>`.
  With team mode off, only the owner's object ID is allowed.
- **Logs** at level Warning, and HTTP logs kept 7 days (35 MB). Basic auth for the deployment endpoints is off.
- **Model parameters**: `authorModel`, `gateModel` and `graderModel` (name, version, capacity) and `modelSku`.
  The three must stay three different model families (see [architecture.md](architecture.md)).

## Backups and restore

All of Dojo's state is files under `/home/dojo-data`. App Service makes automatic backups of `/home` on Basic
plans and above: about every hour, kept for 30 days. Automatic backups stop working once the site's content
is larger than 30 GB, so watch the size (the avatar cache has its own 3 GB budget).

To restore, use the Azure portal (the app → Backups → Restore). A restore puts back the whole share as it was
at that time. In team mode, tell members first: their work since that backup is lost. Restoring to a new
app and copying files back is safer than overwriting the live app.

You can also copy `/home/dojo-data` yourself through the Kudu console or SSH. Treat the copy as personal
data (see [privacy.md](privacy.md)).

## Costs and cost controls

You pay Azure's normal prices. Dojo has no licence fee.

- **Studio** (the owner's cost page) shows Dojo's estimate of what each kind of work costs, at Azure **list
  price**. It reads prices from the public Azure Retail Prices API and counts the tokens, characters and
  minutes Dojo used. Your real bill can differ (discounts, taxes, other resources). Use Azure Cost Management
  for the truth.
- **The fixed part** is the B1 plan and the storage. Check the Azure pricing page for your region.
- **AI use** (models, speech, avatar) is pay as you go. Background work has daily limits (see
  [architecture.md](architecture.md#background-services-and-their-limits)). Avatar filming only starts on an explicit click that
  shows the list price first.
- **Labs** cost about **USD 23 a month** even when nobody uses them: Defender for SQL (about USD 15, if your
  tenant's policy switches it on), the private endpoint (about USD 7.30) and the private DNS zone (about
  USD 0.50). The serverless database uses the free offer and pauses after 60 idle minutes. To stop the cost,
  remove the lab resources and the `DOJO_LAB_SERVER` and `DOJO_LAB_DATABASE` settings from
  `infra/main.bicep`, so the next deploy does not bring them back, and delete the SQL server, the private
  endpoint and the private DNS zone. With both settings empty, labs are off and the rest of Dojo works as
  before.
- **Team budgets** (team mode only, ADR 0009): all members together share a daily cap, **USD 5 by default**
  (the owner can set up to USD 500). Each member also has daily allowances per action, such as graded
  answers or Ask questions. Every paid call reserves its worst case first, so the cap can feel tight when
  several members work at once. The owner is not capped. The Members page shows costs only for groups of five
  or more members.

## Switching team mode on safely

Team mode lets colleagues use your Dojo. It is **off by default**, and you should treat switching it on as a
privacy decision, not a technical one. In team mode you, as the operator, can technically read everything
stored on the share, and members cannot see when you do. The app itself shows you nothing per member.

1. **Get a written privacy review.** Give your privacy owner [privacy.md](privacy.md); it is written for that
   review. In countries with works councils (for example Germany or the Netherlands), they may need to agree
   too. If the review says no, each colleague can run their own Dojo instead.
2. **Decide on the model geography.** The default GlobalStandard deployments may process requests outside
   your region. If the review asks for it, switch to Data Zone deployments (for example Data Zone EU) where
   your models offer them, by changing `modelSku`.
3. **Set the budget** you are willing to pay (the default cap is USD 5 a day for all members).
4. **Invite members.** Colleagues outside your tenant sign in as B2B guests: invite them in the Entra admin
   center. The bootstrap sets "Assignment required" on the sign-in app, so also assign each member to its
   enterprise application. (You may switch assignment off instead; then every account in the tenant reaches
   Dojo's "Ask for access" page, and Dojo's own approval is the only gate.)
5. **Switch it on.** Set the repository variable `DOJO_TEAM_MODE` to `true`
   (`gh variable set DOJO_TEAM_MODE --body true --repo <github-owner>/<repo>`, see [GitHub secrets](#github-secrets)),
   then run the deploy workflow (Actions → deploy → Run workflow, or
   `gh workflow run deploy.yml --repo <github-owner>/<repo>`). The Bicep parameter `teamMode` is then `true`: this
   sets `DOJO_TEAM=1`, lets Easy Auth admit the tenant's accounts and turns on the team routes.
6. **Approve requests.** Each new member asks for access. Approve them on the Members page.
7. **Circles are optional.** The social layer of Play (ADR 0010) stays off until you switch it on on the
   Members page.

To stop, use the Members page, not the deployment switch. **Pause** stops members' work at once but keeps their
data, and they can still export or delete it. **End team mode** asks you to tell every member outside Dojo,
then ends membership on a date at least 14 days later and deletes members' data on that day. Only after
that, set `DOJO_TEAM_MODE` back to `false` (or delete it) and run the deploy workflow. See ADR 0009
("Pausing and ending team mode") and [privacy.md](privacy.md).

## Upgrading

- **Your own changes**: open a pull request. CI runs. Merge to `main` and the deploy workflow ships it.
- **Upstream changes**: if you forked, pull from upstream into a branch, resolve conflicts in the files you
  changed (usually only the names above), and merge through a pull request as usual.
- **Dependencies** are pinned in `requirements.txt`. Dependabot proposes updates weekly for pip and GitHub
  Actions. Let CI run, read the release notes, and merge one at a time.
- **Models**: change the model parameters in `infra/main.bicep` and deploy. Keep the three roles in three
  different model families.

## Troubleshooting

**The app deploy step fails, but the run goes on.** The infrastructure step can restart the deployment service
(Kudu) a minute later, in the middle of the zip upload or of the build that installs the requirements. Then
`az webapp deploy` fails (for example with HTTP 502, 504 or 409) while the build goes on, or the build stops and
the site starts without its packages: its log says `No module named 'uvicorn'` and `/healthz` answers 503. So
the step tries once more after 90 seconds and may fail: the smoke test decides. If the smoke test fails too,
re-run the workflow; before that, `/healthz` tells you which build runs.

**The smoke test fails.** Sign in and open `/api/diag`. It shows which self-test check failed and why. Common
causes: a model deployment missing or out of quota; a role assignment not yet active (it can take a few
minutes after the first deploy); Speech not reachable.

**Sign-in loops on "Pick an account".** The account you chose is not allowed: it is not assigned to the
sign-in app, or (with team mode off) it is not the owner. Sign out at `https://<app-name>.azurewebsites.net/.auth/logout`,
then sign in again with the right account. A private browser window helps when your browser holds several
work accounts.

**A lab run waits a long time first.** The serverless lab database pauses after 60 idle minutes. The first
query wakes it up, which takes about a minute.

**Avatar filming is slow to start.** The avatar service allows about two new jobs a minute. Dojo spaces its
requests and queues the rest.

**I cannot find errors in the logs.** App logs are kept at level Warning on purpose, so routine requests are
not logged. Use the log stream in the portal, or `/api/diag` for the self-test details.

**I lost the owner account.** The owner's data is filed under a learner key: a one-way hash of the tenant
and user IDs. A new account has a new user ID, so it would get a new, empty key. Keep a second administrator
(or a break-glass account) in the tenant who can recreate or re-invite you. Then set the `DOJO_OWNER_OID` secret
(the Bicep parameter `ownerObjectId`) to your new user ID, set the Bicep parameter `ownerLearnerKey` to
the old key in a reviewed pull request, and deploy. The old key is `l` plus 31 hex digits; it is the name of your
folder under `learners/`. Leave `ownerLearnerKey` set from then on. Details: ADR 0009, "Lost accounts".

## Local development and tests

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # on macOS or Linux: source .venv/bin/activate
pip install -r requirements.txt
$env:DOJO_LOCAL = "1"
python -m unittest discover -s tests -t .
python -m uvicorn app.main:app --port 8000 --no-access-log
```

Open `http://localhost:8000`. With `DOJO_LOCAL=1` you are signed in as a fake owner, and a stub AI answers
every model call, so you need no Azure resources. Data goes to `.dojo-data` in the repository (ignored by Git).

To try the real models from your laptop, also set `DOJO_LOCAL_AI=real`, `DOJO_AI_ENDPOINT`,
`DOJO_SPEECH_ENDPOINT`, `DOJO_AI_RESOURCE_ID`, `DOJO_MODELS` and `DOJO_TENANT_ID`, and sign in with
`az login`. Dojo then uses your Azure CLI sign-in; the bootstrap gave you the roles it needs.
`tools/dev_real.py` runs a lesson, an item or a rehearsal this way. It reads these settings from the
environment and stops with a clear message if one is missing.

Before you push, run the same checks as CI:

```powershell
$env:DOJO_LOCAL = "1"; python -m unittest discover -s tests -t .
python tools/secret_scan.py
node --check app/static/app.js
```

Keep `--no-access-log`. Podcast feed and calendar links carry their token in the path, and an access log
would write them down.
