# PWA Reference — Push Notifications + iOS/Android Permissions

Distilled from the Scheduler app (React + Vite + Express). Use this as a starting blueprint for the next PWA.

## TL;DR Stack

- **Frontend**: Vite + React, `vite-plugin-pwa` (Workbox under the hood)
- **Backend**: Express + `web-push` npm package, PostgreSQL for subscription storage
- **Transport**: VAPID-signed Web Push (works on Chrome/Firefox/Edge/Samsung, and Safari 16.4+ on iOS *if installed to home screen*)

## 1. PWA Setup (Vite)

`vite.config.js`:
```js
VitePWA({
  registerType: 'autoUpdate',
  includeAssets: ['favicon.ico', 'apple-touch-icon.png', 'sw-push.js'],
  manifest: {
    name: 'AppName', short_name: 'AppName',
    theme_color: '#3b82f6', background_color: '#ffffff',
    display: 'standalone', orientation: 'portrait',
    scope: '/', start_url: '/',
    icons: [
      { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
      { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
      { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
    ],
  },
  workbox: {
    globPatterns: ['**/*.{js,css,ico,png,svg}'],
    importScripts: ['/sw-push.js'],   // <-- merges your push handler into the generated SW
    runtimeCaching: [{
      urlPattern: /^https:\/\/api\//,
      handler: 'NetworkFirst',
      options: { cacheName: 'api-cache', networkTimeoutSeconds: 10, cacheableResponse: { statuses: [0, 200] } },
    }],
  },
})
```

Register in `main.jsx`:
```js
import { registerSW } from 'virtual:pwa-register'
registerSW({
  immediate: true,
  onRegisteredSW(_url, reg) {
    if (reg) setInterval(() => reg.update(), 30 * 60 * 1000)  // poll for new SW
  },
})
```

**Required public/ assets**: `favicon.ico`, `pwa-192x192.png`, `pwa-512x512.png`, `apple-touch-icon.png` (180×180), `sw-push.js`.

## 2. index.html — Don't Skip These Meta Tags

The Scheduler is missing some of these and it costs iOS UX. **Include all of them in the next app:**

```html
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
<meta name="theme-color" content="#3b82f6" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png" />

<!-- iOS PWA polish — currently MISSING in Scheduler -->
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="AppName" />
<meta name="format-detection" content="telephone=no,email=no" />
```

## 3. Service Worker — `public/sw-push.js`

Plain JS, loaded via `importScripts` in the Workbox-generated SW:

```js
self.addEventListener('push', (event) => {
  if (!event.data) return
  const data = event.data.json()
  event.waitUntil(self.registration.showNotification(data.title || 'AppName', {
    body: data.body || '',
    tag: data.tag || 'default',
    data: data.data || {},
    icon: '/pwa-192x192.png',
    badge: '/pwa-192x192.png',
    vibrate: [200, 100, 200],
  }))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const raw = event.notification.data?.url || '/'
  const url = raw.startsWith('/') ? raw : '/'   // security: block external redirects
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const c of wins) {
      if (c.url.includes(self.location.origin) && 'focus' in c) {
        c.navigate(url); return c.focus()
      }
    }
    if (clients.openWindow) return clients.openWindow(url)
  }))
})
```

## 4. Subscription Hook — `hooks/usePushNotifications.js`

The whole subscribe flow:
1. `Notification.requestPermission()`
2. `GET /api/notifications/vapid-key` → base64-URL public key
3. Convert key to `Uint8Array` (helper below)
4. `registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })`
5. `POST /api/notifications/subscribe` with `subscription.toJSON()`

```js
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}
```

Check support with: `'serviceWorker' in navigator && 'PushManager' in window`.

## 5. Backend — VAPID + web-push

```bash
npx web-push generate-vapid-keys   # generate once, store in env
```

Env vars:
```
VAPID_PUBLIC_KEY=...
VAPID_PRIVATE_KEY=...
VAPID_SUBJECT=mailto:admin@yourapp.com
```

Configure on boot:
```js
const webpush = require('web-push')
if (process.env.VAPID_PUBLIC_KEY && process.env.VAPID_PRIVATE_KEY) {
  webpush.setVapidDetails(process.env.VAPID_SUBJECT, process.env.VAPID_PUBLIC_KEY, process.env.VAPID_PRIVATE_KEY)
}
```

Send (and auto-clean dead subscriptions):
```js
for (const sub of subs) {
  try {
    await webpush.sendNotification(
      { endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } },
      JSON.stringify({ title, body, data, tag: type })
    )
  } catch (err) {
    if (err.statusCode === 404 || err.statusCode === 410) {
      await deleteSubscription(sub.id)   // endpoint expired
    }
  }
}
```

## 6. DB Schema

```sql
CREATE TABLE push_subscriptions (
  id SERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  user_agent VARCHAR(500),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, endpoint)
);

-- In-app fallback (so the badge/list works for logged-out users too)
CREATE TABLE notifications (
  id SERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type VARCHAR(50) NOT NULL,
  title VARCHAR(255) NOT NULL,
  body TEXT,
  data JSONB,                    -- { url: '/somewhere', ...ids }
  read BOOLEAN DEFAULT FALSE,
  pushed BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON notifications(user_id, read);
CREATE INDEX ON notifications(created_at DESC);

-- User opt-out per category
CREATE TABLE notification_preferences (
  id SERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category VARCHAR(50) NOT NULL,
  enabled BOOLEAN DEFAULT TRUE,
  UNIQUE(user_id, category)
);
```

