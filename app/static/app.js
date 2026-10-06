"use strict";
/* Dojo front end. No framework, no build step. Every piece of text from the server or a model is
   put on the page with textContent (through h()), never parsed as HTML. The one exception is Help: the
   server renders the user guide with every text escaped, and helpNodes() rebuilds it from an allow-list.
   The Content-Security-Policy forbids inline styles, so layout lives in app.css and the few
   measured lengths (bars, timelines) are set through the CSSOM with el.style.setProperty. */

const state = { me: null, gate: null, pid: null, nav: 0, pkg: null, pkgPid: null, askDraft: "", pickedSkill: null, laterHours: 20, feedLink: null, calLink: null, timer: null, keys: null, deckOff: null, help: null, gateHelp: false };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const $view = () => document.getElementById("view");
const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const put = (el, ...kids) => {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  dropLostMicrophones();  // a recorder whose view has just been replaced must stop listening
};
const safeUrl = (u) => (typeof u === "string" && /^https:\/\/(docs\.github\.com|learn\.microsoft\.com)\//.test(u) ? u : "#");
const ext = (href, text) => h("a", { class: "link", href: safeUrl(href), target: "_blank", rel: "noopener noreferrer" }, text);

const SVGNS = "http://www.w3.org/2000/svg";
function icon(name, cls) {
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", cls ? "i " + cls : "i");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVGNS, "use");
  use.setAttribute("href", `/static/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}
// A smaller icon shape used inside the skill rows, where it may be dimmed.
function ficon(name, dim) {
  const svg = icon(name);
  svg.setAttribute("class", dim ? "fic dim" : "fic");
  return svg;
}
const wide = (el, w) => { el.style.setProperty("width", w); return el; };

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function fmtWhen(s) {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d) ? String(s) : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
// Exam dates are plain YYYY-MM-DD: read them as local days so the day never shifts.
function parseDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
const fmtDay = (s) => { const d = parseDay(s); return d ? d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : ""; };
function weeksTo(s) {
  const d = parseDay(s);
  if (!d) return null;
  const today = new Date();
  return Math.round((d - new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 604800000);
}

const chip = (text, kind) => h("span", { class: kind ? `chip ${kind}` : "chip" }, text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const button = (label, onclick, kind) => h("button", { class: kind ? `btn ${kind}` : "btn", type: "button", onclick }, label);
const linkButton = (label, href, kind) => h("a", { class: kind ? `btn ${kind}` : "btn", href }, label);
const panel = (...kids) => h("section", { class: "panel" }, ...kids);
const eyebrow = (name, text, kind) => h("p", { class: kind ? `eyebrow ${kind}` : "eyebrow" }, name ? icon(name, "sm") : null, text);
// A small "?" beside a page title that opens the matching part of Help. Anchors are literal strings, so
// tests/test_help.py can check that each one exists in docs/user-guide.md.
const helpQ = (anchor) => h("a", { class: "helpq", href: `#/help/${anchor}`, title: "Help on this page", "aria-label": "Help on this page" }, "?");
const pageHead = (title, sub, brow, help) => h("div", { class: "pageh" }, brow || null,
  help ? h("div", { class: "pageh-t" }, h("h1", null, title), help) : h("h1", null, title), sub ? h("p", { class: "sub" }, sub) : null);
const errorPanel = (e) => panel(h("h2", null, "That did not work"), h("p", { class: "sub" }, (e && e.message) || String(e)),
  h("div", { class: "actions top" }, linkButton("Back to Today", "#/today", "ghost")));

const ACTION_LABEL = { lesson: "Study the lesson", probe: "Probe, no help", practice: "Practise with hints", check: "Check, no help" };
const ACTION_ICON = { lesson: "read", probe: "target", practice: "hand", check: "check" };
const MODE_NAME = { probe: "Probe", practice: "Practice", check: "Check" };
const MODE_TEXT = {
  probe: "Probe: a new item with no help offered. It shows what you can already do.",
  practice: "Practice: hints are available. Each hint you open is recorded as help before you answer.",
  check: "Check: a new item with no help offered.",
};
const FORMATS = [
  { tab: "watch", name: "video", label: "Watch", note: "two presenters on the slides" },
  { tab: "read", name: "read", label: "Read", note: "the written lesson" },
  { tab: "listen", name: "listen", label: "Listen", note: "two voices talk it through" },
  { tab: "present", name: "deck", label: "Present", note: "a narrated slide deck" },
];
// How a lesson view reads in the record. A view is recorded when it starts, so the words never claim
// more than a start. Watch views made before "watch" had its own name were recorded as "present".
// Dojo cannot see a podcast app, so a podcast listen is only ever what the learner said before answering.
const SEEN_AS = {
  read: "Opened the written lesson",
  listen: "Started the lesson audio",
  present: "Started the narrated slide deck",
  watch: "Started the filmed lesson",
  podcast: "You said you listened in a podcast app",
};
const SEEN_DEEP = "Started the Deeper Listen audio";
const NOT_A_PREDICTION = "Dojo records conditions, not scores. None of this is a pass prediction.";
// The two presenters. The same person reads a role everywhere: the narration voices come from this
// same table on the server (app/avatar.py PRESENTERS). The keys are the filmed roles and never
// change; the role words here are labels only — Harry is the coach who explains, Meg the classmate
// who asks what a learner would ask. In a sentence "classmate" is a plain noun: "classmate Meg".
const PRESENTER = { guide: { name: "Harry", role: "Coach" }, coach: { name: "Meg", role: "Classmate" } };
const presenterLabel = (voice) => { const p = PRESENTER[voice] || PRESENTER.guide; return `${p.role} · ${p.name}`; };

// ---------------------------------------------------------------- the evidence language
// v1 keeps three conditions: with help, on your own, and on your own a day later.
// There is no evidence for "in a new situation", so that pip is always an empty ring.
const CONDITIONS = ["with help", "on your own", "later", "in a new situation"];
function pipKinds(st) {
  if (st === "met_with_help") return ["help", "", "", ""];
  if (st === "met_unaided") return ["help", "own", "", ""];
  if (st === "met_unaided_later") return ["help", "own", "own", ""];
  return ["", "", "", ""];
}
function pips(st, disputed) {
  const kinds = pipKinds(st);
  return h("span", { class: "pips", role: "img", "aria-label": pipsLabel(st) },
    kinds.map((k, i) => h("span", { class: ["pip", k, disputed && !k ? "disp" : ""].filter(Boolean).join(" "), title: CONDITIONS[i] })));
}
function pipsLabel(st) {
  if (st === "met_with_help") return "Shown with help";
  if (st === "met_unaided") return "Shown on your own";
  if (st === "met_unaided_later") return "Shown on your own, and again later";
  if (st === "not_met") return "Not yet";
  return "Not tested";
}
const evidence = (s) => (s.state === "not_met" ? h("span", { class: "notyet" }, "not yet") : pips(s.state, s.disputed));

const BAR_ORDER = [
  ["untested", "s-untested"],
  ["not_met", "s-notyet"],
  ["met_with_help", "s-help"],
  ["met_unaided", "s-own"],
  ["met_unaided_later", "s-later"],
];
// Share of skills per state, weakest evidence first, so the bar fills from the left as you go.
function sbar(counts, total) {
  const bar = h("div", { class: "sbar" });
  if (!total) return bar;
  for (const [k, cls] of BAR_ORDER) {
    const n = counts[k] || 0;
    if (!n) continue;
    bar.append(wide(h("i", { class: cls }), `${(n * 100 / total).toFixed(2)}%`));
  }
  return bar;
}
function countStates(skills) {
  const c = {};
  for (const s of skills) c[s.state] = (c[s.state] || 0) + 1;
  return c;
}
const allSkills = (p) => p.domains.flatMap((d) => d.groups.flatMap((g) => g.skills));
const domainSkills = (d) => d.groups.flatMap((g) => g.skills);

// ---------------------------------------------------------------- links that open later

/* Easy Auth loses everything after the "#" when it sends you to sign in, and keeps the path and the query. So a
   link Dojo hands out to be opened later (a calendar event, a nudge, an episode's notes) is /?go=<route>, and the
   app turns it into the page route when it starts. A route must pass GO_ROUTE, and it only ever goes after the
   fixed "/#/": it cannot be an address, so this never leaves the app. The same rule is in app/deeplink.py and
   sw.js, and tests/test_deeplink.py keeps the three equal. */
const GO_ROUTE = /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,5}$/;
const goRoute = (value) => (typeof value === "string" && value.length <= 120 && GO_ROUTE.test(value) ? value : null);

/* First thing at start, before anything reads the route: /?go=<route> becomes /#/<route> and the query goes.
   URLSearchParams decodes the value once; a "%" left after that fails the rule, so nothing is decoded twice.
   A value that fails, or a repeated one, is dropped and the start page opens. */
function openDeepLink() {
  const params = new URLSearchParams(location.search);
  if (!params.has("go")) return;
  const all = params.getAll("go");
  const route = all.length === 1 ? goRoute(all[0]) : null;
  try { history.replaceState(null, "", route ? `/#/${route}` : "/"); } catch (e) { /* the page opens as it is */ }
}

// ---------------------------------------------------------------- server calls

/* Every call says it is not a page (X-Requested-With). Once the sign-in has ended, Easy Auth then answers
   401 or an empty 403 instead of its sign-in page. That page would set a new nonce cookie, which breaks a
   sign-in under way in another tab ("invalid nonce", round and round). So Dojo never starts a sign-in on
   its own: it says the sign-in has ended, and a reload signs in again. The reload asks for /?go=<route> and
   not for the page as it is, because Easy Auth would lose the "#" and open the start page. */
const signIn = { banner: null };
function reloadToSignIn() {
  const route = location.hash.startsWith("#/") ? goRoute(location.hash.slice(2)) : null;
  location.replace(route ? `/?go=${route}` : "/");
}
function signInEnded() {
  if (!signIn.banner) {
    hideNewBuild();
    signIn.banner = h("div", { class: "signedout", role: "alert" },
      h("span", { class: "ic" }, icon("lock", "sm")),
      h("p", null, h("b", null, "Your sign-in has ended."), " Reload to sign in again."),
      h("div", { class: "actions" }, button("Reload", reloadToSignIn, "sm")));
    document.body.append(signIn.banner);
  }
  return Object.assign(new Error("Your sign-in has ended. Reload the page to sign in again."), { status: 401, signedOut: true });
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" };
  if (opts.raw !== undefined) {
    init.headers["Content-Type"] = opts.type;
    init.body = opts.raw;
  } else if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  if (opts.signal) init.signal = opts.signal;
  if (init.method !== "GET") init.headers["X-Dojo"] = "1";
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    throw new Error("Dojo could not be reached. If you were away for a while, your sign-in may have expired: reload the page.");
  }
  if (r.status === 401) throw signInEnded();
  let data = null, text = "";
  try { text = await r.text(); data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  // Easy Auth refuses a call without a sign-in with an empty 403; Dojo's own refusals always say why.
  if (r.status === 403 && !text) throw signInEnded();
  if (signIn.banner && r.ok) { signIn.banner.remove(); signIn.banner = null; }  // signed in again in another tab
  if (!r.ok) {
    const d = data && data.detail;
    const fail = (message) => Object.assign(new Error(message), { status: r.status });
    if (typeof d === "string") throw fail(d);
    // Team Dojo (ADR 0009): a refusal with a code says which page to show (not_member, notice, removed, ...).
    if (d && typeof d === "object" && typeof d.code === "string") throw Object.assign(fail(String(d.message || "")), { code: d.code });
    if (d) throw fail("The request was not accepted: " + JSON.stringify(d).slice(0, 300));
    const plain = text.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    throw fail(`HTTP ${r.status}${plain ? ": " + plain.slice(0, 240) : ""}`);
  }
  return data;
}

async function runJob(start, onStep) {
  let job = await start;
  const t0 = Date.now();
  while (job.state === "queued" || job.state === "running") {
    if (onStep) onStep(job.step || "Working", Math.round((Date.now() - t0) / 1000));
    await sleep(2000);
    job = await api(`/api/jobs/${job.id}`);
  }
  if (job.state !== "done") throw new Error(job.error || `This ${job.kind} was ${job.state}.`);
  return job.result;
}

function jobCard(title, start, onDone, onFail) {
  const step = h("p", { class: "muted small" }, "Starting…");
  const el = panel(eyebrow("wand", "Working"), h("h2", null, title),
    h("div", { class: "progress", role: "progressbar", "aria-label": title }, h("div", { class: "progress-fill" })), step,
    h("p", { class: "small muted" }, "The models write this from the official sources, and an independent model checks it. It can take a minute or two. You can leave this page; the result will be on the skill page."));
  const token = state.nav;
  runJob(start, (s, secs) => { step.textContent = `${s} · ${secs}s`; })
    .then((res) => { if (token === state.nav) onDone(res); })
    .catch((e) => {
      if (token !== state.nav) return;
      if (onFail) onFail(e);
      else put(el, h("h2", null, "That did not work"), h("p", { class: "sub" }, e.message),
        h("div", { class: "actions top" }, linkButton("See the studio log", "#/quality", "ghost")));
    });
  return el;
}

const withProgress = (title, start) => new Promise((resolve, reject) => { put($view(), jobCard(title, start, resolve, reject)); });

async function act(action, pid, sid) {
  const token = state.nav;
  try {
    if (action === "lesson") return await startLesson(pid, sid, false);
    const res = await withProgress(ACTION_LABEL[action], api("/api/items", { method: "POST", body: { package: pid, skill: sid, mode: action } }));
    if (token === state.nav) location.hash = `#/item/${res.item}`;
  } catch (e) {
    if (token === state.nav) put($view(), errorPanel(e));
  }
}

// Writing a skill's lesson again replaces it for everyone, so it is the owner's (ADR 0009).
async function rewriteLesson(lessonId) {
  const token = state.nav;
  try {
    const res = await withProgress("Writing and checking the lesson", api(`/api/lessons/${lessonId}/rewrite`, { method: "POST" }));
    if (token === state.nav) location.hash = `#/lesson/${res.lesson}`;
  } catch (e) {
    if (token === state.nav) put($view(), errorPanel(e));
  }
}

async function startLesson(pid, sid, fresh, tab) {
  const token = state.nav;
  const suffix = tab ? `/${tab}` : "";
  try {
    if (!fresh) {
      const v = await api(`/api/skills/${pid}/${sid}`);
      if (v.lessons.length) { location.hash = `#/lesson/${v.lessons[0].id}${suffix}`; return; }
    }
    const res = await withProgress("Writing and checking the lesson", api("/api/lessons", { method: "POST", body: { package: pid, skill: sid } }));
    if (token === state.nav) location.hash = `#/lesson/${res.lesson}${suffix}`;
  } catch (e) {
    if (token === state.nav) put($view(), errorPanel(e));
  }
}

function sourceList(list) {
  // A page Dojo found later for a built-in skill (ADR 0008) carries when the checker confirmed it.
  return h("div", null, (list || []).map((x) => h("div", { class: "src" }, icon("file", "sm"),
    h("div", null, ext(x.url, x.title || x.url),
      h("div", { class: "snap" }, [x.publisher, x.license && `licensed ${x.license}`, x.verified_at && `found and checked by Dojo on ${fmtWhen(x.verified_at)}`,
        x.stale && "last good copy used"].filter(Boolean).join(" · "))))));
}

function quoteToggle(p, sources) {
  const src = (sources || []).find((x) => x.n === p.source);
  const box = h("blockquote", { class: "quote", hidden: true }, `“${p.quote}”`,
    h("footer", null, "From ", src ? ext(src.url, src.title) : `source ${p.source}`, ", word for word"));
  const btn = h("button", { class: "cite", type: "button", "aria-expanded": "false", title: "Show the exact words from the source" }, `[${p.source}]`);
  btn.addEventListener("click", () => { box.hidden = !box.hidden; btn.setAttribute("aria-expanded", String(!box.hidden)); });
  return [btn, box];
}

// ---------------------------------------------------------------- when a source page changes (ADR 0006)
// The server words every notice, so the skill, the lesson tabs, the course and Studio say the same.
// "recheck": a quote or a whole page is gone. "holds" and "rewritten" are quieter: nothing is wrong.
function driftNote(d, tab) {
  if (!d || !d.notice) return null;
  const warn = d.state === "recheck";
  const gone = warn ? d.gone || [] : [];
  return h("div", { class: warn ? "drift-note" : "drift-note ok", role: warn ? "status" : null },
    icon(warn ? "refresh" : "check", "sm"),
    h("div", null, h("p", null, d.notice),
      gone.length ? h("p", { class: "small" }, gone.length === 1 ? "This page is gone: " : "These pages are gone: ",
        gone.map((p, i) => [i ? ", " : "", ext(p.url, p.title || p.url), p.moved ? ` (moved to ${movedTo(p)})` : ""])) : null,
      warn && d.replaced_by ? h("p", { class: "small" },
        h("a", { class: "link", href: `#/lesson/${d.replaced_by}/${tab || "read"}` }, "Open the lesson written from the page as it is now")) : null));
}

// Where a cited page that moved to another page now leads: the path on the same site, else the whole address.
// Shown as text, not as a link: it is where the site sends a reader, not a page Dojo checked.
function movedTo(p) {
  try {
    const from = new URL(p.url), to = new URL(p.moved);
    return to.host === from.host ? to.pathname + to.search : p.moved;
  } catch (e) {
    return String(p.moved || "");
  }
}

// In a lesson being re-checked, the quotes that are no longer on their page.
function lostQuote(L) {
  const d = L.drift && L.drift.state === "recheck" ? L.drift : null;
  if (!d) return () => null;
  const removed = new Set(d.removed || []);
  const gone = new Map((d.gone || []).map((p) => [p.url, p]));
  const url = (n) => ((L.sources || []).find((x) => x.n === n) || {}).url;
  return (p) => (removed.has(p.quote) ? chip("no longer on the page", "rose")
    : gone.has(url(p.source)) ? chip(gone.get(url(p.source)).moved ? "page moved" : "page gone", "rose") : null);
}

// What changed on a cited page since a lesson was written from it, or since a lab was last looked at; or null.
function pageChange(d, url) {
  if (!d || d.state !== "recheck") return null;
  const g = (d.gone || []).find((p) => p.url === url);
  if (g) return h("span", { class: "ckd drift", title: g.moved ? `Moved to ${movedTo(g)}` : null }, icon("alert", "xs"), g.moved ? "page moved" : "page gone");
  if ((d.pages || []).some((p) => p.url === url)) return h("span", { class: "ckd drift" }, icon("refresh", "xs"), `changed ${d.date}`);
  return null;
}

// The check mark next to a lesson's source, or what changed on that page since the lesson was written.
function sourceCheck(L, s) {
  return pageChange(L.drift, s.url) || h("span", { class: "ckd" }, icon("check", "xs"), s.stale ? "last good copy" : "checked");
}

async function ensurePkg() {
  if (state.pkg && state.pkgPid === state.pid) return state.pkg;
  const p = await api(`/api/packages/${state.pid}`);
  state.pkg = p;
  state.pkgPid = state.pid;
  state.laterHours = p.later_hours;
  renderSide();
  return p;
}

// ---------------------------------------------------------------- the shell

function examWhen(pid) {
  const d = (state.me.profile.exam_dates || {})[pid];
  if (!d) return null;
  const w = weeksTo(d);
  return { date: d, text: w === null ? fmtDay(d) : w >= 0 ? `exam ${fmtDay(d)} · ${w} week${w === 1 ? "" : "s"}` : `exam was ${fmtDay(d)}`, short: fmtDay(d) };
}

function renderHeader() {
  const me = state.me;
  const pack = myExams().find((p) => p.id === state.pid) || myExams()[0] || {};
  $("exam-code").textContent = pack.exam || "—";
  $("exam-title").textContent = pack.title || "";
  const w = examWhen(state.pid);
  $("exam-when").textContent = w ? w.text : "no date set";
  $("exam-pill").setAttribute("title", `${pack.exam || ""} ${pack.title || ""}${w ? " · " + w.text : ""}`);
  const initials = String(me.name || "You").split(/\s+/).filter(Boolean).slice(0, 2).map((x) => x[0].toUpperCase()).join("") || "Y";
  $("me-initials").textContent = initials;
  $("me-initials").setAttribute("title", me.name || "You");
}

// The owner (admin) sees Studio, Add an exam and Members; a member of a team Dojo does not (ADR 0009).
// With team mode off the one learner is the owner, so nothing changes.
const isAdmin = () => !state.me || state.me.role !== "learner";

// The exams this learner studies: every exam for the owner, the chosen ones for a member (ADR 0009).
function myExams() {
  const me = state.me;
  if (!me || isAdmin() || !Array.isArray(me.enrolled)) return me ? me.packages : [];
  return me.packages.filter((p) => me.enrolled.includes(p.id));
}

function navLink(href, iconName, label, count) {
  return h("a", { class: "nav", href, "data-sec": href }, icon(iconName), h("span", null, label), count !== null && count !== undefined ? h("span", { class: "n" }, count) : null);
}

function renderSide() {
  const side = $("side");
  const skills = state.pkg && state.pkgPid === state.pid ? state.pkg.coverage.skills : null;
  const exams = h("div", { class: "exams" }, h("h6", null, "Your exams"),
    myExams().map((p) => {
      const w = examWhen(p.id);
      return h("button", { class: "ex", type: "button", "aria-current": p.id === state.pid ? "true" : null, onclick: () => switchPackage(p.id) },
        h("b", null, p.exam), h("span", null, w ? w.short : "no date"));
    }),
    isAdmin() ? h("a", { class: "exadd", href: "#/add-exam" }, "Add an exam") : h("a", { class: "exadd", href: "#/exams" }, "Change my exams"));
  put(side,
    navLink("#/today", "today", "Today"),
    navLink("#/skills", "course", "Course", skills),
    navLink("#/rehearsal", "practice", "Practice"),
    navLink("#/record", "record", "Record"),
    navLink("#/plan", "plan", "Plan"),
    h("div", { class: "sep" }),
    navLink("#/quality", "studio", isAdmin() ? "Studio" : "Studio log"),
    h("p", { class: "cap" }, isAdmin() ? "how your course is built" : "what the checker removed"),
    isAdmin() && state.me.team ? navLink("#/members", "shield", "Members") : null,
    exams,
    h("div", { class: "foot" }, h("a", { href: "#/help" }, "Help"), h("a", { href: "#/phone" }, "On your phone"), h("a", { href: "#/about" }, "How Dojo works"),
      h("a", { href: "#/record" }, "Export or delete"),
      h("span", null, "Not a pass prediction.")));
  markNav();
}

const NAV_SECTION = [
  [/^#\/(skills|skill|lesson|item)/, "#/skills"],
  [/^#\/(rehearsal|exam\/|readiness)/, "#/rehearsal"],
  [/^#\/(record|play)/, "#/record"],
  [/^#\/plan/, "#/plan"],
  [/^#\/(quality|add-exam)/, "#/quality"],
  [/^#\/(today|check|recall|five)/, "#/today"],
  [/^#\/members/, "#/members"],
];
function markNav() {
  const hash = location.hash || "#/today";
  const sec = (NAV_SECTION.find(([re]) => re.test(hash)) || [])[1] || "";
  for (const a of document.querySelectorAll("#side .nav")) a.classList.toggle("on", a.getAttribute("data-sec") === sec);
}

async function choosePackage(pid) {
  state.pid = pid;
  state.pkg = null;
  state.pickedSkill = null;
  $("exam").value = pid;
  renderHeader();
  renderSide();
  try { await api("/api/settings", { method: "PUT", body: { active_package: pid } }); } catch (e) { /* the view still switches */ }
}

async function switchPackage(pid) {
  if (pid === state.pid) return;
  await choosePackage(pid);
  if (/^#\/(skill|lesson|item|answer|rehearsal\/)/.test(location.hash)) location.hash = "#/today"; else route();
}

// A calendar event names its exam: #/check/for/<exam>, #/recall/for/<exam> and #/rehearsal/for/<exam>
// switch to that exam first.
async function openFor(pid, where) {
  if (pid !== state.pid && myExams().some((p) => p.id === pid)) await choosePackage(pid);
  location.replace(where);
  return [];
}

// The last choice in the exam switcher opens "Add an exam" instead of switching.
const ADD_EXAM = "+add";
function fillExamSelect() {
  const sel = $("exam");
  put(sel, myExams().map((p) => h("option", { value: p.id }, `${p.exam} · ${p.title}`)),
    isAdmin() ? h("option", { value: ADD_EXAM }, "Add an exam…") : null);
  sel.value = state.pid;
}

// The exams change when one is added or removed; an exam that went is never left selected.
async function refreshMe() {
  state.me = await api("/api/me");
  if (!myExams().some((p) => p.id === state.pid)) {
    state.pid = state.me.profile.active_package;
    state.pkg = null;
    state.pickedSkill = null;
  }
  fillExamSelect();
  renderHeader();
  renderSide();
}

// ---------------------------------------------------------------- Today

function fmtCard(iconName, label, note, onclick) {
  return h("button", { class: "fmt", type: "button", onclick }, h("span", { class: "ic" }, icon(iconName)),
    h("span", null, h("b", null, label), h("small", null, note)));
}

function heroCard(s, sk, weight) {
  const hasLesson = sk && sk.has_lesson;
  const fmts = hasLesson
    ? FORMATS.map((f) => fmtCard(f.name, f.label, f.note, () => startLesson(state.pid, s.skill, false, f.tab)))
    : [fmtCard(ACTION_ICON[s.action] || "target", ACTION_LABEL[s.action] || "Start",
        s.action === "lesson" ? "written from the official sources, then checked" : MODE_TEXT[s.action] || "", () => act(s.action, state.pid, s.skill))];
  const then = "a question in your own words, no hints — graded against the official sources, quoting you.";
  return h("section", { class: "panel hero" },
    h("div", { class: "panel-h" }, eyebrow("target", "Your next step", "amber"),
      h("span", { class: "tag amber" }, [s.domain, weight ? `${weight} of the exam` : null].filter(Boolean).join(" · "))),
    h("h2", null, s.text),
    h("p", { class: "s-line" }, "Study-guide skill", h("span", { class: "mono" }, skillTag(s.skill)),
      sk && sk.seen ? h("span", { class: "seen" }, icon("eye", "xs"), "seen") : null,
      sk ? evidence(sk) : null),
    h("div", { class: "why" }, icon("sparkle", "sm"),
      h("div", null, h("p", { class: "eyebrow" }, "Why this, now"), h("p", null, s.why))),
    h("div", { class: "panel-h tight" }, h("p", { class: "eyebrow" }, hasLesson ? "Choose how to learn it" : "Start here"),
      hasLesson ? h("span", { class: "small muted" }, "every format teaches the same thing · none counts as proof on its own") : null),
    h("div", { class: "fmts" }, fmts),
    h("div", { class: "then" },
      h("p", { class: "prow" }, icon("arrow", "xs"), h("span", null, h("b", null, "Then: "), then)),
      h("p", { class: "prow" }, icon("eye", "xs"), h("span", null, h("b", null, "What this cannot show yet: "),
        "that you still know it later. A later check counts only after " + (state.laterHours || 20) + " hours."))),
    h("div", { class: "actions", role: "group", "aria-label": "Other ways to work on this skill" },
      hasLesson ? button("Practise with hints", () => act("practice", state.pid, s.skill), "ghost sm") : null,
      hasLesson ? button("Check, no help", () => act("check", state.pid, s.skill), "ghost sm") : null,
      linkButton("Open the skill", `#/skill/${state.pid}/${s.skill}`, "ghost sm")));
}

function domainEvidence(p) {
  const rows = p.domains.map((d) => {
    const sk = domainSkills(d);
    const c = countStates(sk);
    const own = (c.met_unaided || 0) + (c.met_unaided_later || 0);
    return h("div", { class: "dom" },
      h("span", null, d.title),
      h("span", { class: "w" }, d.weight),
      sbar(c, sk.length),
      h("span", { class: "c" }, `${sk.length} skills · ${own ? `${own} on your own or better` : `${c.untested || 0} not tested`}`));
  });
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("scale", `${p.meta.exam} · what you have shown so far`),
      h("div", { class: "legend" },
        h("span", null, h("i", { class: "sw s-untested" }), "not tested"),
        h("span", null, h("i", { class: "sw s-notyet" }), "not yet"),
        h("span", null, h("i", { class: "sw s-help" }), "with help"),
        h("span", null, h("i", { class: "sw s-own" }), "on your own"),
        h("span", null, h("i", { class: "sw s-later" }), "later"))),
    rows,
    h("p", { class: "note top" }, `About ${p.coverage.weight_not_shown_unaided}% of the exam's weight is not yet shown without help. `
      + "Weights come from the study guide's domain ranges, split evenly across each domain's skills. " + NOT_A_PREDICTION));
}

// ---------------------------------------------------------------- due recalls (ADR 0011)
// A link to the lesson section that teaches what was missed, or to the lesson when no section matched.
const sectionHref = (w) => `#/lesson/${w.lesson}/read${Number.isInteger(w.section) ? `/s${w.section}` : ""}`;
const LADDER_DAYS = [1, 3, 7, 16, 35];

function recallWhy(d) {
  if (d.missed) return "Not right last time, so it is due again today.";
  if (d.last_success) return `Right on ${fmtWhen(d.last_success)}. Recalling it again today helps it last.`;
  return "Due today.";
}

async function startRecalls(btn, status) {
  btn.disabled = true;
  status.textContent = "Starting…";
  const token = state.nav;
  try {
    const s = await api("/api/checks", { method: "POST", body: { package: state.pid, recall: true } });
    if (token === state.nav) location.hash = `#/check/${s.id}`;
  } catch (e) { status.textContent = e.message; btn.disabled = false; }
}

/* First on Today when anything is due. Capped by the server, so it is never a wall: what is over the cap
   is only counted, and comes on the next days. Nothing here says "overdue". */
function recallCard(r, closed) {
  if (!r || closed || (!r.due.length && !r.done_today && !r.waiting)) return null;
  if (!r.due.length) {
    return h("section", { class: "panel" },
      h("div", { class: "panel-h" }, eyebrow("bell", "Recalls"), chip("done for today", "line")),
      h("p", { class: "sub" }, (r.done_today ? `${plural(r.done_today, "recall")} done today. ` : "Today's recall questions have been written. ")
        + (r.waiting ? "More are waiting. They come over the next days." : "Nothing else is due today.")));
  }
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const go = button("Start recalls", () => startRecalls(go, status), "amber");
  return h("section", { class: "panel recall" },
    h("div", { class: "panel-h" }, eyebrow("bell", "Due recalls", "amber"), chip(`about ${r.minutes} min`, "line")),
    h("p", { class: "sub" }, "Skills you showed on your own before. Answering again on a later day helps them last. "
      + "A new question for each, no hints, spoken or typed."),
    h("ul", { class: "cpicks" }, r.due.map((d) => h("li", { class: "cpick" },
      h("span", { class: "cpip" }, icon(d.missed ? "refresh" : "bell", "xs")),
      h("span", null, h("b", null, d.text), h("small", null, recallWhy(d)),
        d.repair ? h("a", { class: "link small", href: sectionHref(d.repair) },
          d.repair.heading ? `Look at “${d.repair.heading}” first` : "Look at the lesson first") : null)))),
    r.waiting ? h("p", { class: "note top" }, "More are waiting. They come over the next days.") : null,
    h("div", { class: "actions top" }, go, linkButton("Only 5 minutes", "#/five", "ghost sm"), linkButton("What happens", "#/recall", "ghost sm")), status);
}

// ---------------------------------------------------------------- the 5-minute session (ADR 0013)
// Today's due recalls (at most two), then one repair of a confident error, then a clear stop.
// The steps live in this tab only; the recalls and the repair are recorded as they always are.
const FIVE_KEY = "dojo.five";
function fiveRead() {
  try { return JSON.parse(sessionStorage.getItem(FIVE_KEY) || "null"); } catch (e) { return null; }
}
function fiveSave(s) {
  try { sessionStorage.setItem(FIVE_KEY, JSON.stringify(s)); } catch (e) { /* private browsing: the steps start over */ }
}
let fiveNote = "";   // a calm note for the next draw of the 5 minutes
// The repair question of the 5 minutes was answered: only now is the repair done.
function fiveRepaired(itemId) {
  const s = fiveRead();
  if (!s || s.step !== "repair" || s.item !== itemId) return;
  s.step = "done";
  s.did += 1;
  fiveSave(s);
}
const FIVE_STEPS = [["recalls", "Due recalls"], ["repair", "One repair"], ["done", "Stop"]];

function fiveClosed(e) {
  return h("section", { class: "panel" }, eyebrow("clock", "Closed for now"),
    h("h2", null, "The 5 minutes are closed"),
    h("p", { class: "sub" }, (e && e.message) || "A timed practice exam is running."),
    h("p", { class: "small muted" }, "They open again as soon as the exam is handed in."),
    h("div", { class: "actions top" }, linkButton("Back to Today", "#/today", "ghost")));
}

async function viewFive() {
  const token = state.nav;
  let v;
  try { v = await api("/api/five"); } catch (e) { if (checkShut(e)) return fiveClosed(e); throw e; }
  const old = fiveRead();
  const s = old && old.day === v.day ? old : { day: v.day, step: "recalls", check: null, did: 0, repair: null };
  if (s.step === "recalls" && !v.recalls.count && !v.check) s.step = "repair";
  if (s.step === "repair" && !v.repair) s.step = "done";
  if (s.step === "done") { s.repair = null; s.item = null; }
  fiveSave(s);
  const at = FIVE_STEPS.findIndex(([k]) => k === s.step);
  const steps = h("ol", { class: "five-steps", "aria-label": "The steps" }, FIVE_STEPS.map(([k, label], i) =>
    h("li", { class: i < at ? "done" : i === at ? "on" : null, "aria-current": i === at ? "step" : null },
      h("span", { class: "n" }, i < at && s.did ? icon("check", "xs") : String(i + 1)), h("span", null, label))));
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" }, fiveNote);
  fiveNote = "";
  const goOn = (step) => { s.step = step; fiveSave(s); route(); };
  let card;
  if (s.step === "recalls") {
    const start = async (btn, endOpen) => {
      btn.disabled = true;
      status.textContent = "Starting…";
      try {
        const id = v.check || (await api("/api/five/start", { method: "POST", body: endOpen ? { end_open: true } : {} })).id;
        s.check = id;
        fiveSave(s);
        if (token === state.nav) location.hash = `#/check/${id}`;
      } catch (e) {
        if (e.status === 409 && !endOpen && token === state.nav) { route(); return; }   // a check was opened meanwhile
        status.textContent = e.message; btn.disabled = false;
      }
    };
    const open = !v.check && v.open_check;
    const go = button(v.check ? "Go on with the recalls" : "Start", () => start(go, false), "amber");
    const endIt = button("End it and start the 5 minutes", () => start(endIt, true), "amber");
    card = h("section", { class: "panel recall" },
      h("div", { class: "panel-h" }, eyebrow("bell", "Due recalls", "amber"), chip(`about ${v.recalls.minutes} min`, "line")),
      h("p", { class: "sub" }, "Skills you showed on your own before. A new question for each, no hints, spoken or typed."),
      h("ul", { class: "cpicks" }, v.recalls.skills.map((x) => h("li", { class: "cpick" },
        h("span", { class: "cpip" }, icon("bell", "xs")), h("span", null, h("b", null, x.text))))),
      v.recalls.later ? h("p", { class: "note top" }, `${plural(v.recalls.later, "other recall")} can wait for the next days.`) : null,
      open ? h("div", { class: "five-open top" },
        h("h3", null, "You have a check open"),
        h("p", { class: "small muted" }, "Go on with it, or end it and start the 5 minutes. Answers you already gave stay in your record."),
        h("div", { class: "actions top" }, linkButton("Go on with the check", `#/check/${open.id}`, "ghost"), endIt,
          button("Skip the recalls", () => { s.skipped = true; goOn("repair"); }, "ghost sm")))
        : h("div", { class: "actions top" }, go, button("Skip the recalls", () => { s.skipped = true; goOn("repair"); }, "ghost sm")),
      status);
  } else if (s.step === "repair") {
    const r = v.repair;
    // The repair counts as done only once its question has been answered (answerForm calls fiveRepaired).
    const tryIt = button(s.item ? "Go on with the question" : "Try a new question", async () => {
      if (s.item) { location.hash = `#/item/${s.item}`; return; }
      tryIt.disabled = true;
      status.textContent = "";
      try {
        const res = await withProgress(ACTION_LABEL.check, api("/api/items", { method: "POST", body: { package: v.package, skill: r.skill, mode: "check" } }));
        s.repair = r.skill;
        s.item = res.item;
        fiveSave(s);
        if (token === state.nav) location.hash = `#/item/${res.item}`;
      } catch (e) {
        if (token !== state.nav) return;
        // withProgress replaced the page: draw the repair step again, with a calm note.
        fiveNote = "Dojo could not write the question just now. Nothing is lost; try again in a moment.";
        route();
      }
    }, "amber");
    card = h("section", { class: "panel" },
      h("div", { class: "panel-h" }, eyebrow("refresh", "One repair"), chip(`about ${v.minutes - v.recalls.minutes} min`, "line")),
      h("p", { class: "sub" }, "You were sure of this answer, and it was not right. Read what the lesson says, then try a new question."),
      h("div", { class: "rv kerr" }, h("span", null, h("b", null, r.text), h("small", null, `${r.label} · ${r.domain}`),
        r.stem ? h("small", { class: "kstem" }, r.stem) : null)),
      h("div", { class: "actions top" },
        r.where ? linkButton(r.where.heading ? `Read “${r.where.heading}”` : "Read the lesson", sectionHref(r.where), "ghost sm") : null,
        tryIt,
        button("Skip", () => { s.skipped = true; s.item = null; goOn("done"); }, "ghost sm")),
      status);
  } else {
    const head = s.did ? ["Done for today", "That is it for today", "You can stop here. Everything else can wait for another day."]
      : s.skipped ? ["Stopped", "Stopped for today", "Nothing was answered this time. What is due stays due, with no penalty."]
      : ["Nothing due", "Nothing is due right now", "No recall is due and there is nothing to repair. If nudges are on, Dojo sends one when something is."];
    card = h("section", { class: "panel cdone" }, eyebrow("check", head[0]),
      h("h2", null, head[1]),
      h("p", { class: "sub" }, head[2]),
      v.recalls.later ? h("p", { class: "small muted" }, `${plural(v.recalls.later, "other recall")} can wait for the next days.`) : null,
      h("div", { class: "actions top" }, linkButton("Back to Today", "#/today", "amber")));
  }
  return [pageHead("5 minutes", `${v.exam} · recalls first, then one repair, then stop.`, null, helpQ("the-5-minute-session")),
    h("div", { class: "five" }, s.step === "done" && !s.did && !s.skipped ? null : steps, card)];
}

// A way back to the 5 minutes from the lesson section and the repair question (an installed app has no Back button).
function fiveBanner(hash) {
  const s = fiveRead();
  if (!s) return null;
  const text = s.step === "repair" && (hash.startsWith("#/lesson/") || (s.item && hash === `#/item/${s.item}`)) ? "Back to your 5 minutes"
    : s.step === "done" && s.repair && hash.startsWith("#/item/") ? "Finish your 5 minutes" : "";
  return text ? h("p", { class: "five-back" }, icon("clock", "sm"), h("a", { class: "link", href: "#/five" }, text)) : null;
}

function planCard(t) {
  const plan = t.plan;
  const dateInput = h("input", { type: "date", id: "examdate", value: plan.exam_date || "" });
  const save = button("Save", async () => {
    await api("/api/settings", { method: "PUT", body: { exam_dates: { [state.pid]: dateInput.value || null } } });
    state.me = await api("/api/me");
    renderHeader(); renderSide(); route();
  }, "ghost sm");
  const row = (iconName, big, small) => h("div", { class: "planrow" }, h("span", { class: "picon" }, icon(iconName, "sm")),
    h("span", null, h("b", null, big), h("small", null, small)));
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("clock", "Your plan"),
      plan.days_left !== null ? chip(plan.days_left >= 0 ? `${plan.days_left} days to go` : "date has passed", "amber") : null),
    h("div", { class: "datefield" }, h("label", { class: "field", for: "examdate" }, "Exam date", dateInput), save),
    h("div", { class: "divider" }),
    row("target", `${plan.never_attempted} of ${plan.skills_total} skills never attempted`, "nothing in the record yet"),
    row("hand", `${plan.skills_not_shown_unaided} of ${plan.skills_total} not yet shown without help`, "help was available, or not tested"),
    row("refresh", `${plan.attempted_without_later} attempted skill${plan.attempted_without_later === 1 ? "" : "s"} without a later check`,
      plan.later_checks_per_week ? `about ${plan.later_checks_per_week} later checks a week to cover them all` : "a later check is a day after the teaching"),
    row("file", `Lessons ready: ${t.lessons_ready} of ${plan.skills_total}`,
      t.lessons_ready < plan.skills_total ? "Dojo writes the rest in the background, most exam weight first" : "every skill has a written lesson"),
    h("p", { class: "note top" }, plan.note));
}

function rehearsalCard(t, x) {
  const r = t.rehearsals[0];
  const label = (y) => rehearsalOpen(y) ? `${y.answered} of ${y.items} answered` : `${y.correct} of ${y.answered} right`;
  const last = x && x.attempts.length ? x.attempts[0] : null;
  const exam = x && x.running
    ? h("a", { class: "rv", href: `#/exam/${x.running.id}` }, h("span", null, h("b", null, "An exam is still running"),
        h("small", null, "Its clock is kept by the server. Go back to it.")), icon("right", "sm"))
    : last
      ? h("a", { class: "rv", href: `#/exam/${last.id}` }, h("span", null,
          h("b", null, last.state === "closed" ? `${last.correct} of ${last.questions} right` : "Ready to start"),
          h("small", null, `${LENGTH_NAME[last.length] || last.length} · ${fmtDate(last.closed_at || last.created)}`
            + (last.closed_reason === "time" ? " · time ran out" : ""))), icon("right", "sm"))
      : h("p", { class: "sub" }, "No timed practice exam yet. One question at a time, a countdown, and the reasons at the end.");
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("practice", "Practice"), linkButton("Start one", "#/rehearsal", "ghost sm")),
    exam,
    r ? h("div", { class: "divider" }) : null,
    r
      ? h("div", null,
          h("p", { class: "eyebrow" }, "Quick questions"),
          h("a", { class: "rv", href: `#/rehearsal/${r.id}` }, h("span", null, h("b", null, label(r)),
            h("small", null, r.answered < r.items ? `not finished · ${fmtDate(r.created)}` : `${r.answered} of ${r.items} answered · ${fmtDate(r.created)}`)), icon("right", "sm")),
          t.rehearsals.slice(1, 2).map((y) => h("a", { class: "rv", href: `#/rehearsal/${y.id}` },
            h("span", null, h("b", null, label(y)), h("small", null, fmtDate(y.created))), icon("right", "sm"))))
      : null,
    h("p", { class: "note top" }, "Multiple-choice answers can be guesses, so they never fill a pip. They sit beside your evidence, not in it."),
    h("div", { class: "divider" }),
    h("a", { class: "rv", href: "#/readiness" }, h("span", null, h("b", null, "Should you book the exam?"),
      h("small", null, "Dojo's advice from your record and your practice exams. Not a prediction.")), icon("right", "sm")));
}

async function viewToday() {
  const [t, p, x, cplan, tp, pl] = await Promise.all([api(`/api/today/${state.pid}`), ensurePkg(),
    api(`/api/exams?package=${encodeURIComponent(state.pid)}`).catch(() => null),
    api(`/api/checks?package=${encodeURIComponent(state.pid)}`).catch((e) => (checkShut(e) ? { closed: true } : null)),
    api("/api/plan/today").catch(() => null), api("/api/play").catch(() => null)]);
  const byId = new Map(allSkills(p).map((s) => [s.id, s]));
  const weightOf = new Map(p.domains.map((d) => [d.title, d.weight]));
  const first = t.suggestions[0];
  const rest = t.suggestions.slice(1);
  const left = h("div", { class: "col grow" },
    recallCard(t.recalls, cplan && cplan.closed),
    first ? heroCard(first, byId.get(first.skill), weightOf.get(first.domain)) : panel(h("h2", null, "Nothing is waiting"),
      h("p", { class: "sub" }, "Nothing is due right now. Practise, or pick any skill in the course.")),
    checkCard(cplan),
    domainEvidence(p),
    rest.length || t.due.length
      ? h("section", { class: "panel" },
          h("div", { class: "panel-h" }, eyebrow("course", "Then these"), h("a", { class: "link small", href: "#/skills" }, "See the whole course")),
          rest.map((s) => h("div", { class: "rv" },
            h("a", { href: `#/skill/${state.pid}/${s.skill}` }, h("b", null, s.text), h("small", null, `${s.domain} · ${s.why}`)),
            button(ACTION_LABEL[s.action], () => act(s.action, state.pid, s.skill), "ghost sm"))),
          t.due.length ? h("div", { class: "divider wide" }) : null,
          t.due.length ? eyebrow("refresh", "Later checks") : null,
          t.due.map((d) => h("div", { class: "rv" },
            h("a", { href: `#/skill/${state.pid}/${d.skill}` }, h("b", null, d.text),
              h("small", null, d.due_now ? "due now" : `due ${fmtDate(d.due)}`)),
            d.due_now ? button("Check, no help", () => act("check", state.pid, d.skill), "ghost sm") : chip(fmtWhen(d.due)))))
      : null);
  const now = new Date();
  const hour = now.getHours();
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const stamp = now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })
    + " · " + now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const planned = tp ? h("div", { class: "chips today-plan" },
    chip([icon("clock", "xs"), tp.minutes ? `${tp.minutes} min planned today · ${tp.done} done` : "Nothing planned today"], "line"),
    linkButton([icon("plan", "sm"), "My week"], "#/plan", "ghost sm")) : null;
  return [h("div", { class: "between top wrap" }, pageHead(`${greet}, ${state.me.name}`,
      `${t.package.exam} · ${t.package.title}. One next step, chosen by exam weight and by what your record does not yet show.`,
      h("p", { class: "eyebrow" }, stamp), helpQ("today")), planned),
    h("div", { class: "row stackable grow" }, left, h("div", { class: "col side-col" }, todayPlanCard(tp), playCard(pl), planCard(t), rehearsalCard(t, x)))];
}

// ---------------------------------------------------------------- Course

// An added exam can hold a skill for which no official page passed the checks. Dojo keeps it in the
// list, so the blueprint stays whole, but never teaches it or asks about it.
const NO_SOURCE = "No verified official source yet. Dojo does not teach this skill or ask about it, and it counts as not shown.";

// Skill ids of an added exam end in a short hash of the skill's words; the position alone reads better.
const skillTag = (id) => String(id).replace(/\.[0-9a-f]{6}$/, "");

function skillRow(s, onPick) {
  const el = h("button", { class: "skill", type: "button", "data-sid": s.id, onclick: () => onPick(s, el) },
    h("span", { class: "s-name" }, s.text, h("span", { class: "mono" }, skillTag(s.id)),
      s.drift ? h("span", { class: "chip amber s-drift", title: s.drift.notice }, icon("refresh", "xs"), "re-checking") : null),
    h("span", { class: "s-ev" }, evidence(s), s.seen ? h("span", { class: "seen" }, icon("eye", "xs"), "seen") : null,
      s.disputed ? chip("disputed", "ochre") : null),
    s.sourced === false
      ? h("span", { class: "s-fmt nosrc", title: NO_SOURCE }, "no verified source")
      : h("span", { class: "s-fmt", title: s.has_lesson ? "Watch, read, listen to or present this lesson" : "No lesson written yet" },
          ficon("video", !s.has_lesson), ficon("read", !s.has_lesson), ficon("listen", !s.has_lesson), ficon("watch", !s.has_lesson)),
    h("span", { class: "s-last" }, s.latest ? `${s.latest.met} of ${s.latest.total} points · ${fmtWhen(s.latest.at)}`
      : s.rehearsal ? `practice only` : "—"));
  return el;
}

function skillDetail(box, pid, sid) {
  put(box, h("p", { class: "loading" }, "Loading…"));
  const token = state.nav;
  api(`/api/skills/${pid}/${sid}`).then((v) => {
    if (token !== state.nav) return;
    const s = v.skill, e = v.evidence;
    const taught = s.sources.length > 0;
    put(box,
      eyebrow("course", s.domain_title),
      h("h3", null, s.text),
      h("div", { class: "chips" }, chip(skillTag(s.id), "line"), chip(`weight ${s.domain_weight}`, "line"),
        chip(s.kind === "scenario" ? "a doing skill" : "an explaining skill", "line")),
      h("div", { class: "ev-status" }, h("div", { class: "between" }, h("b", null, e.state_label), evidence({ state: e.state, disputed: e.disputed })),
        e.latest ? h("q", null, `${e.latest.met} of ${e.latest.total} points on ${fmtWhen(e.latest.at)}`) : h("q", null, "Nothing in the record yet.")),
      h("div", { class: "d-sec first" }, eyebrow("read", "Learn it"),
        v.drift && v.drift.state === "recheck" ? driftNote(v.drift) : null,
        !taught
          ? h("p", { class: "prow" }, icon("alert", "xs"), NO_SOURCE)
          : v.lessons.length
          ? FORMATS.map((f) => h("a", { class: "fl", href: `#/lesson/${v.lessons[0].id}/${f.tab}` },
              h("span", { class: "ficon" }, icon(f.name, "sm")), h("b", null, f.label), h("span", { class: "fmeta" }, f.note)))
          : h("button", { class: "fl", type: "button", onclick: () => startLesson(pid, sid, false) },
              h("span", { class: "ficon" }, icon("wand", "sm")), h("b", null, "Write the lesson"), h("span", { class: "fmeta" }, "from the official sources"))),
      h("div", { class: "d-sec" }, eyebrow("check", "What the record shows"),
        e.shown.length
          ? e.shown.map((x) => h("p", { class: "prow" }, icon("check", "xs"), x))
          : e.latest
            ? h("p", { class: "prow" }, icon("hand", "xs"),
                h("span", null, `Latest attempt: ${e.latest.met} of ${e.latest.total} points · ` + (e.latest.conditions || []).join(" · ")))
            : h("p", { class: "prow" }, icon("clock", "xs"), "Nothing in the record yet."),
        e.not_yet_shown.slice(0, 4).map((x) => h("p", { class: "prow" }, icon("x", "xs"), h("span", null, h("b", null, "Not yet: "), x))),
        e.disputed ? h("p", { class: "conf" }, icon("alert", "xs"), `${e.disputed} disputed judgement${e.disputed === 1 ? " is" : "s are"} not counted.`) : null,
        e.rehearsal_total ? h("p", { class: "conf" }, icon("practice", "xs"),
          `Practice exam: ${e.rehearsal_correct} of ${e.rehearsal_total} multiple-choice right. Kept apart from your evidence.`) : null),
      pid === "dp-800" ? h("div", { class: "d-sec" }, eyebrow("target", "Try it in Azure SQL"),
        !labsOn() ? h("p", { class: "small muted" }, LABS_OFF)
        : LAB_SKILLS[sid] ? linkButton("Open the hands-on lab", `#/lab/${LAB_SKILLS[sid]}`, "amber sm") : h("p", { class: "small muted" }, "Labs are available for selected DP-800 skills.")) : null,
      h("div", { class: "d-sec" }, eyebrow("file", "Official sources"), taught ? sourceList(s.sources) : h("p", { class: "small muted" }, "None verified.")),
      h("div", { class: "acts" },
        linkButton("Open the skill", `#/skill/${pid}/${sid}`, "ghost sm"),
        taught ? button("Practise with hints", () => act("practice", pid, sid), "ghost sm") : null,
        taught ? button("Check, no help", () => act("check", pid, sid), "sm") : null));
  }).catch((err) => { if (token === state.nav) put(box, h("p", { class: "sub" }, err.message)); });
}

async function viewSkills() {
  const p = await ensurePkg();
  const skills = allSkills(p);
  const counts = countStates(skills);
  const detail = h("section", { class: "panel detail w430 stack scroll" },
    h("p", { class: "sub" }, "Pick a skill to see what the record holds, where it comes from and how to work on it."));
  const body = h("div", { class: "acc" });
  let filter = "", query = "";
  const onPick = (s, el) => {
    state.pickedSkill = s.id;
    for (const n of body.querySelectorAll(".skill")) n.classList.toggle("selected", n === el);
    skillDetail(detail, state.pid, s.id);
  };
  const render = () => {
    const q = query.trim().toLowerCase();
    const narrowed = Boolean(filter || q);
    const doms = p.domains.map((d) => {
      const sk = domainSkills(d).filter((s) => (!filter || (filter === "drift" ? s.drift : s.state === filter)) && (!q || s.text.toLowerCase().includes(q) || s.id.includes(q)));
      return { d, sk, all: domainSkills(d) };
    }).filter((x) => x.sk.length);
    if (!doms.length) { put(body, h("p", { class: "loading" }, "No skill matches this filter.")); return; }
    put(body, doms.map(({ d, sk, all }) => {
      // Domains start closed, as in the design: open the one you are working in.
      const open = narrowed || sk.some((s) => s.id === state.pickedSkill);
      const list = h("div", { class: "skills", hidden: !open }, sk.map((s) => skillRow(s, onPick)));
      const chev = icon(open ? "down" : "right", "chev");
      const head = h("button", { class: "dom-h", type: "button", "aria-expanded": String(open) }, chev,
        h("span", { class: "d-title" }, d.title, h("span", { class: "cnt" }, ` ${sk.length} of ${all.length} skills`)),
        h("span", { class: "w" }, d.weight), sbar(countStates(all), all.length));
      head.addEventListener("click", () => {
        const nowOpen = list.hidden;
        list.hidden = !nowOpen;
        head.setAttribute("aria-expanded", String(nowOpen));
        head.replaceChild(icon(nowOpen ? "down" : "right", "chev"), head.firstChild);
      });
      return [head, list];
    }));
    if (state.pickedSkill) {
      const el = body.querySelector(`.skill[data-sid="${state.pickedSkill}"]`);
      if (el) el.classList.add("selected");
    }
  };
  const chipFor = (key, label, n) => {
    const el = h("button", { class: filter === key ? "fchip on" : "fchip", type: "button" }, label, h("span", { class: "num" }, n));
    el.addEventListener("click", () => { filter = key; render(); for (const x of bar.querySelectorAll(".fchip")) x.classList.remove("on"); el.classList.add("on"); });
    return el;
  };
  const search = h("input", { type: "search", placeholder: "Find a skill", "aria-label": "Find a skill" });
  search.addEventListener("input", () => { query = search.value; render(); });
  const drifted = skills.filter((s) => s.drift).length;
  const bar = h("div", { class: "between filterbar" },
    h("div", { class: "chips" }, chipFor("", "All skills", skills.length),
      Object.entries(p.coverage.state_labels).filter(([k]) => counts[k]).map(([k, v]) => chipFor(k, v, counts[k] || 0)),
      drifted ? chipFor("drift", "Re-checking", drifted) : null),
    h("div", { class: "searchbox" }, icon("search", "sm"), search));
  render();
  if (!state.pickedSkill) {
    const withEvidence = skills.find((s) => s.state !== "untested") || skills[0];
    if (withEvidence) { state.pickedSkill = withEvidence.id; render(); }
  }
  if (state.pickedSkill) skillDetail(detail, state.pid, state.pickedSkill);
  const legend = h("div", { class: "piplegend" }, h("span", { class: "lead" }, "Four conditions:"),
    h("span", null, h("i", { class: "pip help" }), "with help"),
    h("span", null, h("i", { class: "pip own" }), "on your own"),
    h("span", null, h("i", { class: "pip own" }), "later"),
    h("span", null, h("i", { class: "pip" }), "in a new situation"),
    h("span", null, h("i", { class: "pip" }), "not tested"),
    h("span", null, h("span", { class: "notyet" }, "not yet"), "tried, not shown"),
    h("span", null, icon("eye", "xs"), "lesson seen"),
    drifted ? h("span", null, h("span", { class: "chip amber s-drift" }, icon("refresh", "xs"), "re-checking"), "a source page changed") : null);
  return [pageHead(`${p.meta.exam} · ${p.meta.title}`, `${p.coverage.skills} skills, word for word from the official study guide. ${NOT_A_PREDICTION}`,
      eyebrow("course", "Course · built from the official study guide"), helpQ("course-and-skills")),
    state.pid === "dp-800" ? panel(eyebrow("target", "Hands-on in Azure SQL"),
      h("p", { class: "sub" }, "Three small labs in a private, authorized live-write sandbox. Dojo checks the database, not a screenshot."),
      labsOn() ? h("div", { class: "actions" }, LAB_LINKS.map(([id, title]) => linkButton(title, `#/lab/${id}`, "ghost sm")))
        : h("p", { class: "small muted" }, LABS_OFF)) : null,
    bar,
    h("div", { class: "row stackable grow" }, h("section", { class: "panel grow stack" }, body, legend), detail)];
}

// ---------------------------------------------------------------- one skill

const SEEN_ICON = { read: "read", listen: "listen", present: "deck", watch: "video", podcast: "rss" };
const LAB_LINKS = [["vector", "Vector table"], ["search", "Nearest product"], ["hybrid", "Semantic product search"]];
const LAB_SKILLS = { "d3.g2.s3": "vector", "d3.g2.s7": "search", "d3.g2.s8": "hybrid" };
// /api/me says whether this Dojo has a lab database; without one, no page links to a lab.
const LABS_OFF = "Labs are not set up on this Dojo.";
const labsOn = () => !state.me || state.me.labs !== false;

function exposureRow(x) {
  return h("div", { class: "evc" },
    h("span", { class: "date" }, fmtWhen(x.at)),
    h("span", { class: "rail" }, h("span", { class: "node" })),
    h("div", { class: "evcard exposure" },
      h("div", { class: "ev-head" },
        h("span", { class: "t" }, icon(SEEN_ICON[x.modality] || "eye", "sm"), " ", h("b", null, x.modality === "podcast" && !x.declared ? "Podcast episode, recorded by an earlier version of Dojo"
          : x.modality === "listen" && x.narration === "deep" ? SEEN_DEEP : SEEN_AS[x.modality] || `Lesson viewed (${x.modality})`)),
        h("span", { class: "chip line" }, icon("eye", "xs"), x.declared ? "declared · not evidence" : "seen · not evidence")),
      x.declared ? h("p", { class: "small ink2" }, "You said so before answering. Dojo cannot see what your podcast app plays, or how much.") : null));
}

function attemptTimeline(e) {
  const attempts = e.attempts.slice().reverse().map((a) => {
    const node = a.disputed ? "up" : a.all_met ? (a.hints ? "help" : "own") : "";
    return { at: a.at, row: h("div", { class: "evc" },
      h("span", { class: "date" }, fmtWhen(a.at)),
      h("span", { class: "rail" }, h("span", { class: `node ${node}` })),
      h("div", { class: `evcard ${a.disputed ? "up" : ""}` },
        h("div", { class: "ev-head" },
          h("span", { class: "t" }, h("b", null, MODE_NAME[a.mode] || a.mode), ` · ${a.met} of ${a.total} points`),
          h("a", { class: "link small", href: `#/item/${a.item}` }, "Open")),
        h("div", { class: "chips" }, a.conditions.map((c) => chip(c, "line"))),
        a.changed_condition ? h("p", { class: "small ink2" }, "Changed condition: " + a.changed_condition) : null,
        a.source_changed ? h("p", { class: "conf" }, icon("refresh", "xs"), h("span", null, a.source_changed.notice)) : null,
        a.rejudged_at ? h("p", { class: "conf" }, icon("refresh", "xs"), h("span", null, `Judged again on ${fmtWhen(a.rejudged_at)} with newer judging rules, at your request.`)) : null,
        a.disputed ? h("p", { class: "dispute" }, icon("alert", "xs"), "You disputed this judgement, so it counts neither way.") : null)) };
  });
  const seen = (e.exposures || []).slice().reverse().map((x) => ({ at: x.at, row: exposureRow(x) }));
  const rows = attempts.concat(seen).sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0)).map((r) => r.row);
  if (!rows.length) return h("p", { class: "sub" }, "Nothing in the record yet.");
  return [h("div", { class: "tl2" }, rows),
    (e.exposures || []).some((x) => !x.declared) ? h("p", { class: "note top" }, "A lesson view is recorded when it starts, once per 30 minutes, or 45 for the longer Deeper Listen. A play can last that long, so time since teaching counts from the end of that window. A view is never evidence.") : null];
}

async function viewSkill(pid, sid) {
  const v = await api(`/api/skills/${pid}/${sid}`);
  const s = v.skill, e = v.evidence;
  const big = h("div", { class: "bigpips" }, pipKinds(e.state).map((k, i) => h("div", { class: "bp" },
    h("span", { class: `bd ${k || "empty"}` }, k ? icon("check", "xs bold") : null),
    h("b", null, CONDITIONS[i]),
    h("small", null, i === 3 ? "not in v1" : k ? "shown" : "not yet"))));
  const left = h("div", { class: "col grow" },
    h("section", { class: "panel" },
      h("div", { class: "panel-h" }, eyebrow("scale", "What the record shows"), chip(e.state_label, e.state === "untested" ? "" : "amber")),
      big,
      h("p", { class: "note top" }, "The fourth condition, in a new situation, is not part of this version: nothing is claimed about it."),
      h("div", { class: "divider wide" }),
      e.shown.map((x) => h("p", { class: "prow" }, icon("check", "xs"), x)),
      e.not_yet_shown.map((x) => h("p", { class: "prow" }, icon("x", "xs"), h("span", null, h("b", null, "Not yet: "), x))),
      e.disputed ? h("p", { class: "conf" }, icon("alert", "xs"), `${e.disputed} disputed judgement${e.disputed === 1 ? " is" : "s are"} not counted.`) : null,
      e.rehearsal_total ? h("p", { class: "conf" }, icon("practice", "xs"),
        `Practice exam: ${e.rehearsal_correct} of ${e.rehearsal_total} multiple-choice right. A right answer can be a guess, so it never fills a pip.`) : null),
    h("section", { class: "panel" }, h("div", { class: "panel-h" }, eyebrow("record", "Every attempt and lesson view"),
      h("span", { class: "small muted" }, `${plural(e.attempts.length, "attempt")} · ${plural((e.exposures || []).length, "view")}`)), attemptTimeline(e)));
  if (v.labs && v.labs.length) left.append(h("section", { class: "panel" }, eyebrow("target", "Database-checked practice"),
    h("p", { class: "small muted" }, "These lab checks show what was in the sandbox. They do not fill pips."),
    v.labs.slice().reverse().map((x) => h("p", { class: "prow" }, `${fmtWhen(x.at)} · ${x.lab}: ${x.passed ? "verified" : "not yet"} · ${x.hints_shown} hints · ${x.runs} runs`))));
  const taught = s.sources.length > 0;
  const right = h("div", { class: "col w430" },
    taught ? null : h("section", { class: "panel amber" }, eyebrow("alert", "Not taught yet", "amber"), h("p", { class: "sub" }, NO_SOURCE)),
    taught ? h("section", { class: "panel" }, h("div", { class: "panel-h" }, eyebrow("read", "Learn it"),
      v.lessons.length && isAdmin() ? button("Fresh lesson", () => rewriteLesson(v.lessons[0].id), "ghost sm") : null),
      driftNote(v.drift),
      v.lessons.length
        ? FORMATS.map((f) => h("a", { class: "fl", href: `#/lesson/${v.lessons[0].id}/${f.tab}` },
            h("span", { class: "ficon" }, icon(f.name, "sm")), h("b", null, f.label), h("span", { class: "fmeta" }, f.note)))
        : h("button", { class: "fl", type: "button", onclick: () => startLesson(pid, sid, false) },
            h("span", { class: "ficon" }, icon("wand", "sm")), h("b", null, "Write the lesson"), h("span", { class: "fmeta" }, "from the official sources")),
      v.lessons.length > 1 ? h("p", { class: "small muted" }, `${v.lessons.length} lessons written for this skill.`) : null,
      v.lessons[0] && v.lessons[0].blocked ? h("p", { class: "conf" }, icon("shield", "xs"),
        `${v.lessons[0].blocked} part${v.lessons[0].blocked === 1 ? " was" : "s were"} removed by the checks. `) : null) : null,
    taught ? h("section", { class: "panel" }, eyebrow("target", "Show what you can do"),
      h("div", { class: "actions top" },
        button("Probe, no help", () => act("probe", pid, sid), "ghost sm"),
        button("Practise with hints", () => act("practice", pid, sid), "ghost sm"),
        button("Check, no help", () => act("check", pid, sid), "sm")),
      h("p", { class: "note top" }, s.kind === "scenario"
        ? "A doing skill: every item is a scenario with one changed condition, so the same answer never works twice."
        : "An explaining skill: items ask you to explain it in your own words.")) : null,
    pid === "dp-800" && LAB_SKILLS[sid] ? h("section", { class: "panel" }, eyebrow("target", "Hands-on lab"),
      labsOn() ? h("p", { class: "sub" }, "Write SQL in your private sandbox. A database check is shown here as practice, not a pip.") : null,
      labsOn() ? linkButton("Open the lab", `#/lab/${LAB_SKILLS[sid]}`, "amber") : h("p", { class: "small muted" }, LABS_OFF)) : null,
    v.items.length ? h("section", { class: "panel" }, eyebrow("quiz", "Items"),
      v.items.slice().reverse().map((it) => h("a", { class: "near", href: `#/item/${it.id}` },
        h("b", null, it.stem), it.drift && it.drift.state === "withheld"
          ? h("span", { class: "chip rose", title: it.drift.notice }, "held back")
          : chip(it.result ? `${it.result.met}/${it.result.total}` : it.answered ? "judging" : MODE_NAME[it.mode], it.disputed ? "ochre" : "line")))) : null,
    v.asks.length ? h("section", { class: "panel" }, eyebrow("chat", "Your questions"),
      v.asks.map((a) => h("a", { class: "near", href: `#/answer/${a.id}` }, h("b", null, a.question), icon("right", "sm")))) : null,
    taught ? h("section", { class: "panel" }, h("div", { class: "panel-h" }, eyebrow("file", "Official sources"), chip(plural(s.sources.length, "page"), "line")),
      sourceList(s.sources),
      h("p", { class: "small muted top note" }, "Everything Dojo teaches for this skill is grounded in these pages and quoted word for word.")) : null);
  return [
    h("nav", { class: "crumb" }, h("a", { href: "#/skills" }, "Course"), h("span", { class: "sep" }, "›"), s.domain_title,
      h("span", { class: "sep" }, "›"), h("b", null, s.group)),
    h("div", { class: "pageh" }, h("h1", null, s.text),
      h("div", { class: "chips" }, chip(skillTag(s.id), "line"), chip(`weight ${s.domain_weight}`, "line"),
        chip(e.state_label, e.state === "untested" ? "" : "amber"), evidence({ state: e.state, disputed: e.disputed }),
        taught ? h("a", { class: "link small", href: `#/ask/${s.id}` }, "Ask about this skill") : null)),
    h("div", { class: "row stackable grow" }, left, right)];
}

// ---------------------------------------------------------------- lessons

// Every start is sent. The server keeps one event per lesson and way of viewing per 30 minutes, or 45 for
// the longer Deeper Listen, and notes which narration a listen played.
async function markViewed(id, modality, narration, retry = true) {
  try { await api(`/api/lessons/${id}/viewed`, { method: "POST", body: { modality, narration } }); }
  catch (e) { if (retry) setTimeout(() => markViewed(id, modality, narration, false), 5000); }
}

async function viewLesson(id, tab, section) {
  tab = tab || "read";
  const L = await api(`/api/lessons/${id}`);
  state.scrollTo = tab === "read" && section !== undefined ? `sec-${Number(section)}` : null;  // a link to one section
  if (tab === "read") markViewed(id, "read");
  const tabs = h("div", { class: "seg", role: "tablist" }, FORMATS.map((f) =>
    h("a", { href: `#/lesson/${id}/${f.tab}`, role: "tab", "aria-selected": String(f.tab === tab), class: f.tab === tab ? "s on" : "s" },
      icon(f.name, "sm"), f.label)));
  const head = [
    h("nav", { class: "crumb" }, h("a", { href: "#/skills" }, "Course"), h("span", { class: "sep" }, "›"),
      h("a", { href: `#/skill/${L.package}/${L.skill}` }, skillTag(L.skill)), h("span", { class: "sep" }, "›"), h("b", null, L.skill_text)),
    h("div", { class: "between top lesson-head" }, h("div", { class: "pageh grow" }, h("div", { class: "pageh-t" }, h("h1", null, L.title), helpQ("lessons")), h("p", { class: "sub" }, L.skill_text)), tabs),
    L.drift && L.drift.state === "recheck" ? driftNote(L.drift, tab) : null,
  ];
  const body = tab === "listen" ? lessonListen(L) : tab === "present" ? lessonPresent(L)
    : tab === "watch" ? lessonWatch(L) : lessonRead(L);
  return [head, body];
}

function provenance(L) {
  return [
    h("p", { class: "small muted" }, `Written by ${L.models.author} from the pages listed here and checked by ${L.models.gate}, a different model family. `,
      "Every quote was matched word for word against the page. The text around the quotes is an AI-written summary, not official wording. ",
      "Source content is licensed CC BY 4.0 by its publishers."),
    L.blocked.length
      ? h("p", { class: "small" }, `${L.blocked.length} part${L.blocked.length === 1 ? " was" : "s were"} removed because ${L.blocked.length === 1 ? "it" : "they"} did not hold. `,
          h("a", { class: "link", href: "#/quality" }, "The studio log lists them and why."))
      : h("p", { class: "small" }, "Nothing had to be removed."),
    h("p", { class: "small muted" }, "Points, misconceptions and self-check answers carry exact quotes. Titles, slide bullets and narration carry no quote; the independent model checked them against the same source pages."),
    h("p", { class: "small muted" }, `Lesson written ${fmtDate(L.created)}.`),
    L.drift && L.drift.state !== "recheck" ? driftNote(L.drift) : null,
  ];
}

function lessonRead(L) {
  const lost = lostQuote(L);
  const parts = [];
  if (L.summary) parts.push(h("section", { class: "panel" }, h("p", { class: "lead" }, L.summary)));
  L.sections.forEach((sec, i) => {
    parts.push(h("section", { class: "panel", id: `sec-${i}`, tabindex: "-1" }, h("h2", null, sec.heading),
      h("ul", { class: "points" }, sec.points.map((p) => {
        const [b, q] = quoteToggle(p, L.sources);
        return h("li", null, h("span", { class: "dotm" }), h("span", null, p.text, " ", b, lost(p) ? [" ", lost(p)] : null, q));
      }))));
  });
  if (L.misconceptions.length) {
    parts.push(h("section", { class: "panel" }, h("h2", null, "Watch out"), L.misconceptions.map((m) => {
      const [b, q] = quoteToggle(m, L.sources);
      return h("div", { class: "misc" }, h("p", { class: "wrong" }, h("strong", null, "Tempting but wrong: "), m.wrong),
        h("p", { class: "right" }, h("strong", null, "Instead: "), m.right, " ", b, lost(m) ? [" ", lost(m)] : null), q);
    })));
  }
  if (L.example && (L.example.situation || L.example.steps.length)) {
    parts.push(h("section", { class: "panel" }, h("h2", null, L.example.title || "Example"),
      L.example.situation ? h("p", null, L.example.situation) : null,
      L.example.steps.length ? h("ol", { class: "points" }, L.example.steps.map((st) => h("li", null, h("span", { class: "dotm" }), h("span", null, st)))) : null));
  }
  if (L.self_check.length) {
    parts.push(h("section", { class: "panel" }, h("h2", null, "Check yourself"),
      h("p", { class: "small muted" }, "Answer in your head first. Then say how sure you were, and see the answer. "
        + "This is for you: it is kept apart from your Record and is never evidence."),
      L.self_check.map((q, i) => selfCheck(L, q, i, lost))));
  }
  parts.push(h("section", { class: "panel" }, eyebrow("target", "Now show it"), h("div", { class: "actions top" },
    button("Practise with hints", () => act("practice", L.package, L.skill), "ghost"),
    button("Check, no help", () => act("check", L.package, L.skill)),
    linkButton("Watch it", `#/lesson/${L.id}/watch`, "ghost"),
    linkButton("Present it", `#/lesson/${L.id}/present`, "ghost"),
    linkButton("Listen to it", `#/lesson/${L.id}/listen`, "ghost"))));
  const side = h("div", { class: "col w430" },
    h("section", { class: "panel" }, eyebrow("file", "Where this comes from"), sourceList(L.sources), h("div", { class: "divider" }), provenance(L)));
  return h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" }, parts), side);
}

/* One "Check yourself" question: answer in your head, say how sure you were, see the answer, then say
   whether yours matched. The marks are the learner's own (ADR 0011): kept apart, never evidence. */
function selfCheck(L, q, i, lost) {
  const [b, qt] = quoteToggle(q, L.sources);
  const box = h("div", { class: "qa" }, h("p", { class: "qa-q" }, q.question));
  const sure = sureBox();
  const reveal = button("Show the answer", async () => {
    reveal.hidden = true;
    const confidence = await askSure(sure);
    const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
    const mark = (matched, btn) => async () => {
      for (const x of marks.querySelectorAll("button")) x.disabled = true;
      btn.classList.add("on");
      try {
        await api(`/api/lessons/${L.id}/selfcheck`, { method: "POST", body: { index: i, confidence, matched } });
        status.textContent = matched === "yes" ? "Kept. Well done." : "Kept. The points above are worth another look.";
      } catch (e) { status.textContent = e.message; for (const x of marks.querySelectorAll("button")) x.disabled = false; }
    };
    const marks = h("div", { class: "hs-btns" });
    for (const [v, label] of [["yes", "Yes"], ["partly", "Partly"], ["no", "Not yet"]]) {
      const btn = button(label, null, "ghost sm");
      btn.addEventListener("click", mark(v, btn));
      marks.append(btn);
    }
    box.append(h("p", { class: "qa-a" }, q.answer, " ", b, lost(q) ? [" ", lost(q)] : null), qt,
      h("div", { class: "hs" }, h("p", { class: "hs-q" }, h("b", null, "Did your answer match?")), marks), status);
  }, "ghost sm");
  box.append(h("div", { class: "actions" }, reveal), sure);
  return box;
}

async function ensureAudio(L, status) {
  if (L.audio.length && L.audio.every(Boolean)) return L.audio;
  const res = await runJob(api(`/api/lessons/${L.id}/audio`, { method: "POST" }), (s, secs) => { status.textContent = `${s} · ${secs}s`; });
  L.audio = res.audio;
  return res.audio;
}

// Listen plays the lesson's Deeper Listen once it is ready (ADR 0007): the whole lesson in the same two
// voices, one audio file per part, each part starting the next. Until then it plays the slide narration,
// and it always says which one plays.
const DEEP_WAIT = {
  missing: "The longer Deeper Listen of this lesson is not written yet. Dojo writes up to 20 a day in the background.",
  writing: "The longer Deeper Listen of this lesson is being written and checked now.",
  speaking: "The longer Deeper Listen of this lesson is written and checked. Its audio is being made.",
  failed: "The longer Deeper Listen of this lesson did not pass its checks, so Listen keeps the slide narration.",
  stopped: "The longer Deeper Listen of this lesson stopped before it was finished, so Listen keeps the slide narration.",
  invalidated: "The longer Deeper Listen of this lesson was withdrawn, because a newer lesson replaced this one.",
  held: "A changed source page no longer supports this lesson, so its Deeper Listen waits until the lesson is checked again.",
  upgrading: "This lesson is written again first, from every official page its skill cites, and the new lesson gets the Deeper Listen.",
};
const WORDS_PER_MINUTE = 150;
const spokenMinutes = (lines) => lines.reduce((n, ln) => n + String(ln.text).split(/\s+/).filter(Boolean).length, 0) / WORDS_PER_MINUTE;
const aboutMinutes = (m) => (m < 1.5 ? "about a minute" : `about ${Math.round(m)} minutes`);
const transcript = (lines) => h("div", { class: "transcript" }, lines.map((ln) => h("p", null, h("strong", null, `${presenterLabel(ln.voice)}: `), ln.text)));

// What the Deeper Listen that plays covers, from its parts: a part that was left out is not claimed.
function deepCovers(L) {
  const kinds = new Set(L.deep.parts.map((p) => p.kind));
  const sections = L.deep.parts.filter((p) => p.kind === "section").length;
  const all = (L.sections || []).length;
  const items = [sections ? (sections >= all ? "every section" : `${sections} of ${all} sections`) : "",
    kinds.has("example") ? "the worked example" : "", kinds.has("misconceptions") ? "the misconceptions" : "",
    kinds.has("traps") ? "the exam traps" : ""].filter(Boolean);
  if (!items.length) return "";
  const list = items.length === 1 ? items[0] : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
  return `${list[0].toUpperCase()}${list.slice(1)}. `;
}

function deepParts(L) {
  const D = L.deep;
  const boxes = [];
  const players = D.parts.map((p, i) => h("audio", {
    controls: true, preload: "none", src: `/api/media/${p.media}`, "aria-label": `Part ${i + 1} of ${D.parts.length}: ${p.title}`,
    onplay: () => { boxes.forEach((b, k) => b.classList.toggle("playing", k === i)); markViewed(L.id, "listen", "deep"); },
    onended: () => { boxes[i].classList.remove("playing"); if (players[i + 1]) players[i + 1].play().catch(() => {}); },
  }));
  D.parts.forEach((p, i) => boxes.push(h("div", { class: "listen-slide" }, h("h3", null, `${i + 1}. ${p.title}`), players[i], transcript(p.lines))));
  return boxes;
}

function lessonListen(L) {
  const status = h("p", { class: "small muted" });
  const list = h("div");
  const D = L.deep || { state: "missing" };
  const deep = D.state === "ready";
  const slides = () => L.slides.map((sl, i) => h("div", { class: "listen-slide" },
    h("h3", null, `${i + 1}. ${sl.title}`),
    L.audio[i] ? h("audio", { controls: true, preload: "none", src: `/api/media/${L.audio[i]}`, onplay: () => markViewed(L.id, "listen", "slides") }) : null,
    transcript(sl.narration)));
  const render = () => put(list, deep ? deepParts(L) : slides());
  render();
  const ready = deep || (L.audio.length && L.audio.every(Boolean));
  const prep = ready ? null : button("Prepare the audio", async () => {
    prep.disabled = true;
    try { await ensureAudio(L, status); status.textContent = "Ready."; prep.remove(); render(); }
    catch (e) { status.textContent = e.message; prep.disabled = false; }
  }, "amber");
  const playing = deep
    ? h("div", { class: "deep-now" }, chip("Deeper Listen", "amber"),
      h("p", null, h("b", null, `Playing: Deeper Listen${D.missing.length ? "" : ", the whole lesson"}, ${aboutMinutes(D.minutes)}. `),
        deepCovers(L), "The parts play one after another. The self-check stays in Dojo."))
    : h("div", { class: "deep-now" }, chip("Slide narration", "line"),
      h("p", null, h("b", null, `Playing: the slide narration, ${aboutMinutes(spokenMinutes(L.slides.flatMap((sl) => sl.narration)))}. `),
        DEEP_WAIT[D.stopped ? "stopped" : D.state] || DEEP_WAIT.missing, " Read has the whole lesson."));
  const left = deep && D.missing.length
    ? h("p", { class: "small top" }, `Left out because ${D.missing.length === 1 ? "it" : "they"} did not pass the checks: ${D.missing.join(", ")}. Read covers ${D.missing.length === 1 ? "it" : "them"}.`) : null;
  const made = deep
    ? h("p", { class: "small muted" }, `Deeper Listen was written by ${D.models.author} from this lesson and its sources, not from new facts, and every line was checked by ${D.models.gate}. `,
      `${Math.round(D.kept_share * 100)}% of what was written held. Written ${fmtDate(D.created)}.`) : null;
  return h("div", { class: "row stackable grow" },
    h("div", { class: "col grow article" },
      h("section", { class: "panel" },
        h("div", { class: "panel-h" }, eyebrow("listen", "Listen"), prep),
        h("p", { class: "sub" }, `Two voices talk the lesson through: ${PRESENTER.guide.role} ${PRESENTER.guide.name} explains, `
          + `${PRESENTER.coach.role.toLowerCase()} ${PRESENTER.coach.name} asks what a learner would ask.`),
        playing, left, status, list)),
    h("div", { class: "col w430" },
      podcastPanel(),
      h("section", { class: "panel" }, eyebrow("file", "Where this comes from"), sourceList(L.sources), h("div", { class: "divider" }), made, provenance(L))));
}

// ---------------------------------------------------------------- the podcast feed

// The link carries its own secret. Dojo shows it once, when it is made, and keeps only a fingerprint
// of it, so it lives in this page's memory until the page is closed. It never goes into the address bar.
const PODCAST_NOTES = [
  ["target", "Listening counts as seen, not as shown. Check each skill in Dojo afterwards."],
  ["eye", "Dojo cannot see what your podcast app downloads or plays. It notes only which lessons the feed has offered."],
  ["clock", "So before an answer that could count as ‘later’, Dojo asks whether you listened since the last teaching."],
  ["practice", "The feed pauses while a timed practice exam runs, and the exam asks at the end whether you listened during it."],
  ["lock", "Your podcast app, and possibly its servers, keep copies Dojo cannot delete. Making a new link stops new downloads."],
];

function podcastPanel(label = "On your phone", iconName = "phone") {
  const box = h("section", { class: "panel podcast" });
  let st = null, ask = null, note = "";
  const reload = async () => {
    try { st = await api("/api/feed"); } catch (e) { note = e.message; }
    render();
  };
  const run = async (fn) => {
    try { await fn(); note = ""; } catch (e) { note = e.message; }
    ask = null;
    await reload();
  };
  const make = (path) => run(async () => { state.feedLink = await api(path, { method: "POST" }); });
  const off = () => run(async () => { await api("/api/feed/link", { method: "DELETE" }); state.feedLink = null; });
  const confirmRow = (question, yes, onYes) => h("div", { class: "pod-confirm", role: "group", "aria-label": yes },
    h("p", { class: "small" }, question),
    h("div", { class: "actions" }, button(yes, onYes, "sm"), button("Cancel", () => { ask = null; render(); }, "ghost sm")));
  const manage = () => ask === "new" ? confirmRow("Apps using the old link stop updating.", "Make a new link", () => make("/api/feed/link/new"))
    : ask === "off" ? confirmRow("Apps using this link stop updating. Episodes already on your phone can still play, so Dojo keeps asking whether you listened.", "Turn the feed off", off)
    : h("div", { class: "actions top" }, button("Make a new link", () => { ask = "new"; render(); }, "ghost sm"),
      button("Turn the feed off", () => { ask = "off"; render(); }, "ghost sm"));
  const render = () => {
    const head = eyebrow(iconName, label);
    if (!st) { put(box, head, h("p", { class: "sub" }, note || "Loading…")); return; }
    const link = st.active && state.feedLink && state.feedLink.created === st.created ? state.feedLink : null;
    if (!link) state.feedLink = null;
    const feed = link ? link.feeds.find((f) => f.package === state.pid) || link.feeds[0] : null;
    const count = feed || st.packages.find((p) => p.package === state.pid) || st.packages[0];
    const ready = count ? h("p", { class: "small muted top" }, `${count.exam}: ${count.ready} of ${plural(count.total, "episode")} ready. `,
      count.ready < count.total ? "The rest appear when their narration is made." : "") : null;
    const said = note ? h("p", { class: "small", role: "alert" }, note) : null;
    const notes = h("ul", { class: "pod-notes" }, PODCAST_NOTES.map(([name, text]) => h("li", null, icon(name, "sm"), h("span", null, text))));
    if (!st.active) {
      put(box, head, h("h3", { class: "pod-h" }, "Listen in your podcast app"),
        h("p", { class: "sub" }, "Every lesson of this exam as a private podcast, one episode per skill in study-guide order."),
        h("p", { class: "sub" }, "Your podcast app fetches new episodes by itself. Anyone with the link can listen, so keep it to yourself."),
        st.on_phone ? h("p", { class: "small top" }, "Your feed is off. Episodes already on your phone can still play, so Dojo keeps asking whether you listened.") : null,
        ready, said, h("div", { class: "actions top" }, button("Get my podcast link", () => make("/api/feed/link"), "amber")), notes);
      return;
    }
    if (!feed) {
      put(box, head, h("h3", { class: "pod-h" }, "Your podcast link is on"),
        h("p", { class: "sub" }, `You made it on ${fmtWhen(st.created)}. Dojo keeps only a fingerprint of it, so it cannot show it again.`),
        h("p", { class: "sub" }, "To add Dojo to another app or phone, make a new link."), ready, said, manage(), notes);
      return;
    }
    const url = h("input", { type: "text", readonly: true, value: feed.url, "aria-label": `Podcast link for ${feed.exam}`, spellcheck: "false" });
    url.addEventListener("focus", () => url.select());
    const copied = h("span", { class: "small muted", role: "status" });
    const copy = button("Copy", async () => {
      try { await navigator.clipboard.writeText(feed.url); copied.textContent = "Copied."; }
      catch (e) { url.focus(); url.select(); copied.textContent = "Select the link and copy it."; }
    }, "sm");
    put(box, head, h("h3", { class: "pod-h" }, `Your podcast link for ${feed.exam}`),
      h("div", { class: "pod-url" }, url, copy), copied,
      h("img", { class: "pod-qr", src: feed.qr, width: 200, height: 200, alt: `QR code of the podcast link for ${feed.exam}` }),
      h("p", { class: "sub" }, "In your podcast app, choose Add a show by URL, or scan the code."),
      h("p", { class: "small muted" }, "Dojo shows this link only until you leave this page. Each exam has its own link; switch exams to see the other one. A new link replaces both."),
      ready, said, manage(), notes);
  };
  render();
  reload();
  return box;
}

// ---------------------------------------------------------------- Dojo as an app, and nudges (ADR 0013)

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();  // Dojo offers the install on the phone page, not as a banner
  installPrompt = e;
  window.dispatchEvent(new Event("dojo-install"));
});
window.addEventListener("appinstalled", () => { installPrompt = null; window.dispatchEvent(new Event("dojo-install")); });

const standalone = () => (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;
const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const pushOk = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;

// The worker caches nothing: it only shows nudges and opens Dojo when one is tapped.
function registerWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register(`/static/sw.js?v=${encodeURIComponent(loadedBuild() || "0")}`, { scope: "/" })
    .catch(() => { /* Dojo works without it; only nudges need it */ });
}

function deviceLabel() {
  const ua = navigator.userAgent;
  const browser = /Edg(A|iOS)?\//.test(ua) ? "Edge" : /Firefox\/|FxiOS/.test(ua) ? "Firefox" : /Chrome\/|CriOS/.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "A browser";
  const os = /iPhone/.test(ua) ? "iPhone" : (/iPad/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) ? "iPad"
    : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser} on ${os}` : browser;
}
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));

// The same id the server gives a device: "dev-" and the first 20 hex digits of SHA-256("dojo-push:" + endpoint).
async function pushDeviceId(endpoint) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`dojo-push:${endpoint}`));
  return `dev-${Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("").slice(0, 20)}`;
}

async function pushSubscription() {
  if (!pushOk()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

const workerReady = (ms = 10000) => Promise.race([navigator.serviceWorker.ready,
  new Promise((_, no) => setTimeout(() => no(new Error("The browser did not start Dojo's worker. Reload the page and try again.")), ms))]);

const NUDGE_NOTES = [
  ["bell", "At most one nudge a day, and only when something is due: recalls, or a session in your Plan."],
  ["clock", "It comes within two hours after the time you choose, Berlin time. Never during a practice exam."],
  ["eye", "The lock screen shows only how many recalls and about how long. No skill names, no answers."],
  ["hand", "Missing a day costs nothing. There are no streaks to keep."],
  ["lock", "For each device Dojo keeps the address its push service gave and a name like ‘Edge on Windows’. Turning nudges off deletes them."],
];

function nudgePanel() {
  const box = h("section", { class: "panel nudges", "aria-labelledby": "nudge-h" });
  const status = h("p", { class: "small", role: "status", "aria-live": "polite" });
  let st = null, here = null, busy = false, note = "";
  const reload = async () => {
    try {
      st = await api("/api/push");
      const sub = await pushSubscription().catch(() => null);
      const id = sub ? await pushDeviceId(sub.endpoint) : null;
      here = id && st.devices.some((d) => d.id === id) ? id : null;
    } catch (e) { note = e.message; }
    render();
  };
  const run = (fn) => async () => {
    if (busy) return;
    busy = true;
    note = "";
    try { await fn(); } catch (e) { note = e.message || "That did not work."; }
    busy = false;
    await reload();
  };
  const turnOn = async () => {
    // Asked first, straight from the tap: Safari allows the question only then.
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") { note = "Nudges stay off: the browser was not allowed to show them."; return; }
    const reg = await workerReady();
    let sub = await reg.pushManager.getSubscription();
    const old = sub && sub.options && sub.options.applicationServerKey;
    if (sub && (!old || b64u(old) !== st.public_key)) { await sub.unsubscribe(); sub = null; }
    if (!sub) {
      try { sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: fromB64u(st.public_key) }); }
      catch (e) { throw new Error("The browser's push service did not set this device up, so nudges stay off here. Try again later, or in another browser."); }
    }
    const keys = sub.toJSON().keys || {};
    await api("/api/push/devices", { method: "POST", body: { endpoint: sub.endpoint, p256dh: keys.p256dh, auth: keys.auth, label: deviceLabel() } });
    note = "Turned on.";
  };
  const offHere = async () => {
    const sub = await pushSubscription().catch(() => null);
    const id = sub ? await pushDeviceId(sub.endpoint) : here;
    if (sub) await sub.unsubscribe().catch(() => false);
    if (id) await api(`/api/push/devices/${id}`, { method: "DELETE" });
    note = "Turned off on this device.";
  };
  const offAll = async () => {
    const sub = await pushSubscription().catch(() => null);
    if (sub) await sub.unsubscribe().catch(() => false);
    await api("/api/push", { method: "DELETE" });
    note = "Nudges are off on every device, and your times are deleted.";
  };
  const test = async () => {
    const r = await api("/api/push/test", { method: "POST", body: { device: here } });
    note = r.sent ? "Sent. It should arrive in a few seconds."
      : r.removed ? "The push service no longer knows this device. Turn nudges on again."
      : "The push service did not take it. Try again later.";
  };
  const render = () => {
    const head = h("div", { class: "panel-h" }, eyebrow("bell", "Nudges"), st && st.on ? chip("on", "line") : null);
    if (!st) { put(box, head, h("p", { class: "sub" }, note || "Loading…")); return; }
    const time = h("input", { type: "time", id: "nudge-time", value: st.time, step: 300 });
    const boxes = PLAN_DAYS.map((d, i) => h("input", { type: "checkbox", checked: st.days.includes(i), "aria-label": PLAN_WEEKDAYS[(i + 1) % 7] }));
    const pills = boxes.map((b, i) => {
      const pill = h("label", { class: b.checked ? "dpill on" : "dpill" }, b, PLAN_DAYS[i]);
      b.addEventListener("change", () => pill.classList.toggle("on", b.checked));
      return pill;
    });
    const save = run(async () => {
      const days = boxes.map((b, i) => (b.checked ? i : -1)).filter((i) => i >= 0);
      if (!/^([01][0-9]|2[0-3]):[0-5][05]$/.test(time.value)) { note = "Give a time in steps of 5 minutes, like 08:00 or 18:35."; return; }
      if (!days.length) { note = "Pick at least one day."; return; }
      await api("/api/push/settings", { method: "PUT", body: { time: time.value, days } });
      note = "Saved.";
    });
    const when = h("fieldset", { class: "nudge-when" }, h("legend", { class: "eyebrow" }, "When"),
      h("div", { class: "nudge-row" }, h("label", { class: "field" }, "From", time),
        h("div", { class: "dpills", role: "group", "aria-label": "Days" }, pills)),
      h("div", { class: "actions top" }, button("Save", save, "ghost sm")));
    let device;
    if (!pushOk()) {
      device = h("p", { class: "sub" }, isIOS() && !standalone()
        ? "On iPhone and iPad, nudges work once Dojo is on your Home Screen. The steps are on this page."
        : "This browser cannot show nudges. Dojo works the same without them.");
    } else if (Notification.permission === "denied" && !here) {
      device = h("p", { class: "sub" }, "This browser blocks notifications from Dojo. To get nudges, allow them for this site in the browser's settings, then come back.");
    } else if (here) {
      device = [h("p", { class: "sub top" }, "Nudges are on for this device."),
        h("div", { class: "actions top" }, button("Send a test nudge", run(test), "ghost sm"), button("Turn off on this device", run(offHere), "ghost sm"))];
    } else {
      device = [h("p", { class: "sub top" }, "Nudges are off for this device."),
        h("div", { class: "actions top" }, button("Turn on nudges on this device", run(turnOn), "amber"))];
    }
    const list = st.devices.length ? h("ul", { class: "nudge-devs", "aria-label": "Devices with nudges on" }, st.devices.map((d) => h("li", null,
      h("span", null, h("b", null, d.label), h("small", null, [d.id === here ? "This device" : null, `on since ${fmtWhen(d.added)}`,
        d.last_ok ? `last nudge ${fmtWhen(d.last_ok)}` : null].filter(Boolean).join(" · "))),
      d.id === here ? null : button("Turn off", run(() => api(`/api/push/devices/${d.id}`, { method: "DELETE" })), "ghost sm")))) : null;
    put(box, head, h("h3", { class: "pod-h", id: "nudge-h" }, "A nudge when something is due"),
      h("p", { class: "sub" }, "A short note on your phone, like “2 recalls are ready, about 4 minutes”. Tap it and the 5-minute session opens."),
      when, device, list,
      st.devices.length ? h("div", { class: "actions top" }, button("Turn off everywhere", run(offAll), "ghost sm")) : null,
      status, h("ul", { class: "pod-notes" }, NUDGE_NOTES.map(([name, text]) => h("li", null, icon(name, "sm"), h("span", null, text)))));
    status.textContent = note;
    for (const b of box.querySelectorAll("button")) b.disabled = busy;
  };
  render();
  reload();
  return box;
}

function appPanel() {
  const box = h("section", { class: "panel" });
  const render = () => {
    if (!box.isConnected && box.childNodes.length) return;
    const head = eyebrow("phone", "Dojo as an app");
    if (standalone()) {
      put(box, head, h("p", { class: "sub" }, "You are using Dojo as an app. It opens from its icon, in its own window, without the browser bar."));
      return;
    }
    const how = installPrompt
      ? h("div", { class: "actions top" }, button("Install Dojo", async () => {
        const p = installPrompt;
        installPrompt = null;
        try { await p.prompt(); await p.userChoice; } catch (e) { /* the browser said no: nothing changes */ }
        render();
      }, "amber"))
      : isIOS() ? h("p", { class: "small" }, "On iPhone and iPad, follow the steps under iPhone and iPad.")
      : h("p", { class: "small" }, "In Chrome or Edge, open the browser menu and choose Install Dojo, or Add to Home screen on Android.");
    put(box, head, h("h3", { class: "pod-h" }, "Put Dojo on your Home Screen"),
      h("p", { class: "sub" }, "Dojo then opens from its own icon, in its own window. It is the same Dojo: nothing else is downloaded, and it needs the internet as before."),
      how,
      h("p", { class: "small muted top" }, "Nothing private is kept on the device: no lessons, answers or record are stored for offline use."));
  };
  window.addEventListener("dojo-install", render);
  render();
  return box;
}

function iosPanel() {
  return h("section", { class: "panel" }, eyebrow("phone", "iPhone and iPad"),
    h("p", { class: "sub" }, "Apple lets a web app send nudges only once it is on the Home Screen, from iOS 16.4."),
    h("ol", { class: "calsteps" },
      h("li", null, "Open Dojo in Safari."),
      h("li", null, "Tap Share, then Add to Home Screen, then Add."),
      h("li", null, "Open Dojo from the new icon and sign in."),
      h("li", null, "Open On your phone and tap Turn on nudges on this device."),
      h("li", null, "Tap Allow when your iPhone asks.")),
    h("p", { class: "small muted top" }, "To switch them off: here, or in Settings, Notifications, Dojo."));
}

function viewPhone() {
  return [pageHead("On your phone", "Your lessons in any podcast app, a calm nudge when recalls are due, and Dojo itself on your Home Screen.", null, helpQ("on-your-phone")),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow reading" }, podcastPanel("Your private podcast", "rss"), nudgePanel()),
      h("div", { class: "col w430" },
        appPanel(),
        iosPanel(),
        h("section", { class: "panel" }, eyebrow("plan", "Your Plan in your calendar"),
          h("p", { class: "sub" }, "The Plan has a private calendar link of its own. Outlook shows each session as an event with a reminder."),
          h("div", { class: "actions top" }, linkButton("Open the Plan", "#/plan", "ghost sm"))),
        h("section", { class: "panel" }, eyebrow("lock", "What the link opens"),
          h("p", { class: "sub" }, "Podcast apps cannot sign in, so the link itself is the key: 43 random characters. Dojo keeps only a fingerprint of it."),
          h("p", { class: "sub" }, "It opens the episodes of your exams and nothing else: the lesson audio, the slide titles and the sources. Nothing from your record: no name, no answers, no evidence.")),
        h("section", { class: "panel" }, eyebrow("listen", "The episodes"),
          h("p", { class: "sub" }, "One per skill, from its latest lesson, in study-guide order. The voices are AI-generated. A lesson becomes an episode once every slide has its narration."))))];
}

// ---------------------------------------------------------------- present: the deck (ADR 0014)
// Present plays a deck of scenes that the server composes from the checked lesson (app/deck.py): a title,
// what the lesson covers, a divider before each part, one scene per slide whose bullets and diagram parts
// build as the narration reaches them, the worked example, the common traps, a recap, the self-check and a
// closing scene. The narration is the lesson's own audio, one file per slide; without it a clock runs the
// same cues. Each diagram is drawn here as SVG from its checked spec, so it shows no text the gate did not
// check. Everything between "deck: pure begin" and "deck: pure end" is plain computation with no page, so
// tests/test_deck.py runs it in node.

// deck: pure begin
const DECK_WIDE = { w: 1280, h: 720 };   // the stage in canvas pixels, scaled to fit the page
const DECK_TALL = { w: 440, h: 720 };    // the tall card on a phone
const DECK_CPS = 14;      // characters a second when no audio sets the pace
const DECK_GAP = 350;     // ms between two lines on that clock
const DECK_HOLD = 1400;   // ms a slide stays up after its narration
const DECK_FONTS = {
  label: (s) => `600 ${s}px Inter`,
  note: (s) => `400 ${s}px Inter`,
  bold: (s) => `700 ${s}px Inter`,
  serif: (s) => `600 ${s}px Fraunces`,
  serifi: (s) => `italic 400 ${s}px Fraunces`,
  mono: (s) => `400 ${s}px "JetBrains Mono"`,
};
const DECK_LH = { label: 1.24, note: 1.38, bold: 1.22, serif: 1.12, serifi: 1.42, mono: 1.6 };
// The sizes each kind of diagram text steps down through, largest first.
const DECK_SIZES = {
  wide: { label: [24, 23, 22, 21, 20, 19, 18], note: [19, 18, 17, 16], head: [21, 20, 19, 18, 17, 16] },
  tall: { label: [22, 21, 20, 19, 18], note: [18, 17, 16], head: [19, 18, 17, 16] },
};

// Lines of at most `width`, breaking between words. No word is ever dropped: one word wider than the box
// is broken inside, so nothing runs out of it.
function deckWrap(text, width, size, font, M) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const out = [];
  let cur = "";
  for (const w of words) {
    const next = cur ? `${cur} ${w}` : w;
    if (cur && M(next, size, font) > width) { out.push(cur); cur = w; } else cur = next;
  }
  if (cur) out.push(cur);
  return out.flatMap((ln) => {
    if (ln.includes(" ") || M(ln, size, font) <= width) return [ln];
    const bits = [];
    let piece = "";
    for (const ch of ln) {
      if (piece && M(piece + ch, size, font) > width) { bits.push(piece); piece = ch; } else piece += ch;
    }
    return piece ? bits.concat(piece) : bits;
  });
}

// The largest size at which a text takes at most `most` lines; at the smallest size it takes the lines it needs.
function deckFit(text, width, sizes, font, M, most) {
  let f = null;
  for (const size of sizes) {
    const lines = deckWrap(text, width, size, font, M);
    const lh = Math.round(size * DECK_LH[font]);
    f = { lines, size, lh, h: lines.length * lh, w: lines.reduce((a, l) => Math.max(a, M(l, size, font)), 0), ok: lines.length <= most };
    if (f.ok) return f;
  }
  return f;
}

// One size for a set of texts, so the cards of one diagram look alike. `width` may be one per text.
function deckFitAll(texts, width, sizes, font, M, most) {
  let fits = [];
  for (const size of sizes) {
    fits = texts.map((t, i) => deckFit(t, Array.isArray(width) ? width[i] : width, [size], font, M, most));
    if (fits.every((f) => f.ok)) return fits;
  }
  return fits;
}

// ---- timing
const deckRead = (chars, lo, hi) => Math.max(lo, Math.min(hi, 1100 + 48 * chars));

// When each narration line starts and ends: shared out by length over the audio, else on a clock.
function deckTimes(texts, dur) {
  if (!texts.length) return [];
  let at = 0;
  if (dur > 0) {
    const w = texts.map((x) => String(x).length + 12), sum = w.reduce((a, b) => a + b, 0);
    return w.map((x, i) => { const t0 = at; at = i === w.length - 1 ? dur : at + (dur * x) / sum; return { t0, t1: at }; });
  }
  return texts.map((x) => {
    const t0 = at, t1 = at + Math.max(900, (1000 * String(x).length) / DECK_CPS);
    at = t1 + DECK_GAP;
    return { t0, t1 };
  });
}

// When each part of a scene appears (`at`) and stops being the one in focus (`till`), in ms from the start
// of the scene, and when the scene is over. A slide follows its narration; the other scenes hold long
// enough to read; the self-check and the closing scene wait for the learner.
function deckPlan(sc, texts, dur) {
  const marks = {}, lines = sc.kind === "slide" ? deckTimes(texts, dur) : [];
  const seq = (keys, from, each) => {
    let at = from;
    keys.forEach((k, j) => { marks[k] = { at, till: at + each[j] }; at += each[j]; });
    return at;
  };
  const span = (xs, lo, hi) => xs.map((x) => deckRead(String(x).length, lo, hi));
  let end = 4000;
  if (sc.kind === "slide") {
    const cues = sc.cues || {}, nv = (cues.visual || []).length;
    if (!lines.length) {
      const keys = sc.bullets.map((_, j) => `b${j}`).concat(Array.from({ length: nv }, (_, j) => `v${j}`), sc.tip ? ["tip"] : []);
      for (const k of keys) marks[k] = { at: 400, till: 400 };
      end = deckRead(sc.title.length + sc.bullets.join(" ").length + (sc.tip ? sc.tip.text.length : 0), 3000, 9000);
    } else {
      const last = lines.length - 1;
      const line = (c, j, n) => (Number.isInteger(c) && c >= 0 ? Math.min(c, last) : Math.min(last, Math.floor((j * lines.length) / Math.max(1, n))));
      // Parts brought in by the same line come in one after another within its first four fifths.
      const spread = (prefix, cs, n) => {
        const groups = new Map();
        for (let j = 0; j < n; j++) {
          const li = line(cs[j], j, n);
          if (!groups.has(li)) groups.set(li, []);
          groups.get(li).push(j);
        }
        for (const [li, js] of groups) {
          const { t0, t1 } = lines[li], step = (0.8 * (t1 - t0)) / js.length;
          js.forEach((j, q) => { marks[`${prefix}${j}`] = { at: t0 + step * q, till: q + 1 < js.length ? t0 + step * (q + 1) : t1 }; });
        }
      };
      spread("b", cues.bullets || [], sc.bullets.length);
      spread("v", cues.visual || [], nv);
      if (sc.tip) {
        const li = Number.isInteger(cues.tip) && cues.tip >= 0 ? Math.min(cues.tip, last) : last;
        marks.tip = { at: lines[li].t0, till: lines[li].t1 };
      }
      end = lines[last].t1 + DECK_HOLD;
    }
  } else if (sc.kind === "title") {
    marks.s = { at: 500, till: 500 };
    marks.m = { at: 1300, till: 1300 };
    end = deckRead(`${sc.title} ${sc.summary || ""}`.length, 5000, 9000);
  } else if (sc.kind === "agenda") {
    let at;
    if (sc.outcomes) {
      at = seq(sc.items.map((_, j) => `a${j}`), 700, span(sc.items, 1500, 4500));
      marks.map = { at, till: at + 2600 };
      at += 2600;
    } else {
      at = seq(sc.parts.map((_, j) => `p${j}`), 700, span(sc.parts.map((p) => `${p.title} ${p.items.join(" ")}`), 1500, 4500));
    }
    end = at + 1200;
  } else if (sc.kind === "part") {
    marks.i = { at: 900, till: 900 };
    end = Math.min(7000, 4200 + 350 * (sc.items || []).length);
  } else if (sc.kind === "process") {
    let at = 400;
    if (sc.situation) { const d = deckRead(sc.situation.length, 2000, 8000); marks.s = { at, till: at + d }; at += d; }
    end = seq(sc.steps.map((_, j) => `s${j}`), at, span(sc.steps, 1800, 7000)) + 1200;
  } else if (sc.kind === "trap") {
    const w = deckRead(sc.wrong.length, 2600, 7000), r = deckRead(sc.right.length + (sc.quote || "").length / 2, 3000, 9000);
    marks.w = { at: 300, till: 300 + w };
    marks.r = { at: 300 + w, till: 300 + w + r };
    end = 300 + w + r + 600;
  } else if (sc.kind === "recap") {
    end = seq(sc.items.map((_, j) => `r${j}`), 600, span(sc.items, 1800, 6000)) + 1200;
  } else if (sc.kind === "check" || sc.kind === "end") {
    end = Infinity;
  }
  return { end, lines, marks };
}

// The line being spoken at `t`, kept up through the short pause after it; -1 when none is.
function deckLineAt(lines, t) {
  for (let i = lines.length - 1; i >= 0; i--) if (t >= lines[i].t0) return t <= lines[i].t1 + DECK_GAP + 400 ? i : -1;
  return -1;
}

function deckSceneTitle(sc) {
  if (sc.kind === "part") return `Part ${sc.n}: ${sc.title}`;
  if (sc.kind === "trap") return sc.of > 1 ? `Common trap ${sc.n} of ${sc.of}` : "Common trap";
  return sc.title || "";
}

// The sections of the progress bar and the overview: the opening scenes, then each part.
function deckGroups(D) {
  const S = D.scenes, parts = D.parts || [];
  const first = parts.length ? parts[0].first : S.length;
  const out = first > 0 ? [{ label: "Start", role: "intro", from: 0, to: first }] : [];
  parts.forEach((p, i) => out.push({ label: p.title, role: p.role, from: p.first, to: i + 1 < parts.length ? parts[i + 1].first : S.length }));
  return out;
}

// A diagram sits beside the slide's bullets only when it is narrow enough; otherwise it takes the stage and
// the bullets are there for screen readers.
const deckRail = (v) => (v.type === "flow" ? v.steps.length <= 3 : v.type === "timeline" ? v.events.length <= 3 : v.type === "stack" || v.type === "keynum");

// ---- the diagrams. Each layout returns { h, base, parts, under, back } in its own pixels: `base` is drawn
// first and stays, `parts[j]` builds with the j-th cue, `under[j]` (code) is drawn beneath the text and
// over `back` (the code panel).
const n1 = (v) => Math.round(v * 10) / 10;
const dvR = (x, y, w, h, c, r) => ({ k: "rect", x: n1(x), y: n1(y), w: n1(w), h: n1(h), r: r === undefined ? 14 : r, c });
const dvC = (cx, cy, r, c) => ({ k: "circle", cx: n1(cx), cy: n1(cy), r: n1(r), c });
const dvP = (d, c, draw) => ({ k: "path", d, c, draw: !!draw });
const dvT = (x, y, fit, f, c, a) => ({ k: "text", x: n1(x), y: n1(y), lines: fit.lines, size: fit.size, lh: fit.lh, w: fit.w, f, c: c || "", a: a || "start" });
const dvI = (x, y, s, name, c) => ({ k: "icon", x: n1(x), y: n1(y), s, name, c: c || "" });
const dvLine = (text, size, font, M) => ({ lines: [String(text)], size, lh: Math.round(size * DECK_LH[font]), h: Math.round(size * DECK_LH[font]), w: M(String(text), size, font) });
// An arrowhead whose tip is at (x, y), pointing along `ang` (radians).
function dvHead(x, y, ang, len, c) {
  const l = len || 11, a1 = ang + Math.PI - 0.5, a2 = ang + Math.PI + 0.5;
  return dvP(`M${n1(x + l * Math.cos(a1))} ${n1(y + l * Math.sin(a1))}L${n1(x)} ${n1(y)}L${n1(x + l * Math.cos(a2))} ${n1(y + l * Math.sin(a2))}`, c || "dkv-arr dkv-head");
}
function dvBadge(cx, cy, r, num, M, size) {
  const f = dvLine(num, size || Math.round(r * 0.95), "bold", M);
  return [dvC(cx, cy, r, "dkv-badge"), dvT(cx, cy - f.lh / 2, f, "bold", "dkv-on", "middle")];
}
// A box with only its left or only its top corners rounded.
const dvLeftRound = (x, y, w, h, r) => `M${n1(x + w)} ${n1(y)}H${n1(x + r)}Q${n1(x)} ${n1(y)} ${n1(x)} ${n1(y + r)}V${n1(y + h - r)}Q${n1(x)} ${n1(y + h)} ${n1(x + r)} ${n1(y + h)}H${n1(x + w)}Z`;
const dvTopRound = (x, y, w, h, r) => `M${n1(x)} ${n1(y + h)}V${n1(y + r)}Q${n1(x)} ${n1(y)} ${n1(x + r)} ${n1(y)}H${n1(x + w - r)}Q${n1(x + w)} ${n1(y)} ${n1(x + w)} ${n1(y + r)}V${n1(y + h)}Z`;
const dvMax = (fits) => fits.reduce((a, f) => Math.max(a, f.h), 0);
const dvNote = (f) => f.lines.length > 0;
// The sizes a text steps down through at the level the diagram is laid out at: each level drops the largest
// size left, so a crowded diagram gets smaller text before it is scaled down (deckScale).
const dvZ = (X, xs) => xs.slice(Math.min(X.lvl || 0, xs.length - 1));

// flow: steps in order, left to right (two rows when they would be too narrow); a column when tall.
function dvFlow(v, W, H, tall, X) {
  if (tall) return dvColumn(v.steps, W, X, false);
  const st = v.steps, n = st.length, S = X.S, pad = 20, gap = 56;
  let per = n, rows = 1, cw = (W - (n - 1) * gap) / n;
  if (cw < 180 && n > 3) { per = Math.ceil(n / 2); rows = 2; cw = (W - (per - 1) * gap) / per; }
  // two rows get a smaller badge row, so both still fit the stage
  const br = rows > 1 ? 18 : 22, top = 2 * br + 16, rowGap = rows > 1 ? 52 : 0, ic = rows > 1 ? 28 : 32;
  const lab = deckFitAll(st.map((s) => s.label), cw - 2 * pad, dvZ(X, S.label), "label", X.M, 3);
  const notes = deckFitAll(st.map((s) => s.note), cw - 2 * pad, dvZ(X, S.note), "note", X.M, 4);
  const lh = dvMax(lab), nh = dvMax(notes);
  const ch = pad + top + lh + (nh ? 10 + nh : 0) + pad;
  const parts = st.map((s, i) => {
    const col = i % per, x = col * (cw + gap), y = Math.floor(i / per) * (ch + rowGap);
    const p = [];
    if (i > 0 && col > 0) {
      const ay = y + pad + br, x0 = x - gap + 8, x1 = x - 8;
      p.push(dvP(`M${n1(x0)} ${n1(ay)}H${n1(x1)}`, "dkv-arr", true), dvHead(x1, ay, 0));
    } else if (i > 0) {
      const px = (per - 1) * (cw + gap) + cw / 2, py = y - rowGap, x1 = cw / 2;
      p.push(dvP(`M${n1(px)} ${n1(py + 6)}V${n1(py + rowGap / 2)}H${n1(x1)}V${n1(y - 8)}`, "dkv-arr", true), dvHead(x1, y - 8, Math.PI / 2));
    }
    p.push(dvR(x, y, cw, ch, "dkv-card", 16), ...dvBadge(x + pad + br, y + pad + br, br, i + 1, X.M));
    if (s.icon) p.push(dvI(x + cw - pad - ic, y + pad + br - ic / 2, ic, s.icon, "dkv-ica"));
    p.push(dvT(x + pad, y + pad + top, lab[i], "label"));
    if (dvNote(notes[i])) p.push(dvT(x + pad, y + pad + top + lh + 10, notes[i], "note", "dkv-mut"));
    return p;
  });
  return { h: rows * ch + (rows - 1) * rowGap, base: [], parts };
}

// The tall form of a flow or a cycle: cards down a column, a cycle looping back on the right.
function dvColumn(st, W, X, loop) {
  const S = X.S, pad = 16, br = 18, gap = 30, cw = W - (loop ? 40 : 0), tx = pad + 2 * br + 12;
  const tw = cw - tx - pad - (st.some((s) => s.icon) ? 36 : 0);
  const lab = deckFitAll(st.map((s) => s.label), tw, dvZ(X, S.label), "label", X.M, 3);
  const notes = deckFitAll(st.map((s) => s.note), tw, dvZ(X, S.note), "note", X.M, 5);
  const boxes = [];
  let y = 0;
  const parts = st.map((s, i) => {
    const th = lab[i].h + (dvNote(notes[i]) ? 6 + notes[i].h : 0);
    const ch = Math.max(2 * br, th) + 2 * pad, ty = y + pad + Math.max(0, (2 * br - th) / 2);
    const p = [];
    if (i > 0) p.push(dvP(`M${pad + br} ${n1(y - gap + 4)}V${n1(y - 7)}`, "dkv-arr", true), dvHead(pad + br, y - 7, Math.PI / 2));
    p.push(dvR(0, y, cw, ch, "dkv-card", 14), ...dvBadge(pad + br, y + pad + br, br, i + 1, X.M));
    if (s.icon) p.push(dvI(cw - pad - 28, y + pad + 4, 28, s.icon, "dkv-ica"));
    p.push(dvT(tx, ty, lab[i], "label"));
    if (dvNote(notes[i])) p.push(dvT(tx, ty + lab[i].h + 6, notes[i], "note", "dkv-mut"));
    boxes.push({ y, ch });
    y += ch + gap;
    return p;
  });
  if (loop && st.length > 1) {
    const a = boxes[boxes.length - 1], b = boxes[0], y0 = a.y + a.ch / 2, y1 = b.y + b.ch / 2;
    parts[parts.length - 1].push(dvP(`M${n1(cw + 4)} ${n1(y0)}H${n1(W - 12)}V${n1(y1)}H${n1(cw + 8)}`, "dkv-arr", true), dvHead(cw + 8, y1, Math.PI));
  }
  return { h: y - gap, base: [], parts };
}

// cycle: steps round a ring, clockwise from the top (or, with an even number, from just right of it when that
// is less tall), the last arrow closing the loop.
function dvCycle(v, W, H, tall, X) {
  if (tall) return dvColumn(v.steps, W, X, true);
  const st = v.steps, n = st.length, S = X.S, nr = 40;
  const at = (R, off) => {
    const ang = st.map((_, i) => -Math.PI / 2 + off + (2 * Math.PI * i) / n);
    const pos = st.map((_, i) => {
      const c = Math.cos(ang[i]), side = Math.abs(c) < 0.3 ? 0 : Math.sign(c), px = R * c, py = R * Math.sin(ang[i]);
      const w = side === 0 ? Math.min(380, W / 2 - 20) : side > 0 ? W / 2 - (px + nr + 18) - 4 : px - nr - 18 + W / 2 - 4;
      return { side, px, py, w: Math.max(60, w) };
    });
    const lab = deckFitAll(st.map((s) => s.label), pos.map((q) => q.w), dvZ(X, S.label), "label", X.M, 2);
    const notes = deckFitAll(st.map((s) => s.note), pos.map((q) => q.w), dvZ(X, S.note), "note", X.M, 3);
    let y0 = -R - nr, y1 = R + nr, bad = 0;
    const boxes = pos.map((q, i) => {
      const bh = lab[i].h + (dvNote(notes[i]) ? 4 + notes[i].h : 0), bw = Math.max(lab[i].w, notes[i].w);
      let x, y;
      if (q.side === 0) { x = q.px - bw / 2; y = Math.sin(ang[i]) < 0 ? q.py - nr - 12 - bh : q.py + nr + 12; }
      else { x = q.side > 0 ? q.px + nr + 18 : q.px - nr - 18 - bw; y = q.py - bh / 2; }
      y0 = Math.min(y0, y);
      y1 = Math.max(y1, y + bh);
      return { x, y, w: bw, h: bh };
    });
    const hit = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
    const nodes = pos.map((q) => ({ x: q.px - nr, y: q.py - nr, w: 2 * nr, h: 2 * nr }));
    boxes.forEach((b, i) => { boxes.forEach((o, j) => { if (j > i && hit(b, o)) bad += 1; }); nodes.forEach((o, j) => { if (j !== i && hit(b, o)) bad += 1; }); });
    const tight = pos.some((q) => q.w < 140) || lab.some((f) => !f.ok) || notes.some((f) => !f.ok);
    return { R, ang, pos, lab, notes, boxes, y0, y1, score: bad * 1e6 + (tight ? 1e4 : 0) + Math.max(0, y1 - y0 - H) };
  };
  let best = null;
  for (const off of n % 2 ? [0] : [0, Math.PI / n]) {
    for (let R = 220; R >= 90; R -= 10) {
      const c = at(R, off);
      if (!best || c.score < best.score) best = c;
      if (c.score === 0) break;
    }
  }
  const { R, ang, pos, lab, notes, boxes } = best, cx = W / 2, cy = -best.y0, d = (nr + 10) / R;
  const base = [dvC(cx, cy, R, "dkv-track"), dvI(cx - 26, cy - 26, 52, "refresh", "dkv-icf")];
  const arc = (a, b) => {
    const a0 = ang[a] + d, a1 = (b === 0 ? ang[0] + 2 * Math.PI : ang[b]) - d;
    const x0 = cx + R * Math.cos(a0), y0 = cy + R * Math.sin(a0), x1 = cx + R * Math.cos(a1), y1 = cy + R * Math.sin(a1);
    return [dvP(`M${n1(x0)} ${n1(y0)}A${n1(R)} ${n1(R)} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${n1(x1)} ${n1(y1)}`, "dkv-arr", true), dvHead(x1, y1, a1 + Math.PI / 2)];
  };
  const parts = st.map((s, i) => {
    const q = pos[i], x = cx + q.px, y = cy + q.py, b = boxes[i];
    const p = i > 0 ? arc(i - 1, i) : [];
    if (i === n - 1) p.push(...arc(n - 1, 0));
    p.push(dvC(x, y, nr, "dkv-node"));
    if (s.icon) p.push(dvI(x - 17, y - 17, 34, s.icon, "dkv-ica"), ...dvBadge(x + nr * 0.74, y - nr * 0.74, 17, i + 1, X.M));
    else { const f = dvLine(i + 1, 30, "serif", X.M); p.push(dvT(x, y - f.lh / 2, f, "serif", "dkv-ink", "middle")); }
    const a = q.side === 0 ? "middle" : q.side > 0 ? "start" : "end";
    const tx = q.side === 0 ? cx + q.px : q.side > 0 ? cx + b.x : cx + b.x + b.w;
    p.push(dvT(tx, cy + b.y, lab[i], "label", "", a));
    if (dvNote(notes[i])) p.push(dvT(tx, cy + b.y + lab[i].h + 4, notes[i], "note", "dkv-mut", a));
    return p;
  });
  return { h: best.y1 - best.y0, base, parts };
}

// stack: layers top first, each a full-width slab with a coloured spine.
function dvStack(v, W, H, tall, X) {
  const ls = v.layers, S = X.S, sp = tall ? 54 : 72, pad = tall ? 14 : 16, gap = 8;
  const tw = W - sp - 2 * pad, lw = tall ? tw : Math.round((tw - pad) * 0.42), nw = tall ? tw : tw - pad - lw;
  const lab = deckFitAll(ls.map((l) => l.label), lw, dvZ(X, S.label), "label", X.M, tall ? 3 : 2);
  const notes = deckFitAll(ls.map((l) => l.note), nw, dvZ(X, S.note), "note", X.M, tall ? 4 : 3);
  const inner = ls.map((_, i) => (tall ? lab[i].h + (dvNote(notes[i]) ? 6 + notes[i].h : 0) : Math.max(lab[i].h, notes[i].h)));
  const sh = Math.max(tall ? 56 : 68, Math.max(...inner) + 2 * pad);
  const parts = ls.map((l, i) => {
    const y = i * (sh + gap), x = sp + pad;
    const p = [dvR(0, y, W, sh, "dkv-slab", 14), dvP(dvLeftRound(0, y, sp, sh, 14), `dkv-spine dkv-lv${Math.min(i, 4)}`)];
    if (l.icon) p.push(dvI(sp / 2 - 15, y + sh / 2 - 15, 30, l.icon, "dkv-icw"));
    else { const f = dvLine(i + 1, 26, "serif", X.M); p.push(dvT(sp / 2, y + (sh - f.lh) / 2, f, "serif", "dkv-on", "middle")); }
    if (tall) {
      const ty = y + (sh - inner[i]) / 2;
      p.push(dvT(x, ty, lab[i], "label"));
      if (dvNote(notes[i])) p.push(dvT(x, ty + lab[i].h + 6, notes[i], "note", "dkv-mut"));
    } else {
      p.push(dvT(x, y + (sh - lab[i].h) / 2, lab[i], "label"));
      if (dvNote(notes[i])) p.push(dvT(x + lw + pad, y + (sh - notes[i].h) / 2, notes[i], "note", "dkv-mut"));
    }
    return p;
  });
  return { h: ls.length * sh + (ls.length - 1) * gap, base: [], parts };
}

// timeline: events along an axis, the time above each; down the left when tall.
function dvTimeline(v, W, H, tall, X) {
  const ev = v.events, S = X.S, icon = ev.some((e) => e.icon), dr = icon ? 18 : 12;
  if (tall) {
    const ax = 22, tx = 56, tw = W - tx - 2, gap = 24;
    const when = deckFitAll(ev.map((e) => e.when), tw, dvZ(X, [24, 22, 20]), "serif", X.M, 2);
    const lab = deckFitAll(ev.map((e) => e.label), tw, dvZ(X, S.label), "label", X.M, 3);
    const notes = deckFitAll(ev.map((e) => e.note), tw, dvZ(X, S.note), "note", X.M, 4);
    let y = 0, prev = 0;
    const parts = ev.map((e, i) => {
      const cy = y + Math.max(dr, when[i].lh / 2), p = [];
      if (i) p.push(dvP(`M${ax} ${n1(prev + dr + 3)}V${n1(cy - dr - 3)}`, "dkv-prog", true));
      p.push(dvC(ax, cy, dr, "dkv-dot"));
      if (e.icon) p.push(dvI(ax - 11, cy - 11, 22, e.icon, "dkv-icw"));
      p.push(dvT(tx, cy - when[i].lh / 2, when[i], "serif", "dkv-when"));
      let bottom = cy - when[i].lh / 2 + when[i].h + 4;
      p.push(dvT(tx, bottom, lab[i], "label"));
      bottom += lab[i].h;
      if (dvNote(notes[i])) { p.push(dvT(tx, bottom + 4, notes[i], "note", "dkv-mut")); bottom += 4 + notes[i].h; }
      prev = cy;
      y = Math.max(bottom, cy + dr) + gap;
      return p;
    });
    return { h: y - gap, base: [dvP(`M${ax} 0V${n1(y - gap)}`, "dkv-axis")], parts };
  }
  const n = ev.length, colW = W / n, tw = colW - 24;
  const when = deckFitAll(ev.map((e) => e.when), tw, dvZ(X, [30, 28, 26, 24, 22]), "serif", X.M, 2);
  const lab = deckFitAll(ev.map((e) => e.label), tw, dvZ(X, S.label), "label", X.M, 3);
  const notes = deckFitAll(ev.map((e) => e.note), tw, dvZ(X, S.note), "note", X.M, 4);
  const wh = dvMax(when), lh = dvMax(lab), nh = dvMax(notes), ay = wh + 22 + dr;
  const base = [dvP(`M2 ${n1(ay)}H${n1(W - 4)}`, "dkv-axis"), dvHead(W - 4, ay, 0, 12, "dkv-axis")];
  const parts = ev.map((e, i) => {
    const x = colW * (i + 0.5), x0 = i ? colW * (i - 0.5) + dr + 3 : 2;
    const p = [dvP(`M${n1(x0)} ${n1(ay)}H${n1(x - dr - 3)}`, "dkv-prog", true), dvC(x, ay, dr, "dkv-dot")];
    if (e.icon) p.push(dvI(x - 11, ay - 11, 22, e.icon, "dkv-icw"));
    p.push(dvT(x, ay - dr - 14 - when[i].h, when[i], "serif", "dkv-when", "middle"));
    p.push(dvT(x, ay + dr + 20, lab[i], "label", "", "middle"));
    if (dvNote(notes[i])) p.push(dvT(x, ay + dr + 20 + lh + 8, notes[i], "note", "dkv-mut", "middle"));
    return p;
  });
  return { h: ay + dr + 20 + lh + (nh ? 8 + nh : 0), base, parts };
}

// hub: one thing in the middle and its parts around it, clockwise from the top right; a tree when tall.
function dvHub(v, W, H, tall, X) {
  const sp = v.spokes, S = X.S, pad = 16, icon = sp.some((s) => s.icon);
  if (tall) {
    const center = deckFit(v.center, W - 2 * pad - (v.icon ? 44 : 0), dvZ(X, [24, 22, 20, 18]), "bold", X.M, 2);
    const ph = Math.max(58, center.h + 2 * pad), trunk = 20, cx0 = 44, cw = W - cx0;
    const base = [dvR(0, 0, W, ph, "dkv-hubp", 16)];
    if (v.icon) base.push(dvI(pad + 4, (ph - 28) / 2, 28, v.icon, "dkv-icw"));
    base.push(dvT(pad + (v.icon ? 44 : 4), (ph - center.h) / 2, center, "bold", "dkv-on"));
    const tw = cw - 2 * pad - (icon ? 40 : 0);
    const lab = deckFitAll(sp.map((s) => s.label), tw, dvZ(X, S.label), "label", X.M, 2);
    const notes = deckFitAll(sp.map((s) => s.note), tw, dvZ(X, S.note), "note", X.M, 4);
    let y = ph + 20, mid = 0;
    const parts = sp.map((s, i) => {
      const th = lab[i].h + (dvNote(notes[i]) ? 6 + notes[i].h : 0), ch = Math.max(icon ? 58 : 0, th + 2 * pad);
      mid = y + ch / 2;
      const p = [dvP(`M${trunk} ${n1(mid)}H${cx0 - 2}`, "dkv-link", true), dvR(cx0, y, cw, ch, "dkv-card", 14), dvC(cx0, mid, 5, "dkv-acc")];
      let tx = cx0 + pad;
      if (s.icon) { p.push(dvI(tx, mid - 14, 28, s.icon, "dkv-ica")); tx += 40; }
      p.push(dvT(tx, mid - th / 2, lab[i], "label"));
      if (dvNote(notes[i])) p.push(dvT(tx, mid - th / 2 + lab[i].h + 6, notes[i], "note", "dkv-mut"));
      y += ch + 12;
      return p;
    });
    base.push(dvP(`M${trunk} ${ph}V${n1(mid)}`, "dkv-link"));
    return { h: y - 12, base, parts };
  }
  // the cards grow with the box (a diagram scaled down is laid out wider), up to what leaves room for the links
  const n = sp.length, cr = 86, cw = Math.min(Math.max(340, W * 0.31), (W - 2 * cr - 140) / 2), vg = 16;
  const tw = cw - 2 * pad - (icon ? 52 : 0);
  const lab = deckFitAll(sp.map((s) => s.label), tw, dvZ(X, S.label), "label", X.M, 2);
  const notes = deckFitAll(sp.map((s) => s.note), tw, dvZ(X, S.note), "note", X.M, 3);
  const th = sp.map((_, i) => lab[i].h + (dvNote(notes[i]) ? 6 + notes[i].h : 0));
  const ch = Math.max(icon ? 64 : 0, Math.max(...th) + 2 * pad);
  const right = Math.ceil(n / 2), left = n - right, colH = (k) => k * ch + (k - 1) * vg;
  const hgt = Math.max(colH(right), colH(left), 2 * cr + 28), cx = W / 2, cy = hgt / 2;
  const center = deckFit(v.center, cr * 1.5, dvZ(X, [24, 22, 20, 18, 16]), "bold", X.M, 3);
  const ic = v.icon ? 42 : 0, bh = ic + center.h;
  const base = [dvC(cx, cy, cr + 12, "dkv-halo"), dvC(cx, cy, cr, "dkv-hubc")];
  if (v.icon) base.push(dvI(cx - 17, cy - bh / 2, 34, v.icon, "dkv-icw"));
  base.push(dvT(cx, cy - bh / 2 + ic, center, "bold", "dkv-on", "middle"));
  const pos = [];
  for (let i = 0; i < right; i++) pos.push({ x: W - cw, y: (hgt - colH(right)) / 2 + i * (ch + vg), side: 1 });
  for (let j = 0; j < left; j++) pos.push({ x: 0, y: (hgt - colH(left)) / 2 + (left - 1 - j) * (ch + vg), side: -1 });
  const parts = sp.map((s, i) => {
    const { x, y, side } = pos[i], ex = side > 0 ? x : x + cw, ey = y + ch / 2;
    const a = Math.atan2(ey - cy, ex - cx), sx = cx + (cr + 12) * Math.cos(a), sy = cy + (cr + 12) * Math.sin(a), mx = (sx + ex) / 2;
    const p = [dvP(`M${n1(sx)} ${n1(sy)}C${n1(mx)} ${n1(sy)} ${n1(mx)} ${n1(ey)} ${n1(ex)} ${n1(ey)}`, "dkv-link", true), dvR(x, y, cw, ch, "dkv-card", 14), dvC(ex, ey, 5, "dkv-acc")];
    let tx = x + pad;
    if (s.icon) { p.push(dvR(tx, ey - 20, 40, 40, "dkv-icbg", 10), dvI(tx + 7, ey - 13, 26, s.icon, "dkv-ica")); tx += 52; }
    p.push(dvT(tx, ey - th[i] / 2, lab[i], "label"));
    if (dvNote(notes[i])) p.push(dvT(tx, ey - th[i] / 2 + lab[i].h + 6, notes[i], "note", "dkv-mut"));
    return p;
  });
  return { h: hgt, base, parts };
}

// compare: a table with coloured column heads; yes and no are drawn as a tick or a cross, with the word.
const dvYesNo = (c) => /^(yes|no)$/i.test(String(c).trim());
const dvCap = (c) => { const t = String(c).trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
function dvCompare(v, W, H, tall, X) {
  const cols = v.columns, rows = v.rows, k = cols.length, S = X.S;
  const mark = (x, cy, c, size) => {
    const ok = /^yes$/i.test(String(c).trim()), f = dvLine(dvCap(c), size, "label", X.M);
    return [dvC(x + 15, cy, 15, ok ? "dkv-yes" : "dkv-no"), dvI(x + 6, cy - 9, 18, ok ? "check" : "x", "dkv-icw dkv-thick"), dvT(x + 38, cy - f.lh / 2, f, "label", ok ? "dkv-yest" : "dkv-not")];
  };
  if (tall) {
    const pad = 14, pills = deckFitAll(cols, 132, [16, 15, 14], "bold", X.M, 2);
    const pw = Math.min(150, Math.max(...pills.map((f) => f.w)) + 24), tx = pad + pw + 12, tw = W - tx - pad;
    const labs = deckFitAll(rows.map((r) => r.label), W - 2 * pad - 10, dvZ(X, S.head), "bold", X.M, 2);
    const cells = deckFitAll(rows.flatMap((r) => r.cells.map((c) => (dvYesNo(c) ? "" : c))), tw, dvZ(X, S.note), "note", X.M, 4);
    let y = 0;
    const parts = rows.map((r, i) => {
      const p = [], top = y;
      let yy = y + pad + labs[i].h + 10;
      const inner = r.cells.map((c, j) => {
        const f = cells[i * k + j], hh = Math.max(pills[j].h + 10, dvYesNo(c) ? 32 : f.h);
        const out = [dvR(pad, yy + (hh - pills[j].h - 10) / 2, pw, pills[j].h + 10, `dkv-colh dkv-col${j}`, 8),
          dvT(pad + pw / 2, yy + (hh - pills[j].h) / 2, pills[j], "bold", `dkv-colt${j}`, "middle")];
        if (dvYesNo(c)) out.push(...mark(tx, yy + hh / 2, c, 17));
        else out.push(dvT(tx, yy + (hh - f.h) / 2, f, "note"));
        yy += hh + 8;
        return out;
      });
      const bh = yy - 8 + pad - top;
      p.push(dvR(0, top, W, bh, "dkv-card", 14), dvR(pad, top + pad + 2, 4, labs[i].h - 4, "dkv-acc", 2), dvT(pad + 12, top + pad, labs[i], "bold"), ...inner.flat());
      y = top + bh + 12;
      return p;
    });
    return { h: y - 12, base: [], parts };
  }
  const pad = 16, lw = Math.round(Math.min(250, Math.max(170, W * 0.21))), cw = (W - lw) / k;
  const heads = deckFitAll(cols, cw - 2 * pad - 8, dvZ(X, S.head), "bold", X.M, 2);
  const hh = dvMax(heads) + 22;
  const labs = deckFitAll(rows.map((r) => r.label), lw - 2 * pad, dvZ(X, S.head), "bold", X.M, 3);
  const cells = deckFitAll(rows.flatMap((r) => r.cells.map((c) => (dvYesNo(c) ? "" : c))), cw - 2 * pad, dvZ(X, S.note), "note", X.M, 4);
  const base = cols.flatMap((c, j) => [dvR(lw + j * cw + 6, 0, cw - 12, hh, `dkv-colh dkv-col${j}`, 12),
    dvT(lw + j * cw + cw / 2, (hh - heads[j].h) / 2, heads[j], "bold", `dkv-colt${j}`, "middle")]);
  let y = hh + 10;
  const parts = rows.map((r, i) => {
    const cs = cells.slice(i * k, i * k + k), rh = Math.max(labs[i].h, ...cs.map((f) => f.h), 34) + 2 * pad;
    const p = [dvR(0, y, W, rh, i % 2 ? "dkv-zb1" : "dkv-zb0", 12), dvT(pad, y + (rh - labs[i].h) / 2, labs[i], "bold")];
    r.cells.forEach((c, j) => {
      const x = lw + j * cw + pad;
      if (dvYesNo(c)) p.push(...mark(x, y + rh / 2, c, 18));
      else p.push(dvT(x, y + (rh - cs[j].h) / 2, cs[j], "note"));
    });
    y += rh + 6;
    return p;
  });
  return { h: y - 6, base, parts };
}

// contrast: two titled sides and a "vs" between them; each point builds on its own.
function dvContrast(v, W, H, tall, X) {
  const sides = [v.left, v.right], S = X.S, pad = 20, vs = tall ? 44 : 76, mk = 28, gap = 14;
  const pw = tall ? W : (W - vs) / 2, tw = pw - 2 * pad - mk - 12;
  const titles = deckFitAll(sides.map((s) => s.title), pw - 2 * pad - 40, dvZ(X, tall ? [22, 21, 20, 19, 18] : [25, 24, 23, 22, 21, 20]), "bold", X.M, 2);
  const pts = deckFitAll(v.left.points.concat(v.right.points), tw, dvZ(X, tall ? [19, 18, 17, 16] : [21, 20, 19, 18, 17, 16]), "note", X.M, 4);
  const of = (s) => (s ? pts.slice(v.left.points.length) : pts.slice(0, v.left.points.length));
  const headH = dvMax(titles) + 30;
  const body = (s) => of(s).reduce((a, f) => a + Math.max(f.h, mk) + gap, 0) - gap + 2 * pad;
  const ph = tall ? [headH + body(0), headH + body(1)] : [headH + Math.max(body(0), body(1)), headH + Math.max(body(0), body(1))];
  const at = tall ? [{ x: 0, y: 0 }, { x: 0, y: ph[0] + vs }] : [{ x: 0, y: 0 }, { x: pw + vs, y: 0 }];
  const base = [];
  sides.forEach((s, i) => {
    const { x, y } = at[i];
    base.push(dvR(x, y, pw, ph[i], `dkv-pan dkv-t-${s.tone}`, 16), dvP(dvTopRound(x, y, pw, headH, 16), `dkv-panh dkv-t-${s.tone}`));
    if (s.icon) base.push(dvI(x + pad, y + (headH - 26) / 2, 26, s.icon, "dkv-icw"));
    base.push(dvT(x + pad + (s.icon ? 38 : 0), y + (headH - titles[i].h) / 2, titles[i], "bold", "dkv-on"));
  });
  const vx = tall ? W / 2 : pw + vs / 2, vy = tall ? ph[0] + vs / 2 : ph[0] / 2, vf = dvLine("vs", 22, "serifi", X.M);
  base.push(dvC(vx, vy, tall ? 19 : 26, "dkv-vs"), dvT(vx, vy - vf.lh / 2 - 1, vf, "serifi", "dkv-vst", "middle"));
  const parts = [];
  sides.forEach((s, i) => {
    const { x, y } = at[i];
    let yy = y + headH + pad;
    of(i).forEach((f) => {
      const rh = Math.max(f.h, mk), cy = yy + Math.min(rh, f.lh) / 2;
      const p = [dvC(x + pad + mk / 2, cy, mk / 2, `dkv-mk dkv-t-${s.tone}`)];
      if (s.tone === "neutral") p.push(dvC(x + pad + mk / 2, cy, 4, "dkv-mkdot"));
      else p.push(dvI(x + pad + mk / 2 - 8, cy - 8, 16, s.tone === "good" ? "check" : "x", "dkv-icw dkv-thick"));
      p.push(dvT(x + pad + mk + 12, yy + (f.h < mk ? (mk - f.h) / 2 : 0), f, "note"));
      parts.push(p);
      yy += rh + gap;
    });
  });
  return { h: tall ? ph[0] + vs + ph[1] : ph[0], base, parts };
}

// matrix: a two by two with named axes; y high is the top row.
function dvMatrix(v, W, H, tall, X) {
  const S = X.S, cells = v.cells, hw = tall ? 104 : 156, gap = 10, ax = 12;
  const gw = W - hw, cw = (gw - gap) / 2, axv = dvZ(X, tall ? [16, 15, 14] : [18, 17, 16]);
  const yl = deckFit(v.y.label, W - 8, dvZ(X, S.head), "bold", X.M, 2);
  const top = yl.h + 14;
  const rh = deckFitAll([v.y.high, v.y.low], hw - ax - 22, axv, "label", X.M, 4);
  const lab = deckFitAll(cells.map((c) => c.label), cw - 28, dvZ(X, S.label), "label", X.M, 2);
  const notes = deckFitAll(cells.map((c) => c.note), cw - 28, dvZ(X, S.note), "note", X.M, 4);
  const inner = cells.map((_, i) => lab[i].h + (dvNote(notes[i]) ? 6 + notes[i].h : 0));
  const xh = deckFitAll([v.x.low, v.x.high], cw - 8, axv, "label", X.M, 3);
  const xl = deckFit(v.x.label, gw - 8, dvZ(X, S.head), "bold", X.M, 2);
  const bottom = 12 + dvMax(xh) + 16 + xl.h;
  const need = Math.max(Math.max(...inner) + 36, dvMax(rh) + 16);
  const chh = Math.max(need, Math.min((H - top - bottom - gap) / 2, tall ? 170 : 200));
  const gy = top, gh = 2 * chh + gap, xy = gy + gh + 12;
  const base = [dvT(0, 0, yl, "bold", "dkv-axl"),
    dvP(`M${ax} ${n1(gy + gh - 4)}V${n1(gy + 6)}`, "dkv-axis"), dvHead(ax, gy + 6, -Math.PI / 2, 11, "dkv-axis"),
    dvT(ax + 14, gy + (chh - rh[0].h) / 2, rh[0], "label", "dkv-axv"), dvT(ax + 14, gy + chh + gap + (chh - rh[1].h) / 2, rh[1], "label", "dkv-axv"),
    dvT(hw + cw / 2, xy, xh[0], "label", "dkv-axv", "middle"), dvT(hw + cw + gap + cw / 2, xy, xh[1], "label", "dkv-axv", "middle")];
  const ly = xy + dvMax(xh) + 8;
  base.push(dvP(`M${n1(hw + 6)} ${n1(ly)}H${n1(W - 4)}`, "dkv-axis"), dvHead(W - 4, ly, 0, 11, "dkv-axis"), dvT(hw + gw / 2, ly + 8, xl, "bold", "dkv-axl", "middle"));
  const parts = cells.map((c, i) => {
    const x = hw + c.x * (cw + gap), y = gy + (1 - c.y) * (chh + gap), ty = y + (chh - inner[i]) / 2;
    const p = [dvR(x, y, cw, chh, `dkv-q dkv-q${c.x}${c.y}`, 14), dvT(x + cw / 2, ty, lab[i], "label", "", "middle")];
    if (dvNote(notes[i])) p.push(dvT(x + cw / 2, ty + lab[i].h + 6, notes[i], "note", "dkv-mut", "middle"));
    return p;
  });
  return { h: ly + 8 + xl.h, base, parts };
}

// keynum: one number that matters, large, with what it counts and the exact words it comes from.
function dvKeynum(v, W, H, tall, X) {
  const two = !tall && W >= 640, zw = two ? Math.round(W * 0.42) : W;
  const sizes = dvZ(X, tall ? [120, 104, 92, 80, 68, 60] : [176, 156, 140, 124, 108, 96, 84, 72, 60]);
  const vs = sizes.find((s) => X.M(v.value, s, "serif") <= zw - 70) || sizes[sizes.length - 1];
  const num = { lines: [v.value], size: vs, lh: Math.round(vs * 1.04), h: Math.round(vs * 1.04), w: X.M(v.value, vs, "serif") };
  const us = Math.max(22, Math.round(vs * 0.22));
  const unit = v.unit ? deckFit(v.unit, zw - 60, [us, us - 2, us - 4], "serif", X.M, 2) : null;
  const bh = num.h + (unit ? 2 + unit.h : 0), rr = Math.min(zw / 2 - 14, bh / 2 + 44);
  const tw = two ? W - zw - 40 : W - 8;
  const lab = deckFit(v.label, tw, dvZ(X, tall ? [23, 22, 21, 20, 19, 18] : [30, 28, 26, 24, 22, 20]), "label", X.M, 3);
  const q = deckFit(`“${v.quote}”`, tw, dvZ(X, tall ? [18, 17, 16] : [21, 20, 19, 18, 17, 16]), "serifi", X.M, 4);
  const src = deckFit(`— ${X.src(v.source)}`, tw, tall ? [15, 14] : [16], "note", X.M, 2);
  const th = lab.h + 16 + q.h + 8 + src.h;
  const p = [];
  const numText = (cx, top) => {
    const t = dvT(cx, top, num, "serif", "dkv-num", "middle");
    if (/^\d+$/.test(v.value)) t.count = Number(v.value);
    p.push(t);
    if (unit) p.push(dvT(cx, top + num.h + 2, unit, "serif", "dkv-unit", "middle"));
  };
  if (two) {
    const hgt = Math.max(2 * rr + 24, th), cx = zw / 2, cy = hgt / 2, tx = zw + 40, ty = (hgt - th) / 2;
    numText(cx, cy - bh / 2);
    p.push(dvT(tx, ty, lab, "label"), dvT(tx, ty + lab.h + 16, q, "serifi", "dkv-quote"), dvT(tx, ty + lab.h + 16 + q.h + 8, src, "note", "dkv-mut"));
    return { h: hgt, base: [dvC(cx, cy, rr + 10, "dkv-kring"), dvC(cx, cy, rr, "dkv-kc")], parts: [p] };
  }
  const cx = W / 2, cy = rr + 10, ty = 2 * rr + 34;
  numText(cx, cy - bh / 2);
  p.push(dvT(cx, ty, lab, "label", "", "middle"), dvT(cx, ty + lab.h + 14, q, "serifi", "dkv-quote", "middle"), dvT(cx, ty + lab.h + 14 + q.h + 8, src, "note", "dkv-mut", "middle"));
  return { h: ty + th - 2, base: [dvC(cx, cy, rr + 10, "dkv-kring"), dvC(cx, cy, rr, "dkv-kc")], parts: [p] };
}

// code: the page's own lines, coloured lightly, with numbered notes on lines beside it (or under it).
const DECK_CMT = { yaml: "#", shell: "#", python: "#", powershell: "#", ini: "#", sql: "--", javascript: "//", typescript: "//", csharp: "//", bicep: "//" };
const DECK_KW = new Set(("true false null none yes no on off if else elif then fi for foreach while in return function def class import from "
  + "const let var select where and or not as public private static void new param resource output module async await try catch").split(" "));
function deckTokens(line, lang) {
  const out = [], mark = DECK_CMT[lang] || "";
  const push = (t, c) => {
    if (!t) return;
    const last = out[out.length - 1];
    if (last && last.c === c) last.t += t; else out.push({ t, c });
  };
  let i = 0;
  const key = /^(yaml|json|http|ini|bicep)$/.test(lang) ? /^(\s*(?:-\s+)?)("[^"]*"|[\w.$@-]+)(\s*[:=])(?=\s|$)/.exec(line) : null;
  if (key) { push(key[1], ""); push(key[2], "k"); push(key[3], "p"); i = key[0].length; }
  const re = /("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)|(\d+(?:\.\d+)?)|([A-Za-z_][\w-]*)|(\s+)|([\s\S])/y;
  while (i < line.length) {
    if (mark && line.startsWith(mark, i) && (i === 0 || /\s/.test(line[i - 1]))) { push(line.slice(i), "c"); break; }
    re.lastIndex = i;
    const m = re.exec(line);
    if (m[1]) push(m[1], "s");
    else if (m[2]) push(m[2], "n");
    else if (m[3]) push(m[3], DECK_KW.has(m[3].toLowerCase()) ? "w" : "");
    else if (m[4]) push(m[4], "");
    else push(m[5], "p");
    i = re.lastIndex;
  }
  return out;
}
function dvCode(v, W, H, tall, X) {
  const br = tall ? 15 : 17, gut = tall ? 64 : 72, pad = 18, head = 46, cgap = 46, callW = 330, marks = v.marks;
  const longest = v.lines.reduce((a, l) => (l.length > a.length ? l : a), "");
  const sizes = dvZ(X, tall ? [16, 15, 14] : [21, 20, 19, 18, 17, 16]);
  const sizeFor = (w) => sizes.find((s) => X.M(longest, s, "mono") <= w);
  let side = !tall && marks.length > 0, fs = side ? sizeFor(W - callW - cgap - gut - 8 - pad) : 0;
  if (!fs) side = false;
  if (!side) fs = sizeFor(W - gut - 8 - pad) || sizes[sizes.length - 1];
  const pw = side ? W - callW - cgap : W, lh = Math.round(fs * DECK_LH.mono), cx0 = gut + 8;
  const rows = [];
  v.lines.forEach((ln, li) => {
    const cpr = Math.max(12, Math.floor((pw - cx0 - pad) / X.M("0", fs, "mono")));
    const ind = (/^\s*/.exec(ln) || [""])[0].length;
    let rest = ln, first = true;
    while (rest.length > cpr) {
      let cut = rest.lastIndexOf(" ", cpr);
      if (cut <= ind) cut = cpr;
      rows.push({ text: rest.slice(0, cut), li, first });
      rest = " ".repeat(Math.min(ind + 2, 8)) + rest.slice(cut).replace(/^\s+/, "");
      first = false;
    }
    rows.push({ text: rest, li, first });
  });
  const top = head + 12, ph = top + rows.length * lh + pad - 4, small = tall ? 14 : 16;
  const chip = dvLine(v.lang.toUpperCase(), small, "mono", X.M), cw = chip.w + 18;
  // `back` is the panel: the marked-line bands go over it and under the text
  const back = [dvR(0, 0, pw, ph, "dkv-code", 16), dvP(`M0 ${head}H${n1(pw)}`, "dkv-codeh"),
    dvR(pad, (head - 26) / 2, cw, 26, "dkv-chip", 6), dvT(pad + cw / 2, (head - chip.lh) / 2, chip, "mono", "dkv-chipt", "middle")];
  const srcw = pw - 3 * pad - cw;
  const sf = deckFit(X.src(v.source), srcw, [small], "note", X.M, 1);
  const sl = sf.ok ? sf : dvLine(`Source ${v.source}`, small, "note", X.M);
  if (sl.w <= srcw) back.push(dvT(pw - pad, (head - sl.lh) / 2, sl, "note", "dkv-srcl", "end"));
  const nums = { lines: rows.map((r) => (r.first ? String(r.li + 1) : "")), size: fs - 3, lh, w: X.M("00", fs - 3, "mono") };
  const base = [dvT(gut - 12, top, nums, "mono", "dkv-lnum", "end"),
    dvT(cx0, top, { lines: rows.map((r) => deckTokens(r.text, v.lang)), size: fs, lh, w: X.M(longest, fs, "mono") }, "mono", "dkv-ctext")];
  const rowOf = (li) => rows.findIndex((r) => r.li === li);
  const spanOf = (li) => rows.filter((r) => r.li === li).length;
  const under = marks.map((m) => [dvR(4, top + rowOf(m.line) * lh - 2, pw - 8, spanOf(m.line) * lh + 4, "dkv-band", 6)]);
  const parts = marks.map((m, j) => dvBadge(br + 2, top + rowOf(m.line) * lh + lh / 2, Math.min(br, lh / 2 + 2), j + 1, X.M, small));
  let h = ph;
  if (side) {
    const tw = callW - 2 * br - 30, nf = deckFitAll(marks.map((m) => m.note), tw, dvZ(X, [19, 18, 17, 16]), "note", X.M, 4);
    const order = marks.map((m, j) => j).sort((a, b) => marks[a].line - marks[b].line);
    let floor = 0;
    for (const j of order) {
      const ry = top + rowOf(marks[j].line) * lh + lh / 2, ch = Math.max(2 * br + 14, nf[j].h + 24);
      const y = Math.max(floor, ry - ch / 2), x = pw + cgap, my = y + ch / 2;
      parts[j].push(dvP(`M${n1(pw + 2)} ${n1(ry)}H${n1(pw + cgap / 2)}V${n1(my)}H${n1(x - 2)}`, "dkv-link", true),
        dvR(x, y, callW, ch, "dkv-card", 12), ...dvBadge(x + 12 + br, my, br, j + 1, X.M), dvT(x + 2 * br + 22, my - nf[j].h / 2, nf[j], "note"));
      floor = y + ch + 12;
      h = Math.max(h, y + ch);
    }
  } else {
    const nf = deckFitAll(marks.map((m) => m.note), W - 2 * br - 14, dvZ(X, tall ? [17, 16, 15] : [19, 18, 17, 16]), "note", X.M, 4);
    let y = ph + 16;
    marks.forEach((m, j) => {
      parts[j].push(...dvBadge(br, y + Math.min(nf[j].lh, 2 * br) / 2, br, j + 1, X.M), dvT(2 * br + 12, y, nf[j], "note"));
      y += Math.max(2 * br, nf[j].h) + 10;
    });
    if (marks.length) h = y - 10;
  }
  return { h, back, base, parts, under };
}

const DECK_LAYOUTS = { flow: dvFlow, cycle: dvCycle, stack: dvStack, timeline: dvTimeline, hub: dvHub, compare: dvCompare, contrast: dvContrast, matrix: dvMatrix, keynum: dvKeynum, code: dvCode };

// The scale and text level a diagram is drawn at in a box of W by H. A diagram that does not fit is laid out
// again with smaller text (a level down, dvZ) or scaled down, and a diagram that is scaled down is laid out
// that much wider, so it still fills the box: of the ways it fits, the one whose smallest text comes out
// largest. Nothing is scaled below DECK_LEAST. A diagram that fits no way is drawn at full size on a phone
// and at DECK_LEAST on the stage, and its box scrolls.
const DECK_LEAST = { wide: 0.75, tall: 0.8 };
const DECK_LEVELS = 6;
function deckSmallest(lay) {
  let least = Infinity;
  for (const s of [...(lay.back || []), ...lay.base, ...lay.parts.flat(), ...(lay.under || []).flat()]) {
    if (s.k === "text" && !/\bdkv-lnum\b/.test(s.c)) least = Math.min(least, s.size);
  }
  return least;
}
function deckScale(v, W, H, tall, X) {
  const L = DECK_LAYOUTS[v.type], least = tall ? DECK_LEAST.tall : DECK_LEAST.wide;
  const at = (s, lvl) => L(v, W / s, H / s, tall, { ...X, lvl });
  let best = null;
  for (let lvl = 0; lvl <= DECK_LEVELS; lvl++) {
    let s = 1, lay = at(1, lvl);
    if (lay.h > H) {
      const low = at(least, lvl);
      if (low.h * least > H) continue;
      let lo = least, hi = 1;
      lay = low;
      for (let k = 0; k < 6; k++) {
        const mid = (lo + hi) / 2, m = at(mid, lvl);
        if (m.h * mid <= H) { lo = mid; lay = m; } else hi = mid;
      }
      s = lo;
    }
    const eff = deckSmallest(lay) * s;
    if (!best || eff > best.eff + 0.05) best = { s, lay, lvl, eff, scroll: false };
    if (s === 1) break;   // it fits at full scale: smaller text would not help
  }
  if (best) return best;
  const s = tall ? 1 : least, lay = at(s, tall ? 0 : DECK_LEVELS);
  return { s, lay, lvl: tall ? 0 : DECK_LEVELS, eff: deckSmallest(lay) * s, scroll: true };
}

// What a diagram says, for screen readers: the label of the picture, then lists or a table.
function deckAlt(v, src) {
  const it = (x) => (x.note ? `${x.label}: ${x.note}` : x.label);
  const list = (kind, intro, items) => ({ kind, intro, items });
  switch (v.type) {
    case "flow": return { label: `Flow: ${v.steps.map((s) => s.label).join(", then ")}`, blocks: [list("ol", "The steps, in order:", v.steps.map(it))] };
    case "cycle": return { label: `Cycle: ${v.steps.map((s) => s.label).join(", then ")}, then back to the start`, blocks: [list("ol", "A loop: after the last step it starts again at step 1.", v.steps.map(it))] };
    case "stack": return { label: `Stack of ${v.layers.length} layers, top first: ${v.layers.map((s) => s.label).join(", ")}`, blocks: [list("ol", "The layers, from the top:", v.layers.map(it))] };
    case "timeline": return { label: `Timeline: ${v.events.map((e) => `${e.when}, ${e.label}`).join("; ")}`, blocks: [list("ol", "In order:", v.events.map((e) => `${e.when}: ${it(e)}`))] };
    case "hub": return { label: `${v.center}, with ${v.spokes.length} parts: ${v.spokes.map((s) => s.label).join(", ")}`, blocks: [list("ul", `${v.center} and its parts:`, v.spokes.map(it))] };
    case "compare": return { label: `Table comparing ${v.columns.join(", ")}`, blocks: [{ kind: "table", head: [""].concat(v.columns), rows: v.rows.map((r) => [r.label].concat(r.cells)) }] };
    case "contrast": return { label: `${v.left.title} compared with ${v.right.title}`, blocks: [list("ul", `${v.left.title}:`, v.left.points), list("ul", `${v.right.title}:`, v.right.points)] };
    case "matrix": {
      const at = (x, y) => { const c = v.cells.find((q) => q.x === x && q.y === y); return c ? it(c) : ""; };
      return { label: `Two by two: ${v.y.label} against ${v.x.label}`,
        blocks: [{ kind: "table", head: [`${v.y.label} / ${v.x.label}`, v.x.low, v.x.high], rows: [[v.y.high, at(0, 1), at(1, 1)], [v.y.low, at(0, 0), at(1, 0)]] }] };
    }
    case "keynum": return { label: `${v.value}${v.unit ? ` ${v.unit}` : ""}: ${v.label}`, blocks: [list("p", "", [`“${v.quote}” — ${src(v.source)}`])] };
    case "code": return { label: `Code (${v.lang}), ${v.lines.length} line${v.lines.length === 1 ? "" : "s"} from ${src(v.source)}`,
      blocks: [{ kind: "pre", text: v.lines.join("\n") }].concat(v.marks.length ? [list("ol", "Notes on the code:", v.marks.map((m) => `Line ${m.line + 1}: ${m.note}`))] : []) };
    default: return { label: "Diagram", blocks: [] };
  }
}
// deck: pure end

// ---- the page side: drawing a diagram, the scenes, and the player
const DECK_XMLNS = "http://www.w3.org/XML/1998/namespace";
const DECK_ICON = { intro: "deck", ideas: "sparkle", practice: "hand", traps: "alert", wrap: "check" };
const DECK_KIND = { title: "Title", agenda: "In this lesson", part: "Part", slide: "Slide", process: "Worked example", trap: "Common trap", recap: "Recap", check: "Check yourself", end: "Your turn" };
const DECK_TYPE = { flow: "steps", cycle: "a loop", stack: "layers", timeline: "a timeline", hub: "its parts", compare: "a table", contrast: "two sides", matrix: "a two by two", keynum: "the number", code: "code" };
const DECK_FONT_LOAD = ["400 16px Inter", "600 16px Inter", "700 16px Inter", "600 16px Fraunces", "italic 400 16px Fraunces", "400 16px \"JetBrains Mono\""];
// What Present says about the deck it plays (ADR 0014): the visual deck, or why it is the derived one.
const DECK_WAIT = {
  missing: "This deck is built from the checked lesson alone.",
  writing: "This deck is built from the checked lesson alone. Its diagrams are being drawn and checked now.",
  failed: "This deck is built from the checked lesson alone: its diagrams did not pass the checks.",
  stopped: "This deck is built from the checked lesson alone: drawing its diagrams stopped before it was finished.",
  invalidated: "This deck is built from the checked lesson alone: its diagrams were withdrawn, because a newer lesson replaced this one.",
  held: "This deck is built from the checked lesson alone: a changed source page no longer supports this lesson, so its diagrams wait until it is checked again.",
};
function deckNote(D) {
  if (D.level === "visual") {
    const m = D.models || {};
    return `The diagrams, outcomes, tips and recap were written by ${m.author || "the author model"} from this lesson and checked by ${m.gate || "an independent model"} against the same pages; anything that did not hold was left out. The slide bullets and the narration are the checked lesson's own.`;
  }
  return DECK_WAIT[D.state] || "This deck is built from the checked lesson alone.";
}

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined && v !== false) el.setAttribute(k, String(v));
  return el;
}

// Text widths in the page's own fonts, kept; cleared once the fonts have loaded.
const deckMeasure = (() => {
  const cache = new Map();
  let ctx = null;
  const M = (text, size, font) => {
    const key = `${font}|${size}|${text}`;
    let w = cache.get(key);
    if (w === undefined) {
      if (!ctx) ctx = document.createElement("canvas").getContext("2d");
      ctx.font = DECK_FONTS[font](size);
      w = ctx.measureText(String(text)).width;
      if (cache.size > 8000) cache.clear();
      cache.set(key, w);
    }
    return w;
  };
  M.reset = () => cache.clear();
  return M;
})();

function deckShape(s) {
  if (s.k === "rect") return svgEl("rect", { x: s.x, y: s.y, width: Math.max(0, s.w), height: Math.max(0, s.h), rx: s.r, class: s.c });
  if (s.k === "circle") return svgEl("circle", { cx: s.cx, cy: s.cy, r: s.r, class: s.c });
  if (s.k === "path") return svgEl("path", { d: s.d, class: s.draw ? `${s.c} dkv-draw` : s.c, pathLength: s.draw ? 1 : null });
  if (s.k === "icon") {
    const o = svgEl("svg", { x: s.x, y: s.y, width: s.s, height: s.s, class: `dkv-ic ${s.c}`.trim(), "aria-hidden": "true", focusable: "false" });
    o.append(svgEl("use", { href: `/static/icons.svg#i-${s.name}` }));
    return o;
  }
  const t = svgEl("text", { x: s.x, "font-size": s.size, "text-anchor": s.a === "start" ? null : s.a, class: `dkf-${s.f}${s.c ? " " + s.c : ""}`, "data-count": s.count });
  if (s.f === "mono") t.setAttributeNS(DECK_XMLNS, "xml:space", "preserve");
  s.lines.forEach((ln, i) => {
    const row = svgEl("tspan", { x: s.x, y: n1(s.y + i * s.lh + s.lh / 2 + 0.35 * s.size) });
    if (Array.isArray(ln)) {
      for (const tk of ln) {
        const k = svgEl("tspan", { class: tk.c ? `dkv-tk-${tk.c}` : null });
        k.textContent = tk.t;
        row.append(k);
      }
    } else row.textContent = ln;
    t.append(row);
  });
  return t;
}

// One diagram at the size of its box: drawn as SVG from the checked spec, with its text alternative.
let deckFigN = 0;
function deckFigure(v, W, H, tall, src) {
  const fit = deckScale(v, W, H, tall, { M: deckMeasure, S: tall ? DECK_SIZES.tall : DECK_SIZES.wide, src }), lay = fit.lay;
  const alt = deckAlt(v, src), id = `dkf${++deckFigN}`, w = W / fit.s, hh = fit.scroll ? lay.h : H / fit.s;
  const vh = Math.max(hh, lay.h), dy = lay.h < hh ? (hh - lay.h) / 2 : 0;
  const svg = svgEl("svg", { class: `dkv dkv-${v.type}`, viewBox: `0 ${n1(-dy)} ${n1(w)} ${n1(vh)}`, preserveAspectRatio: "xMidYMid meet",
    role: "img", "aria-label": alt.label, focusable: "false" });
  if (fit.scroll) svg.style.setProperty("height", `${n1(lay.h * fit.s)}px`);
  const defs = svgEl("defs"), shadow = svgEl("filter", { id, x: "-15%", y: "-15%", width: "130%", height: "160%" });
  shadow.append(svgEl("feDropShadow", { dx: 0, dy: 4, stdDeviation: 6, "flood-color": "#2B2622", "flood-opacity": 0.11 }));
  defs.append(shadow);
  svg.append(defs);
  const draw = (shapes, cls, k) => {
    const g = svgEl("g", { class: cls, "data-k": k });
    for (const s of shapes) {
      const el = deckShape(s);
      if (s.k === "rect" && /\bdkv-(card|slab)\b/.test(s.c)) el.setAttribute("filter", `url(#${id})`);
      g.append(el);
    }
    return g;
  };
  if (lay.back) svg.append(draw(lay.back, "dkv-back", null));
  (lay.under || []).forEach((u, j) => svg.append(draw(u, "dkv-p dkv-u dk-b", `v${j}`)));
  svg.append(draw(lay.base, "dkv-base", null));
  lay.parts.forEach((p, j) => svg.append(draw(p, "dkv-p dk-b", `v${j}`)));
  const fig = h("figure", { class: "dkv-fig" }, svg, h("figcaption", { class: "vh" }, alt.blocks.map(deckAltBlock)));
  fig.dkScroll = fit.scroll;
  return fig;
}

// Keeps the part being narrated in view when a diagram is taller than its box. The stage is scaled, so
// distances on the screen are turned back into the box's own pixels.
function deckFollow(g, instant) {
  const box = g.closest(".dkv-box.scroll");
  if (!box || !box.clientHeight) return;
  const r = g.getBoundingClientRect(), b = box.getBoundingClientRect(), k = b.height / box.clientHeight || 1;
  const top = box.scrollTop + (r.top - b.top) / k - (box.clientHeight - r.height / k) / 2;
  box.scrollTo({ top: Math.max(0, Math.round(top)), behavior: instant ? "auto" : "smooth" });
}

function deckAltBlock(b) {
  if (b.kind === "table") {
    return h("table", null, h("thead", null, h("tr", null, b.head.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", null, b.rows.map((r) => h("tr", null, r.map((c, i) => (i ? h("td", null, c) : h("th", { scope: "row" }, c)))))));
  }
  if (b.kind === "pre") return h("pre", null, b.text);
  if (b.kind === "p") return b.items.map((x) => h("p", null, x));
  return [b.intro ? h("p", null, b.intro) : null, h(b.kind, null, b.items.map((x) => h("li", null, x)))];
}

// The rings behind the title, the part dividers and the closing scene.
function deckRings() {
  const svg = svgEl("svg", { class: "dk-rings", viewBox: "0 0 600 600", "aria-hidden": "true", focusable: "false" });
  [292, 236, 180, 124].forEach((r, i) => svg.append(svgEl("circle", { cx: 300, cy: 300, r, class: `dk-ring dk-ring${i}` })));
  const orbit = svgEl("g", { class: "dk-orbit" });
  orbit.append(svgEl("circle", { cx: 300, cy: 8, r: 7, class: "dk-sat" }), svgEl("circle", { cx: 536, cy: 300, r: 5, class: "dk-sat dk-sat2" }));
  svg.append(orbit);
  return svg;
}

const deckPad = (n) => String(n).padStart(2, "0");
const deckCap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// One scene of the deck, at the canvas size. Parts that build carry class dk-b and their key in data-k;
// text that must fit carries the sizes it may step down through in data-fit (deckFitScene).
function deckScene(sc, i, X) {
  const { L, D, tall } = X;
  const part = Number.isInteger(sc.part) && D.parts[sc.part] ? D.parts[sc.part] : null;
  const role = sc.kind === "part" ? sc.role : part ? part.role : "intro";
  const dark = sc.kind === "title" || sc.kind === "part" || sc.kind === "end";
  const B = (k, node) => { if (node) { node.classList.add("dk-b"); node.dataset.k = k; } return node; };
  const F = (node, wide, narrow, lines, tallLines) => {
    const s = tall ? narrow : wide;
    node.dataset.fit = s.join(",");
    node.style.setProperty("--dk-fs", `${s[0]}px`);
    const n = tall ? tallLines || lines : lines;
    if (n) node.dataset.lines = String(n);
    return node;
  };
  const kick = (text, ic) => h("p", { class: "dk-kick" }, h("span", { class: "dk-kbar", "aria-hidden": "true" }), ic ? icon(ic, "sm") : null, text);
  const foot = () => h("div", { class: "dk-foot", "aria-hidden": "true" }, h("span", { class: "dk-ft" }, L.title), h("span", { class: "dk-pg" }, `${i + 1} / ${X.n}`));
  const quote = (q, n) => h("blockquote", { class: "dk-q" }, h("span", { class: "dk-qt" }, `“${q}”`), h("span", { class: "dk-qs" }, ` — ${X.src(n)}`));
  let kids = [];
  switch (sc.kind) {
    case "title":
      kids = [deckRings(), h("div", { class: "dk-hero" },
        h("p", { class: "dk-eye" }, icon("deck", "sm"), `${String(L.package || "").toUpperCase()} · ${L.skill_text || ""}`),
        F(h("h1", { class: "dk-h1" }, sc.title), [70, 62, 56, 50, 44, 40], [44, 40, 36, 32, 28], 3, 4),
        sc.summary ? B("s", F(h("p", { class: "dk-sum" }, sc.summary), [25, 23, 21, 19, 18], [19, 18, 17, 16, 15], 4, 7)) : null,
        B("m", h("ul", { class: "dk-meta" },
          h("li", null, icon("clock", "xs"), deckCap(aboutMinutes(X.minutes))),
          h("li", null, icon("deck", "xs"), plural(X.nSlides, "slide")),
          X.nVisuals ? h("li", null, icon("sparkle", "xs"), plural(X.nVisuals, "diagram")) : null,
          h("li", null, icon("shield", "xs"), `Written by ${L.models.author}, checked by ${L.models.gate}`))))];
      break;
    case "agenda": {
      const map = (p) => h("li", { class: `dk-r-${p.role}` }, h("span", { class: "dk-mapi" }, icon(DECK_ICON[p.role] || "deck", "sm")),
        h("span", { class: "dk-mapt" }, h("b", null, p.title), p.items.length ? h("small", null, p.items.join(" · "), p.more ? ` · and ${p.more} more` : "") : null));
      if (sc.outcomes) {
        kids = [kick("In this lesson"), h("div", { class: "dk-ag" },
          h("div", { class: "dk-agl" },
            F(h("h2", { class: "dk-h2" }, sc.title), [46, 42, 38], [32, 30, 28], 2),
            F(h("ol", { class: "dk-out" }, sc.items.map((t, j) => B(`a${j}`, h("li", null, h("span", { class: "dk-on" }, deckPad(j + 1)), h("span", null, t))))),
              [26, 24, 22, 20, 18], [19, 18, 17, 16, 15])),
          B("map", F(h("div", { class: "dk-map" }, h("p", { class: "dk-maph" }, "How it unfolds"), h("ol", null, sc.parts.map(map))), [17, 16, 15, 14], [15, 14, 13]))), foot()];
      } else {
        kids = [kick("Start"), F(h("h2", { class: "dk-h2" }, sc.title), [46, 42, 38], [32, 30, 28], 2),
          F(h("ol", { class: `dk-cards n${sc.parts.length}` }, sc.parts.map((p, j) => B(`p${j}`, h("li", { class: `dk-card dk-r-${p.role}` },
            h("span", { class: "dk-cn" }, deckPad(j + 1)), h("span", { class: "dk-ci" }, icon(DECK_ICON[p.role] || "deck", "sm")), h("b", null, p.title),
            p.items.length ? h("ul", null, p.items.map((t) => h("li", null, t)), p.more ? h("li", { class: "dk-more" }, `and ${p.more} more`) : null) : null)))),
            [20, 19, 18, 17, 16, 15], [16, 15, 14, 13]), foot()];
      }
      break;
    }
    case "part":
      kids = [deckRings(), h("div", { class: "dk-pt" },
        h("span", { class: "dk-big", "aria-hidden": "true" }, deckPad(sc.n)),
        h("div", { class: "dk-ptx" },
          h("p", { class: "dk-eye" }, icon(DECK_ICON[sc.role] || "deck", "sm"), `Part ${sc.n} of ${sc.of}`),
          F(h("h2", { class: "dk-h1" }, sc.title), [66, 60, 54, 48, 42], [42, 38, 34, 30], 2, 3),
          sc.role === "traps"
            ? B("i", h("p", { class: "dk-ptn" }, `${plural(sc.count || 0, "common trap")}, each shown with its correction from the source.`))
            : sc.items.length ? B("i", h("ul", { class: "dk-pchips" }, sc.items.map((t) => h("li", null, t)), sc.more ? h("li", { class: "dk-more" }, `and ${sc.more} more`) : null)) : null))];
      break;
    case "slide": {
      const v = sc.visual, rail = v && !tall && deckRail(v);
      const list = (cls) => h("ul", { class: cls }, sc.bullets.map((b, j) => B(`b${j}`, h("li", null, b))));
      const box = () => { const el = h("div", { class: "dkv-box" }); el.dkVisual = v; return el; };
      let body;
      if (!v) {
        body = h("div", { class: "dk-body dk-novis" },
          F(h("ol", { class: "dk-nb" }, sc.bullets.map((b, j) => B(`b${j}`, h("li", null, h("span", { class: "dk-n" }, deckPad(j + 1)), h("span", { class: "dk-bt" }, b))))),
            [30, 28, 26, 24, 22, 20, 18], [21, 20, 19, 18, 17, 16]),
          h("span", { class: "dk-wm", "aria-hidden": "true" }, icon(DECK_ICON[role] || "deck")));
      } else if (rail) body = h("div", { class: "dk-body dk-railed" }, F(list("dk-rail"), [24, 23, 22, 21, 20, 19, 18, 17], [18]), box());
      else body = h("div", { class: "dk-body dk-full" }, h("div", { class: "vh" }, list("")), box());
      const tip = sc.tip ? B("tip", h("aside", { class: `dk-tip dk-tip-${sc.tip.kind === "trap" ? "trap" : "tip"}` },
        h("span", { class: "dk-tipi", "aria-hidden": "true" }, icon(sc.tip.kind === "trap" ? "alert" : "sparkle")),
        F(h("div", { class: "dk-tipb" }, h("p", { class: "dk-tiph" }, sc.tip.kind === "trap" ? "Exam trap" : "Exam tip"),
          h("p", { class: "dk-tipt" }, sc.tip.text), sc.tip.quote ? quote(sc.tip.quote, sc.tip.source) : null), [19, 18, 17, 16, 15], [15, 14, 13]))) : null;
      kids = [kick(part ? part.title : ""), F(h("h2", { class: "dk-h2" }, sc.title), [46, 42, 38, 34, 30], [30, 28, 26, 24], 2, 3), body, tip, foot()];
      break;
    }
    case "process":
      kids = [kick(part ? part.title : "In practice"), F(h("h2", { class: "dk-h2" }, sc.title), [44, 40, 36, 32], [30, 28, 26, 24], 2, 3),
        h("div", { class: `dk-proc${sc.situation ? "" : " solo"}` },
          sc.situation ? B("s", F(h("div", { class: "dk-sit" }, h("p", { class: "dk-lab" }, icon("pin", "sm"), "The situation"), h("p", { class: "dk-sitt" }, sc.situation)),
            [23, 22, 21, 20, 19, 18, 17, 16], [17, 16, 15, 14])) : null,
          F(h("ol", { class: "dk-steps" }, sc.steps.map((t, j) => B(`s${j}`, h("li", null, h("span", { class: "dk-sn" }, String(j + 1)), h("span", { class: "dk-stt" }, t))))),
            [23, 22, 21, 20, 19, 18, 17, 16, 15], [17, 16, 15, 14, 13])), foot()];
      break;
    case "trap":
      kids = [kick(sc.of > 1 ? `Common trap ${sc.n} of ${sc.of}` : "Common trap", "alert"), h("div", { class: "dk-trap" },
        B("w", F(h("div", { class: "dk-wrong" }, h("p", { class: "dk-lab" }, icon("x", "sm"), "Tempting but wrong"), h("p", { class: "dk-wt" }, sc.wrong)),
          [36, 33, 30, 28, 26, 24, 22, 20], [24, 22, 20, 19, 18, 17])),
        h("span", { class: "dk-turn", "aria-hidden": "true" }, icon("arrow")),
        B("r", F(h("div", { class: "dk-right" }, h("p", { class: "dk-lab" }, icon("check", "sm"), "Instead"), h("p", { class: "dk-rt" }, sc.right),
          sc.quote ? quote(sc.quote, sc.source) : null), [29, 27, 25, 23, 21, 20, 19, 18], [20, 19, 18, 17, 16, 15]))), foot()];
      break;
    case "recap":
      kids = [kick(part ? part.title : "Wrap-up"), F(h("h2", { class: "dk-h2" }, sc.title), [46, 42, 38], [32, 30, 28], 2),
        F(h("ul", { class: `dk-recap${sc.items.length > 4 && !tall ? " two" : ""}` }, sc.items.map((t, j) => B(`r${j}`, h("li", null,
          h("span", { class: "dk-ck", "aria-hidden": "true" }, icon("check", "sm")), h("span", null, t))))),
          [27, 25, 23, 21, 20, 19, 18, 17], [19, 18, 17, 16, 15]), foot()];
      break;
    case "check": {
      const st = X.checks[i] || (X.checks[i] = { k: 0, shown: false });
      const q = sc.items[Math.min(st.k, sc.items.length - 1)];
      const more = st.k + 1 < sc.items.length;
      const reveal = h("button", { class: "btn dk-btn", type: "button", "data-act": "reveal", hidden: st.shown }, icon("eye", "sm"), "Show the answer");
      reveal.addEventListener("click", () => { st.shown = true; X.redraw(more ? "[data-act=next]" : ".dk-ans"); });
      const next = more ? h("button", { class: `btn ${st.shown ? "" : "ghost "}dk-btn`, type: "button", "data-act": "next" }, "Next question", icon("right", "sm")) : null;
      if (next) next.addEventListener("click", () => { st.k += 1; st.shown = false; X.redraw("[data-act=reveal]"); });
      const ring = svgEl("svg", { class: "dk-thr", viewBox: "0 0 120 120", "aria-hidden": "true", focusable: "false" });
      ring.append(svgEl("circle", { cx: 60, cy: 60, r: 52, class: "dk-thr0" }), svgEl("circle", { cx: 60, cy: 60, r: 52, class: "dk-thr1", pathLength: 1 }));
      kids = [kick("Check yourself", "target"), h("div", { class: "dk-chk" },
        F(h("div", { class: "dk-cq" }, h("p", { class: "dk-qn" }, sc.items.length > 1 ? `Question ${st.k + 1} of ${sc.items.length}` : "One question"),
          h("p", { class: "dk-qq" }, q.question), h("div", { class: "dk-acts" }, reveal, next)), [34, 31, 28, 26, 24, 22, 20], [23, 21, 19, 18, 17]),
        st.shown
          ? F(h("div", { class: "dk-ans", tabindex: "-1" }, h("p", { class: "dk-lab" }, icon("check", "sm"), "The answer"), h("p", { class: "dk-at" }, q.answer),
            q.quote ? quote(q.quote, q.source) : null), [25, 23, 21, 20, 19, 18, 17], [18, 17, 16, 15, 14])
          : h("div", { class: "dk-think" }, ring, h("p", null, h("b", null, "Think first."), " Say your answer in your head, then show it."))),
        h("p", { class: "dk-just" }, icon("lock", "xs"), "Just for you: nothing here is recorded or graded."), foot()];
      break;
    }
    case "end":
      kids = [deckRings(), h("div", { class: "dk-endc" },
        h("p", { class: "dk-eye" }, icon("target", "sm"), sc.title || "Your turn"),
        h("h2", { class: "dk-h1" }, "Now show it on your own"),
        h("p", { class: "dk-sum" }, "A new question, written fresh and graded against the same official pages. No hints."),
        h("div", { class: "dk-acts" },
          button("Answer a question, no help", () => act("check", L.package, L.skill), "dk-btn dk-go"),
          linkButton("Read the lesson", `#/lesson/${L.id}/read`, "ghost dk-btn"),
          h("button", { class: "btn ghost dk-btn", type: "button", onclick: () => X.restart() }, icon("refresh", "sm"), "Play from the start")))];
      break;
    default:
      kids = [h("h2", { class: "dk-h2" }, deckSceneTitle(sc))];
  }
  return h("section", { class: `dk-scene dk-k-${sc.kind} dk-r-${role}${dark ? " dark" : ""}` }, kids);
}

// Steps each text down through its sizes until it fits (its lines, or its box), then draws the diagrams
// at the size their boxes were left with.
function deckFitScene(el, X) {
  for (const t of el.querySelectorAll("[data-fit]")) {
    if (!t.getClientRects().length) continue;
    const sizes = t.dataset.fit.split(",").map(Number), lines = Number(t.dataset.lines || 0);
    t.classList.remove("dk-free");
    let ok = false;
    for (const s of sizes) {
      t.style.setProperty("--dk-fs", `${s}px`);
      if (lines) ok = t.scrollHeight <= lines * (parseFloat(getComputedStyle(t).lineHeight) || s * 1.3) + 2;
      else ok = t.scrollHeight <= t.clientHeight + 1 && t.scrollWidth <= t.clientWidth + 1;
      if (ok) break;
    }
    if (!ok) t.classList.add("dk-free");
  }
  for (const box of el.querySelectorAll(".dkv-box")) {
    const W = box.clientWidth, H = box.clientHeight;
    if (!box.dkVisual || W <= 60 || H <= 60) continue;
    const fig = deckFigure(box.dkVisual, W, H, X.tall, X.src);
    box.replaceChildren(fig);
    box.classList.toggle("scroll", fig.dkScroll);
    // a box that scrolls can be reached and scrolled from the keyboard
    if (fig.dkScroll) { box.tabIndex = 0; box.setAttribute("role", "group"); box.setAttribute("aria-label", "The diagram, which scrolls"); }
    else for (const a of ["tabindex", "role", "aria-label"]) box.removeAttribute(a);
  }
}

// The player: plays the scenes on the lesson's own narration, or on a reading clock without it.
let deckFs = null;  // the one deck on the page that full screen changes are for
function deckPlayer(L) {
  const calm = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const phone = window.matchMedia ? matchMedia("(max-width: 640px)") : null;
  let D = L.deck, groups = deckGroups(D), N = D.scenes.length;
  let si = 0, t = 0, whole = true, playing = false, auto = !calm, cc = true, rate = 1;
  let plan = { end: 4000, lines: [], marks: {} }, mode = "clock", aSlide = -1, wantAudio = false, seekTo = null;
  let scene = null, keyed = [], fig = null, followed = null, lastLine = -2, raf = 0, last = 0, ready = false, ovOpen = false, stallT = -1, stallAt = 0;
  let audioJob = null, hearLines = [], cells = [], curFill = null;
  const durs = {}, broken = new Set();
  const srcTitle = (n) => { const s = (L.sources || []).find((x) => x.n === n); return s ? s.title || s.publisher || "an official page" : "an official page"; };
  const texts = (sc) => (sc.kind === "slide" ? (L.slides[sc.slide].narration || []).map((ln) => String(ln.text || "")) : []);
  const audioUrl = (sc) => (sc.kind === "slide" && L.audio && L.audio[sc.slide] && !broken.has(sc.slide) ? `/api/media/${L.audio[sc.slide]}` : null);
  const X = { L, D, n: N, tall: false, src: srcTitle, minutes: 0, nSlides: L.slides.length, nVisuals: 0, checks: {},
    restart: () => { go(0); play(); }, redraw: (sel) => redraw(sel) };
  const count = () => {
    X.D = D; X.n = N;
    X.nVisuals = D.scenes.filter((sc) => sc.kind === "slide" && sc.visual).length;
    X.minutes = D.scenes.reduce((a, sc) => { const p = deckPlan(sc, texts(sc), 0); return a + (isFinite(p.end) ? p.end : 0); }, 0) / 60000;
  };
  count();

  const root = h("div", { class: `dk${calm ? " calm" : ""}`, role: "region", "aria-label": "Present: the narrated slide deck" });
  const stage = h("div", { class: "dk-stage" });
  const canvas = h("div", { class: "dk-canvas" }, stage);
  const ovList = h("div", { class: "dk-ovl" });
  const ov = h("section", { class: "dk-ov", hidden: true, id: `dk-ov-${L.id}`, "aria-label": "Every scene of the deck" },
    h("div", { class: "dk-ovh" }, h("h2", null, "Overview"),
      h("button", { class: "tbtn", type: "button", "aria-label": "Close the overview", title: "Close (Esc)", onclick: () => ovShow(false) }, icon("x", "sm"))),
    ovList);
  const frame = h("div", { class: "dk-frame" }, canvas, ov);
  const capWho = h("span", { class: "who" }), capTx = h("span", { class: "tx" });
  const cap = h("div", { class: "dk-cap" }, capWho, capTx);
  const live = h("p", { class: "vh", "aria-live": "polite" });
  const status = h("p", { class: "dk-status", role: "status" });
  const audio = h("audio", { preload: "auto" });
  const say = (text) => { status.textContent = text || ""; };

  const tbtn = (name, label, onclick) => h("button", { class: "tbtn", type: "button", "aria-label": label, title: label, onclick }, icon(name, "sm"));
  const playBtn = h("button", { class: "tbtn big", type: "button", onclick: () => toggle() });
  const prevBtn = tbtn("skipb", "Previous scene", () => go(si - 1));
  const nextBtn = tbtn("skipf", "Next scene", () => go(si + 1));
  const whereN = h("span", { class: "tt" }), whereG = h("span", { class: "dk-wg" });
  const autoBox = h("input", { type: "checkbox", id: `dk-auto-${L.id}` });
  autoBox.checked = auto;
  const autoLab = h("label", { class: auto ? "pill2 on" : "pill2", for: autoBox.id }, autoBox, "Continue automatically");
  autoBox.addEventListener("change", () => { auto = autoBox.checked; autoLab.classList.toggle("on", auto); });
  const ccBtn = h("button", { class: "pill2 on", type: "button", "aria-pressed": "true", title: "Captions (C)" }, icon("cc", "xs"), "Captions");
  ccBtn.addEventListener("click", () => setCc(!cc));
  const speedBtn = h("button", { class: "pill2", type: "button", "aria-label": "Playback speed 1×" }, "1×");
  speedBtn.addEventListener("click", () => {
    rate = rate === 1 ? 1.25 : 1;
    speedBtn.textContent = rate === 1 ? "1×" : "1.25×";
    speedBtn.setAttribute("aria-label", `Playback speed ${speedBtn.textContent}`);
    try { audio.playbackRate = rate; } catch (e) { /* no audio */ }
  });
  const ovBtn = h("button", { class: "pill2", type: "button", "aria-expanded": "false", "aria-controls": ov.id, title: "Overview (O)" }, icon("deck", "xs"), "Overview");
  ovBtn.addEventListener("click", () => ovShow(!ovOpen));
  const canFs = !!(root.requestFullscreen || root.webkitRequestFullscreen);
  const fsBtn = h("button", { class: "pill2", type: "button", hidden: !canFs, title: "Full screen (F)" }, icon("watch", "xs"), "Full screen");
  fsBtn.addEventListener("click", () => fsToggle());
  const prog = h("div", { class: "dk-prog" });
  const ctl = h("div", { class: "player dk-ctl" },
    h("div", { class: "ptop" }, prevBtn, playBtn, nextBtn, h("span", { class: "dk-where" }, whereN, whereG),
      h("div", { class: "rgt" }, autoLab, ccBtn, speedBtn, ovBtn, fsBtn)),
    prog, status);
  root.append(frame, cap, ctl, live, audio);
  const hear = h("div", { class: "box" });
  const hearPanel = h("section", { class: "panel nowtx dk-hear" }, eyebrow("volume", "What you hear now"), hear);

  // ------------------------------------------------ building the chrome
  function buildProg() {
    cells = [];
    put(prog, groups.map((g) => {
      const own = [];
      for (let k = g.from; k < g.to; k++) {
        const c = h("span", { class: "dk-cell", title: deckSceneTitle(D.scenes[k]) || DECK_KIND[D.scenes[k].kind] }, h("i"));
        c.addEventListener("click", () => go(k));
        own.push(c);
        cells.push(c);
      }
      const seg = h("div", { class: `dk-seg dk-r-${g.role}`, "data-from": g.from, "data-to": g.to }, h("div", { class: "dk-cells", "aria-hidden": "true" }, own),
        h("button", { class: "dk-segl", type: "button", onclick: () => go(g.from), "aria-label": `Go to ${g.label}` }, g.label));
      seg.style.setProperty("flex-grow", String(g.to - g.from));
      return seg;
    }));
  }
  function buildOv() {
    put(ovList, groups.map((g) => h("div", { class: `dk-ovg dk-r-${g.role}` }, h("h3", null, icon(DECK_ICON[g.role] || "deck", "xs"), g.label),
      h("ol", null, D.scenes.slice(g.from, g.to).map((sc, j) => {
        const k = g.from + j;
        return h("li", null, h("button", { type: "button", class: "dk-ovb", "data-n": k, onclick: () => { ovShow(false, true); go(k); } },
          h("span", { class: "dk-ovn" }, String(k + 1)), h("span", { class: "dk-ovt" }, deckSceneTitle(sc) || DECK_KIND[sc.kind]),
          h("span", { class: "dk-ovk" }, sc.kind === "slide" && sc.visual ? `Slide with ${DECK_TYPE[sc.visual.type] || "a diagram"}` : DECK_KIND[sc.kind])));
      })))));
  }
  function ovShow(on, picked) {
    ovOpen = on;
    ov.hidden = !on;
    ovBtn.setAttribute("aria-expanded", String(on));
    ovBtn.classList.toggle("on", on);
    if (on) {
      if (playing) setPlaying(false);
      paintOv();
      const cur = ovList.querySelector(`[data-n="${si}"]`);
      if (cur) { cur.focus(); cur.scrollIntoView({ block: "nearest" }); }
    } else (picked ? playBtn : ovBtn).focus();
  }
  const paintOv = () => { for (const b of ovList.querySelectorAll(".dk-ovb")) { if (Number(b.dataset.n) === si) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current"); } };
  function setCc(on) {
    cc = on;
    root.classList.toggle("nocc", !cc);
    ccBtn.classList.toggle("on", cc);
    ccBtn.setAttribute("aria-pressed", String(cc));
  }
  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  function fsToggle() {
    if (!canFs) return;
    const r = fsEl() ? (document.exitFullscreen || document.webkitExitFullscreen).call(document) : (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
    if (r && r.catch) r.catch(() => say("Full screen is not available here."));
  }
  const onFs = () => {
    const on = fsEl() === root;
    root.classList.toggle("fs", on);
    fsBtn.lastChild.textContent = on ? "Exit full screen" : "Full screen";
  };
  deckFs = onFs;
  if (!deckPlayer.hooked) {
    deckPlayer.hooked = true;
    const relay = () => { if (deckFs) deckFs(); };
    document.addEventListener("fullscreenchange", relay);
    document.addEventListener("webkitfullscreenchange", relay);
  }
  function paintPlay() {
    const sc = D.scenes[si], again = sc.kind === "end";
    const label = playing ? "Pause" : again ? "Play from the start" : "Play";
    playBtn.replaceChildren(icon(playing ? "pause" : again ? "refresh" : "play", "sm"));
    playBtn.setAttribute("aria-label", label);
    playBtn.title = `${label} (space)`;
    root.classList.toggle("playing", playing);
    prevBtn.disabled = si === 0;
    nextBtn.disabled = si === N - 1;
  }
  // Per scene: where we are, the progress bar, the overview, what is heard, and the announcement.
  function chrome(announce) {
    const sc = D.scenes[si], g = groups.find((x) => si >= x.from && si < x.to);
    whereN.textContent = `${si + 1} / ${N}`;
    whereG.textContent = g ? g.label : "";
    cells.forEach((c, k) => {
      c.classList.toggle("done", k < si);
      c.classList.toggle("on", k === si);
      if (k !== si) c.firstChild.style.removeProperty("width");
    });
    curFill = cells[si] ? cells[si].firstChild : null;
    for (const s of prog.children) s.classList.toggle("on", !!g && Number(s.dataset.from) === g.from);
    paintOv();
    if (sc.kind === "slide") {
      const lines = L.slides[sc.slide].narration || [];
      hearLines = lines.map((ln) => h("p", { class: "line" }, h("span", { class: "who" }, presenterLabel(ln.voice)), ln.text));
      put(hear, h("div", { class: "spk" }, h("span", { class: "t" }, icon("volume", "xs"), "Narration"), h("span", { class: "m" }, `slide ${sc.slide + 1} of ${L.slides.length}`)),
        hearLines.length ? hearLines : h("p", { class: "line" }, "This slide has no narration."));
    } else {
      hearLines = [];
      const note = sc.kind === "check" ? "Think, then show the answer. Nothing here is recorded or graded."
        : sc.kind === "end" ? "The deck has ended. The next step is yours."
          : "No narration on this scene: it stays up long enough to read, then the deck moves on.";
      put(hear, h("p", { class: "line" }, note));
    }
    if (announce) live.textContent = `Slide ${si + 1} of ${N}: ${deckSceneTitle(sc) || DECK_KIND[sc.kind]}`;
    paintPlay();
  }

  // ------------------------------------------------ scenes in and out
  // A scene is put in place with no transitions, painted, then released: only then may parts move.
  function show(el) {
    el.classList.add("instant");
    stage.append(el);
    deckFitScene(el, X);
    keyed = Array.from(el.querySelectorAll("[data-k]"));
    fig = el.querySelector("svg.dkv");
    followed = null;
    lastLine = -2;
  }
  function release(el) {
    void el.offsetWidth;
    el.classList.remove("instant", "from-r", "from-l");
  }
  function mount(el, dir) {
    const old = scene;
    scene = el;
    const slide = old && dir && !calm;
    if (slide) el.classList.add(dir > 0 ? "from-r" : "from-l");
    show(el);
    if (!old) return;
    old.inert = true;
    old.setAttribute("aria-hidden", "true");
    old.classList.add("old");
    if (!slide) { old.remove(); return; }
    old.classList.add(dir > 0 ? "to-l" : "to-r");
    setTimeout(() => old.remove(), 650);
  }
  function enter(dir, announce) {
    const sc = D.scenes[si];
    if (sc.kind === "check" || sc.kind === "end") { if (playing) setPlaying(false); whole = true; }
    const el = deckScene(sc, si, X);
    mount(el, dir);
    mode = audioUrl(sc) ? "audio" : "clock";
    plan = deckPlan(sc, texts(sc), mode === "audio" ? durs[sc.slide] || 0 : 0);
    stallT = -1;
    t = 0;
    paint(true, playing ? -1 : 0);  // when playing, even the first part comes in moving
    release(el);
    if (playing) startMedia();
    chrome(announce);
    loop();
  }
  // The same scene again, drawn afresh (a new size, the fonts, a revealed answer): nothing replays.
  function redraw(focusSel) {
    if (!scene) return;
    const el = deckScene(D.scenes[si], si, X);
    el.classList.add("instant");
    scene.replaceWith(el);
    scene = el;
    deckFitScene(el, X);
    keyed = Array.from(el.querySelectorAll("[data-k]"));
    fig = el.querySelector("svg.dkv");
    followed = null;
    lastLine = -2;
    paint(true);
    release(el);
    if (focusSel) { const f = el.querySelector(focusSel); if (f) f.focus(); }
  }
  function go(k) {
    if (!ready || k < 0 || k >= N || k === si) return;
    const dir = k > si ? 1 : -1;
    wantAudio = false;
    if (!audio.paused) audio.pause();
    si = k;
    t = 0;
    whole = !playing;
    seekTo = null;
    enter(dir, true);
  }

  // ------------------------------------------------ playing
  function startMedia() {
    const sc = D.scenes[si], url = audioUrl(sc);
    if (mode !== "audio" || !url) return;
    if (aSlide !== sc.slide || !audio.src.endsWith(url)) { audio.src = url; aSlide = sc.slide; }
    try { audio.playbackRate = rate; } catch (e) { /* not ready */ }
    if (audio.readyState >= 1) { try { audio.currentTime = Math.max(0, t) / 1000; } catch (e) { /* not seekable */ } } else seekTo = Math.max(0, t);
    wantAudio = true;
    stallT = -1;
    const p = audio.play();
    if (p && p.catch) {
      p.catch((e) => {
        if (D.scenes[si] !== sc || mode !== "audio" || !wantAudio) return;
        if (e && e.name === "NotAllowedError") { setPlaying(false); say("Press play to start the narration."); } else if (e && e.name !== "AbortError") fallback(sc);
      });
    }
  }
  // This slide's audio cannot play (not made yet, or the local stand-in): the same cues run on the clock.
  function fallback(sc) {
    broken.add(sc.slide);
    if (D.scenes[si] !== sc) return;
    wantAudio = false;
    if (!audio.paused) audio.pause();
    mode = "clock";
    plan = deckPlan(sc, texts(sc), 0);
    last = performance.now();
  }
  const current = () => { const sc = D.scenes[si]; return sc.kind === "slide" && aSlide === sc.slide && mode === "audio" ? sc : null; };
  audio.addEventListener("loadedmetadata", () => {
    const sc = current();
    if (!sc) return;
    const d = audio.duration;
    if (!(isFinite(d) && d > 0)) { fallback(sc); return; }
    durs[sc.slide] = d * 1000;
    plan = deckPlan(sc, texts(sc), durs[sc.slide]);
    if (seekTo !== null) { try { audio.currentTime = seekTo / 1000; } catch (e) { /* not seekable */ } seekTo = null; }
  });
  audio.addEventListener("error", () => { const sc = current(); if (sc) fallback(sc); });
  audio.addEventListener("ended", () => {
    const sc = current();
    if (!sc) return;
    mode = "hold";
    t = Math.max(t, durs[sc.slide] || 0);
    last = performance.now();
  });
  audio.addEventListener("pause", () => {
    // paused from outside (a headset button, the system's media controls): the deck pauses with it
    if (playing && mode === "audio" && wantAudio && audio.paused && !audio.ended && audio.readyState >= 2) setPlaying(false);
  });
  function setPlaying(on) {
    playing = on;
    if (!on) { wantAudio = false; if (!audio.paused) audio.pause(); }
    paintPlay();
    loop();
  }
  function toggle() { if (playing) { setPlaying(false); say(""); } else play(); }
  function play() {
    if (!ready) return;
    const sc = D.scenes[si];
    if (sc.kind === "end") go(0);
    else if (sc.kind === "check" || (!whole && t >= plan.end && si < N - 1)) go(si + 1);
    if (!audioJob && L.audio && L.audio.some((a) => !a)) {
      say("The narration audio is being made. Until it is ready the deck keeps time with a reading clock.");
      audioJob = ensureAudio(L, { textContent: "" })
        .then(() => { if (root.isConnected) say(""); })
        .catch((e) => { if (root.isConnected) say(`The narration audio could not be made, so the deck keeps time with a reading clock. ${e.message || ""}`.trim()); });
    } else if (!status.textContent.startsWith("The narration audio")) say("");
    if (whole) { whole = false; t = 0; }
    playing = true;
    last = performance.now();
    if (D.scenes[si].kind === "check" || D.scenes[si].kind === "end") playing = false;
    else {
      markViewed(L.id, "present");  // only when a scene really plays; sent at each start, the server keeps one per 30 minutes
      if (mode === "audio") startMedia();
    }
    paintPlay();
    paint(false);
    loop();
  }
  function sceneDone() {
    if (si >= N - 1) { setPlaying(false); return; }
    if (auto) go(si + 1);
    else { setPlaying(false); t = plan.end; say("Press play for the next scene."); }
  }
  function tick(now) {
    raf = 0;
    if (!root.isConnected) { detach(); return; }
    const dt = Math.min(250, Math.max(0, now - last));
    last = now;
    if (playing) {
      if (mode === "audio") {
        if (audio.readyState >= 1) t = audio.currentTime * 1000;
        if (t !== stallT) { stallT = t; stallAt = now; }
        else if (now - stallAt > 10000) fallback(D.scenes[si]);  // the audio never came: go on without it
      } else t += dt * rate;
      if (t >= plan.end) sceneDone();
    }
    paint(false);
    if (playing) raf = requestAnimationFrame(tick);
  }
  function loop() { if (!raf && root.isConnected) { last = performance.now(); raf = requestAnimationFrame(tick); } }

  // ------------------------------------------------ painting the moment
  function countUp(txt) {
    const n = Number(txt.getAttribute("data-count")), row = txt.querySelector("tspan");
    if (!row || !(n > 1)) return;
    const t0 = performance.now(), span = Math.min(1200, 500 + 40 * n);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / span), e = 1 - Math.pow(1 - p, 3);
      row.textContent = String(Math.round(n * e));
      if (p < 1 && txt.isConnected) requestAnimationFrame(step); else row.textContent = String(n);
    };
    requestAnimationFrame(step);
  }
  function paint(quietly, at) {
    const sc = D.scenes[si], now = at === undefined ? t : at;
    let focusV = false, nowV = null;
    for (const el of keyed) {
      const m = plan.marks[el.dataset.k];
      const isIn = whole || !m || now >= m.at;
      const isNow = !whole && !!m && now >= m.at && now < m.till;
      if (isIn !== el.classList.contains("in")) {
        el.classList.toggle("in", isIn);
        if (isIn && !quietly && !calm) for (const c of el.querySelectorAll("[data-count]")) countUp(c);
      }
      if (isNow !== el.classList.contains("now")) el.classList.toggle("now", isNow);
      if (isNow && el.dataset.k.charAt(0) === "v") { focusV = true; if (!nowV && !el.classList.contains("dkv-u")) nowV = el; }
    }
    if (fig) fig.classList.toggle("focus", focusV);
    if (nowV && nowV !== followed) { followed = nowV; deckFollow(nowV, quietly || calm); }
    const li = sc.kind === "slide" && !whole ? deckLineAt(plan.lines, now) : -1;
    if (li !== lastLine) {
      lastLine = li;
      const ln = li >= 0 ? (L.slides[sc.slide].narration || [])[li] : null;
      cap.classList.toggle("on", !!ln);
      capWho.textContent = ln ? presenterLabel(ln.voice) : "";
      capTx.textContent = ln ? ln.text : "";
      hearLines.forEach((p, j) => p.classList.toggle("now", j === li));
    }
    if (curFill) curFill.style.setProperty("width", `${whole || !isFinite(plan.end) ? 100 : Math.max(0, Math.min(100, (100 * now) / plan.end)).toFixed(1)}%`);
  }

  // ------------------------------------------------ keys, size and start
  const onKey = (e) => {
    if (!root.isConnected) { detach(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey || !ready) return;
    const tg = e.target, tag = (tg && tg.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (tg && tg.isContentEditable)) return;
    const k = e.key;
    if (k === "Escape") { if (!ovOpen) return; ovShow(false); }
    else if (ovOpen && k !== "o" && k !== "O") return;  // the overview is a list of buttons: Tab and Enter work there
    else if (k === "ArrowRight" || k === "PageDown") go(si + 1);
    else if (k === "ArrowLeft" || k === "PageUp") go(si - 1);
    else if (k === "Home") go(0);
    else if (k === "End") go(N - 1);
    else if (k === " " || k === "Spacebar") { if (tag === "BUTTON" || tag === "A" || tag === "SUMMARY") return; toggle(); }
    else if (k === "f" || k === "F") fsToggle();
    else if (k === "o" || k === "O") ovShow(!ovOpen);
    else if (k === "c" || k === "C") setCc(!cc);
    else return;
    e.preventDefault();
  };

  function scale() {
    const w = frame.clientWidth;
    if (w > 0) canvas.style.setProperty("--dk-s", String(w / (X.tall ? DECK_TALL.w : DECK_WIDE.w)));
  }
  function fitTall() {
    const tall = !!(phone && phone.matches);
    if (tall === X.tall) return false;
    X.tall = tall;
    root.classList.toggle("tall", tall);
    return true;
  }
  let pending = 0, ro = null;
  const onResize = () => {
    if (pending) return;
    pending = requestAnimationFrame(() => {
      pending = 0;
      if (!root.isConnected) { detach(); return; }
      const flipped = fitTall();
      scale();
      if (flipped) redraw();
    });
  };
  // Leaving the page: nothing outside the player keeps it, its lesson or its sound alive. The next route
  // (stopClock) or the next player calls this; so does any of these watchers that fires after the page is gone.
  function detach() {
    if (ro) { ro.disconnect(); ro = null; }
    window.removeEventListener("resize", onResize);
    if (phone && phone.removeEventListener) phone.removeEventListener("change", onResize);
    document.removeEventListener("keydown", onKey);
    if (state.keys === onKey) state.keys = null;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    if (pending) { cancelAnimationFrame(pending); pending = 0; }
    wantAudio = false;
    if (!audio.paused) audio.pause();
    if (state.deckOff === detach) state.deckOff = null;
  }
  // The new deck once it is ready (the visual deck replacing the derived one): the same place if it can.
  function load(deck) {
    const was = D.scenes[si];
    D = deck;
    L.deck = deck;
    groups = deckGroups(D);
    N = D.scenes.length;
    X.checks = {};
    count();
    buildProg();
    buildOv();
    let k = 0;
    if (was && was.kind === "slide") k = Math.max(0, D.scenes.findIndex((s) => s.kind === "slide" && s.slide === was.slide));
    wantAudio = false;
    if (!audio.paused) audio.pause();
    si = k;
    t = 0;
    whole = !playing;
    if (scene) { scene.remove(); scene = null; }
    if (ready) enter(0, true);
  }
  async function boot() {
    const nav = state.nav;
    while (!root.isConnected) {
      if (state.nav !== nav) return;  // the learner moved on before the page was shown
      await new Promise((r) => requestAnimationFrame(r));
    }
    if (document.fonts && document.fonts.load) {
      const all = Promise.all(DECK_FONT_LOAD.map((f) => document.fonts.load(f))).then(() => true, () => false);
      const done = await Promise.race([all, sleep(1200).then(() => false)]);
      if (!done) all.then(() => { deckMeasure.reset(); if (root.isConnected && ready) redraw(); });
    }
    if (!root.isConnected) return;
    deckMeasure.reset();
    fitTall();
    scale();
    ready = true;
    // Only a player on the page listens: one built for a page the learner already left never gets here.
    if (state.deckOff) state.deckOff();   // an earlier player's watchers
    state.deckOff = detach;
    if (state.keys) document.removeEventListener("keydown", state.keys);
    state.keys = onKey;
    document.addEventListener("keydown", onKey);
    if (window.ResizeObserver) { ro = new ResizeObserver(onResize); ro.observe(frame); }
    else window.addEventListener("resize", onResize);
    if (phone && phone.addEventListener) phone.addEventListener("change", onResize);
    enter(0, false);
  }
  buildProg();
  buildOv();
  setCc(true);
  paintPlay();
  boot();
  return { el: root, hear: hearPanel, load, get played() { return playing || !whole || si > 0; } };
}

function lessonPresent(L) {
  if (!L.slides.length) return panel(h("p", { class: "sub" }, "This lesson has no slides left after the checks."));
  if (!L.deck || !Array.isArray(L.deck.scenes) || !L.deck.scenes.length) return panel(h("p", { class: "sub" }, "This lesson has no deck to show."));
  const P = deckPlayer(L);
  return h("div", { class: "col grow dk-page" }, P.el,
    h("div", { class: "row stackable dk-under" }, P.hear, claimsPanel(L, claimList(L))),
    h("div", { class: "strip" }, h("span", { class: "ic" }, icon("shield", "sm")),
      h("span", { class: "tx" }, h("b", null, "Space plays, the arrow keys move, F is full screen, O the overview, C the captions. "), deckNote(L.deck))));
}

// The right-hand panel of the slide stage: where every claim on the slides comes from.
function claimsPanel(L, claims, tail) {
  return h("section", { class: "panel rp stack" },
    h("div", { class: "panel-h tight" }, eyebrow("file", "Sources for this lesson"), chip(`${L.sources.length}`, "line")),
    claims,
    h("p", { class: "prov" }, icon("shield", "sm"),
      h("span", null, h("b", null, "Where the claims come from. "),
        `Every point in the written lesson carries an exact quote. Slide bullets are a summary of those points, checked element by element by ${L.models.gate}.`)),
    L.blocked.length ? h("p", { class: "conf" }, icon("alert", "xs"),
      h("span", null, `${L.blocked.length} part${L.blocked.length === 1 ? " was" : "s were"} removed. `, h("a", { class: "link", href: "#/quality" }, "See the studio log."))) : null,
    tail || null);
}

function claimList(L) {
  return h("div", null, L.sources.map((s) => h("div", { class: "claim" },
    h("p", { class: "ct" }, s.title || s.url),
    h("div", { class: "cs" }, h("span", { class: "src" }, icon("link", "xs"), h("span", null, s.publisher || "official page"), " ", ext(s.url, "open")),
      sourceCheck(L, s)))));
}

// ---------------------------------------------------------------- watch (the filmed lesson)
// The backend films one video per presenter for the whole lesson and tells us, per slide, the
// speaker turns in speaking order with the second each turn starts and ends inside that presenter's
// file. So we keep one <video> per presenter and seek it; nothing is reloaded between turns.

// The same grouping the backend used when it filmed: consecutive lines of one voice are one turn.
function narrationTurns(sl) {
  const lines = (sl.narration && sl.narration.length)
    ? sl.narration
    : [{ voice: "guide", text: [sl.title].concat(sl.bullets || []).filter(Boolean).join(". ") }];
  const out = [];
  for (const ln of lines) {
    const text = String((ln && ln.text) || "").trim();
    if (!text) continue;
    const speaker = ln && ln.voice === "coach" ? "coach" : "guide";
    if (out.length && out[out.length - 1].speaker === speaker) out[out.length - 1].texts.push(text);
    else out.push({ speaker, texts: [text] });
  }
  return out;
}

const isFilmed = (L) => Array.isArray(L.video) && L.video.length === L.slides.length
  && L.video.every((row) => Array.isArray(row) && row.length > 0 && row.every((t) => t && t.media));

/* The warm-up before a filmed lesson, and the one pause in the middle (design 03, INV-7): the lesson
   asks before it explains. What the learner writes or says is theirs, not evidence. It never leaves
   this browser tab - only the recording goes out, to be written down, exactly as anywhere else - and
   it is gone when the browser session ends. */
const WARMUP_NOTE = "A warm-up for you. Dojo does not keep, grade or record it.";
const warmupKey = (id) => `dojo.warmup.${id}`;
function warmupSaid(id) {
  try { return JSON.parse(sessionStorage.getItem(warmupKey(id)) || "null"); } catch (e) { return null; }
}
function warmupKeep(id, answer) {
  try { sessionStorage.setItem(warmupKey(id), JSON.stringify({ answer: (answer || "").trim() })); } catch (e) { /* storage off */ }
}

function lessonWatch(L) {
  if (!L.slides.length) return panel(h("p", { class: "sub" }, "This lesson has no slides left after the checks."));
  const host = h("div", { class: "row stackable grow" });
  // A lesson written again on the deeper template keeps its earlier film until the owner has it filmed again (ADR 0008).
  const E = !isFilmed(L) && L.earlier_film && isFilmed(L.earlier_film) ? L.earlier_film : null;
  const mount = () => put(host, isFilmed(L) ? watchPlayer(L) : E ? earlierWatch(L, E) : filmPane(L, mount),
    claimsPanel(E || L, claimList(E || L), null));
  mount();
  return host;
}

// The video of the earlier version, said plainly. Filming the new version costs money, so it happens
// only from Studio's Film again button, which shows the price first: there is no film button here.
function earlierWatch(L, E) {
  return h("div", { class: "col grow" },
    h("section", { class: "panel" },
      h("div", { class: "panel-h" }, eyebrow("video", "Watch", "amber"), chip(E.remaking ? "Being filmed again" : "Earlier version", "amber")),
      h("p", { class: "sub" }, E.note),
      h("div", { class: "actions top" }, linkButton("Read the new version", `#/lesson/${L.id}/read`, "ghost"),
        linkButton("Listen to it", `#/lesson/${L.id}/listen`, "ghost"), linkButton("Present it", `#/lesson/${L.id}/present`, "ghost"))),
    watchPlayer(E));
}

// Nothing filmed yet (or a presenter is missing): one button, then the job's own words.
// A lesson written again after its page changed says why its video is missing (ADR 0006).
function filmPane(L, onFilmed) {
  const film = L.drift && L.drift.state === "rewritten" ? L.drift.film : null;
  const instead = [linkButton("Present it instead", `#/lesson/${L.id}/present`, "ghost"),
    linkButton("Read it instead", `#/lesson/${L.id}/read`, "ghost"),
    linkButton("Listen instead", `#/lesson/${L.id}/listen`, "ghost")];
  if (film && film.state === "remaking") {
    return h("div", { class: "col grow" }, h("section", { class: "panel" },
      eyebrow("video", "Watch", "amber"),
      h("h2", null, "The video is being remade"),
      h("p", { class: "sub" }, film.text),
      h("div", { class: "actions top" }, instead)));
  }
  if (!isAdmin()) {
    // Filming costs money, so the owner of this Dojo decides it (ADR 0009).
    return h("div", { class: "col grow" }, h("section", { class: "panel" },
      eyebrow("video", "Watch", "amber"),
      h("h2", null, "This lesson has not been filmed yet"),
      h("p", { class: "sub" }, "Filming costs money, so the owner of this Dojo decides when a lesson is filmed. Read or listen to it meanwhile."),
      h("div", { class: "actions top" }, instead[1], instead[2])));
  }
  const status = h("p", { class: "small muted" },
    "Each presenter is filmed once for the whole lesson, so it only happens on the first watch.");
  const btn = button("Film this lesson (about 2 minutes)", async () => {
    btn.disabled = true;
    status.textContent = "Starting…";
    try {
      const res = await runJob(api(`/api/lessons/${L.id}/video`, { method: "POST" }),
        (step, secs) => { status.textContent = `${step} · ${secs}s`; });
      L.video = (res && res.video) || L.video;
      if (!isFilmed(L)) { status.textContent = "The film came back without every turn. Try again."; btn.disabled = false; return; }
      onFilmed();
    } catch (e) { status.textContent = e.message; btn.disabled = false; }
  }, "amber");
  return h("div", { class: "col grow" }, h("section", { class: "panel" },
    eyebrow("video", "Watch", "amber"),
    h("h2", null, film ? "This version has not been filmed yet" : "This lesson has not been filmed yet"),
    film ? h("p", { class: "sub" }, film.text) : null,
    film && film.error ? h("p", { class: "conf" }, icon("alert", "xs"), h("span", null, `The last try did not work: ${film.error}`)) : null,
    h("p", { class: "sub" },
      `Two presenters read the narration you can already listen to: ${PRESENTER.guide.role} ${PRESENTER.guide.name} explains, `
      + `${PRESENTER.coach.role.toLowerCase()} ${PRESENTER.coach.name} asks what a learner would ask. Nothing new is written; the words are the checked ones.`),
    h("div", { class: "actions top" }, btn, film ? instead : [instead[1], instead[2]]),
    status));
}

function watchPlayer(L) {
  const n = L.slides.length;
  const clips = L.video;
  const caps = L.slides.map(narrationTurns);
  const media = {};
  clips.forEach((row) => row.forEach((t) => { if (t.media && !media[t.speaker]) media[t.speaker] = t.media; }));
  const order = ["guide", "coach"].filter((sp) => media[sp]);
  const videos = {}, cards = {}, broken = {};
  const cast = h("div", { class: "cast" });
  for (const sp of order) {
    const v = h("video", { class: "pv", preload: "auto", playsinline: true, src: `/api/media/${media[sp]}` });
    v.addEventListener("error", () => { broken[sp] = true; });
    v.addEventListener("timeupdate", tick);
    videos[sp] = v;
    cards[sp] = h("div", { class: "pres" }, v, h("span", { class: "who" }, presenterLabel(sp)));
    cast.append(cards[sp]);
  }
  const slideBox = h("div", { class: "slidebox" });
  const stage = h("div", { class: "stage watch", tabindex: "0", "aria-live": "polite", "aria-label": "Slide" }, slideBox, cast);
  const caption = h("p", { class: "capline", "aria-live": "polite" });
  const status = h("span", { class: "small muted" });
  const tt = h("span", { class: "tt" });
  const fill = h("div", { class: "cfill" });
  const head = h("div", { class: "chead" });
  const track = h("div", { class: "ctrack" }, fill, head);
  const labels = h("div", { class: "clabels" });
  const calm = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let si = 0, ti = 0, playing = false, rate = 1, raf = 0, timer = 0, ended = false, cc = true;
  // One pause, at the slide boundary nearest the middle. A short lesson, or one with a single
  // self-check question, is not interrupted at all.
  const midAt = (L.self_check.length >= 2 && n >= 4) ? Math.round(n / 2) : -1;
  let overlay = null, overKind = "", overMic = null, midShown = false, midResume = false;

  for (let k = 1; k < n; k++) {
    const tick2 = h("div", { class: "ctick" });
    tick2.style.setProperty("left", `${(k * 100 / n).toFixed(2)}%`);
    track.append(tick2);
  }
  labels.style.setProperty("grid-template-columns", `repeat(${n}, 1fr)`);
  const labelEls = L.slides.map((sl, k) => {
    const el = h("button", { class: "clab", type: "button" }, h("span", { class: "num" }, `SLIDE ${k + 1}`), sl.title);
    el.addEventListener("click", () => go(k));
    return el;
  });
  put(labels, labelEls);

  const turnsOn = (k) => clips[k] || [];
  const capOn = (k, t) => { const c = caps[k] || []; return c[t] ? c[t].texts.join(" ") : ""; };
  const stop = () => { if (raf) cancelAnimationFrame(raf); raf = 0; if (timer) clearTimeout(timer); timer = 0; };
  const pauseAll = () => { for (const sp of order) { try { videos[sp].pause(); } catch (e) { /* stub media */ } } };

  function paintSlide() {
    const sl = L.slides[si];
    put(slideBox, h("div", { class: "slide" },
      h("p", { class: "kicker" }, icon("sparkle", "xs"), L.title),
      h("span", { class: "pgn" }, `${si + 1} / ${n}`),
      h("h3", { class: "st" }, sl.title),
      h("ul", { class: "blist" }, sl.bullets.map((b) => h("li", null, h("span", { class: "dotm" }), h("span", null, b)))),
      h("p", { class: "srcline" }, `Grounded in ${L.sources.length} official page${L.sources.length === 1 ? "" : "s"}; checked by ${L.models.gate}.`)));
    const pct = `${((si + 0.5) * 100 / n).toFixed(2)}%`;
    fill.style.setProperty("width", pct);
    head.style.setProperty("left", pct);
    labelEls.forEach((el, k) => el.classList.toggle("now", k === si));
    paintTurn();
  }
  function paintTurn() {
    const t = turnsOn(si)[ti];
    const count = turnsOn(si).length;
    tt.textContent = count ? `Slide ${si + 1} of ${n} · turn ${Math.min(ti + 1, count)} of ${count}` : `Slide ${si + 1} of ${n}`;
    for (const sp of order) cards[sp].classList.toggle("on", !!t && t.speaker === sp);
    put(caption, t ? [h("span", { class: "who" }, presenterLabel(t.speaker)), capOn(si, ti)] : []);
  }
  function paintPlay() {
    put(playBtn, icon(playing ? "pause" : "play", "sm"));
    const label = playing ? "Pause" : "Play the lesson";
    playBtn.setAttribute("aria-label", label);
    playBtn.setAttribute("title", label);
  }
  function playTurn() {
    stop();
    const t = turnsOn(si)[ti];
    if (!t) { endOfSlide(); return; }
    paintTurn();
    if (!playing) return;
    for (const sp of order) if (sp !== t.speaker) { try { videos[sp].pause(); } catch (e) { /* stub media */ } }
    const v = videos[t.speaker];
    if (v && !broken[t.speaker] && t.start !== null && t.end !== null) {
      try { v.currentTime = t.start; v.playbackRate = rate; } catch (e) { /* stub media */ }
      const p = v.play();
      if (p && typeof p.catch === "function") p.catch(() => { broken[t.speaker] = true; byClock(); });
      raf = requestAnimationFrame(frame);
    } else byClock();
  }
  // The stop is driven by the clip's own clock; a frame check catches the end even when timeupdate
  // is sparse, and a plain timer takes over if a file will not play at all.
  function frame() {
    raf = 0;
    if (!playing) return;
    const t = turnsOn(si)[ti];
    if (!t || broken[t.speaker]) return;
    const v = videos[t.speaker];
    if (!v) return;
    if (t.end !== null && v.currentTime >= t.end - 0.04) { try { v.pause(); } catch (e) { /* stub media */ } nextTurn(); return; }
    raf = requestAnimationFrame(frame);
  }
  function tick() {
    if (!playing || raf === 0) return;
    const t = turnsOn(si)[ti];
    if (!t || broken[t.speaker] || t.end === null) return;
    const v = videos[t.speaker];
    if (v && v.currentTime >= t.end - 0.04) { try { v.pause(); } catch (e) { /* stub media */ } stop(); nextTurn(); }
  }
  function byClock() {
    stop();
    const t = turnsOn(si)[ti];
    if (!t) return;
    const span = (t.start !== null && t.end !== null && t.end > t.start) ? t.end - t.start : Math.max(2, capOn(si, ti).length / 14);
    timer = setTimeout(nextTurn, Math.max(600, (span * 1000) / rate));
  }
  function nextTurn() {
    stop();
    if (ti + 1 < turnsOn(si).length) { ti += 1; playTurn(); return; }
    endOfSlide();
  }
  function endOfSlide() {
    stop();
    pauseAll();
    if (si + 1 >= n) { finish(); return; }
    si += 1; ti = 0;
    paintSlide();
    if (playing && si === midAt && !midShown) { pauseToThink(); return; }
    if (calm) { playing = false; paintPlay(); status.textContent = "Next slide when you are ready."; return; }
    if (playing) playTurn();
  }
  function finish() {
    playing = false; ended = true;
    leaveOver();
    stop(); pauseAll(); paintPlay(); paintTurn();
    status.textContent = "That is the whole lesson.";
    paintSaid();
    yourTurn.hidden = false;
    yourTurn.focus();
  }
  function go(to) {
    if (to < 0 || to >= n) return;
    stop(); pauseAll();
    leaveOver();
    si = to; ti = 0; ended = false;
    yourTurn.hidden = true;
    paintSlide();
    if (playing) playTurn();
  }
  // Every move from paused to playing is a Watch start. The server keeps one event per lesson and
  // way of viewing per half hour, so coming back after a long think does not leave the teaching
  // clock behind: what a later check compares itself with is the last time the lesson really taught.
  function startPlaying() {
    playing = true;
    paintPlay();
    markViewed(L.id, "watch");
    status.textContent = "";
    playTurn();
  }
  function toggle() {
    leaveOver();  // pressing play is "now watch"
    if (ended) { go(0); }
    if (playing) { playing = false; paintPlay(); stop(); pauseAll(); paintTurn(); }
    else startPlaying();
  }

  // ------------------------------------------------ the card over the slide (warm-up, and the pause)
  function closeOver() {
    if (overMic) { overMic.dispose(); overMic = null; }  // a card that leaves stops listening
    if (overlay) { overlay.remove(); overlay = null; }
    overKind = "";
    dropLostMicrophones();
  }
  function openOver(kind, make) {
    closeOver();          // the old card leaves first, with the microphone it had
    const card = make();  // and only then is the new one built: its Speak button is not a lost one
    overlay = card;
    overKind = kind;
    hold.append(card);
    if (card.isConnected) card.focus();
    else requestAnimationFrame(() => { if (card.isConnected && overlay === card) card.focus(); });
  }
  // Leaving the card by any other route: the warm-up keeps what is in the box, the pause just goes.
  function leaveOver() {
    if (overKind === "warmup") closeWarmUp(true); else closeOver();
  }
  // Keeps what is in the box, in this tab only, and takes the card away. `start` is "Now watch".
  function closeWarmUp(start) {
    const box = overlay ? overlay.querySelector("textarea") : null;
    if (box) warmupKeep(L.id, box.value);
    closeOver();
    paintSaid();
    if (!start) { status.textContent = "Skipped the warm-up. Play when you are ready."; playBtn.focus(); }
  }
  function warmUpCard() {
    const ta = h("textarea", { rows: 4, maxlength: 2000, id: "warmup",
      placeholder: "Say or write what you think. A sentence is enough." });
    overMic = canSpeak() ? speakBox(ta, { item: "ask" }) : null;
    return h("section", { class: "turnq", tabindex: "-1", role: "group",
      "aria-label": `Before ${PRESENTER.guide.name} explains, your turn` },
      eyebrow("target", `Before ${PRESENTER.guide.name} explains — your turn`, "amber"),
      h("p", { class: "q" }, L.self_check[0].question),
      h("label", { class: "skip", for: "warmup" }, "Your answer before the lesson"),
      ta,
      overMic ? overMic.el : null,
      h("div", { class: "actions top" },
        button("Now watch", () => { closeWarmUp(true); if (!playing) toggle(); stage.focus(); }),
        button("Skip, just watch", () => closeWarmUp(false), "ghost")),
      h("p", { class: "warm" }, icon("shield", "xs"), WARMUP_NOTE));
  }
  function pauseToThink() {
    midShown = true;
    midResume = playing && !calm;
    playing = false;
    stop(); pauseAll(); paintPlay();
    status.textContent = "Paused for you to think.";
    const card = h("section", { class: "turnq", tabindex: "-1", role: "group", "aria-label": "A pause to think" },
      eyebrow("clock", "Think it over, then Continue", "amber"),
      h("p", { class: "q" }, L.self_check[1].question),
      h("div", { class: "actions top" }, button("Continue", continueLesson)),
      h("p", { class: "warm" }, icon("shield", "xs"), "Answer it in your head. Nothing here is recorded."));
    card.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); continueLesson(); } });
    openOver("mid", () => card);
  }
  function continueLesson() {
    closeOver();
    if (midResume) { startPlaying(); stage.focus(); }
    else { status.textContent = "Play when you are ready."; playBtn.focus(); }
  }
  function paintSaid() {
    const w = warmupSaid(L.id);
    const text = w && w.answer ? w.answer : "";
    saidBox.hidden = !text;
    if (!text) { put(saidBox); return; }
    put(saidBox,
      h("p", { class: "eyebrow" }, "What you said before you watched"),
      h("blockquote", { class: "saidq mine" }, text),
      h("p", { class: "small muted" }, "Yours to compare with, nothing more. It was never kept, graded or recorded."));
  }

  const tbtn = (name, label, onclick, cls) => h("button", { class: cls ? `tbtn ${cls}` : "tbtn", type: "button", "aria-label": label, title: label, onclick }, icon(name, "sm"));
  const playBtn = h("button", { class: "tbtn big", type: "button", onclick: toggle }, icon("play", "sm"));
  const ccBtn = h("button", { class: "pill2 on", type: "button", "aria-pressed": "true" }, icon("cc", "xs"), "Captions");
  ccBtn.addEventListener("click", () => {
    cc = !cc;
    caption.hidden = !cc;
    ccBtn.classList.toggle("on", cc);
    ccBtn.setAttribute("aria-pressed", String(cc));
  });
  const speedBtn = h("button", { class: "pill2", type: "button", "aria-label": "Playback speed" }, "1×");
  speedBtn.addEventListener("click", () => {
    rate = rate === 1 ? 1.25 : 1;
    speedBtn.textContent = rate === 1 ? "1×" : "1.25×";
    for (const sp of order) { try { videos[sp].playbackRate = rate; } catch (e) { /* stub media */ } }
  });
  stage.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); go(si + 1); }
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); go(si - 1); }
    if (e.key === " ") { e.preventDefault(); toggle(); }
  });
  const saidBox = h("div", { class: "saidfirst", hidden: true });
  const yourTurn = h("section", { class: "panel yt", hidden: true, tabindex: "-1" },
    h("div", { class: "panel-h" }, eyebrow("target", "Your turn", "amber"), chip("no hints", "line")),
    h("h2", null, "Now say it in your own words"),
    L.self_check.length
      ? [h("p", { class: "small muted" }, "You thought these through while you watched. The question you get is written fresh and graded against the same official pages, quoting you."),
        h("ul", { class: "points" }, L.self_check.map((q) => h("li", null, h("span", { class: "dotm" }), h("span", null, q.question)))),
        saidBox]
      : h("p", { class: "small muted" }, "The question you get is written fresh and graded against the same official pages, quoting you."),
    h("div", { class: "actions top" },
      button("Answer a question, no help", () => act("check", L.package, L.skill)),
      linkButton("Read the lesson", `#/lesson/${L.id}/read`, "ghost")));
  const hold = h("div", { class: "stagehold" }, stage);
  const wrap = h("div", { class: "stagewrap grow" }, hold, caption,
    h("div", { class: "player" },
      h("div", { class: "ptop" },
        tbtn("skipb", "Previous slide", () => go(si - 1)),
        playBtn,
        tbtn("skipf", "Next slide", () => go(si + 1)),
        tt,
        h("div", { class: "rgt" }, ccBtn, speedBtn,
          h("button", { class: "pill2", type: "button", onclick: () => { if (wrap.requestFullscreen) wrap.requestFullscreen(); stage.focus(); } }, icon("watch", "xs"), "Full screen"),
          status)),
      h("div", { class: "chapters" }, track, labels)),
    yourTurn,
    h("div", { class: "strip" }, h("span", { class: "ic" }, icon("shield", "sm")),
      h("span", { class: "tx" }, h("b", null, "Space plays, arrow keys move. "),
        `${PRESENTER.guide.role} ${PRESENTER.guide.name} and ${PRESENTER.coach.role.toLowerCase()} ${PRESENTER.coach.name} are synthetic presenters reading the checked narration. They add nothing of their own.`)));
  paintSlide();
  paintPlay();
  paintSaid();
  // The lesson asks before it explains: the first self-check question, once per lesson per browser session.
  if (L.self_check.length && !warmupSaid(L.id)) openOver("warmup", warmUpCard);
  return wrap;
}

// ---------------------------------------------------------------- saying your answer out loud

const SPEECH_PRIVACY = "https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/speech-service/speech-to-text/data-privacy-security";
const RECORD_LIMIT_MS = 180000;
const NOTICE_KEY = "dojo.mic.notice";
const MIC_ERRORS = {
  NotAllowedError: "Edge is blocking the microphone. Click the padlock left of the address bar, set Microphone to Allow, then try again. You can always type instead.",
  SecurityError: "Edge is blocking the microphone. Click the padlock left of the address bar, set Microphone to Allow, then try again. You can always type instead.",
  NotFoundError: "No microphone was found. Plug one in, or type your answer.",
  DevicesNotFoundError: "No microphone was found. Plug one in, or type your answer.",
  NotReadableError: "Another program is using the microphone. Close it and try again, or type your answer.",
};
const canSpeak = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

function recordingType() {
  const wanted = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4", "audio/mpeg"];
  for (const t of wanted) {
    if (!MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

const clock = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
const words = (text) => (text || "").toLowerCase().match(/[a-z0-9']+/g) || [];

function micNotice(onStart, onCancel, float) {
  return h("div", { class: float ? "notice float" : "notice" }, eyebrow("mic", "Before you speak"),
    h("p", { class: "small ink2" }, "Dojo sends the recording to Azure AI Speech to write it down. Your audio and the raw transcript are not kept by Dojo. The answer you send is kept like a typed answer."),
    h("p", { class: "small ink2" }, "You can always type instead."),
    h("p", { class: "small" }, ext(SPEECH_PRIVACY, "What Microsoft says about speech-to-text data")),
    h("div", { class: "actions top" }, button("Start recording", onStart, "sm"), button("Not now", onCancel, "ghost sm")));
}

/* Every Speak button on the page right now. When a view is replaced the buttons that went with it are
   gone from the screen, so their microphones must stop too: nobody can see or cancel them any more.
   A button that has not been put on the page yet is not lost, it is still being built, so it is left
   alone until it has been seen there once. */
const speakers = new Set();
function dropLostMicrophones() {
  for (const s of Array.from(speakers)) {
    if (s.el.isConnected) { s.mounted = true; continue; }
    if (!s.mounted) continue;
    s.dispose();
    speakers.delete(s);
  }
}

/* Going to another page, or leaving the site, stops every recording - the header box too. */
function stopEveryMicrophone() {
  for (const s of Array.from(speakers)) s.dispose();
  dropLostMicrophones();
}

/* Is a microphone on anywhere on the page? Nothing may interrupt a learner who is speaking. */
const anyRecording = () => Array.from(speakers).some((s) => s.el.isConnected && s.recording());

/* One microphone at a time. Two recorders on one page would fight over the device and over the
   learner's attention - the warm-up box and the header Ask box are both a Speak button away. The
   one that is already listening keeps it; the other says so and waits. */
const micInUse = (except) => Array.from(speakers).some((s) => s !== except && s.el.isConnected && s.holding());
const MIC_TAKEN = "Another microphone is on. Stop that recording first, then try again.";

/* A Speak button for one text box. The words are put into the box; nothing is sent for you.
   Returns the element plus `used()`, `changed()` and `receipts()`, so the form can record how the
   answer was given, and `dispose()` to let go of the microphone. */
function speakBox(target, opts = {}) {
  const compact = !!opts.compact;
  const label = opts.label || "Speak your answer";
  const placeholder = target.getAttribute("placeholder") || "";
  const status = h("p", { class: compact ? "small muted mic-status skip" : "small muted mic-status", role: "status", "aria-live": "polite" });
  const bar = h("span", { class: "bar" });
  const level = h("span", { class: "lvl", "aria-hidden": "true" }, bar);
  const stop = button("Stop", () => finish(true), "sm");
  const cancel = button("Cancel", () => finish(false), "ghost sm");
  const toggle = () => (run && run.rec.state === "recording" ? finish(true) : begin());
  const start = compact
    ? h("button", { class: "mic", type: "button", "aria-label": label, title: label, onclick: toggle }, icon("mic", "sm"))
    : button([icon("mic", "sm"), "Speak"], toggle, "ghost sm");
  const live = h("div", { class: "mic-live" }, level, stop, cancel);
  const box = h("div", { class: compact ? "speak compact" : "speak" }, start, live, status);
  const tickets = [];
  const pieces = [];  // every bit of text a recording put into the box
  /* `run` is the recording that is happening now, with its own stream, recorder, chunks and clock.
     Handlers keep a reference to their own recording, so a stop event that arrives late can only ever
     close its own microphone - never the one a newer recording just opened. */
  let corrected = false, asHeard = "", run = null, gen = 0, sending = null, notice = null, starting = false;
  // Holding the device, or about to: from asking the browser for it until the recorder stops.
  const holding = () => starting || !!(run && run.rec && run.rec.state === "recording");

  function say(text) {
    status.textContent = text || "";
    if (compact) target.setAttribute("placeholder", text || placeholder);  // the header box has no room for a line of status
  }

  function show(mode, text) {
    box.className = `${compact ? "speak compact" : "speak"} ${mode}`;
    start.disabled = mode === "starting" || mode === "writing" || mode === "asked";
    if (compact) start.title = mode === "recording" ? "Stop recording" : label;
    if (mode === "recording") hideNewBuild();  // nothing interrupts a learner who is speaking
    else if (mode === "idle") setTimeout(offerNewBuild, 0);
    say(text);
  }

  function meter(r) {
    if (!r.audio) return;
    const data = new Uint8Array(r.audio.analyser.frequencyBinCount);
    r.audio.analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
    bar.style.setProperty("width", `${Math.min(100, Math.round((peak / 90) * 100))}%`);
  }

  function tick(r) {
    if (run !== r) { release(r); return; }  // a clock left over from an older recording stops itself
    if (!box.isConnected) { finish(false); return; }  // the button left the page: stop listening at once
    const ms = Date.now() - r.startedAt;
    if (ms >= RECORD_LIMIT_MS) { finish(true, "Three minutes is the limit. Writing down what you said…"); return; }
    meter(r);
    say(`Recording · ${clock(ms)} · Stop when you are done`);
  }

  /* Let go of one recording's microphone and clock. It touches nothing but its own. */
  function release(r) {
    window.clearInterval(r.ticker);
    r.ticker = 0;
    if (r.audio) { try { r.audio.ctx.close(); } catch (e) { /* already closed */ } r.audio = null; }
    if (r.stream) { r.stream.getTracks().forEach((t) => t.stop()); r.stream = null; }
  }

  function done(r) {  // this recording is over: the Speak button is free for the next one
    release(r);
    if (run === r) run = null;
  }

  function listen(stream) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      return { ctx, analyser };
    } catch (e) { return null; }
  }

  function closeNotice(focus) {
    if (notice) { notice.remove(); notice = null; }
    show("idle", "");
    if (focus) start.focus();
  }

  async function begin() {
    if (notice || run) return;  // one notice, one recording at a time
    if (micInUse(handle)) { show("idle", MIC_TAKEN); return; }  // and one on the whole page
    if (!sessionStorage.getItem(NOTICE_KEY)) {
      notice = micNotice(() => { sessionStorage.setItem(NOTICE_KEY, "1"); closeNotice(false); begin(); }, () => closeNotice(true), compact);
      if (compact) document.body.append(notice); else box.before(notice);  // the header form is too small to hold it
      show("asked", "");  // the Speak button waits until the notice is answered
      notice.querySelector("button").focus();
      return;
    }
    const mine = ++gen;
    show("starting", "Asking for the microphone…");
    starting = true;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      starting = false;
      if (mine === gen) show("idle", MIC_ERRORS[e && e.name] || "Dojo could not start recording. Type your answer instead.");
      return;
    }
    starting = false;
    if (mine !== gen || run || micInUse(handle)) {  // the view is gone, or another recording got there first
      stream.getTracks().forEach((t) => t.stop());
      if (mine === gen && !run) show("idle", MIC_TAKEN);
      return;
    }
    const r = { mine, stream, rec: null, chunks: [], audio: null, ticker: 0, startedAt: 0, dropped: false };
    const type = recordingType();
    try {
      r.rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    } catch (e) {
      release(r);
      show("idle", "This browser cannot record audio here. Type your answer instead.");
      return;
    }
    run = r;
    r.rec.addEventListener("dataavailable", (e) => { if (e.data && e.data.size) r.chunks.push(e.data); });
    r.rec.addEventListener("stop", () => write(r));
    r.rec.start();
    r.audio = listen(stream);
    r.startedAt = Date.now();
    r.ticker = window.setInterval(() => tick(r), 250);
    show("recording", "Recording · 0:00 · Stop when you are done");
    stop.focus();
  }

  /* Let go of the microphone at once and throw the recording away. Used when the view that holds this
     button is replaced, and when the answer is sent: a recorder nobody can see must not keep running. */
  function dispose() {
    gen += 1;  // a microphone request still in flight is now orphaned
    starting = false;
    const r = run;
    if (r) {
      r.dropped = true;
      if (r.rec.state !== "inactive") {
        try { r.rec.stop(); } catch (e) { /* already stopping */ }
      }
      r.chunks = [];
      done(r);
    }
    if (sending) { try { sending.abort(); } catch (e) { /* already done */ } sending = null; }
    if (notice) { notice.remove(); notice = null; }
    show("idle", "");
  }

  function finish(keep, note) {
    const r = run;
    if (!r || r.rec.state === "inactive") return;
    r.dropped = !keep;
    window.clearInterval(r.ticker);
    r.ticker = 0;
    r.rec.stop();
    if (keep) show("writing", note || "Writing down what you said…");
    else { done(r); show("idle", "Cancelled. Nothing was sent."); start.focus(); }
  }

  async function write(r) {
    const type = (r.rec.mimeType || "audio/webm").split(";")[0].trim();
    const blob = new Blob(r.chunks, { type });
    r.chunks = [];
    release(r);
    if (r.dropped || r.mine !== gen || run !== r) return;  // cancelled or left behind: it never leaves the browser
    if (!blob.size) { done(r); show("idle", "That recording was empty. Try again, or type your answer."); return; }
    sending = new AbortController();
    try {
      const said = await api(`/api/transcribe?item=${encodeURIComponent(opts.item || "ask")}`,
        { method: "POST", raw: blob, type, signal: sending.signal });
      if (r.mine !== gen) return;
      const text = (said.text || "").trim();
      if (!text) { show("idle", "Dojo heard nothing. Try again, or type your answer."); return; }
      // typing before speaking, or changing what was written down, both count as correcting it
      if (target.value !== (pieces.length ? asHeard : "")) corrected = true;
      target.value = target.value.trim() ? `${target.value.replace(/\s+$/, "")} ${text}` : text;
      pieces.push(text);
      asHeard = target.value;
      if (said.receipt) tickets.push(said.receipt);
      show("idle", "Added what you said. Read it, fix anything that is wrong, then send it yourself.");
      target.focus();
      if (target.setSelectionRange) target.setSelectionRange(target.value.length, target.value.length);
      if (opts.onText) opts.onText(text);
    } catch (e) {
      if (r.mine === gen && e.name !== "AbortError") show("idle", e.message);
    } finally {
      sending = null;
      if (run === r) run = null;
    }
  }

  /* Is any of what the learner said still in the box? Wiping it out and typing something else is a
     typed answer, however it started; fixing a word or two is still a spoken one. */
  function stillSpoken() {
    const now = new Set(words(target.value));
    return pieces.some((p) => {
      const said = words(p);
      return said.length > 0 && said.filter((w) => now.has(w)).length / said.length >= 0.5;
    });
  }

  show("idle", "");
  const handle = {
    el: box,
    mounted: false,
    used: stillSpoken,
    changed: () => corrected || (pieces.length > 0 && target.value !== asHeard),
    receipts: () => tickets.slice(),
    recording: () => !!run,
    holding,
    dispose,
  };
  speakers.add(handle);
  return handle;
}

/* How the answer was given. It is a condition, not help and not a penalty. */
const SPOKEN_CHIP = {
  spoken: { plain: "spoken, transcribed", edited: "spoken, transcribed, corrected" },
  spoken_unconfirmed: { plain: "sent as spoken", edited: "sent as spoken, corrected" },
};
function spokenChip(a) {
  const words = a ? SPOKEN_CHIP[a.input] : null;
  return words ? chip(a.edited_before_send ? words.edited : words.plain, "line") : null;
}

/* The same in a full sentence, for the item's conditions. It says only what Dojo can show: that it
   wrote a recording down for this question before the answer came in. */
function spokenCondition(a) {
  if (!a) return null;
  const fixed = a.edited_before_send ? ", and you corrected it before sending" : "";
  if (a.input === "spoken") {
    return h("p", { class: "cond" }, icon("mic", "sm"), h("span", null, h("b", null, "Spoken: "),
      `Dojo wrote down a recording for this question before you sent the answer${fixed}. It counts exactly like a typed answer.`));
  }
  if (a.input === "spoken_unconfirmed") {
    return h("p", { class: "cond" }, icon("mic", "sm"), h("span", null, h("b", null, "Sent as spoken: "),
      `this answer was sent as spoken${fixed}, but Dojo has no record of writing down a recording for this question. It counts exactly like a typed answer.`));
  }
  return null;
}

// ---------------------------------------------------------------- your turn (items)

async function viewItem(id) {
  const it = await api(`/api/items/${id}`);
  const held = it.drift && it.drift.state === "withheld" ? it.drift : null;
  const head = h("section", { class: "panel" },
    h("div", { class: "panel-h tight" }, h("span", { class: "qnum" }, `${MODE_NAME[it.mode]} · one item, answered once`),
      chip(skillTag(it.skill), "line")),
    h("h2", null, held ? "Held back" : "Your turn"),
    h("p", { class: "qtext" }, it.stem));
  // A source page no longer has the words this item was written from: it cannot be answered (ADR 0006).
  if (held) head.append(driftNote({ state: "recheck", notice: held.notice }),
    h("div", { class: "actions top" }, button("Write a new one", () => act(it.mode, it.package, it.skill)),
      linkButton("Open the skill", `#/skill/${it.package}/${it.skill}`, "ghost")));
  else if (!it.answer) head.append(answerForm(it));
  else if (!it.result) head.append(await pendingCard(it));
  if (it.drift && it.drift.state === "changed") head.append(driftNote({ state: "recheck", notice: it.drift.notice }));
  const side = h("div", { class: "col w430" },
    h("section", { class: "panel" }, eyebrow("scale", "The conditions"),
      h("p", { class: "cond" }, icon(it.mode === "practice" ? "hand" : "target", "sm"), h("span", null, MODE_TEXT[it.mode])),
      it.changed_condition ? h("p", { class: "cond" }, icon("refresh", "sm"), h("span", null, h("b", null, "Changed condition: "), it.changed_condition)) : null,
      spokenCondition(it.answer),
      h("p", { class: "cond" }, icon("record", "sm"), h("span", null, "What is recorded: whether each point was met, and under which conditions. Never a score.")),
      h("p", { class: "note top" }, NOT_A_PREDICTION)),
    h("section", { class: "panel" }, eyebrow("course", "The skill"),
      h("p", { class: "sub" }, it.skill_text),
      h("div", { class: "actions top" }, linkButton("Open the skill", `#/skill/${it.package}/${it.skill}`, "ghost sm"),
        linkButton("Ask about it", `#/ask/${it.skill}`, "ghost sm"))));
  const body = [h("div", { class: "row stackable grow" }, h("div", { class: "col grow" }, head, ...(it.result ? resultCards(it) : [])), side)];
  return [
    h("nav", { class: "crumb" }, h("a", { href: "#/skills" }, "Course"), h("span", { class: "sep" }, "›"),
      h("a", { href: `#/skill/${it.package}/${it.skill}` }, it.skill_text)),
    body];
}

// "today at 09:30", "yesterday at 18:05", "Monday at 07:40", "3 Sep at 10:00"
function plainSince(s) {
  const d = new Date(s);
  if (isNaN(d)) return "the last teaching";
  const now = new Date();
  const at = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days === 0) return `today at ${at}`;
  if (days === 1) return `yesterday at ${at}`;
  if (days > 1 && days < 7) return `${d.toLocaleDateString(undefined, { weekday: "long" })} at ${at}`;
  return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" })} at ${at}`;
}

/* Dojo cannot see a podcast app. Once the feed has offered a skill's episode, and still after it is turned off, an answer that
   could count as "later" first asks whether the learner listened, and the server records the reply before the answer. */
function podcastQuestion(it) {
  const el = h("div");
  let q = it.podcast_question || null, heard = null, first = null, asked = null;
  // A prompt this question put in the form's status line goes once a reply is chosen.
  const ask = (status, text) => { status.textContent = text; asked = { status, text }; first.focus(); };
  const paint = () => {
    heard = null;
    if (!q) { el.replaceChildren(); return; }
    const opts = [];
    const opt = (value, text) => {
      const input = h("input", { type: "radio", name: `podq-${it.id}`, value: value ? "yes" : "no" });
      const label = h("label", { class: "podq-opt" }, input, h("span", { class: "dot" }), h("span", null, text));
      input.addEventListener("change", () => {
        heard = value;
        opts.forEach((o) => o.classList.toggle("on", o === label));
        if (asked && asked.status.textContent === asked.text) asked.status.textContent = "";
        asked = null;
      });
      opts.push(label);
      return label;
    };
    const row = h("div", { class: "podq-opts" }, opt(true, "Yes, I listened"), opt(false, "No, I did not"));
    first = row.querySelector("input");
    el.replaceChildren(h("fieldset", { class: "podq" },
      h("legend", null, icon("listen", "sm"), h("span", null, q.since
        ? `Did you listen to this lesson in a podcast app since ${plainSince(q.since)}?`
        : "Have you listened to this lesson in a podcast app?")),
      h("p", null, q.episode ? `It is episode ${q.episode} in your podcast feed. `
        : q.feed_on ? "Its episode is not in your feed right now, but an earlier download may still be on your phone. "
        : "Your podcast feed is off, but an earlier download may still be on your phone. ",
        "Dojo cannot see what your podcast app plays, so it asks."),
      row,
      h("p", null, "Yes: this answer counts as right after teaching. No: the record notes what you said.")));
  };
  paint();
  return {
    el,
    ready(status) {
      if (!q || heard !== null) return true;
      ask(status, "Answer the podcast question first.");
      return false;
    },
    body: () => (q && heard !== null ? { podcast_heard: heard } : {}),
    // The question can start to apply while the page is open; the server then refuses the answer.
    async refused(e, status) {
      if (!String(e.message || "").startsWith("Answer the podcast question first")) return false;
      try { q = (await api(`/api/items/${it.id}`)).podcast_question || null; } catch (x) { return false; }
      if (!q) return false;
      paint();
      ask(status, "One more question before you send.");
      return true;
    },
  };
}

// ---------------------------------------------------------------- how sure were you (ADR 0011)
/* Asked once, after the answer is committed and before any feedback, and never in a timed exam. It is a
   mirror for the learner: it never changes a mark, never fills a pip and is not used in the advice. */
const CONF_LEVELS = [["guess", "Guessing"], ["fair", "Fairly sure"], ["certain", "Certain"]];
const CONF_WORD = Object.fromEntries(CONF_LEVELS);
const CONF_NOTE = "Only for you. It never changes your mark and never fills a pip.";
const SURE_NOT_YET = "Sure, but not right yet: worth another look.";

// Shows the question in `box` and resolves to "guess", "fair", "certain", or null when skipped.
function askSure(box) {
  return new Promise((resolve) => {
    const said = (v) => {
      put(box, h("p", { class: "small muted hs-said" }, icon("check", "xs"), v === "skip" ? "Skipped." : `You said: ${CONF_WORD[v]}.`));
      resolve(v === "skip" ? null : v);
    };
    const btns = CONF_LEVELS.map(([v, label]) => button(label, () => said(v), "ghost sm"));
    box.hidden = false;
    put(box, h("div", { class: "hs", role: "group", "aria-label": "How sure were you?" },
      h("p", { class: "hs-q", tabindex: "-1" }, h("b", null, "How sure were you?"), h("span", { class: "small muted" }, CONF_NOTE)),
      h("div", { class: "hs-btns" }, btns, h("button", { class: "link", type: "button", onclick: () => said("skip") }, "Skip"))));
    box.querySelector(".hs-q").focus();   // no answer is pre-focused, so Enter cannot pick one by accident
  });
}
const sureBox = () => h("div", { class: "hs-box", hidden: true, "aria-live": "polite" });
const sureChip = (a) => (a && a.confidence ? chip(`you said: ${CONF_WORD[a.confidence].toLowerCase()}`, "line") : null);

function answerForm(it) {
  const started = Date.now();
  const ta = h("textarea", { rows: 9, maxlength: 8000, id: "answer", placeholder: "Write your answer in your own words." });
  const mic = canSpeak() ? speakBox(ta, { item: it.id }) : null;
  const pq = podcastQuestion(it);
  const hints = h("div", null, it.hints.map((x, i) => h("p", { class: "hint" }, h("strong", null, `Hint ${i + 1}: `), x)));
  const status = h("p", { class: "small muted" });
  let left = it.hints_left;
  const hintBtn = it.mode === "practice" && left > 0 ? button(`Show a hint (${left} left)`, async () => {
    hintBtn.disabled = true;
    try {
      const r = await api(`/api/items/${it.id}/hint`, { method: "POST" });
      hints.append(h("p", { class: "hint" }, h("strong", null, `Hint ${r.index + 1}: `), r.hint));
      left = r.left;
      hintBtn.textContent = `Show a hint (${left} left)`;
      hintBtn.disabled = left <= 0;
    } catch (e) { status.textContent = e.message; hintBtn.disabled = false; }
  }, "ghost") : null;
  const sure = sureBox();
  const submit = button("Submit answer", async () => {
    const text = ta.value.trim();
    if (!text) { status.textContent = "Write an answer first."; return; }
    if (!pq.ready(status)) return;
    if (!window.confirm("Submit this answer? Each item is answered once.")) return;
    submit.disabled = true;
    if (hintBtn) hintBtn.disabled = true;
    ta.readOnly = true;   // committed: the confidence question cannot change the answer
    const elapsed = Math.round((Date.now() - started) / 1000);
    const spoken = !!(mic && mic.used());
    const edited = !!(mic && mic.changed());
    const receipts = mic ? mic.receipts() : [];
    if (mic) mic.dispose();  // the answer is on its way: stop listening now
    const token = state.nav;
    const confidence = await askSure(sure);
    if (token !== state.nav) return;
    try {
      const job = await api(`/api/items/${it.id}/answer`, { method: "POST", body: {
        text, elapsed_s: elapsed, confidence,
        input: spoken ? "spoken" : "typed", edited_before_send: edited, receipts, ...pq.body(),
      } });
      sessionStorage.setItem(`grade:${it.id}`, job.id);
      fiveRepaired(it.id);
      await withProgress("Judging your answer", Promise.resolve(job));
      if (token === state.nav) route();
    } catch (e) {
      if (token === state.nav && await pq.refused(e, status)) {
        submit.disabled = false; ta.readOnly = false; if (hintBtn) hintBtn.disabled = left <= 0; return;
      }
      if (token === state.nav) route();
    }
  });
  return h("div", null, h("label", { class: "field", for: "answer" }, "Your answer, in your own words", ta),
    mic ? mic.el : null,
    it.hints_note ? h("p", { class: "small muted" }, it.hints_note) : null, hints, pq.el,
    h("div", { class: "qfoot" }, h("div", { class: "actions" }, submit, hintBtn), status), sure);
}

async function pendingCard(it) {
  const jobId = sessionStorage.getItem(`grade:${it.id}`);
  const answer = h("blockquote", { class: "quote mine" }, it.answer.text);
  if (it.grading && !jobId) {
    return h("div", null, answer, h("p", { class: "sub" }, "Your answer is being judged right now."),
      h("div", { class: "actions top" }, button("Refresh", () => route())));
  }
  let failed = "";
  if (jobId) {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      // A judging that fails comes back here, where the card says why.
      if (job.state === "queued" || job.state === "running") return jobCard("Judging your answer", Promise.resolve(job), () => route(), () => route());
      if (job.state === "failed") { failed = job.error || ""; sessionStorage.removeItem(`grade:${it.id}`); }
    } catch (e) { sessionStorage.removeItem(`grade:${it.id}`); }
  }
  // Why judging waits, said plainly: the server's reason when a budget limit holds it until the next budget
  // day (the button stays shut until then), else what stopped the last try.
  const said = h("p", { class: "sub" }, it.grade_waits
    || (failed.startsWith("Your answer is saved") ? failed
      : failed ? `Your answer is saved, but judging did not finish: ${failed}` : "Your answer is saved but not judged yet."));
  const btn = button("Judge my answer now", async () => {
    btn.disabled = true;
    const token = state.nav;
    try {
      const job = await api(`/api/items/${it.id}/grade`, { method: "POST" });
      sessionStorage.setItem(`grade:${it.id}`, job.id);
      fiveRepaired(it.id);
      await withProgress("Judging your answer", Promise.resolve(job));
      if (token === state.nav) route();
    } catch (e) {
      if (token !== state.nav) return;
      if (e.status === 429) {   // refused before anything ran: a budget limit keeps the button shut until it opens
        said.textContent = `Your answer is saved. ${e.message}`;
        btn.disabled = /midnight UTC|raise the cap/.test(e.message);
        return;
      }
      if (!e.status) { route(); return; }   // the judging job failed: the card says why
      put($view(), errorPanel(e));
    }
  });
  if (it.grade_waits) btn.disabled = true;
  return h("div", null, answer, said, h("div", { class: "actions top" }, btn));
}

// The grader may quote a list-like answer in fragments separated by " … ": shown one per line, like the
// fragments quoted line by line. Only the display changes; the stored quote stays as the grader gave it.
const fragmentLines = (quote) => String(quote || "").replace(/[ \t]+\u2026[ \t]+/g, "\n");

// An answer judged again (ADR 0015): when, and the earlier judgement it replaced, which stays visible.
function earlierJudgement(it) {
  const hist = it.history || [];
  if (!hist.length) return null;
  const old = hist[hist.length - 1], o = old.result || {};
  return h("div", { class: "note" },
    h("p", null, `Judged again on ${fmtDate(it.result.judged_at)} with judging rules version ${it.result.version || 1}, at your request. `
      + `The earlier judgement (${o.met} of ${o.total} points met, on ${fmtDate(o.judged_at)}, with version ${o.version || 1}) no longer counts. It stays in your record.`
      + (old.disputed ? ` Your dispute of it, filed on ${fmtDate(old.disputed.at)}, stays on record too.` : "")),
    h("details", { class: "how" }, h("summary", null, "The earlier judgement"),
      h("ul", { class: "judged" }, (o.points || []).map((p) => {
        const ok = p.met && p.verified;
        return h("li", null,
          h("div", { class: "pt" }, h("span", { class: `mk ${ok ? "ok" : "no"}` }, icon(ok ? "check" : "x", "xs bold")),
            h("span", null, h("b", null, ok ? "Met: " : "Not met: "), p.point)),
          ok ? h("p", { class: "words" }, "Your words: ", h("mark", { class: "mine" }, fragmentLines(p.learner_quote))) : null,
          p.met && !p.verified ? h("p", { class: "words" }, "The grader said met, but its quote of your answer did not hold, so this point counted as not met.") : null);
      })),
      old.disputed ? h("p", { class: "words" }, "Your dispute: ", h("q", { class: "mine" }, old.disputed.reason)) : null));
}

// A judgement made with older judging rules can be judged again with today's, once, when the learner asks.
function judgeAgainPanel(it) {
  const ja = it.judge_again;
  if (!ja) return null;
  if (ja.busy) {
    return h("section", { class: "panel" }, eyebrow("refresh", "Judge again"),
      h("p", { class: "sub" }, "This answer is being judged again right now, with today's judging rules."),
      h("div", { class: "actions top" }, button("Refresh", () => route())));
  }
  const failed = sessionStorage.getItem(`rejudge:${it.id}`);
  sessionStorage.removeItem(`rejudge:${it.id}`);
  const status = h("p", { class: "small muted" }, ja.waits || (failed ? `Judging again did not finish: ${failed}` : ""));
  const b = button("Judge again with today's rules", async () => {
    if (!window.confirm("Judge this answer again with today's rules? It is done once. The new judgement counts in place of this one, whether it comes out higher or lower.")) return;
    b.disabled = true;
    const token = state.nav;
    try {
      const job = await api(`/api/items/${it.id}/judge-again`, { method: "POST" });
      await withProgress("Judging your answer again", Promise.resolve(job));
      if (token === state.nav) route();
    } catch (e) {
      if (token !== state.nav) return;
      if (!e.status) { sessionStorage.setItem(`rejudge:${it.id}`, e.message); route(); return; }   // the job failed: nothing changed
      status.textContent = e.message;
      b.disabled = e.status === 409 || /midnight UTC|raise the cap/.test(e.message);
    }
  }, "ghost");
  if (ja.waits) b.disabled = true;
  return h("section", { class: "panel" }, eyebrow("refresh", "Judged with older rules"),
    h("p", { class: "small ink2" }, `Dojo judged this answer with an older version of its judging rules (version ${ja.version}; today's is version ${ja.current}).`
      + ((ja.changes || []).length ? " What changed since: " + ja.changes.join(" ") : "")),
    h("p", { class: "small ink2" }, "You can have it judged again with today's rules, once. The new judgement replaces this one in your record, higher or lower, and this one stays visible."
      + (it.disputed ? " Your dispute stays on record with this judgement. Judging again is not a review of your dispute: you can dispute the new judgement on its own." : "")),
    h("div", { class: "actions top" }, b), status);
}

function resultCards(it) {
  const r = it.result;
  const out = [];
  out.push(h("section", { class: "panel seal" }, eyebrow("record", "What you wrote"),
    h("q", { class: "mine" }, it.answer.text),
    h("div", { class: "chips" }, chip(it.answer.hints_seen ? `${it.answer.hints_seen} hint${it.answer.hints_seen === 1 ? "" : "s"} opened` : "no hints", it.answer.hints_seen ? "amber" : "line"),
      chip(`${Math.max(1, Math.round((it.answer.elapsed_s || 0) / 60))} min`, "line"), chip(MODE_NAME[it.mode], "line"),
      spokenChip(it.answer), sureChip(it.answer),
      (it.history || []).length ? chip("judged again", "line") : null,
      it.disputed ? chip("disputed", "ochre") : null)));
  out.push(h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("scale", "The judgement"), h("b", null, `${r.met} of ${r.total} points met`)),
    earlierJudgement(it),
    it.disputed ? h("p", { class: "note" }, `You disputed this judgement on ${fmtDate(it.disputed.at)}. It no longer counts either way.`) : null,
    h("ul", { class: "judged" }, r.points.map((p) => {
      const ok = p.met && p.verified;
      return h("li", null,
        h("div", { class: "pt" }, h("span", { class: `mk ${ok ? "ok" : "no"}` }, icon(ok ? "check" : "x", "xs bold")),
          h("span", null, h("b", null, ok ? "Met: " : "Not met: "), p.point)),
        ok ? h("p", { class: "words" }, "Your words: ", h("mark", { class: "mine" }, fragmentLines(p.learner_quote))) : null,
        p.met && !p.verified ? h("p", { class: "words" }, p.not_shown
          ? "The checker read the quoted parts of your answer in the whole answer and found they do not show this point, so it counts as not met."
          : "The grader's quote of your answer was not found in it, so this point counts as not met.") : null,
        p.why ? h("p", { class: "words muted" }, p.why) : null);
    })),
    r.feedback
      ? h("div", { class: "feedback" }, h("b", null, "Feedback"), h("p", null, r.feedback),
          r.misconception ? h("p", null, h("strong", null, "A likely misconception: "), r.misconception) : null)
      : h("p", { class: "note top" }, `Feedback withheld: ${r.withheld_reason || "it did not pass the independent check."}`),
    h("p", { class: "small muted" }, `Judged by ${r.models.grader}; feedback checked by ${r.models.gate}, with Dojo's judging rules version ${r.version || 1}. Every point it gives you must quote your own words.`)));
  out.push(judgeAgainPanel(it));
  out.push(h("section", { class: "panel" }, eyebrow("file", "What a full answer covers"),
    it.changed_condition ? h("p", { class: "cond" }, icon("refresh", "sm"), h("span", null, h("b", null, "The changed condition: "), it.changed_condition)) : null,
    h("ul", { class: "points" }, it.rubric.map((p) => { const [b, q] = quoteToggle(p, it.sources); return h("li", null, h("span", { class: "dotm" }), h("span", null, p.point, " ", b, q)); })),
    h("h3", null, "A model answer"), h("p", { class: "sub" }, it.model_answer)));
  if (!it.disputed) {
    const reason = h("textarea", { rows: 3, maxlength: 2000, id: "dispute", placeholder: "What is wrong with this judgement?" });
    const status = h("p", { class: "small muted" });
    const b = button("Dispute this judgement", async () => {
      if (reason.value.trim().length < 3) { status.textContent = "Say briefly what is wrong."; return; }
      b.disabled = true;
      try { await api(`/api/items/${it.id}/dispute`, { method: "POST", body: { reason: reason.value.trim() } }); route(); }
      catch (e) { status.textContent = e.message; b.disabled = false; }
    }, "dispute");
    out.push(h("section", { class: "panel" }, h("div", { class: "disputebox" },
      eyebrow("alert", "Disagree?"),
      h("p", { class: "small ink2" }, "A disputed judgement is taken out of your record: it no longer counts as met or as not met."),
      h("label", { class: "field", for: "dispute" }, "Why", reason),
      h("div", { class: "actions top" }, b), status)));
  }
  out.push(h("section", { class: "panel" }, h("div", { class: "actions" },
    button("Another item, no help", () => act("check", it.package, it.skill)),
    button("Practise with hints", () => act("practice", it.package, it.skill), "ghost"),
    button("Study the lesson", () => act("lesson", it.package, it.skill), "ghost"),
    linkButton("Back to the skill", `#/skill/${it.package}/${it.skill}`, "ghost")),
    r.met < r.total ? shelfLink() : null));
  return out;
}

// ---------------------------------------------------------------- practice exam

const LENGTH_NAME = { full: "Full length", short: "Short" };

// What Microsoft Learn states about the real exam, and what it does not state. The count is Dojo's
// own default whenever Learn does not publish one, and it says so.
function specNote(s) {
  const bits = [];
  if (s.duration_quote) bits.push(h("p", { class: "small muted" }, "Microsoft Learn: ", h("q", null, s.duration_quote), " ", s.duration_source ? ext(s.duration_source, "source") : null));
  if (s.count_is_stated) bits.push(h("p", { class: "small muted" }, `Learn states ${s.real_questions} questions for the real exam.`));
  else bits.push(h("p", { class: "small muted" }, "Microsoft Learn does not publish a question count for this exam, so Dojo uses its own default of ",
    h("b", null, `${s.default_questions} questions`), " and scales the time from the stated duration. ",
    s.count_note ? h("q", null, s.count_note) : null, " ", s.count_source ? ext(s.count_source, "source") : null));
  if (s.checked) bits.push(h("p", { class: "small muted" }, `Dojo checked these pages on ${fmtDay(s.checked) || s.checked}.`));
  return bits;
}

function lengthCard(pid, key, s, busy, running, opts) {
  const go = button("Start", async () => {
    const token = state.nav;
    busy(true);
    try {
      const res = await api("/api/exams", { method: "POST", body: { package: pid, length: key, ...(opts ? opts() : {}) } });
      if (token !== state.nav) return;
      location.hash = `#/exam/${res.attempt}`;
    } catch (e) { busy(false); if (token === state.nav) put($view(), errorPanel(e)); }
  });
  if (running) { go.disabled = true; go.textContent = "Closed"; }
  return h("div", { class: "lencard" },
    h("div", null, h("b", null, LENGTH_NAME[key] || key),
      h("small", null, `${s.questions} questions · ${s.minutes} minutes${key === "full" ? "" : " · a shorter sitting"}`)),
    go);
}

async function viewRehearsals() {
  const [exams, list] = await Promise.all([
    api(`/api/exams?package=${encodeURIComponent(state.pid)}`),
    api(`/api/rehearsals?package=${encodeURIComponent(state.pid)}`),
  ]);
  // One exam runs at a time, and it closes what teaches whichever package it belongs to.
  const running = exams.running;
  const full = exams.lengths ? exams.lengths.full : null;
  const status = h("p", { class: "small muted" });
  const busy = (on) => { status.textContent = on ? "Writing and checking the questions…" : ""; };
  const count = h("select", { id: "count", "aria-label": "Number of questions" }, [10, 20, 30].map((n) => h("option", { value: n }, `${n} questions`)));
  const startQuick = button("Start", async () => {
    const token = state.nav;
    try {
      const res = await withProgress("Writing and checking the questions", api("/api/rehearsals", { method: "POST", body: { package: state.pid, count: Number(count.value) } }));
      if (token === state.nav) location.hash = `#/rehearsal/${res.rehearsal}`;
    } catch (e) { if (token === state.nav) put($view(), errorPanel(e)); }
  });
  const attemptRow = (a) => {
    const open = a.state === "open" || a.state === "ready" || a.state === "building";
    // No score while an exam can still be taken: a.correct is only sent once it has ended.
    const label = open ? (a.state === "open" ? "in progress" : a.state === "ready" ? "ready to start" : "still being written")
      : a.correct === null || a.correct === undefined ? "never taken"
      : a.points && a.points !== a.questions ? `${a.correct} of ${a.points} points` : `${a.correct} of ${a.questions} right`;
    const sub = [LENGTH_NAME[a.length] || a.length, `${a.questions} questions`, a.style === "exam" ? "exam format" : null,
      a.open_book ? "Microsoft Learn allowed" : null, `${a.new_questions} new`,
      a.withdrawn ? `${a.withdrawn} withdrawn` : null,
      a.conditions === "exam" ? "exam conditions" : a.conditions === "unknown" ? "conditions unknown" : null,
      a.closed_reason === "time" ? "time ran out" : null, fmtDate(a.closed_at || a.created)].filter(Boolean).join(" · ");
    // An old results page shows its answers, so while an exam runs it is closed, not a link.
    if (running && a.id !== running.id) {
      return h("div", { class: "rv" }, h("span", null, h("b", null, label), h("small", null, sub)));
    }
    return h("a", { class: "rv", href: `#/exam/${a.id}` }, h("span", null, h("b", null, label), h("small", null, sub)), icon("right", "sm"));
  };
  if (running) askClosed(true);   // the coach is closed here too while an exam is open
  // ADR 0012: the real exam's question types, and Microsoft Learn where Microsoft allows it.
  const book = exams.open_book || {};
  const mixes = exams.mix || {};
  const styleBox = h("fieldset", { class: "xopts" }, h("legend", null, "Question types"),
    h("label", { class: "xopt" }, h("input", { type: "radio", name: "xstyle", value: "exam", checked: true }),
      h("span", null, h("b", null, "Exam format"),
        ["full", "short"].filter((k) => mixes[k]).map((k) => h("small", null, `${LENGTH_NAME[k] || k}: ${mixText(mixes[k])}`)))),
    h("label", { class: "xopt" }, h("input", { type: "radio", name: "xstyle", value: "single" }),
      h("span", null, h("b", null, "Multiple choice only"), h("small", null, "One answer per question, as before."))));
  const bookBox = book.allowed
    ? h("label", { class: "xopt" }, h("input", { type: "checkbox", id: "xbook" }),
      h("span", null, h("b", null, "With Microsoft Learn allowed"),
        h("small", null, "As in the real exam: a button opens Microsoft Learn in a separate window, the clock keeps running, and the result says how often you looked and for how long.")))
    : h("p", { class: "small muted xclosed" }, icon("lock", "xs"), h("span", null, `Closed book. ${book.reason || ""}`));
  const opts = () => {
    const picked = styleBox.querySelector("input:checked");
    const box = $("xbook");
    return { style: picked ? picked.value : "exam", open_book: !!(book.allowed && box && box.checked) };
  };
  return [pageHead("Practice", "A timed practice exam in the real exam's format, and quick questions for a single sitting. Selected-response answers fill no pip.", null, helpQ("practice-exams")),
    running ? h("section", { class: "panel amber" }, h("div", { class: "panel-h" }, eyebrow("clock", "An exam is still running"),
      linkButton("Go back to it", `#/exam/${running.id}`, "sm")),
      h("p", { class: "sub" }, "Its clock is kept by the server. When the deadline passes, the exam closes itself and later answers are not taken.")) : null,
    h("div", { class: "row stackable" },
      h("section", { class: "panel grow" },
        h("div", { class: "panel-h" }, eyebrow("practice", "Timed practice exam"), linkButton("Should you book it?", "#/readiness", "ghost sm")),
        h("p", { class: "sub" }, `No hints, no coach, no sources. One question at a time, with a countdown and a review screen before you finish. Answers and reasons come at the end.`),
        full
          ? [h("div", { class: "xchoices" }, styleBox, bookBox),
            h("div", { class: "lengths" }, ["full", "short"].map((k) => lengthCard(state.pid, k, exams.lengths[k], busy, !!running, opts)))]
          : h("p", { class: "note top" }, exams.untimed),
        running ? h("p", { class: "small muted" }, "One exam at a time: finish the one that is running, or let its clock run out.") : null,
        status,
        exams.pool ? h("p", { class: "small muted top" }, `${exams.pool} checked question${exams.pool === 1 ? "" : "s"} are already waiting, so the start is quicker.`) : null,
        h("div", { class: "divider wide" }),
        full ? specNote(full) : null,
        h("p", { class: "note top" }, "Each question is written from the official sources and checked by an independent model; questions that do not hold are dropped. New exams avoid questions you have already seen while enough new ones can be written, and the result says how many were new.")),
      h("section", { class: "panel w430" }, eyebrow("record", "Earlier exams"),
        running ? h("p", { class: "small muted" }, "Their answers are closed while your exam runs.") : null,
        exams.attempts.length ? exams.attempts.map(attemptRow) : h("p", { class: "sub" }, "None yet."))),
    h("div", { class: "row stackable" },
      h("section", { class: "panel grow" }, eyebrow("practice", "Quick questions"),
        h("p", { class: "sub" }, "Not timed, and the reason is shown straight after each answer. Good for a short sitting; it does not count towards Dojo's readiness rules."),
        running
          ? h("p", { class: "note top" }, "Closed while your timed exam is running: these show their reasons straight away, and an exam is taken without help.")
          : h("div", { class: "datefield" }, h("label", { class: "field", for: "count" }, "Length", count), startQuick)),
      h("section", { class: "panel w430" }, eyebrow("record", "Earlier quick questions"),
        list.length ? list.map((r) => h("a", { class: "rv", href: `#/rehearsal/${r.id}` },
          h("span", null, h("b", null, rehearsalOpen(r) ? `${r.answered} of ${r.items} answered` : `${r.correct} of ${r.answered} right`),
            h("small", null, rehearsalOpen(r) ? `not finished · ${fmtDate(r.created)}` : `${r.answered} of ${r.items} answered · ${fmtDate(r.created)}`)),
          icon("right", "sm"))) : h("p", { class: "sub" }, "None yet.")))];
}

// A quick practice is open while a question is left that can still be answered. One a changed page no
// longer supports is held back (ADR 0006): it cannot be answered and is not counted.
const rehearsalOpen = (r) => r.answered + (r.held || 0) < r.items;

function examPane(R, startAt) {
  let cur = startAt;
  const wrap = h("div", { class: "row stackable grow" });
  const render = () => {
    const q = R.items[cur];
    const answered = "choice" in q;
    const held = !answered && q.drift ? q.drift : null;
    const status = h("p", { class: "small muted" });
    let picked = answered ? q.choice : null;
    const opts = h("div", { role: "radiogroup", "aria-label": `Question ${q.index + 1}` }, q.options.map((o, k) => {
      const input = h("input", { type: "radio", name: `q${q.index}`, value: k, disabled: answered || !!held, checked: answered && q.choice === k });
      const lab = h("label", { class: "opt" + (answered ? (k === q.answer ? " right" : k === q.choice ? " wrong" : "") : "") },
        input, h("span", { class: "radio" }), h("span", { class: "ltr" }, "ABCD"[k]), h("span", { class: "otext" }, o));
      input.addEventListener("change", () => {
        picked = k;
        for (const n of opts.querySelectorAll(".opt")) n.classList.remove("selected");
        lab.classList.add("selected");
      });
      if (answered && k === q.choice) lab.classList.add("selected");
      return lab;
    }));
    const sure = sureBox();
    const answer = button("Answer", async () => {
      if (picked === null) { status.textContent = "Pick an option first."; return; }
      answer.disabled = true;
      for (const x of opts.querySelectorAll("input")) x.disabled = true;   // committed before the confidence question
      const token = state.nav;
      const confidence = await askSure(sure);
      if (token !== state.nav) return;
      try {
        const res = await api(`/api/rehearsals/${R.id}/answer`, { method: "POST", body: { index: q.index, choice: picked, confidence } });
        Object.assign(q, { choice: picked, confidence }, res);
        R.answered = R.items.filter((x) => "choice" in x).length;
        R.correct = R.items.filter((x) => x.correct).length;
        render();
      } catch (e) {
        // Answered elsewhere, or held back meanwhile because a source page changed: show it as it is now.
        if (e.status === 409) {
          try { Object.assign(R, await api(`/api/rehearsals/${R.id}`)); render(); return; } catch (_) { /* keep the first message */ }
        }
        status.textContent = e.message; answer.disabled = false;
        for (const x of opts.querySelectorAll("input")) x.disabled = false;
      }
    });
    const next = R.items.findIndex((x, k) => k > cur && !("choice" in x) && !x.drift);
    const left = h("section", { class: "panel grow stack" },
      h("div", { class: "panel-h tight" }, h("span", { class: "qnum" }, `Question ${q.index + 1} of ${R.items.length}`), chip(q.domain, "line")),
      h("p", { class: "qtext" }, q.stem),
      opts,
      answered ? null : sure,
      held ? h("p", { class: "conf top" }, icon("refresh", "xs"), h("span", null, held.notice)) : null,
      answered ? h("div", { class: "feedback" },
        h("p", null, h("strong", null, q.correct ? "Right. " : `Not this time: the answer is ${"ABCD"[q.answer]}. `), q.rationale),
        !q.correct && (q.confidence === "certain" || q.confidence === "fair") ? h("p", { class: "small ink2" }, icon("eye", "xs"), " ",
          SURE_NOT_YET, " It is in your Record, under Know what you know.") : null,
        h("blockquote", { class: "quote" }, `“${q.quote}”`, h("footer", null, "From ", ext(q.source.url, q.source.title), ", word for word")),
        h("p", { class: "small" }, h("a", { class: "link", href: `#/skill/${R.package}/${q.skill}` }, `Skill: ${q.skill_text}`))) : null,
      h("div", { class: "qfoot" },
        h("div", { class: "actions" },
          button("Previous", () => { if (cur > 0) { cur -= 1; render(); } }, "ghost sm"),
          button("Next", () => { if (cur < R.items.length - 1) { cur += 1; render(); } }, "ghost sm")),
        h("div", { class: "actions" }, status,
          answered || held ? (next >= 0 ? button("Next unanswered", () => { cur = next; render(); })
            : linkButton("See the result", `#/rehearsal/${R.id}`)) : answer)));
    const grid = h("div", { class: "navgrid" }, R.items.map((x, k) => {
      const done = "choice" in x;
      const cls = ["sq", k === cur ? "cur" : done ? "done" : x.drift ? "held" : ""].filter(Boolean).join(" ");
      return h("button", { class: cls, type: "button", "aria-label": `Question ${k + 1}${!done && x.drift ? ", held back" : ""}`, onclick: () => { cur = k; render(); } }, k + 1);
    }));
    const side = h("section", { class: "panel w330" }, eyebrow("practice", "Questions"),
      grid,
      h("div", { class: "nleg" },
        h("span", null, h("i", { class: "lgsq done" }), "answered"),
        h("span", null, h("i", { class: "lgsq cur" }), "here"),
        h("span", null, h("i", { class: "lgsq empty" }), "not answered"),
        R.held ? h("span", null, h("i", { class: "lgsq held" }), "held back") : null),
      h("p", { class: "note top" }, `${R.items.filter((x) => "choice" in x).length} of ${R.items.length} answered. `,
        R.dropped ? `${R.dropped} question${R.dropped === 1 ? " was" : "s were"} dropped because they did not pass the checks. ` : "",
        R.held ? `${R.held} ${R.held === 1 ? "is" : "are"} held back because a source page changed: not answered and not counted.` : ""),
      h("p", { class: "scorefoot" }, icon("shield", "sm"), R.note));
    put(wrap, left, side);
  };
  render();
  return wrap;
}

function examResults(R, weights) {
  const wrong = R.items.filter((q) => "choice" in q && !q.correct);
  const counted = R.items.length - (R.held || 0);
  const missed = counted - R.correct;
  const score = h("section", { class: "panel w430" },
    h("p", { class: "eyebrow" }, "Your score · Dojo questions"),
    h("div", { class: "score" }, h("span", { class: "big" }, R.correct), h("span", { class: "den" }, `/ ${counted}`)),
    h("p", { class: "sub" }, `${missed} missed · ${R.answered} of ${counted} answered`),
    h("p", { class: "note top" }, "Correct answers on Dojo's own questions. ", h("b", null, "Not a pass prediction: "),
      "the real exam uses different questions and a scaled score."),
    R.dropped ? h("p", { class: "small muted top" }, `${R.dropped} question${R.dropped === 1 ? " was" : "s were"} dropped because ${R.dropped === 1 ? "it" : "they"} did not pass the checks.`) : null,
    R.held ? h("p", { class: "small muted top" }, `${R.held} question${R.held === 1 ? " was" : "s were"} held back and not counted: a source page changed and no longer has the words ${R.held === 1 ? "it was" : "they were"} written from.`) : null,
    h("div", { class: "divider" }),
    h("p", { class: "share-line" }, icon("record", "sm"),
      h("span", null, "Shown next to your record, never inside it: multiple-choice answers fill no pips.")),
    h("p", { class: "scorefoot" }, icon("shield", "sm"), R.note));
  const table = h("section", { class: "panel grow" },
    h("div", { class: "panel-h" }, eyebrow("scale", "Where the points went · by exam domain"),
      h("span", { class: "small muted" }, "bars show right ÷ answered")),
    R.by_domain.length
      ? h("div", { class: "dscroll" }, h("table", { class: "t dtbl" },
          h("thead", null, h("tr", null, h("th", null, "Domain"), h("th", { class: "num" }, "weight"),
            h("th", { class: "num" }, "answered"), h("th", { class: "num" }, "right"), h("th", { class: "res" }, "result"))),
          h("tbody", null, R.by_domain.map((d) => {
            const share = d.answered ? (d.correct * 100 / d.answered) : 0;
            const bar = h("div", { class: "bar" }, wide(h("i", { class: share >= 60 ? "" : "amber" }), `${share.toFixed(1)}%`));
            return h("tr", null, h("td", null, h("b", null, d.domain)),
              h("td", { class: "num w" }, (weights && weights.get(d.domain)) || "—"),
              h("td", { class: "num" }, d.answered), h("td", { class: "num" }, d.correct), h("td", { class: "res" }, bar));
          }))))
      : h("p", { class: "sub" }, "These questions were not grouped by domain."));
  const review = h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("record", "Review answers"), linkButton("New exam", "#/rehearsal", "ghost sm")),
    h("div", { class: "revgrid" }, R.items.map((q) => {
      const answered = "choice" in q;
      const held = !answered && q.drift;
      return h("div", { class: "rev" },
        h("span", { class: `rmk ${answered ? (q.correct ? "ok" : "wrong") : held ? "held" : ""}` }, answered ? icon(q.correct ? "check" : "x", "xs bold") : null),
        h("div", null, h("b", null, q.stem),
          h("div", { class: "rchoice" }, answered ? `you chose ${"ABCD"[q.choice]} · answer ${"ABCD"[q.answer]}` : held ? "held back · not counted" : "not answered"),
          answered && q.rationale ? h("p", { class: "ex" }, q.rationale) : null,
          held ? h("p", { class: "ex" }, q.drift.notice) : null,
          h("p", { class: "rlinks" }, answered && q.source ? ext(q.source.url, "source") : null,
            h("a", { class: "link small", href: `#/skill/${R.package}/${q.skill}` }, q.skill_text))));
    })),
    wrong.length ? h("p", { class: "note top" }, `${wrong.length} to look at again. Open the skill to read the lesson or take a check with no help.`) : null);
  return [h("div", { class: "row stackable grow" }, score, table), review];
}

async function viewRehearsal(id) {
  const R = await api(`/api/rehearsals/${id}`);
  const duel = R.origin === "duel";
  // A duel can be on another of your exams: show it under that exam, so domains and weights match.
  if (duel && R.package !== state.pid && myExams().some((x) => x.id === R.package)) await choosePackage(R.package);
  const crumb = duel
    ? h("nav", { class: "crumb" }, h("a", { href: "#/play" }, "Play"), h("span", { class: "sep" }, "›"), h("b", null, "Duel"))
    : h("nav", { class: "crumb" }, h("a", { href: "#/rehearsal" }, "Practice"), h("span", { class: "sep" }, "›"), h("b", null, fmtDate(R.created)));
  const firstOpen = R.items.findIndex((q) => !("choice" in q) && !q.drift);
  if (firstOpen >= 0) {
    return [
      crumb,
      h("div", { class: "pageh" }, h("h1", null, duel ? "Duel" : "Practice exam"),
        h("p", { class: "sub" }, duel
          ? `The same ${R.items.length} questions as the other player. No clock: take your time. Only the number right counts, and only the two of you see it. Not a pass prediction.`
          : `${R.items.length} multiple-choice questions, spread over the skills by their weight. ${NOT_A_PREDICTION}`)),
      examPane(R, firstOpen)];
  }
  const p = await ensurePkg();
  const weights = new Map(p.domains.map((d) => [d.title, d.weight]));
  return [
    crumb,
    pageHead(`${R.correct} of ${R.items.length - (R.held || 0)} correct — here is where the points went`, duel ? "Your own answers and reasons. The other player does not see them." : null,
      h("p", { class: "eyebrow" }, `${duel ? "Duel" : "Practice exam"} · ${fmtWhen(R.created)} · ${R.items.length} questions`)),
    examResults(R, weights)];
}

// ---------------------------------------------------------------- the timed practice exam

function clockText(secs) {
  const s = Math.max(0, Math.round(secs));
  const mm = String(Math.floor(s / 60) % 60).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return s >= 3600 ? `${String(Math.floor(s / 3600)).padStart(2, "0")}:${mm}:${ss}` : `${mm}:${ss}`;
}

function stopClock() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (state.keys) { document.removeEventListener("keydown", state.keys); state.keys = null; }
  if (state.away) { state.away(); state.away = null; }   // the open-book exam's watch on this page's visibility
  if (state.deckOff) state.deckOff();   // a Present player's watchers: window size, phone width, keys, sound
}

// While an exam runs the header's Ask box is closed, with the reason in it. The microphone beside it
// goes too: a recording there feeds the coach, and the coach is closed.
function askClosed(on) {
  const form = $("askform"), q = $("askq"), go = form && form.querySelector(".go");
  if (!form || !q) return;
  form.classList.toggle("off", !!on);
  q.disabled = !!on;
  if (on) { q.value = ""; stopEveryMicrophone(); }
  form.querySelectorAll("button").forEach((b) => {
    if (on && !b.disabled) { b.dataset.examclosed = "1"; b.disabled = true; }
    else if (!on && b.dataset.examclosed) { delete b.dataset.examclosed; b.disabled = false; }
  });
  q.placeholder = on ? "Closed: no coach during a timed exam" : "Ask the coach anything…";
  if (go) go.textContent = on ? "Closed" : "Ask";
  form.setAttribute("title", on ? "The coach is closed while a timed practice exam runs." : "");
}

// In the runner itself the sidebar goes away as well: one question, one clock, nothing else.
function examMode(on) {
  document.body.classList.toggle("examing", !!on);
  askClosed(on);
  if (on) hideNewBuild(); else showNewBuild();  // no reload banner over a running exam
}

const examCrumb = (tail) => h("nav", { class: "crumb" }, h("a", { href: "#/rehearsal" }, "Practice"),
  h("span", { class: "sep" }, "›"), h("b", null, tail));

async function examBuilding(X) {
  const step = h("p", { class: "muted small" }, `${X.questions} of ${X.wanted} questions ready`);
  const fill = h("div", { class: "progress-fill" });
  const el = panel(eyebrow("wand", "Writing your exam"), h("h2", null, `A ${LENGTH_NAME[X.length].toLowerCase()} practice exam`),
    h("div", { class: "progress", role: "progressbar", "aria-label": "Questions written" }, fill), step,
    h("p", { class: "small muted" }, "Every question is written from the official sources and checked by an independent model. Questions already checked and waiting are used first, so this is usually short. You can leave this page and come back."),
    h("div", { class: "actions top" }, linkButton("Back to Practice", "#/rehearsal", "ghost sm")));
  const token = state.nav;
  (async () => {
    for (let i = 0; i < 900; i++) {
      await sleep(2000);
      if (token !== state.nav) return;
      let now;
      try { now = await api(`/api/exams/${X.id}`); } catch (e) { step.textContent = e.message; return; }
      if (token !== state.nav) return;
      step.textContent = `${now.questions} of ${now.wanted} questions ready`;
      fill.style.setProperty("width", `${Math.round(100 * now.questions / Math.max(1, now.wanted))}%`);
      if (now.state !== "building") { route(); return; }
    }
  })();
  return [examCrumb("New exam"), el];
}

function examBroken(X) {
  return [examCrumb("New exam"),
    panel(eyebrow("alert", "This exam was never taken"), h("h2", null, "Dojo stopped while it was writing this exam"),
      h("p", { class: "sub" }, X.conditions_note || "Its conditions are recorded as unknown."),
      h("p", { class: "note top" }, "Nothing was answered, so nothing was recorded about you. Start a new one."),
      h("div", { class: "actions top" }, linkButton("Back to Practice", "#/rehearsal")))];
}

const EXAM_RULES = [
  ["clock", "The clock is kept by the server. When the deadline passes the exam closes itself, and answers after it are not taken."],
  ["hand", "No hints, no coach, no sources. The right answers and the reasons come at the end, all at once."],
  ["flag", "Flag anything you want to look at again. A review screen lists what is unanswered and what is flagged before you finish."],
  ["record", "Selected-response answers fill no pip, whatever the question type. They go to your practice bucket, beside your record, never inside it."],
];
const EXAM_STYLE_RULES = [
  ["lock", "A yes/no series and a case study come last. Once you leave one you cannot go back to it, as in the real exam; Dojo asks before you leave."],
  ["scale", "No points are taken off for a wrong answer. Where a question has parts (choose two, drag and drop, hot area, each yes/no statement) each right part earns a point; a build list counts only in the right order."],
];
const EXAM_PODCAST_RULE = ["listen", "Episodes from your podcast feed may be on your phone, and they can play during the exam without Dojo seeing them. So before the exam closes, it asks whether you listened to one."];
const PODCAST_ASK = "Answer the podcast question first.";

function examBrief(X) {
  const s = X.spec;
  // A source page changed since this exam was written: its start leaves out what the page no longer says (ADR 0006).
  const lost = X.source_waiting || 0, none = lost > 0 && lost >= X.questions;
  const go = button("Start the exam", async () => {
    go.disabled = true;
    try { await api(`/api/exams/${X.id}/start`, { method: "POST" }); route(); }
    catch (e) { go.disabled = false; put($view(), errorPanel(e)); }
  });
  go.disabled = none;
  const exam = X.style === "exam";
  const rule = X.open_book_rule || {};
  const rules = [...EXAM_RULES, ...(exam ? EXAM_STYLE_RULES : []), ...(X.podcast_question ? [EXAM_PODCAST_RULE] : [])]
    .map(([ic, text]) => (ic === "hand" && X.open_book
      ? ["hand", "Microsoft Learn is allowed, as in the real exam: a button opens it in a separate window and the clock keeps running. No hints and no coach. The right answers and the reasons come at the end."]
      : [ic, text]));
  return [examCrumb("New exam"),
    pageHead(`${X.questions} questions · ${X.minutes} minutes`, `${X.exam} practice exam, ${LENGTH_NAME[X.length].toLowerCase()}. The clock starts when you press Start.`,
      h("p", { class: "eyebrow" }, "Practice exam · exam conditions"), helpQ("practice-exams")),
    h("div", { class: "row stackable" },
      h("section", { class: "panel grow" }, eyebrow("practice", "Before you start"),
        h("ul", { class: "tips big" }, rules.map(([ic, text]) => h("li", null, icon(ic, "sm"), h("span", null, text)))),
        h("div", { class: "divider wide" }),
        exam ? h("p", { class: "sub" }, h("b", null, "Question types: "), mixText(X.mix), ".") : null,
        h("p", { class: "sub" }, X.open_book ? "Open book: Microsoft Learn allowed. " : "Closed book. ",
          X.open_book ? `Dojo counts how often you open it and how long this page is out of view, and the result says so. ${rule.rule || ""}`
            : rule.allowed ? "You chose to take it without Microsoft Learn." : (rule.reason || "")),
        h("p", { class: "sub" }, `${X.new_questions} of ${X.questions} question${X.questions === 1 ? "" : "s"} are ones you have not seen.`,
          X.new_questions < X.questions ? " Dojo could not write enough new ones, so some come round again; the result says so too." : ""),
        X.dropped ? h("p", { class: "small muted" }, `${X.dropped} question${X.dropped === 1 ? " was" : "s were"} dropped because ${X.dropped === 1 ? "it" : "they"} did not pass the checks.`) : null,
        lost ? driftNote({ state: "recheck", notice: none
          ? "A source page changed, and none of these questions matches its words any more. Build a new practice exam."
          : `A source page changed, and ${plural(lost, "question")} no longer ${lost === 1 ? "matches" : "match"} its words. `
            + `${lost === 1 ? "It is" : "They are"} left out when you start, and the clock is set for the rest.` }) : null,
        h("div", { class: "actions top" }, go, linkButton(none ? "Build a new one" : "Not now", "#/rehearsal", "ghost"))),
      h("section", { class: "panel w430" }, eyebrow("scale", "Why this length"),
        specNote(s),
        h("p", { class: "scorefoot" }, icon("shield", "sm"), X.note)))];
}

// ---------------------------------------------------------------- the kinds of question (ADR 0012)

const KIND_LABELS = { single: "Multiple choice", multi: "Multiple answers", sequence: "Build list", match: "Drag and drop",
  hotarea: "Hot area", yesno: "Yes/No series", case: "Case study" };
const KIND_ORDER = Object.keys(KIND_LABELS);
const LOCKING_KINDS = ["yesno", "case"];
const LETTERS = "ABCDEFGH";
const kindOf = (q) => (q && q.kind && KIND_LABELS[q.kind] ? q.kind : "single");
const sizeOf = (q) => (q && q.size) || 1;

// How many questions of each kind, on one line: "29 multiple choice · 5 multiple answers · 4 in a case study".
const UNIT_QUESTIONS = { yesno: 3, case: 4 };
function mixText(mix) {
  return KIND_ORDER.filter((k) => mix && mix[k]).map((k) => {
    const n = mix[k], per = UNIT_QUESTIONS[k];
    if (!per) return `${n} ${KIND_LABELS[k].toLowerCase()}`;
    const units = Math.max(1, Math.round(n / per)), name = k === "case" ? "case stud" : "yes/no series";
    return units === 1 ? `${n} in a ${name}${k === "case" ? "y" : ""}` : `${n} in ${units} ${name}${k === "case" ? "ies" : ""}`;
  }).join(" · ");
}

// How many of an item's questions have an answer: a yes/no statement and a case-study question count one each.
function partsDone(q) {
  const k = kindOf(q);
  if (k === "single") return "choice" in q ? 1 : 0;
  const r = q.response;
  if (!r) return 0;
  if (k === "yesno") return (r.yes || []).filter((v) => v === true || v === false).length;
  if (k === "case") return Object.keys(r.parts || {}).length;
  return 1;
}

const casePart = (q, n) => ((q.response && q.response.parts) || {})[String(n)] || null;
const firstOpenPart = (q) => {
  if (kindOf(q) !== "case") return 0;
  const n = q.questions.findIndex((_, k) => !casePart(q, k));
  return n < 0 ? 0 : n;
};
const swap = (a, i, j) => { const b = [...a]; [b[i], b[j]] = [b[j], b[i]]; return b; };

// Option rows: radios for one answer, checkboxes for several. Every control carries a data-fk, so the
// keyboard focus comes back to it after the pane is drawn again.
function optionRows(name, options, chosen, many, onPick, label) {
  return h("div", { class: "opts", role: many ? "group" : "radiogroup", "aria-label": label }, options.map((o, k) => {
    const on = chosen.includes(k);
    const input = h("input", { type: many ? "checkbox" : "radio", name, value: k, checked: on, "data-fk": `${name}-${k}` });
    input.addEventListener("change", () => onPick(k, input));
    return h("label", { class: "opt" + (on ? " selected" : "") }, input,
      h("span", { class: many ? "radio box" : "radio" }), h("span", { class: "ltr" }, LETTERS[k]), h("span", { class: "otext" }, o));
  }));
}

function multiRows(name, sub, chosen, onChange) {
  const max = sub.choose || 2;
  const count = h("p", { class: "small muted", "aria-live": "polite" }, `${chosen.length} of ${max} chosen.`);
  const rows = optionRows(name, sub.options, chosen, true, (k, input) => {
    const next = input.checked ? [...chosen, k] : chosen.filter((c) => c !== k);
    if (next.length > max) { input.checked = false; count.textContent = `Choose no more than ${max}: untick one first.`; return; }
    onChange(next.sort((a, b) => a - b));
  }, `Choose ${max}`);
  return [h("p", { class: "qhint" }, `Choose ${max}. Each right choice earns a point; a wrong one costs nothing.`), rows, count];
}

// Build list: no dragging needed. Steps are added, moved up or down and taken out with buttons.
function sequenceBox(q, onChange) {
  const order = (q.response && q.response.order) || [];
  const rest = q.options.map((_, k) => k).filter((k) => !order.includes(k));
  const name = `s${q.index}`;
  const step = (k, n) => h("li", { class: "seqrow" },
    h("span", { class: "seqn" }, n + 1), h("span", { class: "otext" }, q.options[k]),
    h("span", { class: "seqbtns" },
      h("button", { class: "btn ghost sm", type: "button", "data-fk": `${name}-up-${k}`, disabled: n === 0,
        "aria-label": `Move up: ${q.options[k]}`, onclick: () => onChange(swap(order, n, n - 1)) }, "Up"),
      h("button", { class: "btn ghost sm", type: "button", "data-fk": `${name}-down-${k}`, disabled: n === order.length - 1,
        "aria-label": `Move down: ${q.options[k]}`, onclick: () => onChange(swap(order, n, n + 1)) }, "Down"),
      h("button", { class: "btn ghost sm", type: "button", "data-fk": `${name}-out-${k}`,
        "aria-label": `Take out of the list: ${q.options[k]}`, onclick: () => onChange(order.filter((x) => x !== k)) }, "Remove")));
  const spare = (k) => h("li", { class: "seqrow" }, h("span", { class: "otext" }, q.options[k]),
    h("span", { class: "seqbtns" }, h("button", { class: "btn ghost sm", type: "button", "data-fk": `${name}-add-${k}`,
      "aria-label": `Add to your list: ${q.options[k]}`, onclick: () => onChange([...order, k]) }, "Add")));
  return h("div", { class: "seq" },
    h("p", { class: "qhint" }, `Put ${q.steps || "the"} steps that belong in the right order. Not every step belongs. `
      + "The list is right only as a whole: one point, or none."),
    h("div", { class: "seqcols" },
      h("section", null, h("h3", { class: "seqh", id: `${name}-a` }, "Steps"),
        rest.length ? h("ul", { class: "seqlist", "aria-labelledby": `${name}-a` }, rest.map(spare))
          : h("p", { class: "small muted" }, "Every step is in your list.")),
      h("section", null, h("h3", { class: "seqh", id: `${name}-b` }, "Your list, in order"),
        order.length ? h("ol", { class: "seqlist", "aria-labelledby": `${name}-b` }, order.map(step))
          : h("p", { class: "small muted" }, "Empty. Add steps from the list of steps."))));
}

// Drag and drop, done with a drop-down per target.
function matchBox(q, onChange) {
  const pairs = (q.response && q.response.pairs) || q.targets.map(() => null);
  return h("div", { class: "match" },
    h("p", { class: "qhint" }, "Choose a value for each target. Each value may be used once, more than once, or not at all. Each right target earns a point."),
    h("ul", { class: "mvalues", "aria-label": "Values" }, q.options.map((o) => h("li", null, o))),
    q.targets.map((t, n) => {
      const id = `m${q.index}-${n}`;
      const sel = h("select", { id, "data-fk": id }, h("option", { value: "" }, "— choose a value —"),
        q.options.map((o, k) => h("option", { value: k, selected: pairs[n] === k }, o)));
      sel.addEventListener("change", () => { const next = [...pairs]; next[n] = sel.value === "" ? null : Number(sel.value); onChange(next); });
      return h("div", { class: "mrow" }, h("label", { for: id }, t), sel);
    }));
}

// Hot area: drop-down lists inside a statement or a code sample, where [[1]], [[2]] … stand.
function hotBox(q, onChange) {
  const picks = (q.response && q.response.picks) || q.blanks.map(() => null);
  const bits = String(q.text || "").split(/\[\[(\d+)\]\]/);
  return h("div", { class: "hot" },
    h("p", { class: "qhint" }, "Choose the right value in each drop-down list. Each right one earns a point."),
    h("div", { class: "hottext" }, bits.map((p, i) => {
      if (i % 2 === 0) return p;
      const n = Number(p) - 1, b = q.blanks[n];
      if (!b) return `[[${p}]]`;
      const sel = h("select", { "aria-label": `Drop-down ${n + 1}`, "data-fk": `h${q.index}-${n}` },
        h("option", { value: "" }, `(${n + 1}) choose…`), b.options.map((o, k) => h("option", { value: k, selected: picks[n] === k }, o)));
      sel.addEventListener("change", () => { const next = [...picks]; next[n] = sel.value === "" ? null : Number(sel.value); onChange(next); });
      return sel;
    })));
}

// A yes/no series: one statement at a time; an answered statement cannot be changed.
function yesnoBox(q, onAnswer) {
  const yes = (q.response && q.response.yes) || q.statements.map(() => null);
  const last = yes.reduce((m, v, i) => (v === true || v === false ? i : m), -1);
  const now = last + 1;
  const rows = q.statements.map((s, n) => {
    if (n < now) {
      return h("li", { class: "ynrow done" }, h("span", { class: "otext" }, `${n + 1}. ${s}`),
        h("b", null, yes[n] === true ? "Yes" : yes[n] === false ? "No" : "skipped"), h("small", { class: "muted" }, "cannot be changed"));
    }
    if (n > now) return null;
    let pick = null;
    const ok = h("button", { class: "btn", type: "button", disabled: true, "data-fk": `yn${q.index}-ok` },
      n === q.statements.length - 1 ? "Answer" : "Answer and show the next statement");
    const labels = [];
    const radios = h("div", { class: "opts yn", role: "radiogroup", "aria-label": `Statement ${n + 1}` }, [true, false].map((v) => {
      const input = h("input", { type: "radio", name: `yn${q.index}-${n}`, value: v ? "yes" : "no", "data-fk": `yn${q.index}-${n}-${v ? "y" : "n"}` });
      const lab = h("label", { class: "opt" }, input, h("span", { class: "radio" }), h("span", { class: "ltr" }, v ? "Y" : "N"),
        h("span", { class: "otext" }, v ? "Yes" : "No"));
      input.addEventListener("change", () => { pick = v; ok.disabled = false; labels.forEach((l) => l.classList.toggle("selected", l === lab)); });
      labels.push(lab);
      return lab;
    }));
    ok.addEventListener("click", () => { if (pick !== null) onAnswer(n, pick, ok); });
    return h("li", { class: "ynrow now" }, h("p", { class: "qtext sm" }, h("span", { class: "muted" }, `Statement ${n + 1} of ${q.statements.length}: `), s),
      radios, h("div", { class: "actions" }, ok));
  });
  return h("div", { class: "ynbox" },
    h("p", { class: "qhint" }, "Answer each statement Yes or No. Once you answer one it cannot be changed, and once you leave this series you cannot come back to it."),
    h("ol", { class: "ynlist" }, rows),
    now >= q.statements.length ? h("p", { class: "note top" }, "Every statement has an answer. Move on when you are ready.") : null);
}

// A case study: the exhibits in tabs, and its questions one at a time.
function caseBox(q, tab, part, on) {
  const tabs = h("div", { class: "cxtabs", role: "tablist", "aria-label": "Exhibits" }, q.exhibits.map((e, k) => {
    const b = h("button", { class: "cxtab" + (k === tab ? " on" : ""), type: "button", role: "tab", id: `cx${q.index}-${k}`,
      "aria-selected": k === tab ? "true" : "false", "aria-controls": `cxp${q.index}`, tabindex: k === tab ? "0" : "-1", "data-fk": `cx${q.index}-${k}` }, e.title);
    b.addEventListener("click", () => on.tab(k));
    b.addEventListener("keydown", (ev) => {
      const n = q.exhibits.length;
      const to = { ArrowRight: (k + 1) % n, ArrowLeft: (k + n - 1) % n, Home: 0, End: n - 1 }[ev.key];
      if (to === undefined) return;
      ev.preventDefault(); ev.stopPropagation();
      on.tab(to, `cx${q.index}-${to}`);
    });
    return b;
  }));
  const ex = q.exhibits[tab] || { text: "" };
  const sub = q.questions[part];
  const got = casePart(q, part);
  const name = `c${q.index}-${part}`;
  const body = kindOf(sub) === "multi"
    ? multiRows(name, sub, (got && got.choices) || [], (next) => on.answer(part, { choices: next }))
    : optionRows(name, sub.options, got && got.choice !== undefined ? [got.choice] : [], false, (k) => on.answer(part, { choice: k }), `Case-study question ${part + 1}`);
  return h("div", { class: "case" },
    h("p", { class: "qhint" }, "Read the exhibits, then answer the questions. You can move between them freely until you leave the case study; then it is locked."),
    h("div", { class: "casecols" },
      h("section", { class: "caseex" }, h("h3", { class: "seqh" }, "Exhibits"), tabs,
        h("div", { class: "exhibit", role: "tabpanel", id: `cxp${q.index}`, "aria-labelledby": `cx${q.index}-${tab}`, tabindex: "0" }, ex.text)),
      h("section", { class: "caseq" },
        h("div", { class: "casenav", role: "group", "aria-label": "Questions in this case study" }, q.questions.map((_, n) =>
          h("button", { class: "sq" + (n === part ? " cur" : casePart(q, n) ? " done" : ""), type: "button", "data-fk": `cq${q.index}-${n}`,
            "aria-label": `Case-study question ${n + 1}${casePart(q, n) ? ", answered" : ", not answered"}`, "aria-current": n === part ? "true" : null,
            onclick: () => on.part(n) }, n + 1))),
        h("p", { class: "qnum" }, `Case-study question ${part + 1} of ${q.questions.length}`),
        h("p", { class: "qtext sm" }, sub.stem),
        body,
        h("div", { class: "actions top" },
          h("button", { class: "btn ghost sm", type: "button", disabled: part === 0, onclick: () => on.part(part - 1) }, "Previous question"),
          h("button", { class: "btn ghost sm", type: "button", disabled: part === q.questions.length - 1, onclick: () => on.part(part + 1) }, "Next question")))));
}

function examRunner(X, startMode) {
  examMode(true);
  stopClock();
  const isLocked = (k) => (X.locked || []).includes(k);
  // Where each item starts in the count of questions: a yes/no series of three is three questions.
  const qno = [];
  X.items.reduce((n, q, k) => { qno[k] = n; return n + sizeOf(q); }, 1);
  const total = X.items.reduce((n, q) => n + sizeOf(q), 0);
  const numText = (k) => { const n = qno[k], s = sizeOf(X.items[k]); return s > 1 ? `${n}–${n + s - 1}` : `${n}`; };
  const firstOpen = X.items.findIndex((q, k) => partsDone(q) < sizeOf(q) && !isLocked(k));
  let cur = X.inside !== null && X.inside !== undefined && !isLocked(X.inside) ? X.inside : Math.max(0, firstOpen);
  let mode = startMode === "review" ? "review" : "q";
  let ending = false;
  let heard = null;  // the reply to the podcast question, when this exam asks one
  let leaving = null; // where the learner asked to go from inside a section that locks once left
  let tab = 0, part = firstOpenPart(X.items[cur] || {});
  const target = Date.now() + X.seconds_left * 1000;
  const wrap = h("div", { class: "exam" });
  const clock = h("span", null, `${clockText(X.seconds_left)} left`);
  const timer = h("span", { class: "timer", role: "timer", "aria-label": "Time left" }, icon("clock", "sm"), clock);
  const status = h("span", { class: "small muted", role: "status" });

  // Dojo cannot see a podcast app. When an episode could be on the phone, the exam asks before it closes,
  // and only an explicit "no" lets it claim exam conditions.
  const podcastAsk = (line) => {
    const opts = [];
    const opt = (value, text) => {
      const input = h("input", { type: "radio", name: `podx-${X.id}`, value: value ? "yes" : "no", checked: heard === value });
      const label = h("label", { class: "podq-opt" + (heard === value ? " on" : "") }, input, h("span", { class: "dot" }), h("span", null, text));
      input.addEventListener("change", () => {
        heard = value;
        opts.forEach((o) => o.classList.toggle("on", o === label));
        if (line.textContent === PODCAST_ASK) line.textContent = "";
      });
      opts.push(label);
      return label;
    };
    return h("fieldset", { class: "podq" },
      h("legend", null, icon("listen", "sm"), h("span", null, "Did you listen to a lesson in a podcast app while this exam was open?")),
      h("p", null, "Episodes from your podcast feed may be on your phone, and Dojo cannot see what your podcast app plays, so it asks."),
      h("div", { class: "podq-opts" }, opt(true, "Yes, I listened"), opt(false, "No, I did not")),
      h("p", null, "Yes: this exam's conditions are recorded as unknown. No: the result notes what you said."));
  };
  const askFirst = (line) => {
    line.textContent = PODCAST_ASK;
    const first = wrap.querySelector(".podq input");
    if (first) first.focus();
  };

  // The clock has run out, but the exam stays open on the server until this reply is sent with it.
  const timeUp = () => {
    ending = true;
    stopClock();
    const line = h("span", { class: "small muted" });
    put(wrap, panel(eyebrow("alert", "Time is up"),
      h("h2", null, "Time is up. One question before your results."),
      h("p", { class: "sub" }, "Your answers are already on the server, and none can be changed now."),
      podcastAsk(line),
      h("div", { class: "actions top" }, button("See my results", () => {
        if (heard === null) { askFirst(line); return; }
        ending = false;
        finish("time");
      }), line)));
  };

  const finish = async (reason) => {
    if (ending) return;
    if (X.podcast_question && heard === null) {
      if (reason === "time") { timeUp(); return; }
      if (mode !== "review") { move(-1, true); }
      askFirst(status);
      return;
    }
    ending = true;
    stopClock();
    put(wrap, panel(eyebrow(reason === "time" ? "alert" : "check", reason === "time" ? "Time is up" : "Finishing"),
      h("h2", null, reason === "time" ? "Time is up. Dojo is closing the exam." : "Marking your answers…"),
      h("p", { class: "sub" }, "Your answers are already on the server. The right answers and the reasons come next.")));
    await settled();   // the answers already sent are stored before the exam closes
    const body = X.podcast_question ? { reason, podcast_heard: heard } : { reason };
    try { await api(`/api/exams/${X.id}/submit`, { method: "POST", body }); }
    catch (e) {
      // The deadline may have closed it first; the results page tells the story. A podcast question
      // this page did not know about sends the learner back to the review, where it is now shown.
      if (String(e.message || "").startsWith("Answer the podcast question first")) {
        examMode(false);
        const to = `#/exam/${X.id}/review`;
        if (location.hash !== to) { location.hash = to; return; }
      }
    }
    examMode(false);
    route();
  };

  const answered = () => X.items.reduce((n, q) => n + partsDone(q), 0);

  // The server said no. An ended exam goes to its results; a locked section or a stale page is drawn
  // again from what the server holds.
  const failed = (e) => {
    status.textContent = e.message;
    if (/time is up|has ended|closed/i.test(e.message || "")) { stopClock(); examMode(false); ending = true; route(); return; }
    if (e.status === 409) {
      api(`/api/exams/${X.id}`).then((now) => {
        if (now.state !== "open") { stopClock(); examMode(false); ending = true; route(); return; }
        Object.assign(X, { items: now.items, locked: now.locked, inside: now.inside });
        if (isLocked(cur)) { const k = X.items.findIndex((_, i) => !isLocked(i)); cur = k < 0 ? 0 : k; if (k < 0) mode = "review"; }
        render();
        status.textContent = e.message;
      }).catch(() => {});
    }
  };

  // Answers, flags and moves go to the server one at a time, in the order they were made, so leaving a
  // section can never overtake an answer still on its way. Every answer carries the item's write count
  // (rev): the server ignores one older than what it holds, and only the reply to the latest write is drawn.
  let writes = Promise.resolve();
  const queue = (fn) => { const run = writes.then(fn); writes = run.catch(() => false); return run; };
  const settled = () => writes;

  const send = (q, body) => {
    if (ending) return Promise.resolve(false);
    const rev = (q.rev = (q.rev || 0) + 1);
    return queue(async () => {
      try {
        const res = await api(`/api/exams/${X.id}/answer`, { method: "POST", body: { index: q.index, rev, ...body } });
        if (res.locked) X.locked = res.locked;
        if (rev !== q.rev) return false;   // a later change of this item is on its way; its reply decides
        if (res.stale) q.rev = res.rev;     // another tab wrote later: show what the server holds
        if (kindOf(q) === "single") { if (res.stale) { q.choice = res.choice; render(); } }
        else if (res.response) q.response = res.response;
        else delete q.response;
        return true;
      } catch (e) { failed(e); return false; }
    });
  };

  const pick = async (k) => {
    const q = X.items[cur];
    if (!q || kindOf(q) !== "single" || k < 0 || k >= q.options.length || ending) return;
    q.choice = k;
    render();
    await send(q, { choice: k });
  };
  // Every other kind draws the answer at once and sends it; the server's copy of the latest write replaces it.
  const respond = (q, response, extra) => {
    if (ending) return;
    q.response = response;
    render();
    send(q, { response, ...(extra || {}) }).then((ok) => { if (ok) render(); });
  };
  const toggleFlag = async () => {
    const q = X.items[cur];
    if (!q || ending) return;
    q.flagged = !q.flagged;
    const flagged = q.flagged;
    render();
    await queue(async () => {
      try { await api(`/api/exams/${X.id}/flag`, { method: "POST", body: { index: q.index, flagged } }); }
      catch (e) { q.flagged = !flagged; failed(e); }
    });
  };

  // The server keeps the locks: it is told where the learner goes, so a reload or a second tab cannot reopen
  // a section. The move waits for every answer sent before it.
  const visit = (k) => {
    if (ending) return;
    queue(() => api(`/api/exams/${X.id}/visit`, { method: "POST", body: { index: k } })
      .then((res) => {
        const was = (X.locked || []).join();
        X.locked = res.locked; X.inside = res.inside;
        if (res.locked.join() !== was) render();
      })
      .catch(failed));
  };
  const leaves = (to) => mode === "q" && to !== cur && LOCKING_KINDS.includes(kindOf(X.items[cur])) && !isLocked(cur);
  // to: an item, or -1 for the review screen. Leaving a yes/no series or a case study asks first.
  const move = (to, sure) => {
    if (ending || to < -1 || to >= X.items.length) return;
    if (to >= 0 && isLocked(to)) { status.textContent = X.locked_note; return; }
    if (to === cur && mode === "q") return;
    if (!sure && leaves(to)) { leaving = to; render("leave-stay"); return; }
    if (leaves(to)) X.locked = [...(X.locked || []), cur].sort((a, b) => a - b);
    leaving = null;
    status.textContent = "";
    if (to === -1) mode = "review";
    else { cur = to; mode = "q"; tab = 0; part = firstOpenPart(X.items[to]); }
    render();
    visit(to);
  };
  const go = (k) => { if (k >= 0) move(k); };
  const review = () => move(-1);

  // Microsoft Learn beside an open-book exam: Dojo counts the lookups and the time this page is out of
  // view. It cannot see what is read there.
  const learnCount = h("span", { class: "small muted learnct", "aria-live": "polite" });
  const learnText = () => {
    const l = X.learn || {};
    const mins = Math.round((l.away_seconds || 0) / 60);
    learnCount.textContent = `${plural(l.lookups || 0, "lookup")} · ${mins} min away`;
  };
  const lookup = () => {
    if (!X.open_book || ending) return;
    window.open(safeUrl((X.open_book_rule && X.open_book_rule.url) || "https://learn.microsoft.com/"), "_blank", "noopener");
    X.learn = { ...(X.learn || {}), lookups: ((X.learn && X.learn.lookups) || 0) + 1 };
    learnText();
    api(`/api/exams/${X.id}/learn`, { method: "POST", body: { event: "open" } }).then((r) => { X.learn = r; learnText(); }).catch(failed);
  };
  let awaySince = null;
  const awayStart = () => { if (awaySince === null && !ending) awaySince = Date.now(); };
  const awayEnd = () => {
    if (awaySince === null) return;
    const secs = Math.round((Date.now() - awaySince) / 1000);
    awaySince = null;
    if (secs < 1 || ending) return;
    api(`/api/exams/${X.id}/learn`, { method: "POST", body: { event: "away", seconds: secs } })
      .then((r) => { X.learn = r; learnText(); }).catch(() => {});
  };
  if (X.open_book) {
    const onVis = () => (document.visibilityState === "hidden" ? awayStart() : awayEnd());
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("blur", awayStart);
    window.addEventListener("focus", awayEnd);
    state.away = () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("blur", awayStart);
      window.removeEventListener("focus", awayEnd);
    };
    learnText();
  }

  const unitName = (q) => (kindOf(q) === "case" ? `${q.title || "Case study"}` : q.stem);
  const doneText = (q) => {
    const d = partsDone(q), s = sizeOf(q);
    if (kindOf(q) === "single") return "choice" in q ? `answered ${LETTERS[q.choice]}` : "not answered";
    if (s > 1) return d ? `${d} of ${s} answered` : "not answered";
    return d ? "answered" : "not answered";
  };

  const navGrid = () => h("div", { class: "navgrid" }, X.items.map((q, k) => {
    const lock = isLocked(k);
    const full = partsDone(q) >= sizeOf(q);
    const cls = ["sq", k === cur && mode === "q" ? "cur" : full ? "done" : partsDone(q) ? "part" : "", lock ? "locked" : "", sizeOf(q) > 1 ? "multi" : ""].filter(Boolean).join(" ");
    return h("button", { class: cls, type: "button", disabled: lock, "data-fk": `sq-${k}`,
      "aria-label": `${sizeOf(q) > 1 ? "Questions" : "Question"} ${numText(k)}, ${KIND_LABELS[kindOf(q)].toLowerCase()}, ${doneText(q)}${q.flagged ? ", flagged" : ""}${lock ? ", locked" : ""}`,
      onclick: () => go(k) },
      numText(k), q.flagged ? icon("flag", "fl") : null, lock ? icon("lock", "lk") : null);
  }));

  const navPane = () => {
    const done = answered();
    const marks = X.items.filter((q) => q.flagged && !isLocked(q.index));
    const sections = X.items.some((q) => LOCKING_KINDS.includes(kindOf(q)));
    return h("aside", { class: "xnav" }, h("section", { class: "panel stack" },
      h("div", { class: "panel-h tight" }, eyebrow("practice", "Question navigator"),
        h("span", { class: "small muted" }, `${numText(cur)} / ${total}`)),
      h("p", { class: "small muted" }, `${done} answered · ${marks.length} flagged · ${total - done} to go`),
      navGrid(),
      h("div", { class: "nleg" },
        h("span", null, h("i", { class: "lgsq done" }), "answered"),
        h("span", null, h("i", { class: "lgsq done" }, icon("flag", "fl")), "flagged"),
        h("span", null, h("i", { class: "lgsq cur" }), "here"),
        h("span", null, h("i", { class: "lgsq empty" }), "not answered"),
        sections ? h("span", null, h("i", { class: "lgsq locked" }), "locked") : null),
      h("div", { class: "actions top" }, button("Review all", review, "ghost")),
      marks.length ? h("div", { class: "divider" }) : null,
      marks.length ? eyebrow("flag", "Flagged for review") : null,
      marks.length ? h("div", { class: "chips top" }, marks.map((q) => h("button", { class: "chip line fchip", type: "button", onclick: () => go(q.index) },
        icon("flag", "xs"), `Q${numText(q.index)}`))) : null,
      h("div", { class: "divider" }),
      eyebrow("check", "Before you finish"),
      h("ul", { class: "tips top" },
        h("li", null, icon("check", "sm"), h("span", null, `Answer all ${total}: an unanswered question earns nothing, and a wrong answer costs nothing.`)),
        h("li", null, icon("refresh", "sm"), h("span", null, sections
          ? "You can change an answer until you end the exam, except in a yes/no series or a case study you have left."
          : "You can change any answer until you end the exam.")),
        h("li", null, icon("clock", "sm"), h("span", null, "The deadline is kept by the server; answers after it are not taken.")),
        h("li", null, icon("record", "sm"), h("span", null, "No question here fills a pip.")))));
  };

  const reviewPane = () => {
    const open = X.items.filter((q) => partsDone(q) < sizeOf(q) && !isLocked(q.index));
    const marks = X.items.filter((q) => q.flagged && !isLocked(q.index));
    const missing = total - answered();
    const row = (q) => {
      const lock = isLocked(q.index);
      return h("button", { class: "revrow" + (lock ? " locked" : ""), type: "button", disabled: lock, onclick: () => go(q.index) },
        h("span", { class: "n" }, numText(q.index)),
        h("span", { class: "s" }, unitName(q)),
        h("span", { class: "tag" }, kindOf(q) === "single" ? "" : `${KIND_LABELS[kindOf(q)]} · `, doneText(q), q.flagged ? " · flagged" : "", lock ? " · locked" : ""),
        lock ? icon("lock", "sm") : icon("right", "sm"));
    };
    const send_ = button("Submit the exam", () => finish("submitted"));
    return h("div", { class: "qpane" },
      h("div", { class: "panel-h" }, eyebrow("check", "Before you finish"), timer),
      h("h2", null, `${answered()} of ${total} answered`),
      h("p", { class: "sub" }, missing ? `${missing} question${missing === 1 ? "" : "s"} still have no answer. An unanswered question earns nothing and a wrong answer costs nothing, so a guess is free.`
        : "Everything has an answer."),
      marks.length ? h("p", { class: "sub" }, `${marks.length} flagged for review.`) : null,
      (X.locked || []).length ? h("p", { class: "small muted" }, X.locked_note) : null,
      h("div", { class: "revlist" }, (open.length || marks.length ? [...open, ...marks.filter((q) => partsDone(q) >= sizeOf(q))] : X.items).map(row)),
      X.podcast_question ? podcastAsk(status) : null,
      h("div", { class: "qfoot" },
        h("div", { class: "actions" }, button("Back to the questions", () => { mode = "q"; render(); visit(cur); }, "ghost")),
        h("div", { class: "actions" }, status, send_)));
  };

  // Leaving a yes/no series or a case study: as in the real exam, there is no way back.
  const leavePane = () => {
    const q = X.items[cur];
    const left = sizeOf(q) - partsDone(q);
    const what = kindOf(q) === "case" ? "case study" : "yes/no series";
    return h("div", { class: "leave", role: "alertdialog", "aria-labelledby": "leave-h", "aria-describedby": "leave-p" },
      h("h2", { id: "leave-h" }, `Leave this ${what}?`),
      h("p", { id: "leave-p", class: "sub" }, `Once you leave it you cannot come back to it, as in the real exam. `,
        left ? `${left} of its ${sizeOf(q)} questions ${left === 1 ? "has" : "have"} no answer yet.` : "All of its questions have an answer."),
      h("div", { class: "actions top" },
        h("button", { class: "btn", type: "button", "data-fk": "leave-go", onclick: () => move(leaving, true) }, `Leave the ${what}`),
        h("button", { class: "btn ghost", type: "button", "data-fk": "leave-stay", onclick: () => { leaving = null; render(); } }, "Stay here")));
  };

  const body = (q) => {
    const k = kindOf(q);
    if (k === "single") {
      return optionRows(`q${q.index}`, q.options, "choice" in q ? [q.choice] : [], false, (n) => pick(n), `Question ${numText(cur)}`);
    }
    if (k === "multi") return multiRows(`q${q.index}`, q, (q.response && q.response.choices) || [], (next) => respond(q, { choices: next }));
    if (k === "sequence") return sequenceBox(q, (order) => respond(q, { order }));
    if (k === "match") return matchBox(q, (pairs) => respond(q, { pairs }));
    if (k === "hotarea") return hotBox(q, (picks) => respond(q, { picks }));
    if (k === "yesno") {
      return yesnoBox(q, async (n, v, ok) => {
        ok.disabled = true;
        if (await send(q, { response: { yes: v }, part: n })) render(n + 1 < q.statements.length ? `yn${q.index}-${n + 1}-y` : null);
        else ok.disabled = false;
      });
    }
    return caseBox(q, tab, part, {
      tab: (n, fk) => { tab = n; render(fk); },
      part: (n) => { if (n >= 0 && n < q.questions.length) { part = n; render(`cq${q.index}-${n}`); } },
      answer: (n, raw) => {
        const parts = { ...((q.response && q.response.parts) || {}) };
        if (raw.choices && !raw.choices.length) delete parts[String(n)]; else parts[String(n)] = raw;
        q.response = Object.keys(parts).length ? { parts } : null;
        render();
        send(q, { response: raw, part: n }).then((ok) => { if (ok) render(); });
      },
    });
  };

  const questionPane = () => {
    if (leaving !== null) return h("div", { class: "qpane" }, h("div", { class: "qmid" }, h("div", { class: "qwrap" }, leavePane())));
    const q = X.items[cur];
    const k = kindOf(q);
    return h("div", { class: "qpane" },
      h("div", { class: "qmid" }, h("div", { class: "qwrap" + (k === "case" ? " widecase" : "") },
        h("p", { class: "qnum" }, `${sizeOf(q) > 1 ? "Questions" : "Question"} ${numText(cur)}`, k === "single" ? "" : ` · ${KIND_LABELS[k]}`,
          q.points > 1 && k !== "yesno" && k !== "case" ? ` · ${q.points} points` : "", q.flagged ? " · flagged for review" : ""),
        k === "case" ? h("h2", { class: "casetitle" }, q.title) : null,
        h("p", { class: "qtext" }, q.stem),
        body(q))),
      h("div", { class: "qfoot" },
        h("div", { class: "actions" },
          h("button", { class: "btn ghost", type: "button", "data-fk": "nav-prev", onclick: () => go(cur - 1), disabled: cur === 0 || isLocked(cur - 1) },
            icon("back", "sm"), "Previous"),
          h("span", { class: "keys" }, k === "single" ? "A–D to answer · ← → to move · F to flag · R to review" : "← → to move · F to flag · R to review")),
        h("div", { class: "actions" }, status,
          cur === X.items.length - 1 ? button("Review and finish", review)
            : h("button", { class: "btn", type: "button", "data-fk": "nav-next", onclick: () => go(cur + 1) }, "Next", icon("right", "sm")))));
  };

  const render = (focusKey) => {
    const a = document.activeElement;
    const fk = focusKey || (a && wrap.contains(a) && a.dataset ? a.dataset.fk : null);
    const done = answered();
    const bar = h("div", { class: "xprog" }, wide(h("i"), `${Math.round(100 * done / Math.max(1, total))}%`));
    const q = X.items[cur];
    put(wrap,
      h("header", { class: "xbar" },
        h("div", { class: "xseg" }, h("span", { class: "pe" }, "Practice exam"), chip(X.exam, "line"),
          chip(LENGTH_NAME[X.length] || X.length, "line")),
        h("div", { class: "xseg center" }, mode === "review" ? "Review before you finish"
          : [h("b", null, `${sizeOf(q) > 1 ? "Questions" : "Question"} ${numText(cur)}`), ` of ${total}`]),
        h("div", { class: "xseg right" }, timer,
          X.open_book ? h("button", { class: "btn ghost sm", type: "button", "data-fk": "learn", onclick: lookup,
            "aria-label": "Open Microsoft Learn in a new window. The exam clock keeps running." }, icon("read", "sm"), "Microsoft Learn") : null,
          X.open_book ? learnCount : null,
          mode === "q" && leaving === null ? h("button", { class: "btn ghost sm", type: "button", "data-fk": "flag", onclick: toggleFlag },
            icon("flag", "sm"), q && q.flagged ? "Unmark" : "Mark for review") : null,
          h("button", { class: "btn ghost sm", type: "button", onclick: review }, icon("x", "sm"), "End exam"))),
      bar,
      h("div", { class: "xbody" }, mode === "review" ? reviewPane() : questionPane(), mode === "review" ? null : navPane()),
      h("footer", { class: "xinfo" },
        h("span", { class: "it" }, icon("lock", "sm"), h("span", null, h("b", null, "Exam mode: "), X.open_book
          ? "Microsoft Learn is allowed, as in the real exam; no hints and no coach. Answers and reasons appear after you finish."
          : "no hints, no coach, no sources. Answers and reasons appear after you finish.")),
        h("span", { class: "vsep" }),
        h("span", { class: "it" }, icon("shield", "sm"), h("span", null, "Questions are written by Dojo from the study guide and the official docs, and checked by an independent model — ", h("b", null, "never from recalled exam content."))),
        h("span", { class: "vsep" }),
        h("span", { class: "it" }, icon("record", "sm"), h("span", null, X.note))));
    if (fk) {
      const n = wrap.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
      if (n && !n.disabled) n.focus();
      else if (n) { const alt = n.closest("li, .actions"); const b = alt && alt.querySelector("button:not([disabled]), input, select"); if (b) b.focus(); }
    }
  };

  state.timer = setInterval(() => {
    const left = Math.max(0, Math.round((target - Date.now()) / 1000));
    clock.textContent = `${clockText(left)} left`;
    timer.classList.toggle("low", left <= 300);
    if (left <= 0) finish("time");
  }, 1000);
  state.keys = (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || ending) return;
    const t = e.target;
    if (t && (t.tagName === "TEXTAREA" || t.tagName === "SELECT" || (t.tagName === "INPUT" && t.type !== "radio"))) return;
    if (t && t.closest && t.closest("[role=tablist], .ynbox, .case")) return;
    const k = (e.key || "").toLowerCase();
    if (mode !== "q" || leaving !== null) return;
    const single = kindOf(X.items[cur]) === "single";
    if (k === "arrowright" || k === "n") go(cur + 1);
    else if (k === "arrowleft" || k === "p") go(cur - 1);
    else if (k === "f") toggleFlag();
    else if (k === "r") review();
    else if (single && "abcdef".includes(k) && k.length === 1) pick("abcdef".indexOf(k));
    else if (single && "123456".includes(k) && k.length === 1) pick(Number(k) - 1);
    else return;
    e.preventDefault();
  };
  document.addEventListener("keydown", state.keys);
  render();
  if (mode === "q") visit(cur);
  return wrap;
}

function examScorePanel(X) {
  const possible = X.points || X.questions;
  const missed = possible - X.correct;
  const byPoints = possible !== X.questions;
  return h("section", { class: "panel w430" },
    h("p", { class: "eyebrow" }, byPoints ? "Your points · Dojo's own questions" : "Your score · Dojo's own questions"),
    h("div", { class: "score" }, h("span", { class: "big" }, X.correct), h("span", { class: "den" }, `/ ${possible}`)),
    h("p", { class: "sub" }, byPoints
      ? `${X.correct} of ${possible} points · ${X.answered} of ${X.questions} questions answered · ${X.new_questions} new to you`
      : `${missed} missed · ${X.answered} of ${X.questions} answered · ${X.new_questions} new to you`),
    X.partial ? h("p", { class: "small muted" }, `${plural(X.partial, "question")} earned part of ${X.partial === 1 ? "its" : "their"} points. Nothing was taken off for a wrong answer.`) : null,
    h("p", { class: "note top" }, h("b", null, "Not a pass prediction. "), X.diagnosis_note.replace("Not a pass prediction. ", "")),
    X.dropped ? h("p", { class: "small muted top" }, `${X.dropped} question${X.dropped === 1 ? " was" : "s were"} dropped because ${X.dropped === 1 ? "it" : "they"} did not pass the checks.`) : null,
    X.source_dropped ? h("p", { class: "small muted top" }, `${X.source_dropped} question${X.source_dropped === 1 ? " was" : "s were"} left out at the start because a source page changed.`) : null,
    X.withdrawn ? h("p", { class: "conf top" }, icon("refresh", "xs"),
      h("span", null, `${X.withdrawn_note} ${X.withdrawn === 1 ? "It is" : "They are"} not scored and not counted here.`, X.thinned_note ? ` ${X.thinned_note}` : "")) : null,
    h("div", { class: "divider" }),
    h("p", { class: "cond" }, icon(X.conditions === "exam" ? "shield" : "alert", "sm"),
      h("span", null, h("b", null, X.conditions === "exam" ? "Exam conditions. " : "Conditions unknown. "),
        X.learn_line ? X.conditions_note.replace(X.learn_line, "").trim() : X.conditions_note)),
    X.learn_line ? h("p", { class: "cond" }, icon("read", "sm"), h("span", null, h("b", null, "Open book. "), X.learn_line)) : null,
    h("p", { class: "share-line" }, icon("record", "sm"),
      h("span", null, "Shown next to your record, never inside it: selected-response answers fill no pips.")),
    h("p", { class: "scorefoot" }, icon("shield", "sm"), X.note));
}

function examDomainTable(X) {
  return h("section", { class: "panel grow" },
    h("div", { class: "panel-h" }, eyebrow("scale", "Where the points went · by exam domain"),
      h("span", { class: "small muted" }, `bars show points earned ÷ points possible · ${X.weak_gap} points or more below your own average in amber`)),
    h("div", { class: "dscroll" }, h("table", { class: "t dtbl" },
      h("thead", null, h("tr", null, h("th", null, "Domain"), h("th", { class: "num" }, "weight"),
        h("th", { class: "num" }, "questions"), h("th", { class: "num" }, "points"), h("th", { class: "res" }, "result"))),
      h("tbody", null, X.by_domain.map((d) => {
        const most = d.points === undefined ? d.questions : d.points;
        const share = most ? (d.correct * 100 / most) : 0;
        return h("tr", null, h("td", null, h("span", { class: "dtag" }, d.id.toUpperCase()), h("b", null, d.domain),
            d.weak ? chip("weakest", "amber") : null, d.thinned ? chip("too few left to judge", "amber") : null,
            d.withdrawn ? h("small", { class: "muted" }, ` ${d.withdrawn} withdrawn`) : null),
          h("td", { class: "num w" }, d.weight || "—"), h("td", { class: "num" }, d.questions), h("td", { class: "num" }, `${d.correct} / ${most}`),
          h("td", { class: "res" }, h("div", { class: "bar" }, wide(h("i", { class: d.weak ? "amber" : "" }), `${share.toFixed(1)}%`))));
      })))),
    h("p", { class: "note top" }, `Your average in this exam was ${X.average}%. A domain is called weakest when it sits ${X.weak_gap} points or more below it and had at least ${X.weak_min} questions to judge it on — the same definition the readiness rules use.`,
      X.by_domain.some((d) => !d.judged) ? ` ${X.by_domain.filter((d) => !d.judged).length} of ${X.by_domain.length} domains had fewer than ${X.weak_min} questions here, too few to judge either way.` : "",
      " Domains, weights and question counts come from the published study guide. Dojo never turns this into Microsoft's scaled score and never draws a pass line."));
}

function examPatterns(X) {
  const steps = X.next_steps || [];
  const step = (m) => h("div", { class: "pat" },
    h("div", null, h("span", { class: "dtag" }, m.domain || "—"), h("b", null, m.text || m.skill),
      h("small", null, `${m.misses} missed${m.unanswered ? ` · ${m.unanswered} left unanswered` : ""}`)),
    h("div", { class: "rlinks" },
      m.lesson ? h("a", { class: "link", href: `#/lesson/${m.lesson}/watch` }, "Watch") : null,
      m.lesson ? h("a", { class: "link", href: `#/lesson/${m.lesson}/read` }, "Read") : null,
      m.lesson ? h("a", { class: "link", href: `#/lesson/${m.lesson}/listen` }, "Listen") : null,
      m.lesson ? null : h("a", { class: "link", href: `#/skill/${X.package}/${m.skill}` }, "Study this skill"),
      button("New item, no help", () => act("check", X.package, m.skill), "ghost sm")));
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("target", "What the misses have in common"),
      h("span", { class: "small muted" }, "grouped by domain and skill")),
    X.patterns.length
      ? h("p", { class: "sub" }, `${X.patterns.length} skill${X.patterns.length === 1 ? "" : "s"} you missed more than once.`)
      : h("p", { class: "sub" }, "No skill was missed more than once, so there is no pattern to name. Dojo does not invent one."),
    X.single_misses ? h("p", { class: "small muted" }, `${X.single_misses} other skill${X.single_misses === 1 ? " was" : "s were"} missed once.`) : null,
    steps.length ? h("div", { class: "divider wide" }) : null,
    steps.length ? eyebrow("plan", "Next steps") : null,
    steps.map(step),
    steps.length ? h("p", { class: "note top" }, "There is no calendar yet, so these are next steps, not appointments.") : null);
}

// What the learner answered and what the key says, for every kind but single choice (ADR 0012).
function reviewLines(q) {
  const k = kindOf(q), r = q.response || {};
  const opt = (list, i) => (i === null || i === undefined || !list || list[i] === undefined ? "nothing" : list[i]);
  const row = (label, yours, right) => h("li", { class: yours === right ? "ok" : "bad" },
    label ? h("b", { class: "rlab" }, label) : null, `you: ${yours}`, yours === right ? " · right" : ` · answer: ${right}`);
  const sub = (s, got, label) => (kindOf(s) === "multi"
    ? row(label, got && got.choices ? got.choices.map((i) => s.options[i]).join("; ") : "nothing", (s.answers || []).map((i) => s.options[i]).join("; "))
    : row(label, opt(s.options, got ? got.choice : null), opt(s.options, s.answer)));
  let items = [];
  if (k === "multi") items = [sub(q, q.response, "Choices")];
  else if (k === "sequence") {
    items = [row("Order", r.order ? r.order.map((i) => q.options[i]).join(" → ") : "nothing", (q.order || []).map((i) => q.options[i]).join(" → "))];
  } else if (k === "match") items = q.targets.map((t, n) => row(t, opt(q.options, (r.pairs || [])[n]), opt(q.options, (q.pairs || [])[n])));
  else if (k === "hotarea") items = q.blanks.map((b, n) => row(`[ ${n + 1} ]`, opt(b.options, (r.picks || [])[n]), opt(b.options, (q.blank_answers || [])[n])));
  else if (k === "yesno") {
    const yn = (v) => (v === true ? "Yes" : v === false ? "No" : "nothing");
    items = q.statements.map((s, n) => row(s, yn((r.yes || [])[n]), yn((q.yes || [])[n])));
  } else if (k === "case") {
    items = q.questions.map((s, n) => sub(s, casePart(q, n), `Question ${n + 1}. ${s.stem}`));
  }
  return h("ul", { class: "rkey" }, items);
}

function reviewQuotes(q) {
  return (q.quotes || []).map((x) => h("blockquote", { class: "quote" }, x.part ? h("b", { class: "qpart" }, `${x.part}: `) : null, `“${x.quote}”`,
    h("footer", null, "From ", x.source ? ext(x.source.url, x.source.title) : "the source", ", word for word")));
}

function examReview(X) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("record", "Every question, with the reason"), linkButton("Practice", "#/rehearsal", "ghost sm")),
    h("div", { class: "revgrid" }, X.items.map((q) => {
      const many = kindOf(q) !== "single";
      const done = many ? partsDone(q) > 0 : "choice" in q;
      if (q.withdrawn) {
        return h("div", { class: "rev" },
          h("span", { class: "rmk held" }),
          h("div", null, h("b", null, many && kindOf(q) === "case" ? q.title : q.stem),
            h("div", { class: "rchoice" }, many ? (done ? "answered · withdrawn, not scored" : "not answered · withdrawn, not scored")
              : done ? `you chose ${"ABCDEF"[q.choice]} · withdrawn, not scored` : "not answered · withdrawn, not scored"),
            h("p", { class: "ex" }, "Its official source changed during the exam, so its answer may no longer hold. It is not scored, and its answer is not shown."),
            h("p", { class: "rlinks" }, q.source ? ext(q.source.url, q.source.title || "The source page") : null,
              q.skill ? h("a", { class: "link small", href: `#/skill/${X.package}/${q.skill}` }, q.skill_text) : null)));
      }
      if (many) {
        const mark = q.correct ? "ok" : q.earned ? "part" : done ? "wrong" : "";
        return h("div", { class: "rev" },
          h("span", { class: `rmk ${mark}` }, done ? icon(q.correct ? "check" : q.earned ? "check" : "x", "xs bold") : null),
          h("div", null, h("p", { class: "small muted" }, `${KIND_LABELS[kindOf(q)]} · ${q.earned || 0} of ${q.points} point${q.points === 1 ? "" : "s"}`),
            h("b", null, kindOf(q) === "case" ? `${q.title}: ${q.stem}` : q.stem),
            kindOf(q) === "hotarea" ? h("p", { class: "hottext small" }, String(q.text || "").replace(/\[\[(\d+)\]\]/g, "[ $1 ]")) : null,
            reviewLines(q),
            q.rationale ? h("p", { class: "ex" }, q.rationale) : null,
            kindOf(q) === "case" ? q.questions.map((s, n) => (s.rationale ? h("p", { class: "ex" }, `Question ${n + 1}: ${s.rationale}`) : null)) : null,
            reviewQuotes(q),
            h("p", { class: "rlinks" }, h("a", { class: "link small", href: `#/skill/${X.package}/${q.skill}` }, q.skill_text),
              q.flagged ? h("span", { class: "small muted" }, "you flagged this") : null)));
      }
      return h("div", { class: "rev" },
        h("span", { class: `rmk ${done ? (q.correct ? "ok" : "wrong") : ""}` }, done ? icon(q.correct ? "check" : "x", "xs bold") : null),
        h("div", null, h("b", null, q.stem),
          h("div", { class: "rchoice" }, done ? `you chose ${"ABCDEF"[q.choice]} · answer ${"ABCDEF"[q.answer]}` : `not answered · answer ${"ABCDEF"[q.answer]}`),
          q.rationale ? h("p", { class: "ex" }, q.rationale) : null,
          q.quote ? h("blockquote", { class: "quote" }, `“${q.quote}”`,
            h("footer", null, "From ", q.source ? ext(q.source.url, q.source.title) : "the source", ", word for word")) : null,
          h("p", { class: "rlinks" }, h("a", { class: "link small", href: `#/skill/${X.package}/${q.skill}` }, q.skill_text),
            q.flagged ? h("span", { class: "small muted" }, "you flagged this") : null)));
    })));
}

function timeUpNote(X) {
  const note = "The exam closed itself at its deadline. Answers after the deadline were not taken, so ";
  // A withdrawn question is not scored (ADR 0006), so it is not an unanswered miss.
  return X.withdrawn
    ? note + "a scored question left open counts as unanswered. A withdrawn question is not scored, so it is not a miss."
    : note + "anything left open counts as unanswered.";
}

function examResultsPage(X) {
  const possible = X.points || X.questions;
  const head = pageHead(possible !== X.questions ? `${X.correct} of ${possible} points — here is where they went` : `${X.correct} of ${X.questions} — here is where the points went`, null,
    h("p", { class: "eyebrow" }, `Practice exam · ${LENGTH_NAME[X.length] || X.length} · ${fmtDate(X.closed_at || X.created)}`));
  return [examCrumb(fmtWhen(X.created)),
    X.closed_reason === "time"
      ? h("section", { class: "panel amber" }, eyebrow("clock", "Time ran out"), h("p", { class: "sub" }, timeUpNote(X)))
      : null,
    head,
    h("div", { class: "row stackable grow" }, examScorePanel(X), examDomainTable(X)),
    examPatterns(X),
    examReview(X)];
}

async function viewExam(aid, sub) {
  let X = await api(`/api/exams/${aid}`);
  if (X.state === "building") return examBuilding(X);
  if (X.state === "broken") return examBroken(X);
  if (X.state === "ready") return examBrief(X);
  if (X.state === "open") return examRunner(X, sub === "review" ? "review" : "q");
  // The reasons are shown now, so the Record gets the moment now: it is when the teaching starts.
  if (!X.rationales_shown) X = await api(`/api/exams/${aid}/rationales`, { method: "POST" });
  return examResultsPage(X);
}

// ---------------------------------------------------------------- should you book the exam?

const SAY_WORD = { book: "Book it", close: "Almost", "not-yet": "Not yet" };
const SURE_WORD = { low: "low", some: "some", good: "good" };

function ruleMark(r) {
  const part = !r.met && /^[1-9]/.test(String(r.value || ""));
  return h("span", { class: `mk${r.met ? " met" : part ? " part" : ""}` }, r.met ? icon("check", "xs bold") : null);
}

function ruleExtra(r, pid, refresh) {
  if (r.id === "rule1") {
    return h("div", null, h("p", { class: "small muted top" }, "Skills shown on your own and later, per domain:"),
      h("div", { class: "chips" }, r.domains.map((d) => chip(`${d.title}: ${d.later} of ${d.skills}`, d.met ? "sage" : "line"))));
  }
  if (r.id === "rule2") {
    if (!r.attempts.length) return h("p", { class: "small muted top" }, "No timed practice exam has been finished yet.");
    return h("div", { class: "top" }, r.attempts.map((a) => h("p", { class: "small muted" },
      `${fmtWhen(a.closed_at)} · ${a.correct} of ${a.points && a.points !== a.questions ? `${a.points} points` : a.questions} · ${a.new_share}% new · `,
      a.exam_conditions ? "exam conditions" : "conditions unknown",
      a.withdrawn ? ` · ${a.withdrawn} withdrawn (a source changed)` : "",
      a.current_blueprint ? null : " · an earlier blueprint of this exam",
      a.weak.length ? ` · weak: ${a.weak.map((w) => w.title).join(", ")}` : a.every_domain_judged ? " · no weak domain"
        : a.thinned && a.thinned.length ? ` · too few questions left to judge ${a.thinned.join(", ")}`
        : ` · too few questions to judge ${a.too_few.length} domain${a.too_few.length === 1 ? "" : "s"}`,
      r.counted.includes(a.id) ? " · counted" : " · not counted")));
  }
  if (r.id === "rule3") {
    if (!r.measured) return h("p", { class: "small muted top" }, r.note);
    return h("div", { class: "top" }, (r.open || []).map((o) => h("p", { class: "small muted" }, `Still open: ${o.wrong}`)));
  }
  const bits = [h("p", { class: "small muted top" }, r.note)];
  if (r.url) bits.push(h("p", { class: "rlinks" }, ext(r.url, "Open Microsoft's practice assessment")));
  if (r.available && !r.met) bits.push(h("div", { class: "actions top" }, button("I have taken it", async () => {
    try { await api("/api/readiness/official", { method: "POST", body: { package: pid } }); refresh(); }
    catch (e) { put($view(), errorPanel(e)); }
  }, "ghost sm")));
  if (r.taken_at) bits.push(h("p", { class: "small muted" }, `You told Dojo you took it on ${fmtWhen(r.taken_at)}. Dojo never asks for the result.`));
  return h("div", null, bits);
}

const MOVE_LABEL = { probe: "Probe, no help", check: "Check, no help", exam: "Timed practice exam" };

function moveRow(m, pid) {
  const action = m.move === "exam"
    ? linkButton("Start one", "#/rehearsal", "ghost sm")
    : button(MOVE_LABEL[m.move], () => act(m.move, pid, m.skill), "ghost sm");
  return h("div", { class: "mv" },
    h("span", null, m.rule.replace("rule", "rule ")),
    h("span", null, h("b", null, m.text), h("small", null, [m.domain, m.why].filter(Boolean).join(" · ")),
      m.lesson && m.skill ? h("p", { class: "rlinks" }, h("a", { class: "link", href: `#/skill/${pid}/${m.skill}` }, "the lesson")) : null),
    h("span", { class: "cr" }, action));
}

async function viewReadiness() {
  const pid = state.pid;
  // One call: the server computes the advice, keeps it in the record and returns exactly that. If it
  // cannot be kept the page says so and shows nothing, rather than advice with no trace in the record.
  const a = await api("/api/readiness", { method: "POST", body: { package: pid } });
  const refresh = () => route();
  const kept = a.kept;
  const r4 = a.rules[3];
  const exams = a.earlier_attempts
    ? `${a.attempts} practice exam${a.attempts === 1 ? "" : "s"} on this blueprint, ${a.earlier_attempts} on an earlier one`
    : `${a.attempts} finished practice exam${a.attempts === 1 ? "" : "s"}`;
  const hero = h("section", { class: "panel hero" },
    h("div", { class: "verdict" },
      h("div", null, h("p", { class: "eyebrow" }, `${a.exam} · Dojo's advice, not a prediction`),
        h("p", { class: "big" }, SAY_WORD[a.say] || a.say),
        h("p", { class: "sub" }, a.headline),
        h("p", { class: "sub" }, a.why)),
      h("div", { class: "sure" }, icon("scale", "sm"),
        h("span", null, h("b", null, `How sure: ${SURE_WORD[a.sure.level] || a.sure.level}`), h("small", null, a.sure.words),
          h("small", null, `${a.sure.skills_tested} of ${a.sure.skills} skills answered on your own · ${exams}`)))),
    h("div", { class: "divider wide" }),
    h("p", { class: "eyebrow" }, "Why, in Dojo's reading of your evidence"),
    h("ul", { class: "whylist" }, a.reasons.map((s) => h("li", null, s))),
    h("div", { class: "note top" }, a.scope.map((s) => h("p", null, s)),
      h("p", null, "This is Dojo reading your own record out loud, so you can disagree with the reasoning rather than with a number.")));
  const rules = h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("check", "What would make Dojo say “book it”"),
      h("span", { class: "small muted" }, "every rule, its definition and where you stand")),
    a.rules.map((r, i) => h("div", { class: "crit" }, ruleMark(r),
      h("div", null, h("b", null, `${i + 1} · ${r.title}`), h("small", null, r.definition), ruleExtra(r, pid, refresh)),
      h("span", { class: "st" }, r.value))),
    h("div", { class: "moves" }, eyebrow("plan", "What this week can move"),
      a.moves.length ? a.moves.map((m) => moveRow(m, pid)) : h("p", { class: "sub" }, "Nothing is waiting: every rule Dojo can measure is met."),
      h("p", { class: "note top" }, "There is no calendar in Dojo yet, so these are next steps, not appointments.")));
  const side = h("div", { class: "col side-col" },
    h("section", { class: "panel" }, eyebrow("record", "Kept in your record"),
      h("p", { class: "sub" }, "Every advice you are shown is stored with its inputs and the date, so a result you choose to share later can be held against it."),
      kept ? h("p", { class: "small muted" }, `This advice was kept on ${fmtDate(kept.at)}.`) : null,
      a.history.length ? h("div", { class: "divider" }) : null,
      a.history.map((d) => h("div", { class: "rv plain" }, h("span", null, h("b", null, SAY_WORD[d.say] || d.say),
        h("small", null, `${fmtDate(d.at)} · how sure: ${SURE_WORD[d.sure] || d.sure} · ${d.inputs.attempts} exam${d.inputs.attempts === 1 ? "" : "s"}`)),
        d.outcome ? chip(d.outcome, "line") : chip("no result shared", "line")))),
    h("section", { class: "panel" }, eyebrow("scale", "Know what you know"),
      h("p", { class: "sub" }, "How sure you were, next to how often you were right, is in your Record. It is a mirror for you: "
        + "it is not used in this advice."),
      h("div", { class: "actions top" }, linkButton("Open it in the Record", "#/record", "ghost sm"))),
    h("section", { class: "panel" }, eyebrow("shield", "Microsoft's own practice assessment"),
      r4.available
        ? h("div", null, h("p", { class: "sub" }, "Free, and the one check Dojo did not write."),
            h("p", { class: "rlinks" }, ext(r4.url, "Open it on Microsoft Learn")),
            h("p", { class: "small muted" }, r4.note))
        : h("div", null, h("p", { class: "sub" }, r4.value === "none verified" ? "None verified." : r4.value),
            h("p", { class: "small muted" }, r4.note)),
      h("p", { class: "note top" }, "Rule 4 never blocks the advice: Dojo will not hold a missing Microsoft page against you.")),
    h("section", { class: "panel" }, eyebrow("alert", "What Dojo will not do"),
      h("ul", { class: "tips" },
        h("li", null, icon("x", "sm"), h("span", null, "No probability of passing and no predicted score.")),
        h("li", null, icon("x", "sm"), h("span", null, "No mapping to Microsoft's 1–1000 scale and no pass line.")),
        h("li", null, icon("x", "sm"), h("span", null, "Nothing from how you click, how long you stay, or any other telemetry.")))));
  return [pageHead("Should you book the exam?", `${a.exam} · ${a.title}. Dojo's advice from your own record and your own practice exams.`,
      h("p", { class: "eyebrow" }, `Advice as of ${fmtDate(a.at)}`), helpQ("readiness")),
    h("div", { class: "row stackable grow" }, h("div", { class: "col grow" }, hero, rules), side)];
}

// ---------------------------------------------------------------- ask the coach

async function viewAsk(sid) {
  const [p, past] = await Promise.all([ensurePkg(), api(`/api/asks?package=${encodeURIComponent(state.pid)}`)]);
  const sel = h("select", { id: "askskill", "aria-label": "Skill" }, p.domains.map((d) => h("optgroup", { label: d.title },
    d.groups.flatMap((g) => g.skills).map((s) => h("option", { value: s.id, selected: s.id === sid }, s.text)))));
  const q = h("textarea", { rows: 4, maxlength: 1500, id: "askq2", placeholder: "Ask anything about this skill." });
  if (state.askDraft) { q.value = state.askDraft; state.askDraft = ""; }
  const mic = canSpeak() ? speakBox(q, { label: "Speak your question" }) : null;
  const status = h("p", { class: "small muted" });
  const go = button("Ask", async () => {
    if (q.value.trim().length < 3) { status.textContent = "Write a question first."; return; }
    if (mic) mic.dispose();  // the question is on its way: stop listening now
    const token = state.nav;
    try {
      const res = await withProgress("Answering from the official sources", api("/api/ask", { method: "POST", body: { package: state.pid, skill: sel.value, question: q.value.trim() } }));
      if (token === state.nav) location.hash = `#/answer/${res.ask}`;
    } catch (e) { if (token === state.nav) put($view(), errorPanel(e)); }
  });
  return [pageHead("Ask the coach", "Answers come only from the skill's official sources, with the exact words they rely on. Nothing is invented.", null, helpQ("ask-the-coach")),
    h("div", { class: "row stackable" },
      h("section", { class: "panel grow" }, eyebrow("chat", "Your question"),
        h("label", { class: "field", for: "askskill" }, "Skill", sel),
        h("label", { class: "field", for: "askq2" }, "Question", q),
        mic ? mic.el : null,
        h("div", { class: "actions top" }, go), status),
      h("section", { class: "panel w430" }, eyebrow("record", "Earlier questions"),
        past.length ? past.map((a) => h("a", { class: "near", href: `#/answer/${a.id}` }, h("b", null, a.question),
          h("span", { class: "small muted" }, fmtWhen(a.created)))) : h("p", { class: "sub" }, "None yet.")))];
}

async function viewAnswer(id) {
  const A = await api(`/api/ask/${id}`);
  return [
    h("nav", { class: "crumb" }, h("a", { href: "#/ask" }, "Ask"), h("span", { class: "sep" }, "›"),
      h("a", { href: `#/skill/${A.package}/${A.skill}` }, A.skill_text)),
    h("div", { class: "pageh" }, h("h1", null, A.question)),
    h("div", { class: "row stackable" },
      h("section", { class: "panel grow article" }, eyebrow("chat", "Answer"),
        A.points.length
          ? h("ul", { class: "points" }, A.points.map((p) => { const [b, qt] = quoteToggle(p, A.sources); return h("li", null, h("span", { class: "dotm" }), h("span", null, p.text, " ", b, qt)); }))
          : h("p", { class: "sub" }, "Nothing in the sources could be verified as an answer."),
        A.not_covered ? h("p", { class: "note top" }, h("strong", null, "Not covered by the sources: "), A.not_covered) : null,
        A.blocked.length ? h("p", { class: "conf" }, icon("alert", "xs"),
          h("span", null, `${A.blocked.length} part${A.blocked.length === 1 ? " was" : "s were"} removed because ${A.blocked.length === 1 ? "it" : "they"} did not hold. `,
            h("a", { class: "link", href: "#/quality" }, "The studio log lists them and why."))) : null),
      h("section", { class: "panel w430" }, eyebrow("file", "Sources"), sourceList(A.sources)))];
}

// ---------------------------------------------------------------- your record

// ---------------------------------------------------------------- know what you know (ADR 0011)
const CONF_ORDER = ["certain", "fair", "guess"];

function relearnLine(r) {
  if (!r) return null;
  let text;
  if (r.relearned) {
    text = `Relearned on ${fmtWhen(r.relearned_at)}: right with no help on ${r.needed} different days, spread out. `
      + `It still comes back now and then, every ${r.interval} days now.`;
  } else if (r.relearned_at) {
    text = `Relearned on ${fmtWhen(r.relearned_at)}. Not right once since, so it comes back sooner. That is normal.`;
  } else if (!r.started) {
    text = "Recalls start after your first answer with every point met and no help.";
  } else if (r.waiting_later) {
    text = "Its first recall is the later check.";
  } else {
    const when = r.due_today ? "today" : fmtDay(r.due);   // the server says, in the schedule's own day (Berlin)
    text = `Recall: right on ${r.on_time} of ${r.needed} different days so far. Next recall ${when}.`
      + (r.repair ? " It was not right last time, so it came back the next day." : "");
  }
  return h("p", { class: "cond" }, icon(r.relearned ? "check" : "bell", "sm"), h("span", null, text));
}

// One row of the calibration table: right out of answered per level, a rate only with enough answers.
function confCells(levels, min) {
  return CONF_ORDER.map((k) => {
    const x = levels[k];
    return h("td", { class: "num" }, !x.total ? h("span", { class: "muted" }, "–")
      : x.enough ? [h("b", null, `${x.rate}%`), h("small", null, ` ${x.right} of ${x.total}`)]
      : h("small", { class: "muted", title: `A rate shows from ${min} answers.` }, `${x.right} of ${x.total} · too few yet`));
  });
}

// The same counts in one sentence, for the narrow skill panel.
function confLine(levels, min) {
  const parts = CONF_ORDER.filter((k) => levels[k].total).map((k) => {
    const x = levels[k];
    return `${CONF_WORD[k]}: right ${x.right} of ${x.total}${x.enough ? ` (${x.rate}%)` : ""}`;
  });
  const few = CONF_ORDER.some((k) => levels[k].total && !levels[k].enough);
  return h("p", { class: "cond" }, icon("scale", "sm"),
    h("span", null, `How sure you were. ${parts.join(" · ")}.${few ? ` A rate shows from ${min} answers.` : ""}`));
}

function knowPanel(c, pid) {
  if (!c) return null;
  const head = h("thead", null, h("tr", null, h("th", null, ""), CONF_ORDER.map((k) => h("th", { class: "num" }, CONF_WORD[k]))));
  const rows = [h("tr", { class: "total" }, h("td", null, h("b", null, "This exam")), confCells(c.levels, c.min_answers))]
    .concat(c.domains.filter((d) => d.answers).map((d) => h("tr", null, h("td", null, d.title), confCells(d.levels, c.min_answers))));
  const errs = c.errors.map((e) => h("div", { class: "rv kerr" },
    h("span", null, h("b", null, e.text),
      h("small", null, `${e.label} · ${fmtWhen(e.at)} · ${e.kind === "mcq" ? "quick question" : "your own words"}`),
      e.stem ? h("small", { class: "kstem" }, e.stem) : null,
      e.where ? h("a", { class: "link small", href: sectionHref(e.where) },
        e.where.heading ? `Read “${e.where.heading}”` : "Read the lesson")
        : h("a", { class: "link small", href: `#/skill/${pid}/${e.skill}` }, "Open the skill")),
    button("Try a new question", () => act("check", pid, e.skill), "ghost sm")));
  return h("section", { class: "panel know" },
    h("div", { class: "panel-h" }, eyebrow("scale", "Know what you know"),
      chip(`${plural(c.answers, "answer")} with how sure you were`, "line")),
    h("p", { class: "sub" }, "How often you were right, next to how sure you said you were."),
    c.answers
      ? h("div", { class: "dscroll" }, h("table", { class: "t ktbl" }, head, h("tbody", null, rows)))
      : h("p", { class: "small muted" }, "Nothing yet. After each answer Dojo asks how sure you were. You can always skip it."),
    c.answers && c.answers < c.min_answers * 3 ? h("p", { class: "small muted" },
      `A rate shows from ${c.min_answers} answers at one level. Until then Dojo shows only the counts.`) : null,
    c.selfchecks.answers ? h("div", { class: "top" }, h("p", { class: "eyebrow" }, "Lesson self-checks · your own marks"),
      h("div", { class: "dscroll" }, h("table", { class: "t ktbl" }, head.cloneNode(true),
        h("tbody", null, h("tr", null, h("td", null, "Matched the answer"), confCells(c.selfchecks.levels, c.min_answers)))))) : null,
    h("div", { class: "divider" }),
    eyebrow("eye", SURE_NOT_YET),
    errs.length ? errs : h("p", { class: "small muted" }, "Nothing here right now. When you are sure and an answer is not right yet, it shows here, with the part of the lesson to look at."),
    c.relearned.length ? [h("div", { class: "divider" }), eyebrow("check", "Relearned"),
      h("p", { class: "small muted" }, "Right with no help on three different days, spread out. That tends to last."),
      c.relearned.map((r) => h("p", { class: "prow" }, icon("check", "xs"), h("span", null, h("b", null, r.text), ` · ${fmtWhen(r.at)}`)))] : null,
    h("p", { class: "note top" }, c.note));
}

function recordDetail(box, pid, sid, cal) {
  put(box, h("p", { class: "loading" }, "Loading…"));
  const token = state.nav;
  api(`/api/skills/${pid}/${sid}`).then((v) => {
    if (token !== state.nav) return;
    const s = v.skill, e = v.evidence;
    put(box,
      h("div", { class: "panel-h" }, eyebrow("course", s.domain_title), chip(skillTag(s.id), "line")),
      h("h2", null, s.text),
      h("div", { class: "bigpips" }, pipKinds(e.state).map((k, i) => h("div", { class: "bp" },
        h("span", { class: `bd ${k || "empty"}` }, k ? icon("check", "xs bold") : null),
        h("b", null, CONDITIONS[i]), h("small", null, i === 3 ? "not in v1" : k ? "shown" : "not yet")))),
      e.last_teaching ? h("p", { class: "cond" }, icon("read", "sm"), h("span", null, `Time since teaching counts from ${fmtDate(e.last_teaching)}.`)) : null,
      e.lessons_viewed ? h("p", { class: "cond" }, icon("eye", "sm"), h("span", null, `${e.lessons_viewed} lesson view${e.lessons_viewed === 1 ? "" : "s"} recorded.`)) : null,
      relearnLine(v.relearn),
      cal && cal.skills[sid] ? confLine(cal.skills[sid].levels, cal.min_answers) : null,
      attemptTimeline(e),
      h("p", { class: "share-line top note" }, icon("shield", "sm"),
        h("span", null, "Every line here is an event in the hash chain. Nothing is scored, nothing is predicted.")));
  }).catch((err) => { if (token === state.nav) put(box, h("p", { class: "sub" }, err.message)); });
}

async function viewRecord() {
  const [chain, p, pl, cal] = await Promise.all([api("/api/record/chain"), ensurePkg(), api("/api/play").catch(() => null),
    api(`/api/calibration?package=${encodeURIComponent(state.pid)}`).catch(() => null)]);
  // The export carries lessons, coach answers, item feedback and rehearsal rationales, so the server
  // closes it while any exam runs. The page says so instead of offering a link that cannot work.
  const running = await api(`/api/exams?package=${encodeURIComponent(state.pid)}`)
    .then((x) => x.running).catch(() => null);
  const withEvidence = allSkills(p).filter((s) => s.state !== "untested" || s.attempts || s.seen || s.disputed || s.rehearsal);
  const detail = h("section", { class: "panel grow stack scroll" }, h("p", { class: "sub" }, "Pick a skill to see every attempt Dojo recorded."));
  const list = h("div", { class: "rec-list" }, withEvidence.map((s) => {
    const el = h("button", { class: "rec", type: "button", onclick: () => {
      for (const n of list.querySelectorAll(".rec")) n.classList.remove("on");
      el.classList.add("on");
      recordDetail(detail, state.pid, s.id, cal);
    } }, h("span", { class: "rn" }, s.text), evidence(s));
    return el;
  }));
  if (withEvidence.length) { list.firstChild.classList.add("on"); recordDetail(detail, state.pid, withEvidence[0].id, cal); }
  const confirmBox = h("input", { type: "text", id: "delconfirm", placeholder: "Type DELETE" });
  const status = h("p", { class: "small muted" });
  const del = button("Delete my whole record", async () => {
    if (confirmBox.value !== "DELETE") { status.textContent = "Type DELETE to confirm."; return; }
    try { await api("/api/record/delete", { method: "POST", body: { confirm: "DELETE" } }); state.pkg = null; location.hash = "#/today"; }
    catch (e) { status.textContent = e.message; }
  }, "danger");
  const side = h("div", { class: "col w430" },
    shelfPanel(pl),
    h("section", { class: "panel" }, eyebrow("shield", "Integrity"),
      chain.ok
        ? h("p", { class: "status-sage" }, icon("check", "sm"), `${chain.events} events. Each one is chained to the one before it by a hash, and the whole chain checks out.`)
        : h("p", { class: "status-slate" }, icon("alert", "sm"), `The chain is broken at event ${chain.broken_at + 1} of ${chain.events}: the record was changed outside Dojo.`),
      h("p", { class: "small muted top" }, "The record is append-only. Disputed judgements stay visible but count neither way. An answer judged again keeps its earlier judgement visible; only the new one counts.")),
    h("section", { class: "panel" }, eyebrow("download", "Export"),
      h("p", { class: "sub" }, "Download your learning record as one JSON file: events, items, answers, judgements, practice exams, questions, studio notes and your settings. Answer keys stay out until the answer is judged or the exam has ended."),
      running
        ? h("p", { class: "small muted top" }, "Export is closed while a timed exam runs. Finish or end the exam first.")
        : h("div", { class: "actions top" }, h("a", { class: "btn ghost", href: "/api/record/export", download: "dojo-record.json" }, "Download my record"))),
    h("section", { class: "panel" }, eyebrow("x", "Delete"),
      h("p", { class: "sub" }, "This permanently deletes your events, items, answers, practice exams, questions and studio log. Lessons and narration audio are general study material and stay."),
      h("div", { class: "datefield" }, h("label", { class: "field", for: "delconfirm" }, "Type DELETE to confirm", confirmBox), del), status),
    state.me && state.me.team ? allowancePanel() : null,
    state.me && state.me.team ? displayNamePanel() : null,
    state.me && state.me.team && !isAdmin() ? leavePanel() : null);
  return [pageHead("Your record", "Everything Dojo keeps about your learning, in the order it happened.", null, helpQ("your-record")),
    h("div", { class: "row stackable grow recrow" },
      h("section", { class: "panel w330 stack" }, eyebrow("record", "Skills in your record"), list),
      detail, side),
    knowPanel(cal, state.pid)];
}

// ---------------------------------------------------------------- studio

const PIPE = [
  ["01", "Sources", "official pages only", "file"],
  ["02", "Author", "writes the lesson", "wand"],
  ["03", "Gate", "checks every element", "shield"],
  ["04", "You", "read, listen or present", "read"],
  ["05", "Grader", "must quote your words", "scale"],
  ["06", "Record", "hash-chained events", "record"],
];

async function viewQuality() {
  if (!isAdmin()) return viewStudioLog();
  const [log, a, v, c, dr, dl] = await Promise.all([api("/api/quality").catch((e) => ({ error: e.message })), api("/api/about").catch(() => null),
    api("/api/studio/voices").catch((e) => ({ error: e.message })), api("/api/studio/costs").catch((e) => ({ error: e.message })),
    api("/api/studio/drift").catch((e) => ({ error: e.message })), api("/api/studio/deeper").catch((e) => ({ error: e.message }))]);
  // During a timed exam the log is closed, because blocked sentences are still lesson text.
  const q = Array.isArray(log) ? log : [];
  const who = { "02": a && a.models.author, "03": a && a.models.gate, "05": a && a.models.grader };
  const pipe = h("div", { class: "pipe" }, PIPE.map(([num, name, note, ic]) => h("div", { class: num === "03" ? "pstage gate" : "pstage" },
    h("span", { class: "num" }, num), h("b", null, icon(ic, "sm"), " ", name), h("small", null, note),
    who[num] ? h("div", { class: "who" }, chip(who[num], "line")) : null)));
  const blocked = q.filter((x) => x.text);
  return [pageHead("Studio", "How your course is built, and everything the checker blocked on the way. Nothing is dropped silently.", null, helpQ("studio")),
    pipe,
    h("div", { class: "row stackable" }, voicesPanel(v), h("div", { class: "col w430" }, examsPanel(), costsPanel(c))),
    driftRow(dr),
    deeperRow(dl),
    h("div", { class: "row stackable grow studio-log" },
      h("section", { class: "panel grow stack scroll" },
        h("div", { class: "panel-h" }, eyebrow("alert", "What was blocked, and why"), Array.isArray(log) ? chip(`${q.length} entries`, "line") : null),
        !Array.isArray(log) ? h("p", { class: "sub" }, log.error || "The log could not be read.")
        : q.length ? q.map((x) => h("div", { class: "qentry" },
          h("div", { class: "qh" }, h("b", null, [x.kind, x.part].filter(Boolean).join(" · ")), h("span", { class: "small muted" }, fmtDate(x.at))),
          x.text ? h("p", { class: "blocked-line" }, x.text) : null,
          x.reason ? h("p", { class: "small ink2" }, h("b", null, "Why: "), x.reason) : null)) : h("p", { class: "sub" }, "Nothing so far.")),
      h("div", { class: "col w430" },
        h("section", { class: "panel" }, eyebrow("scale", "The rule"),
          h("p", { class: "sub" }, "Every model-written sentence you see has to survive an independent model from a different family, element by element. What does not hold is removed and written down here, with the reason."),
          h("dl", { class: "mrow top" }, h("dt", null, "Blocked text"), h("dd", null, Array.isArray(log) ? `${blocked.length}` : "–")),
          h("dl", { class: "mrow" }, h("dt", null, "Entries"), h("dd", null, Array.isArray(log) ? `${q.length}` : "–")),
          a ? h("dl", { class: "mrow" }, h("dt", null, "Author"), h("dd", { class: "jname" }, a.models.author)) : null,
          a ? h("dl", { class: "mrow" }, h("dt", null, "Gate"), h("dd", { class: "jname" }, a.models.gate)) : null,
          a ? h("dl", { class: "mrow" }, h("dt", null, "Grader"), h("dd", { class: "jname" }, a.models.grader)) : null),
        h("section", { class: "panel" }, eyebrow("shield", "Why you can trust the gaps"),
          h("p", { class: "share-line" }, icon("check", "sm"), h("span", null, "A blocked claim never reaches you, so a thin lesson is honest, not broken.")),
          h("p", { class: "share-line" }, icon("check", "sm"), h("span", null, "Quotes are matched word for word by Dojo itself, not by a model.")),
          h("p", { class: "note top" }, NOT_A_PREDICTION))))];
}

// What Dojo's own voices actually said (EG-19): a background check writes down the Listen audio and a
// sample of the presenter videos, and compares the words with the approved narration.
const pct = (x) => (x === null || x === undefined ? "–" : `${(x * 100).toFixed(1)}%`);
const FLAG_LABEL = { number: "number", name: "name", negation: "negation" };

// Long unchanged stretches are shortened to "…", so a long slide shows where it differs.
// A heard text with nothing marked (the words were only left out) is shown around where the approved text
// differs, so a long Deeper Listen part does not show its first words instead.
function voiceWords(words, anchor = -1) {
  const marked = words.some(([, mark]) => mark);
  if (!marked && anchor < 0) return words.length > 18 ? [words.slice(0, 18).map(([w]) => w).join(" "), " …"] : words.map(([w]) => w).join(" ");
  const at = Math.min(anchor, words.length - 1);
  const near = words.map(([, mark], i) => (marked ? words.slice(Math.max(0, i - 4), i + 5).some(([, m]) => m) : Math.abs(i - at) <= 4));
  const out = [];
  words.forEach(([word, mark], i) => {
    if (!near[i]) {
      if (i === 0 || near[i - 1]) out.push(h("span", { class: "vgap" }, "…"), " ");
      return;
    }
    out.push(mark ? h("span", { class: mark === 2 ? "vw meaning" : "vw diff" }, word) : word, " ");
  });
  return out;
}

function voiceCase(w) {
  const where = w.kind === "watch" ? `Watch · slide ${w.slide} · ${presenterLabel(w.speaker)}`
    : w.kind === "deep" ? `Deeper Listen · part ${w.part}${w.part_title ? ` · ${w.part_title}` : ""}` : `Listen · slide ${w.slide}`;
  return h("div", { class: "vcase" },
    h("div", { class: "qh" }, h("b", null, w.title || w.lesson),
      h("span", { class: "chips" }, w.flags.map((f) => chip(FLAG_LABEL[f] || f, "rose")), chip(`WER ${pct(w.wer)}`, "line"))),
    h("p", { class: "small muted" }, where, ` · ${w.errors} of ${plural(w.words, "word")} differ · checked ${fmtWhen(w.checked)}`),
    h("p", { class: "vline" }, h("span", { class: "vlab" }, "Approved"), h("span", null, voiceWords(w.approved))),
    h("p", { class: "vline" }, h("span", { class: "vlab" }, "Heard"),
      h("span", null, w.heard.length ? voiceWords(w.heard, w.approved.findIndex(([, m]) => m)) : h("i", { class: "muted" }, "nothing"))));
}

function voicesPanel(v) {
  const head = (extra) => h("div", { class: "panel-h" }, eyebrow("listen", "What the voices actually said"), extra || null);
  if (!v || v.error) return h("section", { class: "panel grow" }, head(), h("p", { class: "sub" }, (v && v.error) || "The voice check could not be read."));
  const L = v.listen, W = v.watch, D = v.deep || { lessons: 0, parts: 0, flagged: 0, median_wer: null };
  const flagged = L.flagged + W.flagged + D.flagged;
  const stat = (big, label, sub) => h("div", { class: "vstat" }, h("span", { class: "big" }, big), h("b", null, label), h("small", null, sub));
  const st = v.status || {};
  const status = !st.started ? "The check is not running in this copy of Dojo." : st.running ? "Checking now." : st.note ? `Now: ${st.note}.` : "";
  const large = W.too_large ? ` ${plural(W.too_large, "presenter video")} ${W.too_large === 1 ? "is" : "are"} too large to check.` : "";
  return h("section", { class: "panel grow stack" },
    head(chip(`${L.lessons} of ${plural(v.lessons, "lesson")} checked`, "line")),
    h("div", { class: "vstats" },
      stat(`${L.lessons}`, "Lessons heard", `Listen audio, ${plural(L.slides, "slide")}`),
      stat(pct(L.median_wer), "Median WER", L.slides ? `per slide; ${pct(L.wer)} of all words` : "nothing measured yet"),
      stat(`${flagged}`, "Meaning at risk", "slides, parts or turns where a number, name or negation differs"),
      stat(`${D.lessons}`, "Deeper Listen", D.parts ? `${plural(D.parts, "part")}, median WER ${pct(D.median_wer)}` : "none heard yet"),
      stat(`${W.lessons}`, "Watch sample", W.turns ? `${plural(W.turns, "turn")}, median WER ${pct(W.median_wer)}` : "no presenter video checked yet")),
    h("p", { class: "small muted top" },
      `At most ${v.daily.listen} Listen lessons, ${v.daily.deep} Deeper Listen lessons and ${v.daily.watch} Watch lesson a day, one at a time, never while your own work runs; `,
      `a failure goes to the log below and is tried again after ${v.retry_hours} hours. Today: ${v.today.listen} of ${v.daily.listen}, ${v.today.deep} of ${v.daily.deep} `,
      `and ${v.today.watch} of ${v.daily.watch}. ${status}${large}`),
    v.worst.length
      ? [h("h3", { class: "top" }, "Where the words differ most"), h("div", { class: "vlist" }, v.worst.map(voiceCase))]
      : h("p", { class: "sub top" }, L.lessons || W.lessons || D.lessons ? "Every checked slide and part was heard exactly as approved." : "Nothing has been checked yet."),
    h("p", { class: "note top" }, v.note));
}

// The way in to screen 12 from Studio: which exams Dojo builds courses for, and a way to add one.
function examsPanel() {
  return panel(h("div", { class: "panel-h" }, eyebrow("course", "Your exams"), linkButton("Add an exam", "#/add-exam", "sm")),
    h("div", { class: "chips" }, state.me.packages.map((p) => chip(p.added ? `${p.exam} · added` : p.exam, "line"))),
    h("p", { class: "small muted top" }, "Type any Microsoft or GitHub exam code. Dojo reads the official study guide, shows what the course costs, and builds it when you confirm."));
}

// The estimate. Every price comes from the public list; a meter that is not listed is said so, not guessed.
const MODEL_ROLES = { author: "Author", gate: "Gate", grader: "Grader" };
const DEPLOYMENT_NAMES = { GlobalStandard: "Global Standard", DataZoneStandard: "Data Zone Standard" };
const perMillion = (x) => `$${Number(x).toFixed(2)}`;
function costsPanel(c) {
  const head = h("div", { class: "panel-h" }, eyebrow("scale", "What this costs"), chip("Estimate", "amber"));
  if (!c || c.error) return panel(head, h("p", { class: "sub" }, (c && c.error) || "The estimate could not be made."));
  const money = (x) => (x > 0 && x < 0.01 ? "< $0.01" : `$${x.toFixed(2)}`);
  const u = c.usage;
  const amount = (l) => (l.unit === "minutes" ? `${l.quantity.toFixed(1)} minutes` : `${Number(l.quantity).toLocaleString()} characters`);
  // "1 Minute" reads as "minute"; "1M" of a character meter reads as "1M characters".
  const per = (l) => (/^\d+(\.\d+)?\s*[KM]?$/i.test(l.price_unit) ? `${l.price_unit} ${l.unit}` : String(l.price_unit).replace(/^1\s+/, "").toLowerCase());
  const extra = { video: ` in ${plural(u.videos, "stored video")}`, listen_voice: ` in ${plural(u.audio_files, "audio file")}`, watch_voice: "", transcription: "" };
  return panel(head,
    h("p", { class: "sub" }, c.label),
    c.budget_flags ? h("p", { class: "note bad" }, icon("alert", "sm"), " Reservation bound exceeded: ",
      c.budget_flags.kinds.join(", "), ". A call cost more than Dojo reserved for it; its table entry needs raising.") : null,
    c.lines.map((l) => h("div", { class: "costrow" },
      h("div", null, h("b", null, l.what),
        h("small", null, amount(l), extra[l.key] || "", " · ", l.found ? `$${l.price.toFixed(2)} per ${per(l)}` : "price not found"),
        h("small", { class: "mono" }, l.meter)),
      h("span", { class: l.found ? "cost" : "cost none" }, l.found ? money(l.cost) : "price not found"))),
    h("div", { class: "costrow total" }, h("b", null, c.complete ? "Total so far" : "Total of the lines with a price"), h("span", { class: "cost" }, money(c.total))),
    pricesAge(c),
    (c.models || []).length ? [h("h3", { class: "top" }, "Model list prices, not in this total"),
      c.models.map((m) => h("div", { class: "costrow" },
        h("div", null, h("b", null, `${MODEL_ROLES[m.role] || m.role}: ${m.model}`),
          m.found ? h("small", null, `${perMillion(m.input.per_million)} per million input tokens, ${perMillion(m.output.per_million)} per million output tokens, ${DEPLOYMENT_NAMES[m.deployment] || m.deployment}`)
            : h("small", { class: "bad" }, m.note),
          m.found ? h("small", { class: "mono" }, `${m.input.meter} · ${m.output.meter}`) : null),
        h("span", { class: m.found ? "cost" : "cost none" }, m.found ? "" : "no list price")))] : null,
    h("p", { class: "small muted top" }, `List prices: Azure Retail Prices API, ${c.region}, ${c.currency}, pay as you go.`),
    c.notes.map((n) => h("p", { class: "small muted" }, n)),
    deepCosts(c.deep),
    examItemCosts(c.exam_items));
}

// Practice-exam questions by kind (ADR 0012): what 100 cost at list price, in the exam mix and each kind alone.
function examItemCosts(x) {
  if (!x) return null;
  const cost = (v) => (v === null || v === undefined ? "no list price" : usd(v));
  const kinds = KIND_ORDER.filter((k) => x.kinds[k]);
  return h("div", { class: "deep-costs" },
    h("h3", { class: "top" }, "Practice-exam questions"),
    h("p", { class: "deep-line" }, `100 questions in the exam mix: about ${cost(x.exam_per_100)}; all multiple choice: about ${cost(x.single_per_100)}`
      + (x.extra_per_100 !== null && x.extra_per_100 !== undefined ? ` (${x.extra_per_100 >= 0 ? "+" : ""}${usd(x.extra_per_100)}).` : ".")),
    h("p", { class: "small muted" }, `The mix in 100 questions: ${mixText(Object.fromEntries(Object.entries(x.mix_100).map(([k, n]) => [k, n * (k === "yesno" ? 3 : k === "case" ? 4 : 1)])))}.`),
    h("p", { class: "small muted" }, `Kept ready per exam by the background pool: ${mixText(Object.fromEntries(Object.entries(x.pool_mix).map(([k, n]) => [k, n * (k === "yesno" ? 3 : k === "case" ? 4 : 1)])))}.`),
    x.left_out && x.left_out.length ? h("p", { class: "small bad" }, `Not the full cost. ${x.left_out.join(" ")}`) : null,
    h("details", { class: "how" }, h("summary", null, "Each kind alone, per 100 questions"),
      kinds.map((k) => h("div", { class: "costrow" }, h("div", null, h("b", null, x.kinds[k].label)), h("span", { class: "cost" }, cost(x.kinds[k].per_100)))),
      h("ul", { class: "facts" }, x.assumptions.map((a) => h("li", null, a)))));
}

// Deeper Listen (ADR 0007): how many lessons play it, what it has cost so far and what every lesson costs.
function deepCosts(d) {
  if (!d) return null;
  const so = d.so_far, all = d.every_lesson;
  const checks = (d.failed || 0) - (d.stopped || 0);
  const state = [d.per_day ? `Today: ${d.today} of ${d.per_day} written.` : "",
    d.speaking ? `${plural(d.speaking, "lesson")} written, audio being made.` : "",
    d.waiting_upgrade ? `${plural(d.waiting_upgrade, "lesson")} ${d.waiting_upgrade === 1 ? "waits" : "wait"} until ${d.waiting_upgrade === 1 ? "it is" : "they are"} written again on the deeper template, so ${d.waiting_upgrade === 1 ? "its" : "each"} Deeper Listen is written once.` : "",
    checks > 0 ? `${plural(checks, "lesson")} did not pass the checks and ${checks === 1 ? "keeps" : "keep"} the slide narration.` : "",
    d.stopped ? `${plural(d.stopped, "lesson")} stopped before ${d.stopped === 1 ? "it was" : "they were"} finished and ${d.stopped === 1 ? "keeps" : "keep"} the slide narration.` : "",
    d.failed ? "Dojo writes each lesson at most twice, a week apart." : ""].filter(Boolean).join(" ");
  const every = `All ${plural(all.lessons, "lesson")}`;
  return h("div", { class: "deep-costs" },
    h("h3", { class: "top" }, "Deeper Listen"),
    h("p", { class: "deep-line" }, `Deeper Listen: ${d.ready} of ${d.lessons} ready; about ${usd(so.total)} at list price so far`
      + `${so.complete ? "" : ", for the lines with a price"}; ${d.per_day} a day.`),
    so.complete ? null : h("p", { class: "small bad" }, `So far: not the full cost. ${leftOut(so)}`),
    state ? h("p", { class: "small muted" }, state) : null,
    h("div", { class: "costrow total" },
      h("div", null, h("b", null, all.complete ? every : `${every}, for the lines with a price`),
        all.per_lesson !== null ? h("small", null, `About ${usd(all.per_lesson)} a lesson, at list price.`) : null,
        all.complete ? null : h("small", { class: "bad" }, `Not the full cost. ${leftOut(all)}`)),
      h("span", { class: "cost" }, usd(all.total))),
    h("details", { class: "how" }, h("summary", null, "How Dojo estimates this"),
      all.lines.map((l) => h("div", { class: "costrow" },
        h("div", null, h("b", null, l.what), h("small", null, lineAmount(l)), l.found ? null : h("small", { class: "bad" }, l.note)),
        h("span", { class: l.found ? "cost" : "cost none" }, l.found ? usd(l.cost) : "not included"))),
      h("ul", { class: "facts" }, d.assumptions.map((a) => h("li", null, a)))));
}

// ---------------------------------------------------------------- Studio: source changes (ADR 0006)
// What a changed page touched, and the one button that spends money on it: Dojo never films on its own.
const LESSON_NOW = { holds: ["still holds", "sage"], rechecking: ["re-checking", "amber"], waiting: ["waiting", "amber"], rewritten: ["written again", "sage"] };
const LAB_NOW = { flagged: ["to look at", "amber"], looked: ["looked at", "sage"], clear: ["nothing to look at", "line"] };
const FILM_NOW = { waiting: ["video waits for you", "amber"], remaking: ["video being remade", "line"], done: ["filmed again", "sage"] };
const FILM_STATE = { approved: ["waiting its turn", "amber"], filed: ["filming", "amber"], done: ["filmed", "sage"], failed: ["did not work", "rose"] };
const dollars = (x) => (x > 0 && x < 0.01 ? "< $0.01" : `$${x.toFixed(2)}`);

function driftRow(dr) {
  const host = h("div", { class: "row stackable" });
  const paint = (d) => put(host, driftChanges(d, paint), h("div", { class: "col w430" }, refilmPanel(d, paint)));
  if (dr && !dr.error) paint(dr);
  else put(host, h("section", { class: "panel grow" }, eyebrow("refresh", "Source changes"),
    h("p", { class: "sub top" }, (dr && dr.error) || "The source check could not be read.")));
  return host;
}

function driftChanges(d, paint) {
  const P = d.pages, L = d.limits, st = d.status || {}, R = d.rewrites;
  const labs = d.labs || [];
  const now = !d.started ? "The check is not running in this copy of Dojo." : st.running ? "Reading a page now." : st.note ? `Now: ${st.note}.` : "";
  const waiting = P.waiting.length ? ` Leaving ${P.waiting.join(" and ")} alone for now, as asked.` : "";
  return h("section", { class: "panel grow stack" },
    h("div", { class: "panel-h" }, eyebrow("refresh", "Source changes"),
      chip(P.last_read ? `Sources checked daily · last ${fmtDate(P.last_read)}` : "Sources checked daily · none read again yet", "line")),
    h("p", { class: "sub" }, "Dojo reads every page a lesson, question or lab was written from again, about once a day, and looks for each quote word for word. A change in formatting only starts nothing."),
    h("p", { class: "small muted top" }, `Today: ${P.read_today} of ${L.per_day} reads, at least ${L.gap_minutes} minutes apart, each page at most once in ${L.every_hours} hours. `,
      `${plural(P.cited, "page")} cited, ${P.gone ? `${P.gone} gone${P.moved ? ` (${P.moved} moved to another page)` : ""}` : "none gone"}. `,
      P.unchecked ? `${plural(P.unchecked, "page")} not checked yet: ${P.unchecked === 1 ? "its copy was" : "their copies were"} stored before Dojo checked where a read ends, or read from another page. ${P.unchecked === 1 ? "It is" : "They are"} not used until read again, in turn. ` : "",
      `${now}${waiting}`),
    R ? h("p", { class: "small muted" }, `${R.used_today} of ${R.per_day} rewrites used today; ${R.waiting} waiting.`,
      R.waiting && !R.left_today ? " The rest wait for tomorrow (UTC), in the same order: your active exam first, then most exam weight." : "",
      R.waiting_for_pages ? ` ${plural(R.waiting_for_pages, "lesson")} ${R.waiting_for_pages === 1 ? "waits" : "wait"} without using one: all ${R.waiting_for_pages === 1 ? "its" : "their"} official pages are gone or moved; update the package's source.` : "") : null,
    d.rechecking ? h("p", { class: "conf top" }, icon("refresh", "xs"), h("span", null,
      `${plural(d.rechecking, "lesson")} ${d.rechecking === 1 ? "is" : "are"} being re-checked: held back from your podcast feed and written again from the page as it is now.`)) : null,
    labs.length ? h("p", { class: "conf top" }, icon("target", "xs"), h("span", null,
      `${plural(labs.length, "lab")} to look at: a page ${labs.length === 1 ? "it cites" : "they cite"} changed. A lab has no quotes to check, so compare its steps with the page, then press “I looked at it”.`)) : null,
    d.changes.length ? h("div", { class: "top" }, d.changes.map((c) => driftChange(c, paint))) : h("p", { class: "sub top" }, "No cited page has changed its words so far."),
    d.formatting.length ? [h("h3", { class: "top" }, "Formatting only, nothing to re-check"),
      d.formatting.map((f) => h("p", { class: "prow" }, icon("check", "xs"), h("span", null, ext(f.url, f.title || f.url), ` · ${f.date}`)))] : null,
    h("p", { class: "note top" }, d.record));
}

function driftChange(c, paint) {
  const moved = c.kind === "moved", gone = c.kind === "gone" || moved;
  const it = c.items || {}, pool = c.pool || {}, labs = c.labs || [];
  const hold = (n) => (n === 1 ? "holds" : "hold");
  const touched = [
    it.withheld ? `${plural(it.withheld, "question")} held back` : "",
    it.answered ? `${plural(it.answered, "answered question")} kept in your record as ${it.answered === 1 ? "it was" : "they were"}` : "",
    it.holds ? `${plural(it.holds, "question")} still ${hold(it.holds)}` : "",
    pool.withheld ? `${plural(pool.withheld, "practice-exam question")} held back` : "",
    pool.holds ? `${plural(pool.holds, "practice-exam question")} still ${hold(pool.holds)}` : "",
  ].filter(Boolean);
  return h("div", { class: "dchange" },
    h("div", { class: "qh" }, eyebrow(gone ? "alert" : "refresh", moved ? "A source page moved" : gone ? "A source page is gone" : "A source changed", "amber"),
      h("span", { class: "small muted" }, c.date)),
    h("p", { class: "dtitle" }, c.publisher ? `${c.publisher} · ` : "", ext(c.url, c.title || c.url),
      moved ? ` moved to ${movedTo(c)}.` : gone ? " can no longer be found." : " was updated."),
    c.removed.length ? [h("p", { class: "small ink2 top" }, c.removed_total > c.removed.length
      ? `No longer on the page (${c.removed.length} of ${c.removed_total} shown):` : "No longer on the page:"),
      c.removed.map((q) => h("p", { class: "blocked-line" }, `“${q}”`))] : null,
    c.lessons.length ? h("div", { class: "top" }, c.lessons.map(driftLesson)) : null,
    labs.length ? h("div", { class: "top" }, labs.map((x) => driftLab(x, paint))) : null,
    touched.length ? h("p", { class: "small ink2 top" }, touched.join(" · ")) : null,
    !c.lessons.length && !labs.length && !touched.length ? h("p", { class: "small muted top" }, "Nothing Dojo wrote used the words that changed.") : null);
}

function driftLesson(x) {
  const [label, kind] = LESSON_NOW[x.now] || [x.now, "line"];
  const film = x.film && FILM_NOW[x.film.state];
  return h("div", { class: "near" },
    h("span", null, h("a", { class: "link", href: `#/lesson/${x.now === "rewritten" && x.by ? x.by : x.id}` }, x.title || x.skill),
      " ", h("span", { class: "mono muted" }, x.skill), x.why ? h("small", { class: "block muted" }, x.why) : null),
    h("span", { class: "chips" }, chip(label, kind), film ? chip(film[0], film[1]) : null));
}

// A lab a changed page touched. Only a person can say whether its steps still match, so its flag stays
// until the owner says they looked.
function driftLab(x, paint) {
  const [label, kind] = LAB_NOW[x.now] || [x.now, "line"];
  const msg = h("small", { class: "block muted" });
  const looked = x.now === "flagged" ? button("I looked at it", async () => {
    looked.disabled = true;
    try { paint(await api(`/api/studio/drift/labs/${x.id}/looked`, { method: "POST" })); }
    catch (e) { msg.textContent = e.message; looked.disabled = false; }
  }, "ghost sm") : null;
  return h("div", { class: "near" },
    h("span", null, "Lab: ", labsOn() ? h("a", { class: "link", href: `#/lab/${x.id}` }, x.title || x.id) : (x.title || x.id), " ", h("span", { class: "mono muted" }, x.skill), msg),
    h("span", { class: "chips" }, chip(label, kind), looked));
}

function refilmPanel(d, paint) {
  const R = d.refilm, est = R.estimate, n = R.lessons.length;
  const deeper = R.lessons.filter((x) => x.why === "deeper").length, changed = n - deeper;
  const cost = est.usd === null ? "price not listed" : `about ${dollars(est.usd)} at list price`;
  const status = h("p", { class: "small muted" });
  const btn = n ? button(`Re-film ${plural(n, deeper ? "lesson" : "changed lesson")} (${cost})`, async () => {
    if (!window.confirm(`Film ${plural(n, "lesson")} again? This costs ${cost}. The films are made one at a time.`)) return;
    btn.disabled = true;
    status.textContent = "Filing the films…";
    try { paint(await api("/api/studio/drift/refilm", { method: "POST", body: { lessons: R.lessons.map((x) => x.id) } })); }
    catch (e) { status.textContent = e.message; btn.disabled = false; }
  }, "amber") : null;
  const pace = est.pace === "measured" ? "at the pace of your own videos" : "at an assumed pace of 14 characters a second";
  const why = [changed ? `${plural(changed, "lesson")} after a source page changed` : null,
    deeper ? `${plural(deeper, "lesson")} on the deeper template, from more official pages` : null].filter(Boolean).join(" and ");
  const until = [changed ? "a changed lesson offers Present, Read and Listen" : null,
    deeper ? "a deeper lesson shows the earlier film and says so" : null].filter(Boolean).join("; ");
  return panel(
    h("div", { class: "panel-h" }, eyebrow("video", "Film again"), chip("Only when you say so", "amber")),
    n ? [h("p", { class: "sub" }, `Written again: ${why}. `
          + `The earlier ${n === 1 ? "version was" : "versions were"} filmed; the new ${n === 1 ? "one is" : "ones are"} not. Until then, Watch: ${until}.`),
        R.lessons.map((x) => h("div", { class: "near" },
          h("span", null, h("a", { class: "link", href: `#/lesson/${x.id}/watch` }, x.title || x.skill), " ", h("span", { class: "mono muted" }, x.skill),
            x.why === "deeper" ? h("small", { class: "block muted" }, "deeper lesson") : null),
          h("span", { class: "small muted" }, x.error ? "the last try did not work" : `${Number(x.characters).toLocaleString()} characters`))),
        h("div", { class: "actions top refilm" }, btn), pricesAge(est), status,
        h("p", { class: "small muted top" }, `About ${est.minutes} minutes of video ${pace}. `,
          est.voice_priced ? "The presenters' voices are included. " : "The presenters' voices have no listed price, so they are not included. ", est.label)]
      : h("p", { class: "sub" }, "No changed lesson is waiting for a new video. Filming costs money, so Dojo never films on its own: when a filmed lesson is written again, it waits here for your go-ahead."),
    R.films.length ? [h("h3", { class: "top" }, "Films you approved"),
      R.films.map((f) => { const [label, kind] = FILM_STATE[f.state] || [f.state, "line"];
        return h("div", { class: "near" }, h("span", null, f.title || f.skill, " ", h("span", { class: "mono muted" }, f.skill),
          f.state === "failed" && f.error ? h("small", { class: "block muted" }, f.error) : null),
          chip(label, kind)); })] : null,
    R.approvals.length ? h("p", { class: "small muted top" }, "Approved: ", R.approvals.map((a) =>
      `${fmtDate(a.at)}, ${plural(a.lessons.length, "lesson")}, ${a.usd === null || a.usd === undefined ? "price not listed" : "about " + dollars(a.usd)}`).join(" · ")) : null);
}

// ---------------------------------------------------------------- Studio: deeper lessons (ADR 0008)
// More official pages for each skill, and lessons written again from all of them. The estimate uses the
// same list prices and token assumptions as adding an exam. Nothing here is evidence.
function deeperRow(d) {
  if (!d || d.error) return h("div", { class: "row stackable" }, h("section", { class: "panel grow" }, eyebrow("course", "Deeper lessons"),
    h("p", { class: "sub top" }, (d && d.error) || "Deeper lessons could not be read.")));
  return h("div", { class: "row stackable" }, deeperPanel(d), h("div", { class: "col w430" }, deeperCost(d)));
}

function deeperPanel(d) {
  const L = d.limits, T = d.today, S = d.search || {};
  const now = !d.started ? "The page search is not running in this copy of Dojo." : S.current ? `Searching now in ${S.current}.` : S.note ? `Now: ${S.note}` : "";
  return h("section", { class: "panel grow stack" },
    h("div", { class: "panel-h" }, eyebrow("course", "Deeper lessons"), chip(`${T.used} of ${L.per_day} written again today`, "line")),
    h("p", { class: "sub" }, `A skill of a built-in exam can be taught from up to ${L.pages_per_skill} official pages. Dojo searches the documentation for more, `
      + "keeps a page only when the checker shows, in the page's own words, that it teaches the skill, and writes the lesson again from every page. "
      + "The earlier lesson stays until the new one passes every check."),
    h("p", { class: "small muted top" },
      `One group of skills is searched at a time, at least ${L.search_gap_minutes} minutes apart and never while your own work runs; `,
      `a skill where nothing new was found is searched again after ${L.search_again_days} days, and one whose page found earlier went away is searched once straight away, for a page to take its place. `,
      `At most ${L.per_day} lessons are written again a day (UTC), after source changes and missing lessons, your active exam first; `,
      `a try that fails keeps the earlier lesson and waits ${L.wait_hours} hours, and after ${L.tries} tries Dojo gives up on it. ${now}`),
    h("div", { class: "top" }, d.exams.map((x) => deeperExam(x, L))),
    d.failures.length ? [h("h3", { class: "top" }, "Last failures"),
      h("div", { class: "dfail" }, failureRuns(d.failures).slice(0, 5).map((f) => h("div", { class: "near" },
        h("span", null, h("b", null, f.what === "search" ? "Page search" : "Writing again"), " ",
          d.exams.some((x) => x.package === f.package)
            ? h("a", { class: "mono link", href: `#/skill/${f.package}/${f.skill}` }, `${f.package} ${f.skill}`)
            : h("span", { class: "mono muted" }, `${f.package} ${f.skill}`),
          f.times > 1 ? h("span", { class: "small muted" }, ` · ${f.times} times`) : null,
          h("small", { class: "block muted" }, f.reason)),
        h("span", { class: "small muted" }, fmtWhen(f.at)))))] : null,
    h("p", { class: "note top" }, d.record));
}

// The same failure, one after another (a lesson that failed three times), is shown once, with how often.
function failureRuns(list) {
  const out = [];
  for (const f of list) {
    const last = out[out.length - 1];
    if (last && ["package", "skill", "what", "reason"].every((k) => last[k] === f[k])) last.times += 1;
    else out.push({ ...f, times: 1 });
  }
  return out;
}

function deeperExam(x, L) {
  const share = x.lessons ? Math.round((100 * x.deeper) / x.lessons) : 0;
  const money = (e) => (!e ? "–" : e.complete ? `about ${usd(e.total)}` : `at least ${usd(e.total)}`);
  const left = [
    x.waiting ? `${x.waiting} to write again` : "",
    x.searching_first ? `${x.searching_first} ${x.searching_first === 1 ? "waits" : "wait"} for ${x.searching_first === 1 ? "its" : "their"} page search first` : "",
    x.given_up ? `${x.given_up} given up after ${L.tries} tries` : "",
    x.builtin && x.to_search ? `${plural(x.to_search, "skill")} to search` + (x.replacing ? ` (${x.replacing} for a page that went away)` : "") : "",
  ].filter(Boolean);
  return h("div", { class: "dexam" },
    h("div", { class: "qh" }, h("b", null, `${x.exam} · ${x.title}`), chip(x.builtin ? "built in" : "added", "line")),
    h("p", { class: "small ink2" }, x.builtin
      ? `${x.deep} of ${plural(x.skills, "skill")} have two or more official pages.`
      : `${x.deep} of ${plural(x.skills, "skill")} have two or more official pages, chosen when you added the exam.`),
    h("p", { class: "small ink2" }, `${x.deeper} of ${plural(x.lessons, "lesson")} on the deeper template.`),
    h("div", { class: "bar", role: "img", "aria-label": `${share}% of the lessons on the deeper template` }, wide(h("i", { class: "amber" }), `${share}%`)),
    left.length ? h("p", { class: "small muted" }, left.join(" · ")) : null,
    h("p", { class: "small muted" }, `Spent so far: ${money(x.spent_estimate)}. To finish: ${money(x.to_finish)}. At list price.`));
}

function deeperCost(d) {
  const est = d.estimate, spent = d.spent;
  const head = h("div", { class: "panel-h" }, eyebrow("scale", "What deeper lessons cost"), chip("Estimate", "amber"));
  if (!est) return panel(head, h("p", { class: "sub" }, "The estimate could not be made."));
  return panel(head,
    h("p", { class: "sub" }, est.lessons || est.searches
      ? `To finish: ${plural(est.searches, "skill")} to search and ${plural(est.lessons, "lesson")} to write again`
        + (est.deep_again ? `; ${est.deep_again} of them ${est.deep_again === 1 ? "plays" : "play"} a Deeper Listen that is written again for the new lesson.` : ".")
      : "Nothing is left to do: every skill has been searched and every lesson is on the deeper template."),
    h("div", { class: "top" }, estimateRows(est, "About, at list price, to finish")),
    spent ? h("p", { class: "small muted top" }, `Spent so far, by the same assumptions: ${spent.complete ? "about" : "at least"} ${usd(spent.total)} `
      + `(${plural(spent.searches, "skill")} searched, ${spent.lessons} ${spent.lessons === 1 ? "try" : "tries"} at writing a lesson again).`) : null);
}

// ---------------------------------------------------------------- how Dojo works

async function viewAbout() {
  const [a, d] = await Promise.all([api("/api/about"), isAdmin() ? api("/api/diag").catch(() => null) : null]);
  const checks = d && d.selftest && d.selftest.checks ? Object.entries(d.selftest.checks) : [];
  const labStatus = h("p", { class: "small", role: "status", "aria-live": "polite" }, "Not checked. The system check never wakes the lab database.");
  const labProbe = button("Check lab connection now", async () => {
    labProbe.disabled = true;
    labStatus.textContent = "Waking the database, about a minute…";
    try {
      const result = await api("/api/diag/lab", { method: "POST" });
      labStatus.textContent = result.ok
        ? (result.database_checked ? `Connected to ${result.database} through private SQL. Your lab runner ${result.runner_allowed ? "reaches only your own lab schema" : "needs setup"}.`
          : "Local demo: no database connection.")
        : (result.message || "The lab database could not be reached.");
    } catch (e) { labStatus.textContent = e.message; }
    finally { labProbe.disabled = false; }
  }, "ghost sm");
  // The old shared-runner clean-up (ADR 0009, "Labs"): it reaches the whole database, so only the owner runs it.
  const labRepair = button("Clean dbo and escaped history", async () => {
    if (!window.confirm("Drop every table in dbo and history tables that escaped their lab schema in older runs? Members' and your own lab schemas are kept. This cannot be undone.")) return;
    labRepair.disabled = true;
    labStatus.textContent = "Cleaning…";
    try { labStatus.textContent = (await api("/api/diag/lab/repair", { method: "POST", body: { confirm: true } })).message; }
    catch (e) { labStatus.textContent = e.message; }
    finally { labRepair.disabled = false; }
  }, "ghost sm");
  return [pageHead("How Dojo works", `A personal trainer for Microsoft and GitHub certification exams. Build ${String(a.build).slice(0, 12)}.`),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow article" },
        h("section", { class: "panel" }, eyebrow("today", "What Dojo is"),
          h("p", { class: "lead" }, "It teaches each skill from the official study guide in three ways — read, listen, present — tests you on new items, and keeps an honest record of what you have and have not yet shown."),
          h("p", { class: "note top" }, "Dojo does not predict exam results, does not use real exam questions and does not score you against a pass mark.")),
        h("section", { class: "panel" }, eyebrow("scale", "Three AI models, each with one job"),
          h("div", { class: "jrow" }, h("span", { class: "jrole" }, "Author"), h("span", { class: "jname" }, a.models.author)),
          h("p", { class: "judge" }, "Writes lessons, items, hints, practice questions and answers."),
          h("div", { class: "jrow" }, h("span", { class: "jrole" }, "Gate"), h("span", { class: "jname" }, a.models.gate)),
          h("p", { class: "judge" }, "A different model family. Checks every model-written text you see against the official sources, element by element. What does not hold is removed and logged in the studio."),
          h("div", { class: "jrow" }, h("span", { class: "jrole" }, "Grader"), h("span", { class: "jname" }, a.models.grader)),
          h("p", { class: "judge" }, "A third family. Judges your answers and must quote your own words for every point it gives you."),
          h("p", { class: "small muted top" }, "All three run in Azure AI Foundry. Dojo itself also matches every quote from a source word for word.")),
        h("section", { class: "panel" }, eyebrow("record", "The evidence language"),
          h("p", { class: "sub" }, "A skill is not a percentage. Dojo records the conditions under which you showed it."),
          h("div", { class: "piplegend" }, h("span", { class: "lead" }, "Four conditions:"),
            h("span", null, h("i", { class: "pip help" }), "with help"),
            h("span", null, h("i", { class: "pip own" }), "on your own"),
            h("span", null, h("i", { class: "pip own" }), "later"),
            h("span", null, h("i", { class: "pip" }), "in a new situation")),
          h("p", { class: "note top" }, "This version records the first three. Nothing is claimed about the fourth, so that ring is always empty.")),
        h("section", { class: "panel" }, eyebrow("file", "Sources and licences"),
          h("p", { class: "sub" }, "Skills and weights come from the official study guides. Lessons are grounded in GitHub Docs and Microsoft Learn pages, licensed CC BY 4.0 by their publishers; Dojo quotes them and links back. Dojo is an independent tool, not endorsed by Microsoft or GitHub."),
          a.packages.map((p) => h("div", { class: "src" }, icon("file", "sm"), h("div", null,
            h("b", null, `${p.exam} ${p.title}`),
            h("div", { class: "snap" }, p.source.skills_as_of ? `skills measured as of ${p.source.skills_as_of}, ` : "the study guide gives no date, ", `read ${p.source.retrieved} · `,
              h("a", { class: "link", href: /^https:\/\/learn\.microsoft\.com\//.test(p.source.url) ? p.source.url : "#", target: "_blank", rel: "noopener noreferrer" }, "study guide")),
            p.notes && p.notes.length ? p.notes.map((n) => h("div", { class: "snap" }, n)) : null))))),
      h("div", { class: "col w430" },
        h("section", { class: "panel" }, eyebrow("help", "Help"),
          h("p", { class: "sub" }, "The user guide: how each page works, what Dojo does not do, and what to try when something goes wrong."),
          h("div", { class: "actions top" }, linkButton("Open Help", "#/help", "ghost sm"), linkButton("Limitations", "#/help/limitations", "ghost sm"))),
        h("section", { class: "panel" }, eyebrow("lock", "Your data"),
          h("p", { class: "sub" }, a.storage),
          h("p", { class: "sub" }, state.me && state.me.team
            ? "Only the owner and the members the owner approved can sign in. The administrators of that Microsoft Entra tenant and Azure subscription can technically reach the stored data."
            : "Only your account can sign in. The administrators of that Microsoft Entra tenant and Azure subscription can technically reach the stored data."),
          state.me && state.me.team && !isAdmin() ? h("div", { class: "actions top" }, linkButton("Read the notice again", "#/notice", "ghost sm")) : null,
          h("div", { class: "actions top" }, linkButton("Export or delete", "#/record", "ghost sm"))),
        !isAdmin() ? null : h("section", { class: "panel" }, eyebrow("check", "System check"),
          checks.length ? h("div", { class: "chips" }, checks.map(([k, v]) => chip(`${k}: ${v ? "ok" : "failing"}`, v ? "sage" : "rose"))) : h("p", { class: "sub" }, "Not run yet."),
          d && d.selftest && d.selftest.at ? h("p", { class: "small muted top" }, `Checked ${fmtDate(d.selftest.at)}`) : null,
          d && d.preparation ? h("p", { class: "small muted" }, "Lessons prepared ahead of time: ",
            Object.entries(d.preparation.packages).map(([pid, c]) => `${pid.toUpperCase()} ${c.ready} of ${c.skills}`).join(", "),
            d.preparation.current ? ` (writing ${d.preparation.current} now)` : "") : null),
        !isAdmin() ? null : h("section", { class: "panel" }, eyebrow("target", "Lab database"),
          h("p", { class: "small muted" }, d && d.labs ? d.labs.mode : "Not configured"),
          d && d.team_sync ? h("p", { class: d.team_sync.state === "unsynced" ? "note bad" : "small muted" }, d.team_sync.text) : null,
          labStatus, h("div", { class: "actions" }, labProbe, d && d.labs && d.labs.on !== false && !d.labs.mode.startsWith("local") ? labRepair : null))))];
}

// ---------------------------------------------------------------- Team Dojo (ADR 0009)
/* With team mode on, the owner approves colleagues as members. A member sees their own learning only;
   the owner's Members page shows only what admission needs: no activity, results or costs per member.
   Pages a member must pass first (Ask for access, the notice, an ended membership) are "gates": the
   side bar, the Ask box and the exam picker are hidden while one is shown. */

function setGate(kind) {
  state.gate = kind || null;
  document.body.classList.toggle("gate", Boolean(kind));
}

async function showGate(kind) {
  setGate(kind);
  if (!state.gateHelp) { state.gateHelp = true; window.addEventListener("hashchange", gateHelp); }
  if (HELP_ROUTE.test(location.hash || "")) { await gateHelp(); return; }
  const view = $view();
  try {
    const nodes = kind === "access" ? await viewAccess() : kind === "notice" ? await viewNotice()
      : kind === "enroll" ? viewEnroll(true) : kind === "removed" ? viewEnded() : viewPausedGate();
    put(view, nodes);
  } catch (e) { put(view, panel(h("h2", null, "That did not work"), h("p", { class: "sub" }, e.message))); }
}

// The learner's own Studio: the log of what the checker removed from what they were shown.
async function viewStudioLog() {
  const log = await api("/api/quality").catch((e) => ({ error: e.message }));
  const q = Array.isArray(log) ? log : [];
  return [pageHead("Studio log", "Everything the checker removed from what you were shown, and why. Nothing is dropped silently.", null, helpQ("how-judging-works")),
    h("section", { class: "panel grow stack scroll" },
      h("div", { class: "panel-h" }, eyebrow("alert", "What was blocked, and why"), Array.isArray(log) ? chip(`${q.length} entries`, "line") : null),
      !Array.isArray(log) ? h("p", { class: "sub" }, log.error || "The log could not be read.")
      : q.length ? q.map((x) => h("div", { class: "qentry" },
        h("div", { class: "qh" }, h("b", null, [x.kind, x.part].filter(Boolean).join(" · ")), h("span", { class: "small muted" }, fmtDate(x.at))),
        x.text ? h("p", { class: "blocked-line" }, x.text) : null,
        x.reason ? h("p", { class: "small ink2" }, h("b", null, "Why: "), x.reason) : null)) : h("p", { class: "sub" }, "Nothing so far."))];
}

// A line above every page: team mode is ending, the Dojo is paused, or a membership needs renewing.
function teamBanners() {
  const me = state.me;
  if (!me) return null;
  const out = [];
  if (me.banner) out.push(h("div", { class: "team-banner", role: "status" }, icon("alert", "sm"), h("span", null, me.banner.text)));
  if (me.renew_by) {
    const note = h("span", { class: "small muted" });
    const box = h("div", { class: "team-banner", role: "status" }, icon("clock", "sm"),
      h("span", null, `Your membership ends on ${fmtDay(me.renew_by)} unless you keep it. Nothing else is asked.`),
      button("Keep my membership", async () => {
        try { await api("/api/me/renew", { method: "POST" }); me.renew_by = null; box.remove(); }
        catch (e) { note.textContent = e.message; }
      }, "ghost sm"), note);
    out.push(box);
  }
  return out.length ? h("div", { class: "team-banners" }, out) : null;
}

async function viewAccess() {
  const a = await api("/api/access");
  if (a.member) { location.reload(); return []; }
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const before = h("input", { type: "checkbox", id: "usedbefore" });
  const ask = button("Ask for access", async () => {
    ask.disabled = true;
    try { await api("/api/access/request", { method: "POST", body: { used_before: before.checked } }); showGate("access"); }
    catch (e) { status.textContent = e.message; ask.disabled = false; }
  });
  const r = a.request;
  const body = !r
    ? [h("p", { class: "sub" }, `You are signed in as ${a.shown_as}. This Dojo is run by its owner for a small group of colleagues, and the owner decides who joins.`),
      h("p", { class: "sub" }, "If you ask, the owner sees the name and email address your sign-in gives Dojo, and the day you asked. Nothing else is stored about you until you are approved."),
      h("label", { class: "conf top", for: "usedbefore" }, before,
        h("span", null, "I used this Dojo before with an account that no longer works. The owner checks with you directly before your old record is joined to this account.")),
      h("div", { class: "actions top" }, ask), status]
    : r.state === "declined"
      ? [h("p", { class: "sub" }, `The owner declined your request of ${fmtDay(r.at)}. If you think this is a mistake, ask the owner directly.`)]
      : [h("p", { class: "status-slate" }, icon("clock", "sm"), `You asked on ${fmtDay(r.at)}. The owner decides; there is nothing more to do here.`),
        h("p", { class: "small muted top" }, "Reload this page later. Once you are approved you will see a short notice about your data, and then your Dojo.")];
  return [pageHead("Ask for access", "This Dojo is not open to everyone in the tenant."),
    h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" }, panel(eyebrow("lock", "Members only"), ...body)))];
}

async function viewNotice() {
  const n = await api("/api/notice");
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const ack = (state.me && state.me.notice && state.me.notice.required && !state.me.notice.acknowledged)
    ? button("I have read this", async () => {
      ack.disabled = true;
      try { await api("/api/notice/ack", { method: "POST", body: { version: n.version } }); location.hash = "#/today"; location.reload(); }
      catch (e) { status.textContent = e.message; ack.disabled = false; }
    })
    : null;
  return [pageHead("Before you start", `This Dojo is run by ${n.operator}. Please read how your data is handled.`),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow article" },
        n.sections.map((s) => panel(h("h2", null, s.title), String(s.text).split("\n\n").map((para) => h("p", { class: "sub" }, para)))),
        panel(ack ? h("div", { class: "actions" }, ack, button("Leave instead", () => put($view(), viewLeave()), "ghost")) : linkButton("Back", "#/about", "ghost"), status)))];
}

// Download and delete stay open to a member whose access ended, for 30 days (ADR 0009).
function exitPanels() {
  return [panel(eyebrow("download", "Download"),
    h("p", { class: "sub" }, "Download everything Dojo keeps about you as one JSON file."),
    h("div", { class: "actions top" }, h("a", { class: "btn ghost", href: "/api/record/export", download: "dojo-record.json" }, "Download my record"))),
  leavePanel()];
}

function viewEnded() {
  const me = state.me || {};
  const day = me.removed_at ? fmtWhen(me.removed_at) : "";
  return [pageHead("Your access ended", day ? `Your access to this Dojo ended on ${day}.` : "Your access to this Dojo ended."),
    h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" },
      panel(h("p", { class: "sub" }, "For 30 days after that you can still download your record or delete it now. After 30 days Dojo deletes it."),
        h("p", { class: "small muted" }, "Automatic backups of the server can keep a copy for up to 30 days more.")),
      ...exitPanels()))];
}

function viewPausedGate() {
  return [pageHead("This Dojo is paused", "The owner paused it for members. Nothing is deleted."),
    h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" },
      panel(h("p", { class: "sub" }, "While it is paused you can still download your record, or leave and delete it.")),
      ...exitPanels()))];
}

// A member chooses the exams they study from the exams this Dojo has, and can change the choice later
// (ADR 0009, "Enrollment"). The owner studies every exam and never sees this page.
function viewEnroll(first) {
  const me = state.me || {};
  if (isAdmin()) { location.hash = "#/today"; return []; }
  const chosen = new Set(Array.isArray(me.enrolled) ? me.enrolled : []);
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const rows = (me.packages || []).map((p) => {
    const box = h("input", { type: "checkbox", id: `enr-${p.id}`, value: p.id, checked: chosen.has(p.id) });
    return [box, h("label", { class: "conf", for: `enr-${p.id}` }, box, h("span", null, h("b", null, p.exam), ` · ${p.title}`))];
  });
  const save = button(first ? "Start with these" : "Save", async () => {
    const picked = rows.filter(([b]) => b.checked).map(([b]) => b.value);
    if (!picked.length) { status.textContent = "Choose at least one exam."; return; }
    save.disabled = true;
    const active = picked.includes(state.pid) ? state.pid : picked[0];
    try {
      await api("/api/settings", { method: "PUT", body: { enrolled: picked, active_package: active } });
      location.hash = "#/today";
      location.reload();
    } catch (e) { status.textContent = e.message; save.disabled = false; }
  });
  return [pageHead(first ? "Choose your exams" : "Your exams", "Pick the exams you are studying for. You can change this at any time.", null, helpQ("choosing-your-exams")),
    h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" },
      panel(eyebrow("course", "Exams in this Dojo"),
        h("p", { class: "sub" }, "Dojo shows you the course, practice and plan for the exams you pick. Leaving an exam out deletes nothing: your record for it stays, and it comes back if you pick the exam again."),
        rows.map(([, label]) => label),
        h("div", { class: "actions top" }, save, first ? null : linkButton("Back", "#/today", "ghost")), status)))];
}

// What is left of today's allowances (ADR 0009, "Money"): the learner's own numbers, seen only by them.
function allowancePanel() {
  const body = h("div", null, h("p", { class: "small muted" }, "Loading…"));
  api("/api/me/allowance").then((a) => {
    const rows = a.actions.filter((x) => a.owner ? x.used > 0 : true);
    put(body,
      h("p", { class: "sub" }, a.owner ? "You are not limited. What you used today:" : "What you can still do today. It starts again at midnight UTC."),
      rows.length ? h("table", { class: "t" }, h("tbody", null, rows.map((x) => h("tr", null,
        h("td", null, x.label), h("td", { class: "num" }, a.owner ? `${x.used}` : `${x.left} of ${x.allowance} left`))))) : h("p", { class: "small muted" }, "Nothing yet today."));
  }).catch((e) => put(body, h("p", { class: "small muted" }, e.message)));
  return panel(eyebrow("scale", "Today's allowance"), body);
}

function displayNamePanel() {
  const input = h("input", { type: "text", id: "dispname", maxlength: "40", value: (state.me && state.me.display_name) || "", spellcheck: "false" });
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const save = button("Save", async () => {
    try { const r = await api("/api/me/display-name", { method: "PUT", body: { name: input.value } }); state.me.display_name = r.display_name; status.textContent = "Saved."; }
    catch (e) { status.textContent = e.message; }
  }, "ghost");
  return panel(eyebrow("people", "Your name in this Dojo"),
    h("p", { class: "sub" }, "The name other members would see. Your sign-in name is never shown to them."),
    h("div", { class: "datefield" }, h("label", { class: "field", for: "dispname" }, "Display name", input), save), status);
}

function leavePanel() {
  const box = h("input", { type: "text", id: "leaveconfirm", placeholder: "Type LEAVE" });
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const go = button("Leave and delete everything", async () => {
    if (box.value !== "LEAVE") { status.textContent = "Type LEAVE to confirm."; return; }
    go.disabled = true;
    try { await api("/api/team/leave", { method: "POST", body: { confirm: "LEAVE" } }); setGate("left"); put($view(), viewLeft()); }
    catch (e) { status.textContent = e.message; go.disabled = false; }
  }, "danger");
  return panel(eyebrow("x", "Leave this Dojo"),
    h("p", { class: "sub" }, "Your membership ends now and Dojo deletes your whole folder: record, answers, plan, exams and settings. Shared lessons stay. Automatic server backups can keep a copy for up to 30 days."),
    h("div", { class: "datefield" }, h("label", { class: "field", for: "leaveconfirm" }, "Type LEAVE to confirm", box), go), status);
}

function viewLeft() {
  return [pageHead("You left this Dojo", "Your data was deleted."),
    panel(h("p", { class: "sub" }, "Automatic server backups can keep a copy for up to 30 days, then it is gone. You can close this page."))];
}

function viewLeave() {
  return [pageHead("Leave this Dojo", "You can leave at any time."), h("div", { class: "row stackable grow" }, h("div", { class: "col grow article" }, ...exitPanels()))];
}

// ---------------------------------------------------------------- the owner's Members page

async function viewMembers() {
  if (!isAdmin()) { location.hash = "#/today"; return []; }
  const host = h("div", { class: "col grow" });
  const paint = (m) => put(host, membersBody(m, paint));
  const money = h("div", { class: "col grow" });
  const paintMoney = (b) => put(money, budgetPanel(b, paintMoney));
  const [m, b] = await Promise.all([api("/api/team/members"), api("/api/team/budget").catch((e) => ({ error: e.message }))]);
  // Personal mode: the side bar has no Members link and there is no one to manage, so a typed address gets a
  // note about team mode instead of controls that do nothing. With members' data left over, the page stays.
  if (!m.team && !m.suspended) return [pageHead("Members", "This Dojo runs in personal mode: only you sign in.", null, helpQ("members-page")), personalModePanel()];
  paint(m);
  paintMoney(b);
  return [pageHead("Members", "Who may use this Dojo. Only what admission needs: no activity, results or costs of any member.", null, helpQ("members-page")), host, money];
}

function personalModePanel() {
  return panel(eyebrow("people", "Team mode is off"),
    h("p", { class: "sub" }, "In team mode a small group of colleagues uses this Dojo, each with a private record of their own. They ask for access and you approve them on this page. It then also holds their daily allowances, Pause, the end of team mode and the switch for study circles in Play."),
    h("p", { class: "small muted top" }, "Team mode is a setting of the deployment, not a button in the app. Switch it on only after a privacy review."),
    h("div", { class: "actions top" }, linkButton("How team mode works", "#/help/team-mode-switch", "ghost")));
}

// Allowances, the members' daily cap and cost totals (ADR 0009, "Money"). Totals are pooled sums only:
// with fewer than 5 members there is one total for the whole Dojo.
const BUCKETS = { total: "Dojo in total", members: "Members together", members_other: "Members, other exams", owner: "You", content: "Shared content work" };
function budgetPanel(b, paint) {
  if (!b || b.error) return panel(eyebrow("scale", "Allowances and costs"), h("p", { class: "sub" }, (b && b.error) || "Could not be read."));
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const inputs = {};
  const rows = b.allowances.map((x) => {
    const input = h("input", { type: "number", min: "0", max: "1000", step: "1", value: String(x.value), id: `allow-${x.action}`, class: "num" });
    inputs[x.action] = input;
    return h("tr", null, h("td", null, h("label", { for: `allow-${x.action}` }, x.label)), h("td", null, input),
      h("td", { class: "small muted" }, x.value === x.default ? "" : `default ${x.default}`));
  });
  const cap = h("input", { type: "number", min: "0", max: "500", step: "0.5", value: String(b.cap_usd), id: "capusd", class: "num" });
  const save = button("Save", async () => {
    const allowances = {};
    for (const [k, el] of Object.entries(inputs)) allowances[k] = Number.parseInt(el.value, 10);
    try { paint(await api("/api/team/budget", { method: "PUT", body: { allowances, cap: Number(cap.value) } })); }
    catch (e) { status.textContent = e.message; }
  }, "ghost");
  const usd = (x) => (x > 0 && x < 0.01 ? "< $0.01" : `$${Number(x).toFixed(2)}`);
  const label = (k) => BUCKETS[k] || (k.startsWith("exam:") ? `Members, ${k.slice(5)}` : k);
  const sums = (o) => Object.keys(o).length ? Object.entries(o).map(([k, v]) => h("dl", { class: "mrow" }, h("dt", null, label(k)), h("dd", null, usd(v))))
    : h("p", { class: "small muted" }, "Nothing yet.");
  return h("div", { class: "row stackable" },
    h("div", { class: "col grow" }, panel(eyebrow("scale", "Daily allowances per member"),
      h("p", { class: "sub" }, "Each member can do this much a day; it starts again at midnight UTC. You are metered, never limited. Dojo enforces these itself, so you never see a member's numbers."),
      h("div", { class: "table-scroll" }, h("table", { class: "t" }, h("tbody", null, rows))),
      h("div", { class: "datefield top" }, h("label", { class: "field", for: "capusd" }, "Members' daily cap, USD at list price", cap), save), status)),
    h("div", { class: "col w430" }, panel(eyebrow("scale", "Costs, totals only"),
      h("p", { class: "small muted" }, "List-price estimates. An exam gets its own line only while 5 or more members study it."),
      h("h3", null, "Today"), sums(b.totals.today), h("h3", { class: "top" }, "This month"), sums(b.totals.month),
      b.flags ? h("p", { class: "note bad top" }, "Reservation bound exceeded: ", b.flags.kinds.join(", ")) : null)));
}

function membersBody(m, paint) {
  const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const run = async (fn) => { status.textContent = "Working…"; try { paint(await fn()); } catch (e) { status.textContent = e.message; } };
  const post = (url, body) => run(() => api(url, { method: "POST", body: body || {} }));
  const STATUS = { active: "active", pending: "approved, notice not read yet", removed: "removed" };
  const table = h("table", { class: "t" },
    h("thead", null, h("tr", null, ["Display name / sign-in", "Email", "Role", "Status", "Joined", ""].map((x) => h("th", null, x)))),
    h("tbody", null, m.members.map((r) => h("tr", null,
      h("td", null, h("b", null, r.display_name || "–"), r.entra_name ? h("div", { class: "small muted" }, r.entra_name) : null),
      h("td", { class: "small" }, r.email || "–"),
      h("td", null, r.role === "admin" ? "owner" : "learner"), h("td", null, STATUS[r.status] || r.status),
      h("td", null, r.joined ? fmtDay(r.joined) : "–"),
      h("td", null, r.role === "learner" && r.status !== "removed" ? button("Remove", () => {
        if (window.confirm(`Remove ${r.display_name || r.email || "this member"}? They lose access at once and their data is deleted after 30 days.`)) {
          post(`/api/team/members/${r.id}/remove`, { confirm: "REMOVE" });
        }
      }, "ghost sm") : null)))));
  const requests = panel(eyebrow("hand", "Asking for access"),
    m.requests.length ? m.requests.map((q) => requestRow(q, m, post)) : h("p", { class: "sub" }, "No one is waiting."));
  const sync = m.sync || { state: "file" };
  const NO_RESTORE = "No restore of /home has happened since the Pause.";
  const pause = panel(eyebrow("pause", "Pause"),
    h("p", { class: "sub" }, m.paused ? "Paused: members can only download or delete their record. Nothing is deleted." : "Pausing stops members' use at once and deletes nothing. Your own use is not affected."),
    m.paused && sync.state !== "file" ? h("p", { class: "small muted" }, sync.pause_recorded ? "Pause recorded in the lab database." : "Recording the Pause in the lab database…") : null,
    h("div", { class: "actions top" }, m.paused ? button("Resume", () => post("/api/team/resume")) : button("Pause for members", () => post("/api/team/pause"), "ghost"),
      m.paused && sync.state !== "file" ? button("Resume without the database", () => {
        if (window.confirm(`Resume needs the lab database, so a restored /home cannot let removed members back in. Resume without it only if this is true:\n\n${NO_RESTORE}\n\nThis is written to the admin log.`)) {
          post("/api/team/resume", { without_database: true, no_restore: true });
        }
      }, "ghost sm") : null));
  const syncLine = sync.state === "file" ? null : h("p", { class: sync.state === "unsynced" ? "note bad" : "small muted" }, icon(sync.state === "synced" ? "check" : "alert", "sm"), " ", sync.text,
    sync.unknown_lab_schemas ? ` ${plural(sync.unknown_lab_schemas, "lab schema")} belong to no known learner and were left in place.` : "");
  return [m.suspended ? h("p", { class: "status-slate" }, icon("alert", "sm"), `Team mode is off but ${plural(m.suspended, "member")} still have data.`) : null, syncLine,
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow" },
        panel(eyebrow("people", `Members (up to ${m.max_members})`), h("div", { class: "table-scroll" }, table),
          h("p", { class: "small muted top" }, "Removing takes effect on the member's next request. They can download or delete their record for 30 days; then Dojo deletes it.")),
        requests, status),
      h("div", { class: "col w430" }, invitePanel(), pause, circlesSwitch(), endPanel(m, run, post)))];
}

// Play circles (ADR 0010 P-1): the admin role sees only this switch, never anyone's Play data.
function circlesSwitch() {
  const box = h("section", { class: "panel" });
  const st = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
  const paint = (sw) => put(box, eyebrow("people", "Study circles"),
    h("p", { class: "sub" }, sw.on ? "On: members who opted in can form circles and share moments and passes." : "Off: nobody can form a circle."),
    h("p", { class: "small muted top" }, "Turning circles off deletes every circle at once; members opt in again when they are switched back on. The admin role sees only this switch: no circle, member's Play data or shared moment."),
    h("div", { class: "actions top" }, button(sw.on ? "Turn circles off" : "Turn circles on", async () => {
      if (sw.on && !window.confirm("Turn circles off? Every circle, invitation and shared moment is deleted at once.")) return;
      try { paint(await api("/api/team/play", { method: "PUT", body: { on: !sw.on } })); } catch (e) { st.textContent = e.message; }
    }, sw.on ? "ghost sm" : "sm")), st);
  api("/api/team/play").then(paint).catch(() => box.remove());
  return box;
}

function requestRow(q, m, post) {
  const pick = h("select", { "aria-label": "Earlier member" }, h("option", { value: "" }, "Choose the earlier member…"),
    m.rebind_candidates.map((c) => h("option", { value: c.id }, [c.display_name, c.entra_name, c.email].filter(Boolean).join(" · "))));
  const checked = h("input", { type: "checkbox", id: `chk-${q.id}` });
  return h("div", { class: "qentry" },
    h("div", { class: "qh" }, h("b", null, q.name || q.email || "A colleague"), h("span", { class: "small muted" }, `asked ${fmtDay(q.at)}`)),
    q.email ? h("p", { class: "small ink2" }, q.email) : null,
    h("div", { class: "actions top" }, button("Approve", () => post(`/api/team/requests/${q.id}/approve`)),
      button("Decline", () => post(`/api/team/requests/${q.id}/decline`), "ghost")),
    q.used_before ? h("div", { class: "top" },
      h("p", { class: "small ink2" }, "Says they used this Dojo before with another account. Join their earlier record to this account only after you checked with the person directly (a call or a Teams message)."),
      h("div", { class: "datefield" }, pick,
        h("label", { class: "conf", for: `chk-${q.id}` }, checked, h("span", null, "I checked with the person directly")),
        button("Join to earlier record", () => post(`/api/team/requests/${q.id}/rebind`, { member: pick.value, checked: checked.checked }), "ghost sm"))) : null);
}

function invitePanel() {
  return panel(eyebrow("share", "Inviting a colleague"),
    h("p", { class: "sub" }, "Dojo cannot invite anyone: it holds no directory permission. You do this in the Microsoft Entra admin center."),
    h("ol", { class: "steps" },
      h("li", null, "Entra admin center → Users → New user → Invite external user."),
      h("li", null, "Enter the colleague's work email. As the redirect URL use ", h("code", null, location.origin), "."),
      h("li", null, "They accept the invitation and its consent page. How soon they can sign in afterwards is not documented."),
      h("li", null, "They open Dojo, sign in and ask for access. You approve them here.")),
    h("p", { class: "small muted top" }, "Optional hardening: in Entra you can set \u201cAssignment required\u201d on the Dojo app. Then only assigned users reach the Ask for access page."));
}

function endPanel(m, run, post) {
  const e = m.end;
  if (!m.team) return null;
  if (e && e.date) {
    return panel(eyebrow("flag", "Ending team mode", "amber"),
      h("p", { class: "sub" }, `Team mode ends on ${fmtDay(e.date)}. Members can download or delete their record until then; afterwards Dojo deletes their data. Only then switch teamMode off in the deployment.`),
      h("div", { class: "actions top" }, button("Cancel the end", () => post("/api/team/end/cancel"), "ghost")));
  }
  if (e && e.told_at) {
    const day = h("input", { type: "date", id: "enddate", min: e.earliest, value: e.earliest });
    return panel(eyebrow("flag", "Ending team mode", "amber"),
      h("p", { class: "sub" }, `You told every member on ${fmtDay(e.told_at)}. Choose the end date: ${fmtDay(e.earliest)} or later.`),
      h("div", { class: "datefield" }, h("label", { class: "field", for: "enddate" }, "End date", day),
        button("Set the end date", () => post("/api/team/end", { date: day.value }))),
      h("div", { class: "actions top" }, button("Cancel", () => post("/api/team/end/cancel"), "ghost sm")));
  }
  const host = h("div");
  const start = button("End team mode…", async () => {
    try {
      const r = await api("/api/team/end");
      const list = r.emails.filter(Boolean).join("; ");
      const told = h("input", { type: "checkbox", id: "toldall" });
      const copied = h("span", { class: "small muted" });
      put(host,
        h("p", { class: "sub top" }, "Dojo cannot send mail. Tell every member yourself, by email or Teams: the end date, that they can download or delete their record until then, and that afterwards their data is deleted."),
        h("textarea", { readonly: true, rows: "3", "aria-label": "Member email addresses" }, list || "No members."),
        h("div", { class: "actions top" }, button("Copy addresses", async () => {
          try { await navigator.clipboard.writeText(list); copied.textContent = "Copied."; } catch { copied.textContent = "Select and copy them by hand."; }
        }, "ghost sm"), copied),
        h("label", { class: "conf top", for: "toldall" }, told, h("span", null, "I have told every member.")),
        h("div", { class: "actions top" }, button("Confirm", () => post("/api/team/end/told", { confirmed: told.checked }))),
        h("p", { class: "small muted" }, "The 14 days of notice start when you confirm."));
    } catch (err) { put(host, h("p", { class: "sub" }, err.message)); }
  }, "ghost");
  return panel(eyebrow("flag", "End team mode"),
    h("p", { class: "sub" }, "Ending is staged: you tell every member, then at least 14 days later Dojo deletes their data. Pausing instead deletes nothing."),
    h("div", { class: "actions top" }, start), host);
}

// ---------------------------------------------------------------- a newer build is waiting

/* The page is one file the browser holds on to until it is reloaded. When the server starts
   answering with a different build, say so and let the learner reload when it suits them: never
   during a timed exam, never while the microphone is on. Reloading keeps the page you are on. */
const BUILD_EVERY_MS = 300000;
const buildTag = (b) => String(b || "").slice(0, 12);
const newBuild = { mine: "", waiting: "", later: "", banner: null };

// The build this page was served with: the ?v= the server put on its own script tag.
function loadedBuild() {
  const el = document.querySelector('script[src*="/static/app.js"]');
  const m = /[?&]v=([^&]*)/.exec((el && el.getAttribute("src")) || "");
  return m ? buildTag(decodeURIComponent(m[1])) : "";
}

/* /healthz needs no sign-in, so this check still works after the sign-in has ended, and it can never start
   one. A background request that Easy Auth answers with its sign-in page gets a new nonce cookie, which
   breaks a sign-in under way in another tab ("invalid nonce", and round it goes). */
async function serverBuild() {
  try {
    const r = await fetch("/healthz", { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!r.ok) return "";  // a quiet background check never takes the learner anywhere
    const data = await r.json();
    return buildTag(data && data.build);
  } catch (e) { return ""; }
}

const buildBusy = () => document.body.classList.contains("examing") || document.body.classList.contains("checking") || anyRecording();

/* The exam runner is a page like any other: a learner can walk away from a running attempt and the
   class on the body goes with it, while the clock on the server keeps going. So ask the server which
   attempt is open before saying anything. If the answer cannot be had, stay quiet. The check runs on its
   own, so it says it is not a page (X-Requested-With): after the sign-in has ended Easy Auth then answers
   403 instead of starting a sign-in, whose new nonce cookie would break one under way in another tab. */
async function examRunning() {
  const pid = state.pid || (state.me && state.me.profile && state.me.profile.active_package) || "";
  if (!pid) return false;
  try {
    const r = await fetch(`/api/exams?package=${encodeURIComponent(pid)}`,
      { headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" });
    if (!r.ok) return true;
    const data = await r.json();
    return !!(data && data.running);
  } catch (e) { return true; }
}

function showNewBuild() {
  if (newBuild.banner || !newBuild.waiting || newBuild.waiting === newBuild.later || buildBusy()) return;
  const line = h("p", null, h("b", null, "A newer version of Dojo is ready."), " Reloading keeps you on this page.");
  const reload = button("Reload", async () => {
    reload.disabled = true;
    // Asked again after the exam check: a recording may have started while that request was out.
    if (buildBusy() || await examRunning() || buildBusy()) {  // something started while the banner waited
      put(line, h("b", null, "Not now."), " A timed exam or a recording is still running. Dojo will ask again when it is over.");
      window.setTimeout(hideNewBuild, 8000);
      return;
    }
    location.reload();
  }, "sm");
  newBuild.banner = h("div", { class: "newbuild", role: "status" },
    h("span", { class: "ic" }, icon("refresh", "sm")),
    line,
    h("div", { class: "actions" }, reload,
      button("Not now", () => { newBuild.later = newBuild.waiting; hideNewBuild(); }, "ghost sm")));
  document.body.append(newBuild.banner);
}

/* The same offer, but only after the server has said no exam is running. */
async function offerNewBuild() {
  if (!newBuild.waiting || newBuild.waiting === newBuild.later) return;
  if (buildBusy() || await examRunning()) { hideNewBuild(); return; }
  showNewBuild();
}

function hideNewBuild() {
  if (newBuild.banner) { newBuild.banner.remove(); newBuild.banner = null; }
}

async function checkBuild() {
  if (signIn.banner) return;  // signed out: the reload it asks for brings the new build too
  if (buildBusy()) { hideNewBuild(); return; }  // ask nothing, say nothing, until the learner is free
  const now = await serverBuild();
  if (now && newBuild.mine && now !== newBuild.mine) newBuild.waiting = now;
  await offerNewBuild();  // the exam question is only worth asking when there is something to offer
}

function watchTheBuild() {
  newBuild.mine = loadedBuild() || buildTag(state.me && state.me.build);
  window.setInterval(checkBuild, BUILD_EVERY_MS);
}

// ---------------------------------------------------------------- the 3-minute check

/* A short spoken session: Dojo reads up to three questions out loud and you answer out loud, or type.
   They are ordinary check items - no hints, no lesson, one answer - so the results land in the Record
   like any other answer. The three minutes are a guide: nothing is ever cut off. */

const CHECK_GUIDE_MS = 180000;
const CHECK_POLL_MS = 2000;
const CHECK_POLL_TRIES = 5;   // failed looks in a row before the waiting screen says it lost touch
const CHECK_NEXT = "Your answers are judged in the background, from the official sources, quoting your own words. "
  + "Each result appears in your Record when it is ready. Nothing here predicts an exam result.";

/* One question, one clock, nothing else: the sidebar steps aside, as it does for a timed exam. */
function checkMode(on) {
  document.body.classList.toggle("checking", !!on);
  if (on) hideNewBuild(); else showNewBuild();
}

/* The check is closed while a timed practice exam is running. That is not a failure, so it does not
   look like one. */
const checkShut = (e) => !!(e && e.status === 409);
function checkClosedPanel(e) {
  return h("section", { class: "panel" }, eyebrow("clock", "Closed for now"),
    h("h2", null, "The 3-minute check is closed"),
    h("p", { class: "sub" }, (e && e.message) || "A timed practice exam is running."),
    h("p", { class: "small muted" }, "An exam is answered under exam conditions: no coach, no lessons, no checks. "
      + "It opens again as soon as the exam is handed in."),
    h("div", { class: "actions top" }, linkButton("Back to Today", "#/today", "ghost")));
}

function pickRow(p) {
  const name = p.picked === "recall" ? "bell" : p.picked === "due_later" ? "refresh" : "target";
  return h("li", { class: "cpick" }, h("span", { class: "cpip" }, icon(name, "xs")),
    h("span", null, h("b", null, p.text), h("small", null, p.why)));
}

/* The card on Today. It says who would be asked and why before anything is written. */
function checkCard(plan) {
  if (!plan) return null;
  const head = h("div", { class: "panel-h" }, eyebrow("mic", "3-minute check"),
    chip(plan.closed ? "closed" : plan.running ? "not finished" : "spoken · no hints", plan.running ? "amber" : "line"));
  if (plan.closed) {
    return h("section", { class: "panel" }, head,
      h("p", { class: "sub" }, "Closed while a timed practice exam is running. It opens again when the exam is handed in."));
  }
  if (plan.running) {
    return h("section", { class: "panel" }, head,
      h("p", { class: "sub" }, "You are in the middle of a check. Its questions are waiting where you left them."),
      h("div", { class: "actions top" }, linkButton("Go back to it", `#/check/${plan.running}`, "amber")));
  }
  if (!plan.picks.length) {
    return h("section", { class: "panel" }, head,
      h("p", { class: "sub" }, "Nothing is due for a spoken check right now. Every skill you have met on your own has "
        + "already been shown later. Every other skill has been tried at least once, or has no official page to "
        + "write a question from just now."));
  }
  return h("section", { class: "panel" }, head,
    h("p", { class: "sub" }, `${plural(plan.picks.length, "question")}, read out loud, answered out loud — or typed. `
      + `About ${plan.minutes} minutes. No hints, no lesson.`),
    h("ul", { class: "cpicks" }, plan.picks.map(pickRow)),
    h("div", { class: "actions top" }, linkButton("Start a 3-minute check", "#/check", "amber"),
      linkButton("What happens", "#/check", "ghost sm")));
}

/* #/check - what would be asked, and why, before a single question is written. #/recall is the same
   page for a session made only of the recalls due today (ADR 0011). */
async function viewCheckStart(recall) {
  let plan;
  try {
    plan = await api(`/api/checks?package=${encodeURIComponent(state.pid)}${recall ? "&recall=true" : ""}`);
  } catch (e) {
    if (checkShut(e)) return checkClosedPanel(e);
    throw e;
  }
  if (plan.running) { location.hash = `#/check/${plan.running}`; return []; }
  const status = h("p", { class: "small muted" });
  const go = plan.picks.length ? button(recall ? "Start the recalls" : "Start the check", async () => {
    go.disabled = true;
    status.textContent = "Starting…";
    const token = state.nav;
    try {
      const s = await api("/api/checks", { method: "POST", body: { package: state.pid, recall: !!recall } });
      if (token === state.nav) location.hash = `#/check/${s.id}`;
    } catch (e) { status.textContent = e.message; go.disabled = false; }
  }, "amber") : null;
  const how = [
    ["mic", "Dojo reads each question out loud in the coach's voice. The words are on the screen as well — always."],
    ["target", "You answer out loud and Dojo writes it down, or you type. Both count exactly the same."],
    ["clock", `${plan.minutes} minutes is a guide, not a limit. Nothing is ever cut off mid-answer.`],
    ["record", "Each answer is judged in the background. The result appears in your Record, never here."],
  ];
  if (recall) how.splice(1, 0, ["bell", "Each question is new. Right again: the next recall comes a little later. "
    + "Not yet: it comes back tomorrow, with the lesson section to look at first."]);
  return [pageHead(recall ? "Recall" : "3-minute check", `${plan.package.exam} · ${plural(plan.picks.length, "question")} out loud, no hints.`,
      eyebrow(recall ? "bell" : "mic", recall ? "Due today" : "Spoken"), recall ? helpQ("recalls") : helpQ("spoken-checks")),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow" },
        h("section", { class: "panel" },
          h("div", { class: "panel-h" }, eyebrow("sparkle", "What Dojo would ask, and why"),
            chip(`${plan.minutes} min guide`, "line")),
          plan.picks.length
            ? h("ul", { class: "cpicks big" }, plan.picks.map(pickRow))
            : h("p", { class: "sub" }, recall ? "No recalls are due right now. They come back on the day they are due."
              : "Nothing is due for a check right now. Pick any skill in the course instead."),
          h("p", { class: "note top" }, recall
            ? "Oldest due first, heaviest in the exam first. At most a few a day, so a backlog never becomes a wall."
            : "Recalls due today come first, then skills that would show you still know it later, then skills that "
              + "have never been tried, heaviest in the exam first."),
          h("div", { class: "actions top" }, go, linkButton("Not now", "#/today", "ghost")), status)),
      h("div", { class: "col w430" },
        h("section", { class: "panel" }, eyebrow("scale", "How it works"),
          h("ul", { class: "tips big" }, how.map(([name, text]) => h("li", null, icon(name, "sm"), h("span", null, text)))),
          h("p", { class: "note top" }, NOT_A_PREDICTION))))];
}

/* #/check/<id> - the session itself. */
async function viewCheckRun(cid) {
  const token = state.nav;
  let S;
  try {
    S = await api(`/api/checks/${cid}`);
  } catch (e) {
    if (checkShut(e)) return checkClosedPanel(e);
    throw e;
  }
  if (token !== state.nav) return null;  // the learner moved on while this was loading
  let ending = false, started = 0, lost = false;
  checkMode(true);
  const ready = h("span", { class: "cnum", role: "status", "aria-live": "polite" });
  const clockText = h("b", null);
  const timer = h("span", { class: "ctimer" }, icon("clock", "sm"), clockText);
  const stage = h("div", { class: "cstage" });
  const leave = button("End the check", () => finish(), "ghost sm");
  const bar = h("div", { class: "cbar" },
    h("span", { class: "cseg" }, chip(S.recall ? "Recall · no hints" : "Check · no hints", "amber"), ready),
    h("span", { class: "cseg center" }, timer),
    h("span", { class: "cseg right" }, leave));
  const el = h("div", { class: "check" }, bar, stage);

  const current = () => S.questions.find((q) => q.state === "ready" && !q.answered) || null;
  const position = (q) => S.questions.indexOf(q) + 1;

  function counters() {
    const done = S.ready + S.skipped;
    ready.textContent = S.coming ? `${done} of ${S.total} questions ready` : `${S.answered} of ${S.total} answered`;
  }

  function runClock() {
    if (state.timer) return;
    started = Date.now();
    const paint = () => {
      const left = (S.minutes ? S.minutes * 60000 : CHECK_GUIDE_MS) - (Date.now() - started);
      clockText.textContent = left >= 0 ? `${clock(left)} of ${S.minutes} min` : `${clock(-left)} over — no rush`;
      timer.classList.toggle("over", left < 0);
    };
    paint();
    state.timer = window.setInterval(paint, 1000);
  }

  function skips() {
    const out = S.questions.filter((q) => q.state === "skipped");
    return out.length
      ? h("div", { class: "cskip" }, eyebrow("x", out.length === 1 ? "One skill was left out" : `${out.length} skills were left out`),
          out.map((q) => h("p", { class: "small ink2" }, h("b", null, q.text + ": "), q.note)))
      : null;
  }

  function drawWaiting() {
    if (lost) {
      const again = button("Try again", () => { lost = false; drawWaiting(); poll(); }, "amber");
      return put(stage, h("div", { class: "cwrap" },
        panel(eyebrow("refresh", "Lost touch for a moment"),
          h("h2", null, "Dojo cannot reach the server just now"),
          h("p", { class: "sub" }, "Your check is kept, and so is every question already written. "
            + "This is usually a short break in the connection."),
          h("div", { class: "actions top" }, again, linkButton("Back to Today", "#/today", "ghost")))));
    }
    const rows = S.questions.map((q, i) => h("div", { class: "cstep" },
      h("span", { class: "n" }, `${i + 1}`),
      h("span", null, h("b", null, q.text),
        h("small", null, q.state === "ready" ? "ready" : q.state === "skipped" ? "left out" : q.step || "waiting for a free slot"))));
    put(stage, h("div", { class: "cwrap" },
      panel(eyebrow("wand", "Getting your questions ready…"),
        h("h2", null, "Dojo is writing your questions"),
        h("div", { class: "progress", role: "progressbar", "aria-label": "Questions ready" }, h("div", { class: "progress-fill" })),
        h("p", { class: "small muted", role: "status", "aria-live": "polite" },
          `${S.ready} of ${S.total} ready. You start as soon as the first one is.`),
        h("div", { class: "csteps" }, rows),
        h("p", { class: "small muted" }, "Each question is written from the official sources and checked by an "
          + "independent model. It takes a few tens of seconds. You can leave this page and come back."),
        skips())));
  }

  function drawQuestion(q) {
    runClock();
    const n = position(q);
    const asked = Date.now();
    const ta = h("textarea", { rows: 6, maxlength: 8000, id: "canswer", placeholder: "Say it out loud, or type it here." });
    const mic = canSpeak() ? speakBox(ta, { item: q.item }) : null;
    const pq = podcastQuestion({ id: q.item, podcast_question: q.podcast_question });
    const status = h("p", { class: "small muted", role: "status", "aria-live": "polite" });
    const audio = q.audio
      ? h("audio", { class: "caudio", controls: true, preload: "auto", src: `/api/media/${q.audio}`,
          "aria-label": `${presenterLabel(S.voice)} reading question ${n}` })
      : null;
    const again = audio ? button([icon("listen", "sm"), "Read it again"], () => { audio.currentTime = 0; audio.play().catch(() => {}); }, "ghost sm") : null;
    const sure = sureBox();
    const send = button("Send and go on", async () => {
      const text = ta.value.trim();
      if (!text) { status.textContent = "Say or write an answer first."; ta.focus(); return; }
      if (!pq.ready(status)) return;
      send.disabled = true;
      ta.readOnly = true;   // committed: the confidence question cannot change the answer
      const elapsed = Math.round((Date.now() - asked) / 1000);
      const spoken = !!(mic && mic.used());
      const edited = !!(mic && mic.changed());
      const receipts = mic ? mic.receipts() : [];
      if (mic) mic.dispose();  // the answer is on its way: stop listening now
      if (audio) audio.pause();
      const confidence = await askSure(sure);
      if (token !== state.nav) return;
      status.textContent = "Sending…";
      try {
        const job = await api(`/api/items/${q.item}/answer`, { method: "POST", body: {
          text, elapsed_s: elapsed, confidence,
          input: spoken ? "spoken" : "typed", edited_before_send: edited, receipts, ...pq.body(),
        } });
        sessionStorage.setItem(`grade:${q.item}`, job.id);
        if (token !== state.nav) return;
        S = await api(`/api/checks/${cid}`);
        if (token !== state.nav) return;  // nothing may be drawn, spoken or timed on another page
        counters();
        draw();
      } catch (e) {
        if (token !== state.nav) return;
        send.disabled = false;
        ta.readOnly = false;
        if (await pq.refused(e, status)) return;  // the podcast question started to apply: it is shown now
        if (token !== state.nav) return;
        status.textContent = e.message;
      }
    }, "amber");
    put(stage, h("div", { class: "cwrap" },
      h("section", { class: "panel cq" },
        h("div", { class: "panel-h tight" },
          h("span", { class: "qnum" }, `Question ${n} of ${S.total} · no hints, one answer`),
          chip(q.domain, "line")),
        h("p", { class: "cskill" }, q.text, h("span", { class: "mono" }, skillTag(q.skill))),
        h("p", { class: "qtext" }, q.stem),
        h("div", { class: "cplay" }, audio, again,
          h("span", { class: "small muted" }, audio ? `${presenterLabel(S.voice)} reads the question out loud. The words are here too.`
            : "The question could not be read out loud this time. The words are here.")),
        h("label", { class: "field", for: "canswer" }, "Your answer, in your own words", ta),
        mic ? mic.el : null,
        pq.el,
        h("div", { class: "qfoot" }, h("div", { class: "actions" }, send), status),
        sure,
        h("p", { class: "cwhy" }, icon("sparkle", "xs"), h("span", null, h("b", null, "Why this one: "), q.why))),
      S.coming ? h("p", { class: "small muted cmore", role: "status", "aria-live": "polite" },
        `Dojo is still writing ${plural(S.coming, "question")}. It will be here when you have answered this one.`) : null,
      skips()));
    if (audio) audio.play().catch(() => { /* the browser wants a click first: the button is right there */ });
    ta.focus();
  }
  function drawDone() {
    stopClock();
    checkMode(false);
    timer.hidden = true;  // the guide clock has nothing left to guide
    leave.hidden = true;
    state.pkg = null;  // the answers just given belong to the record: let the next page read it again
    const answered = S.questions.filter((q) => q.answered);
    const left = S.questions.filter((q) => !q.answered && q.state !== "skipped");
    // Started from the 5 minutes: the next step is the repair.
    const five = fiveRead();
    const inFive = !!(five && five.check === cid);
    if (inFive && five.step === "recalls") { five.step = "repair"; five.did += answered.length; fiveSave(five); }
    put(stage, h("div", { class: "cwrap" },
      h("section", { class: "panel cdone" },
        h("div", { class: "panel-h" }, eyebrow("check", S.recall ? "Recalls finished" : "Check finished"), chip(`${answered.length} of ${S.total} answered`, "line")),
        h("h2", null, answered.length ? (S.recall ? "That is today's recall done" : "That is the check done") : "Nothing was answered this time"),
        h("h3", null, "What happens next"),
        h("p", { class: "sub" }, CHECK_NEXT),
        h("ul", { class: "cpicks" }, S.questions.map((q) => h("li", { class: "cpick" },
          h("span", { class: "cpip" }, icon(q.answered ? "check" : q.state === "skipped" ? "x" : "clock", "xs")),
          h("span", null, h("b", null, q.text),
            h("small", null, q.answered ? "answered · being judged" : q.state === "skipped" ? q.note : "not answered"))))),
        left.length ? h("p", { class: "note top" }, `${plural(left.length, "question")} went unanswered. `
          + "Unanswered questions are not recorded either way - you can answer them from the Record whenever you like.") : null,
        h("div", { class: "actions top" }, inFive ? linkButton("Go on with your 5 minutes", "#/five", "amber") : linkButton("Open the Record", "#/record", "amber"),
          linkButton("Back to Today", "#/today", "ghost")),
        h("p", { class: "note top" }, NOT_A_PREDICTION))));
  }

  function draw() {
    if (S.state === "ended" || ending) return drawDone();
    const q = current();
    if (q) return drawQuestion(q);
    if (S.coming) return drawWaiting();
    return finish();
  }

  async function finish() {
    if (ending) return;
    ending = true;
    leave.disabled = true;
    try { S = await api(`/api/checks/${cid}/end`, { method: "POST" }); } catch (e) { /* the screen is the same either way */ }
    if (token !== state.nav) return;
    counters();
    drawDone();
  }

  // While questions are still being written, keep the screen honest about it. One failed look is
  // tried again after CHECK_POLL_MS; after CHECK_POLL_TRIES in a row the waiting screen says so calmly
  // and offers to try again, instead of waiting for ever.
  async function poll() {
    let fails = 0;
    while (token === state.nav && !ending && S.coming) {
      await sleep(CHECK_POLL_MS);
      if (token !== state.nav || ending) return;
      const was = current();
      try {
        S = await api(`/api/checks/${cid}`);
        fails = 0;
      } catch (e) {
        if (++fails < CHECK_POLL_TRIES) continue;
        lost = true;
        if (!current()) drawWaiting();   // a question on screen stays; the next draw shows it
        return;
      }
      if (token !== state.nav || ending) return;
      counters();
      if (!was && (current() || !S.coming)) draw();  // the waiting screen has something to show now
      else if (!was) drawWaiting();
    }
  }
  poll();

  counters();
  draw();
  return el;
}

// ---------------------------------------------------------------- Plan

/* Dojo proposes the sessions; the learner decides. Any session can be moved, skipped or pinned, and
   the free times are the learner's own. Every time here is the plan's own zone (Berlin), sent by the
   server as text, so this page and the calendar always show the same times. */

const PLAN_ICON = { listen: "listen", lesson: "read", practice: "hand", check: "check", later: "refresh", check3: "mic", exam: "practice", lab: "lab", recall: "bell" };
const PLAN_KIND_TEXT = {
  listen: "Listen to the lesson. It fits a time when you can only listen.",
  lesson: "Study the lesson: read it, watch it or listen to it.",
  practice: "Practise with hints. Each hint you open is recorded as help.",
  check: "A check: a new item, with no help offered.",
  later: "A later check: a new item with no help, a day or more after the teaching.",
  check3: "A 3-minute check: up to three questions out loud, no hints.",
  recall: "Recalls due that day: a new question for each skill you showed before, no hints. Answering again on a later day helps it last.",
  exam: "A timed practice exam, one question at a time.",
  lab: "A hands-on lab: write SQL in your private sandbox, then Dojo checks the database. It needs a desk, so Dojo never plans it in a time when you can only listen. No lab fills a pip.",
};
const PLAN_DONE_BY = {
  listen: "the lesson opened for this skill, or you said you listened to it in your podcast app.",
  lesson: "the lesson opened for this skill, or you said you listened to it in your podcast app.",
  practice: "an answer for this skill.",
  check: "an answer for this skill with no help: a probe or a check.",
  later: "an answer for this skill with no help, after the check came due.",
  check3: "a check answer in this exam, such as a 3-minute check.",
  recall: "a recall answer in this exam.",
  exam: "a practice exam started in this exam.",
  lab: "a check of this lab in the database, passed or not.",
};
const CAL_NOTES = [
  ["lock", "An event says only the exam, the activity, the skill, its length and a link into Dojo. No answers, no evidence and no reasons: the calendar sits in your work mailbox."],
  ["eye", "Dojo never reads your calendar, so it cannot see your meetings. It plans only inside the free times you set on this page."],
  ["clock", "Your calendar app fetches the plan by itself, every few hours, so a change here can take that long to show there."],
  ["bell", "Each event has a reminder before it starts. Whether it rings is up to your calendar app."],
  ["link", "The link in an event opens that activity in Dojo, which asks you to sign in as usual."],
];
const PLAN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const PLAN_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const PLAN_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOUR_PX = 56;
const BLOCK_MIN_PX = 56;   // room for two lines of name and one of length and exam, however short the session
const PLAN_WEEKS = 26;     // the server shows half a year back and ahead

const hm = (s) => String(s || "").slice(11, 16);
const minutesOf = (t) => { const m = /(\d{2}):(\d{2})/.exec(String(t || "")); return m ? Number(m[1]) * 60 + Number(m[2]) : 0; };
const dur = (m) => (m >= 120 && m % 60 === 0 ? `${m / 60} h` : m > 120 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`);
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const shiftDay = (s, n) => { const d = parseDay(s); d.setDate(d.getDate() + n); return isoDay(d); };
const mondayOf = (s) => { const d = parseDay(s); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return isoDay(d); };
const place = (el, prop, px) => { el.style.setProperty(prop, typeof px === "number" ? `${px}px` : px); return el; };
const sessionName = (s) => (s.title ? `${s.label} · ${s.title}` : s.label);
const examsWord = (n) => (n > 2 ? `all ${n} exams` : n === 2 ? "both exams" : "your exam");
function planDay(s, opts = {}) {
  const d = parseDay(String(s || "").slice(0, 10));
  if (!d) return "";
  const day = PLAN_WEEKDAYS[d.getDay()];
  const month = PLAN_MONTHS[d.getMonth()];
  return [opts.weekday ? (opts.short ? day.slice(0, 3) : day) : null, d.getDate(), opts.short ? month.slice(0, 3) : month,
    opts.year ? d.getFullYear() : null].filter((x) => x !== null).join(" ");
}
function fetchedText(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const t = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const n = Math.round((day(new Date()) - day(d)) / 86400000);
  return n === 0 ? `today at ${t}` : n === 1 ? `yesterday at ${t}` : `on ${d.getDate()} ${PLAN_MONTHS[d.getMonth()].slice(0, 3)} at ${t}`;
}
function planLabel(s) {
  const when = s.at ? `${planDay(s.at, { weekday: true })}, ${hm(s.at)} to ${hm(s.end)}` : "Waiting for a free time";
  const state = s.state === "done" ? ", done" : s.state === "now" ? ", on now" : "";
  return `${when}: ${sessionName(s)}, ${s.minutes} minutes, ${s.exam}${state}${s.pinned ? ", pinned" : ""}`;
}
const planClass = (base, s) => [base, s.state === "done" ? "done" : s.group, s.state === "now" ? "now" : ""].filter(Boolean).join(" ");

function calendarChip(cal, onclick) {
  const old = cal.fetched && Date.now() - new Date(cal.fetched).getTime() > 86400000;
  const [kind, name, text] = !cal.active ? ["line", "plan", "Not in your calendar yet"]
    : !cal.fetched ? ["amber", "clock", "Calendar has not fetched it yet"]
    : [old ? "amber" : "sage", old ? "clock" : "check", `Calendar fetched ${fetchedText(cal.fetched)}`];
  return h("button", { class: `chip ${kind} chipbtn`, type: "button", onclick, title: "Your calendar link" }, icon(name, "xs"), text);
}

/* The private calendar link. Like the podcast link, it is shown once and only its fingerprint is kept. */
function calendarPanel(onStatus) {
  const box = h("section", { class: "panel podcast calp", id: "calendar", tabindex: "-1", "aria-label": "Your calendar" });
  let st = null, ask = null, note = "";
  const reload = async () => {
    try { st = await api("/api/calendar"); } catch (e) { note = e.message; }
    if (st && onStatus) onStatus(st);
    render();
  };
  const run = async (fn) => {
    try { await fn(); note = ""; } catch (e) { note = e.message; }
    ask = null;
    await reload();
  };
  const make = (path) => run(async () => { state.calLink = await api(path, { method: "POST" }); });
  const off = () => run(async () => { await api("/api/calendar/link", { method: "DELETE" }); state.calLink = null; });
  const confirmRow = (question, yes, onYes) => h("div", { class: "pod-confirm", role: "group", "aria-label": yes },
    h("p", { class: "small" }, question),
    h("div", { class: "actions" }, button(yes, onYes, "sm"), button("Cancel", () => { ask = null; render(); }, "ghost sm")));
  const manage = () => ask === "new" ? confirmRow("Calendars using the old link stop updating. Subscribe with the new link, then remove the old Dojo calendar.", "Make a new link", () => make("/api/calendar/link/new"))
    : ask === "off" ? confirmRow("Calendars using this link stop updating, and their events stay until you remove the Dojo calendar there: Dojo cannot reach your calendar.", "Turn the feed off", off)
    : h("div", { class: "actions top" }, button("Make a new link", () => { ask = "new"; render(); }, "ghost sm"),
      button("Turn the feed off", () => { ask = "off"; render(); }, "ghost sm"));
  const sync = () => h("p", { class: "calsync small" }, icon(st.fetched ? "check" : "clock", "sm"),
    h("span", null, st.fetched ? `Your calendar app last fetched the plan ${fetchedText(st.fetched)}. Dojo keeps only that time.`
      : "No calendar app has fetched the plan yet. Outlook can take a few hours the first time."));
  const render = () => {
    const head = eyebrow("plan", "Your calendar");
    if (!st) { put(box, head, h("p", { class: "sub" }, note || "Loading…")); return; }
    const link = st.active && state.calLink && state.calLink.created === st.created ? state.calLink : null;
    if (!link) state.calLink = null;
    const said = note ? h("p", { class: "small", role: "alert" }, note) : null;
    const notes = h("ul", { class: "pod-notes" }, CAL_NOTES.map(([name, text]) => h("li", null, icon(name, "sm"), h("span", null, text))));
    if (!st.active) {
      put(box, head, h("h3", { class: "pod-h" }, "Put the plan in Outlook"),
        h("p", { class: "sub" }, "Outlook subscribes to a private link and shows every session as an event with a reminder. Each event opens exactly that activity in Dojo."),
        h("p", { class: "sub" }, "Anyone with the link can see what you plan to study and when, so keep it to yourself."),
        said, h("div", { class: "actions top" }, button("Get my calendar link", () => make("/api/calendar/link"), "amber")), notes);
      return;
    }
    if (!link) {
      put(box, head, h("h3", { class: "pod-h" }, "Your calendar link is on"),
        h("p", { class: "sub" }, `You made it on ${fmtWhen(st.created)}. Dojo keeps only a fingerprint of it, so it cannot show it again.`),
        h("p", { class: "sub" }, "To add the plan to another calendar, make a new link."), sync(), said, manage(), notes);
      return;
    }
    const url = h("input", { type: "text", readonly: true, value: link.url, "aria-label": "Calendar link", spellcheck: "false" });
    url.addEventListener("focus", () => url.select());
    const copied = h("span", { class: "small muted", role: "status" });
    const copy = button("Copy", async () => {
      try { await navigator.clipboard.writeText(link.url); copied.textContent = "Copied."; }
      catch (e) { url.focus(); url.select(); copied.textContent = "Select the link and copy it."; }
    }, "sm");
    put(box, head, h("h3", { class: "pod-h" }, "Your calendar link"),
      h("div", { class: "pod-url" }, url, copy), copied,
      h("ol", { class: "calsteps" },
        h("li", null, "In Outlook, open the calendar and choose Add calendar."),
        h("li", null, "Choose Subscribe from web and paste the link."),
        h("li", null, "Name it Dojo and choose Import. The sessions appear after the first fetch.")),
      h("img", { class: "pod-qr", src: link.qr, width: 200, height: 200, alt: "QR code of the calendar link" }),
      h("p", { class: "small muted" }, "The code carries the same link, for a calendar app on a phone. Dojo shows the link only until you leave this page."),
      sync(), said, manage(), notes);
  };
  render();
  reload();
  return box;
}

function rulesBar(v, onChange) {
  return h("div", { class: "rulesbar" }, icon("scale", "sm"),
    h("p", null, h("b", null, "You set: "), v.summary, v.rules.reminder ? ` · a reminder ${v.rules.reminder} min before` : " · no reminder"),
    h("button", { class: "link", type: "button", onclick: onChange }, "Change"));
}

/* The learner's own times. Numbers are checked here so a typo gets a sentence, not a schema error. */
function rulesEditor(v, onSave, onCancel) {
  const r = v.rules;
  const said = h("p", { class: "small re-said", role: "alert" });
  const num = (value, max, label, id, step) => h("input", { type: "number", class: "num", min: 0, max, step, value, id, inputmode: "numeric", "aria-label": label });
  const dayInputs = r.day_minutes.map((m, i) => num(m, 600, `${PLAN_WEEKDAYS[(i + 1) % 7]}: minutes at most`, `re-d${i}`, 5));
  const week = num(r.week_minutes, 4200, "The week: minutes at most", "re-w", 5);
  const weekHint = h("span", { class: "small muted" });
  const hint = () => { const n = Number(week.value); weekHint.textContent = week.value !== "" && Number.isInteger(n) && n >= 0 ? `= ${dur(n)}` : ""; };
  week.addEventListener("input", hint);
  hint();
  const earliest = h("input", { type: "time", id: "re-e", value: r.earliest });
  const reminder = num(r.reminder, 120, "Reminder: minutes before a session", "re-r", 1);
  const list = h("div", { class: "re-wins" });
  const rows = [];
  const addBtn = button([icon("plan", "sm"), "Add a free time"], () => addRow({ label: "", days: [0, 1, 2, 3, 4], start: "18:00", end: "19:00", audio_only: false }, true), "ghost sm");
  const sync = () => { addBtn.disabled = rows.length >= 12; };
  const addRow = (w, focus) => {
    const name = h("input", { type: "text", value: w.label, maxlength: 40, placeholder: "Name, like Lunch", "aria-label": "Name of the free time" });
    const boxes = PLAN_DAYS.map((d, i) => h("input", { type: "checkbox", checked: w.days.includes(i), "aria-label": PLAN_WEEKDAYS[(i + 1) % 7] }));
    const pills = boxes.map((b, i) => {
      const pill = h("label", { class: b.checked ? "dpill on" : "dpill" }, b, PLAN_DAYS[i]);
      b.addEventListener("change", () => pill.classList.toggle("on", b.checked));
      return pill;
    });
    const from = h("input", { type: "time", value: w.start, "aria-label": "From" });
    const to = h("input", { type: "time", value: w.end, "aria-label": "To" });
    const audio = h("input", { type: "checkbox", checked: w.audio_only });
    const row = { read: () => ({ label: name.value.trim(), days: boxes.map((b, i) => (b.checked ? i : -1)).filter((i) => i >= 0),
      start: from.value, end: to.value, audio_only: audio.checked }) };
    const el = h("div", { class: "re-win", role: "group", "aria-label": w.label || "A free time" },
      name, h("div", { class: "dpills" }, pills),
      h("div", { class: "re-time" }, from, h("span", { class: "muted" }, "to"), to),
      h("label", { class: "re-audio" }, audio, icon("listen", "sm"), "Audio only"),
      h("button", { class: "iconbtn", type: "button", title: "Remove this free time", "aria-label": "Remove this free time",
        onclick: () => { rows.splice(rows.indexOf(row), 1); el.remove(); sync(); addBtn.focus(); } }, icon("x", "sm")));
    rows.push(row);
    list.append(el);
    sync();
    if (focus) name.focus();
  };
  const whole = (x, max) => x.value !== "" && Number.isInteger(Number(x.value)) && Number(x.value) >= 0 && Number(x.value) <= max;
  const save = button("Save", async () => {
    said.textContent = "";
    const wins = rows.map((x) => x.read());
    const wrong = !dayInputs.every((x) => whole(x, 600)) ? "Give each day a whole number of minutes from 0 to 600."
      : !whole(week, 4200) ? "Give the week a whole number of minutes from 0 to 4200 (70 hours)."
      : !earliest.value ? "Give the earliest time, like 08:00."
      : !whole(reminder, 120) ? "Give the reminder a whole number of minutes from 0 to 120."
      : wins.some((w) => !w.start || !w.end) ? "Give each free time a start and an end."
      : wins.some((w) => !w.days.length) ? "Pick at least one day for each free time."
      : "";
    if (wrong) { said.textContent = wrong; return; }
    save.disabled = true;
    try {
      await onSave({ day_minutes: dayInputs.map((x) => Number(x.value)), week_minutes: Number(week.value), earliest: earliest.value,
        reminder: Number(reminder.value), windows: wins });
    } catch (e) { said.textContent = e.message; save.disabled = false; }
  }, "amber");
  const el = h("section", { class: "panel rules-ed", "aria-labelledby": "re-h" },
    h("div", { class: "panel-h" }, h("h2", { id: "re-h" }, "Your times"), h("span", { class: "small muted" }, "Dojo plans only inside these")),
    h("fieldset", null, h("legend", { class: "eyebrow" }, "Minutes a day, at most"),
      h("div", { class: "re-days" }, dayInputs.map((x, i) => h("label", { class: "field" }, PLAN_DAYS[i], x)))),
    h("div", { class: "re-line" },
      h("label", { class: "field" }, "The week, at most", h("span", { class: "re-with" }, week, h("span", { class: "small muted" }, "min"), weekHint)),
      h("label", { class: "field" }, "Nothing before", earliest),
      h("label", { class: "field" }, "Reminder", h("span", { class: "re-with" }, reminder, h("span", { class: "small muted" }, "min before")))),
    h("fieldset", null, h("legend", { class: "eyebrow" }, "Free times"),
      h("p", { class: "small muted" }, "An audio-only time gets lessons to listen to, nothing to read or type. Use it for a commute."),
      list, h("div", { class: "actions top" }, addBtn)),
    said,
    h("div", { class: "actions top" }, save, button("Cancel", onCancel, "ghost")));
  r.windows.forEach((w) => addRow(w, false));
  return el;
}

function planBlock(s, y, height, pick) {
  const mark = s.state === "done" ? icon("check", "xs bold") : s.pinned ? icon("pin", "xs") : icon(PLAN_ICON[s.kind] || "plan", "xs");
  const el = h("button", { class: planClass("wk-ev", s), type: "button", "data-sid": s.id,
    onclick: () => pick(s), "aria-label": planLabel(s), title: `${hm(s.at)}–${hm(s.end)} · ${sessionName(s)} · ${s.exam}` },
    h("span", { class: "t" }, mark, h("span", null, sessionName(s))),
    h("span", { class: "m" }, `${s.minutes} min`, h("code", null, s.exam)));
  return place(place(el, "top", y), "height", height);
}

function agendaRow(s, pick) {
  const bits = [`${s.minutes} min`, s.exam, s.state === "done" ? "done" : s.state === "now" ? "on now" : null, s.pinned ? "pinned" : null];
  return h("button", { class: planClass("ag-ev", s), type: "button", "data-sid": s.id, onclick: () => pick(s), "aria-label": planLabel(s) },
    h("span", { class: "ag-t" }, s.at ? hm(s.at) : "—"),
    h("span", { class: "ag-i" }, icon(PLAN_ICON[s.kind] || "plan", "sm")),
    h("span", { class: "ag-n" }, h("b", null, sessionName(s)), h("small", null, bits.filter(Boolean).join(" · "))),
    icon("right", "sm"));
}

function planWeekCard(v, pick) {
  const w = v.week;
  const offset = Math.round((parseDay(w.start) - parseDay(mondayOf(v.today))) / 604800000);
  const nav = (n, name, label) => Math.abs(offset + n) <= PLAN_WEEKS
    ? h("a", { class: "iconbtn", href: `#/plan/${shiftDay(w.start, 7 * n)}`, title: label, "aria-label": label }, icon(name, "sm"))
    : h("span", { class: "iconbtn off", "aria-hidden": "true" }, icon(name, "sm"));
  const top = h("div", { class: "wk-top" },
    h("div", { class: "wk-nav" }, nav(-1, "left", "Previous week"),
      h("h2", { class: "wk-range" }, `${planDay(w.start, { short: true })} – ${planDay(shiftDay(w.start, 6), { short: true, year: true })}`),
      nav(1, "right", "Next week"), offset ? h("a", { class: "link small", href: "#/plan" }, "This week") : null),
    h("div", { class: "legend" }, h("span", null, h("i", { class: "sw teach" }), "teaching"), h("span", null, h("i", { class: "sw check" }), "checks & exams"),
      h("span", null, h("i", { class: "sw done" }), "done"), h("span", { class: "lg-free" }, h("i", { class: "sw free" }), "free time you set"),
      h("span", { class: "lg-free" }, h("i", { class: "sw audio" }), "audio only")));

  const byDay = new Map(w.days.map((d) => [d.date, []]));
  for (const s of v.sessions) if (byDay.has(s.at.slice(0, 10))) byDay.get(s.at.slice(0, 10)).push(s);
  let lo = minutesOf(v.rules.earliest), hi = lo + 8 * 60;
  for (const d of w.days) for (const x of d.windows) { lo = Math.min(lo, minutesOf(x.start)); hi = Math.max(hi, minutesOf(x.end)); }
  for (const s of v.sessions) { lo = Math.min(lo, minutesOf(s.at)); hi = Math.max(hi, minutesOf(s.at) + s.minutes); }
  lo = Math.floor(lo / 60) * 60;
  hi = Math.min(1440, Math.ceil(hi / 60) * 60);
  const px = (m) => ((m - lo) / 60) * HOUR_PX;
  const hours = [];
  for (let m = lo; m < hi; m += 60) hours.push(m);
  const gutter = h("div", { class: "wk-gutter", "aria-hidden": "true" },
    hours.map((m) => place(h("span", null, `${String(m / 60).padStart(2, "0")}:00`), "top", px(m))));
  let height = px(hi);
  const cols = w.days.map((d) => {
    const col = h("div", { class: ["wk-col", d.today ? "today" : "", d.past ? "past" : "", d.limit ? "" : "rest"].filter(Boolean).join(" "),
      role: "group", "aria-label": `${planDay(d.date, { weekday: true })}: ${d.limit ? `${d.minutes} of ${d.limit} minutes planned` : "rest day"}` });
    for (const x of d.windows) {
      const y = px(minutesOf(x.start));
      col.append(place(place(h("div", { class: x.audio_only ? "wk-win audio" : "wk-win", "aria-hidden": "true",
        title: `${x.label || "Free"} · ${x.start}–${x.end}${x.audio_only ? " · audio only" : ""}` },
        h("span", null, x.audio_only ? icon("listen", "xs") : null, h("em", null, x.label || "Free"))), "top", y), "height", px(minutesOf(x.end)) - y));
    }
    if (!d.limit) col.append(h("div", { class: "wk-rest", "aria-hidden": "true" }, icon("moon", "sm"), h("b", null, "Rest day"), h("small", null, "no prompts")));
    let floor = 0;
    for (const s of byDay.get(d.date)) {
      const y = Math.max(px(minutesOf(s.at)), floor);   // a short block may push the next one down, never onto it
      const tall = Math.max((s.minutes / 60) * HOUR_PX, BLOCK_MIN_PX);
      col.append(planBlock(s, y, tall - 2, pick));
      floor = y + tall;
      height = Math.max(height, floor);
    }
    return col;
  });
  const today = w.days.findIndex((d) => d.today);
  const now = minutesOf(v.now);
  if (today >= 0 && now >= lo && now <= hi) {
    cols[today].append(place(h("div", { class: "wk-now", "aria-hidden": "true" }), "top", px(now)));
    gutter.append(place(h("span", { class: "wk-nowt" }, hm(v.now)), "top", px(now)));
  }
  const head = h("div", { class: "wk-head", "aria-hidden": "true" }, h("span", null), w.days.map((d) => h("div", { class: d.today ? "wk-day today" : "wk-day" },
    h("span", null, h("span", { class: "dn" }, d.name.slice(0, 3)), h("b", null, String(Number(d.date.slice(8, 10))))),
    h("small", { class: d.minutes > d.limit ? "over" : null }, d.limit ? `${d.minutes} of ${d.limit} min` : d.minutes ? `${d.minutes} min` : "rest day"))));
  const body = place(place(h("div", { class: "wk-body" }, gutter, cols), "height", Math.ceil(height)), "--hh", HOUR_PX);

  const agenda = h("div", { class: "wk-agenda" }, w.days.map((d) => {
    const list = byDay.get(d.date);
    return h("section", { class: d.today ? "ag-day today" : "ag-day", "aria-label": planDay(d.date, { weekday: true }) },
      h("div", { class: "ag-h" }, h("b", null, planDay(d.date, { weekday: true, short: true })), d.today ? chip("today", "amber") : null,
        h("span", { class: d.minutes > d.limit ? "small over" : "small muted" },
          d.limit ? `${d.minutes} of ${d.limit} min` : d.minutes ? `${d.minutes} min on a rest day` : "rest day")),
      list.length ? list.map((s) => agendaRow(s, pick)) : h("p", { class: "small muted" }, d.limit ? "Nothing planned." : "No prompts."),
      d.limit && d.windows.length ? h("p", { class: "ag-free small muted" }, "Free: ",
        d.windows.map((x) => `${x.label || "free"} ${x.start}–${x.end}${x.audio_only ? ", audio only" : ""}`).join(" · ")) : null);
  }));
  const empty = !v.sessions.length
    ? h("p", { class: "note top" }, w.start > v.today ? "Dojo places sessions up to two weeks ahead. This week fills in as it comes closer."
      : "Nothing was planned in this week.")
    : null;
  return h("section", { class: "panel wk", "aria-label": "The week" }, top, h("div", { class: "wk-grid" }, head, body), agenda, empty);
}

function planWaiting(v, pick) {
  if (!v.waiting.length) return null;
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("clock", "Waiting for a free time"), h("span", { class: "small muted" }, plural(v.waiting.length, "session"))),
    h("p", { class: "sub" }, "Missed, and no free time in the next two weeks fits them yet. Dojo places them as soon as one does. Add a free time, or move one yourself."),
    h("div", { class: "ag-list top" }, v.waiting.map((s) => agendaRow(s, pick))));
}

function planWeekTotals(v) {
  const w = v.week;
  const scale = Math.max(w.limit, w.planned, 1);
  const over = w.days.filter((d) => d.minutes > d.limit);
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow(null, "This week"), h("span", { class: "small muted" }, "planned time")),
    w.packages.map((p) => h("div", { class: "pt" }, h("code", null, p.exam),
      h("div", { class: "ptbar", role: "img", "aria-label": `${p.exam}: ${dur(p.minutes)} planned, ${dur(p.done)} of it done` },
        wide(h("i", { class: "done" }), `${((p.done / scale) * 100).toFixed(2)}%`),
        wide(h("i", { class: "plan" }), `${(((p.minutes - p.done) / scale) * 100).toFixed(2)}%`)),
      h("b", null, dur(p.minutes)))),
    h("p", { class: "note top" }, `${dur(w.planned)} planned of your ${dur(w.limit)} limit for the week`,
      w.done ? `, ${dur(w.done)} of it done. ` : ". ",
      over.length ? `Over the day's limit on ${over.map((d) => d.name).join(", ")}: a session you placed yourself counts there too.`
        : "No day goes over its limit."));
}

function planMoved(v) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow(null, "Moved for you"), h("span", { class: "small muted" }, "the last 7 days")),
    v.moved.length
      ? h("div", { class: "mv-list" }, v.moved.map((m) => h("div", { class: "mv" }, h("span", { class: "mv-i" }, icon(m.removed ? "x" : "refresh", "sm")),
          h("div", null,
            h("p", { class: "mv-ft" }, m.from ? [h("s", null, h("span", { class: "vh" }, "from "), m.from), icon("arrow", "xs"), h("span", { class: "vh" }, " to ")] : null,
              h("b", null, m.to)),
            h("p", { class: "small" }, m.title ? `${m.label} · ${m.title}` : m.label),
            h("small", null, `${m.why} · ${m.exam}`)))))
      : h("p", { class: "sub" }, "Nothing was moved. When a session is not done by its end, Dojo moves it to the next free time and says so here."),
    h("p", { class: "mv-note small" }, icon("shield", "sm"),
      h("span", null, h("b", null, "Skipped something? "), "Nothing is lost: untested stays untested, and nothing is marked failed.")));
}

function planHorizon(v, onDate) {
  const today = parseDay(v.today);
  const ahead = v.horizon.filter((x) => x.date && x.days_left >= 0);
  const span = Math.max(28 * 86400000, ...ahead.map((x) => parseDay(x.date) - today));
  const pct = (s) => `${Math.min(100, Math.max(0, ((parseDay(s) - today) / span) * 100)).toFixed(2)}%`;
  const ticks = [];
  for (const t = new Date(today.getFullYear(), today.getMonth() + 1, 1); t - today <= span; t.setMonth(t.getMonth() + 1)) ticks.push(isoDay(t));
  const dateForm = (x, text) => {
    const input = h("input", { type: "date", min: v.today, value: x.date || "", "aria-label": `Exam date for ${x.exam}` });
    const said = h("span", { class: "small", role: "alert" });
    const save = button("Save", async () => {
      if (!input.value) { said.textContent = "Pick a date first."; return; }
      save.disabled = true;
      try { await onDate(x.package, input.value); } catch (e) { said.textContent = e.message; save.disabled = false; }
    }, "ghost sm");
    return h("div", { class: "hz-date" }, h("p", { class: "small muted" }, text), h("div", { class: "datefield" }, input, save), said);
  };
  const rows = v.horizon.map((x) => {
    if (!x.date) return h("div", { class: "hz-row" }, h("code", null, x.exam), dateForm(x, "No exam date yet. Set one and Dojo plans practice exams in the four weeks before it."));
    if (x.days_left < 0) return h("div", { class: "hz-row" }, h("code", null, x.exam), dateForm(x, `The exam date, ${planDay(x.date, { short: true, year: true })}, has passed. Set the next one to plan toward it.`));
    const rehearse = x.rehearse_from > v.today ? x.rehearse_from : v.today;
    const track = h("div", { class: "hz-track", role: "img",
      "aria-label": `${x.exam}: learning until ${planDay(rehearse)}, then a practice exam every week until the exam on ${planDay(x.date)}` },
      place(h("i", { class: "learn" }), "width", pct(rehearse)),
      place(place(h("i", { class: "reh" }), "left", pct(rehearse)), "width", `calc(${pct(x.date)} - ${pct(rehearse)})`),
      place(h("i", { class: "flag" }), "left", pct(x.date)), h("i", { class: "here" }));
    const exams = x.practice_exams ? `, with ${plural(x.practice_exams, "practice exam")}` : "";
    return h("div", { class: "hz-row" }, h("code", null, x.exam),
      h("div", null, track,
        h("p", { class: "small" }, h("b", null, planDay(x.date, { short: true, year: true })), ` · ${plural(x.days_left, "day")} to go`),
        h("p", { class: "small muted" }, x.rehearse_from > v.today ? `A practice exam every week from ${planDay(x.rehearse_from, { short: true })}.` : "A practice exam every week until the exam.",
          ` ${dur(x.planned_minutes)} planned in the next two weeks${exams}.`)));
  });
  return h("section", { class: "panel hz" },
    h("div", { class: "panel-h" }, eyebrow(null, "To the exams"), h("span", { class: "small muted" }, examsWord(v.horizon.length))),
    h("p", { class: "hz-here" }, h("i", { class: "dot" }), h("b", null, "You are here"), ` · ${planDay(v.today)}`),
    ahead.length ? h("div", { class: "hz-axis", "aria-hidden": "true" }, ticks.map((d) => place(h("span", null, planDay(d, { short: true }).split(" ")[1]), "left", pct(d)))) : null,
    rows,
    ahead.length ? h("div", { class: "legend hz-legend" }, h("span", null, h("i", { class: "sw learn" }), "learn and check"),
      h("span", null, h("i", { class: "sw reh" }), "practice exams"), h("span", null, h("i", { class: "sw flag" }), "exam")) : null,
    h("p", { class: "note top" }, "Dojo places sessions two weeks ahead, and nothing on an exam day or after it. ",
      "It plans a practice exam every week in the last four weeks before an exam, and a DP-800 lab a week until you pass each one. ",
      v.unplaced ? `${plural(v.unplaced, "more step")} wait for room after that. ` : "", "Nothing here is a pass prediction."));
}

function planSheet(s, v, update) {
  const dlg = h("dialog", { class: "sheet", "aria-labelledby": "sheet-h" });
  const said = h("p", { class: "small sheet-said", role: "alert" });
  const base = `/api/plan/sessions/${encodeURIComponent(s.id)}`;
  const run = async (btn, path, opts, text) => {
    btn.disabled = true;
    said.textContent = "";
    try {
      const apply = await update(path, opts, text, s.id);
      dlg.close();
      apply();
    } catch (e) { said.textContent = e.message; btn.disabled = false; }
  };
  const when = s.at ? `${planDay(s.at, { weekday: true, short: true })} · ${hm(s.at)}–${hm(s.end)}` : "Waiting for a free time";
  const how = s.state === "done" ? "Done: your Record shows it."
    : s.state === "now" ? "On now."
    : s.state === "waiting" ? "No free time in the next two weeks fits it yet. Dojo places it as soon as one does, or you move it yourself."
    : s.by === "you" ? "You put it here, so Dojo keeps it at this time. If it is not done by its end, Dojo moves it to the next free time."
    : s.pinned ? "Pinned, so Dojo keeps it at this time. If it is not done by its end, Dojo moves it to the next free time."
    : "Planned by Dojo. Until a day before, Dojo may move it when your times or your Record change.";
  const acts = [];
  let moveBox = null, skipBox = null;
  if (s.state !== "done") {
    const input = h("input", { type: "datetime-local", id: "sheet-when", value: s.at || "", min: v.now });
    const moveBtn = button("Move it", () => {
      const to = input.value.slice(0, 16);
      if (!to) { said.textContent = "Pick a day and a time."; return; }
      run(moveBtn, `${base}/move`, { method: "POST", body: { start: to } },
        `Moved to ${planDay(to, { weekday: true, short: true })} at ${to.slice(11)}. Dojo keeps it there.`);
    }, "sm");
    moveBox = h("div", { class: "sheet-box", hidden: true },
      h("label", { class: "field", for: "sheet-when" }, "New day and time (Berlin time)", input), h("div", { class: "actions" }, moveBtn));
    const skipBtn = button("Skip it", () => run(skipBtn, `${base}/skip`, { method: "POST" }, "Skipped. Dojo does not plan that step again before tomorrow."), "sm");
    skipBox = h("div", { class: "sheet-box", hidden: true },
      h("p", { class: "small" }, "Dojo takes it out of the plan and does not plan that step again before tomorrow. Nothing is marked failed: untested stays untested."),
      h("div", { class: "actions" }, skipBtn));
    const show = (box, other) => { box.hidden = !box.hidden; other.hidden = true; said.textContent = ""; };
    acts.push(button([icon("clock", "sm"), "Move"], () => { show(moveBox, skipBox); if (!moveBox.hidden) input.focus(); }, "ghost sm"));
    if (s.at) {
      const pinBtn = button([icon("pin", "sm"), s.pinned ? "Unpin" : "Pin"], () => run(pinBtn, `${base}/pin`, { method: "POST", body: { pinned: !s.pinned } },
        s.pinned ? "Unpinned. Dojo may move it again." : "Pinned. Dojo keeps it at this time."), "ghost sm");
      acts.push(pinBtn);
    }
    acts.push(button([icon("x", "sm"), "Skip"], () => show(skipBox, moveBox), "ghost sm"));
  }
  put(dlg, h("div", { class: "sheet-in" },
    h("div", { class: "between" }, h("p", { class: "eyebrow" }, icon(PLAN_ICON[s.kind] || "plan", "sm"), when),
      h("button", { class: "iconbtn", type: "button", "aria-label": "Close", onclick: () => dlg.close() }, icon("x", "sm"))),
    h("h2", { id: "sheet-h" }, sessionName(s)),
    h("div", { class: "chips top" }, chip(s.exam, "ink"), chip(`${s.minutes} min`, "line"),
      s.state === "done" ? chip("done", "sage") : s.state === "now" ? chip("on now", "amber") : s.state === "waiting" ? chip("waiting", "amber") : null,
      s.pinned ? chip("pinned", "line") : null),
    h("p", { class: "sub" }, PLAN_KIND_TEXT[s.kind] || ""),
    h("p", { class: "small muted" }, how),
    h("p", { class: "small muted" }, `Done is read from your Record: ${PLAN_DONE_BY[s.kind] || "the activity itself."}`),
    h("div", { class: "actions sheet-acts" }, h("a", { class: "btn amber sm", href: s.link }, icon("arrow", "sm"), s.state === "done" ? "Open it" : "Start"), acts),
    moveBox, skipBox, said));
  const onHash = () => dlg.close();
  window.addEventListener("hashchange", onHash);
  dlg.addEventListener("close", () => { window.removeEventListener("hashchange", onHash); dlg.remove(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // a click on the backdrop
  document.body.append(dlg);
  dlg.showModal();
}

/* #/plan and #/plan/<a day of the week> */
async function viewPlan(week) {
  const q = week ? `?week=${week}` : "";
  const P = { v: null, editing: false, note: "", elsewhere: false };
  const head = h("div", { class: "between top wrap plan-head" });
  const said = h("p", { class: "plan-said", role: "status", tabindex: "-1" });
  const main = h("div", { class: "plan-main" });
  const chips = h("div", { class: "chips" });
  const cal = calendarPanel((st) => { if (P.v) { P.v.calendar = st; drawChip(); } });
  const toCal = () => { cal.scrollIntoView({ behavior: "smooth", block: "start" }); cal.focus({ preventScroll: true }); };
  const drawChip = () => put(chips, calendarChip(P.v.calendar, toCal));
  const synced = () => (P.v.calendar.active ? " Your calendar shows the change after its next fetch." : "");
  const refocus = (id) => {
    const el = id ? [...main.querySelectorAll("[data-sid]")].find((e) => e.dataset.sid === id && e.getClientRects().length) : null;
    if (el) el.focus(); else said.focus({ preventScroll: true });
  };
  const render = () => {
    const v = P.v;
    drawChip();
    put(head, pageHead(`Week of ${planDay(v.week.start)}`,
      "Dojo plans each step inside the free times you set, and moves a session that was missed. You decide: move, skip or pin any session."
        + (P.elsewhere ? " All times are Berlin time." : ""),
      eyebrow(null, v.horizon.length > 1 ? `Plan · ${examsWord(v.horizon.length)}` : "Plan"), helpQ("plan-and-the-calendar-link")), chips);
    put(said, P.note ? [icon("check", "sm"), h("span", null, P.note)] : []);
    put(main,
      P.editing ? rulesEditor(v, saveRules, () => { P.editing = false; render(); }) : rulesBar(v, () => { P.editing = true; P.note = ""; render(); }),
      h("div", { class: "row stackable" },
        h("div", { class: "col grow" }, planWeekCard(v, pick), planWaiting(v, pick), cal),
        h("div", { class: "col w338" }, planWeekTotals(v), planMoved(v), planHorizon(v, saveDate))));
  };
  // A change comes back as the new plan; the sheet closes before the page is drawn again.
  const update = async (path, opts, text, id) => {
    const v = await api(path + q, opts);
    return () => { P.v = v; P.note = text + synced(); render(); refocus(id); };
  };
  const saveRules = async (body) => {
    P.v = await api(`/api/plan/rules${q}`, { method: "PUT", body });
    P.editing = false;
    P.note = "Saved. Dojo planned again inside your times." + synced();
    render();
    refocus(null);
  };
  const saveDate = async (pid, value) => {
    await api("/api/settings", { method: "PUT", body: { exam_dates: { [pid]: value } } });
    state.me = await api("/api/me");
    renderHeader();
    renderSide();
    P.v = await api(`/api/plan${q}`);
    P.note = "Saved the exam date. Dojo planned again." + synced();
    render();
    refocus(null);
  };
  const pick = (s) => planSheet(s, P.v, update);
  P.v = await api(`/api/plan${q}`);
  // The server's "now" is Berlin wall-clock time; read here as local time, it is off unless this device keeps Berlin time.
  P.elsewhere = Math.abs(new Date(P.v.now) - Date.now()) > 10 * 60000;
  render();
  return h("div", { class: "plan" }, head, said, main);
}

/* Today's planned sessions, on Today. */
function todayPlanCard(tp) {
  if (!tp) return null;
  const row = (s) => h("a", { class: planClass("tp-row", s), href: s.link, "aria-label": planLabel(s) },
    h("span", { class: "tp-t" }, hm(s.at)),
    h("span", { class: "tp-dot", "aria-hidden": "true" }, s.state === "done" ? icon("check", "xs bold") : null),
    h("span", { class: "tp-n" }, h("b", null, sessionName(s)),
      h("small", null, [`${s.minutes} min`, s.exam, s.state === "done" ? "done" : s.state === "now" ? "on now" : null].filter(Boolean).join(" · "))),
    icon("right", "sm"));
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("plan", "Today"), h("a", { class: "link small", href: "#/plan" }, tp.calendar.active ? "in your calendar" : "your plan")),
    tp.sessions.length ? h("div", { class: "tp-list" }, tp.sessions.map(row))
      : h("p", { class: "sub" }, tp.limit ? "Nothing is planned for today." : "A rest day: nothing is planned."),
    tp.calendar.active ? null : h("p", { class: "small muted top" }, h("a", { class: "link", href: "#/plan" }, "Put the plan in your calendar"),
      " to get a reminder before each session."));
}

// ---------------------------------------------------------------- add an exam (screen 12)

/* Type an exam code; Dojo reads the official study guide on Microsoft Learn and shows what it found and
   what writing and narrating the course costs at list price. Nothing is built before "Build this
   course", and presenter videos are filmed only after their own, priced approval. */
const CODE_FORM = /^\s*(?:exam\s*)?([a-z]{2})\s*[-\u2013\u2014_]?\s*(\d{3})\s*$/i;
const CODE_HELP = "Two letters and three digits, such as AZ-104, DP-300 or GH-500.";
const examCode = (raw) => { const m = CODE_FORM.exec(String(raw || "")); return m ? `${m[1]}-${m[2]}`.toUpperCase() : null; };
const usd = (x) => (x === null || x === undefined ? "–" : x > 0 && x < 0.01 ? "< $0.01" : `$${Number(x).toFixed(2)}`);
const gb = (bytes) => `${((bytes || 0) / 1e9).toFixed(1)} GB`;
const weightText = (d) => (d.weight_min === d.weight_max ? `${d.weight_min}%` : `${d.weight_min}–${d.weight_max}%`);
// A study guide dates its skills to the day or only to the month ("2026-01").
function fmtAsOf(s) {
  const m = /^(\d{4})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" }) : fmtDay(s) || String(s || "");
}
const EXAM_POLL_MS = 3000;
const COURSE_POLL_MS = 15000;
let lookupFresh = false;   // "Try again" asks Microsoft Learn again instead of using the page read a moment ago

const BUILD_STEPS = [
  ["Read the study guide", "every skill measured and its weight, word for word"],
  ["Match official sources", "Microsoft Learn and GitHub Docs pages; a second AI must quote each one"],
  ["Write, then check", "a second AI can block any line"],
  ["Narrate", "Listen and your podcast. Presenter videos only when you approve their price"],
];
const BUILD_RULES = [
  "Official sources only, linked and quoted",
  "A page is read as data: nothing written on it gives Dojo orders",
  "Watching or listening never counts as proof",
  "No pass prediction: the exam decides",
];

const stepHead = (n, title, note) => h("div", { class: "panel-h" },
  h("p", { class: "eyebrow" }, h("span", { class: "stepno" }, n), title), note ? h("span", { class: "small muted" }, note) : null);

function howBuiltPanel() {
  return panel(h("div", { class: "panel-h" }, eyebrow(null, "How it is built"), h("a", { class: "link small", href: "#/quality" }, "See Studio")),
    h("ol", { class: "bsteps" }, BUILD_STEPS.map(([b, s], i) => h("li", null, h("span", { class: "n" }, i + 1),
      h("div", null, h("b", null, b), h("small", null, s))))));
}

function rulesPanel() {
  return panel(eyebrow(null, "The rules it keeps"),
    h("div", { class: "top" }, BUILD_RULES.map((r) => h("p", { class: "share-line" }, icon("check", "sm"), h("span", null, r)))));
}

// Priced lines as the Studio shows them: a line with no list price says so and is not guessed, and the
// total says what it leaves out.
function lineAmount(l) {
  const n = (x) => Number(x).toLocaleString();
  if (!l.model) return `${n(l.quantity)} ${l.unit}`;
  const price = l.per_million ? ` at ${perMillion(l.per_million.input)} and ${perMillion(l.per_million.output)} per million` : "";
  return `${l.model}: ${n(l.input_tokens)} input and ${n(l.output_tokens)} output tokens${price}`;
}
// What a total leaves out, said next to it and next to the button that approves it.
const leftOut = (est) => (est.left_out && est.left_out.length ? `${est.left_out.join(". ")}.` : "Some lines have no price.");
// When the list prices were read, said right under a total. Prices that could not be refreshed are a
// warning there, never only inside a collapsed section.
function pricesAge(est) {
  if (est.stale) return h("p", { class: "conf bad" }, icon("alert", "xs"), h("span", null, est.fetched
    ? `The price list could not be refreshed just now. These are the list prices from ${fmtDate(est.fetched)}, and they may be out of date.`
    : "The price list could not be read just now, so no line has a price."));
  return est.fetched ? h("p", { class: "small muted" }, `List prices from ${fmtDate(est.fetched)}. Dojo reads them again after a day.`) : null;
}
function estimateRows(est, what) {
  return [
    est.lines.map((l) => h("div", { class: "costrow" },
      h("div", null, h("b", null, l.what), h("small", null, lineAmount(l)), l.found ? null : h("small", { class: "bad" }, l.note)),
      h("span", { class: l.found ? "cost" : "cost none" }, l.found ? usd(l.cost) : "not included"))),
    h("div", { class: "costrow total" },
      h("div", null, h("b", null, est.complete ? what : `${what}, for the lines with a price`), est.complete ? null : h("small", { class: "bad" }, `Not the full cost. ${leftOut(est)}`)),
      h("span", { class: "cost" }, usd(est.total))),
    pricesAge(est),
    h("details", { class: "how" }, h("summary", null, "How Dojo estimates this"),
      h("ul", { class: "facts" }, (est.assumptions || []).map((a) => h("li", null, a))),
      h("p", { class: "small muted top" }, est.label)),
  ];
}

const notesBlock = (notes) => (notes && notes.length ? h("details", { class: "how" },
  h("summary", null, `${plural(notes.length, "note")} from reading the pages`),
  h("ul", { class: "facts" }, notes.map((n) => h("li", null, n)))) : null);

function foundCard(f) {
  const g = f.guide;
  const top = Math.max(1, ...f.domains.map((d) => d.weight_max));
  const when = !g.as_of ? "no date on the page" : g.forthcoming ? `skills measured from ${fmtAsOf(g.as_of)}` : `skills measured as of ${fmtAsOf(g.as_of)}`;
  const facts = [
    `${plural(f.domains.length, "domain")}, ${plural(f.groups, "group")} and ${plural(f.skills, "skill")}, word for word from the study guide.`,
    g.forthcoming ? `This version of the skills starts on ${fmtAsOf(g.as_of)}. Until then the exam may still follow the earlier list.` : null,
    g.changes ? `The change log lists ${plural(g.changes, "change")}${g.changed ? ` for ${fmtDay(g.changed)}` : ""}.` : null,
    f.cert && f.cert.update_quote ? ["The exam page says: ", h("q", null, f.cert.update_quote)] : null,
    f.retirement ? `Microsoft Learn says this exam retires on ${fmtDay(f.retirement.date) || f.retirement.date}.` : null,
    f.cert && f.cert.duration_minutes ? `${f.cert.duration_minutes} minutes for the real exam, from the exam page.`
      : "No exam duration found, so Dojo cannot time a practice exam for it.",
  ].filter(Boolean);
  return h("div", { class: "found" },
    h("div", { class: "found-h" }, h("span", { class: "badge" }, f.code),
      h("div", { class: "grow" }, h("h3", null, f.title), f.cert && f.cert.name ? h("p", { class: "small muted" }, f.cert.name) : null,
        h("div", { class: "chips top" }, chip([icon("check", "xs"), `study guide · ${when}`], g.forthcoming ? "amber wrapok" : "sage wrapok")))),
    h("div", { class: "wlist" }, f.domains.map((d) => h("div", { class: "wrow" },
      h("span", { class: "wt" }, d.title, h("small", null, plural(d.skills, "skill"))),
      h("span", { class: "w" }, weightText(d)),
      h("span", { class: "bar" }, wide(h("i"), `${Math.round((100 * d.weight_max) / top)}%`))))),
    h("ul", { class: "facts top" }, facts.map((x) => h("li", null, h("span", null, x)))),
    h("div", { class: "found-f" },
      h("span", null, icon("target", "xs"), f.practice ? ["Official practice assessment: ", ext(f.practice.url, "available · linked")] : "No official practice assessment linked"),
      h("span", null, icon("link", "xs"), ext(g.url, "Study guide")),
      f.cert ? h("span", null, icon("link", "xs"), ext(f.cert.url, "Exam page")) : null,
      (f.training || []).map((t) => h("span", null, icon("read", "xs"), ext(t.url, t.title)))),
    notesBlock(f.notes));
}

const LOOKUP_HEAD = {
  missing: ["alert", "No study guide found"],
  retired: ["clock", "A retired exam"],
  unparsed: ["alert", "Dojo could not read the skills"],
  unreadable: ["alert", "Microsoft Learn did not answer"],
  added: ["check", "Already in Dojo"],
};

// A build that stopped keeps the pages it checked; the owner may clear them.
function clearBuild(row, text) {
  const msg = h("p", { class: "small", role: "status" });
  const b = button("Clear them", async () => {
    b.disabled = true;
    try {
      await api(`/api/exam-packages/${row.package}/remove`, { method: "POST", body: { confirm: "REMOVE" } });
      route();
    } catch (e) { b.disabled = false; msg.textContent = e.message; msg.className = "small bad"; }
  }, "ghost sm");
  return h("div", { class: "note top" }, text, h("div", { class: "actions top" }, b), msg);
}

function lookupMessage(f, kept) {
  const [ic, title] = LOOKUP_HEAD[f.state] || ["alert", "Not found"];
  const bits = [eyebrow(ic, title, f.state === "added" ? null : "rose"), h("p", { class: "sub" }, f.message)];
  if (f.state === "missing") bits.push(h("p", { class: "small muted top" }, `${CODE_HELP} Dojo looks for its study guide on `, ext(f.guide_url, "Microsoft Learn"), "."));
  if (f.state === "retired" && f.retirement) bits.push(h("p", { class: "small muted top" }, h("q", null, f.retirement.text), " ", ext(f.guide_url, "study guide")));
  if (f.state === "unparsed") bits.push(h("ul", { class: "facts top" }, (f.problems || []).map((p) => h("li", null, p))),
    h("p", { class: "small muted top" }, ext(f.guide_url, "Open the study guide")));
  if (f.state === "unreadable") bits.push(h("div", { class: "actions top" }, button("Try again", () => { lookupFresh = true; route(); }, "ghost sm")));
  if (f.state === "added") bits.push(h("div", { class: "actions top" }, button("Open its course", () => openExam(f.package), "sm")));
  if (kept) bits.push(clearBuild(kept, `An earlier build of ${kept.exam} stopped after ${kept.checked || 0} of ${plural(kept.skills || 0, "skill")}. The pages it checked are kept until you clear them.`));
  return h("div", { class: f.state === "added" ? "found msg" : "found msg bad" }, bits);
}

function buildPanel(f, date, kept, token) {
  const msg = h("p", { class: "small", role: "status" });
  const go = button([icon("wand", "sm"), `Build this course`], async () => {
    go.disabled = true;
    msg.className = "small muted";
    msg.textContent = "Starting…";
    try {
      await api("/api/exam-packages", { method: "POST", body: { code: f.code, exam_date: date.value || null } });
      if (token === state.nav) route();
    } catch (e) { go.disabled = false; msg.className = "small bad"; msg.textContent = e.message; }
  });
  const minutes = f.cert && f.cert.duration_minutes;
  const tile = (big, small) => h("div", { class: "tile" }, h("b", null, big), h("small", null, small));
  return h("section", { class: "panel hero" },
    stepHead(3, `What Dojo will build for ${f.code}`),
    h("div", { class: "tiles" },
      tile(`${f.skills} lessons`, "one for each skill"),
      tile("narration", "for Listen and your podcast"),
      tile("exams", minutes ? `timed, ${minutes} minutes, weighted like the blueprint` : "untimed: no exam duration found"),
      tile("checks", "three questions in three minutes")),
    h("p", { class: "note top" }, "Checking the official pages takes a few minutes. Then the lessons are written, checked and narrated in the background, one at a time, taking turns with your other exams. A skill with no verified official page is marked, not faked."),
    f.resume && kept ? clearBuild(kept, `An earlier build stopped after ${f.resume.checked} of ${plural(f.resume.skills || 0, "skill")}${f.resume.error ? `: ${f.resume.error}` : "."} Building again keeps the pages already checked.`) : null,
    h("h3", { class: "top" }, "What it costs"),
    h("p", { class: "small muted" }, "Writing and narration run by themselves once you confirm. Presenter videos are not in this price: they are filmed only when you press Film the lessons, which shows its own price."),
    estimateRows(f.estimate, "About, at list price"),
    f.estimate.complete ? null : h("p", { class: "conf bad top" }, icon("alert", "xs"),
      h("span", null, `Building costs more than ${usd(f.estimate.total)}. ${leftOut(f.estimate)}`)),
    h("div", { class: "actions top" }, go),
    msg);
}

function addedList(list) {
  if (!list.length) return null;
  const line = (x) => (x.added ? `${x.lessons.ready} of ${x.lessons.teachable} lessons ready`
    : x.state === "matching" ? `checking official pages: ${x.checked || 0} of ${x.skills || "?"} skills`
    : `stopped after ${x.checked || 0} of ${x.skills || "?"} skills`);
  return panel(eyebrow(null, "Exams you added"),
    list.map((x) => h("a", { class: "rv", href: `#/add-exam/${x.package}` },
      h("span", null, h("b", null, [x.exam, x.title].filter(Boolean).join(" · ")), h("small", null, line(x))), icon("right", "sm"))));
}

function addExamPage(f, list, token) {
  const bad = f && f.state === "error";
  const input = h("input", { type: "text", class: "codein", value: f ? f.code || "" : "", placeholder: "AZ-104", autocomplete: "off",
    spellcheck: "false", autocapitalize: "characters", maxlength: "40", "aria-label": "Exam code", "aria-describedby": "code-help", "aria-invalid": bad ? "true" : null });
  const help = h("p", { class: bad ? "small bad" : "small muted", id: "code-help" }, bad ? f.message : CODE_HELP);
  const form = h("form", { class: "codeform", role: "search", "aria-label": "Find an exam" },
    h("label", { class: "codebox" }, icon("search"), input),
    h("button", { class: "btn", type: "submit" }, "Find it"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const c = examCode(input.value);
    if (!c) {
      help.textContent = `That is not an exam code. ${CODE_HELP}`;
      help.className = "small bad";
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    const hash = `#/add-exam/${c.toLowerCase()}`;
    if (location.hash === hash) route(); else location.hash = hash;
  });
  const kept = f && list.find((x) => x.package === f.package && !x.added);
  const found = f && f.state === "found";
  const date = h("input", { type: "date", id: "exam-date" });
  const result = !f || bad ? null
    : f.state === "reading" ? h("div", { class: "found msg" }, h("p", { class: "loading" }, `Reading the study guide for ${f.code} on Microsoft Learn…`))
    : found ? foundCard(f) : lookupMessage(f, kept);
  const left = h("div", { class: "col grow" },
    h("section", { class: "panel" }, stepHead(1, "Which exam", "reads the study guides on Microsoft Learn"), form, help, result,
      f ? null : h("p", { class: "note top" }, "Dojo reads the skills measured and their weights, the date they changed, the length of the exam and whether an official practice assessment exists. You see all of it, and the price, before anything is built.")),
    found ? h("section", { class: "panel" }, stepHead(2, "Your exam date", "optional · change it later"),
      h("div", { class: "datefield" }, h("label", { class: "field", for: "exam-date" }, "Exam date", date),
        h("p", { class: "small muted" }, "Planned or booked. Leave it empty if you do not know yet."))) : null);
  const right = h("div", { class: "col w440" }, found ? buildPanel(f, date, kept, token) : null, addedList(list), howBuiltPanel(), rulesPanel());
  return [
    h("div", { class: "between top" },
      pageHead("Type an exam code. Dojo builds the course.", null, eyebrow(null, "Add an exam · Microsoft or GitHub certifications"), helpQ("add-an-exam")),
      linkButton([icon("x", "sm"), "Cancel"], "#/today", "ghost")),
    h("div", { class: "row stackable" }, left, right)];
}

async function viewAddExam(code) {
  const token = state.nav;
  const fresh = lookupFresh;
  lookupFresh = false;
  const list = await api("/api/exam-packages").then((r) => r.exams).catch(() => []);
  if (!code) return addExamPage(null, list, token);
  const row = list.find((x) => x.package === code);
  if (row && (row.added || row.state === "matching")) return examProgress(await api(`/api/exam-packages/${code}`));
  // Reading the study guide and the exam page takes a few seconds: say so while it happens.
  const box = h("div", { class: "col" }, addExamPage({ state: "reading", code: code.toUpperCase(), package: code }, list, token));
  api(`/api/exam-lookup?code=${encodeURIComponent(code)}${fresh ? "&fresh=true" : ""}`)
    .then(async (f) => {
      if (token !== state.nav) return;
      if (f.state === "building" || (f.state === "added" && !f.builtin)) {
        const st = await api(`/api/exam-packages/${f.package}`);
        if (token === state.nav) put(box, examProgress(st));
      } else put(box, addExamPage(f, list, token));
    })
    .catch((e) => { if (token === state.nav) put(box, addExamPage({ state: "error", code: code.toUpperCase(), message: e.message }, list, token)); });
  return box;
}

// Opening an added (or built-in) exam's course from this page.
async function openExam(pid) {
  if (pid !== state.pid) await choosePackage(pid);
  location.hash = "#/skills";
}

// Asks the server again every few seconds for as long as `onStatus` says there is more to see.
function follow(pid, token, every, onStatus) {
  (async () => {
    while (token === state.nav) {
      await sleep(document.hidden ? Math.max(every, 30000) : every);
      if (token !== state.nav) return;
      let st;
      try { st = await api(`/api/exam-packages/${pid}`); } catch (e) { if (e.status === 404 || e.signedOut) return; continue; }
      if (token !== state.nav || !onStatus(st)) return;
    }
  })();
}

function matchingPanel(st) {
  const m = st.matching || {};
  const skills = m.skills || 0, checked = m.checked || 0;
  return h("section", { class: "panel hero" },
    h("div", { class: "panel-h" }, eyebrow("search", "Checking the official pages"), chip("building", "amber")),
    h("p", { class: "bigline" }, `${checked} of ${plural(skills, "skill")} checked`),
    h("div", { class: "bar", role: "progressbar", "aria-label": "Skills checked", "aria-valuemin": "0", "aria-valuemax": String(skills), "aria-valuenow": String(checked) },
      wide(h("i", { class: "amber" }), `${skills ? Math.round((100 * checked) / skills) : 0}%`)),
    h("p", { class: "small muted top" }, (st.job && st.job.step) || "Starting…"),
    m.sourced ? h("p", { class: "small muted" }, `${plural(m.sourced, "skill")} with a verified official page so far.`) : null,
    h("p", { class: "note top" }, "For each skill, one AI picks official pages from Microsoft Learn or GitHub Docs, and a second AI must quote a sentence from the page that Dojo then finds there word for word. You can leave this page: the build goes on, and the exam appears in the exam switcher when it is ready."));
}

function lessonsPanel(st) {
  const L = st.lessons, P = st.preparing;
  const all = L.ready >= L.teachable;
  const mine = P.current && P.current.startsWith(`${st.package}/`);
  const now = all ? "Every lesson is written."
    : !P.running ? "Lesson preparation does not run in this copy of Dojo. A lesson is written when you open its skill."
    : mine ? "Writing one of its lessons now."
    : "Lessons are written and narrated in the background, one at a time, taking turns with your other exams.";
  return h("section", { class: "panel hero" },
    h("div", { class: "panel-h" }, eyebrow("course", "The course"), chip(all ? "all lessons ready" : "preparing", all ? "sage" : "amber")),
    h("p", { class: "bigline" }, all ? `All ${plural(L.teachable, "lesson")} ready` : `${L.ready} of ${L.teachable} lessons ready`),
    h("div", { class: "bar", role: "progressbar", "aria-label": "Lessons ready", "aria-valuemin": "0", "aria-valuemax": String(L.teachable), "aria-valuenow": String(L.ready) },
      wide(h("i", { class: "amber" }), `${L.teachable ? Math.round((100 * L.ready) / L.teachable) : 0}%`)),
    h("ul", { class: "facts top" },
      h("li", null, L.ready ? `${L.narrated} of ${L.ready} narrated for Listen and your podcast` : "Each lesson is narrated for Listen and your podcast once it is written"),
      h("li", null, `${L.filmed} filmed with presenters`),
      L.unsourced ? h("li", null, `${plural(L.unsourced, "skill")} of ${L.skills} without a verified official page: not taught, not asked, counted as not shown`) : null),
    st.no_exam ? h("p", { class: "note top" }, st.no_exam) : null,
    h("p", { class: "small muted top" }, now),
    h("div", { class: "actions top" }, button("Open the course", () => openExam(st.package), "sm")));
}

function sourcePanel(st) {
  const s = st.source || {}, p = st.practice;
  return panel(eyebrow("file", "Where it comes from"),
    h("div", { class: "src" }, icon("file", "sm"), h("div", null, ext(s.url, `Study guide for ${st.exam}`),
      h("div", { class: "snap" }, [s.skills_as_of ? `skills measured as of ${fmtAsOf(s.skills_as_of)}` : "the page gives no date",
        s.retrieved ? `read ${fmtDay(s.retrieved)}` : null].filter(Boolean).join(" · ")))),
    st.exam_page ? h("div", { class: "src" }, icon("link", "sm"), h("div", null, ext(st.exam_page, "Certification and exam page"),
      h("div", { class: "snap" }, st.duration_minutes ? `${st.duration_minutes} minutes for the real exam` : "no exam duration found"))) : null,
    h("div", { class: "src" }, icon("target", "sm"), p
      ? h("div", null, ext(p.url, "Official practice assessment"), h("div", { class: "snap" }, `opened and checked ${fmtDay(p.checked)}`))
      : h("div", null, h("b", null, "No official practice assessment"), h("div", { class: "snap" }, "Neither the study guide nor the exam page links one."))),
    h("p", { class: "small muted top" }, `Exam date: ${st.exam_date ? fmtDay(st.exam_date) : "not set"}.`),
    notesBlock(st.notes));
}

function filmPanel(st, onStatus, note) {
  const F = st.film, L = st.lessons;
  const parts = [L.filmed ? `${L.filmed} filmed` : null, F.filming ? `${F.filming} filming now` : null, F.waiting ? `${F.waiting} waiting` : null].filter(Boolean);
  const bits = [h("div", { class: "panel-h" }, eyebrow("video", "Presenter videos"), chip("only when you approve", "line")),
    h("p", { class: "sub" }, parts.length ? `Lessons with presenter video: ${parts.join(", ")}.` : "No lesson of this exam is filmed yet.")];
  if (F.offer) {
    const e = F.offer.estimate;
    const msg = h("p", { class: note ? "small bad" : "small", role: "status" }, note || "");
    const btn = button([icon("video", "sm"), `Film the lessons (${e.complete ? "about" : "more than"} ${usd(e.total)} at list price)`], async () => {
      btn.disabled = true;
      try {
        onStatus(await api(`/api/exam-packages/${st.package}/film`, { method: "POST", body: { confirm: "FILM", offer: F.offer.token } }));
      } catch (err) {
        if (err.status === 409) {
          try { onStatus(await api(`/api/exam-packages/${st.package}`), err.message); return; } catch (e2) { /* say what failed */ }
        }
        btn.disabled = false;
        msg.className = "small bad";
        msg.textContent = err.message;
      }
    }, "amber");
    bits.push(h("p", { class: "small ink2" }, `${plural(F.offer.lessons, "written lesson")} not filmed yet: about ${e.lines[0].quantity} minutes of video.`),
      h("div", { class: "top" }, estimateRows(e, "About, at list price")),
      h("div", { class: "actions top" }, btn), msg,
      h("p", { class: "small muted top" }, "Pressing it approves this price for exactly these lessons. Dojo films one lesson at a time, only when nothing else has run for a few minutes, and pauses before presenter videos fill 90% of their storage. Lessons written later need a new approval."));
  } else if (!F.waiting && !F.filming) {
    bits.push(h("p", { class: "small muted" }, L.ready ? "Every written lesson is filmed or approved." : "A lesson can be filmed once it is written."));
  }
  if ((F.waiting || F.filming) && F.note) bits.push(h("p", { class: "note top" }, F.note));
  if (F.failed.length) bits.push(h("p", { class: "conf top" }, icon("alert", "xs"),
    `Filming failed for ${plural(F.failed.length, "lesson")} (${F.failed[0].error}). A failed lesson needs a new approval.`));
  if (F.approvals.length) bits.push(h("div", { class: "top" }, F.approvals.slice().reverse().map((a) =>
    h("p", { class: "small muted" }, `You approved ${plural(a.lessons, "lesson")} for about ${usd(a.total)} on ${fmtDate(a.at)}.`))));
  bits.push(h("p", { class: "small muted top" }, `Presenter videos of all exams use ${gb(F.storage.bytes)} of ${gb(F.storage.budget_bytes)} of their storage.`));
  return h("section", { class: "panel" }, bits);
}

function removedPage(res) {
  return [pageHead(`${res.exam} is removed`, "Your record is kept: every answer and event stays.", eyebrow(null, "Add an exam")),
    panel(h("p", { class: "sub" }, `Dojo deleted ${plural(res.lessons, "lesson")}, ${plural(res.audio, "audio file")} and ${plural(res.videos, "presenter video")}.`),
      h("div", { class: "actions top" }, linkButton("Add an exam", "#/add-exam", "ghost"), linkButton("Today", "#/today")))];
}

// "Remove this exam" asks first. The Record is append-only, so every event about the exam stays.
function removePanel(st, token) {
  const box = h("section", { class: "panel" });
  const L = st.lessons || { ready: 0 };
  const msg = h("p", { class: "small", role: "status" });
  const buttons = () => box.querySelectorAll("button");
  const go = async () => {
    for (const b of buttons()) b.disabled = true;
    try {
      const res = await api(`/api/exam-packages/${st.package}/remove`, { method: "POST", body: { confirm: "REMOVE" } });
      await refreshMe().catch(() => null);
      if (token === state.nav) put($view(), removedPage(res));
    } catch (e) {
      for (const b of buttons()) b.disabled = false;
      msg.className = "small bad";
      msg.textContent = e.message;
    }
  };
  const ask = () => put(box, eyebrow("alert", `Remove ${st.exam}?`, "rose"),
    h("p", { class: "sub" }, `Dojo deletes the course for ${st.exam}: ${plural(L.ready, "lesson")} with their audio and presenter videos, and the practice questions waiting for it. Your record stays: every answer and event is kept.`),
    h("div", { class: "actions top" }, button(`Remove ${st.exam}`, go, "danger sm"), button("Keep it", draw, "ghost sm")), msg);
  const draw = () => put(box, eyebrow(null, "Remove this exam"),
    h("p", { class: "sub" }, "Removes this course and what was prepared for it. Your record stays."),
    h("div", { class: "actions top" }, button("Remove this exam", ask, "ghost sm")));
  draw();
  return box;
}

function examProgress(st) {
  const token = state.nav;
  const head = h("div", { class: "between top" },
    pageHead([st.exam, st.title].filter(Boolean).join(" · "),
      st.added ? `Added ${fmtWhen(st.added_at)} from the official study guide.` : "Dojo is building this course from the official study guide.",
      eyebrow(null, st.added ? "Added exam" : "Add an exam")),
    linkButton("Add another exam", "#/add-exam", "ghost sm"));
  if (!st.added && st.state === "matching") {
    const box = h("div", { class: "col" }, matchingPanel(st));
    follow(st.package, token, EXAM_POLL_MS, (s) => {
      if (s.state === "matching") { put(box, matchingPanel(s)); return true; }
      (s.added ? refreshMe() : Promise.resolve()).catch(() => null).then(() => { if (token === state.nav) route(); });
      return false;
    });
    return [head, h("div", { class: "row stackable" }, h("div", { class: "col grow" }, box), h("div", { class: "col w440" }, howBuiltPanel(), rulesPanel()))];
  }
  if (!st.added) {
    return [head, panel(eyebrow("alert", "The build stopped", "rose"), h("p", { class: "sub" }, st.matching.error || "It stopped before the course was put together."),
      h("div", { class: "actions top" }, button("Look at it again", () => route(), "sm")))];
  }
  const lessonsBox = h("div", { class: "col" }, lessonsPanel(st));
  const filmBox = h("div", { class: "col" });
  let filmKey = "";
  let following = false;
  const busy = (s) => (s.preparing.running && (s.lessons.ready < s.lessons.teachable || s.lessons.narrated < s.lessons.ready))
    || s.film.waiting > 0 || s.film.filming > 0;
  const show = (s, note) => {
    put(lessonsBox, lessonsPanel(s));
    const key = JSON.stringify([s.film, s.lessons]);
    if (key !== filmKey || note) {   // the film button is redrawn only when its price or lessons changed
      filmKey = key;
      put(filmBox, filmPanel(s, show, note));
    }
    watch(s);
  };
  const watch = (s) => {
    if (following || !busy(s)) return;
    following = true;
    follow(st.package, token, COURSE_POLL_MS, (x) => {
      if (!x.added) { following = false; route(); return false; }
      following = busy(x);
      show(x);
      return following;
    });
  };
  show(st);
  return [head, h("div", { class: "row stackable" },
    h("div", { class: "col grow" }, lessonsBox, filmBox),
    h("div", { class: "col w440" }, sourcePanel(st), removePanel(st, token)))];
}

// ---------------------------------------------------------------- hands-on Azure SQL labs

async function viewLab(id) {
  if (state.pid !== "dp-800") {
    state.pid = "dp-800";
    $("exam").value = "dp-800";
    renderHeader();
    renderSide();
  }
  const lab = await api(`/api/labs/${id}`);
  const status = h("p", { class: "lab-message", role: "status", "aria-live": "polite" }, lab.local
    ? "Local demo: no database. Checks here cannot verify work." : "Authorized live-write sandbox. Only lab schema objects may be changed.");
  const input = h("textarea", { id: "lab-sql", class: "lab-sql", "aria-label": "SQL editor", spellcheck: "false", rows: 14 }, lab.starter);
  const output = h("div", { class: "lab-output" }, h("p", { class: "small muted" }, "Run a query to see results."));
  const check = h("div", { class: "lab-check" }, h("p", { class: "small muted" }, "Dojo checks the database, not a screenshot. No lab fills a pip."));
  const hints = h("div", { class: "lab-hints" });
  const setBusy = (busy) => { for (const b of controls.querySelectorAll("button")) b.disabled = busy; };
  const call = async (kind, body) => {
    setBusy(true);
    status.textContent = lab.local ? "Running the local demo…" : "Waking the database, about a minute. Please wait…";
    const deadline = Date.now() + 120000;
    try {
      let r;
      while (true) {
        try {
          r = await api(`/api/labs/${id}/${kind}`, { method: "POST", body });
          break;
        } catch (e) {
          if (lab.local || e.status !== 503 || !e.message.startsWith("Waking the database")) throw e;
          if (Date.now() + 10000 >= deadline) throw new Error("Could not reach the database after about two minutes. Try again.");
          status.textContent = "Waking the database, about a minute. Trying again…";
          await sleep(10000);
        }
      }
      status.textContent = r.message || "Done.";
      if (kind === "run") {
        const table = h("table", { class: "lab-table" },
          h("thead", null, h("tr", null, r.columns.map((x) => h("th", null, x)))),
          h("tbody", null, r.rows.map((row) => h("tr", null, row.map((v) => h("td", null, v == null ? "NULL" : v))))));
        put(output, r.columns.length ? h("div", { class: "lab-scroll" }, table)
          : h("p", { class: "small muted" }, "SQL completed. No result set."),
        r.truncated ? h("p", { class: "small muted" }, `Showing only the first ${lab.limits.rows} rows.`) : null);
      }
      if (kind === "check") put(check, h("h3", null, lab.local ? (r.passed ? "Demo pattern matched" : "Demo pattern not matched") : r.passed ? "Verified in Azure SQL" : "Not yet"),
        h("p", null, r.message), r.conditions ? h("p", { class: "small muted" },
          `${r.conditions.runs} runs · ${r.conditions.hints_shown} hints shown · ${r.conditions.coach_answers} coach answers. Practice only: no pip.`) : null);
      if (kind === "reset") put(output, h("p", { class: "small muted" }, "This lab is back at its starting state."), h("p", { class: "small muted" }, "You can run SQL again."));
    } catch (e) {
      status.textContent = e.message;
    } finally { setBusy(false); }
  };
  const reveal = async (index) => {
    setBusy(true);
    try {
      const r = await api(`/api/labs/${id}/hint`, { method: "POST", body: { index } });
      hints.append(h("p", { class: "note" }, r.hint));
      hintButtons[index].remove();
      status.textContent = "Hint shown. This is practice with help.";
    } catch (e) { status.textContent = e.message; }
    finally { setBusy(false); }
  };
  const hintButtons = Array.from({ length: lab.hint_count }, (_, index) => button(`Show hint ${index + 1}`, () => reveal(index), "ghost sm"));
  const reset = button("Reset this lab", () => {
    if (!window.confirm(`Reset ${lab.schema}? Everything you made in this lab goes and the starting tables come back. Nothing outside ${lab.schema} is touched. This cannot be undone.`)) return;
    call("reset");
  }, "ghost sm");
  const controls = h("div", { class: "actions top" }, button("Run SQL", () => {
    const destructive = /\b(DROP|TRUNCATE|DELETE|ALTER)\b/i.test(input.value);
    if (destructive && !window.confirm(`This SQL may change or remove objects in ${lab.schema}. Run it?`)) return;
    call("run", { sql: input.value, confirm_destructive: destructive });
  }, "amber"),
    button("Check my work", () => call("check"), "ghost"), reset);
  return [h("nav", { class: "crumb" }, h("a", { href: "#/skills" }, "Course"), " › ",
    h("a", { href: `#/skill/dp-800/${lab.skill}` }, "DP-800 skill"), " › Lab"),
    pageHead(lab.title, lab.goal, eyebrow("target", "Lab · DP-800 · Azure SQL"), helpQ("labs-dp-800")),
    driftNote(lab.drift),
    h("div", { class: "lab-boundary" }, h("b", null, lab.local ? "Synthetic local demo" : "Authorized live-write sandbox"),
      h("span", null, lab.local ? `Nothing reaches Azure SQL. Your schema would be ${lab.schema}.` : `Private Azure SQL · your own schema ${lab.schema} only · no learner record in SQL · database auto-pauses at the free monthly limit.`
        + (lab.shared ? " Members share one database, so one query runs at a time and the free monthly allowance is shared." : ""))),
    h("div", { class: "lab-layout" },
      panel(eyebrow("course", "Lab steps"), h("p", { class: "small" }, lab.why),
        h("ol", { class: "lab-steps" }, lab.steps.map((s) => h("li", null, s))),
        h("div", { class: "divider" }), h("p", { class: "small" }, "Opening a hint counts as help. Docs you open yourself are allowed."),
        h("div", { class: "actions" }, hintButtons), hints,
        h("h3", null, "Official docs"), lab.sources.map((s) => {
          const changed = pageChange(lab.drift, s.url);
          return h("p", null, ext(s.url, s.title), changed ? [" ", changed] : null);
        })),
      h("section", { class: "panel lab-editor" }, h("label", { for: "lab-sql" }, "SQL · one batch, 8 KB max, 20 s timeout"), input,
        controls, status, h("h3", null, "Results"), output),
      panel(eyebrow("check", "Checker"), check,
        h("p", { class: "small muted" }, "The app sets up and checks as its managed identity. Your SQL runs on a fresh, unpooled connection as your own lab runner, which reaches only your schema for this lab. Write table names plain (Products) or with your schema. Outside help cannot be observed. Technical errors are never misses.")))];
}

// ---------------------------------------------------------------- Play (ADR 0010): the personal layer

/* Private to the learner and off until they turn it on. Everything shown is worked out from the record on
   each visit: a shelf of moments, "weeks met" against a weekly target of study days, and study points for
   effort. Right or wrong earns the same. There is no run to keep, nothing to lose and nobody to compare with. */
const PLAY_PRIVATE = "Private to you. Nobody else sees your shelf, your weeks or your points in Dojo.";
const PLAY_DAY = "A day counts when it holds one answer in your own words, five quick questions, a practice exam of 10 or more questions at 5 seconds or more each, or a lab check. Lessons teach, so they do not count a day.";
const PLAY_POINTS = "Right or wrong earns the same. Points are for effort this week, start again each Monday, and say nothing about the exam.";
const POINT_PARTS = [["study_day", "Study days", "10 once a day"], ["answers", "Answers", "3 in your own words, 1 multiple choice · up to 30 a day"],
  ["lessons", "Lessons", "2 a view · up to 5 a day"], ["labs", "Labs", "10 a lab checked · up to 3 a week"]];

const playApi = (path, method, body) => api(`/api/play${path}`, { method, body });

function weekDots(w) {
  const n = Math.max(w.needed, w.days);
  return h("span", { class: "pl-dots", role: "img", "aria-label": `${w.days} of ${w.needed} study days this week` },
    Array.from({ length: n }, (_, i) => h("i", { class: i < w.days ? "on" : null })));
}

function weekLine(r) {
  const w = r.this_week;
  return h("div", { class: "pl-line" }, weekDots(w),
    h("span", null, `This week: ${w.days} of ${w.needed} day${w.needed === 1 ? "" : "s"}${w.met ? " · met" : ""}`),
    r.studied_today ? chip("today counts", "sage") : null);
}

function weeksMet(r) {
  return h("div", { class: "pl-big" }, h("b", null, String(r.weeks_met)), h("span", null, r.weeks_met === 1 ? "week met" : "weeks met"));
}

// Today: the "Your week" card, or a one-time offer that can be dismissed for good.
function playCard(pl) {
  if (!pl) return null;
  if (!pl.on) {
    if (!pl.offer) return null;
    return h("section", { class: "panel" },
      h("div", { class: "panel-h" }, eyebrow("flag", "Your week")),
      h("p", { class: "sub" }, "Pick 3 to 6 study days a week, see study points for effort, and keep a shelf of what you have shown. Private to you, and off until you turn it on."),
      h("div", { class: "actions top" }, linkButton("Have a look", "#/play", "ghost sm"),
        button("Not now", async () => { try { await playApi("/offer/dismiss", "POST"); } catch (e) { /* the card just stays */ } route(); }, "ghost sm")));
  }
  const r = pl.rhythm && pl.rhythm.on ? pl.rhythm : null;
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("flag", "Your week"), h("a", { class: "link small", href: "#/play" }, "Open")),
    r ? h("div", { class: "pl-week" }, weeksMet(r), weekLine(r)) : null,
    h("p", { class: "prow" }, icon("sparkle", "xs"), h("span", null, h("b", null, `${pl.points.total}`), " study points this week")),
    pl.shelf_hint && pl.moments.length
      ? h("a", { class: "rv", href: "#/play" }, h("span", null, h("b", null, `Your shelf: ${plural(pl.moments.length, "moment")}`),
          h("small", null, "What you have shown, to come back to.")), icon("right", "sm"))
      : null);
}

// The Record page: a short view of the shelf, or where to turn Play on.
function shelfPanel(pl) {
  if (!pl) return null;
  if (!pl.on) {
    return h("section", { class: "panel" }, eyebrow("flag", "Your shelf"),
      h("p", { class: "sub" }, "Play can keep a shelf of moments from your record: a skill shown on your own later or in a new situation, a skill relearned, a domain or exam where every skill is relearned, and passes you report. Private to you."),
      h("div", { class: "actions top" }, linkButton("Have a look", "#/play", "ghost sm")));
  }
  return h("section", { class: "panel" },
    h("div", { class: "panel-h" }, eyebrow("flag", "Your shelf"), h("a", { class: "link small", href: "#/play" }, "Open")),
    pl.moments.length
      ? pl.moments.slice(0, 3).map((m) => h("a", { class: "rv pl-rv", href: "#/play" },
          h("span", null, h("b", null, `${m.title} · ${m.exam}`), h("small", null, `${m.skill_text ? m.skill_text + " · " : ""}${momentDate(m)}`),
            h("small", null, m.made_by), h("small", { class: "muted" }, m.line)),
          m.refresh ? chip("worth a refresh", "amber") : null))
      : h("p", { class: "sub" }, "Nothing on your shelf yet."),
    pl.moments.length > 3 ? h("p", { class: "small muted top" }, `and ${plural(pl.moments.length - 3, "more moment")}`) : null);
}

// Under the feedback of an answer not yet met: a quiet way back to what has been shown.
function shelfLink() {
  const box = h("p", { class: "small muted" });
  api("/api/play").then((pl) => {
    if (pl && pl.on && pl.moments.length) put(box, h("a", { class: "link", href: "#/play" }, `Your shelf: ${plural(pl.moments.length, "moment")}`));
  }).catch(() => {});
  return box;
}

const momentDate = (m) => (m.kind === "passed" ? fmtDay(m.at) : fmtWhen(m.at));

function momentCard(m, onHide, share) {
  return h("article", { class: "pl-moment" },
    h("div", { class: "between top" },
      h("div", null, h("p", { class: "eyebrow" }, icon(m.kind === "passed" ? "flag" : m.kind === "changed" ? "refresh" : m.kind === "exam" || m.kind === "domain" ? "sparkle" : "check", "xs"), m.title),
        m.skill_text ? h("h3", null, m.skill_text) : h("h3", null, m.exam)),
      h("span", { class: "small muted nowrap" }, momentDate(m))),
    m.domain ? h("p", { class: "small muted" }, m.domain) : null,
    h("p", { class: "small ink2" }, m.made_by),
    m.refresh ? h("p", { class: "prow" }, chip("worth a refresh", "amber"), h("span", null, m.refresh)) : null,
    h("div", { class: "between" }, h("span", { class: "small muted" }, m.line),
      button("Hide", onHide, "ghost sm")),
    share || null);
}

async function viewPlay() {
  const pl = await api("/api/play");
  if (!pl.on) return playOff();
  // Circles, the board and duels answer 404 unless team mode is on and the owner switched them on (ADR 0010 P-1).
  const [soc, board, duels] = await Promise.all(["/api/play/social", "/api/play/board", "/api/play/duels"].map((u) => api(u).catch(() => null)));
  return playOn(pl, soc, board, duels);
}

function playOff() {
  const status = h("p", { class: "small muted" });
  const choice = (value, label, checked) => h("label", null, h("input", { type: "radio", name: "pl-days", value, checked }), h("span", null, label));
  const days = h("fieldset", { class: "pl-days" }, h("legend", null, "Study days a week"),
    choice("3", "3", true), choice("4", "4"), choice("5", "5"), choice("6", "6"), choice("", "Not now"));
  const include = h("input", { type: "checkbox", id: "pl-include" });
  const go = button("Turn Play on", async () => {
    const picked = days.querySelector("input:checked");
    go.disabled = true;
    try {
      await playApi("/join", "POST", { include_record: include.checked, days: picked && picked.value ? Number(picked.value) : null });
      route();
    } catch (e) { status.textContent = e.message; go.disabled = false; }
  });
  const what = (ic, title, text) => h("div", { class: "pl-what" }, h("span", { class: "picon" }, icon(ic, "sm")), h("div", null, h("b", null, title), h("p", { class: "small ink2" }, text)));
  return [pageHead("Play", "Effort, marked for you. Off until you turn it on, and you can leave at any time.", null, helpQ("play")),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow" },
        panel(eyebrow("flag", "What it is"),
          what("flag", "Your week", `Pick 3, 4, 5 or 6 study days a week; which days does not matter. ${PLAY_DAY} The only number is weeks met. It never goes down: there is nothing to keep going and nothing to lose.`),
          what("sparkle", "Study points", `Points for this week's effort: study days, answers, lessons and labs, with daily and weekly limits. ${PLAY_POINTS} They are not a level.`),
          what("check", "Your shelf", "Moments from your record: a skill shown on your own a day later or in a new situation, a skill relearned through spaced recalls, a domain or exam where every skill is, and passes you report. From Dojo's own questions; not a certification. Something to come back to on a hard week.")),
        panel(eyebrow("shield", "Who sees it"),
          h("p", { class: "prow" }, icon("lock", "xs"), h("span", null, h("b", null, "In the app: "), "only you. No page in Dojo shows your shelf, your weeks or your points to anyone else, and nothing compares you with anyone.")),
          h("p", { class: "prow" }, icon("record", "xs"), h("span", null, h("b", null, "What is kept: "), "when you turned Play on, your weekly target and the moments you hid. Weeks, points and moments are worked out from your record each time and are not stored. A pass you report is written to your record, with the exam and the date only.")),
          h("p", { class: "prow" }, icon("eye", "xs"), h("span", null, h("b", null, "Outside the app: "), "Play's settings sit with your record in Dojo's storage. Whoever operates this Dojo can technically read that storage, as for the rest of your record. ", h("a", { class: "link", href: "#/about" }, "How Dojo works"))),
          h("p", { class: "prow" }, icon("x", "xs"), h("span", null, "Leave Play deletes its settings at once. Your record stays as it is. Dojo data is never used for performance reviews, staffing or HR.")))),
      h("div", { class: "col w430" },
        panel(eyebrow("plan", "Turn it on"), days,
          h("label", { class: "pl-check", for: "pl-include" }, include,
            h("span", null, h("b", null, "Include my Record so far"), h("small", null, "Bring in the moments and weeks already in your record. Otherwise Play starts from now."))),
          h("div", { class: "actions top" }, go), status)))];
}

function playOn(pl, soc, board, duels) {
  const r = pl.rhythm;
  const p = pl.points;
  const status = h("p", { class: "small muted" });
  const run = (fn) => async () => { try { await fn(); route(); } catch (e) { status.textContent = e.message; } };
  const top = Math.max(...p.weeks.map((w) => w.total), 1);
  const weeks = h("div", { class: "pl-weeks", role: "img", "aria-label": "Study points in the last weeks" },
    p.weeks.map((w) => h("div", { class: w.week === p.week ? "pl-wk now" : "pl-wk" },
      h("span", { class: "n" }, String(w.total)),
      h("span", { class: "col-bar" }, h("i", null)),
      h("span", { class: "d" }, fmtDay(w.week).replace(/,? \d{4}$/, "")))));
  weeks.querySelectorAll(".pl-wk i").forEach((el, i) => el.style.setProperty("height", `${Math.round((p.weeks[i].total * 100) / top)}%`));
  const byExam = new Map();
  for (const m of pl.moments) { if (!byExam.has(m.exam)) byExam.set(m.exam, []); byExam.get(m.exam).push(m); }
  const target = h("select", { id: "pl-target", "aria-label": "Study days a week" },
    pl.targets.map((n) => h("option", { value: String(n), selected: r && (r.next_target || r.target) === n }, `${n} days a week`)));
  const passExam = h("select", { id: "pl-pass-exam" }, myExams().map((x) => h("option", { value: x.id, selected: x.id === state.pid }, `${x.exam} · ${x.title}`)));
  const passDay = h("input", { type: "date", id: "pl-pass-day", max: pl.today, value: pl.today });
  const passStatus = h("p", { class: "small muted" });
  const report = button("Add to my shelf", async () => {
    try { await api("/api/passes", { method: "POST", body: { package: passExam.value, earned: passDay.value } }); route(); }
    catch (e) { passStatus.textContent = e.message; }
  }, "ghost");
  return [pageHead("Play", PLAY_PRIVATE, null, helpQ("play")),
    h("div", { class: "row stackable grow" },
      h("div", { class: "col grow" },
        r && r.on
          ? panel(h("div", { class: "panel-h" }, eyebrow("flag", "Your week"), chip(`${r.target} days a week`, "line")),
              h("div", { class: "pl-week" }, weeksMet(r), weekLine(r)),
              r.next_target ? h("p", { class: "small muted top" }, `From Monday: ${r.next_target} days a week.`) : null,
              h("p", { class: "note top" }, PLAY_DAY))
          : panel(eyebrow("flag", "Your week"), h("p", { class: "sub" }, "No weekly target. Pick one under Settings when you want one.")),
        panel(h("div", { class: "panel-h" }, eyebrow("sparkle", "Study points this week"), h("b", { class: "pl-pts" }, String(p.total))),
          POINT_PARTS.map(([k, label, rule]) => h("dl", { class: "mrow" }, h("dt", null, label), h("dd", null, h("b", null, String(p.by_kind[k])), h("span", { class: "small muted" }, ` · ${rule}`)))),
          p.weeks.length > 1 ? weeks : null,
          h("p", { class: "note top" }, `${PLAY_POINTS} At most ${p.caps.day} a day and ${p.caps.week} a week.`)),
        panel(h("div", { class: "panel-h" }, eyebrow("check", "Your shelf"), pl.moments.length ? chip(plural(pl.moments.length, "moment"), "line") : null),
          pl.moments.length
            ? [...byExam].map(([exam, ms]) => h("div", { class: "pl-group" }, h("h3", null, exam),
                ms.map((m) => momentCard(m, run(() => playApi(`/moments/${m.id}`, "DELETE")), soc && soc.opted ? shareBox(soc, m) : null))))
            : h("p", { class: "sub" }, pl.since
                ? `Nothing on your shelf since you turned Play on (${fmtWhen(pl.since)}). A moment is marked when a skill is shown on your own a day after the teaching or in a new situation, or relearned through spaced recalls.`
                : "Nothing on your shelf yet. A moment is marked when a skill is shown on your own a day after the teaching or in a new situation, or relearned through spaced recalls."),
          pl.hidden ? h("p", { class: "small muted top" }, `${plural(pl.hidden, "moment")} hidden.`) : null),
        circlesPanels(soc), boardPanel(board), duelsPanel(duels)),
      h("div", { class: "col w430" },
        panel(eyebrow("flag", "Report a pass"),
          h("p", { class: "sub" }, "Passed a certification exam? Put it on your shelf. Only the exam and the day are kept, never a score."),
          h("label", { class: "field top", for: "pl-pass-exam" }, "Exam", passExam),
          h("div", { class: "datefield" }, h("label", { class: "field", for: "pl-pass-day" }, "The day you passed", passDay), report),
          h("p", { class: "small muted" }, "Self-reported: Dojo does not check it."), passStatus),
        panel(eyebrow("plan", "Settings"),
          h("div", { class: "datefield" }, h("label", { class: "field", for: "pl-target" }, "Weekly target", target),
            button("Save", run(() => playApi("/settings", "PUT", { days: Number(target.value), rhythm_on: true })), "ghost sm")),
          h("p", { class: "small muted" }, r && r.on ? "A change applies from next Monday." : "Saving turns the weekly target on."),
          r && r.on ? h("div", { class: "actions top" }, button("Turn the weekly target off", run(() => playApi("/settings", "PUT", { rhythm_on: false })), "ghost sm")) : null,
          pl.hidden ? h("div", { class: "actions top" }, button(`Show ${plural(pl.hidden, "hidden moment")} again`, run(() => playApi("/settings", "PUT", { show_hidden: true })), "ghost sm")) : null,
          status),
        circleSettings(soc),
        panel(eyebrow("x", "Leave Play"),
          h("p", { class: "sub" }, `Deletes Play's settings at once: your target and the moments you hid${soc && soc.opted ? ", and your circles: your memberships, what you shared, your invitations, your place on the board and your duels" : ""}. Your record stays as it is, and a reported pass stays in it. Turning Play on again starts afresh.`),
          h("div", { class: "actions top" }, button("Leave Play", async () => { if (!window.confirm(`Leave Play? Your settings, hidden moments and shelf choices are deleted${soc && soc.opted ? ", and you leave every circle, the board and your duels" : ""}. Your Record stays as it is.`)) return; await run(() => playApi("/leave", "POST"))(); }, "danger")))))];
}

// ---------------------------------------------------------------- Play: circles (ADR 0010 §4 and §7, team mode only)

// Errors show next to the button that caused them; success re-renders the page.
const runAt = (st) => (fn) => async () => { try { await fn(); route(); } catch (e) { st.textContent = e.message; } };

function peoplePicker(people, idp) {
  const rows = people.map((p, i) => {
    const cb = h("input", { type: "checkbox", id: `${idp}-${i}`, value: p.id });
    return { cb, row: h("label", { class: "pl-person", for: `${idp}-${i}` }, cb, h("span", null, p.name), p.label ? chip(p.label, "line") : null) };
  });
  return {
    node: rows.length ? h("div", { class: "pl-people" }, rows.map((x) => x.row))
      : h("p", { class: "small muted" }, "Nobody to invite yet: people appear here once they join circles and turn on \u201cOthers can invite me\u201d."),
    picked: () => rows.filter((x) => x.cb.checked).map((x) => x.cb.value),
  };
}

function noticeParts(n) {
  const part = (title, items) => h("div", { class: "pl-notice" }, h("h3", null, title),
    items.map((x) => h("p", { class: "small ink2" }, h("b", null, `${x.title}. `), x.text)));
  return [part("In the app", n.in_app), part("Outside the app", n.outside)];
}

function circleBar(b) {
  if (b.state !== "shown") return h("p", { class: "small muted" }, b.text);
  const said = `${b.band}% of this week's goal of ${b.goal} study points`;
  return [h("div", { class: "pl-bands", role: "img", "aria-label": said }, [25, 50, 75, 100].map((x) => h("i", { class: b.band >= x ? "on" : null }))),
    h("p", { class: "small ink2" }, said),
    b.met ? h("p", { class: "prow" }, icon("sparkle", "xs"), h("b", null, b.text)) : null,
    h("p", { class: "small muted" }, "Steps of 25%, at most once a day. It never shows who did what.")];
}

function sharedCopy(s, run) {
  return h("div", { class: "pl-shared" }, h("p", null, s.text), s.line ? h("small", { class: "muted" }, s.line) : null,
    s.mine ? h("div", { class: "actions" }, button("Withdraw", run(() => playApi(`/shares/${s.id}`, "DELETE")), "ghost sm")) : null);
}

function circleCard(c, soc) {
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  const goal = h("input", { type: "number", id: `pl-goal-${c.id}`, min: c.goal_range[0], max: c.goal_range[1], step: "10", value: c.next_goal });
  const free = soc.people.filter((p) => !c.members.some((m) => m.id === p.id));
  let invite = null;
  if (c.can_invite && free.length) {
    const pk = peoplePicker(free, `pl-inv-${c.id}`);
    invite = h("details", { class: "how" }, h("summary", null, "Invite more"), pk.node,
      h("div", { class: "actions top" }, button("Invite", run(() => playApi(`/circles/${c.id}/invite`, "POST", { people: pk.picked() })), "ghost sm")),
      h("p", { class: "small muted" }, "You see who joins, never who declined or let an invitation lapse."));
  }
  return panel(h("div", { class: "panel-h" }, eyebrow("people", c.name), chip(plural(c.size, "member"), "line")),
    h("ul", { class: "pl-members" }, c.members.map((m) => h("li", null, h("span", null, m.name), m.you ? chip("you", "line") : null, m.label ? chip(m.label, "sage") : null))),
    h("div", { class: "pl-group" }, h("h3", null, "This week"), circleBar(c.bar)),
    h("div", { class: "datefield top" }, h("label", { class: "field", for: `pl-goal-${c.id}` }, "Next week's goal (study points)", goal),
      button("Save", run(() => playApi(`/circles/${c.id}/goal`, "PUT", { points: Number(goal.value) })), "ghost sm")),
    h("p", { class: "small muted" }, `${c.goal_range[0]} to ${c.goal_range[1]}: at least ${soc.limits.goal_per_member} per member. Fixed from Monday for the whole week; nobody sees who set it.`),
    h("div", { class: "pl-group" }, h("h3", null, "Shared here"),
      c.shared.length ? c.shared.map((s) => sharedCopy(s, run))
        : h("p", { class: "small muted" }, c.can_share ? "Nothing shared yet. Share a moment or a pass from your shelf when you want to." : `Sharing starts when the circle has ${soc.limits.min} members.`)),
    invite,
    h("div", { class: "actions top" }, button("Leave this circle", async () => {
      if (!window.confirm(`Leave \u201c${c.name}\u201d? What you shared there goes with you.`)) return;
      await run(() => playApi(`/circles/${c.id}/leave`, "POST"))();
    }, "ghost sm")), st);
}

function circleStart(soc) {
  const st = h("p", { class: "small muted" });
  if (soc.owner) return panel(eyebrow("people", "Start a circle"),
    h("p", { class: "sub" }, "You run this Dojo, so you do not start circles or invite anyone. You are in a circle when a member invites you, and there you are shown as \u201cruns this Dojo\u201d."));
  if (!soc.can_start) return panel(eyebrow("people", "Start a circle"), h("p", { class: "sub" }, `You are in ${soc.limits.each} circles, the most there can be. Leave one to start another.`));
  const name = h("input", { type: "text", id: "pl-cname", maxlength: "40", placeholder: "For example: GH-300 Fridays" });
  const pk = peoplePicker(soc.people, "pl-new");
  return panel(eyebrow("people", "Start a circle"),
    h("p", { class: "sub" }, `Invite ${soc.limits.min - 1} to ${soc.limits.max - 1} people who opted in. A circle has ${soc.limits.min} to ${soc.limits.max} members; the shared bar starts at ${soc.limits.bar}.`),
    h("label", { class: "field top", for: "pl-cname" }, "Name", name), pk.node,
    h("div", { class: "actions top" }, button("Start the circle", runAt(st)(() => playApi("/circles", "POST", { name: name.value, people: pk.picked() })))),
    h("p", { class: "small muted" }, `You see who joins, never who declined or let it lapse (invitations last ${soc.limits.invite_days} days). A circle that stays under ${soc.limits.min} members goes after 7 days.`), st);
}

function circlesPanels(soc) {
  if (!soc) return null;
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  if (!soc.opted) {
    const inv = h("input", { type: "checkbox", id: "pl-invitable" });
    return panel(eyebrow("people", "Circles"),
      h("p", { class: "sub" }, "Study alongside a few colleagues who chose each other: a shared weekly goal, and a place to share a moment or a pass when you want to. Once you have joined, you can also join the effort board and accept or send duels, each only if you choose to. Off unless you join. Please read this first."),
      noticeParts(soc.notice),
      h("label", { class: "pl-check", for: "pl-invitable" }, inv,
        h("span", null, h("b", null, "Others can invite me"), h("small", null, "People who joined circles see your display name in the list of people they can invite to a circle or a duel. Off unless you tick it."))),
      h("div", { class: "actions top" }, button("Join circles", run(() => playApi("/social/join", "POST", { version: soc.notice.version, invitable: inv.checked })))), st);
  }
  const out = [];
  if (soc.invitations.length) {
    out.push(panel(eyebrow("hand", "Invitations"),
      soc.invitations.map((i) => h("div", { class: "qentry" }, h("div", { class: "qh" }, h("b", null, i.circle)),
        h("p", { class: "small ink2" }, `With ${i.members.join(", ")}`),
        h("div", { class: "actions top" }, button("Join", run(() => playApi(`/invitations/${i.id}/accept`, "POST")), "sm"),
          button("Decline", run(() => playApi(`/invitations/${i.id}/decline`, "POST")), "ghost sm")))),
      h("p", { class: "small muted top" }, "Declining is recorded nowhere and nobody is told."), st));
  }
  for (const c of soc.circles) out.push(circleCard(c, soc));
  out.push(circleStart(soc));
  return out;
}

function circleSettings(soc) {
  if (!soc || !soc.opted) return null;
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  const inv = h("input", { type: "checkbox", id: "pl-invitable-on", checked: soc.invitable });
  inv.addEventListener("change", run(() => playApi("/social", "PUT", { invitable: inv.checked })));
  return panel(eyebrow("people", "Circles"),
    h("p", { class: "sub" }, `In circles, on the board and in duels you are \u201c${soc.me.name}\u201d${soc.me.label ? ` (${soc.me.label})` : ""}: the display name you chose, never your account name.`),
    h("label", { class: "pl-check", for: "pl-invitable-on" }, inv,
      h("span", null, h("b", null, "Others can invite me"), h("small", null, soc.owner
        ? "Your display name is in the list of people others in circles can invite to a circle or a duel. You never invite anyone yourself."
        : "Your display name is in the list of people others in circles can invite to a circle or a duel."))),
    h("details", { class: "how" }, h("summary", null, "Read the notice again"), noticeParts(soc.notice)),
    h("div", { class: "actions top" }, button("Leave circles", async () => {
      if (!window.confirm("Leave circles? Your memberships, what you shared, your invitations, your place on the board and your duels are deleted at once. Your shelf stays.")) return;
      await run(() => playApi("/social/leave", "POST"))();
    }, "ghost sm")), st);
}

// The share box under a moment: one circle at a time, with a preview of exactly what the circle will see.
function shareBox(soc, m) {
  const box = h("div", { class: "pl-share" });
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  for (const c of soc.circles) {
    const s = c.shared.find((x) => x.mine && x.moment === m.id);
    if (s) {
      put(box, h("div", { class: "between" }, h("span", { class: "prow small" }, icon("share", "xs"), h("span", null, `Shared with \u201c${c.name}\u201d`)),
        button("Withdraw", run(() => playApi(`/shares/${s.id}`, "DELETE")), "ghost sm")), st);
      return box;
    }
  }
  const open = soc.circles.filter((c) => c.can_share);
  if (!open.length) return null;
  const start = h("div", { class: "actions" }, button("Share with a circle", () => {
    const pick = h("select", { "aria-label": "Circle" }, open.map((c) => h("option", { value: c.id }, c.name)));
    const preview = h("div", { class: "pl-preview", "aria-live": "polite" });
    // Share stays off until the preview of the circle now selected has arrived, and shares to exactly that
    // circle; a slower answer for an earlier choice is dropped (EG-25: you see what you share).
    let token = 0;
    let shown = null;
    const go = button("Share", run(() => (shown ? playApi(`/circles/${shown}/share`, "POST", { moment: m.id }) : Promise.reject(new Error("Wait for the preview.")))), "sm");
    const show = async () => {
      const mine = ++token;
      const cid = pick.value;
      shown = null;
      go.disabled = true;
      put(preview, h("p", { class: "small muted" }, "Preparing the preview\u2026"));
      try {
        const p = await playApi(`/circles/${cid}/preview`, "POST", { moment: m.id });
        if (mine !== token) return;
        put(preview, h("p", { class: "small muted" }, `What \u201c${p.circle}\u201d will see, for ${p.days / 7} weeks unless you withdraw it:`),
          h("blockquote", { class: "quote" }, p.text, p.line ? h("footer", null, p.line) : null));
        shown = cid;
        go.disabled = false;
      } catch (e) { if (mine === token) put(preview, h("p", { class: "small muted" }, e.message)); }
    };
    pick.addEventListener("change", show);
    put(box, h("label", { class: "field" }, "Share with", pick), preview,
      h("div", { class: "actions" }, go, button("Cancel", () => { token++; put(box, start); }, "ghost sm")), st);
    show();
  }, "ghost sm"));
  put(box, start);
  return box;
}

// ---------------------------------------------------------------- Play: the effort board and duels (ADR 0010 §5 and §6)

// Weekly bands of effort with no order: your own points and band, and names only inside your band.
function boardPanel(b) {
  if (!b || !b.opted) return null;
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  const head = eyebrow("target", "The effort board");
  if (b.owner) return panel(head, h("p", { class: "sub" }, b.text));
  const bands = h("ul", { class: "pl-bandlist" }, b.bands.map((x) => h("li", { class: b.me && b.me.band.name === x.name ? "on" : null },
    h("b", null, x.name), h("span", { class: "small muted" }, `${x.low}\u2013${x.high}`))));
  const about = h("p", { class: "small muted" }, `Once a week, on Monday, last week's study points go into four bands. You see your own points and your band, and the names of the others in your band only, alphabetically, when at least ${b.names_min} others share it. No order, no positions, nobody else's points. Shown once ${b.min} people have joined.`);
  if (!b.joined) {
    return panel(head, h("p", { class: "sub" }, "A weekly look at effort, not results: who studied about as much as you did last week. Off unless you join."),
      bands, b.text ? h("p", { class: "small ink2 top" }, b.text) : null, about,
      h("div", { class: "actions top" }, button("Join the board", run(() => playApi("/board/join", "POST")), "ghost sm")), st);
  }
  const mine = h("input", { type: "checkbox", id: "pl-mine-only", checked: b.mine_only });
  mine.addEventListener("change", run(() => playApi("/board", "PUT", { mine_only: mine.checked })));
  let body;
  if (b.state === "shown") {
    const others = b.names
      ? h("p", { class: "small ink2" }, `Also in \u201c${b.me.band.name}\u201d: ${b.names.join(", ")}.`)
      : b.others !== undefined ? h("p", { class: "small ink2" }, b.others ? `You and ${plural(b.others, "other")}.` : "Nobody else was in this band last week.") : null;
    body = [h("div", { class: "between" }, h("span", null, `Your study points, week of ${fmtDay(b.week)}`), h("b", { class: "pl-pts" }, String(b.me.points))),
      h("p", { class: "sub" }, `Your band: ${b.me.band.name} (${b.me.band.low}\u2013${b.me.band.high} study points).`), others];
  } else {
    body = h("p", { class: "sub" }, b.text);
  }
  return panel(h("div", { class: "panel-h" }, head, chip("joined", "line")), body, bands, about,
    h("label", { class: "pl-check top", for: "pl-mine-only" }, mine,
      h("span", null, h("b", null, "Just my points"), h("small", null, "Show only your own points and band, without names."))),
    h("div", { class: "actions top" }, button("Leave the board", run(() => playApi("/board/leave", "POST")), "ghost sm")), st);
}

function duelCard(d) {
  const st = h("p", { class: "small muted" });
  const run = runAt(st);
  const who = d.with.label ? `${d.with.name} (${d.with.label})` : d.with.name;
  let actions = null;
  if (d.state === "invited") {
    actions = h("div", { class: "actions top" }, button("Accept", run(() => playApi(`/duels/${d.id}/accept`, "POST")), "sm"),
      button("Decline", run(() => playApi(`/duels/${d.id}/decline`, "POST")), "ghost sm"));
  } else if (d.rehearsal) {
    actions = h("div", { class: "actions top" }, linkButton(d.state === "open" ? "Go to the questions" : "Review my answers", `#/rehearsal/${d.rehearsal}`, "ghost sm"));
  } else if (d.state === "open") {
    actions = h("div", { class: "actions top" }, button("Open the questions", async () => {
      try { const r = await playApi(`/duels/${d.id}/open`, "POST"); location.hash = `#/rehearsal/${r.rehearsal}`; }
      catch (e) { st.textContent = e.message; }
    }, "sm"));
  }
  const r = d.result;
  const score = r && r.theirs !== undefined
    ? h("p", { class: "pl-score" }, h("span", null, "You ", h("b", null, `${r.mine} of ${r.mine_of}`)), h("span", null, `${d.with.name} `, h("b", null, `${r.theirs} of ${r.theirs_of}`)))
    : r ? h("p", { class: "pl-score" }, h("span", null, "You ", h("b", null, `${r.mine} of ${r.mine_of}`))) : null;
  return h("div", { class: "qentry" },
    h("div", { class: "qh" }, h("b", null, `Duel with ${who}`), chip(d.domain ? `${d.exam} \u00b7 ${d.domain}` : d.exam, "line")),
    h("p", { class: "small ink2" }, d.text),
    d.progress && !r ? h("p", { class: "small muted" }, `${d.progress.answered} of ${d.progress.of} answered.`) : null,
    score, r ? h("p", { class: "small muted" }, "Your misses, with the reasons, are in your own answers. Nobody else sees them.") : null,
    actions, st);
}

function duelInvite(dv) {
  const st = h("p", { class: "small muted" });
  const exams = dv.exams.filter((x) => x.people.length);
  if (!exams.length) return h("p", { class: "small muted" }, "Nobody to invite yet: people appear here once they join circles, turn on \u201cOthers can invite me\u201d and study the same exam.");
  const exam = h("select", { id: "pl-duel-exam" }, exams.map((x) => h("option", { value: x.id }, `${x.exam} \u00b7 ${x.title}`)));
  const person = h("select", { id: "pl-duel-person" });
  const domain = h("select", { id: "pl-duel-domain" });
  const fill = () => {
    const x = exams.find((e) => e.id === exam.value);
    put(person, x.people.map((p) => h("option", { value: p.id }, p.label ? `${p.name} (${p.label})` : p.name)));
    put(domain, h("option", { value: "" }, "Any domain"), x.domains.map((dm) => h("option", { value: dm.id }, dm.title)));
  };
  exam.addEventListener("change", fill);
  fill();
  const go = button("Invite to a duel", async () => {
    go.disabled = true;
    try {
      await runJob(playApi("/duels", "POST", { person: person.value, package: exam.value, domain: domain.value || null }).then((r) => r.job),
        (s, secs) => { st.textContent = `${s} \u00b7 ${secs}s`; });
      route();
    } catch (e) { st.textContent = e.message; go.disabled = false; }
  }, "ghost sm");
  return h("details", { class: "how" }, h("summary", null, "Invite someone to a duel"),
    h("label", { class: "field top", for: "pl-duel-exam" }, "Exam", exam),
    h("label", { class: "field top", for: "pl-duel-person" }, "With", person),
    h("label", { class: "field top", for: "pl-duel-domain" }, "Domain", domain),
    h("p", { class: "small muted top" }, `Dojo writes ${dv.limits.questions} new checked questions once; it counts as one quick practice of yours. You see whether you have both finished, never whether they accepted or declined.`),
    h("div", { class: "actions top" }, go), st);
}

function duelsPanel(dv) {
  if (!dv || !dv.opted) return null;
  return panel(h("div", { class: "panel-h" }, eyebrow("quiz", "Duels"), dv.duels.length ? chip(plural(dv.duels.length, "duel"), "line") : null),
    h("p", { class: "sub" }, `By invitation: the same ${dv.limits.questions} questions for both, ${dv.limits.hours} hours to answer, no clock. Only the two of you see the two numbers right, once both have finished. No win, no loss, no record; deleted after ${dv.limits.keep_days} days.`),
    dv.duels.map(duelCard),
    dv.owner ? h("p", { class: "small muted top" }, "You run this Dojo, so you do not invite anyone to a duel. You take part when a member invites you, shown as \u201cruns this Dojo\u201d.") : duelInvite(dv));
}

// ---------------------------------------------------------------- Help (docs/user-guide.md)
/* The server renders the user guide from Markdown with every text escaped (app/helpdoc.py). Here it is
   parsed into an inert document, which runs nothing, and rebuilt through h() from an allow-list: other
   elements keep only their text, and no attribute survives but a checked heading id or link. */
const HELP_ROUTE = /^#\/help(?:\/([a-z0-9_-]{1,80}))?$/;
const HELP_TAGS = new Set(["h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "a"]);
const HELP_ID = /^[a-z0-9_-]{1,80}$/;
const helpId = (anchor) => `help-${anchor}`;

function helpNodes(node, gate) {
  const out = [];
  for (const n of node.childNodes) {
    if (n.nodeType === Node.TEXT_NODE) { out.push(n.data); continue; }
    if (n.nodeType !== Node.ELEMENT_NODE) continue;
    const tag = n.tagName.toLowerCase();
    if (tag === "h1") continue;                   // the page has its own title
    const kids = helpNodes(n, gate);
    if (!HELP_TAGS.has(tag)) { out.push(...kids); continue; }
    if (tag === "a") {
      const href = n.getAttribute("href") || "";
      if (/^https:\/\/[^\s"'<>\\]+$/.test(href)) out.push(h("a", { href, target: "_blank", rel: "noopener noreferrer" }, kids));
      else if (/^#\/help\/[a-z0-9_-]{1,80}$/.test(href)) out.push(h("a", { href }, kids));
      // Behind a gate only Help itself opens: another page of the app is shown as its name.
      else if (/^#\/[A-Za-z0-9/._-]*$/.test(href) && !gate) out.push(h("a", { href }, kids));
      else out.push(...kids);
      continue;
    }
    if (/^h[2-6]$/.test(tag)) {
      const owner = n.classList.contains("owner");
      out.push(h(tag, { id: HELP_ID.test(n.id) ? helpId(n.id) : null, tabindex: "-1", class: owner ? "owner" : null },
        kids, owner && tag !== "h2" ? h("span", { class: "chip amber owner-tag" }, "For the owner") : null));
      continue;
    }
    out.push(h(tag, null, kids));
  }
  return out;
}

// A link to a part of Help on the Help page itself scrolls there. Behind a gate there is no router, and
// a click on the open part would not change the address, so both are handled here.
function helpJump(e) {
  const a = e.target.closest ? e.target.closest('a[href^="#/help/"]') : null;
  if (!a || (!state.gate && a.getAttribute("href") !== location.hash)) return;
  const spot = document.getElementById(helpId(a.getAttribute("href").slice(7)));
  if (!spot) return;
  e.preventDefault();
  spot.scrollIntoView({ block: "start" });
  spot.focus({ preventScroll: true });
}

async function viewHelp(anchor) {
  const g = state.help || (state.help = await api("/api/help"));
  const gate = Boolean(state.gate);
  const doc = new DOMParser().parseFromString(String(g.html || ""), "text/html");
  const toc = h("details", { class: "panel helptoc", open: window.matchMedia("(min-width: 1081px)").matches },
    h("summary", null, "Contents"),
    h("ol", null, (g.toc || []).filter((t) => t.level === 2 && HELP_ID.test(t.id)).map((t) =>
      h("li", null, h("a", { href: `#/help/${t.id}`, class: t.owner ? "owner" : null }, t.title)))));
  const article = h("article", { class: "panel helpdoc" }, helpNodes(doc.body, gate));
  toc.addEventListener("click", helpJump);
  article.addEventListener("click", helpJump);
  if (anchor) state.scrollTo = helpId(anchor);
  return [h("div", { class: "between top wrap" },
      pageHead("Help", "How Dojo works for you, what it does not do, and what to try when something goes wrong. Parts marked \u201cFor the owner\u201d are for the person who runs this Dojo.",
        eyebrow("help", "Help · the user guide")),
      gate ? button("Back", () => { history.replaceState(null, "", location.pathname + location.search); showGate(state.gate); }, "ghost")
        : linkButton("How Dojo works", "#/about", "ghost sm")),
    h("div", { class: "row stackable grow helprow" }, h("div", { class: "col w330 helpside" }, toc), h("div", { class: "col grow" }, article))];
}

// Behind a gate (the notice, a pause, an ended membership) there is no router: Help is the one page that
// opens there, from the header link or a deep link, because it shows nothing personal.
async function gateHelp() {
  if (!state.gate || state.gate === "left") return;
  const m = (location.hash || "").match(HELP_ROUTE);
  if (!m) return;
  const view = $view();
  try {
    put(view, await viewHelp(m[1]));
    const spot = state.scrollTo && document.getElementById(state.scrollTo);
    state.scrollTo = null;
    window.scrollTo(0, 0);
    if (spot) { spot.scrollIntoView({ block: "start" }); spot.focus({ preventScroll: true }); }
  } catch (e) {
    put(view, panel(h("h2", null, "Help is not open yet"),
      h("p", { class: "sub" }, e.code === "not_member" ? "Help opens once the owner has let you in." : e.message),
      h("div", { class: "actions top" }, button("Back", () => { history.replaceState(null, "", location.pathname + location.search); showGate(state.gate); }, "ghost"))));
  }
}

// ---------------------------------------------------------------- router

const routes = [
  [/^#\/today$/, viewToday],
  [/^#\/check$/, viewCheckStart],
  [/^#\/check\/for\/([a-z0-9-]{2,40})$/, (pid) => openFor(pid, "#/check")],
  [/^#\/check\/([a-z]+-[0-9a-f]{20})$/, viewCheckRun],
  [/^#\/recall$/, () => viewCheckStart(true)],
  [/^#\/recall\/for\/([a-z0-9-]{2,40})$/, (pid) => openFor(pid, "#/recall")],
  [/^#\/five$/, viewFive],
  [/^#\/skills$/, viewSkills],
  [/^#\/lab\/(vector|search|hybrid)$/, viewLab],
  [/^#\/skill\/([a-z0-9-]+)\/([a-z0-9.]+)$/, viewSkill],
  [/^#\/lesson\/([a-z]+-[0-9a-f]{20})(?:\/(watch|read|listen|present)(?:\/s(\d{1,2}))?)?$/, viewLesson],
  [/^#\/item\/([a-z]+-[0-9a-f]{20})$/, viewItem],
  [/^#\/rehearsal$/, viewRehearsals],
  [/^#\/rehearsal\/for\/([a-z0-9-]{2,40})$/, (pid) => openFor(pid, "#/rehearsal")],
  [/^#\/rehearsal\/([a-z]+-[0-9a-f]{20})$/, viewRehearsal],
  [/^#\/exam\/([a-z]+-[0-9a-f]{20})(?:\/(review))?$/, viewExam],
  [/^#\/readiness$/, viewReadiness],
  [/^#\/ask(?:\/([a-z0-9.]+))?$/, viewAsk],
  [/^#\/answer\/([a-z]+-[0-9a-f]{20})$/, viewAnswer],
  [/^#\/record$/, viewRecord],
  [/^#\/play$/, viewPlay],
  [/^#\/plan(?:\/(\d{4}-\d{2}-\d{2}))?$/, viewPlan],
  [/^#\/quality$/, viewQuality],
  [/^#\/phone$/, viewPhone],
  [/^#\/about$/, viewAbout],
  [HELP_ROUTE, viewHelp],
  [/^#\/add-exam(?:\/([a-z]{2}-[0-9]{3}))?$/, viewAddExam],
  [/^#\/members$/, viewMembers],
  [/^#\/notice$/, viewNotice],
  [/^#\/exams$/, () => viewEnroll(false)],
];

async function route() {
  const token = ++state.nav;
  const hash = location.hash || "#/today";
  stopClock();
  examMode(false);
  checkMode(false);
  markNav();
  const match = routes.map(([re, fn]) => [hash.match(re), fn]).find(([m]) => m);
  if (!match) { location.hash = "#/today"; return; }
  const [m, fn] = match;
  put($view(), h("p", { class: "loading" }, "Loading…"));
  try {
    const content = await fn(...m.slice(1));
    if (token !== state.nav) return;
    put($view(), teamBanners(), fiveBanner(hash), content);
    $view().scrollTop = 0;
    window.scrollTo(0, 0);
    const spot = state.scrollTo && document.getElementById(state.scrollTo);
    state.scrollTo = null;
    if (spot) { spot.scrollIntoView({ block: "start" }); spot.focus({ preventScroll: true }); }
  } catch (e) {
    if (token !== state.nav) return;
    if (GATE_CODES.has(e.code)) { await gateAgain(); return; }
    put($view(), errorPanel(e));
  }
}

// A membership can change while the page is open (removed, paused, a new notice): ask again and show the gate.
const GATE_CODES = new Set(["not_member", "removed", "paused", "notice"]);
async function gateAgain() {
  try { state.me = await api("/api/me"); }
  catch (e) { if (e.code === "not_member") { showGate("access"); return; } put($view(), errorPanel(e)); return; }
  const kind = gateFor(state.me);
  if (kind) showGate(kind); else location.reload();
}

function gateFor(me) {
  if (!me || me.role !== "learner") return null;
  if (me.status === "removed") return "removed";
  if (!me.notice.acknowledged) return "notice";
  if (me.banner && me.banner.kind === "paused") return "paused";
  if (!(me.enrolled || []).length) return "enroll";   // a new member picks their exams first (ADR 0009)
  return null;
}

async function init() {
  openDeepLink();
  try {
    state.me = await api("/api/me");
  } catch (e) {
    if (e.code === "not_member") { showGate("access"); return; }
    put($view(), errorPanel(e));
    return;
  }
  const gateKind = gateFor(state.me);
  if (gateKind) { showGate(gateKind); return; }
  state.pid = state.me.profile.active_package;
  const sel = $("exam");
  fillExamSelect();
  sel.addEventListener("change", () => {
    if (sel.value !== ADD_EXAM) { switchPackage(sel.value); return; }
    sel.value = state.pid;
    location.hash = "#/add-exam";
  });
  const form = $("askform");
  const askInput = $("askq");
  const headerMic = canSpeak() ? speakBox(askInput, { compact: true, label: "Speak your question" }) : null;
  if (headerMic) form.insertBefore(headerMic.el, form.querySelector(".go"));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("askq");
    state.askDraft = q.value.trim();
    q.value = "";
    if (headerMic) headerMic.dispose();  // the question moves to the Ask page: stop listening here
    if (location.hash.startsWith("#/ask")) route(); else location.hash = "#/ask";
  });
  renderHeader();
  renderSide();
  watchTheBuild();
  registerWorker();
  window.addEventListener("hashchange", stopEveryMicrophone);  // going to another page stops any recording
  window.addEventListener("pagehide", stopEveryMicrophone);
  window.addEventListener("hashchange", route);
  route();
}

document.addEventListener("DOMContentLoaded", init);
