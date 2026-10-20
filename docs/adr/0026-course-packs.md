# ADR 0026 - Course packs: finished work can be handed to another Dojo, and waits where nothing reads it until it is checked

- Status: proposed (not built)
- Date: 2026-10-20
- Decision owner: the Dojo lead, for the owner
- Decision: proposed by the lead, at the owner's request to form the idea first: is it feasible, is it usable, and how would it be designed.
  Nothing is built. The owner can reject or change it. Review triggers are below. Three reviewers from three model families have read it against
  the code, in several rounds, and each round changed it; this is the text after the fourth.
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
   Preparer, the question pool and the Deeper lessons search start 120, 300 and 600 seconds after a start, with no question asked
   (`app/main.py`; in Azure, or when the Dojo is told to prepare). Missing lessons and narration have no daily count. And opening a lesson can start a paid job: `POST /api/lessons/{id}/audio`
   narrates a lesson that has no audio, and `POST /api/lessons/{id}/deck` draws a deck on the first open (`app/main.py`). Budgets with caps exist
   in team mode only, and the owner is not capped (`app/budget.py`, `docs/operations.md`).
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
    hyphen (and 128 characters at most), or that contains `..` (`Store.write`, `Store.path`, `_SAFE_PART` in `app/core.py`). It has no way to commit
    several files together.
13. Dojo remembers what it read: the lesson index (`_lesson_meta`), decks (`_decks`) and Deeper Listen (`_deep`), and a missing deck is remembered as
    missing (`Dojo.deck`, `app/learning.py`). A file that appears later is not seen until the memory is refreshed.
14. A first lesson is written by `Dojo.make_lesson`, which saves it without asking again whether the skill still has none (`app/learning.py`,
    `self.store.write("content", "lessons", ...)`). A first lesson that scores under three quarters is still kept as the best of its two attempts,
    when some of it is verified and it has enough slides. Only a replacement (`upgrade_of`) must reach `KEPT_SHARE`. Deeper lessons searches for
    pages and writes lessons again by reading each skill's entry in `content/package-sources/<exam>.json` (`skill_text`, `searched`), a file it
    writes as a whole under its own lock (`DeeperLessons._overlays`, `Packages.set_overlay`), and each lesson's `template` and `offered`
    (`DeeperLessons._search_due`, `DeeperLessons.why`, `app/deeper.py`). Only a built-in exam has that file; an added exam keeps its pages in its
    package file.

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

- **Words:** lessons, Deeper Listen scripts and decks with their diagram specs (JSON). Each lesson keeps its `template` and `offered` fields. For an
  added exam its package file. For a built-in exam the entries of `content/package-sources/<exam>.json` for the skills it covers, with their
  `skill_text` and `searched` fields. Addresses only, never page snapshots.
- **Voices:** slide narration and Deeper Listen (`mp3`).
- **Films:** presenter films (`webm`) and their timing files (`json`). About 8.2 MB a minute.

A group is a download, not a product of its own. The names of narration and films are hashes of the SSML (fact 3), so a pack's audio and film
belong to the pack's own lesson, or to a local lesson whose narration is exactly the same. They are never put on a different lesson.

Everything inside is written from an allow-listed schema for each kind (the fields that teach, the pages the lesson cites and the models that made
it), rebuilt and not copied as a file. The schemas are fixed in the build and covered by tests. Never in a pack: anything under `learners/` or
`team/`, page snapshots from `sources/`, the quality log, delivery results, job results, check-question audio, links, settings, costs, `blocked`
and `rounds`. Practice questions are not in a pack (fact 4): each Dojo writes its own for its own learners, from the official pages of each skill.

The manifest, in outline (the schema is fixed in the build, with golden tests):

