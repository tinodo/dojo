# ADR 0014 - Present: a real slide deck with checked visuals, synced to the narration

- Status: accepted
- Date: 2026-10-05; accepted 2026-10-06
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: accepted by the builder of the Present deck under the owner's standing instruction to decide
  and build autonomously, with the changes listed under "As built". The lead can reject or change it in
  review. Review triggers are below.
- Guardrails: EG-14 (a generated answer is not a verified Claim), EG-19 (delivery is gated, not exempt),
  EG-20 (each modality carries a learning intention), EG-16 and EG-21 (exposure and telemetry are not
  evidence), EG-30 (accessibility), EG-35 (prompts are engineered assets), EG-37 (retrieved content is
  untrusted instruction), EG-39 (rights), EG-12 (record consequential decisions), EG-43 (dependencies), and
  the Interaction-to-Evidence contract (a lesson format writes exposure only).

## The requirement

The owner, on 5 October: "You present a 'Presentation' but the 'Presentation' is just four lines of static
text. I was expecting a real presentation." It should look good, be different from Watch (the filmed
presenters) and from Read (the written lesson), and have "clear structure, engaging content, confident
delivery, and audience-focused visuals".

Present showed each lesson slide on one template: a kicker, a title, two to four bullets and a source
line, with the slide narration playing beside it. No title, agenda, sections, visuals, recap or check;
nothing on the slide moved with the narration; no captions on the stage, no overview, and full screen did
not keep the slide's shape.

What a lesson already holds, all of it checked: 6-9 slides with 2-4 bullets and 2-5 spoken lines each in two
voices (with per-slide audio once made), 4-6 sections of points with exact quotes, 2-4 misconceptions, a
worked example with steps, and 4-5 self-check questions. Present used only the first.

Official trainer decks were studied for their patterns (not their content, which is licensed courseware
and stays out of Dojo): outcomes at the start mirrored by a summary at the end, numbered sections with
dividers, one idea per slide, diagrams for layers and pipelines, side-by-side comparisons, a practice
handoff with one call to action, and a knowledge check before the recap. They are text-dense because a
trainer speaks over them; Dojo has its own narration, so its slides can be sparse.

## The options

Content
1. **Ask the author model for a whole new deck with new narration.** Rejected: new narration means new
   speech for every lesson, a second spoken version beside Listen and Watch, and more to check; the slide
   narration is already written, checked and voiced.
2. **Build the deck from the checked lesson alone** (chosen, level 1): no model call, no new claim.
3. **Add visuals with one author call per lesson, gated like lesson content** (chosen, level 2).

Visuals
1. **Images from an image model.** See "AI images" below. Rejected for v1.
2. **Slides as HTML text only.** Rejected: that is what Present already was.
3. **Deterministic SVG drawn in the browser from a small structured spec** (chosen): every word in a
   visual is a short string in the spec, and what the visual means (which step comes before which, what a
   table row says) is written out as plain statements the gate checks. Nothing in a visual can be wrong in
   a way the gate did not see.

Presenter
1. **An AI avatar presenting the deck.** Out of scope until the owner decides D-1 (`app/avatar.py` is
   frozen). Watch already is the filmed presenter. The stage keeps a corner the layout can give to a
   presenter later, and the player is driven by narration lines with start times, which is what a
   presenter video would sync to.

## The decision

### What Present is for (EG-20)

Present shows the **shape** of a skill: how its parts fit together (a flow, layers, a comparison), the
number or rule that matters, the trap to avoid, and a recap that answers the promise made at the start.
Read is for reading at your own pace and quoting, Watch is the filmed presenters, Listen is audio only.
Present is a narrated deck with diagrams. Its capability target is the lesson's skill; the confusion it
works on is the lesson's misconceptions; the next challenge is a check without help. Watching it proves
nothing about what the learner can do, and Dojo records it only as exposure.

### Three levels, so nothing breaks

0. **Legacy slides.** A lesson with no slides left after the checks shows the plain note, as before.
1. **The derived deck** (every lesson, free, instant). Built on the server from the checked lesson:
   - a title scene (lesson title and summary, exam and skill, length, who wrote and checked it);
   - "In this lesson": the parts and their slide titles;
   - part dividers with Dojo's own words ("The ideas", "In practice", "Watch out", "Wrap-up");
   - one scene per lesson slide, its bullets appearing as the narration reaches them;
   - the worked example as a step-by-step process scene;
   - each misconception as a "Common trap" scene (the tempting belief, then the correction and its quote);
   - a recap of the slide titles;
   - the self-check ("think, then reveal") and a closing scene with the next step.
