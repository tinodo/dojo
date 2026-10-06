# ADR 0007 - Deeper Listen: a gated two-voice narration of the whole lesson

- Status: accepted
- Date: 2026-09-30
- Decision owner: the Dojo lead, for the owner (the only user)
- Decision: accepted by the builder of Deeper Listen under the owner's standing instruction to decide and
  build autonomously. The lead can reject or change it in review. Review triggers are below.
- Guardrails: EG-14 (a generated answer is not a verified Claim), EG-19 (what the audio says is checked
  against the approved words), EG-16 (exposure is not a Finding), EG-20 (what consumption does not
  prove), EG-12 (record consequential decisions), EG-43 (dependencies)

## The requirement

The owner once said Dojo felt "like Microsoft Learn". Measured on 29 September across the 114 live
lessons, Listen and the podcast narrated only the slides: a median of 180 words, about 1.2 minutes, and
32 words at least. Read had a median of 917 words in its sections, plus a worked example, misconceptions,
a self-check and a summary. On the commute the owner can only listen. So Listen, and the podcast on his
phone, should teach the whole lesson.

## The options

1. **Re-film everything with longer scripts.** Rejected: about $570 at list price, and filming needs the
   owner's click on its price.
2. **Lengthen the slides and their narration.** Rejected: Watch and Present play the slides, and the
   presenter videos are filmed from the slide narration. A longer script changes Watch, its media ids
   (`app/avatar.py` is frozen) and its filming cost.
3. **A second narration, for listening only, written from the checked lesson and gated line by line
   like the slide narration** (chosen).

## The decision

**What it is.** A lesson's Deeper Listen is stored next to it, in `content/deep/<lesson>.json`, with its
own media ids. The lesson, its slides and its `narration` never change. It is a conversation in the two
voices of the slide narration: `guide` (Coach · Harry) explains, and `coach` (Classmate · Meg) asks what a
learner would ask and ties it to the exam. Its parts follow the lesson: an opening, one part per section,
the worked example, the misconceptions and why they are wrong, one or two exam traps, and a summary. The
target is 800 to 1,000 words, about 6 minutes. It ends by sending the learner to the self-check in Dojo
and never gives its answers.

**Where its facts come from.** The author model gets the checked lesson (every point with its quote, the
worked example and the misconceptions) and the same source excerpts that grounded it. It is told to add
no fact of its own. It never sees the self-check.

**Gating (EG-14).** Every line goes to the gate model through `gate_elements`, the call that checks a
lesson and its slide narration. Before that, Dojo itself refuses a line that gives the self-check away:

- a line that asks a self-check question;
- a line that says a self-check answer of six words or more, word for word;
- a line that says a shorter answer, as a whole phrase, next to its question: this line, or the line
  before it in the same part, restates the question. A line that says the answer in the words of its
  question counts too.

How a line asks or restates a question depends on its length. A question of four words or more is
compared with the near-duplicate test of the questions (`too_similar`, on word triples). For restating,
that test runs on the words as they are and on the content words alone, so "the exam may ask which
feature you should use" restates "Which feature should you use?". A shorter question is too short for
that test, so it counts only when the line says it whole: all its words, side by side and in their order,
as whole words, whatever the case and punctuation. "So, who can enroll?" asks "Who can enroll?"; "Who can
unenroll?" does not. Its glue words count too: without them "Who can enroll?" would be "enroll", and every
line about enrolling would match. Asked, a question of any length is refused with or without its answer.

