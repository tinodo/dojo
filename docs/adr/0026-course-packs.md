# ADR 0026 - Course packs: finished work can be handed to another Dojo, and is checked there as if it were new

- Status: proposed (not built)
- Date: 2026-10-20
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the lead, at the owner's request to form the idea first: is it feasible, is it usable, and how would it be designed.
  Nothing is built. The owner can reject or change it. Review triggers are below.
- Guardrails: EG-1 (a catalog cannot stand in for the learner), EG-12, EG-14, EG-33, EG-34, EG-37, EG-39, EG-41, EG-42, EG-43, EG-45;
  ADR 0005, 0006, 0008, 0009 (option 2), 0014, 0020, 0025; `docs/ui-principles.md`.

## The requirement

The owner, 20 October 2026: "Creating these course artifacts is expensive. The solution is open source on GitHub. I don't want everyone to
have to create the same artifacts." He asked for a way to share courses, first with one trusted publisher (himself), and named the danger:
nobody may slip in rogue videos, whether offensive, advertisements or malware. He asked for the idea to be formed, not built, and for a design
that fits where Dojo is going, not who uses it today.

What a course costs, at list price, on the owner's Dojo:

- About $25 to build an added exam such as AZ-900 (the price Studio showed for it when it was built, at the end of September).
- About $74 for the deeper lessons (ADR 0008) and about $55 for Deeper Listen (ADR 0007) across the two built-in exams, so tens of dollars for
  each exam.
- About $1 per video minute for films (ADR 0006). On 19 October Studio offered "Film 72 lessons · about $137.96" (ADR 0025).

So films are about half of what an exam costs (an estimate from these numbers). They are also the big files: about 8.2 MB a minute
(`app/avatar.py`), so roughly 1 GB for an exam. Dojo keeps at most 6 GB of film and deletes the least recently served first
(`docs/architecture.md`), so a film that was paid for can be lost and bought again.

ADR 0009 already named this idea. Its second option, each colleague runs their own Dojo and "course packages are shareable files", was
rejected for now, and "becomes the plan if the privacy owner or a works council refuses operator access". This ADR is that design, written
before it is needed, and widened from colleagues to anyone who runs a Dojo.

## What the code makes true

The design rests on these facts. Each was read in the code on 20 October 2026.

1. Course content is plain files on the data share. A lesson is `content/lessons/<id>.json`, a deeper lesson `content/deep/<lesson id>.json`,
   a deck with its diagram specs `content/decks/<lesson id>.json`, the extra pages found for a built-in exam
   `content/package-sources/<exam>.json`, an added exam `packages/<exam>.json`. Sound and film are in `media/`. A diagram is a small
   structured spec that the browser draws (ADR 0014).
2. A lesson never changes once it is written (`Dojo.lesson_index`). A lesson written again is a new file that names the one it replaced
   (`upgrade_of`), and `lessons_for` lists a skill's lessons newest `created` first. So a date that a pack puts on a lesson could decide
   which lesson wins.
3. Narration is `media/a<39 hex>.mp3` and a film is `media/v<39 hex>.webm` with a `.json` timing file. Each name is a SHA-256 of the words
   spoken and, for a film, the presenter's settings (`speech.media_id_for`, `avatar.media_id_for`). The name proves the words, not the bytes
   of the file.
4. Practice questions, the question pool and practice exams belong to one learner (`learners/<key>/pool`, `learners/<key>/exams`,
   `app/exam.py`). A lesson carries its own self-check.
5. A lesson record also keeps what failed the checks (`blocked`), the rounds and the models (`Dojo.make_lesson`). Delivery results are written
   under `content/delivery`. The owner's check-question audio is in the shared `media/` folder (`app/checks.py`). None of this is course material.
6. Preparer, Deeper lessons, source drift and the delivered-meaning check write and check content on their own, within daily counts
   (ADR 0006, 0008; `app/prepare.py`, `app/deeper.py`, `app/drift.py`, `app/delivery.py`). Budgets with caps exist in team mode only, and the
   owner is not capped (`app/budget.py`, `docs/operations.md`).
