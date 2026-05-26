// Service worker — Phase 4 of pwa-refactor.txt
//
// Hand-rolled (no Workbox / vite-plugin-pwa — we have no build step).
// Three concerns:
//
//   1. Precache the app shell on install so a cold reload offline
//      still renders the layout, CSS, JS modules, and icons.
//   2. Runtime caching strategy per request kind:
//        - HTML navigations  → network-first, fall back to /offline
//        - Static (CSS/JS/icons/manifest) → cache-first
//        - /api/*            → network-only (no SW interception)
//        - Anything else     → network-only
//   3. Cache invalidation: every release bumps CACHE_VERSION; the
//      activate handler deletes any cache whose name doesn't match
//      the current version, then claims clients so the new SW takes
//      over immediately (instead of waiting for every tab to close).
//
// To ship a new release: bump CACHE_VERSION below and redeploy.
// The new SW installs in parallel with the old one, activates on
// next navigation, and purges old caches.
//
// Scope: served from /sw.js (root) via a FastAPI route in client/app.py
// that sets `Service-Worker-Allowed: /`. This lets the SW intercept
// every path on the origin even though the source file lives under
// /static/sw.js.

const CACHE_VERSION = 'v2';
const SHELL_CACHE = `gamerai-shell-${CACHE_VERSION}`;
const RUNTIME_CACHE = `gamerai-runtime-${CACHE_VERSION}`;

// The minimal asset list needed to render the chat UI offline.
// Anything else (e.g. dashboard.html.j2 CSS) is fetched on demand
// and cached opportunistically via the fetch handler below.
const SHELL_ASSETS = [
  '/offline',
  '/static/css/base.css',
  '/static/css/chat.css',
  '/static/marked.min.js',
  '/static/purify.min.js',
  '/static/js/chat.js',
  '/static/js/state.js',
  '/static/js/readAloud.js',
  '/static/js/messageRenderer.js',
  '/static/js/streamingEngine.js',
  '/static/js/composer.js',
  '/static/js/imageGallery.js',
  '/static/js/notifications.js',
  '/static/js/installPrompt.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon.ico',
];

self.addEventListener('install', (event) => {
  // skipWaiting() so a deploy doesn't sit idle waiting for every tab
  // to close before the new SW takes over. Combined with clients.claim()
  // in activate, the user gets the fresh shell on next navigation.
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // addAll() is atomic — any single 404/5xx aborts the whole install.
    // We list only files we know exist; if a release ships without one
    // of them, the install fails and the old SW keeps serving (safe).
    await cache.addAll(SHELL_ASSETS);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n !== SHELL_CACHE && n !== RUNTIME_CACHE)
        .map((n) => caches.delete(n)),
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin GETs. Cross-origin and non-GETs go
  // straight to the network — caching POSTs is meaningless and
  // cross-origin caching invites privacy / CORS confusion.
  if (req.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  // API: always network. Phase 5 will add a Background Sync queue
  // for offline submits; until then, /api/* failures bubble to the
  // app's existing error handling.
  if (url.pathname.startsWith('/api/')) return;

  // Navigations (HTML pages): network-first so users always get the
  // latest server-rendered template when online. If the network
  // fails, try the runtime cache, then the offline fallback.
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        // Opportunistically cache successful HTML so the same path
        // works offline next time. We only cache 200s; 3xx redirects
        // and 4xx pages aren't worth burning storage on.
        if (fresh && fresh.status === 200) {
          const runtime = await caches.open(RUNTIME_CACHE);
          runtime.put(req, fresh.clone());
        }
        return fresh;
      } catch (_e) {
        const cached = await caches.match(req);
        if (cached) return cached;
        return (await caches.match('/offline'))
          || new Response('offline', { status: 503 });
      }
    })());
    return;
  }

  // Static assets: cache-first. The shell cache is the warmest path;
  // the runtime cache catches subsequent first-time fetches (e.g.
  // an admin user loading dashboard.css on first visit).
  event.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.status === 200) {
        const runtime = await caches.open(RUNTIME_CACHE);
        runtime.put(req, fresh.clone());
      }
      return fresh;
    } catch (_e) {
      // No cache, no network — let the browser surface its native
      // error. Returning a synthetic Response here would mask the
      // failure mode (and DOMPurify / marked would behave oddly).
      throw _e;
    }
  })());
});

// ---- push (Phase 6 of pwa-refactor.txt) ------------------------------
// The browser delivers a 'push' event when the OS-level Push service
// hands off a payload encrypted with the subscription's VAPID keys.
// Chrome requires every push to show a notification (userVisibleOnly:
// true was set at subscribe time), so we always call showNotification
// even if the payload is empty.
//
// The payload shape matches what coordinator/notifications.send_to_member
// sends: { title, body, tag, data: { url?, conversation_id?, ... } }.

self.addEventListener('push', (event) => {
  let payload = {};
  if (event.data) {
    try { payload = event.data.json(); }
    catch (_e) {
      try { payload = { title: 'GamerAI', body: event.data.text() }; }
      catch (_e2) { payload = { title: 'GamerAI', body: '' }; }
    }
  }
  const title = payload.title || 'GamerAI';
  const opts = {
    body: payload.body || '',
    // `tag` collapses duplicates — a second "image done" push replaces
    // the first instead of stacking, so a user who left the app
    // overnight doesn't wake to a wall of notifications.
    tag: payload.tag || 'gamerai',
    data: payload.data || {},
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    // iOS Safari ignores vibrate; Android honors it. Harmless either way.
    vibrate: [200, 100, 200],
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

// When the user taps the notification, focus an existing tab on the
// linked URL, or open a new window if none is open. data.url comes
// from the coordinator's notification payload (e.g. /?conv=abc123).
//
// Security: validate data.url starts with '/' before navigating —
// otherwise this is an open-redirect (PWA_REFERENCE §3 + §14).
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const raw = (event.notification.data && event.notification.data.url) || '/';
  const url = (typeof raw === 'string' && raw.startsWith('/')) ? raw : '/';
  event.waitUntil((async () => {
    const wins = await clients.matchAll({
      type: 'window', includeUncontrolled: true,
    });
    for (const c of wins) {
      // Same-origin tab open → focus + navigate. Use includes() rather
      // than equality so a tab at /account or /dashboard still gets
      // reused (and navigated to the linked path).
      if (c.url && c.url.includes(self.location.origin)) {
        try { await c.navigate(url); } catch (_e) {}
        return c.focus();
      }
    }
    if (clients.openWindow) return clients.openWindow(url);
  })());
});