2. **The visual deck** (one author call per lesson, then the gate). The author model gets the checked
   lesson and the same source excerpts and returns a deck spec: 2-4 outcomes ("you will be able to"),
   2-4 named sections over the slides, at most one visual per lesson slide, an optional exam tip or trap
   per slide (with an exact quote), and 3-5 recap lines. Dojo composes the visual deck from the spec and
   the lesson; whatever the spec lacks comes from the derived deck.

Present always opens at once with the best deck there is, and swaps to the visual deck when it is ready.

### Visual types

| Type | For | Spec (sizes are capped) | What the gate checks |
|---|---|---|---|
| flow | steps in order | 3-6 steps: label, note | the steps in that order, as one statement |
| cycle | a loop | 3-6 steps | the loop in that order, one statement |
| stack | layers or levels | 2-5 layers, top first | which layer sits where, one statement |
| timeline | dates or phases | 3-6 events: when, label | the events and their order, one statement |
| hub | one thing and its parts | a centre, 3-6 spokes | each spoke on its own |
| compare | a table | 2-3 columns, 2-5 rows; cells are text, yes or no | each row on its own |
| contrast | two sides | two titled columns of 1-4 points | each point on its own |
| matrix | a 2x2 decision | two axes, four cells | each cell; one failed cell drops the matrix |
| keynum | one number that matters | value, unit, label, exact quote | the quote is found word for word and contains the value; the label |
| code | a snippet | up to 14 lines, verbatim from a source excerpt; up to 4 notes on lines | every line is found in the excerpt; each note |

Rules: an ordered visual (flow, cycle, stack, timeline) is dropped whole when its statement fails; a hub,
table or contrast loses the part that failed and is dropped when too little is left (fewer than 3 spokes,
2 rows, or an empty side); a slide whose visual is dropped shows its checked bullets. Outcomes, section
titles and recap lines are checked one by one; a failed section title becomes "Part N", and too few
outcomes or recap lines fall back to the derived deck's. Text in a spec must not give away a self-check
answer (`_gives_away`), as in Deeper Listen. With less than KEPT_SHARE (75%) of the spec holding, the
visual deck is kept as failed and the derived deck stays. There is no second round: one author call and
one gate pass per attempt.

Each visual is drawn as SVG with Dojo's tokens and icon sprite, in a wide layout for the 16:9 stage and a
tall layout for phones, so text stays readable. Every SVG has `role="img"`, a label, and a text
alternative (an ordered list or a table) for screen readers.

### Delivery

- **The narration spine** is the lesson's checked slide narration and its per-slide audio: no new speech.
  Each slide scene plays its slide's audio. Line start times are estimated from the line lengths and the
  real audio length; without audio (not made yet, or the local stub) a clock runs the same cues at about
  14 characters a second.
- **Builds and highlights**: each part (a bullet, a step, a row) has a cue line. It appears when that line
  starts and is highlighted while the line plays. Cues come from the spec when valid, otherwise from the
  words a part shares with the lines, otherwise they are spread evenly.
- Scenes without narration (title, agenda, dividers, traps, example, recap) hold for a reading time; the
  self-check and the closing scene wait for the learner.
- **Controls**: play and pause, previous and next, "Continue automatically", captions on the stage (who
  speaks and the line), 1x or 1.25x, an overview of every scene, full screen that keeps 16:9, and a
  progress bar with the sections marked. Keys: left and right arrows, Page Up and Page Down, space, Home
  and End, F (full screen), O (overview), C (captions), Esc (close the overview or leave full screen).
- **Motion**: scenes slide and fade, parts build, connectors draw. With `prefers-reduced-motion` nothing
  moves (parts still appear), and "Continue automatically" starts off, as in Watch.
- **Accessibility**: a live region announces each scene ("Slide 4 of 15: ..."); every control has a name;
  the overview is a list of buttons; captions are real text.
- **Phone** (ADR 0013): below 640 px the stage is a tall card, visuals use their tall layout, controls
  wrap, and the right-hand panel moves under the stage.

### The self-check and the Record

