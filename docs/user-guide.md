# Dojo user guide

Dojo is a personal coach for Microsoft and GitHub certification exams. It teaches each skill from the official study guide, asks you new questions, and keeps an honest record of what you have shown.

This guide is also the Help page in the app. Open Help from the header or the side bar. The small "?" next to a page title opens the part about that page.

Dojo runs in one of two ways. In **personal mode** only the owner signs in. In **team mode** the owner also lets in a small group of colleagues, called members. Parts that exist only in team mode say so.

## Getting started

### Signing in

Dojo uses your Microsoft work account. Open the Dojo address and sign in. There is no separate password.

In personal mode only the owner's account gets in. Anyone else sees "This Dojo belongs to someone else."

### Joining in team mode

1. Sign in. If you are not a member yet, you see **Ask for access**.
2. Press **Ask for access**. Dojo stores your request: the name and email address your sign-in gives Dojo, your tenant and account ids, the day you asked, your answer to "used Dojo before", and whether the request is open or declined. The owner sees the name, the email address, the day and that answer. When the owner approves, the request is deleted and your membership starts. A request that is still open, or was declined, is deleted 30 days after you asked.
3. If you used this Dojo before with an account that no longer works, tick the box that says so. The owner sees that, and is asked to check with you directly (a call or a Teams message) before joining your old record to the new account.
4. Wait for the owner. Reload the page later.

### The notice

Once you are approved, Dojo shows **Before you start**. It explains who runs this Dojo and how your data is handled. Read it and press **I have read this**. If you do not agree, press **Leave instead**. When the notice changes, Dojo shows it again. You can read it again at any time from How Dojo works.

### Your display name

In team mode other members may see a display name, never your sign-in name. Set it on Your Record, in **Your name in this Dojo**. It can be up to 40 characters.

### Choosing your exams

In team mode you pick the exams you study for the first time you come in. Change them later with **Change my exams** in the side bar. Leaving an exam out deletes nothing. Your record for it stays and comes back if you pick it again.

The exam picker at the top switches between your exams. Each exam has its own course, practice and plan.

### Membership

A membership lasts 12 months. In the last 30 days a line at the top of every page offers **Keep my membership**. Press it and nothing else is asked.

## Today

Today is your start page. It shows one next step, chosen by exam weight and by what your record does not show yet.

On Today you find:

