"use strict";
/* Dojo's service worker (ADR 0013). It caches nothing: there is no fetch handler, so every request goes
   to the server exactly as without a worker, and nothing is ever put in a cache. It only shows a nudge
   and opens Dojo when the nudge is tapped. */

// A nudge opens a Dojo page and nothing else: /?go=<route> on this origin. The route follows the same rule as in
// the app (app/deeplink.py, app.js) and is never decoded. A nudge sent before this build may carry /#/<route>:
// it is the same route, and Easy Auth would lose the "#" at sign-in, so it opens as /?go=<route>.
const ROUTE = /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*){0,5}$/;
const SAFE = /^\/(?:\?go=|#\/)([^?#]*)$/;
const safePath = (u) => {
  const m = typeof u === "string" ? SAFE.exec(u) : null;
  return m && m[1].length <= 120 && ROUTE.test(m[1]) ? `/?go=${m[1]}` : "/?go=today";
};

self.addEventListener("install", () => { self.skipWaiting(); });

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // Nothing is cached by Dojo; if anything ever was, it goes.
    for (const name of await caches.keys()) await caches.delete(name);
    await self.clients.claim();
  })());
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = typeof data.title === "string" && data.title ? data.title.slice(0, 60) : "Dojo";
  const body = typeof data.body === "string" && data.body ? data.body.slice(0, 200) : "Something is ready in Dojo.";
  event.waitUntil(self.registration.showNotification(title, {
    body, tag: "dojo-nudge", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: safePath(data.url) },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(safePath(event.notification.data && event.notification.data.url), self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin !== self.location.origin) continue;
      try { await w.focus(); } catch (e) { /* not allowed here: open a new window below */ continue; }
      try { await w.navigate(url); } catch (e) { /* an uncontrolled page: it is focused, that is enough */ }
      return;
    }
    await self.clients.openWindow(url);
  })());
});

// The browser replaced the subscription: subscribe again with the same key and tell Dojo. X-Requested-With:
// if the sign-in has ended, Easy Auth answers 403 instead of starting a sign-in, whose new nonce cookie would
// break one under way.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription;
    const key = old && old.options && old.options.applicationServerKey;
    const sub = event.newSubscription || (key ? await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }) : null);
    if (!sub) return;
    const j = sub.toJSON();
    await fetch("/api/push/devices", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Dojo": "1", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth, label: "Renewed by the browser" }),
    });
  })().catch(() => { /* Dojo notices at the next nudge and asks to turn nudges on again */ }));
});