The self-check scene is teaching material: the learner thinks, then reveals the checked answer and its
quote. It writes nothing: no Record event and no self-report. A real check is one click away ("Answer a
question, no help"), as at the end of Watch. `markViewed(L.id, "present")` is unchanged: it is sent when
playback starts, never on open, and the server keeps one per 30 minutes.

### Storage, jobs and money

- `content/decks/<lesson>.json` holds the spec as checked, with `state` (writing, ready, failed,
  invalidated), attempts, rounds started and what was blocked. It is saved before the model call, so a
  crash reads as a failed attempt, as for Deeper Listen.
- **Drawn ahead of time only when the owner turns it on** (Studio, "Present: visual decks", with the price for
  every lesson shown first; off until then, set 15 October): the Preparer then draws them like Deeper Listen,
  at most `DECK_PER_DAY` (20) a day, the active exam first, a failed deck tried again after 7 days and at most
  twice, after the lesson's own upgrade (ADR 0008), and not while a source change holds the lesson back
  (ADR 0006). It was first built to draw on its own; that spent money nobody had asked for, so the switch
  is the owner's (`content/deck-state.json`, `ahead`; `POST /api/studio/decks/ahead`).
- **On first open**, on or off: when the deck is missing, Present starts the same job (`POST
  /api/lessons/{id}/deck`) and keeps playing the derived deck meanwhile. It costs what one deck costs, once,
  for a lesson the learner chose to open; Present's note says so ("up to about 30 cents"). The route's checks,
  the reservation and the start of the job are one step under one lock, so two opens at once, of one learner
  or two, reserve and start once. In team mode the route reserves the worst case first
  (`budget.action(learner, "deck", package)`), with a daily allowance for members; the owner and the
  background are not held.
- A new lesson for the skill (rewrite, Deeper lessons, drift) invalidates the deck with the lesson, like
  Deeper Listen. While a source change holds the lesson back (ADR 0006), the visual deck is not shown:
  Present shows the derived deck under the lesson's own drift note, because the derived deck is the
  lesson itself.
- The author prompt treats the lesson and the page excerpts as data, never as instructions (EG-37), and is
  kept with the other prompts in `app/learning.py` (EG-35).
- **Cost, list prices** (the same assumptions style as `costs.py`). The estimate before building: the
  author reads about 12,000 tokens (the lesson, three page excerpts, the prompt) and writes about 8,000
  (the spec and its reasoning), the gate reads about 20,000 and writes about 4,000: about $0.31 a lesson,
  $0.37 with retries. Measured on 6 October with three real lessons in a scratch Dojo: the author read
  5,100-8,000 tokens and wrote 3,400-5,700 (its reasoning included), the gate wrote up to 7,800 (its
  reasoning included), and a deck cost $0.13-0.21, 97% of it the author. `DECK_CALLS` in `costs.py` is set a
  little above that: the author 10,000 in and 6,000 out, the gate 10,000 in and 8,000 out. At gpt-5.5
  ($5 / $30 per million) and grok-4-1-fast-reasoning ($0.20 / $0.50 per million) that is about $0.24 an
  attempt, $0.28 a lesson with retries, and about $32 for the 114 lessons (worst case about $54 if every
  lesson needed two attempts). There is no speech cost. Studio's cost view shows "so far" and "for every
  lesson" lines, as for Deeper Listen, and `/api/diag` the short line. The checker's second question (when it
  skipped elements, ADR 0009) adds at most four gate calls of about 10,000 tokens, about $0.03, and only then:
  the 1.2 attempts a lesson in the estimate cover it.

### AI images: no, for now

Checked read-only on 5 October: Dojo's own AI resource can deploy gpt-image-1, -1-mini, -1.5,
-2 and -2.5, FLUX 1.1 pro, FLUX.1 Kontext pro, FLUX.2 pro and flex, and MAI-Image 2.5 and 2.6 (preview),
as Global Standard. List prices: gpt-image-1 $40 and gpt-image-2 $30 per million output image tokens
(a medium 1024x1024 image is about 1,000 tokens, so $0.03-0.04; high quality $0.12-0.17), FLUX 1.1 pro
$0.04 an image, FLUX.2 pro $0.03 for the first megapixel. Three hero images a lesson would cost about
$12-57 for all lessons. Not chosen, because:

- the gate cannot read a picture: an image can carry words, a made-up product screen or a wrong technical
  detail that nobody checked, which breaks "nothing unchecked reaches the learner";
- trademarks and look-alike product screens are a real risk in a public app and repository (EG-39);
- it needs a new model deployment in `infra/main.bicep`, quota, content-filter settings, storage and its
  own budget line, for pictures with no learning value;
- each image takes seconds to a minute, and on a phone it costs data.

If the owner wants hero art later: decorative only, never text or a technical claim, made in the
background with its own cap, behind a review, and the deck stays complete without it.

## As built

Built on 5 and 6 October as decided above, with these changes.

The player
- **Readable text comes first.** A diagram that does not fit its box is laid out again with smaller text
  (up to six steps down) or scaled down and laid out that much wider, never below 75% on the 16:9 stage and
  80% on a phone; of the ways it fits, the one whose smallest text comes out largest is drawn. A label
  takes a size only when every word fits whole, so a word is never broken inside. Even at the most a spec
  may hold, the smallest text stays at least 12 canvas pixels on the stage and 11 on a phone; on the stage
  every diagram fits, and on a phone a long one scrolls in its box.
