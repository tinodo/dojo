# ADR 0023 - Performance monitoring of the lab database is the owner's switch

- Status: proposed
- Date: 2026-10-19
- Amended 29 October 2026 by ADR 0028: the lab database is on the Basic tier, which never pauses and has no free allowance. What this record says about the
  free offer, auto-pause, waking and the allowance no longer applies; the switch itself stays. Outcome of the measurement: see the last section.
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the builder (branch `feat/lab-performance-monitoring`). The lead can reject or change it in review.
- Guardrails: ADR 0003 (the lab database is on the free serverless offer and never wakes on its own), ADR 0009 (members' lab
  data and the privacy sheet), ADR 0019 and ADR 0020 (Studio is the owner's page, and the owner presses its buttons).

## The requirement

The owner asked for performance monitoring of the lab database ("I think you do this by setting this in SQL") and pointed at
Learn's article on it. The article confirms it: Azure collects a database's performance data once the database-scoped extended
property `MS_EnablePerformanceMonitoringPreview` is `true` in that database. What Learn says, read again for this decision:

- It is per database. The property is set in the user database, never in `master`, by a member of `db_owner`. Dojo's
  connection is the server's Entra administrator, so it is `dbo`. The script is `sp_updateextendedproperty` or
  `sp_addextendedproperty`; the `GO` in the article is the batch separator of SSMS.
- Single databases are supported: in the vCore model (General Purpose, Business Critical and Hyperscale "with 2 or more
  vCores") and, on a line of its own with no such floor, in the serverless tier, which the lab database is (`GP_S_Gen5`, 0.5 to
  2 vCores). Elastic pools and secondary replicas are not. It is a preview and has no extra price.
- The data is usually there within 10 minutes, in the Fabric Database Hub (its Performance page) or by KQL on
  `https://adx.centralus.arcdataservices.com/kusto/` (database `ArcSqlTelemetry`). Reading it needs the Reader role on the
  subscription and the `Microsoft.Sql` resource provider registered there. Dojo does not read it.
- It samples the engine's views: waits and memory every 10 seconds, CPU every 15, the active sessions (ids and status) every
  30, counters and storage use every minute, the database's properties every 10 minutes. Learn says it holds no personal
  data or customer content and does not keep it at rest outside the database's geography. Turning it off stops new data and
  keeps what was collected.
- **Not said anywhere: whether that sampling keeps a serverless database from pausing, or wakes a paused one.**

The last point is the whole risk. The lab database is on the free offer: 100,000 vCore-seconds a month, then it is closed until
the next month (`freeLimitExhaustionBehavior: AutoPause`), and it pauses after 60 idle minutes. A wake costs at least 1,800
vCore-seconds (0.5 vCore for the hour before it pauses again), about 2% of a month. On 19 October 2026 the database had been
paused since 10:52 UTC, 67,335 of the 100,000 were left, and Dojo's normal use costs about 1,700 a day. A database that
monitoring kept awake would use about 43,000 a day: the 67,335 in a day and a half, and then no labs until the next month.

## The decision

- **Off, and only the owner turns it on.** Nothing in Dojo sets the property by itself: not startup, not `migrate`, `prepare`,
  `reconcile` or the probe, not the deployment, not Bicep. The one caller of `LabRoom.set_monitoring` is
  `POST /api/studio/lab/monitoring` (owner only, with the `X-Dojo` header, the body a strict `true` or `false`), from the
  button in the Lab database panel on Studio's Checks tab. The button asks first, in words: it wakes the database (about 2% of
  the month's free allowance), and Learn does not say whether monitoring keeps a serverless database awake, so watch *Free
  amount remaining* afterwards.
- **One deliberate connection.** The batch is Learn's script without `GO`, with `SET NOCOUNT ON` and a final `SELECT` that
  reads the value back on the same connection. Dojo records "on" or "off" only when the database's own answer says so. Any other
  answer is an error, and nothing is recorded as changed. The lab lock is held for it (30 seconds to get it), so no lab query
  runs beside it. A database that is still waking answers "Waking the database, about a minute. Try again shortly."
- **The page shows what Dojo last confirmed, never the live state.** `GET /api/studio/lab` and `/api/diag` read
  `labs/monitoring.json` (what was observed, when, the last try and why it failed) and open no connection: looking at a page must
  not wake a free database. The owner's existing check of the lab database, which connects anyway, also reads the live value
  and updates the record.
- **Members are told.** The first sign-in notice says what Microsoft records if the operator turns monitoring on,
  `NOTICE_VERSION` is 2 (in team mode every member is asked again), and the privacy sheet has the two rows and its honest limit.

## Options considered

- On by default, set by the deployment, at startup or in `migrate()`. Rejected: an unrequested wake at every deploy and
  start; if monitoring keeps the database awake, a cost nobody sees that ends the free month in days; `migrate()` runs while
  the lab lock (which is not re-entrant) is held and does not retry; and in team mode a failure there would be at startup.
- Set it in Bicep. Rejected: the property is content of the database, not an Azure resource, and the pipeline's runner has no
  network path to a database that only has a private endpoint.
- Show the live value on the page. Rejected: every view would wake the database.
- Read *Free amount remaining* and turn monitoring off when it falls. Not done: it needs a role on the database that the web
  app does not have. If the owner wants it, it is a separate change.

## Consequences

One press wakes a paused database (about 2% of the month). The state shown can be older than the database: a restore or a copy
brings the property back as it was, which is why the panel says "last confirmed"; the owner's check of the lab database
corrects it. While monitoring is on, the privacy sheet's honest limit applies: a burst of lab use shows in the CPU samples.

## How we find out whether monitoring keeps the database awake

Learn does not say, so the first press is a measurement. Before it: the database's status and *Free amount remaining* are read
(67,335 left, Paused, about 1,700 a day on 19 October). After it: at +10 minutes the panel says on and the Database Hub has
data; then nobody uses labs, and the status and *Free amount remaining* are read again at +2 hours (past the 60-minute
auto-pause delay), +24 hours and +7 days. If the database is online while idle, or the amount falls much faster than 1,700 a
day, the owner presses **Turn off**. If the result is unclear, it stays off. The outcome is written here when it is known.

## Review triggers

Learn says what monitoring does to auto-pause; the preview ends or changes its property, endpoint or tables; the lab database
leaves the free offer; the notice or the sheet no longer describes what the database sends.

## Outcome (29 October 2026)

Monitoring did not keep the database awake. It was turned on on 19 October at 17:42 local; the database paused about 16 minutes after each of its first three
wakes, and stayed paused for 52 hours 51 minutes in a row (20 to 23 October) with monitoring on. From 24 October the free amount fell 5,400 to 11,600 a day instead of
about 1,750, for another reason: an application identity that is not Dojo's, passing through a tenant-wide Azure Policy, writes a tag on every resource of the resource
group about every two hours, and every such write resumes a paused serverless database (ADR 0028). The database is now on the Basic tier, which does not pause, so the
question this record asked has no answer left to give. The switch stays: Learn lists single databases in the DTU model as supported.