## 7. API Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/notifications/vapid-key` | public | return public VAPID key (or `null` if not configured) |
| POST | `/api/notifications/subscribe` | yes | store `{ endpoint, keys: { p256dh, auth } }` |
| DELETE | `/api/notifications/subscribe` | yes | remove by endpoint |
| GET | `/api/notifications` | yes | paginated in-app list |
| GET | `/api/notifications/unread-count` | yes | badge count |
| PUT | `/api/notifications/:id/read` | yes | mark one read |
| PUT | `/api/notifications/read-all` | yes | mark all read |
| GET/PUT | `/api/notifications/preferences` | yes | category opt-in/out |

## 8. iOS — The Important Quirks (Solved)

**The core rule:** iOS Web Push **only works after the user adds the site to the Home Screen** (Safari 16.4+ required, iOS 16.4+).

Practical implications + how Scheduler handles them:

1. **Don't prompt for notifications until the app is installed.** Use a banner that only renders when running standalone:
   ```js
   const isPWA = window.matchMedia('(display-mode: standalone)').matches
                 || navigator.standalone === true   // <-- iOS-specific
   if (isPWA && Notification.permission === 'default') showBanner()
   ```
   See `components/PushNotificationBanner.jsx`.

2. **No `beforeinstallprompt` on iOS.** Show manual instructions instead:
   > "Tap the Share button → Scroll down → Add to Home Screen"

   Detect with `/iPhone|iPad|iPod/i.test(navigator.userAgent)`. See `components/InstallPrompt.jsx` for the platform-split UI.

3. **Always have a fallback channel.** Scheduler sends SMS *in addition to* web push (and stores an in-app notification row). On iOS < 16.4 / non-installed devices, this is the only thing that gets through.

4. **Standalone detection is two checks.** Use *both* `matchMedia('(display-mode: standalone)')` *and* `navigator.standalone` — iOS only sets the latter.

5. **Icon cache is sticky.** iOS caches Home Screen icons for weeks. Bump icon filenames (`pwa-512-v2.png`) when redesigning.

6. **VAPID keys are domain-locked.** Changing the prod domain invalidates every existing subscription. Pick the domain before going live.

## 9. Android — What's Different

- `beforeinstallprompt` fires automatically (Chrome/Edge/Samsung). Capture it, prevent default, and show your own Install button:
  ```js
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferred.current = e })
  // later: deferred.current.prompt()
  ```
- Web Push works in-browser without install, but installed is still nicer (notifications survive browser close).
- `vibrate: [200, 100, 200]` works; iOS ignores it.
- Android 13+ wrapper apps need `POST_NOTIFICATIONS` permission. Pure PWAs (no Capacitor/TWA) don't.

## 10. Permission UX Pattern (Copy This)

1. **Don't** call `Notification.requestPermission()` on page load. Browsers penalize it and users deny.
2. Show an **opt-in banner** with a clear "why" *only when*:
   - running as PWA (`isPWA`), AND
   - `Notification.permission === 'default'`, AND
   - user hasn't dismissed it (`localStorage['push-prompted']`).
3. Permission is requested only when they tap **Enable**. Store the dismissal either way.
4. Provide a per-category preferences page so users tune down rather than disabling system-wide.

## 11. Packages

Frontend: `vite-plugin-pwa`
Backend: `web-push`

That's it for push. (Plus your usual Express/React/Postgres deps.)

## 12. Full Flow (One Page)

```
trigger event (e.g. shift change)
  └─ notificationService.sendNotification(userId, type, title, body, data)
       ├─ check preferences (skip if user opted out of this category)
       ├─ INSERT into notifications table  (in-app fallback)
       ├─ for each push_subscription of user:
       │     webpush.sendNotification(sub, JSON.stringify(payload))
       │       └─ on 404/410: DELETE the subscription row
       └─ optionally: SMS/email fallback

browser SW receives push
  └─ self.registration.showNotification(title, { body, data, icon, badge, vibrate, tag })

user taps notification
  └─ notificationclick → focus existing window OR openWindow(data.url)
```

## 13. Key Files in This Repo (Cross-Reference)

| File | What |
|---|---|
| `frontend/vite.config.js` | PWA plugin config |
| `frontend/index.html` | meta tags (add the missing iOS ones in the next app) |
| `frontend/public/sw-push.js` | push + click handlers |
| `frontend/src/main.jsx` | `registerSW` + 30-min update polling |
| `frontend/src/hooks/usePushNotifications.js` | subscribe / unsubscribe / state |
| `frontend/src/components/PushNotificationBanner.jsx` | PWA-only opt-in banner |
| `frontend/src/components/InstallPrompt.jsx` | iOS vs Android install UI |
| `backend/src/services/notificationService.js` | send + preference logic |
| `backend/src/routes/notifications.js` | the 8 endpoints above |
| `backend/src/repositories/notificationRepository.js` | DB ops |
| `backend/src/db/migrations/20240203000000_notifications.js` | schema |

## 14. Gotchas Worth Remembering

- `userVisibleOnly: true` is **required** by Chrome — every push must show a notification.
- A `tag` on the notification collapses duplicates (good for "X updates" type messages).
- Validate `notification.data.url` to start with `/` — otherwise you've built an open-redirect.
- `web-push` errors 404/410 mean the subscription is dead — delete it, don't retry.
- Same user on phone + laptop = two `push_subscriptions` rows. Loop over all of them when sending.
- The Workbox-generated SW is regenerated every build — keep custom code in `sw-push.js` (imported via `importScripts`), never edit the generated file.
- Test on a real iPhone. The simulator lies.