- **The stage takes the full width.** The narration text and the sources sit under it, side by side, and
  stack on a phone; there is no right-hand panel to move.
- **The visual deck comes in at the next scene.** When it is ready while a scene plays, is paused part
  way or the self-check is open, the player finishes that scene first; otherwise it swaps at once. Either
  way it goes to the same slide, trap or part in the new deck.
- **Exposure only when a scene plays.** `markViewed(L.id, "present")` is sent when a scene starts to play,
  not when Play moves from the self-check to the closing scene, which does not play (review of #63).
- The player listens to keys, the window size and the phone width only while it is on the page, and lets
  go of them, its frame loops and its sound when the learner leaves (review of #63).

The visual deck
- **Why there is no visual deck, said.** The document keeps the four states above. What Present receives
  also says why a lesson has none now: "missing" (one may be drawn), "writing" (being drawn, for this
  learner or another), "held" (a changed source page holds the lesson back), "upgrading" (the lesson will
  be written again on the deeper template, and the new lesson gets the deck) or "older" (a newer lesson of
  the skill gets it). A failed attempt cut off by an error, a crash or a restart is marked "stopped".
- **The first-open route** also checks, like the other lesson routes, that the learner takes the exam and
  that no exam is running. When nothing starts, because the deck is being drawn already or is not due, it
  answers `{"started": false, "state": ...}`. The job registry can tell whether a job for the lesson
  runs for anyone (`Jobs.running`), so two learners' first opens start one drawing. A first-open job whose
  lesson got a deck meanwhile stops before the model call.
- **The Preparer** draws a deck ahead of time only when the owner has turned that on, and only when it has no
  lesson, upgrade, narration or Deeper Listen to write.
- **Invalidation is exactly Deeper Listen's**: the deck goes with its lesson when a changed source page
  (ADR 0006) or the deeper template (ADR 0008) replaces the lesson. After a plain rewrite the older lesson
  keeps its deck, and the new lesson gets its own. A deck that is being drawn when this happens is withdrawn
  (or deleted with its lesson) at once: an attempt starts only if its deck was not withdrawn or deleted since
  it was admitted (a counter for each lesson, `Dojo._deck_gen`, also when the lesson had no deck document
  yet), and its late result is not saved (`Dojo._deck_finish`); what the attempt cost stays in the document.
- **"Too few" in numbers**: outcomes are kept when at least 2 hold, recap lines when at least 3 hold. A deck
  is "ready" only when what is left still composes to the visual level; otherwise it is a failed attempt
  ("Nothing was left to draw once the checks were done"), tried again after a week like any other.
- **A badly shaped answer is left out, not an error**: a list in the author's answer is read to its first 12
  entries, and an entry of the wrong type is dropped with its reason (a code snippet whose `lang` is not a
  string once stopped a paid attempt; it now reads as plain text).
- **What a diagram shows that belongs to no one element** (a table's column names, a matrix's axes, the whole
  of a code snippet) is checked for a self-check answer too, and such a diagram goes whole
  (`deck.shared_texts`, `deck.without_visuals`).
- **Tips without a label.** In the real runs 8 of 14 tips began with "Tip:" or "Trap:" under the slide's
  own label. Such a label is now removed, and the prompt asks for none.
- The quality log names the format "visual deck", as in "visual deck · slide 7 diagram point".

Measured with the real models on three lessons (6 October): kept shares 0.95, 0.88 and 1.0 at the first
attempt, six or seven diagrams a deck, and nothing dropped that the sources supported: the gate refused
two contrast points the source does not name, a flow whose steps the author had split with its own
qualifiers, a table row and a recap line, and `_gives_away` blocked a tip that quoted a self-check answer.

## Consequences

- Present becomes the "structured" format: Read is for reading at your own pace, Watch is the filmed
  presenters, Listen is audio only, Present is a narrated deck with diagrams.
- Every lesson gets the derived deck the moment this ships, at no cost.
- The visual deck adds one author and one gate call per lesson: when the learner first opens it, or ahead
  of time within a daily cap if the owner turns that on. Visible in Studio.
- app.js grows by one section (the deck player and the SVG visuals); no library is added.

## Review triggers

- More than one visual deck in five fails its checks, or the owner sees a visual that is wrong.
- The cost per lesson measured in Studio is more than twice the estimate.
- The owner decides D-1 (an avatar could present the deck), or asks for hero images.
- Word timings become available for the slide audio (exact cues instead of estimates).

## How to undo it

Turn drawing ahead of time off in Studio, and remove the first-open job: Present then shows the derived deck,
which costs nothing. Deleting `content/decks/` removes every visual deck; nothing else refers to it.
