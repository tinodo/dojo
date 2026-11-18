# ADR 0031 - A larger web app plan when Azure has no small one left

- Status: accepted (built)
- Date: 2026-11-18
- Decision owner: the Dojo lead, for the owner
- Decision: the lead, on the owner's order of 18 November 2026: everything but the storage account was deleted, and "make sure you are able to redeploy through a pipeline".
- Guardrails: ADR 0030 (the data is in a storage account, so a rebuilt web app starts from it), ADR 0009 (one process on one instance: a bigger plan does not widen what Dojo supports), EG-8 and EG-10 (a change reaches Azure only through the pipeline), AGENTS.md rule 1 (one tenant, tokens and REST).

## The requirement

The pipeline has to be able to build all of Dojo from nothing. The owner may delete everything but the storage account, which keeps the data (ADR 0030), and bring Dojo back later with one run. That has to work on the day he asks, not only when Azure happens to have room.

## What happened

On 18 November 2026 everything but the storage account was deleted, and the pipeline was run twice to prove it could rebuild. Both runs made the network, the database, the AI account with its three models, the private endpoints and the storage container, and then failed at the same resource, the web app plan: `Conflict`, "No available instances to satisfy this request. App Service is attempting to increase capacity."

What was measured, with a one-plan probe that makes a plan, reads Azure's answer and deletes it:

| Minute (local) | B1 | S1 | B2, B3, P0v3, P1v3 |
|---|---|---|---|
| 20:50 to 20:57 | refused | refused | made |
| 21:02 to 21:04 | made (a B2 plan was also scaled down to B1) | made | made |
| 21:14 to 21:22 and on | refused, every 18 seconds | | made (checked 21:20) |

A new resource group did not help: the same request, in two throw-away groups, was refused as well. So the shortage is Azure's, of the small workers in Sweden Central; it comes and goes, and nothing in the project can hurry it. Microsoft's own advice is to retry later, to use async scaling, or to use a new resource group ([Manage an App Service plan](https://learn.microsoft.com/azure/app-service/app-service-plan-manage)).

## Options

| Option | Verdict |
|---|---|
| Wait and run the deploy again by hand | Rejected as the only answer: the run goes red whenever Azure is short, and the owner asked for a pipeline that can redeploy |
| Try the same size for a long time | Rejected: the shortage lasted more than 7 minutes at a stretch, twice |
| A new resource group for the plan | Rejected: it did not help in the test, and every role assignment and the deploy identity's rights are on the one group; a new group needs the owner's bootstrap (`infra/bootstrap`) |
| Async scaling (`asyncScalingEnabled`) | Rejected: Azure then makes the plan with no worker at all (measured: 0 workers), so the site has nothing to run on and the smoke test fails anyway |
| Another region | Rejected: the network, the private endpoints and the storage account are in Sweden Central |
| **B1 a few times, then B2, then B3, and say so in the log** | **Chosen** |

## The decision

- The Bicep parameter `planSku` sets the plan's size. It is `B1` unless told otherwise, and the only values allowed are `B1`, `B2` and `B3`: a bigger plan costs real money, so nothing else can be asked for by accident.
- The deployment step of `deploy.yml` runs the same incremental deployment with `planSku` set to B1, B1, B1, B2 and B3 in that order, a minute apart. It goes on to the next size **only when the plan is the only operation that failed** (two counts from the deployment's list of operations: failed plan operations above zero, other failed operations zero, and a count that cannot be read counts as "not the plan alone"). Anything else that failed would fail again at the next size, and a larger plan could be left billing for nothing, so then the step stops at once. B3 failing ends the step red, as before.
- The log names the size and never an id. **Whenever a plan larger than B1 exists after the last attempt** (the list of operations says the plan operation succeeded), one warning line says that it costs more and goes on costing until the plan is deleted, so the owner sees it in the run: also when something after the plan failed and the run is red.
- **No size is remembered.** Every deploy asks for B1 first, so a plan that had to be B2 goes back to B1 at the first deploy after Azure has a B1 again. If B1 is still short, that request fails and the request for B2 changes nothing.
- The what-if preview is unchanged: it asks for the template's own B1.

## Consequences

- **Cost** (Linux, list prices of Sweden Central from the Azure Retail Prices API on 18 November 2026): B1 USD 0.018 an hour, about USD 13 a month; B2 USD 0.036, about USD 26; B3 USD 0.071, about USD 52. **A plan costs from the moment it exists until it is deleted, with or without a web app on it** (deleting the last app does not delete the plan; [Manage an App Service plan](https://learn.microsoft.com/azure/app-service/app-service-plan-manage), "Delete an App Service plan"). So a larger plan that was made and then left behind by a failed run keeps billing until someone deletes the plan. Studio's Money tab shows what Azure charged.
- **A deploy can take about ten minutes longer** when B1 is short (three more tries, a minute apart, and the deployment's own time), so the job's limit is 90 minutes, not 60.
- **What Dojo supports does not change.** One worker and one thread (ADR 0009); the 30 members of ADR 0009 are still the measured limit, on B1. B2 has more room, which is not used.
- **Scaling an existing plan back to B1 restarts the app**, as every deploy does.
- A deploy that fails for another reason, or for the plan and something else together, is not retried, and the three tries before B2 are spent only on a plan that is alone in failing.

## How we know it worked

Tests (`tests/test_plan_size.py`) run the step's own shell against a stand-in for the Azure CLI: a deployment that works is done at once with B1; B1 comes back on the second or third try and stays B1; B1 never comes back and B2 is used with the warning; B3 is the last size and then the step fails; a failure that is not the plan stops at once, with no pause, and so does the plan failing together with something else (or a count that cannot be read); a larger plan that was made and then followed by a failure is still named in a warning. They also pin the template (one parameter, three allowed values) and the workflow (the order of the sizes, the public summary as the last line, no Azure message in the log). Live: the run that carries this change, from a group that held only the storage account.

## Review triggers

Azure offers a way to ask for capacity ahead of time, or says when the shortage ends; Dojo moves to another region; B1 is no longer sold; a plan above B3 is wanted; the supported size of Dojo (ADR 0009) changes.
