# ADR 0030 - Dojo's data lives in a storage account, and the web app's disk holds a copy

- Status: accepted (built)
- Date: 2026-11-18
- Decision owner: the Dojo lead, for the owner
- Decision: the owner, 18 November 2026. He asked where the lessons, the downloaded pages, the audio, the films and his study record were kept, was told that they
  were files on the web app's own disk and nowhere else, and ordered that they go into persistent storage, not on the web app, and that the data that existed
  be kept.
- Guardrails: ADR 0009 (one process and one lock; a Record that is append-only and hash-chained; deletion that stays deleted), ADR 0002 and 0004 (only a hash of a feed's
  secret is kept), EG-8 and EG-10 (a change reaches Azure only through the pipeline), the tenant's policies (public network access of every new storage account is switched off),
  and the rule that the Record is never written to for a test (AGENTS.md, rule 5).

## The requirement

Everything Dojo makes and keeps is worth money or time: 8.7 GB, in 12,130 files, on 18 November 2026. About 7,400 narration files and 740 films (8.6 GB), the lessons, the
official pages and their snapshots (0.1 GB), and the owner's Record. The pages cost nothing to fetch again; the lessons, the narration and above all the films cost model, Speech
and avatar money to make again, and a Record cannot be made again at all. This data has to survive the loss of the web app and of its plan, and the owner must be able to say where it is.

## What was wrong

All of it was files under `/home/dojo-data`, the web app's own persistent share. That was a choice made on purpose in the first design: the tenant's policies switch public network access
off on every new storage account, a single learner does not need a database, and a file share needed nothing else. But it left the data with one copy and no way back:

- **No backup.** `docs/operations.md` said App Service backs `/home` up every hour. A Basic plan has no automatic backups, and the web app's backup settings (read on 18 November 2026)
  were empty: there never was one.
- **The data was tied to the web app.** Deleting the app, or recreating it, deleted the data. So did a plan change.
- **The owner could not point at it.** "On the web app's own disk" is not a place he could look at, price or protect.

## Options

| Option | Verdict |
|---|---|
| Keep the files on `/home` and add backups of the web app | Rejected: the Basic plan has no automatic backups, a Standard plan costs several times what the whole B1 plan does, and the data would still be "on the web app" |
| Mount an Azure Files share (or blob storage) as `/home/dojo-data` | Rejected: a mount needs the account key, and the tenant's policy (`StorageAccountDisableLocalAuth`) switches shared keys off on storage accounts |
| Make the Store use blobs only, and change every module that touches a path | Rejected: about twenty modules list, stat, glob and serve files by path. Changing each of them is a large change to the Record's own code, and no review can be sure it missed none |
| Switch public access on for a storage account, using the tenant's exclusion tag | Rejected: the policy has an exclusion tag, and the tenant's own tag policy puts it on every new resource, so an update would be let through. But the tenant switches public access off as a security control (its policy is named for the Secure Future Initiative), and the owner has said the governance has no opt-out. Dojo does not work around it |
| **Blob Storage behind a private endpoint; the web app's folder becomes a copy that every write goes through** | **Chosen** |

## The decision

**Every file Dojo keeps is put into a private blob container, and the web app's folder is a working copy. The container is where the data lives.** The code is `app/blobstore.py`
and the hooks in `Store` (`app/core.py`); the resources are in `infra/main.bicep`.

**The resources.** A storage account (`Standard_ZRS`, Hot; no public network access, no shared keys, no anonymous access, TLS 1.2) with one container, `dojo-data`, and blob soft delete
for 7 days. It is reached through a private endpoint in the lab VNet and a private DNS zone, `privatelink.blob.core.windows.net`, linked to that VNet: the same arrangement as the lab database.
Before adding the zone the environment was read: the tenant's Deploy-and-Modify initiative holds no private DNS policy, the VNet is not peered and has Azure's own DNS, and the only
private DNS zone in the group is the lab database's own. There is no central zone to use. The app's managed identity has the role Storage Blob Data Contributor on the resource group, made once by the owner in
`infra/bootstrap/app-roles.bicep` (the pipeline never assigns roles). Zone-redundant keeps the data in the one region the notice names; geo-redundant would add a copy in a second region and a sentence to the notice.