7. The quote check reads a snapshot of the official page that Dojo took itself, in `sources/<key>.json`, from `learn.microsoft.com` and
   `docs.github.com` only (`app/sources.py`: `ALLOWED_HOSTS`, `Sources.get`, `quote_ok`). Anyone who could write a snapshot could make a forged
   quote pass.
8. A model call is a chat call with a system and a user message. It has no tools, and the credential is an `Authorization` header, not part of
   the prompt (`app/ai.py`).
9. Pages put text in with `createTextNode`, and the content security policy is strict (`app/static/app.js` `h()`, `docs/architecture.md`).
   Media is served by a name that matches a fixed pattern, with a fixed content type and Range support (`app/main.py`, `/api/media/{media_id}`).
10. Films are capped at 6 GB in all, and the least recently served is deleted first with its timing file (`app/avatar.py`,
    `docs/architecture.md`). Dojo does not approve a film that would not fit (`docs/user-guide.md`). `app/avatar.py` is frozen: its names
    are pinned by a golden test (AGENTS.md rule 6).
11. Built-in skill ids are fixed (`d1.g1.s1`). An added exam's skill ids end in six characters of a hash of the skill's text (`app/newexam.py`).
    `packages.validate` accepts at most 400 skills and 3 sources a skill, and only addresses on the two official hosts. `packages.blueprint`
    is a short mark of an exam's domains, weights and skills.
12. The Store refuses any name part that is not letters, digits, dot, underscore or hyphen, or that contains `..` (`Store.path`, `app/core.py`).

## The options

1. **Share nothing.** Every Dojo pays to build the same courses. This is the cost the ADR is about.
2. **Team mode only** (ADR 0009, option 1). Colleagues use the owner's Dojo, and lessons and films are shared there at no extra building cost.
   It exists and stays. It does not help someone who runs their own Dojo, and ADR 0009 says to look again if the privacy review or a works
   council refuses operator access.
3. **Hand over the data folder or a backup.** It carries every learner's Record, links and settings, and the ids inside belong to one Dojo.
   Rejected: EG-24, ADR 0009.
4. **A shared cache of artifacts, found by name.** The names are hashes of the words spoken, so two Dojos only meet if their author models
   wrote the same words, which they do not. Rejected.
5. **Courses as git repositories.** Git holds text well and films badly (very large files, paid storage). Partly taken: the list of packs is
   a file in git. The films are not.
6. **A hosted Dojo, or a platform with accounts, uploads and ratings.** The owner would become host and moderator, and Dojo is a one-owner
   product. Rejected: EG-1.
7. **Sealed packs, static lists, and a check on arrival** (chosen).

## The decision

### 1. A pack is one exam at one point in time, sealed

A pack is a manifest and up to three groups of files. It is made of *lesson bundles*: a lesson, its deeper lesson, its deck, the narration it
uses and the films of its presenters.

- **Words:** lessons, deeper lessons and decks with their diagram specs (JSON). For an added exam also its package file and the list of extra
  pages (addresses only, never page snapshots).
- **Voices:** slide narration and Deeper Listen (`mp3`).
- **Films:** presenter films (`webm`) and their timing files (`json`). About 8.2 MB a minute.

A group is a download, not a product of its own. The names of narration and films are hashes of the exact words (fact 3), so a pack's audio and
film only play with the pack's own lesson, or with a local lesson that has exactly the same words. They are never put on a different lesson.

Everything inside is written from a fixed list of fields for each kind (the fields that teach, the pages the lesson cites and the models that made
it), rebuilt and not copied as a file. The list is fixed in the build and covered by a test. Never in a pack: anything under `learners/` or
`team/`, page snapshots from `sources/`, the quality log, delivery results, job results, check-question audio, links, settings, costs, `blocked`
and `rounds`. Practice questions are not in a pack (fact 4): each Dojo writes its own for its own learners, from the pages the lessons cite.

The manifest, in outline (the schema is fixed in the build, with golden tests):

```json
{
  "schema": 1,
  "id": "az-900@<blueprint mark>+1",
  "exam": "AZ-900",
  "made_on": "2026-10-12",
  "publisher": {"id": "...", "name": "..."},
  "label": "Unofficial. Written by AI from official pages as of 2026-10-12. ...",
  "sources": [{"url": "https://learn.microsoft.com/...", "publisher": "Microsoft Learn", "note": "..."}],
  "lessons": [{"skill": "d1.g1.s1.xxxxxx", "text_mark": "...", "audio": ["a..."], "films": {"guide": "v...", "coach": "v..."}}],
  "files": [{"path": "lessons/lesson-....json", "bytes": 0, "sha256": "...", "group": "words"}],
  "groups": {"words": {}, "voices": {}, "films": []},
  "signature": null
}
```

