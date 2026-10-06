# ADR 0003: A private Azure SQL sandbox for DP-800 labs

Status: proposed. The owner and deployment lead must review the first infrastructure change.

## Decision and action boundary

These are **authorized live-write sandbox** exercises, not production work. The only learner is the
signed-in owner. Dojo deploys one new free-offer Azure SQL Database in `rg-dojo-swc`, Sweden Central,
with a private endpoint. It has no learner-record data. The app's existing user-assigned managed
identity (`id-dojo-app`) is SQL's Entra administrator. The Bicep admin `sid` is its **clientId
(application ID)**, with `principalType: 'Application'` and login set to the identity name.
Microsoft Learn specifies the application ID for application/service-principal and managed-identity
administrators; users and groups instead use object IDs. No password, production connection string,
new RBAC assignment, or public SQL endpoint is allowed.

The resource group's SFI-ID4.2.2 SQL DB - Safe Secrets Standard deny policy requires
`administrators.azureADOnlyAuthentication: true` **in the server creation request**. Bicep sets
it inside `administrators` at creation, and explicitly sets `publicNetworkAccess: 'Disabled'`
so subsequent what-if/redeploys retain the private-only boundary. Tenant policy also disables
public access on new database accounts; the tenant's deploy/modify policies may add tags or settings
after creation. Cross-subscription VNet peering is denied but is not part of this design.

Only the app's approved setup and checks run as the Entra admin. Every learner batch uses a **new,
unpooled** connection, first executing `EXECUTE AS USER = N'lab_runner' WITH NO REVERT;`. The user
has no login and gets `CREATE TABLE` plus `ALTER`, `SELECT`, `INSERT`, `UPDATE`, `DELETE` on **one**
lab schema at a time. Each lab schema has its **own owner without login**, not `dbo`, so a
learner-created trigger cannot cross lab schemas via same-owner chaining. Setup migrates any
existing lab schema to its own owner. On each operation, the app revokes grants on the other lab
schemas first.
`CREATE TABLE ... SYSTEM_VERSIONING = ON` can otherwise create its history table in **another
schema**, even without `ALTER` there. A database DDL trigger on `CREATE_TABLE` and `ALTER_TABLE`
rolls back cross-schema history creation based on the current table's metadata. Its fixed body
runs as a separate `lab_guard` user without login, granted only database `VIEW DEFINITION` for
metadata visibility. `lab_runner` cannot impersonate that user; the trigger runs no learner SQL.
No `CONTROL`, `EXECUTE`, `IMPERSONATE`, external model or outbound SQL credentials are granted.
SQL cannot create resources or change Azure RBAC. Within the current schema the learner may create,
change, and delete small lab objects. SQL Server permissions are the boundary, not keyword filtering.
`Run` asks for explicit confirmation when SQL contains a destructive verb; the API also rejects
such a batch without that approval flag. This is an owner confirmation, not a SQL security parser.
The app limits SQL input to 8 KB, one batch with at most 20 semicolons (no `GO`), one operation at a
time, a 20-second statement timeout, and 200 rows displayed. These are resource controls, not a
complete SQL parser. No lab requires an external embedding service; all sample vectors are
hand-made. Do not put personal data, secrets or production data into the SQL editor.

Reset is a **learner-initiated destructive action**, with a confirmation in the UI. Normally it
changes only objects in that lab schema. To repair escaped history tables left by an older
version, it first turns off versioning for temporal parents in the selected schema, **or any
parent whose history is in that schema or `dbo`**. It removes the selected lab's history tables
even if they are in a different lab schema. This database contains no legitimate `dbo` user
tables; reset also removes any user tables in `dbo` after disabling their parent versioning.
It then drops edge/foreign-key constraints, views, tables, sequences and any synonyms in the
selected schema. This exceptional cleanup can change another lab's temporal parent but does not
drop that parent's table; the owner should check the other lab after resetting an old escaped
state. Reset never drops the database, users or schemas. The runner's only database-level DDL grant is `CREATE TABLE`. Its schema
`ALTER` grant also allows `CREATE SEQUENCE`; table constraints, indexes and DML triggers go
away with their tables. Creating synonyms, types or standalone defaults requires separate
database-level grants that the runner does not have, but reset also removes any preexisting
synonyms in the lab schema.
Reset then recreates that lab's fixture tables, even if a learner altered or renamed them.
Fixture creation uses dynamic SQL so an altered table cannot cause compile-time errors in the
conditional setup batch and block reset. If a sequence or other non-table occupies a fixture
name, run/check asks the learner to press Reset; Reset never seeds before dropping objects.
A reset SQL error is shown to the owner and does not become a failed check.
Deletion of the whole sandbox, sharing SQL results
externally, changing the free-offer billing choice, or modifying network/IAM boundaries requires
explicit human approval. Before and after reset, verify that the other two labs still work.

