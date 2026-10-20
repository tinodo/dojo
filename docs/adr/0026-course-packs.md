# ADR 0026 - Course packs: finished work can be handed to another Dojo, and waits where nothing reads it until it is checked

- Status: proposed (not built)
- Date: 2026-10-20
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the lead, at the owner's request to form the idea first: is it feasible, is it usable, and how would it be designed.
  Nothing is built. The owner can reject or change it. Review triggers are below. Three reviewers from three model families read the first
  version against the code; this text is the second.
- Guardrails: EG-1 (a catalog cannot stand in for the learner), EG-12, EG-14, EG-19, EG-33, EG-34, EG-37, EG-39, EG-41, EG-42, EG-43, EG-45;
  ADR 0005, 0006, 0008, 0009 (option 2), 0014, 0020, 0025; `docs/ui-principles.md`.

## The requirement

The owner, 20 October 2026: "Creating these course artifacts is expensive. The solution is open source on GitHub. I don't want everyone to
have to create the same artifacts." He asked for a way to share courses, first with one trusted publisher (himself), and named the danger:
nobody may slip in rogue videos, whether offensive, advertisements or malware. He asked for the idea to be formed, not built, and for a design
that fits where Dojo is going, not who uses it today.

What a course costs, at list price, on the owner's Dojo:

- About $25 to build an added exam such as AZ-900 (the price Studio showed for it at the end of September).
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

1. Course content is plain files on the data share. A lesson is `content/lessons/<id>.json`. `content/deep/<lesson id>.json` holds a lesson's
   Deeper Listen, and `content/decks/<lesson id>.json` its deck with diagram specs. `content/package-sources/<exam>.json` holds the extra pages
   found for a built-in exam, and `packages/<exam>.json` an added exam. Sound and film are in `media/`. A diagram is a small structured spec that
   the browser draws (ADR 0014). A lesson written on the deeper template (ADR 0008) is an ordinary lesson file with a newer template number.
2. A lesson never changes once it is written (`Dojo.lesson_index`). A rewrite is a new lesson file. Only some rewrites name the lesson they
   replace (`upgrade_of`): the deeper-template rewrite does, the rewrite after a page changed does not (`app/prepare.py`). `lessons_for` lists a
   skill's lessons newest `created` first, and most readers take the first. So a date that a pack puts on a lesson could decide which lesson wins.