A pack's name is the exam, the blueprint mark and a build number. A published pack never changes. A correction is a new build.

### 2. A pack waits, is checked, and is then Dojo's own

A pack goes through four steps. In team mode only the owner presses them. Members see labels and nothing else.

1. **Available.** It is in a list. Nothing is downloaded.
2. **Added.** Dojo downloads the groups the owner chose and checks them at the door (section 3). Adding is free of model calls and is all or
   nothing. Added lessons are marked `from_pack` and wait: Preparer, Deeper lessons, source drift and the delivered-meaning check skip them, so
   nothing paid starts on its own.
3. **Checked here.** The owner presses one button with its price. A second model family re-checks each claim against its quote and excerpt, as
   when Dojo writes a lesson (EG-14). The listener transcribes the audio and films and compares them with the approved words.
4. **Dojo's own.** After the check Dojo looks after the lessons like any lesson, including writing one again when an official page changes.
   That costs money in the way it already does.

Rules that go with the steps:

- **Gaps only.** A pack lesson is added only for a skill that has no lesson. If the local lesson has exactly the same words (the same text
  mark), only the missing audio and film are added. Anything else is left alone and named in the preview. This keeps an added lesson from
  outranking a local one (fact 2).
- **Who sees an added lesson.** From a publisher the owner trusts (the official library by default) it is shown with a label. From anyone else it
  is shown to the owner only, until the check passes.
- **Updates.** A newer build replaces a lesson only where the skill's current lesson is still the pack's, untouched. A skill that Dojo wrote
  again is local, and stays.
- **Removal.** "Remove what this pack added" removes the lessons it added that nothing has replaced, and the media that no other lesson uses.
- **Films and room.** Added films count against the 6 GB film budget, and films that would not fit are refused with the room they need, as film
  approvals are (fact 10). They can be deleted to make room like any film, and added again from the pack file.

### 3. The door

Every pack is treated as hostile, the owner's own included: an account can be taken over, and a mistake is a mistake. Each check is a
pure function with tests (EG-33).