## Network, identity and data flow

Browser → existing owner-only App Service Easy Auth → `X-Dojo` and same-origin API guard → lab
room → identity token from `ManagedIdentityCredential` → TLS 1.2+ SQL at
`<server>.database.windows.net` resolved through `privatelink.database.windows.net` → private
endpoint. The app integrates into a delegated subnet. `vnetRouteAllEnabled: false` keeps Azure AI
Services, sign-in, docs fetching, and the site's ordinary outbound traffic on the existing route.
Azure SQL's public access is disabled. A separate endpoint subnet, private DNS zone/link and zone
group give the app private SQL name resolution. The signed-in owner may run only lab-schema SQL.
Lab runs, shown hints, checks and resets append small hash-chained events to the existing Record
on the app's `/home` share; **SQL text, row results and data do not**. Neither the lab database nor
the fake local runner stores a learner record.

Threats: forged cross-origin writes are stopped by the custom header and origin check; unsafe SQL
is constrained by impersonation and grants, not by a regex; a pooled impersonated connection could
cross requests, so pooling is disabled before the first connection, and all connections close.
Private DNS mistakes or a paused database fail as availability errors, never as failed checks.
A compromised web app still holds SQL admin authority, so the database is strictly synthetic and
contains no learner data. Database free-offer limits stop database charges, **not** endpoint/DNS
charges or arbitrary CPU abuse within a monthly allowance. App Service has one worker.

## Evidence

`lab.opened`, `lab.ran`, `lab.hint`, `lab.checked`, and `lab.reset` are recorded separately. The
detail response exposes the number of hints, not their text; opening a hint through its own route
records the help before returning its text. Opening the lab shows the goal and starter. Opening,
hinting, running and checking all restart that skill's teaching clock. These activities near or
during a timed exam make its conditions unknown. A successful check
means only that a particular SQL schema and its data matched a fixed query at that instant.
Checks record hints shown, coach answers, runs, and time since the last lesson view (including its
30-minute exposure window). The checker does not know if a pasted solution, unobserved human
help, or a prior attempt produced the result. Repeatable, public tasks and unchanged results are
not independent challenge items. **Lab checks fill no evidence pip**, whether helped or unaided;
they appear on the skill page as database-checked practice. The separate written-answer checks
still govern pips and the 20-hour "later" rule, measured since the latest teaching including lab
activity. A timeout, wake-up delay, SQL driver failure or
connection error is never recorded as a miss. The local fake is marked synthetic and never makes
`lab.checked` evidence.

SQL login has a 90-second bound. The driver drops native error 40613; the app recognizes its
"is not currently available" / "retry the connection later" message and retries twice with a
short backoff. If it is still waking, the page retries about every 10 seconds for at most two
minutes before reporting failure. A learner query exceeding 20 seconds receives a normal
response only when the driver reports `driver_error == "Timeout expired"`;
other learner SQL errors are shown to the owner rather than mislabeled as wake-up failures.
Logs contain the error class and SQLSTATE if the driver exposes it. The pinned driver actually
discards SQLSTATE for mapped exceptions, so logs use its safe driver error category instead,
never SQL text or returned rows. No failed database
check records a miss.

## Driver and dependency gate

`mssql-python==1.15.0`, Microsoft's maintained MIT-licensed Python driver, supports Python 3.12
Linux `manylinux_2_28_x86_64` wheels, bundled `mssql-python-odbc`, Entra `token_provider`
with `azure-identity`, query timeouts and explicitly disabled pooling. The package feed supplied
the pinned Windows wheel and the Linux CPython 3.12 wheel plus its ODBC bundle. Dependencies
already include `azure-identity`; the driver and its ODBC wheel are the additions. The bundled
ODBC 18 binaries carry separate **Microsoft proprietary license terms**, not MIT. We inspected
the wheel's bundled EULA and its installation/distributable-code sections; review those terms
again before distributing the driver outside this personal app. CI installs
from `requirements.txt`; keep dependency alerts/scans on. We rejected `pyodbc` because App
Service's Python image does not promise a system-installed msodbcsql18. We rejected SQL auth,
including a stored password. At deployment, confirm the App Service Linux glibc/wheel loads and
the token is accepted. Driver updates require a new license, wheel, and vulnerability check.

## Cost, alternatives, and review