**Three kinds of file.** The data folder keeps the names it always had, and they are the blob names.
- **Small files** (documents, logs, lessons, sources, the timings of a film): always on the folder, and put under the Store's own lock, so storage sees the writes in the order the folder did.
  A log is put whole again after each line (the biggest is 0.6 MB).
- **Audio and video** (`.mp3`, `.webm`): written once and never changed, because their names are hashes of what they say. They are put after the lock is released, and fetched from storage the first time
  they are used (the media route, the checks, the podcast's joined episodes). `exists`, the listings and the sizes know what only storage holds.
- **Not kept in storage:** temporary files, the sourcing leases (they only mean something on one machine) and the podcast's joined episodes (built again from the narration).

**The folder wins.** A folder that has data is never overwritten from storage; storage is made to match it. Only an empty folder (a new web app, a lost disk), or one with a `.blob-restoring`
marker, is filled from storage, and then the small files come back before the app serves anything. If that is needed and storage cannot be reached, Dojo does not start: starting with nothing would begin a
second, empty Record. A web app that has data and cannot reach storage starts anyway, says `degraded`, and keeps trying.

**A write that storage cannot take is not lost and does not fail the request.** The folder has it, the name waits, and a background thread puts it until storage takes it. After two failures in a row Dojo stops asking for 20
seconds, so a storage that is down does not make every write wait for a timeout.

**A delete is remembered.** `.blob-deletes.json` names every file whose delete storage still owes. It is written before the folder's copy goes, and when it cannot be written nothing is deleted (the request fails): the note is all that stops a
restore from bringing the data back. It is repeated until storage has done it, and a restore skips what is in it. A folder deleted while storage had never been listed in this run (Dojo restarted while storage was down) is written down as a
folder, and once storage can be listed everything it holds under that folder, and the folder does not, is deleted too (audio that only storage holds is found that way). A journal that exists and cannot be read is moved aside to `.blob-deletes.json.unreadable`, not treated as empty: storage says `degraded`, and an empty folder is not restored, for as long as that file is there (the owner looks at it and removes it). A put or a download that began before a delete can never undo it (each name counts the deletes that began, and a
put or download that finds the count changed leaves the delete in force), and a video that only left the disk, as eviction makes it, was not deleted, so its own put never deletes the copy in storage.

**What `degraded` means.** Storage does not answer; or it lists but takes no writes while some wait; or the folder and storage were found different twice in a row; or the list of deletes owed could not be kept. `migrating` is all well with
something still to put, delete or check, and it does not stop Dojo or a deploy. A pass stops at the first failure of storage and is never reported as done, and after each pass that does not end clean the next waits longer (15 seconds, doubling
to 4 minutes). A call made while the Store's lock is held never waits: one try, short timeouts, and the token comes from a cache that the background thread renews at least every 10 minutes, so a managed identity that answers slowly cannot
freeze the app.

**Checked, not trusted.** Every file is sent with its MD5; the service refuses a body that does not match and keeps the sum it computed. Each pass ends by listing the container again and comparing every file with the folder (the
size of each, and the service's MD5 with the one that was sent): `.blob-report.json` holds the result, `/api/diag` shows the counts, and the log says "storage matches the folder" or what does not. The comparison also runs once an hour, so
a blob changed from outside is found and made right again (the folder wins).

- **What changed around it.** Going over the video budget (6 GB) frees the disk's copy of the coldest video, and only when storage holds exactly the same bytes (the same size and MD5); the video stays in storage and plays again from there
(`Store.evict`). The disk's budget is kept where films come back too: when playback, or the delivery check, brings a film from storage onto the disk, the coldest other films leave the disk to make room (they stay in storage), so the disk's share
of films stays inside the budget however many films storage holds. The gate that pauses the filming Dojo does by itself (at 90% of the budget) still counts every film Dojo holds, on the disk or only in storage, as it always counted what was
kept; the owner's own "Film this lesson" was never behind it. Letting storage hold more films than the disk budget would need that gate to count the disk's films only: a decision for when filming is switched on again, because filming
costs AI money. `/healthz` says `"storage"`, and a Dojo that cannot reach its storage is `degraded`, so the deploy's smoke test does not pass for it. The notice and the About
page say where the data is (notice version 3: every member is asked again, in team mode), and the privacy sheet, the architecture and the operations guide say so too, and no longer claim backups that did not exist.

## The first start with the data that is already there

The deploy that carries this finds a folder with about 12,100 files and an empty container. It starts as before (the app serves at once) and a background pass puts everything: the 3,989 small files first, then the 8,139 audio and video files
(8.6 GB); the two lease files are not kept. Nothing is deleted from the folder. The pass ends with the comparison above; the migration is done when `.blob-report.json` says `checked` is the number of files and every count is 0, and
`/healthz` says `"storage": "ok"`.

## Consequences

- **Cost:** about USD 8 a month more, and nearly all of it is the private endpoint (about USD 7.30) and the private DNS zone (about USD 0.50). The storage itself is about USD 0.20 for 9 GB. Deleting the lab database, its
  private endpoint and its zone (about USD 28) is a separate decision and is not changed here.
- **A deleted learner's data stays recoverable in storage for 7 days** (blob soft delete), then is gone. It is gone from the web app at once. The privacy sheet says so.
- **A write takes one more request** (about 40 ms) for a small file. Reads are unchanged: they are on the folder.
- **Disk:** the folder still holds everything (a copy), so nothing frees on the web app's disk by itself. The disk's copy of a video goes when the budget needs the room, and Dojo fetches it again from storage when it is played.
- **Known limits.** A single file may be 256 MB at most (the biggest film is 23 MB). A log is put whole after every line, so a log of tens of megabytes would make each line slow: the answer then is an append blob. The folder is a
  copy of one process's state: running two workers or two instances is still not supported (ADR 0009). The platform nevertheless runs the old and the new container side by side for some minutes while a deploy replaces one with the other, on the same folder and the same container. That is why a deploy waits for a quiet moment (`HANDOVER.md` 9.3, the quiet check): two writers can break what one process keeps (each Store has its own lock and its own idea of the Record's last event, so alternating appends from two processes could break the chain), and nothing here changes that. What this change corrects is smaller: each writer's note of the sum it last sent could be stale once the other had put the same bytes, and a pass that finds storage holding exactly the folder's bytes now takes those as the baseline. (Before 18 November 2026 the note stayed stale: one file looked different at every comparison, for good, `/healthz` said `degraded`, and the deploy of that evening ended red.) A file that something other than the Store writes is put by the hourly pass (and by `Store.publish` where the code
  knows: the push key). If Dojo dies between the note of a delete and the folder's own delete, the folder is left as it was and the replay deletes only storage's copy, which the folder then puts back: the folder wins, the request was
  never confirmed, and asking again deletes both (at start, team mode also deletes again the folder of any key on its deletion list). If the note cannot be written and storage cannot be reached either, a delete fails rather than go ahead without it.
- **Not covered:** the loss of the whole region (zone redundancy only), and the loss of the storage account itself (a resource lock would help; the pipeline cannot make one, so the owner may).
- **Studio's Money tab is not changed here, and one of its labels is now too narrow.** Its group "Private link to the database" will also hold the storage's private endpoint (about USD 7.30 a month more). Studio's wording is designed before it changes (AGENTS.md rule 13, ADR 0025), so the label goes in the next Studio change, together with the sentences ADR 0029 lists.
- **The Record's rule is unchanged:** nothing here edits or removes an event; only deleting a whole Record removes them, as before.

## How we know it worked

After the deploy that carries it: `/healthz` shows the new build and `"storage": "ok"`; `.blob-report.json` says every file of the folder is in storage with the same size and the MD5 the service holds for it;
the storage account's own metrics (the number of objects and the used capacity) agree with the folder, which is a check made outside the app. Tests start a web app on an empty folder against a stand-in for the Blob service and
get the whole Record back with its chain intact, refuse to start when there is nothing to stand on, and keep a delete that storage could not do from being undone (`tests/test_blobstore.py`).

## Review triggers

A second region is wanted (geo-redundant storage); a log or a file outgrows the limits above; team mode is switched on (the 7-day recovery window is then a statement to each member); the tenant gives storage accounts a way to be
reached other than a private endpoint; Dojo moves to a plan whose disk does not need to hold a copy.
