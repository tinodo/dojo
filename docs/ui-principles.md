# How a page for the owner is made

The owner's pages (Studio, the exam builder, anything he presses) are judged by one test: can he tell, in five seconds, what to do and
whether it is safe? These rules came from a page he could not use (ADR 0025). Follow them before you build, and look at the result.

1. **One question a page.** Its title and its first sentence say what it is for ("What needs you, and what is optional."). If you cannot
   say the question, the page is not ready to be built.
2. **Task before data.** What the owner must decide comes first and costs nothing to read. History comes after, and closed.
3. **One main action a card, with its price on it.** The label says what happens and what it costs ("Film 68 lessons · about $130.56"). Money
   is asked for in a sheet that names the lines, the price and what was left out, with Cancel focused. Never `window.confirm`.
4. **A number says what it counts and over which time, or it is not shown.** No bare numbers. "4 of 73 lessons were rewritten in the last 7
   days", not "4". A cap is said as a cap ("the last 400 are kept"), never as a fact.
5. **Say what is true.** No forecast the data cannot support ("more are due"), no "nothing happens until you press" when something does.
   A check that could not be read is not "fine".
6. **Plain words on the face.** No `held`, `template`, `overlay`, `token`, `WER`, `drift`. They may live behind a closed details with the
   plain sentence above them. One word for one thing ("rewritten", always).
7. **No permanent warnings.** A caution is said where the button is pressed, or when the condition holds, never under a button for good.
8. **A long list is capped**, with what needs a click first. A page that grows with exams has a row for each exam, not a card.
9. **Related things share a page or link to each other.** Never leave the owner to guess how two tabs relate.
10. **Look at every state, with the owner's amount of data.** Empty, normal, long, error, loading, not connected, phone width (390 px), keyboard.
    Build the page against made-up answers that look like the live ones and read the screenshots before you ask for a review. The pages are
    tested in node (`tests/test_studio_v2.py`): the tests read the words off the tree, count the buttons and fail on the words of Dojo's
    insides.

Before you build: write the sentences first, then ask two reviewers from different model families to read the spec (not the code), then
build, then show screenshots.