| # | Check | Cost | What it stops |
|---|---|---|---|
| 1 | **Container and names.** Ceilings on size, file count and expansion. Entries are read as a stream and nothing is written until everything passes. Every name must pass the Store's rule (fact 12). No links, no absolute or parent paths, no duplicates, only the expected folders and extensions. | free | archive bombs, path tricks, files that do not belong |
| 2 | **Fingerprints.** Every file against the manifest, and the manifest against the list entry it came from. | free | a swapped or damaged file |
| 3 | **Shape.** Each JSON goes through the validator of its kind: `packages.validate` for a package, `deck.clean_spec` for a deck, a strict schema for lessons and deeper lessons that drops unknown fields. Text is stored and shown as text only (fact 9). | free | markup, scripts, unknown fields, oversize text |
| 4 | **Dates and ids.** The importer sets `created` itself. The publisher's date is kept only as the made-on date. An id that exists is refused, never overwritten. | free | a far-future date that outranks every later lesson; overwriting |
| 5 | **Quotes.** Dojo fetches the official pages itself through `Sources.get` (official hosts only, its own limits and backoff) and finds every quote word for word. Snapshots in a pack are never read (fact 7). A lesson with a quote that is not found is not added, and is counted in the preview. | no model money; takes fetch time | forged or invented quotes; ads and free text that no official page backs |
| 6 | **Media shape.** The first bytes and the container header are read by a small parser of our own, with limits on duration and size. The media name must equal the hash Dojo rebuilds from the added lesson's words. A mismatch drops the media, and the lesson shows without it. Nothing is decoded on the server. | free | the wrong kind of file, polyglots, oversize files, media on the wrong lesson |
| 7 | **Claims** (the owner's choice, priced). The second model family re-checks each claim. | priced | a true quote used for a false claim, which the quote check cannot see |
| 8 | **Listener** (the owner's choice, priced). The delivered-meaning check on audio and film: transcribe and compare with the approved words. A mismatch drops the media. | priced | audio or film that does not say the approved words. Nothing checks the pictures of a film |
| 9 | **Ceilings.** At most 400 skills, as for an added exam. Limits per file and per pack. Films must fit the film budget. | free | spend and disk exhaustion |

Because nothing checks the pictures of a film, films from a publisher the owner does not trust need the listener and a plain yes from the owner.

Also at the door:

- **Importing never calls a model.** Imported text reaches a model only when the owner presses Check. Those calls have no tools (fact 8) and
  must treat lesson text as quoted data (EG-37). The build audits every prompt that can see a lesson, makes that rule the same in all of
  them, and adds a test that fails when one does not.
- **Fetching.** Dojo's server downloads, never the browser. Only https. Only hosts the owner approved when adding the list. Redirects only
  within them. The address must resolve to a public address. No credentials are sent. Size and time limits.
- **An atomic commit.** Files go to a staging folder, everything is validated, then they are moved into place and one import record is written
  (`content/packs/<id>.json`: what was added, from where, fingerprints, checks and times).

### 4. Lists and publishers

- **A list** is a static JSON file at an https address. An entry has the pack name, exam, blueprint mark, publisher, made-on date, label,
  sources and licence notes, and for each group its size, SHA-256 and one or more addresses (mirrors). It has the lesson count and the counts
  that Dojo's price code needs (characters spoken, video minutes), and a status: active, or withdrawn with a date and a reason. A dollar saving is
  never in the list. Dojo works it out from the counts with `app/costs.py` at today's list price, or says that the price is not available.
- **Which lists.** Dojo reads the lists the owner added in settings. The default is the official Dojo library: a list in the owner's repository.
  Anyone can host another list, so a team can keep a private one.
- **Trust.** The owner trusts publishers in settings. The official library's publisher (the owner) is trusted by default, because it is the same
  GitHub account as the code the Dojo runs, so it adds no new trust. Another publisher signs their packs and list entries with a key, and the
  owner adds that key to a trust list with one plain sentence. The manifest and the list entry carry `publisher` and `signature` fields from the
  first version, so the format does not change later. Signing needs a library, which is a dependency that EG-43 gates (R5).
- **Publishing has no upload service.** The publisher makes the files in Studio, uploads them anywhere (a GitHub release is enough) and adds an
  entry to a list by pull request. The list's own repository checks every entry in CI with the same code as the door, recomputes the
  fingerprints, and runs the scans Dojo cannot (malware, and content safety on sampled film frames). So a pack is checked before it is listed
  and again when it arrives.
- **Withdrawal.** A list entry can be marked withdrawn with a reason. A Dojo that added the pack shows a card on To do and keeps the lessons
  until the owner decides. Nothing is deleted silently. A signing key can be revoked the same way. Neither recalls copies already downloaded.
- **Reports.** Each added lesson has a link, "Tell the publisher something is wrong", that opens the publisher's issue page with the pack, the
  build and the lesson filled in.
- **Not planned:** open uploads, accounts, payments (EG-1).

### 5. Rights and labels

EG-39 says rights are decided for each operation. For a pack the operations are:

- **Quoting short passages with a link.** Dojo does this today (`THIRD-PARTY-NOTICES.md`).
- **Repeating those quotes inside a pack.** Each page's source, publisher and licence note are listed in the manifest. Dojo's `license` note on a
  page is its own note and not a grant (`THIRD-PARTY-NOTICES.md`). The skill statements of a study guide stay quoted as they are in the
  repository.
- **Handing on AI-written lessons.** This is the publisher's text (R2: its licence).
- **Handing on generated speech and avatar video.** This is an open rights question (R1). Voices and likenesses need the rights for the format
  and the audience (EG-39), and the terms of the speech and avatar service are the owner's to read. Until R1 is answered, a pack holds words
  only.
- **Real exam items.** Never. Dojo never holds one (`README.md`, `THIRD-PARTY-NOTICES.md`), so the exporter has none to export, and the label
  says so.
- **The right to publish.** Whoever publishes establishes their own right to (EG-39, EG-41). The page that makes the files asks them to say so
  before it writes anything.

The label is in every pack, in every list entry and on every added lesson: "Unofficial, not affiliated with or endorsed by Microsoft or GitHub.
Written by AI from official pages as of <date>. Practice questions are made by each Dojo and are not taken from a real exam. Not a promise that
you will pass." It follows the notice already in `THIRD-PARTY-NOTICES.md`.

A complaints address is in every list and every pack. A legal check comes before the first pack from anyone but the owner (R3).

### 6. The owner's pages

The sentences come first (`docs/ui-principles.md`, AGENTS.md rule 13). They are a spec: each is true only when the build behind it exists, and
before any page is built two reviewers from different model families read the words. The numbers below are made up to show the shape.

The owner's questions, in his words. "I don't want everyone to have to create the same artifacts" becomes *Is there a finished course I can add
instead of paying to make it?* From ADR 0025, whether it was "safe to film or money down the drain" becomes *Is it safe to add, and what does
it cost?* And a third: *How do I give one of mine away?*

It is not a new tab. A finished course shows up where the owner already decides to spend.

**Add an exam**, after he types the code and sees the price of building it:

- Card: "A finished course is in your library." "AZ-900 · 62 lessons · made 12 October by Tino. Adding the words and voices costs nothing and
  saves you about $25. Its films are 410 MB, cost nothing and save about $230 more."
- Buttons: "Add the finished course · free", next to the existing "Build this course · about $25".
- Sheet: "Add AZ-900 from Tino's library?" "62 lessons to add. You already have 0 of them." "Dojo reads 140 quotes on 21 official pages first.
  Lessons whose pages changed since it was made are left out: 3, named below." "Nothing is rewritten, listened to or paid for until you press
  Check." Buttons: "Add the finished course", "Cancel" (focused).
- True when: the preview of section 3 exists, and the skip of section 2 holds.

**To do, under "Optional · costs money":**

- Card: "Finished courses you added." Row: "AZ-900 · 62 lessons added, not checked on this Dojo yet · Check 62 lessons · about $6.20".
- Sheet, at list price: "Claims: a second AI model reads 62 lessons · about $4.30." "Narration: 62 lessons listened to · about $1.20." "Films:
  41 lessons listened to · about $0.70." Then, for a trusted publisher: "Until you check, learners see these lessons with a note." For another
  publisher: "Until you check, only you see these lessons." And: "After you check, Dojo looks after these lessons like its own. It writes them
  again when an official page changes, and that costs money as it does today." Cancel is focused.
- True when: the check is priced by `app/costs.py`, and the visibility rule of section 2 holds.

**Courses, on a course's card, "Share this course":**

- "Make files of this course that someone else can add. Nothing is uploaded, and nothing about any learner is in them." Button: "Make the files · free".
- Afterwards: "Made: words for 62 lessons, voices for 62, films for 41 (410 MB)." A link to the list of every file. A box: "I have the right to publish
  this." And: "The lessons are offered under the licence you chose. Quotes stay under their pages' terms."
- True when: the exporter writes its contents list, and R2 is answered.

**On a lesson that came from a pack,** one line under the title: "From Tino's library, made 12 October. The quotes match the official pages. This
Dojo has not checked the claims yet." After the check: "From Tino's library, made 12 October. Checked on this Dojo on 21 October."

**To do, under "Needs you · costs nothing", when a list withdraws a pack:** "Tino's library withdrew AZ-900 on 3 November: 'a lesson was wrong'. Your
62 lessons from it stay until you decide." Buttons: "Remove what it added", "Keep them".

### 7. Build order

Each step is useful alone and can be undone. Steps 1 and 2 are code that no page shows.

0. This ADR, and the owner's answers to R1 to R3.
1. **The format, the builder and the validator,** as pure functions with golden tests (`app/packs.py`), and a script that makes a pack from a Dojo's
   data folder and checks a pack file. No routes and no pages. It is useful to the owner alone: a portable copy of finished work, and a way to put
   back films that the 6 GB cap deleted instead of buying them again at about $1 a minute.
2. **The importer and its waiting state:** staging, the door, `from_pack`, the skip in Preparer, Deeper lessons, source drift and the delivered-meaning
   check, the label on a lesson, one admin-only route in the route inventory. Tests with hostile packs (path tricks, archive bombs, id collisions, a
   far-future date, forged quotes, the wrong media) and with a Dojo that has no money cap.
3. **The owner's pages,** as section 6: two reviews of the words, a build against made-up answers, every state looked at on desktop and on a phone,
   and node tests in the pattern of `tests/test_studio_v2.py`.
4. **The list reader,** the settings for lists and trust, the official library repository and its CI.
5. **Other publishers:** signing (R5), the trust list, revocation, the scans in the list's CI, the complaints and take-down routine, and the legal check (R3).
6. **Later:** a choice per skill to use the pack's lesson instead of the local one, mirrors, private lists, and corrections sent back to the publisher.

## What it changes

- **Code:** a new `app/packs.py`; a skip for `from_pack` lessons in `app/prepare.py`, `app/deeper.py`, `app/drift.py` and `app/delivery.py`; a label
  in `app/learning.py`; admin routes in `app/main.py` and the route inventory; cards in `app/studio.py` and `app/static`; tests for each; the user
  guide, the privacy sheet, the operations page and the module table of `docs/architecture.md`. `app/avatar.py` stays frozen. No new dependency
  until another publisher needs signing.
- **Data:** `content/packs/<id>.json` import records, a `from_pack` field on an added lesson, and settings for lists and trusted publishers.
- **Money:** adding costs no model money. A check costs what the price code shows first. Hosting costs the owner nothing (static files, release
  assets).
- **Privacy:** a pack holds no learner data. The exporter writes allowed fields only, and the owner reads the list of files before he shares. Dojo's
  server downloads packs and lists, so the host sees the server's address and never a learner's. `docs/privacy.md` says so when this is built.
- **Evidence:** a pack never carries evidence. Nothing in it can fill a pip, and a learner's Record is still made only on their own Dojo
  (EG-1, EG-34).

## Risks

| Risk | How the design meets it |
|---|---|
| Hostile text in a lesson | Stored and shown as text under the strict content security policy. A schema drops unknown fields. Every quote is found again on the official page. Others than a trusted publisher are checked by a second model before anyone but the owner sees them. |
| A true quote used for a false claim | The quote check cannot see it. The priced claims check can. The label says which checks were done. |
| A wrong or hostile media file | Shape checks, the name bound to the words, the fingerprint, and the listener. Nothing checks pictures, so films come from trusted publishers, or after the listener and the owner's yes. |
| Spending | Adding calls no model. Added lessons wait. Ceilings and the film budget. After the check, Dojo's usual daily counts. Outside team mode there is no money cap (fact 6), and the check sheet says what it will cost first. |
| A taken-over publisher account | For the owner the root of trust is the code account, which already deploys the Dojo. For others, signing keys and revocation. Withdrawal. |
| Private data in an export | Fixed field lists, the list of files shown before saving, and a test that exports a Dojo seeded with private marker strings and fails if one appears. |
| A pack that has gone out of date | The made-on date. The preview counts lessons whose pages changed. After the check, source drift keeps them current. |
| Rights | Section 5, and R1 to R3. |
| Bandwidth | Static hosting, optional films, mirrors. |
| The server fetching a hostile address | Approved hosts only, public addresses only, limits. |
| Prompt injection | No tools, text as data, and no model call on imported text before the owner presses Check. |

## Review triggers

- The first pack by anyone but the owner, or the first list other than the official one.
- Anything that lets Preparer, Deeper lessons, source drift, the delivered-meaning check or a model prompt see an added lesson before the check.
- R1 answered: films and voices may or may not be handed on.
- A pack grows past what a release asset can hold, or past the film budget of an ordinary Dojo.
- The privacy review or a works council refuses team mode (ADR 0009): this ADR then becomes the plan for colleagues.
- Any change that gives a model call tools or secrets.

## Left to the owner

- **R1:** may generated speech and avatar video be handed on? Until it is answered, a pack holds words only.
- **R2:** the licence of the publisher's own text in a pack. Suggestion: CC BY 4.0, quotes excluded.
- **R3:** when the first other publisher may join, and the legal check before it.
- **R4:** the word on the face of the pages. Suggestion: "finished course", with "pack" in the docs and code.
- **R5:** the signing library, decided when the first other publisher asks (EG-43).
