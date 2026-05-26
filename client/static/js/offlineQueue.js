// ---- offline send queue ----------------------------------------------
// IndexedDB-backed queue of pending /api/generate POSTs that failed
// because the browser was offline. Phase 5 of pwa-refactor.txt.
//
// Shape per entry:
//   { id, enqueued_at, conversation_id, payload }
//
// `id` is a client-generated UUID; it's also stamped onto the typing
// bubble's dataset.queueId so the drain code can find the DOM element
// that's waiting for this send to complete. `payload` is the full
// /api/generate body that the submit handler would have sent online.
//
// The store is named 'pending_sends' inside the 'gamerai-offline' DB.
// Bumping DB_VERSION will fire onupgradeneeded again — keep that in
// sync with any schema additions (a new index, a second store, etc.).
//
// IndexedDB is unavailable in some browsers' private-mode windows;
// every method swallows the open failure into a rejection so callers
// can fall back to an inline error message instead of crashing the
// submit handler.

const DB_NAME = 'gamerai-offline';
const DB_VERSION = 1;
const STORE_NAME = 'pending_sends';

function openDb() {
  return new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(DB_NAME, DB_VERSION); }
    catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        // keyPath: 'id' means each entry is its own UUID-keyed row;
        // we never need to scan by conversation_id (the drain reads
        // the full queue and filters in memory — there's never more
        // than a handful of entries).
        db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function enqueue(entry) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).put(entry);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

export async function list() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const req = tx.objectStore(STORE_NAME).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

export async function remove(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// Remove every queue entry for a given conversation_id. Called when the
// user hard-deletes a conversation — without this, the next drain would
// POST /api/generate against a deleted conversation_id and just create
// error bubbles for sends the user has already abandoned.
export async function removeByConversation(conversationId) {
  const entries = await list();
  await Promise.all(
    entries
      .filter((e) => e.conversation_id === conversationId)
      .map((e) => remove(e.id)),
  );
}

// Best-effort: ask the service worker to register a 'send-message'
// background sync. The sync wakes an open tab via postMessage so the
// page-side drain runs; if the browser doesn't expose SyncManager
// (Safari) this is a no-op and we rely on the 'online' + visibility
// listeners in chat.js instead.
export async function registerSync() {
  if (!('serviceWorker' in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    if (reg && 'sync' in reg) {
      await reg.sync.register('send-message');
    }
  } catch (_e) {
    // SyncManager refuses without permission on some browsers and
    // throws when invoked off a secure context. Either way the page
    // will still drain on the 'online' event — no need to surface.
  }
}