3. Narration is `media/a<39 hex>.mp3` and a film is `media/v<39 hex>.webm` with a `.json` timing file. Each name is a SHA-256 of the SSML that
   makes it (and, for a film, the presenter's settings): `speech.media_id_for`, `avatar.media_id_for`. The name proves the words and voices that
   were asked for. It does not prove the bytes of the file, and it does not prove what the file says.
4. Practice questions, the question pool and practice exams belong to one learner (`learners/<key>/pool`, `learners/<key>/exams`,
   `app/exam.py`). A lesson carries its own self-check.
5. A lesson record also keeps what failed the checks (`blocked`), the rounds and the models (`Dojo.make_lesson`). Delivery results are written
   under `content/delivery`. The owner's check-question audio is in the shared `media/` folder (`app/checks.py`). None of this is course material.
6. Work that costs money runs on its own, in more places than the four services this ADR first named (`docs/architecture.md`, "Background
   services and their limits"): Preparer (drift rewrites 10 a day, then missing lessons, deeper-template upgrades 20 a day, narration, Deeper
   Listen 20 a day, decks, approved filming), the question pool, the Deeper lessons search, the delivered-meaning check, and the startup self-test.
   Missing lessons and narration have no daily count. And opening a lesson can start a paid job: `POST /api/lessons/{id}/audio` narrates a
   lesson that has no audio, and `POST /api/lessons/{id}/deck` draws a deck on the first open (`app/main.py`). Budgets with caps exist in team
   mode only, and the owner is not capped (`app/budget.py`, `docs/operations.md`).
7. The quote check reads a snapshot of the official page that Dojo took itself, in `sources/<key>.json`, from `learn.microsoft.com` and
   `docs.github.com` only (`app/sources.py`: `ALLOWED_HOSTS`, `Sources.get`, `quote_ok`). `Sources.get` returns a snapshot under a week old
   without fetching, and when a fetch fails it returns the last good copy marked `stale`. Anyone who could write a snapshot could make a forged
   quote pass.
8. Ordinary model calls are chat calls with a system and a user message. They have no tools, and the credential is an `Authorization` header, not
   part of the prompt (`app/ai.py`, `chat`). One other call uses a tool: `web_search` in the Responses API, used to find pages (ADR 0018). Pack
   checks use only the chat call.
9. Pages put text in with `createTextNode`, and the content security policy is strict (`app/static/app.js` `h()`, `docs/architecture.md`). Media
   is served by a name that matches a fixed pattern, with a fixed content type and Range support, to any signed-in learner who asks for that name
   (`app/main.py`, `/api/media/{media_id}`).
10. Films are capped at 6 GB in all. `Avatar.evict` deletes the least recently served first, with its timing file, until an incoming film fits
    (`app/avatar.py`). Dojo does not approve a film that would not fit (`docs/user-guide.md`). `app/avatar.py` is frozen: its names are pinned by
    a golden test (AGENTS.md rule 6).
11. Built-in skill ids are fixed (`d1.g1.s1`). An added exam's skill ids end in six characters of a hash of the skill's text (`app/newexam.py`).
    `packages.validate` accepts at most 400 skills and 3 sources a skill, and only addresses on the two official hosts, but it returns the object
    it was given, unknown fields included. `packages.blueprint` is a short mark of an exam's domains, weights and skills.
12. The Store writes one file atomically (temporary file, then rename) and refuses any name part that is not letters, digits, dot, underscore or
    hyphen, or that contains `..` (`Store.write`, `Store.path`, `app/core.py`). It has no way to commit several files together.

## The options

1. **Share nothing.** Every Dojo pays to build the same courses. This is the cost the ADR is about.
2. **Team mode only** (ADR 0009, option 1). Colleagues use the owner's Dojo, and lessons and films are shared there at no extra building cost.
   It exists and stays. It does not help someone who runs their own Dojo, and ADR 0009 says to look again if the privacy review or a works
   council refuses operator access.
3. **Hand over the data folder or a backup.** It carries every learner's Record, links and settings, and the ids inside belong to one Dojo.
   Rejected: EG-24, ADR 0009.
4. **A shared cache of artifacts, found by name.** The names are hashes of the SSML that makes the audio, so two Dojos only meet if their author
   models wrote the same words, which they do not. Rejected.
5. **Courses as git repositories.** Git holds text well and films badly (very large files, paid storage). Partly taken: the list of packs is
   a file in git. The films are not.
6. **A hosted Dojo, or a platform with accounts, uploads and ratings.** The owner would become host and moderator, and Dojo is a one-owner
   product. Rejected: EG-1.
7. **Sealed packs, static lists, and a check on arrival** (chosen).

## The decision

### 1. A pack is one exam at one point in time, sealed

A pack is a manifest and up to three groups of files. It is made of *lesson bundles*: a lesson, its Deeper Listen, its deck, the narration it
uses and the films of its presenters.

- **Words:** lessons, Deeper Listen scripts and decks with their diagram specs (JSON). For an added exam its package file. For a built-in exam
  the list of extra pages (`content/package-sources/<exam>.json`). Addresses only, never page snapshots.
- **Voices:** slide narration and Deeper Listen (`mp3`).
- **Films:** presenter films (`webm`) and their timing files (`json`). About 8.2 MB a minute.

A group is a download, not a product of its own. The names of narration and films are hashes of the SSML (fact 3), so a pack's audio and film
belong to the pack's own lesson, or to a local lesson whose narration is exactly the same. They are never put on a different lesson.

Everything inside is written from an allow-listed schema for each kind (the fields that teach, the pages the lesson cites and the models that made
it), rebuilt and not copied as a file. The schemas are fixed in the build and covered by tests. Never in a pack: anything under `learners/` or
`team/`, page snapshots from `sources/`, the quality log, delivery results, job results, check-question audio, links, settings, costs, `blocked`
and `rounds`. Practice questions are not in a pack (fact 4): each Dojo writes its own for its own learners, from the pages the lessons cite.

The manifest, in outline (the schema is fixed in the build, with golden tests):

```json
{
  "schema": 1,
  "id": "az-900@<blueprint mark>+1",
  "exam": "AZ-900",
  "language": "en",
  "made_on": "2026-10-12",
  "publisher": {"id": "...", "name": "..."},
  "label": "Unofficial. Written by AI from official pages as of 2026-10-12. ...",
  "sources": [{"url": "https://learn.microsoft.com/...", "publisher": "Microsoft Learn", "note": "..."}],
  "lessons": [{"skill": "d1.g1.s1.xxxxxx", "skill_mark": "<hash of the skill's normalised text>", "text_mark": "...",
               "audio": ["a..."], "films": {"guide": "v...", "coach": "v..."}}],
  "files": [{"path": "lessons/lesson-....json", "bytes": 0, "sha256": "...", "group": "words"}],
  "groups": {"words": {}, "voices": {}, "films": []},
  "signature": null
}
```

A pack's name is the exam, the blueprint mark and a build number. A published pack never changes. A correction is a new build. Lessons match by
skill, not by exam: a skill matches when its id and the mark of its text are the same, so when a study guide changes two skills, the other
fifty-eight still match.

### 2. A pack waits in a place that nothing reads

This is the core of the design, and the first version got it wrong. It marked added lessons and asked four services to skip them. Fact 6 shows
that is not enough: opening a lesson starts paid jobs, a podcast feed lists lessons, a practice exam reads them, a media route serves any name.
Every one of those would need the new rule, and one missed reader is a hole.

So an added pack is kept apart: **`packs/<id>/` holds the pack's files, and `content/` and `media/` do not.** Every reader of lessons and every
route that serves them looks in `content/` and `media/` only. So nothing in Dojo can open, narrate, draw, film, feed, list, plan, serve or
rewrite what is waiting, because none of it is where those readers look. Waiting is not a flag. It is a place.

The place has one gap, found by reading `app/prepare.py`, and the design closes it. Preparer decides what to write by asking which skills have no
lesson (`lesson_skills`). A skill whose lesson is waiting in `packs/` has none, so Preparer would write it, and spend the money the pack was meant
to save. The same goes for `POST /api/lessons`. So there is one rule, in one place: **a new function, `skills_spoken_for`, lists the skills that
have a lesson or a lesson waiting in `packs/`, and only the two writers ask it** (`Preparer.next_skill` and the check in `new_lesson`). It must not be
`lesson_skills` itself. `lesson_skills` is also what the readiness page, the study plan's moves, Courses' "ready" count and the practice exam use to
mean "taught" (`app/readiness.py`, `app/exam.py`, `app/newexam.py`, `app/learning.py`), and a waiting lesson is not taught. Changing it would make
Dojo say that a skill is taught when no learner can open a lesson for it. It is one function and one test (a waiting skill is spoken for and is not
taught), not a skip in eight modules.

A pack goes through four steps. In team mode only the owner presses them. Members see labels and nothing else.

1. **Available.** It is in a list. Nothing is downloaded.
2. **Added.** Dojo downloads the groups the owner chose, checks them at the door (section 3) and keeps them under `packs/<id>/`. Adding calls no
   model and writes nothing into `content/` or `media/`. Studio's pack card can show a preview of what is waiting (owner only).
3. **Checked here.** The owner presses one button with its price. A second model family re-checks each claim against its quote and excerpt, as
   when Dojo writes a lesson (EG-14), and the listener transcribes the audio and films and compares them with the approved words. A lesson in
   which fewer than three quarters of the claims hold is dropped, as Dojo drops its own (`KEPT_SHARE`, `app/learning.py`). When a line fails
   that changes a lesson's words, so the audio and film made from the old words are dropped with it, because their names no longer match.
4. **Adopted.** Lessons that pass are moved into `content/` and their media into `media/`, in the order of section 3. From that moment they are
   Dojo's own lessons: every reader and every worker sees them, Preparer rewrites them when a page changes, and opening one can narrate or draw
   what is missing. All of that costs money in the way it already does. The owner is told so on the sheet before he presses.

What the owner and the learners see:

- **Before adoption nothing is visible to a learner,** whoever published the pack. That follows from the place, and it keeps EG-14 and EG-19:
  no learner is taught from a lesson no one checked here, and no teaching event is recorded for one. The owner can read a waiting lesson in
  Studio's preview.
- **Adoption needs the check, whoever published.** EG-19 asks for a blocking gate before anything learner-visible, so there is no shortcut for a
  trusted publisher. Trust (section 4) decides something else: whether films are accepted without a yes from the owner, and which publisher's
  name shows first. It is the owner's decision in settings, and never read from a field in the pack.

Rules that go with the steps:

- **Gaps only, with one exception.** A pack lesson is adopted for a skill that has no lesson. For a skill that has a lesson, nothing is replaced.
  The one exception is media: if the local lesson's narration is exactly the pack's (the same SSML, so the same names), the pack's missing audio
  and film are adopted for it. That is how a lost film comes back (section 7, step 2). Everything else is named in the preview and left alone.
- **Local ids, local dates.** Adoption gives each lesson a new local id and sets `created` to the adoption time. The publisher's date is kept
  only as the made-on date. A pack's `upgrade_of` and any other link to another lesson are dropped, and the importer builds the links it needs.
  Because the new lesson is the skill's only lesson, it is the one shown, and a later rewrite gets a newer `created`, so it wins as it should.
- **Gaps are decided under the store lock.** Preparer may write a lesson for the same skill while a pack is being adopted. Adoption checks the
  gap and writes inside the Store's lock, and refuses a skill that has a lesson by then. It updates the lesson index cache.
- **Updates.** A newer build is a new pack. It is adopted by the same rules, so it only fills gaps: a skill whose lesson came from the earlier
  build is replaced only if the owner presses "Use the newer one" for that skill (a later step, section 7).
- **Removal.** "Remove what this pack added" removes the pack's folder, and the lessons adopted from it that nothing has replaced, and the media
  that no other lesson uses.
- **Films and room.** Adopting films counts against the 6 GB film budget. When it is full, Dojo asks first, with the number of films it would
  delete (the least recently served, as in playback, `Avatar.evict`) and the room needed, and does nothing without a yes.

### 3. The door

Every pack is treated as hostile, the owner's own included: an account can be taken over, and a mistake is a mistake. Each check is a
pure function with tests (EG-33). The checks run when a pack is added, and again, for the files that move, when it is adopted.

| # | Check | Cost | What it stops |
|---|---|---|---|
| 1 | **Container and names.** Ceilings on size, file count and expansion. Entries are read as a stream and nothing is written until everything passes. Every name must pass the Store's rule (fact 12). No links, no absolute or parent paths, no duplicates, only the expected folders and extensions. | free | archive bombs, path tricks, files that do not belong |
| 2 | **Fingerprints.** Every file against the manifest, and the manifest against the list entry it came from. | free | a swapped or damaged file |
| 3 | **Shape.** Each file is rebuilt through an import schema of its kind that keeps only allow-listed fields, with limits on length and count. The existing cleaners (`packages.validate`, `deck.clean_spec`) are used inside it as parts. They are not the boundary: `packages.validate` returns what it was given (fact 11). Text is stored and shown as text only (fact 9). | free | markup, scripts, unknown fields, oversize text |
| 4 | **Ids, dates and links.** New local ids and the adoption time as `created`. Links to other lessons are dropped. An id that exists is refused, never overwritten (section 2). | free | a far-future date that outranks every later lesson; a foreign replacement link; overwriting |
| 5 | **Quotes.** Dojo reads the official pages itself through `Sources.get` (official hosts only, its own limits and backoff) and finds every quote word for word. Snapshots in a pack are never read (fact 7). Dojo records the snapshot's own date, and a quote counts as verified today only when the read was fresh: a snapshot under a week old counts, a copy marked `stale` does not, and the preview says which. A lesson with a quote that is not found is left out and counted, with the plain words "its quotes could not be found". A page that changed does not by itself make a quote false, and a missing quote proves a changed page, not a forgery. | no model money; takes fetch time | forged or invented quotes; ads and free text that no official page backs |
| 6 | **Media shape.** Expected container structure is checked with bounded work, with limits on duration and size. The name must equal the hash Dojo rebuilds from the lesson's words, voices and settings. A mismatch drops the media. The server decodes nothing; the browser plays. This does not prove a file harmless, and it does not rule out a polyglot file. The fingerprint (check 2) ties the bytes to the manifest, and the listener (check 8) checks what is said. | free | the wrong kind of file, oversize files, media on the wrong lesson |
| 7 | **Claims** (the owner's choice, priced). The second model family re-checks each claim. A lesson with fewer than three quarters of its claims holding is dropped. | priced | a true quote used for a false claim, which the quote check cannot see |
| 8 | **Listener** (the owner's choice, priced). The delivered-meaning check on audio and film: transcribe and compare with the approved words. A mismatch drops the media. | priced | audio or film that does not say the approved words. Nothing checks the pictures of a film |
| 9 | **Ceilings.** At most 400 skills, as for an added exam. Limits per file and per pack. Films must fit the film budget. | free | spend and disk exhaustion |

Because nothing checks the pictures of a film, films from a publisher the owner does not trust need the listener and a plain yes from the owner.

Also at the door:

- **No model sees waiting text.** Imported text reaches a model only when the owner presses Check. Those calls are chat calls, which have no
  tools (fact 8), and never `web_search`. The prompts must treat lesson text as quoted data (EG-37). The build audits every prompt that can see
  a lesson, makes that rule the same in all of them, and adds a test that fails when one does not. Only the deck prompt (`DECK_SYSTEM`) says in so
  many words that lesson and source text are data and not instructions. The Deeper Listen prompt and the checker prompts do not, and the build
  adds it to them.
- **Fetching.** Dojo's server downloads, never the browser. Only https. Only hosts the owner approved when adding the list. Redirects only
  within them. The address must resolve to a public address. No credentials are sent. Size and time limits.
- **Adoption is a journal, not a promise of atomicity.** The Store writes one file at a time (fact 12). So adoption writes a journal first
  (`packs/<id>/adopt.json`: what it will move), moves the files, and writes a completion mark last. On start Dojo finishes or rolls back an
  adoption with no completion mark. Readers see lessons only once their lesson file exists, and the media is written before the lesson that
  uses it, so a half-adopted pack never shows a lesson without its audio names. The test kills the process between every pair of writes.

### 4. Lists and publishers

- **A list** is a static JSON file at an https address. An entry has the pack name, exam, language, blueprint mark, publisher, made-on date,
  label, sources and licence notes, and for each group its size, SHA-256 and one or more addresses (mirrors). It has the lesson count and the
  counts that Dojo's price code needs (characters spoken, video minutes), and a status: active, or withdrawn with a date and a reason. A
  dollar saving is never in the list. Dojo works it out from the counts with `app/costs.py` at today's list price, or says that the price is
  not available.
- **Which lists.** Dojo reads the lists the owner added in settings. The default is the official Dojo library: a list in the owner's repository.
  Anyone can host another list, so a team can keep a private one. Two entries for the same exam are both shown, with publisher and date, and the
  owner picks.
- **Trust.** Trust belongs to a source, never to a name. A pack is trusted when it was fetched from the pinned official list address. That
  address is in the code, so it has the same root as the Dojo's own code. A `publisher` field in a pack or a list says who claims it and gives
  no authority: another list that names the owner gets no trust from the name. Another publisher is trusted only by a key. They sign their
  packs and list entries, and the owner adds that key to a trust list with one plain sentence. The manifest and the list entry carry
  `publisher` and `signature` fields from the first version, so the format does not change later. Signing needs a library, which is a
  dependency that EG-43 gates. The lead decides it when the first other publisher asks (R3).
- **Publishing has no upload service.** The publisher makes the files in Studio, uploads them anywhere (a GitHub release is enough) and adds an
  entry to a list by pull request. The list's own repository checks every entry in CI with the same code as the door, recomputes the
  fingerprints, and runs the scans Dojo cannot (malware, and content safety on sampled film frames). So a pack is checked before it is listed
  and again when it arrives.
- **Withdrawal.** A list entry can be marked withdrawn with a reason. A Dojo that added the pack shows a card on To do and keeps the lessons
  until the owner decides. Nothing is deleted silently. A signing key can be revoked the same way. Neither recalls copies already downloaded.
  The reason is text from the publisher, and is shown as text only.
- **Reports.** Each adopted lesson has a link, "Tell the publisher something is wrong", that opens the publisher's issue page with the pack, the
  build and the lesson filled in.
- **Not planned:** open uploads, accounts, payments (EG-1).

### 5. Rights and labels

EG-39 says rights are decided for each operation, and choosing a licence does not give a right the publisher does not hold. For a pack the
operations are:

- **Quoting short passages with a link.** Dojo does this today (`THIRD-PARTY-NOTICES.md`).
- **Repeating those quotes inside a pack.** Each page's source, publisher and licence note are listed in the manifest. Dojo's `license` note on a
  page is its own note and not a grant (`THIRD-PARTY-NOTICES.md`). The skill statements of a study guide stay quoted as they are in the
  repository.
- **Handing on AI-written lessons.** The text is the publisher's, to the extent they hold rights in it. Before the first public pack, the
  owner's own included, the owner establishes the rights to hand on the text, the quotes and the study-guide statements (R1).
- **Handing on generated speech and avatar video.** This is an open rights question (R2). Voices and likenesses need the rights for the format
  and the audience (EG-39), and the terms of the speech and avatar service are the owner's to read. Until R2 is answered, a pack holds words
  only.
- **Real exam items.** Never. Dojo never holds one (`README.md`, `THIRD-PARTY-NOTICES.md`), so the exporter has none to export, and the label
  says so.
- **The right to publish.** Whoever publishes establishes their own right to (EG-39, EG-41). The page that makes the files asks them to say so
  before it writes anything.

The label is in every pack, in every list entry and on every adopted lesson: "Unofficial, not affiliated with or endorsed by Microsoft or GitHub.
Written by AI from official pages as of <date>. Practice questions are made by each Dojo and are not taken from a real exam. Not a promise that
you will pass." It follows the notice already in `THIRD-PARTY-NOTICES.md`.

A complaints address is in every list and every pack. A legal check comes before the first pack from anyone but the owner (R1).

### 6. The owner's pages

The sentences come first (`docs/ui-principles.md`, AGENTS.md rule 13). They are a spec: each is true only when the build behind it exists, and
before any page is built two reviewers from different model families read the words again. The numbers below are made up to show the shape.

The owner's questions, in his words. "I don't want everyone to have to create the same artifacts" becomes *Is there a finished course I can add
instead of paying to make it?* From ADR 0025, whether it was "safe to film or money down the drain" becomes *Is it safe to add, and what does
it cost?* And a third: *How do I give one of mine away?*

Words on the face: **finished course** for the thing, **library** for the place it comes from, **add**, **check**. Never "pack" or "list" on
the face. Not "claims" or "listened to": the check is described by what it does.

Studio's rules decide where things go. **Courses has no buttons** (`docs/user-guide.md`), so nothing here is a button there. **To do** is where
the owner decides what to spend, so every action is a card on To do. The one other place is the **Add an exam** page, because that is where he
is already deciding whether to build.

**To do, "Optional · free": "Finished courses in your library."** One row for each finished course that fits an exam he has, built in or
added. That includes GH-300 and DP-800, which have no Add an exam page.

- Row: "AZ-900 · 62 lessons · made 12 October by Tino · [Add 62 lessons · free]".
- Beside the button, one line: "Making them yourself would cost about $25. Films are not included."
- Sheet: "Add 62 lessons of AZ-900 from Tino's library?" Lines: "You have lessons for 0 of the 62 skills." "Dojo reads 140 quotes on 21 official
  pages first." "3 lessons are left out because their quotes could not be found. 2 are left out because you already have a lesson for that
  skill. Named below." "Adding takes about 2 minutes and costs nothing. The lessons then wait where learners cannot see them, and Dojo does
  nothing with them until you press Check." Buttons: "Add 62 lessons", "Cancel" (focused).
- States. Library not reached: "Could not reach Tino's library just now. Nothing was changed. Try again later." None for any of his exams:
  nothing shown. Two for one exam: two rows, each with its publisher and date. Partial fit: the counts above.
- True when: the pack waits in `packs/` (section 2), the preview counts exist, and the saving is `app/costs.py`'s price or the line is left out.

**To do, "Optional · costs money": "Finished courses waiting for your check."**

- Row: "AZ-900 · 62 lessons added, waiting · [Check 62 lessons · about $6.20]".
- Sheet, at list price: "A second AI model checks what 62 lessons say · about $4.30." "Narration of 62 lessons played back and compared with the words · about
  $1.20." "This is separate from Page check, which looks at the pages, not at what the lessons say." "When you press, passing lessons become your
  lessons: learners see them, Dojo rewrites them when an official page changes, and opening one can make its missing voice or diagram. All of that
  costs money, as it does for your other lessons." Cancel is focused.
- States. A partial pass: "Checked: 59 of 62 lessons passed. 3 did not and stay out. Charged: $4.10." Not enough room for films: "These films need 1.2 GB. You
  have 0.4 GB free. Add the lessons now and the films later, or delete 6 films (the ones watched longest ago)?"
- True when: the check is priced by `app/costs.py`, the sheet's price is the sum of its lines, and adoption works as in section 2.

**To do, "Optional · costs money", films:** shown only after the rights question R2 is answered, and only when the room fits: "Films for 41 of these
lessons · 410 MB of your 6 GB film room · [Add 41 films · free]". "Making them yourself would cost about $50." The number is the film price code's
(`Costs.film_estimate`) for the minutes in the manifest, never a number from the pack.

**Add an exam**, after he types the code and sees the price of building it: "A finished course for AZ-900 is in your library. 62 lessons, made 12
October by Tino." The one gold button is "Add 62 lessons · free". Under a closed "Other way": "Build it yourself · about $25". The sheet is the one
above.

**Before the first start of a new Dojo.** Preparer waits 120 seconds after a start (`Preparer.start`, `app/prepare.py`; started from `app/main.py`) and then
writes the built-in exams on its own, and every lesson it writes costs money. So a new Dojo has a window of about two minutes in which a pack can be
added for free, and after it every skill without a lesson is being paid for. The design closes the window with the rule of section 2: a Dojo
that has not been set up yet counts every skill of a built-in exam as spoken for, so Preparer writes nothing, until the owner has answered one
question: "Add a finished course for GH-300 and DP-800 first? Adding costs nothing. Dojo starts writing lessons by itself once you answer, and
each lesson it writes costs money." Answering "No, write them myself" releases the skills. That hold is new behaviour, built in step 4 of
section 7 with the library reader.

**Courses, the card of a course, a link and not a button:** "62 lessons from a finished course are waiting for your check. Not counted below. Decide on To
do." This keeps the Courses numbers true: waiting lessons are in no count.

**Share a course** is a card on To do, "Optional · free": "Share a course. Make files of AZ-900 that someone else can add." A sheet first: a box "I have
the right to share this", the line "Lessons are shared under CC BY 4.0. Quoted passages stay under their pages' terms.", and the button "Make the
files · free", off until the box is ticked. After: "Made: 62 lessons and their diagrams." A link that shows every file. "Nothing is uploaded. The
files are in the folder you chose." Words only until R2 is answered.

**On an adopted lesson, one line under the title,** after the check: "From Tino's library, made 12 October. Quotes matched the official pages on 14
October. Checked on this Dojo on 21 October." If only part of the check passed, the line names what passed. A lesson that has not passed is not an
adopted lesson, and no line is needed for it.

**To do, "Needs you · costs nothing", when a library withdraws a course:** "Tino's library withdrew AZ-900 on 3 November. Reason given: a lesson was
wrong. Your 62 lessons stay until you decide." Buttons "Remove 62 lessons…" and "Keep them". The sheet: "This removes 58 lessons and their voices
and films. 4 that you have rewritten stay. Practice you have already done stays." Cancel is focused. "Keep them" says that the card then goes away.

### 7. Build order

Each step is useful alone and can be undone. Steps 1 and 2 are code that no page shows.

0. This ADR. The owner reads it. R1 and R2 can wait: R2 has a safe answer in the meantime, words only.
1. **The format, the builder and the validator,** as pure functions with golden tests (`app/packs.py`), and a script that makes a pack from a Dojo's
   data folder and checks a pack file. No routes and no pages. It is useful to the owner alone: a portable copy of finished work, including the
   films that are on the share now.
2. **The importer and the waiting place:** `packs/`, the door, the journal, adoption, the new `skills_spoken_for` rule that stops Preparer and
   `new_lesson` writing a skill whose lesson is waiting, one admin-only route in the route inventory, and the one line on
   a lesson. It is useful to the owner alone too: it puts back films that the 6 GB cap deleted, instead of buying them again at about $1 a minute.
   That works when the lesson text is unchanged, because the film's name is a hash of the lesson's SSML and the presenter's settings. Dojo
   rebuilds that name, finds the pack's file and adopts it. If the lesson was rewritten since, the new words need a new film and the old one cannot
   be used. `app/avatar.py` stays frozen: the importer only calls `media_id_for`. Tests with hostile packs (path tricks, archive bombs, id collisions,
   a far-future date, a foreign `upgrade_of`, forged quotes, the wrong media, a kill between every pair of writes) and with a Dojo that has no money cap.
3. **The owner's pages,** as section 6: two reviews of the words, a build against made-up answers, every state looked at on desktop and on a phone,
   and node tests in the pattern of `tests/test_studio_v2.py`.
4. **The list reader,** the settings for lists and trust, the official library repository and its CI, and the hold on Preparer for a new Dojo until the
   owner has answered the question of section 6.
5. **Other publishers:** signing (the lead decides the library, EG-43), the trust list, revocation, the scans in the list's CI, the complaints and
   take-down routine, and the legal check (R1).
6. **Later:** a choice per skill to use a newer or another publisher's lesson instead of the local one, mirrors, private lists, corrections sent
   back to the publisher, and packs in other languages (the manifest already says `language`).

## What it changes

- **Code:** a new `app/packs.py`; admin routes in `app/main.py` and the route inventory; cards in `app/studio.py` and `app/static`; one line on a
  lesson in `app/learning.py`; tests for each; the user guide, the privacy sheet, the operations page and the module table of
  `docs/architecture.md`. No service needs a skip, because waiting lessons are not in `content/`. `app/avatar.py` stays frozen. No new dependency
  until another publisher needs signing.
- **Data:** `packs/<id>/` (waiting files, the journal, the import record), settings for lists and trusted publishers.
- **Money:** adding costs no model money. A check costs what the price code shows first. After adoption the lessons cost what any lesson costs.
  Hosting costs the owner nothing (static files, release assets).
- **Privacy:** a pack holds no learner data. The exporter writes allow-listed fields only, and the owner reads the list of files before he shares.
  Dojo's server downloads packs and lists, so the host sees the server's address and never a learner's. `docs/privacy.md` says so when this is
  built.
- **Evidence:** a pack never carries evidence. Nothing in it can fill a pip, and a learner's Record is still made only on their own Dojo
  (EG-1, EG-34).

## Risks

| Risk | How the design meets it |
|---|---|
| Hostile text in a lesson | Waiting lessons are shown to no learner. Text is stored and shown as text under the strict content security policy. An allow-listed schema drops unknown fields. Every quote is found again on the official page. A second model checks the claims before adoption, unless the owner chooses to trust the publisher. |
| A true quote used for a false claim | The quote check cannot see it. The priced claims check can. The line on a lesson says which checks were done. |
| A wrong or hostile media file | Shape checks, the name bound to the words, the fingerprint, and the listener. Nothing checks pictures or proves a file harmless, so films come from trusted publishers, or after the listener and the owner's yes. |
| Spending | Adding calls no model. Waiting lessons are in no place that a reader of lessons looks, so the paid paths in fact 6 that start from a lesson cannot reach them, and a skill with a waiting lesson is spoken for, so Preparer does not write it (section 2). Ceilings and the film budget. The sheet says what adoption will start. Outside team mode there is no money cap (fact 6). |
| A lesson that outranks or overwrites another | New local ids and dates, foreign links dropped, gaps decided under the store lock, an existing id refused. |
| A taken-over publisher account | Trust belongs to the source address, not to a name. For the owner's library the root is the code's own repository, which already deploys the Dojo. For others, signing keys and revocation. Withdrawal. |
| Private data in an export | Allow-listed schemas, the list of files shown before saving, and a test that exports a Dojo seeded with private marker strings and fails if one appears. |
| A pack that has gone out of date | The made-on date. The preview counts lessons whose quotes could not be found. After adoption, source drift keeps them current. |
| A half-finished adoption | The journal, the completion mark and the kill test of section 3. |
| Rights | Section 5, R1 and R2. |
| Bandwidth | Static hosting, optional films, mirrors. |
| The server fetching a hostile address | Approved hosts only, public addresses only, limits. |
| Prompt injection | No tools, text as data, and no model call on waiting text before the owner presses Check. |

## Review triggers

- The first pack by anyone but the owner, or the first list other than the official one.
- Any change that lets a service, a route or a prompt read `packs/`, or any change that moves waiting files into `content/` or `media/` before
  adoption, or that makes `skills_spoken_for` and `lesson_skills` the same function.
- R2 answered: films and voices may or may not be handed on.
- A pack grows past what a release asset can hold, or past the film budget of an ordinary Dojo.
- The privacy review or a works council refuses team mode (ADR 0009): this ADR then becomes the plan for colleagues.
- Any change that gives a pack check a tool, a secret or `web_search`.

## Decisions made, and decisions left to the owner

Made by the lead in this ADR, under AGENTS.md rule 3: the licence suggestion for a publisher's own text is CC BY 4.0 with quotes excluded; the
words on the face are "finished course" and "library"; signing waits until another publisher asks, and the lead then picks the library under
EG-43.

Left to the owner, because the guardrails reserve them (EG-39, EG-41):

- **R1:** the right to hand on the text, the quotes and the study-guide statements, and the legal check before the first pack from anyone but the
  owner. Nothing public happens before it.
- **R2:** may generated speech and avatar video be handed on? Until it is answered, a pack holds words only.
- **R3:** when the first other publisher may join.