- **Recalls** that are due, if any. See [Recalls](#recalls).
- **Your next step**: study a lesson, a probe, practice with hints, or a check.
- **The 3-minute check**, a short spoken check. See [Spoken checks](#spoken-checks).
- **What you have shown so far**, as a bar for each domain.
- **Then these**: the next few skills, and **Later checks** that come due.
- Today's planned minutes, **Your week** from Play if you turned Play on, your exam date and the practice exam card.

Set your exam date on Today or on Plan. Dojo uses it to plan and to show how many weeks are left.

## Course and skills

The Course page lists every skill of the exam, word for word from the official study guide. Skills sit in domains. Each domain shows its exam weight and a bar of your evidence.

Pick a skill to see:

- what your record shows for it, and what it does not show yet;
- the lesson in its four forms;
- the official source pages;
- **Practise with hints** and **Check, no help**.

Use the filter chips to see only skills in one state, or the search box to find a skill.

A skill is either an "explaining skill" or "a doing skill". For a doing skill the record also lists the changed condition.

Some marks you may see:

- **seen**: you have opened the lesson.
- **re-checking**: a source page changed, moved or is gone. Dojo checks the lesson against the new page.
- **no verified source**: no official page passed the checks. Dojo does not teach this skill or ask about it, and it counts as not shown.

## Lessons

Each skill has one lesson, written from its official sources and checked by a second model. If a skill has no lesson yet, press **Write the lesson**. It can take a minute or two. You can leave the page while it works.

A lesson has four tabs.

### Read

The written lesson. It has short sections, a **Watch out** part about common mistakes, an example, and **Check yourself** questions at the end. Numbers in square brackets, like [1], open the source quote the sentence relies on.

### Listen and Deeper Listen

**Listen** plays the lesson as a talk between two voices.

**Deeper Listen** is a longer audio about the whole lesson. Once it is ready it plays in Listen. Dojo writes up to 20 of them a day, so one may not be ready at once.

### Watch

Two presenters talk over the slides. Harry is the coach who explains. Meg is a classmate who asks what a learner would ask. Filming costs money, so only the owner decides to film a lesson.

### Present

The slides with narration, one slide at a time.

### Sources and quotes

Dojo only uses pages from Microsoft Learn and GitHub Docs. Every quote from a source is matched word for word on that page. A lesson links back to its pages.

When a source page changes, the skill shows **re-checking** until the lesson is checked again.

Opening a lesson counts as teaching in your record. That matters for "later": see [Your Record](#your-record).

## Questions and answers

### Check yourself

The questions at the end of a lesson are for you alone. Answer in your head, say how sure you were, then see the answer. Mark yourself **Yes**, **Partly** or **Not yet**. These marks are never evidence and never fill a pip. Dojo keeps them and shows them on their own line in **Know what you know**, on Your Record.

### Probe, practice and check

On a skill page there are three kinds of open questions. Each is a new question, written for you.

- **Probe**: no help offered. It shows what you can already do.
- **Practice**: hints are there if you want them. Each hint you open is recorded as help.
- **Check**: no help offered.

Only a probe or a check can count as "on your own". You can type your answer, or speak it where the microphone button is shown.

### How sure were you?

After you answer, and before you see feedback, Dojo asks **How sure were you?** Choose Guessing, Fairly sure, Certain, or Skip. It never changes a mark and never fills a pip. Dojo never asks it in a timed practice exam.

Your Record shows how well your sureness matched your results, in **Know what you know**. A rate shows for a level, such as Certain, once you have 5 answers at that level.

### How judging works

A grader model judges each answer against the points the question expects. For every point it gives you, it must quote your own words. Then a checker model reads the quoted words and the grader's reasons, and decides whether they really show the point. When the checker disagrees with the grader about a point, a third model, the referee, reads your answer and decides that point; the point then says so. You see which points were met, with the quotes.

Feedback is checked too. If feedback does not pass the check, Dojo holds it back and logs why in the Studio log. When the referee changed a point, the grader's feedback is held back as well, because it was written for the grader's verdicts.

How well this works is measured: a bench of 36 answers with known results is judged before any change to the judging rules. The current rules got all 144 points right, three times over (`docs/adr/0016-grader-bench-and-referee.md`). That is a measurement on known answers, not a promise for every answer: use Dispute when a judgement looks wrong.

In team mode, if today's budget is used up, your answer is saved. **Judge my answer now** opens again after midnight UTC.

### Disputes

If you think a judgement is wrong, press **Dispute this judgement** and give a short reason. Then a third model, the referee, reviews it, usually within a minute. It is neither the grader nor the checker. It reads the judgement with your reason and decides every point the grader decided, from your answer. A point the referee itself already decided when your answer was judged stands: it does not review its own decisions.

- **Upheld**: if it finds a point wrong, it corrects the judgement. Points can go up or down. The corrected judgement counts, and the old one stays visible.
- **Not upheld**: if it finds every point right, the judgement counts again, and you can read its reason for each point.
- Until the review is done, the judgement counts neither for nor against you. If the review cannot run, press **Review my dispute** later.
- Each dispute is reviewed once. A judgement that came from a review can be disputed, but the referee does not review its own decision.

A dispute does not undo a missed recall. A disputed miss still brings the skill back sooner. See [Recalls](#recalls).

### Judge again

Dojo's judging rules improve over time. If an answer was judged with older rules, its page shows **Judge again with today's rules**.

- You can do it once per answer, and only when you ask.
- The new judgement counts in place of the old one, whether it is higher or lower. The old one stays visible on the answer and in your record.
- If you disputed the old judgement, your dispute stays on record with it. Judging again does not review your dispute. You can dispute the new judgement on its own.
- Your answer keeps its date and conditions, such as hints and time since teaching.
- It is closed during a timed practice exam. In team mode it uses one graded answer from your daily allowance.

### Why a point can count as not met

A point counts as not met when:

- your answer does not say it. Leaving something out never meets a point: a point such as "Do not use X" is met only when you name the right choice or reject X;
- the grader's quote of your answer was not found in your answer, word for word (a list may be quoted in parts, each word for word);
- the checker finds that the quoted words do not show the point, and the referee agrees; or
- the referee decides the point and cannot quote your answer for it.

So a right idea that you did not write down, or wrote only vaguely, does not count. Write the point in plain words.

### Multiple choice

Multiple-choice answers, in quick questions and practice exams, never fill a pip. After each one Dojo shows the reason, and that counts as teaching.

## Spoken checks

The **3-minute check** asks up to 3 questions out loud. Answer by speaking or by typing; both count the same. There are no hints. The 3 minutes are a guide, not a hard limit.

Speaking needs a microphone and a browser that allows it. If the browser blocks the microphone, see [Troubleshooting](#troubleshooting).

## Recalls

After you first show a skill fully on your own, Dojo asks about it again later. The gaps grow: 1, 3, 7, 16 and 35 days.

- If you miss a recall, the ladder starts again at 1 day. Dojo adds a "Look at … first" link to the part of the lesson to repair.
- A skill is **relearned** after 3 on-time successes on different days. It stays relearned even if you miss it later.
- Dojo asks at most 5 recalls a day. The rest wait for the next days.

Recalls show on Today and in the 5-minute session.

## Practice exams

Open **Practice** in the side bar. There are two ways to practise.

### Timed practice exam

Choose a full or a short exam. The number of questions comes from Microsoft Learn. If Learn gives none, Dojo uses its own default. Choose **Exam format** or **Multiple choice only**.

Dojo writes its own questions. They are not real exam questions. Dojo avoids questions you have seen while it has enough new ones.

### Question types

- **Multiple choice**: one answer.
- **Multiple answers**: for example "Choose two". Each right choice scores one point.
- **Build list**: put steps in order.
- **Drag and drop**: match items to targets.
- **Hot area**: pick the right option for each gap in a text or code.
- **Yes/No series**: decide on several statements about one scenario.
- **Case study**: a longer scenario with its own questions.

Each correct part scores one point. Nothing is taken off for a wrong answer.

### The timer

The clock starts when you press Start. The server keeps the time, so reloading the page does not stop it. Answers sent after the time is up are refused.

### Sections that lock

Like the real exam, some parts lock:

- A Yes/No series and a case study lock once you leave them.
- In a Yes/No series, each statement locks once you answer the next one.

Reasons for every question come at the end, in the review.

### Open book

Some real Microsoft exams let you use Microsoft Learn during the exam. Dojo offers **With Microsoft Learn allowed** only for Microsoft associate and expert exams, such as DP-800. GitHub exams and fundamentals exams are closed book. An exam whose level Dojo does not know is closed book. In an open-book exam the clock keeps running, and your lookups and time away are reported with the result.

### What is closed during an exam

While a timed exam runs, Dojo closes the coach, lessons, hints, quick questions, earlier results, the record export and the Studio log.

### Quick questions

Quick questions are 10, 20 or 30 multiple-choice questions with no timer. The reason shows after each answer. They do not count for readiness.

## Readiness

Readiness answers one question: **Should you book the exam?** The advice is **Book it**, **Almost** or **Not yet**. Dojo shows how confident it is, and the rules it used, from your own record and your own practice exams. Open-book practice exams count too.

Readiness is not a pass prediction. Dojo gives no chance of passing, no predicted score, and no mapping to the real 1 to 1000 scale.

## Your Record

Your Record shows your learning record: what you did and what Dojo saw, in the order it happened.

### Pips

A skill is not a percentage. Dojo records the conditions under which you showed it, as four pips:

1. **with help**: you met it in practice, where hints were offered.
2. **on your own**: you met it in a probe or check with no help.
3. **later**: you met it on your own at least 20 hours after you were last taught it.
4. **in a new situation**: Dojo does not record this yet, so this ring is always empty.

"Not yet" means you tried and did not show it. An empty row means not tested.

Being taught includes opening a lesson, an answer from the coach, a lab, a judged answer, and the reason after a multiple-choice answer. That is why "later" waits after any of these.

### Calibration

**Know what you know** compares how sure you said you were with how you did. For each level (Guessing, Fairly sure, Certain) it needs 5 answers at that level before it shows a rate. Your lesson self-checks have their own line and never mix with your answers.

### Export and delete

- **Download my record** gives you your learning record as one JSON file. It is closed during a timed exam. It holds your events, items, answers and judgements, practice exams, quick practice, the coach's answers, spoken checks, your own Studio log entries, your profile (your exams and dates), your plan, self-checks and recalls. It also holds your choices for Play, what your podcast feed listed, and your nudge settings.
- Some things are held back, so the export cannot be used to read answers early. For a question not judged yet, its rubric, its model answer and the hints you did not open stay out until it is judged. For a practice exam you can still take, the answer key and the reasons stay out until it ends. An exam not started yet leaves out its questions. Questions Dojo wrote ahead for you and has not shown are not in it.
- The export does not hold your membership details (name, email address, role and dates), your use of the daily allowances, study circles and duels, what is in the lab database, or the addresses and keys of your nudge devices.
- **Delete my whole record** asks you to type DELETE. It deletes your learning record: events, items, answers, practice exams, questions, plan, settings and your own Studio log entries. Your podcast feed, calendar link and nudges stop. It also takes you out of every study circle, the effort board and your duels, and removes what you shared. Shared lessons and audio stay. In team mode your membership stays; to end it, use **Leave this Dojo**.
- The record also has an **Integrity** check: each entry is chained to the one before, so a change would show.

In team mode Your Record also shows **Today's allowance**, your display name, and **Leave this Dojo**.

## Plan and the calendar link

Plan places study sessions inside the free times you set. By default that is 60 minutes Monday to Friday, 120 minutes on Saturday, nothing on Sunday, and at most 420 minutes a week.

The rules Plan follows:

- It plans 14 days ahead, with at most 6 sessions a day.
- Sessions in the next 24 hours stay fixed.
- In the last 4 weeks before the exam it plans one practice exam a week.
- It plans no recalls on or after exam day.

You can move, skip or pin any session.

### The calendar link

Press **Get my calendar link** and add it in Outlook with **Subscribe from web**. Each session then shows in your calendar with the exam, the activity, the skill, the length and a link back to Dojo.

- The link is shown only once. Dojo keeps only a fingerprint of it. Keep it private.
- **Make a new link** stops the old one. **Turn the feed off** stops it altogether.
- Your calendar app fetches the feed every few hours, so changes show with a delay.
- Dojo never reads your calendar.

## Podcast

Dojo can turn your lessons into a private podcast feed, one episode per skill. It is off until you turn it on, on the On your phone page.

- Press **Get my podcast link**. Add the link to a podcast app, or scan the QR code.
- **Make a new link** stops the old one. **Turn the feed off** stops it.
- Copies that a podcast app already downloaded cannot be deleted by Dojo.

Dojo cannot see what you play in a podcast app. So before an answer that could count as "later", Dojo may ask whether you listened to that episode. If you say yes, the listen counts as teaching.

## On your phone

### Install Dojo

You can add Dojo to your phone or computer like an app. In Edge or Chrome, open the browser menu and choose **Install Dojo**. Dojo stores nothing on the device for offline use; it needs a connection.

### iPhone and iPad

On iPhone and iPad you need iOS 16.4 or later. Open Dojo in Safari and choose **Add to Home Screen** first. Nudges only work from the Home Screen icon.

### Nudges

A nudge is a short notification when recalls or a planned session are due.

- At most one a day, and only when something is due.
- It comes within 2 hours after the time you choose. The default is 08:00, Monday to Friday, Berlin time.
- Never during a timed exam.
- The lock screen only shows counts, never a skill or a question.
- Up to 5 devices. **Send a test nudge** checks one device. **Turn off everywhere** stops all of them.

### The 5-minute session

The 5-minute session is made for a phone. It asks up to 2 due recalls, then repairs one answer you were sure of but got wrong: a short part of the lesson, then a new question. Then it stops.

## Labs (DP-800)

For DP-800 there are three hands-on labs in a private Azure SQL database:

- Make a vector product table
- Find the nearest product
- Filter semantic product search

Open a lab from the Course page, or from a skill with **Open the hands-on lab**.

- You run one SQL batch at a time, up to 8 KB. A batch stops after 20 seconds. Dojo shows up to 200 rows.
- The database sleeps when it is not used. Waking it takes about a minute.
- **Check my work** checks the database itself, not a screenshot.
- **Reset this lab** empties your part of the database for that lab, so you can start again.
- Hints count as help.

No lab fills a pip. Lab work counts as teaching.

Labs need a lab database. Not every Dojo has one. If yours does not, the pages say "Labs are not set up on this Dojo.", show no lab links, and your plan has no lab sessions.

## Ask the coach

Type a question in **Ask the coach anything** at the top, or speak it. The coach answers from the skill's official sources. Each point of its answer quotes the source, and the quote must be found word for word. A second model then checks each point against the sources. Points that fail are removed and listed in your Studio log. If the sources do not cover part of your question, the coach says so. Even so, the answer can still be wrong: see [Limitations](#limitations).

An answer from the coach counts as teaching for that skill, but only when at least one point passed the checks.

## Play

Play is optional. It is off until you turn it on, and you can leave at any time. It marks effort, never results.

### Study points and weeks met

You choose a target of 3 to 6 study days a week. **Weeks met** counts the weeks you reached it. It never goes down, and there is no streak to lose.

Study points:

- a study day: 10
- an open answer: 3
- a multiple-choice answer: 1
- a lesson view: 2 (up to 5 a day)
- a checked lab: 10 (once per lab a day, up to 3 a week)

Answers earn at most 30 points a day, open and multiple-choice together. That is, for example, 10 open answers or 30 multiple-choice answers. An answer sent less than 5 seconds after its question earns nothing.

At most 80 points a day and 400 a week. A right and a wrong answer earn the same.

### The shelf

The shelf keeps moments from your record: a skill shown on your own later, a skill relearned, a domain or an exam where every skill is relearned, and passes you report. A reported pass holds only the exam and the day. Dojo does not check it. You can hide a moment.

**Leave Play** deletes Play's settings at once.

### Study circles, effort board and duels

These exist only in team mode, only when the owner turns them on, and only if you join.

- **Study circles** have 3 to 12 people and are by invitation. A circle sees its effort as a bar in 25% steps once at least 5 of its members count: a member counts from the Monday after they join, and after someone leaves, the bar pauses until the next Monday. Members of a circle see each other's display names, the goal, the bar, and every moment or pass that someone chooses to share with that circle. Sharing works once a circle has 3 members, and a shared moment stays for 28 days. They do not see your points, study days, answers or results, unless you share a moment.
- **The effort board** needs at least 10 people. It shows four bands: Getting going (0 to 99 points a week), Building (100 to 199), Steady (200 to 299) and Strong week (300 to 400). You see names only in your own band, and only when at least 3 others share it. The others in your band see your display name in the same way, so they know your band. **Just my points** hides their names from you; it does not hide yours from them.
- **Duels** are by invitation: the same 10 questions for both, 48 hours to answer, no clock. If both finish within the 48 hours, both players see how many each got right. Otherwise each sees only their own. Nobody else sees it. There is no winner and no record. A duel is deleted 7 days after the invitation.
- **Others can invite me** is off unless you tick it. With it on, people who joined circles see your display name in the list of people they can invite.
- Others see your display name, never your sign-in name or email address.

**The owner in Play.** The person who runs this Dojo may use Play as a learner. They cannot start a circle, invite anyone, or join the board, and they cannot see the board. If a member invites them to a circle or a duel, they take part like anyone else. In a circle they are shown as "runs this Dojo" and see what every member of that circle sees. In a duel they see both results, as the other player does.

### What is never shown

Play shows no rankings. Outside a duel it never shows anyone's results; a duel shows only its two players how many each got right. No admin page shows anyone's Play data to the owner: not the Members page, not Studio, not diagnostics. The admin role sees only whether circles are switched on. What the owner sees as an invited learner is described above.

## Privacy

This part is a summary. In team mode the notice you read at the start is the full text.

### Who sees what in the app

- Your record, answers and plan are yours. No other member sees them, and neither does the owner's admin role.
- In Play, other people see what [Play](#study-circles-effort-board-and-duels) describes: your display name, your band on the effort board, what you choose to share with a circle, and a duel's result. Only if you join. This applies to the owner too when a member invites them to a circle or a duel.
- In team mode the owner's Members page shows only what admission needs: your sign-in name and email address, your display name, your role, your status and the day you joined. It shows no activity, results or costs of any member. Costs are shown to the owner only as totals.
- Other members never see your sign-in name or email address.
- Apart from the app's own files, everything in Dojo needs sign-in. There are two exceptions. Your private calendar and podcast links work for anyone who has the link. A public health check shows anyone only whether Dojo's self-test passed and which build runs; it shows nothing about any person.

### What the operator can technically access

The person who runs this Dojo is the global administrator of its Microsoft Entra tenant and the owner of its Azure subscription. With that access they can technically read everything Dojo stores, including your answers and record, and you cannot see when they do. Their written commitment: they do not read members' stored data, and if a support case ever needs it, they ask you first. Dojo data is never used for performance reviews, staffing or HR decisions, and nothing is shared with managers. A commitment is not a technical control.

### Where your data is stored and processed

- Your data is stored on the Dojo server, in the Azure region and subscription of the person who runs this Dojo. The notice and the "How Dojo works" page name the region when Azure reports it. The lab database, if this Dojo has one, is in the same region.
- The AI models run in Azure AI Foundry with global deployments. What you type or say may be processed outside the EU and EEA.
- Anything you type or say goes to the models. Dojo adds no name or email address to what it sends.

### How long data is kept

- Your data stays until you delete it or leave.
- Automatic server backups can keep a copy for up to 30 days.
- Lab database backups are kept for 7 days. Server logs are kept for 7 days.
- An access request that is still open, or was declined, is deleted 30 days after you asked.
- After you leave or your membership ends, a few things stay for a while, without your name or email address. A fingerprint (hash) of your key and a bare line with your membership's status and dates stay for 40 days, so that restoring a backup cannot bring your data back. The owner's log of admin actions (approvals, pauses, removals) then calls you "a former member", and keeps its entries for up to a year.

### Leaving

In team mode you can leave at any time from Your Record: type LEAVE. Dojo deletes your whole folder at once: record, answers, plan, exams and settings. It takes you out of Play, and removes your name, email address and display name from the member list. Your lab work in the lab database is dropped the next time Dojo connects to that database. Shared lessons stay. What stays for a while is listed under [How long data is kept](#how-long-data-is-kept).

- If the owner pauses the Dojo, you can still download, delete and leave.
- If your access ends, you have 30 days to download or delete your record. Then Dojo deletes it.

## Costs and daily allowances

This applies in team mode. AI calls cost money, so each member has daily allowances. They start again at midnight UTC. Your Record shows what you have left today.

Default allowances per day:

- questions to the coach: 20
- graded answers: 25
- hints: 40
- new practice items: 25
- spoken checks: 4
- rehearsals: 3
- practice exams started: 2
- transcriptions: 60
- first lessons for a skill without one: 3
- lesson audio: 5
- lab runs: 150
- practice-exam question batches: 6

The owner can change these numbers.

All members together also share a daily cap, by default 5 US dollars at list price. When it is used up you see "Dojo has used today's shared budget. It starts again at midnight UTC." The owner's own use is counted but never limited.

## Limitations

- **AI can be wrong.** A second model checks what the first one writes, a third decides where they disagree about a judgement, and quotes are matched word for word. Mistakes can still get through. Use Dispute when a judgement looks wrong.
- **Only Microsoft Learn and GitHub Docs.** Dojo uses only pages from these two sites. A skill with no verified page is not taught.
- **Not real exam questions.** Dojo writes its own questions in the exam's formats. They are practice, not the real exam.
- **No pass prediction.** Readiness is advice from your own record, not a chance of passing.
- **English only.** Lessons, questions and answers are in English.
- **Small teams.** One Dojo runs on one small server for up to 30 members.
- **Browsers.** Use a current Microsoft Edge, Chrome, Firefox or Safari. Speaking needs a browser that can record sound. Nudges need web push; on iPhone and iPad that means iOS 16.4 or later and Dojo on the Home Screen.

## Troubleshooting

### "Pick an account" keeps coming back

After a long time away, the sign-in page may ask you to pick an account. Choose the account you use for Dojo.

If it keeps asking:

1. Close every Dojo tab.
2. Open Dojo in an InPrivate or private window and sign in with only that account.
3. If that works, sign out of the other accounts in your normal window.

### The microphone does not work

- In Edge, click the padlock left of the address bar and set **Microphone** to **Allow**. Then reload the page.
- Check that a microphone is connected and that no other app is using it.
- You can always type your answer instead. It counts the same.

### Nudges do not arrive

- Press **Send a test nudge** on the On your phone page.
- If notifications are blocked, allow them for this site in the browser's settings.
- On iPhone and iPad, open Dojo from its Home Screen icon and turn nudges on there.
- Nudges come only when something is due, within 2 hours after your chosen time, and never during a timed exam.

### A page keeps waiting

Writing a lesson or a set of questions runs in the background and can take a minute or two. You see a **Working** card with the current step. You can leave the page; the result will be there later.

If Dojo says it could not be reached, your sign-in may have expired after a long time away. Reload the page.

### "Your sign-in has ended"

A sign-in lasts 8 hours. After that Dojo says so at the bottom of the page. Press **Reload** and sign in again. Dojo then opens the page you were on. If it opens the start page instead, open the page again from the menu.

## For the owner

Everything in this part is for the owner only. Members do not see these pages or buttons, apart from their own Studio log.

### Studio

Studio shows how each course is built: the steps of the pipeline, a voice check, costs (estimates from list prices, never per member) and source changes. For members the same menu entry is called **Studio log**.

Everyone, the owner included, has a **Studio log**. It lists what the checker removed from shared lessons, and from what Dojo wrote for you alone. Nobody sees another person's own entries, not even the owner.

From Studio and the lesson pages the owner can also:

- film a lesson, after seeing its price (**Film this lesson**, **Film again**);
- rewrite a lesson.

### Add an exam

Open **Add an exam** in the side bar and type an exam code. Dojo reads the official study guide and shows the price before you press **Build this course**. A model finds the source pages, and a second model's quotes are checked word for word. Videos are made only after you approve their price.

### Members page

The Members page is for team mode, and only then does the side bar show it. In personal mode its address shows a short note about team mode instead, unless members still have data from an earlier team mode: then the whole page stays. In team mode the owner can:

- approve or decline requests for access;
- join an old record to a member's new account;
- pause, resume or remove a member;
- end team mode, with 14 days' notice to members;
- turn study circles on or off;
- set the daily allowances and the members' daily cap;
- read the steps to invite a guest in the Microsoft Entra admin center.

### Team mode switch

Team mode is not a button in the app. It is a setting of the deployment (`teamMode` in the Bicep template, set from the repository variable `DOJO_TEAM_MODE`; `DOJO_TEAM` on the server). Turning it on or off is a new deployment. Switch it on only after a privacy review; the operator guide (`docs/operations.md`, "Switching team mode on safely") lists the steps. To stop, use **Pause** or **End team mode** on the Members page first, and switch the setting off only after the end date.

**Study circles** are part of Play in team mode: small groups of 3 to 12 members, by invitation, with a shared goal and the moments members choose to share. They stay off until the owner turns them on on the Members page, and members join them only if they want to. See [Study circles, effort board and duels](#study-circles-effort-board-and-duels).

## Glossary

- **Domain**: a group of skills in the study guide, with its share of the exam.
- **Gate**: the checker model. It checks every model-written text you see against the official sources, and removes what does not hold.
- **Grader**: the model that judges your answers. It must quote your own words for every point it gives.
- **Referee**: the third model that decides a point when the checker disagrees with the grader about it.
- **Pip**: one of the four small circles next to a skill. Each stands for a condition: with help, on your own, later, in a new situation.
- **Relearned**: a skill you got right in 3 on-time recalls on different days.
- **Skill**: one line of the official study guide, such as a task you should be able to do or explain.