Microsoft's free offer gives 100,000 vCore-seconds, 32 GB data and backup per database each
month. `useFreeLimit: true`, `freeLimitExhaustionBehavior: 'AutoPause'`, General Purpose serverless
2 vCores, local backup, and an idle auto-pause after 60 minutes prevent SQL from billing above
the free allowance; after exhaustion it stops until next month. **Before deployment**, confirm
this subscription has a free slot and that Sweden Central can receive it: Learn says the first
free database locks the region for later ones in the subscription and does not confirm this
subscription's eligibility. If it cannot, stop; do not silently deploy a paid database or change
regions. Budget **about $23/month** for the new logical SQL server and its network path:
Defender for Cloud's subscription-level **SqlServers Standard** plan (applied by DeployIfNotExists
policy, with no per-server opt-out) adds about **$15/month per server**; the private endpoint adds
about **$7.30/month**; the private DNS zone about **$0.50/month**. The eligible free-offer database
itself is **$0**. Studio lists these infrastructure costs separately from Speech's estimated
total. Check the real Azure bill.

We did not build a separate lab subscription: stronger cost and learner-record isolation would
require a new owner-approved identity, protected pipeline and RBAC boundary. A public SQL endpoint
with firewall rules would expose a larger attack surface and break the private-only goal.
GH-300 Codespaces labs are not built: they need an owner decision on the personal GitHub
organization, seat, repository, policy and spending boundaries first.

Review after any schema permission change, driver or SQL feature version update, free-offer
policy change, new learner, additional app workers, evidence-rule change, or unexpectedly high
Defender, endpoint, DNS, or database spending. No preview SQL feature is required: VECTOR, VECTOR_DISTANCE,
JSON_VALUE and TOP are documented for Azure SQL Database. Vector indexes and model-generated
embeddings in mockup 05 are intentionally **not** used.

## Release and undo

Only the existing OIDC GitHub deploy pipeline may apply `infra/main.bicep` incrementally, after
review. Before merge inspect the what-if for exactly: VNet, two subnets, SQL logical server and
one free-offer database, SQL private endpoint and its DNS zone group, private DNS zone and VNet
link, plus VNet integration and two settings on the existing site. No RBAC role assignments.
After deploy, explicitly run `POST /api/diag/lab` as the owner (the startup self-test does
**not** connect); check private DNS and IP, `lab_runner` impersonation, one lab end to end,
reset isolation, AI Services and docs fetching. Verify the database is free-offer/AutoPause.

To undo, disable lab routes and remove the integration and lab settings in a reviewed PR,
then have the owner delete only the new SQL database and logical server (ending that server's
Defender charge), endpoint and zone group (ending endpoint charges), private DNS link and zone
(ending DNS charges), and VNet/subnets in `rg-dojo-swc`. **Deleting the server alone does not
delete the billable endpoint and DNS zone.** Incremental deployment **does not delete** these
resources automatically. Verify all three charges stop; do not delete the existing
app, identity, AI resource, or learner Record. Export the Record before deleting it if desired.

Sources: [SQL free offer](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer),
[free offer FAQ](https://learn.microsoft.com/en-us/azure/azure-sql/database/free-offer-faq),
[serverless auto-pause](https://learn.microsoft.com/en-us/azure/azure-sql/database/serverless-tier-overview),
[Entra-only server creation (application ID and principal type)](https://learn.microsoft.com/en-us/azure/azure-sql/database/authentication-azure-ad-only-authentication-create-server?view=azuresql),
[Entra SQL admin](https://learn.microsoft.com/en-us/azure/azure-sql/database/authentication-aad-configure),
[private endpoint](https://learn.microsoft.com/en-us/azure/azure-sql/database/private-endpoint-overview),
[temporal table versioning](https://learn.microsoft.com/en-us/sql/relational-databases/tables/temporal/stop-system-versioning?view=azuresqldb-current),
[sequence permissions](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-sequence-transact-sql?view=azuresqldb-current),
[synonym permissions](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-synonym-transact-sql?view=azuresqldb-current),
[type permissions](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-type-transact-sql?view=azuresqldb-current),
[driver and Entra token provider](https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/entra-authentication),
[driver pooling](https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/connection-pooling),
[VECTOR type](https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type?view=azuresqldb-current),
[VECTOR_DISTANCE](https://learn.microsoft.com/en-us/sql/t-sql/functions/vector-distance-transact-sql?view=azuresqldb-current),
[JSON_VALUE](https://learn.microsoft.com/en-us/sql/t-sql/functions/json-value-transact-sql?view=azuresqldb-current).
