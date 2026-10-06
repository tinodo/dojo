# ADR 0001 - The microphone is allowed, so answers can be spoken

- Status: accepted
- Date: 2026-09-28
- Decision owner: the Dojo lead (the owner is the only user)
- Guardrails: EG-12 (record consequential decisions), EG-17 (no hidden help),
  EG-22 (notice and consent before a new kind of data leaves the page), EG-30 (accessibility
  is not reduced capability)

## The requirement

Dojo asks for answers in the learner's own words. Watch ends with "Your turn - now say it in your
own words", and then the learner could only type. Saying the answer out loud is the natural way to
rehearse for an oral explanation, and it is faster. The design asks for it: DESIGN.md section 1
("answer - out loud or in writing"), mockups 03 and 11.

To record audio in a page, the browser needs permission. Dojo's `Permissions-Policy` header turned
every device off, including the microphone. Allowing one of them is a security decision, so it is
written down here.

## The options

1. **Keep the microphone off.** Nothing changes, nothing new leaves the browser, and half of the
   product idea stays undone. Rejected.
2. **Recognise speech in the browser** (`webkitSpeechRecognition`). In Edge this still sends the
   audio to a cloud service, one Dojo does not control, under a policy Dojo cannot state. It also
   needs no server code, which hides the boundary rather than removing it. Rejected.
3. **Record in the page, transcribe on Dojo's own Azure AI Speech resource** (chosen). The audio
   goes to the same AI Services resource Dojo already uses for narration and filming, in the same
   tenant and region, with the same managed identity and no key. The boundary does not move.

## The decision

- `Permissions-Policy: microphone=(self)` in `app/main.py`. Camera, geolocation, payment and USB
  stay off, and the Content-Security-Policy is unchanged: the page never plays the recording back,
  so `media-src` stays `'self'` and no `blob:` source is needed.
- The recording is sent once to `POST /api/transcribe`, which calls Azure AI Speech fast
  transcription with the app's managed identity (scope `https://cognitiveservices.azure.com/.default`)
  and returns the words. It answers inside the request, so it never waits behind filming in the
  three shared job slots.
- No phrase list. Learn documents `phraseList` for this API only from api-version 2025-10-15, and a
  list built from the item or its rubric would put words in the learner's mouth (EG-17).
- Before the first recording of a browser session the page shows a short notice: what happens to the
  audio, a link to Microsoft's data-privacy page for speech to text, and "You can always type
  instead." There is no "always on" setting (EG-22).

## What is kept, and what is not

Kept, exactly as for a typed answer:

- the answer the learner decided to send, in `answer.submitted` and in the item file;
- two conditions on that event: `input` ("spoken", "spoken_unconfirmed" or "typed") and
  `edited_before_send`;
- while the learner is answering, and for at most 30 minutes, a handful of receipt ids in memory
  (see below): an id, whose learner it belongs to, which answer it was made for, and when it runs out.

Not kept, anywhere:

- the audio - it lives in the page and in one request body, and is never written to the data dir;
- the raw transcript - it goes back to the browser and is never stored server-side;
- neither of them ever reaches the log, not even in an error. Failures are reported by HTTP status
  in plain words, never by quoting the service's response body;
- nothing about what was said is attached to a receipt: no text, no hash of the text, no length.

## "Spoken" is observed, not claimed