Teaching the facts behind an answer is the lesson itself, so such a line is kept, and so is a short answer
said without its question. A part with a line that fails is written once more, with the reasons, and left
out if it fails again. The failed lines of the parts left out are in the quality log as "deep narration"
and in the document's `blocked` list. Deeper Listen plays only when at least 75% of it holds, by lines and
by parts (the lesson's own `KEPT_SHARE`), and at least 450 words are left. Otherwise it is kept as failed,
with the reason, and Listen keeps the slide narration. When the last part kept does not send the learner
to the self-check, Dojo adds its own fixed line: "That is the whole lesson. Now try the self-check in
Dojo." It states no fact, so it is not gated.

**Speech.** The two-voice path of the slide narration (`app/speech.py`), one MP3 per part. As for the
slides, a media id is a hash of the lines and the voices. Watch and Present keep the slide narration and
their videos. The golden test of `media_id_for` pins that.

**Where it plays.**
- Listen plays it once it is ready and says which narration plays and why.
- The podcast episode plays it. Its episode key comes from its media, so it is a new joined file. The
  slides' joined file is deleted after the grace time. The guid and the URL become `<lesson>-deep`, so a
  podcast app downloads it again. `aired.json` notes lessons by id, so the lesson is noted once.
- The voice check (EG-19) compares each part with its approved words, at most 10 lessons a day.

**Exposure (EG-16, EG-20).** A listen is recorded as before, as `lesson.viewed` with its modality, and
now says which narration played. Its window is 45 minutes when the Deeper Listen could have played, and
30 otherwise. The time since teaching counts from the end of that window, so the longer window is never
in the learner's favour. A view is never evidence.

**Background work.** The Preparer writes Deeper Listen only when no lesson and no narration is missing,
and no rewrite after a source change (ADR 0006) is waiting within that day's allowance of rewrites.
It goes active exam first, then the other exams, most exam weight first. It writes at most 20 a day, in
UTC days, counted before the writing starts and kept on the share, so a restart gets no fresh allowance.
It waits while a learner job runs, also between the parts it speaks. Speaking text that is already
checked is not counted. An attempt is kept on the share before its first model call, with its number,
when it started and the rounds it started. One that failed is tried again after a week, at most twice:
one that did not pass its checks, one that stopped on an error, and one that a crash or a restart cut
off (it is still marked as being written, and the Dojo that reads it is not writing it). Studio and
`/api/diag` count the failed ones and, of those, the ones that stopped.

**Rewrites.** While a changed source page holds a lesson back (ADR 0006), its Deeper Listen does not
play. The lesson's rewrite invalidates it, and the new lesson gets its own. The old document stays, with
what it cost. Since ADR 0008, a lesson that will be written again on the deeper template gets none until
it is: the new lesson gets it, so it is paid for once. Writing a lesson again that already plays one
invalidates that one the same way.

**Cost.** At the recorded Sweden Central list prices, about $0.49 a lesson and $55 for all 114:

| What | For 114 lessons |
| --- | --- |
| Writing (GPT-5.5, 10,000 input and 6,000 output tokens a round, 1.5 rounds) | $39.33 |
| Checking every line (Grok 4.1 Fast, 23,000 input and 7,000 output tokens a round) | $1.39 |
| Speech (about 5,800 characters a lesson) | $9.92 |
| The voice check (about 7 minutes a lesson) | $4.72 |

The token counts are assumptions, because Dojo does not count model tokens yet. Studio shows how many
lessons play it, what it has cost so far and what all lessons will cost, and `/api/diag` has a
`deeper_listen` line. So far counts every round from the moment it starts, also the rounds of attempts
that did not pass, stopped on an error or were replaced. The document keeps the rounds, not dollars:
Studio and `/api/diag` price them at the list prices Dojo reads, as every other figure. Storage: about
4.4 MB of part audio a lesson, and a joined episode of the same size that replaces the slides one, so
about 1 GB for 114 lessons.

## Review, round 1

Two independent reviews of the pull request (#30). One found nothing. The other found two faults, and
both are fixed:

1. **A short self-check answer could be spoken.** Answers under six words were never compared, so "Use
   GitHub Copilot Chat." could follow the coach putting the self-check question, and the gate model
   passes it, because the sources support it. Now a short answer is caught next to its question, as
   described under Gating. A short answer said without its question is still a fact the lesson teaches.
2. **A failed attempt was not kept before the model work.** If the author or the gate call raised before
   the document was saved, nothing recorded the attempt. After a restart the lesson looked new: it
   skipped the week's wait and the limit of two attempts, and the rounds it paid for were missing from
   "so far". Now the attempt is kept before the first model call, and an error makes it a failed
   attempt, as described under Background work.

The check of these fixes found one more fault, now fixed: a self-check question of fewer than four words
was never compared. So "Who can enroll?" could be asked, and "Only administrators." could answer it in the
next line. Now a short question counts when a line says it whole, both when the line asks it and as the
question next to a short answer, as described under Gating.

## Consequences

- Listen and the podcast teach the whole lesson in about 6 minutes, one lesson at a time, over about six
  days for 114 lessons.
- A lesson whose Deeper Listen failed keeps its slide narration. Listen says why.
- A line that says a short self-check answer in the words of its question is left out, even when it
  reads as teaching. Read and the self-check still have that fact.
- A short self-check question counts only when it is said whole. A rewording of it ("Who is able to
  enroll?") is not caught, and neither is a short answer right after the rewording.
- Every round started counts in "so far" at once, before its tokens are spent: a round cut short is
  counted in full.
- Phones download every episode again once, when its Deeper Listen is ready.
- Listen offers no choice of the slide narration once Deeper Listen is ready. Present has it.

## Review triggers

- The model bill for Deeper Listen is far from the estimate: count the tokens and correct the assumptions.
- More than one lesson in four fails its checks: look at the quality log before changing the thresholds.
- Many parts are left out because a short answer stood next to its question: look at those lines in the
  quality log before narrowing the test to the line before.
- Self-check questions of fewer than four words become common: look at matching them by their content
  words, at least in the question just before a short answer.
- The owner wants the short narration in Listen as well.
- Filming the longer script becomes cheap, or the owner asks for it.
