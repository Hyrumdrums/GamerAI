// ---- push notifications client (Phase 6 of pwa-refactor.txt) -------
// Vanilla-JS port of PWA_REFERENCE §4 `usePushNotifications.js`.
// Subscribes the current browser to Web Push and POSTs the
// subscription handle to /api/notifications/subscribe. The SW
// (sw.js) handles the actual push events; this module only owns the
// subscribe / unsubscribe lifecycle.
//
// Flow (when the user taps Enable on the banner):
//   1. permission = await Notification.requestPermission()
//   2. fetch /api/notifications/vapid-key → public key (or null)
//   3. urlBase64ToUint8Array(key)
//   4. navigator.serviceWorker.ready → registration
//   5. registration.pushManager.subscribe({...})
//   6. POST /api/notifications/subscribe with subscription.toJSON()
//
// Unsubscribe is the reverse: getSubscription() → sub.unsubscribe()
// → DELETE /api/notifications/subscribe.

// Convert the base64url-encoded VAPID public key into the Uint8Array
// pushManager.subscribe expects. Verbatim from PWA_REFERENCE §4.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

// Capability gate: even on HTTPS, some browsers (older Safari, some
// in-app webviews) lack one of the two APIs we need. PushManager being
// missing is the common one on macOS Safari prior to 16.4.
export function pushSupported() {
  return 'serviceWorker' in navigator
      && 'PushManager' in window
      && 'Notification' in window;
}

export function permissionState() {
  if (!('Notification' in window)) return 'unsupported';
  return Notification.permission;  // 'default' | 'granted' | 'denied'
}

// Already subscribed in this browser? Used by the banner to decide
// whether to show the opt-in or stay hidden. Returns the live
// PushSubscription | null.
export async function getCurrentSubscription() {
  if (!pushSupported()) return null;
  try {
    const reg = await navigator.serviceWorker.ready;
    return await reg.pushManager.getSubscription();
  } catch (_e) {
    return null;
  }
}

// Best-effort wake of the system permission prompt + server-side
// subscription save. Returns the saved subscription on success.
// Throws Error with a user-friendly message on any failure so the
// banner can surface it.
export async function subscribe() {
  if (!pushSupported()) {
    throw new Error('Notifications are not supported in this browser.');
  }
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    // Could be 'denied' (user said no) or 'default' (dismissed the
    // prompt without choosing). Either way, treat as opt-out.
    throw new Error('Notifications permission was not granted.');
  }
  const keyResp = await fetch('/api/notifications/vapid-key');
  if (!keyResp.ok) throw new Error('Failed to fetch VAPID key.');
  const { key } = await keyResp.json();
  if (!key) {
    throw new Error('Push is disabled on the server.');
  }
  const applicationServerKey = urlBase64ToUint8Array(key);
  const reg = await navigator.serviceWorker.ready;
  // userVisibleOnly: true is REQUIRED by Chrome. Silent pushes are
  // not permitted on the web; every push must show a notification.
  // The SW push handler honors that by always calling showNotification.
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });
  const subJson = sub.toJSON();
  const saveResp = await fetch('/api/notifications/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      endpoint: subJson.endpoint,
      keys: subJson.keys,
    }),
  });
  if (!saveResp.ok) {
    // Roll back the browser subscription so we don't keep sending
    // pushes to an endpoint the server doesn't know about.
    try { await sub.unsubscribe(); } catch (_) {}
    throw new Error('Failed to save subscription on the server.');
  }
  return sub;
}

// Reverse of subscribe(): cancel the browser subscription AND tell
// the server to forget the endpoint. Both halves are best-effort so a
// network blip on one doesn't leave the other half stranded.
export async function unsubscribe() {
  if (!pushSupported()) return false;
  const sub = await getCurrentSubscription();
  if (!sub) return false;
  const endpoint = sub.endpoint;
  let browserOk = true;
  try { await sub.unsubscribe(); } catch (_e) { browserOk = false; }
  try {
    await fetch('/api/notifications/subscribe', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    });
  } catch (_e) { /* server cleanup is best-effort */ }
  return browserOk;
}

export async function unreadCount() {
  try {
    const r = await fetch('/api/notifications/unread-count');
    if (!r.ok) return 0;
    return (await r.json()).count || 0;
  } catch (_e) { return 0; }
}