The browser says how an answer was given, and a browser can say anything. So `POST /api/transcribe`
takes the question the recording is for (`?item=<item id>`, or `ask` for the coach box) and hands back
a one-time `receipt`: a random id the server remembers in memory with the learner it belongs to, the
answer it was made for, and an expiry of 30 minutes. The answer sends its receipts along; `input` is
recorded as `spoken` only when at least one of them is still open, for this learner and for this very
item, and it is used up in the process, so one recording can never confirm two answers. A receipt made
for another question, or for the coach box, confirms nothing and is left alone. A claim Dojo cannot
confirm - no receipt, an old one, one already used, one made elsewhere, or one forgotten because the
app restarted - is recorded as `spoken_unconfirmed` and shown in plain words ("Sent as spoken, but
Dojo has no record of writing down a recording for this question"). It is never an error, and it never
changes what counts. The grader is only told about speaking when the receipt confirmed it.

The record says no more than the receipt proves. What Dojo observed is that it wrote down a recording
for this question before the answer arrived, so that is exactly what the condition says: "Dojo wrote
down a recording for this question before the answer was sent". It does not claim that the words sent
are the words spoken - nobody but the learner sees the audio, and the learner may correct the text
before sending, which is the point.

Speaking is neither help nor a penalty. The four pips, "unaided" and the standing state are computed
exactly as before; the Record only lists the spoken condition as one more condition (EG-30). The
grader is told the answer came in from a recording that a machine wrote down, so it ignores
spoken-form differences ("co-pilot", "two thousand twenty-five") but not mistakes of content - and it
must still quote the learner's own submitted words.

## The microphone stops when the learner cannot see it

A recorder the learner can no longer see or cancel must not keep listening. The page lets go of the
microphone - track stopped, timer cleared, recording thrown away, any upload aborted - as soon as the
view that holds the Speak button is replaced, when the page is left or closed, and as soon as the
answer or question is sent. Going to another page stops the header "Ask the coach" recorder too. The
running timer checks every quarter second that its button is still on the page. A permission prompt
that is answered after the view is gone stops its tracks at once and does nothing else, and a second
Speak click while a notice or a recording is already open does nothing, so there is never a second
microphone that Stop cannot reach.

Each recording owns its stream, recorder, chunks and clock. Handlers close over their own recording,
so a `stop` event that arrives after the learner has cancelled and started again releases the
microphone of the recording it belongs to - never the one that is running now.

## What counts as spoken in the browser

The page remembers each piece of text a recording put into the box. The answer is sent as spoken only
while some of those words are still in it: wiping the box and typing something else is a typed answer,
however it started, and fixing a word or two is still a spoken one. It is marked as corrected when the
box already held typed text before a recording was added, or when what was written down was changed
afterwards - and once corrected, it stays corrected.

This is a courtesy, not a control. It runs in the browser, so a determined client could send anything;
it only keeps the honest case honest. The receipt is the part the server checks.

## Consequences

- The site now asks the browser for microphone permission the first time the learner presses Speak.
  Edge remembers the choice per site; the learner can withdraw it in the padlock menu.
- One more service call is on the request path. It is capped: 10 MB, about three minutes, and the
  page stops recording at 3:00.
- The self-test gains a speaking check: Dojo says a fixed sentence, writes it down again through the
  same function and checks the words came back. It measured about half a second and returned the same
  words on every run, so it sits in the background self-test, which means `/healthz` reports it and the
  deploy smoke gate reads it. Speech synthesis was already a gate check, so this adds no new service.
  The price is that a transcription failure - a missing role assignment, a Speech outage - makes the
  app report unhealthy and turns the deploy step red. That is the intended signal: with speaking in
  the product, Dojo is not healthy if it cannot hear.

## Review trigger

Revisit this decision if any of these happen:

- Dojo gets a second user, or any account that is not the owner's;
- the transcription call has to move off the owner's own AI Services resource, or to a key;
- a phrase list, word boost or custom model is considered (weigh it against EG-17 first);
- the page ever needs to play a recording back, which would mean changing the CSP.

## How to undo it

1. Set `Permissions-Policy` back to `microphone=()` in `app/main.py`.
2. Delete the `/api/transcribe` route, the `Receipts` class in `app/learning.py` and the `receipts`
   argument of `Dojo.submit`, and `Speech.transcribe`, `Speech._transcribe` and
   `Speech.probe_transcription` in `app/speech.py`, and the `stt` check in `app/selftest.py`.
3. Remove `speakBox`, `dropLostMicrophones` and their call sites in `app/static/app.js` and the
   `.speak`/`.notice` rules in `app/static/app.css`.
4. Leave `input` and `edited_before_send` where they are. They are part of records that were already
   written, and the Record is append-only: old answers keep saying how they were given.
