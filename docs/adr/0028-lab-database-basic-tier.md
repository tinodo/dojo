# ADR 0028 - The lab database moves to a small fixed-price tier

- Status: accepted (step 1 built; step 2 follows)
- Date: 2026-10-29
- Decision owner: the Dojo lead, for the owner
- Decision: the owner, 29 October 2026, after the cost options were put to him: "Perhaps move it to the $5 a month tier? that is cheaper than
  $1 per day... :-)" and then "Make it all so!". He also said the resource group cannot be left out of the tenant's governance: there is no opt-out.
- Guardrails: ADR 0003 (the labs' private database, its cost and its limits), ADR 0009 (the deletion list that the database mirrors), ADR 0023
  (performance monitoring, which this decision answers), EG-8 and EG-10 (a change reaches Azure only through the pipeline).
- Supersedes in part: ADR 0003 ("Cost, alternatives, and review": the free offer) and ADR 0023 ("How we find out": the risk it measured).

## The requirement

The labs need a database that answers whenever the owner opens a lab, at a small and known price. ADR 0003 chose the free serverless offer
because, as far as Dojo's own code goes, nothing wakes a paused database except a lab. Free meant 100,000 vCore-seconds a month; when they run
out the database stays shut until the next month (`AutoPause`), so labs fail for the rest of that month. Normal use cost about 1,750 a day.

## What was found

Read-only, on 29 October 2026, from Azure's activity log and metrics:

- **Since 24 October something outside Dojo wakes the database about every two hours.** An application identity that is not Dojo's and not the
  owner's writes a tag on every resource in the resource group, about every two hours (it passes through a tenant-wide Azure Policy that adds a tag).
  The owner says it is the tenant's own governance. The tag is already there, so nothing changes, but for a paused serverless database every write
  to it is a resume: 13 wakes a day, about 16 minutes each, about 500 vCore-seconds each. Before 24 October no wake came with such a write. Of the 85
  wakes since 23 October, 70 came within two minutes of such a write, and most of the others were deploys (every deploy wakes the database too).
- **The free amount fell 5,400 to 11,600 a day** since 24 October (the most on a day with seven deploys), against about
  1,750 before. 16,236 were left on 29 October, enough until about 31 October. Left alone, the same happens about two weeks into every month.
- **It is not Dojo and not performance monitoring.** In 70 of the 74 wakes no one logged in or ran a session. The database stayed paused for
  52 hours in a row while monitoring was on (20 to 23 October). Dojo's only code that connects on its own runs at start, when a membership changes
  and every five minutes while the database does not answer.
- **The resource group cannot be exempted.** That is the owner's statement of how the tenant works.

## Options

| Option | Cost at list price | Verdict |
|---|---|---|
| Do nothing | USD 0, but labs are shut for about half of every month | Rejected |
| Stay on the free offer and pay for what goes over it (`BillOverUsage`) | about USD 1 a day, USD 31 a month: 13 wakes a day at USD 0.08 | Rejected: dearer than the next row |
| **Basic tier** (5 DTUs, 2 GB) | **USD 0.161 a day: USD 4.83 a month (4.99 in a 31-day month)** | **Chosen** |
| Standard S0 and up | USD 14.52 a month for S0, three times Basic, for room the labs do not need | Rejected |
| An always-on serverless or provisioned vCore database | USD 200 a month and more | Rejected |

The Basic price is Sweden Central's, read from the Azure Retail Prices API on 29 October 2026. A serverless wake: USD 0.574 per vCore-hour at the
0.5 vCore minimum for about 16.7 minutes.

## The decision

**Move the lab database to the Basic tier, in two steps, each its own change through the pipeline.**

1. **Leave the free offer (this change).** Learn says an update that removes the free offer must not also change the service tier, and that removal is
   permanent for the database: the offer cannot be applied again, even if the next step fails. So `useFreeLimit` becomes `false` and nothing else about the
   database changes: still General Purpose serverless with the 60-minute auto-pause. From here the database is billed at serverless prices, about USD
   0.08 for each wake, until step 2.
2. **Move to Basic (the next change).** `sku` Basic, 5 DTUs, `maxSizeBytes` 2 GB, the serverless settings gone. Everything the owner or a learner reads
   that says the database is free or pauses (the Money tab's line, the lab database line on Health, the Labs page, the guide, the operator guide)
   changes in the same pull request, because it would not be true any more.

**What fits.** The database holds 23 MB (its largest size in 30 days; Basic allows 2 GB). The three labs need the `vector` type, `VECTOR_DISTANCE` and
`JSON_VALUE`; Learn lists no tier limit for them and no preview setting (ADR 0003). Basic allows 5 DTUs, 30 workers and 30 logins and keeps its data on
hard disks: enough for one learner at a time and tiny tables. Learn lists single databases in the DTU model as supported by performance monitoring, so
the owner's switch (ADR 0023) stays, and with a database that never pauses, turning it on or off wakes nothing.

## Consequences

- **Cost:** about USD 5 a month more. The labs' fixed cost goes from about USD 23 to about USD 28 a month (Defender for SQL about USD 15, private
  endpoint about USD 7.30, private DNS zone about USD 0.50, the database about USD 4.83 to 4.99).
- **One way:** the free offer cannot come back to this database. To have it again, Dojo would need a new free database, and any wake would cost
  nothing until the allowance ran out.
- **A database that does not pause:** a lab never waits for a wake, and the "database waking" message becomes rare (it can still appear for a
  few moments when the tier changes).
- **Slower, and it has limits:** a very heavy lab query is cut after 20 seconds, as before. More than about 30 learners at once, or tables that grow
  past 2 GB, mean Standard S0 or S1.
- **Restores:** Learn says a restore on Basic may need a bigger size for a while. The lab data can be rebuilt: the labs are exercises, and the deletion
  list in the database is rebuilt from the file on the data share (ADR 0009).

## How we know it worked

Step 1: Azure shows `useFreeLimit` false and the database online; `/healthz` reports the new build. Step 2: Azure shows the current service objective
Basic and the database online; the app's start-up check of the deletion list connects and does not fail (Azure's connection counts); the owner presses
the lab check on Health once. A daily check on GitHub (`lab-watch`) reads the database's state, tier, storage and load, and fails when one is wrong.

## Review triggers

The tag writes stop (the free offer would then have been enough, but cannot be used again); Basic's price or limits change; more learners or bigger
labs; Learn changes what performance monitoring supports.