```json
{
  "schema": 1,
  "id": "az-900.<blueprint mark>.1",
  "exam": "AZ-900",
  "package": "az-900",
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

A pack's name is the exam, the blueprint mark and a build number, written with letters, digits, dots and hyphens only, so it is also the folder name
under `packs/` (fact 12). A published pack never changes. A correction is a new build. Lessons match by
skill, not by exam: a skill matches when its id and the mark of its text are the same, so when a study guide changes two skills, the other
fifty-eight still match.

### 2. A pack waits in a place that nothing reads

This is the core of the design, and the first version got it wrong. It marked the lessons that came from a pack and asked four services to skip them. Fact 6 shows
that is not enough: opening a lesson starts paid jobs, a podcast feed lists lessons, a practice exam reads them, a media route serves any name.
Every one of those would need the new rule, and one missed reader is a hole.

So a pack that was brought in is kept apart: **`packs/<id>/` holds the pack's files, and `content/` and `media/` do not.** Every reader of lessons and every
route that serves them looks in `content/` and `media/` only. So nothing in Dojo can open, narrate, draw, film, feed, list, plan, serve or
rewrite what is waiting, because none of it is where those readers look. Waiting is not a flag. It is a place.

The place has gaps, and the design closes each one. They were found by reading `app/prepare.py`, `app/learning.py` and `app/deeper.py`.

- **Skills with a waiting lesson look empty.** Preparer decides what to write by asking which skills have no lesson (`lesson_skills`), and the
  Deeper lessons search looks at every skill of a built-in exam, lesson or not (`search_targets`). A skill whose lesson is waiting in `packs/` has no
  lesson, so Preparer would write it and the search would look for pages for it, and both would spend the money the pack was meant to save. The
  same goes for `POST /api/lessons`. So there are two small functions in one place: **`skills_waiting` lists the skills whose finished lesson waits
  in `packs/`, and `skills_spoken_for` lists the skills that have a lesson or one waiting.** Three callers ask them and nothing else does:
  `Preparer.next_skill` and the check in `new_lesson` ask `skills_spoken_for`, and `DeeperLessons.search_targets` leaves out `skills_waiting`.
  `skills_spoken_for` must not replace `lesson_skills`. `lesson_skills` is also what the readiness page, the study plan's moves, Courses' "ready"
  count and the practice exam use to mean "taught" (`app/readiness.py`, `app/exam.py`, `app/newexam.py`, `app/learning.py`), and a waiting lesson is
  not taught. Changing it would make Dojo say that a skill is taught when no learner can open a lesson for it. A lesson that was left out at the
  door, or dropped by the check, is not waiting any more, so its skill is free again and Dojo may write it.
- **A lesson already being written.** Asking at selection time is not enough: Preparer picks a skill and then spends about a minute writing it,
  and `make_lesson` saves without asking again (fact 14). A pack adopted in that minute would be followed by a newer lesson that hides it. So
  writing a lesson for a skill and adopting for a skill share **one claim for each skill**, kept in memory under the Store's lock. The claim is
  taken inside `make_lesson`, the only place a lesson file is written (`app/learning.py`), so its four callers are covered by one change: the
  new-lesson route and the rewrite route (`app/main.py`), and Preparer's step and Preparer's upgrade (`app/prepare.py`). It is taken before the
  writing starts and before the adopting starts, and given back when either ends. Whoever finds it taken skips that skill: adoption names the skill
  in its report as "Dojo was writing this lesson itself", and a `make_lesson` that finds it taken writes nothing and stops with a plain message.
  A writer is never interrupted. A claim dies with the process, and on start an interrupted adoption is finished or rolled back before any request
  or worker, so no claim has to be rebuilt.
- **An exam Dojo does not have yet.** If the package file of an exam that is not in `packages/` went there when the pack was brought in, the
  question pool and the other services would start spending on an exam with no lesson, and learners would see it empty. So it waits in
  `packs/<id>/` too, and becomes `packages/<exam>.json` only at adoption. Until then nothing in Dojo knows the exam, and Studio shows it on the
  pack's card only.
- **An exam that is removed while a pack waits.** Removing an exam cleans the package and content it knows (`app/newexam.py`), and it does not know
  about waiting packs. So removal also cancels the waiting packs of that exam and stops any adoption in progress for it, and adoption checks, under
  the claim and just before it publishes, that the exam and the skill still exist with the same text mark.
- **Deeper lessons would pay again for what the publisher paid for.** Deeper lessons writes a lesson again when its `template` is older than this
  Dojo's, or when its `offered` pages leave out a page the package now lists, and it searches for pages for every skill of a built-in exam that
  has no entry with `skill_text` and `searched` (fact 14). So adoption keeps each lesson's `template` and `offered`. For a built-in exam it also
  merges the pack's entries for the adopted skills, with their `searched` time, into the exam's file of extra pages. It does that through a method
  of Deeper lessons that takes the same lock a search takes (`_overlays`) and writes with `Packages.set_overlay`, which checks every page, so a
  search that ends at the same moment and an adoption cannot overwrite each other. A lesson on an older template is still rewritten later, at the
  price of any rewrite, and the sheet counts them (section 6).
- **Services that would spend before the owner has looked.** Preparer and the Deeper lessons search start by themselves a few minutes after a start
  (fact 6), and a finished course saves money only if it is brought in before they reach its skills. So **a Dojo that first starts with no lesson
  at all holds those two until the owner has answered one question** (section 6). Dojo decides this once, at that start, and writes it down, so the
  hold lasts until he answers, and one answer ends it for both. The hold is one for the whole Dojo, not one for each exam: the card names the exams
  that still have a finished course he has not brought in, and the answer that ends it is the owner's press, with its price shown first. A Dojo that
  already has a lesson at its first start with this change is never asked. The hold covers those two services only. The question pool is not held,
  because a pack carries no questions (fact 4) and holding it would save nothing. A lesson that a person asks for is a press with a price and is not
  held.

It is two small functions, one claim for each skill, one hold and one rule for removal, and a test for each, not a skip in eight modules.

A pack goes through four steps. In team mode only the owner presses them. Members see labels and nothing else. The owner's two buttons are "bring
in" (step 2) and "check and use" (steps 3 and 4 in one press).

1. **Available.** It is in a list. Nothing is downloaded.
2. **Brought in.** Dojo downloads the groups of files the owner chose (the official library has words only until R2 is answered), checks them at the
   door (section 3) and keeps them under `packs/<id>/`. Bringing in calls no model and writes nothing into `content/`, `media/` or `packages/`. The
   owner can read what is waiting, as plain text, on the card.
3. **Checked here.** The owner presses one button with its price. A second model family re-checks each element of the pack's lesson against its
   quote and excerpt, as when Dojo writes a lesson (EG-14), and, when the pack has audio and films (R2), the listener transcribes them and compares
   them with the approved words. Pack adoption has **its own bar, new in this ADR**. It is measured on what the pack published, not on what an
   author wrote: of the points, misconceptions, self-check questions and slides the pack's lesson holds, at least three quarters must pass the
   gate, an element the gate gave no verdict on counts as not passing (as in `gate_elements`), and what remains must still have at least two
   sections and the minimum slides (`MIN_SLIDES`). It uses the same threshold as `KEPT_SHARE` and is not the same check as the deeper-template
   rewrite's, which compares with the lesson it replaces (`make_lesson`, `app/learning.py`). A lesson below the bar is dropped. That is stricter than
   a first lesson that Dojo writes itself, which keeps its best attempt whatever its score (fact 14). When a dropped line changes a lesson's words,
   the audio and film made from the old words are dropped with it, because their names no longer match. **The owner may put adoption off. He cannot
   skip its checks:** every adopted lesson has passed the gate, and every adopted recording has passed the listener.
4. **Adopted.** Lessons that pass are moved into `content/` and their media into `media/`, in the order of section 3. From that moment they are
   Dojo's own lessons: every reader and every worker sees them. Preparer then voices the lessons that have no audio and speaks their Deeper Listen,
   rewrites a lesson when a page changes, and opening a lesson can draw a deck it lacks (`app/prepare.py`). All of that costs money in the way it
   already does. The sheet prices the voicing and says what else starts, before the owner presses.

What the owner and the learners see:

- **Before adoption nothing is visible to a learner,** whoever published the pack. That follows from the place, and it keeps EG-14 and EG-19:
  no learner is taught from a lesson no one checked here, and no teaching event is recorded for one.
- **A skill whose lesson is waiting says so, and no press spends on it.** Where a learner's skill page would offer to write the lesson, it says "A
  lesson for this skill is waiting for the owner to check it.", and the owner's page says "A lesson for this skill is waiting for your check. Decide
  on To do." `POST /api/lessons` answers 409 with the sentence for the person who pressed, the owner included, so any other press that would start a
  lesson for it, such as a next step on Today, gets the same sentence and spends nothing. No lesson is written that makes the finished one
  pointless.
- **There is no shortcut for a trusted publisher.** EG-19 asks for a blocking gate before anything learner-visible. Trust (section 4) decides
  something else: whether films are accepted without a plain yes from the owner, and which publisher's name shows first. It is the owner's
  decision in settings, and never read from a field in the pack.

Rules that go with the steps:

- **Gaps only, with one exception.** A pack lesson is adopted for a skill that has no lesson. For a skill that has a lesson, nothing is replaced.
  The one exception is media: if the skill's newest local lesson has narration that is exactly the pack's (the same SSML, so the same names), the
  pack's missing audio and film are adopted for that lesson, and nothing else about it changes. The pack's own lesson for that skill stays unused,
  and the sheet says so. That is how a lost film comes back (section 7, step 2). Everything else is named in the sheet and left alone.
- **Local ids, local dates.** Adoption gives each lesson a new local id and sets `created` to the adoption time. The publisher's date is kept
  only as the made-on date. A pack's `upgrade_of` and any other link to another lesson are dropped, and the importer builds the links it needs.
  Because the new lesson is the skill's only lesson, it is the one shown, and a later rewrite gets a newer `created`, so it wins as it should.
- **A skill is taken with its claim.** Adoption takes a skill's claim (above) before it moves the lesson, and publishes only when it holds the claim
  and finds the skill still empty. A skill whose claim is taken, or that has a lesson by then, is skipped and named.
- **Updates.** A newer build is a new pack. It is adopted by the same rules, so it only fills gaps: a skill whose lesson came from the earlier
  build is replaced only if the owner presses "Use the newer one" for that skill (a later step, section 7).
- **Removal.** Removing what a pack brought in removes the pack's folder, and the lessons adopted from it that nothing has replaced, and the media
  that no other lesson uses. Removing a pack that still waits removes only its folder: nothing else had changed.
- **Films and room.** Adopting films counts against the 6 GB film budget. When it is full, Dojo asks first, with the number of films it would
  delete (the least recently served, as in playback, `Avatar.evict`) and the room needed, and does nothing without a yes.

### 3. The door

Every pack is treated as hostile, the owner's own included: an account can be taken over, and a mistake is a mistake. Each check is a
pure function with tests (EG-33). The checks run when a pack is brought in, and again, for the files that move, when it is adopted.

| # | Check | Cost | What it stops |
|---|---|---|---|
| 1 | **Container and names.** Ceilings on size, file count and expansion. Entries are read as a stream and nothing is written until everything passes. Every name must pass the Store's rule (fact 12). No links, no absolute or parent paths, no duplicates, only the expected folders and extensions. | free | archive bombs, path tricks, files that do not belong |
| 2 | **Fingerprints.** Every file against the manifest, and the manifest against the list entry it came from. | free | a swapped or damaged file |
| 3 | **Shape.** Each file is rebuilt through an import schema of its kind that keeps only allow-listed fields, with limits on length and count. The existing cleaners (`packages.validate`, `deck.clean_spec`) are used inside it as parts. They are not the boundary: `packages.validate` returns what it was given (fact 11). Text is stored and shown as text only (fact 9). | free | markup, scripts, unknown fields, oversize text |
| 4 | **Ids, dates and links.** New local ids and the adoption time as `created`. Links to other lessons are dropped. An id that exists is refused, never overwritten (section 2). | free | a far-future date that outranks every later lesson; a foreign replacement link; overwriting |
| 5 | **Quotes.** Dojo reads the official pages itself through `Sources.get` (official hosts only, its own limits and backoff) and finds every quote word for word. Snapshots in a pack are never read (fact 7). Dojo records the date of the copy of the page each quote was found in, and a quote counts as found only when the read was fresh: a copy under a week old counts, a copy marked `stale` does not. The line on a lesson (section 6) shows that date. A lesson with a quote that is not found is left out and counted, with the plain words "its quotes could not be found". A missing quote may mean that the documentation has changed, or that the text never existed: Dojo reports only that the quote could not be found. | no model money; takes fetch time | forged or invented quotes; ads and free text that no official page backs |
| 6 | **Media shape.** Expected container structure is checked with bounded work, with limits on duration and size. The name must equal the hash Dojo rebuilds from the lesson's words, voices and settings. A mismatch drops the media. The server decodes nothing; the browser plays. This does not prove a file harmless, and it does not rule out a polyglot file. The fingerprint (check 2) ties the bytes to the manifest, and the listener (check 8) checks what is said. | free | the wrong kind of file, oversize files, media on the wrong lesson |
| 7 | **Claims** (required before adoption, priced). The second model family re-checks each element of the lesson. At least three quarters must pass, a missing verdict counts as a fail, and two sections and the minimum slides must remain (section 2, step 3). Otherwise the lesson is dropped. | priced | a true quote used for a false claim, which the quote check cannot see |
| 8 | **Listener** (required before adoption, priced). The delivered-meaning check on audio and film: transcribe and compare with the approved words. A mismatch drops the media. | priced | audio or film that does not say the approved words. Nothing checks the pictures of a film |
| 9 | **Ceilings.** At most 400 skills, as for an added exam. Limits per file and per pack. Films must fit the film budget. | free | spend and disk exhaustion |

Every adopted recording has passed the listener. Because nothing checks the pictures of a film, films from a publisher the owner has not trusted also
need a plain yes from the owner, on the sheet.

Also at the door:

- **No model sees waiting text.** Imported text reaches a model only when the owner presses "check and use". Those calls are chat calls, which have
  no tools (fact 8), and never `web_search`. The prompts must treat lesson text as quoted data (EG-37). The build audits every prompt that can see
  a lesson, makes that rule the same in all of them, and adds a test that fails when one does not. Only the deck prompt (`DECK_SYSTEM`) says in so
  many words that lesson and source text are data and not instructions. The Deeper Listen prompt and the checker prompts do not, and the build
  adds it to them.
- **An exam Dojo does not have.** Its skills come from Dojo's own reading of the official study guide (`app/studyguide.py`, no model, as Add an exam
  does it), never from the pack. A skill of the pack that is not in that reading is left out and counted. The package file Dojo keeps at adoption
  is built from the reading plus the pack's pages for each skill, and goes through `packages.validate` like any package. If Dojo cannot read the
  study guide just now, the course waits and its card says so.
- **Fetching.** Dojo's server downloads, never the browser. Only https. Only hosts the owner approved when adding the list. Redirects only
  within them. The address must resolve to a public address. No credentials are sent. Size and time limits.
- **Adoption is a journal, not a promise of atomicity.** The Store writes one file at a time (fact 12). So adoption writes a journal first
  (`packs/<id>/adopt.json`: what it will move), writes everything a lesson needs (its audio, films, deck and Deeper Listen) and only then the lesson
  file, then refreshes the memory of what Dojo has read (fact 13: the lesson index, decks and Deeper Listen, including a deck remembered as
  missing), and writes a completion mark last. On start, before any request or worker, Dojo finishes or rolls back an adoption with no completion
  mark. A half-adopted pack never shows a lesson without what it needs. Adopting media alone, for a lesson that exists, is a case of its own and is
  tested on its own. The test kills the process between every pair of writes.

### 4. Lists and publishers

- **A list** is a static JSON file at an https address. An entry has the pack name, exam, language, blueprint mark, publisher, made-on date,
  label, sources and licence notes, and for each group its size, SHA-256 and one or more addresses (mirrors). It has the lesson count, the skills
  it covers (each by id and text mark, so Dojo can say how many match before it downloads anything) and the number of characters its films would
  speak, and a status: active, or withdrawn with a date and a reason. A dollar saving is never in the list.
  Dojo's price code works from characters: `Costs.film_estimate` adds up the characters of the lesson plans it is given and divides by the characters
  spoken a second (`app/costs.py`). So the build adds a small function beside it that prices a number of characters, and Dojo prices the list's
  number at today's list price, or says that the price is not available.
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
- **Withdrawal.** A list entry can be marked withdrawn with a reason. A Dojo that brought the pack in shows a card on To do and keeps the lessons
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
  and the audience (EG-39), and the terms of the speech and avatar service are the owner's to read. Until R2 is answered, a pack that leaves the
  owner's hands holds words only. A pack he keeps for himself hands nothing on.
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
before any page is built two reviewers from different model families read the words again. The numbers below are made up to show the shape, and
they add up.

The owner's questions, in his words. "I don't want everyone to have to create the same artifacts" becomes *Is there a finished course I can bring
in instead of paying to make it?* From ADR 0025, whether it was "safe to film or money down the drain" becomes *Is it safe to use, and what does it
cost?* And a third: *How do I give one of mine away?*

Words on the face: **finished course** for the thing, **library** for the place it comes from, **bring in** for fetching it so that it waits, and
**check and use** for the step that makes the lessons his. Never "pack", "adopt" or "door" on the face, never "list" for the library's file ("list
price" is Dojo's own wording for a price and stays), and not "claims" or "listener": the check is described by what it does. One word for one
thing: a price of nothing is "costs nothing", never "free". A library has the name its list gives it, and the official one is "the Dojo library".
A course says who made it: "by Tino", or "by you" when the maker is the owner of this Dojo.

Studio's rules decide where things go. **Courses has no buttons** (`docs/user-guide.md`), so nothing here is a button there; one line there says what
waits. **To do** is where the owner decides what to spend, so every action is a card on To do. The one other place is **Add an exam**, because that
is where he is already deciding whether to build. ADR 0025 gives To do two groups, *Needs you · costs nothing* and *Optional · costs money*. This ADR
adds a third between them, **Optional · costs nothing**, for what costs nothing and can wait. It is hidden when it has nothing in it. The group is
new, so the user guide's paragraph on To do changes with it.

**To do, "Optional · costs nothing": "Finished courses in the library."** One row for each finished course that fits an exam he has, built in or
added. That includes GH-300 and DP-800, which have no Add an exam page. A course for an exam he does not have is offered on Add an exam, below.

- Row: "AZ-900 · 62 lessons · made 12 October by Tino · [Bring in the finished course · costs nothing]".
- Sheet: "Bring in the finished course of AZ-900 from the Dojo library?" Lines: "You have lessons for 2 of the 62 skills. Those 2 keep your
  lesson." "Dojo downloads the other 60 lessons and finds every quote on the official pages, word for word. That uses no AI model, takes a few
  minutes and costs nothing." "The lessons then wait where learners cannot see them, and Dojo does not use them until you check them. While they
  wait, Dojo does not write lessons for the skills they cover." "Using them later costs money: at most about $14 for 60 lessons at list prices, for
  the check and for voicing them. You see the exact price first." Buttons: "Bring in 60 lessons · costs nothing", "Cancel" (focused).
- While it runs: "Bringing in AZ-900: 23 of 60 lessons read. You can leave this page." If it stops: "Bringing in stopped. Nothing was changed. Try again
  later."
- After it ran, the course moves to the card below: "57 lessons are waiting for your check. 3 were left out: [name · reason]. They are not used, and
  Dojo can write those lessons itself · costs money."
- Voices and films, when the finished course has them (the official library has none until R2 is answered): the sheet gets one more line with an
  unticked box, "Also bring in the voices and films of 41 lessons · 410 MB". Dojo downloads them only when the box is ticked. When he already has every
  lesson, the sheet says "You have lessons for all 62 skills. The finished course has a voice or film for 41 of them that yours lack." and the button
  is "Bring in 41 voices and films · costs nothing".
- States. Library not reached: "Could not reach the Dojo library just now. Last read on 20 October. Nothing was changed. Try again later." Never read:
  "Could not reach the Dojo library just now. Dojo has not read it yet. Nothing was changed." None for any of his exams: the group is not shown. Two for
  one exam: two rows, each with its library and maker. A partial fit: the counts above.
- True when: the finished course waits in `packs/` (section 2), the counts come from the library's entry, the owner's skills and the door's free
  checks, the price line comes from the price code of the next card, and the group "Optional · costs nothing" exists, which ADR 0025 does not have.

**To do, "Optional · costs money": "Finished courses waiting for your check."**

- Row: "AZ-900 · 57 lessons waiting · [Check and use 57 lessons · about $13.30]". Two quiet links under it: "Read what is waiting" (each lesson as plain
  text, for the owner only) and "Remove them".
- Sheet: "Check and use 57 lessons of AZ-900?" Lines, each with its price: "A second AI model checks that what each lesson says matches its quotes ·
  about $4.30." "Afterwards, by itself, as for all your lessons: voicing them and their Deeper Listen · about $9.00." "In all: about $13.30 at list
  prices." Then: "This is separate from Page check, which looks at the pages, not at what the lessons say." "Lessons that pass become your lessons, and
  learners see them. Dojo also rewrites them when an official page changes, which costs money each time and is not in the price." "A lesson that does
  not pass is not used, and checking it costs the same." Cancel is focused. Two more lines appear only when they are true: "This also adds AZ-900 to
  your exams. Dojo then makes practice questions for it in the background, within its daily limit, which costs money and is not in the price." and "9
  of these lessons are written the older way. Dojo rewrites those later, and each rewrite costs money."
- Voices and films, only if he brought them in: the sheet adds "The voices and films of 41 lessons are played back and compared with the words · about
  $1.20", and the voicing line shrinks to the lessons that came without audio, "Afterwards, by itself: voicing the other 16 lessons and their Deeper
  Listen · about $2.50", so the total becomes "In all: about $8.00 at list prices". If the check removes a sentence from a lesson, that lesson's voice
  and film are dropped, and the sheet says so. Films need room: "These films need 410 MB of your 6 GB film room." Not enough room: "These films need
  410 MB. You have 400 MB free. Check and use the lessons now and the films later, or delete 2 films (the ones watched longest ago)?" Where a skill
  already has a lesson whose narration is the same: "1 skill keeps your lesson and adds its missing voice or film." From a library he has not trusted,
  one more unticked box: "Use the films from <maker> as well. Dojo checks what they say, not the pictures."
- States. A partial pass: "Checked: 54 of 57 lessons passed and are now yours. 3 did not pass and are not used. About $4.30 at list prices for the check."
  Remove them: "Remove the 57 waiting lessons of AZ-900? Nothing else changes." and, only for an exam he has, "Dojo can then write these lessons itself ·
  costs money." Cancel is focused.
- True when: the check is priced by a function beside `Costs.deeper_estimate` that counts one check for each lesson (and, for audio, the voice check's
  transcription), the voicing line by the narration and Deeper Listen speech lines of the same price code and worded "by itself" only when this Dojo
  prepares ahead of time (`prepare_on` in `app/main.py`) and "when a lesson is opened" otherwise, the sheet's total is the sum of its lines, the
  number of lessons on an older template is read from the lesson files, "Review all" never includes this button (checking changes what learners
  see, so it is pressed on its own), and adoption works as in section 2.

**Add an exam,** after he types the code and the page shows the price of building it (the number it already shows, from `Costs.course_estimate`):
when a finished course for that exam is in the library, the page says "A finished course for AZ-900 is in the Dojo library. 62 lessons, made 12 October
by Tino." The one gold button is "Bring in the finished course · costs nothing". Under a closed "Other way": "Build it yourself · about $25". The
sheet is the one above, with the extra line "AZ-900 is added to your exams when you check and use the lessons." The skills are the ones Dojo reads from
the official study guide itself (section 3), and a lesson whose skill is not in that reading is left out and counted.

**Before Dojo starts writing lessons on a new Dojo.** Preparer and the Deeper lessons search start by themselves 120 and 600 seconds after a start
and then spend money with no question asked (fact 6). A finished course saves money only when it comes first. So a Dojo that first starts with no
lesson at all holds those two until the owner has answered one question (section 2). The question is one card at the top of To do, above the groups,
and it stays there until he answers. The owner's Today has one line with a link to it, "Dojo is waiting for one answer from you before it writes any
lesson.", and a member's Today says "The owner has not started the lessons yet." The card: "Before Dojo starts writing lessons: is there a finished
course for your exams in the library? Bringing one in costs nothing. Dojo writes no lesson by itself until you answer, and each lesson it writes costs
money." Buttons: "Look in the library first", which opens the group below and leaves the question standing, and "No, let Dojo write them · about $29",
which opens the usual sheet: "Finding pages, writing, checking and voicing 72 lessons of GH-300 and DP-800 · about $29 at list prices." and "Not
included: Deeper Listen and practice questions, which Dojo makes in the background within daily limits, diagrams, which are drawn when a lesson is
opened or ahead of time if you turned that on, and films, which only your press starts." Cancel is focused, and pressing the sheet's button ends the
hold. Once something is brought in or used, the card says "Dojo still writes no lesson by itself. The library also has finished courses for: DP-800."
and the second button reads "Let Dojo write the other lessons · about $…". If the library has nothing for his exams: "Nothing for your exams is in the
library yet." with only the second button. If it cannot be reached: "Could not reach the Dojo library just now. Last read on 20 October. Dojo still
writes no lesson by itself." with "Try again" and the second button. The hold does not cover a lesson that a person asks for: that is a press, and in
team mode it has the member's budget (`app/budget.py`). True when: the hold and its one-time record exist (section 2), the sheet's lines come from
`Costs.course_estimate`, and the Today lines read the record.

**Courses, one line and no button,** among the words under *What each number counts* that list what is missing or waiting: "57 lessons from a finished
course are waiting for your check, so these skills show no lesson yet and Dojo is not writing them. They are not counted above. Decide on To do." The
words *To do* are a link. So the Courses numbers stay true: waiting lessons are in no count.

**Share a course** is not a card. It is a closed section at the end of To do, with a row for each exam that has lessons: "AZ-900 · 62 lessons · [Make
files to share · costs nothing]". The sheet: "Make files of AZ-900 that someone else can bring in?" with a box "I have the right to share this", the line
"Lessons are shared under the licence set in Settings, CC BY 4.0 until you change it. Quoted passages stay under their pages' terms.", and the button
"Make the files · costs nothing", off until the box is ticked. A second button, "Make a copy for myself · costs nothing", needs no box and includes
voices and films, because nothing leaves his hands (R2 does not apply to it). He can keep that copy in a private library of his own and bring it back
from there. After: "Made: 62 lessons and their diagrams. The files are made on this Dojo. Nothing is uploaded. Download them:" with a link for each
group of files and its size. A pack that leaves his hands is words only until R2 is answered. True when: the exporter writes allow-listed fields only,
the list of files is shown before anything is saved, and the licence setting exists (it is new).

**On a lesson from a finished course, one line under the title,** after the check: "From the Dojo library, made by Tino on 12 October. Unofficial, not
affiliated with or endorsed by Microsoft or GitHub. Written by AI from official pages as of 12 October. Dojo matched the quotes to the pages on 14
October and checked the lesson on 21 October." The library and the maker are the publisher's words, shown as text. A lesson that has not passed is not
used, so it needs no line. True when: the door and the check write those dates on the lesson.

**To do, "Needs you · costs nothing", when a library withdraws a course:** "The Dojo library withdrew AZ-900 on 3 November. Reason given: a lesson was
wrong. The lessons from it stay until you decide." Buttons "Remove 50 lessons…" and "Keep them". The sheet: "This removes 50 lessons and their voices
and films. 4 that you have rewritten stay. Learners lose these 50 lessons. Practice already done stays." Cancel is focused. "Keep them": the card
goes away and Dojo does not ask again about this course. If the lessons still wait: "The Dojo library withdrew AZ-900 on 3 November. Reason given: a
lesson was wrong. The 57 lessons you brought in have not been checked yet." Buttons "Remove them" and "Keep them". The reason is the publisher's text
and is shown as text. True when: the card reads the library's status and the lesson files that nothing has replaced.

All of these pages are the owner's. In team mode a member sees only the line on a lesson, the sentence on a skill whose lesson is waiting (section 2),
and, until the owner has answered the first question, the line on Today.

### 7. Build order

Each step is useful alone and can be undone. Steps 1 and 2 are code that no page shows.

0. This ADR. The owner reads it. R1 and R2 can wait: R2 has a safe answer in the meantime, words only.
1. **The format, the builder and the validator,** as pure functions with golden tests (`app/packs.py`), and a script that makes a pack from a Dojo's
   data folder and checks a pack file. No routes and no pages. It is useful to the owner alone: a portable copy of finished work, including the
   films that are on the share now.
2. **The importer and the waiting place:** `packs/`, the door, the journal, adoption, the claim for each skill (taken inside `make_lesson`), the rule for
   an exam that is removed while a pack waits, the cache refresh and the recovery on start, the two small functions `skills_waiting` and
   `skills_spoken_for` that stop Preparer, the Deeper lessons search and `new_lesson` spending on a skill whose lesson is waiting, one admin-only route
   in the route inventory, the sentence on a skill whose lesson is waiting, and the one line on a lesson. It is useful to the owner alone too: it puts
   back films that the 6 GB cap deleted, instead of buying them again at about $1 a minute.
   That works when the lesson text is unchanged, because the film's name is a hash of the lesson's SSML and the presenter's settings. Dojo
   rebuilds that name, finds the pack's file and adopts it. If the lesson was rewritten since, the new words need a new film and the old one cannot
   be used. `app/avatar.py` stays frozen: the importer only calls `media_id_for`. Tests with hostile packs (path tricks, archive bombs, id collisions,
   a far-future date, a foreign `upgrade_of`, forged quotes, the wrong media, a kill between every pair of writes), with a Preparer that writes the
   same skill while the pack is adopted, with an exam removed while its pack waits, with a search that ends while entries are adopted, and with a
   Dojo that has no money cap.
3. **The owner's pages,** as section 6, including the copy he keeps for himself with voices and films: two reviews of the words, a build against
   made-up answers, every state looked at on desktop and on a phone, and node tests in the pattern of `tests/test_studio_v2.py`.
4. **The list reader,** the settings for lists, trust and the licence, the official library repository and its CI, and the first-start question with
   its hold on Preparer and the Deeper lessons search, until the owner has pressed the button that lets Dojo write lessons (section 6).
5. **Other publishers:** signing (the lead decides the library, EG-43), the trust list, revocation, the scans in the list's CI, the complaints and
   take-down routine, and the legal check (R1).
6. **Later:** a choice per skill to use a newer or another publisher's lesson instead of the local one, mirrors, private lists, corrections sent
   back to the publisher, and packs in other languages (the manifest already says `language`).

## What it changes

- **Code:** a new `app/packs.py`; admin routes in `app/main.py` and the route inventory; cards in `app/studio.py` and `app/static`; one line on a
  lesson and the sentence on a skill whose lesson is waiting in `app/learning.py` and the pages; tests for each; the user guide (including its
  paragraph on To do and its new group), the privacy sheet, the operations page and the module table of `docs/architecture.md`. Waiting lessons are
  not in `content/`, so no service needs a skip for them. Three callers ask the two small functions (Preparer, the Deeper lessons search and
  `new_lesson`) and two services ask the first-start hold (Preparer and the Deeper lessons search). Adoption and `make_lesson` share the claim for
  each skill, and adoption takes Deeper lessons' overlay lock for a built-in exam. `app/avatar.py` stays frozen. No new dependency until another
  publisher needs signing.
- **Data:** `packs/<id>/` (waiting files, the journal, the import record), settings for lists, trusted publishers and the licence, and the one-time
  answer to the first-start question.
- **Money:** bringing in costs no model money. A check costs what the price code shows first, and so does the voicing that follows it. After adoption
  the lessons cost what any lesson costs.
  Hosting costs the owner nothing (static files, release assets).
- **Privacy:** a pack holds no learner data. The exporter writes allow-listed fields only, and the owner reads the list of files before he shares.
  Dojo's server downloads packs and lists, so the host sees the server's address and never a learner's. `docs/privacy.md` says so when this is
  built.
- **Evidence:** a pack never carries evidence. Nothing in it can fill a pip, and a learner's Record is still made only on their own Dojo
  (EG-1, EG-34).

## Risks

| Risk | How the design meets it |
|---|---|
| Hostile text in a lesson | Waiting lessons are shown to no learner. Text is stored and shown as text under the strict content security policy. An allow-listed schema drops unknown fields. Every quote is found again on the official page. A second model checks the claims before any lesson is used, whoever published it. |
| A true quote used for a false claim | The quote check cannot see it. The priced claims check can. The line on a lesson says which checks were done. |
| A wrong or hostile media file | Shape checks, the name bound to the words, the fingerprint, and the listener, which every adopted recording passes. Nothing checks pictures or proves a file harmless, so films from a publisher the owner has not trusted also need his plain yes. |
| Spending | Bringing in calls no model. Waiting lessons are in no place that a reader of lessons looks, so the paid paths in fact 6 that start from a lesson cannot reach them. A skill with a waiting lesson is spoken for, so Preparer and `new_lesson` do not write it, and the Deeper lessons search leaves it out (section 2). On a new Dojo, Preparer and that search wait for one press from the owner, with its price shown first (section 6). The sheet prices the check and the voicing that follows it, and says what else starts (rewrites, and practice questions for a new exam). Ceilings and the film budget. Outside team mode there is no money cap (fact 6). |
| Preparer writing a skill while its pack is adopted | One claim for each skill, taken by both. Whoever finds it taken skips the skill, and adoption names it. A kill test and a race test (section 7). |
| A lesson that outranks or overwrites another | New local ids and dates, foreign links dropped, one claim for each skill, an existing id refused. |
| A taken-over publisher account | Trust belongs to the source address, not to a name. For the owner's library the root is the code's own repository, which already deploys the Dojo. For others, signing keys and revocation. Withdrawal. |
| Private data in an export | Allow-listed schemas, the list of files shown before saving, and a test that exports a Dojo seeded with private marker strings and fails if one appears. |
| A pack that has gone out of date | The made-on date. The card counts lessons whose quotes could not be found. After adoption, source drift keeps them current. |
| A half-finished adoption | The journal, the completion mark, the cache refresh and the kill test of section 3. |
| Rights | Section 5, R1 and R2. |
| Bandwidth | Static hosting, optional films, mirrors. |
| The server fetching a hostile address | Approved hosts only, public addresses only, limits. |
| Prompt injection | No tools, text as data, and no model call on waiting text before the owner presses "check and use". |

## Review triggers

- The first pack by anyone but the owner, or the first list other than the official one.
- Any change that lets a service, a prompt or a route for learners read `packs/` (only the importer's owner-only routes may), or any change that
  moves waiting files into `content/` or `media/` before adoption, or that makes `skills_spoken_for` and `lesson_skills` the same function.
- A new service that spends money by itself and does not ask `skills_spoken_for`, `skills_waiting` or the first-start hold.
- R2 answered: films and voices may or may not be handed on.
- A pack grows past what a release asset can hold, or past the film budget of an ordinary Dojo.
- The privacy review or a works council refuses team mode (ADR 0009): this ADR then becomes the plan for colleagues.
- Any change that gives a pack check a tool, a secret or `web_search`.

## Decisions made, and decisions left to the owner

Made by the lead in this ADR, under AGENTS.md rule 3: the licence suggestion for a publisher's own text is CC BY 4.0 with quotes excluded; the
words on the face are "finished course", "library", "bring in" and "check and use", and a price of nothing is "costs nothing"; a new Dojo's
Preparer and Deeper lessons search wait, as one hold for the whole Dojo, for one press from the owner with its price shown first, and the question
pool does not wait; a pack that stays with the owner may carry voices and films, because nothing is handed on; signing waits until another
publisher asks, and the lead then picks the library under EG-43.

Left to the owner, because the guardrails reserve them (EG-39, EG-41):

- **R1:** the right to hand on the text, the quotes and the study-guide statements, and the legal check before the first pack from anyone but the
  owner. Nothing public happens before it.
- **R2:** may generated speech and avatar video be handed on? Until it is answered, a pack that leaves the owner's hands holds words only. A pack he
  keeps for himself, to put his own films back after the 6 GB cap deletes them, hands nothing on, so R2 does not apply to it.
- **R3:** when the first other publisher may join.
