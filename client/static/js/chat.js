// ---- chat orchestrator -----------------------------------------------
// Wires up sidebar, conversation lifecycle, top-bar handlers, and the
// composer submit handler. Streaming, rendering, read-aloud, composer
// toggles, and the image lightbox live in their own modules — see
// pwa-refactor.txt for the layout.

import {
  state, msgCache, searchModeByConv,
  setConvTokens, sumMessageTokens,
} from './state.js';
import { stopReadAloud } from './readAloud.js';
import { messageEl } from './messageRenderer.js';
import { streamIntoBubble } from './streamingEngine.js';
import {
  imageCheckbox, searchCheckbox,
  selectedSearchMode, selectedImageSize,
  IMAGE_SIZE_PRESETS,
  refreshComposerUI,
  restoreModeFor,
} from './composer.js';
import { initImageGallery } from './imageGallery.js';
import { initPWAPrompts } from './installPrompt.js';
import * as offlineQueue from './offlineQueue.js';

// ---- bootstrap --------------------------------------------------------
async function init() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { location.href = '/login'; return; }
    state.me = await r.json();
    const whoEl = document.getElementById('who');
    whoEl.textContent = state.me.username || state.me.email || state.me.member_id || 'signed in';
    whoEl.href = '/account';
    if (state.me.role === 'admin') {
      document.getElementById('adminlink').hidden = false;
    }
    // Show the "Contribute and invite friends" CTA only when the
    // member has no paired agent yet. Once they pair a machine, they
    // *are* a contributor — the link becomes redundant.
    if (!state.me.paired_machines_count) {
      document.getElementById('contributelink').hidden = false;
    }
  } catch { location.href = '/login'; return; }
  await refreshSidebar();
  if (state.conversations.length > 0) {
    openConversation(state.conversations[0].conversation_id);
  }
}
init();

// ---- sidebar ----------------------------------------------------------
async function refreshSidebar() {
  const r = await fetch('/api/conversations');
  if (!r.ok) return;
  const body = await r.json();
  state.conversations = body.conversations || [];
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  for (const c of state.conversations) {
    const div = document.createElement('div');
    div.className = 'conv-item' + (c.conversation_id === state.currentId ? ' active' : '');
    // Collapse whitespace runs (including newlines) so a paste-bombed
    // title from the empty-state DOM still renders as a single ellipsised
    // line instead of leaking blank space into the layout.
    const raw = c.title || '(untitled)';
    const titleSpan = document.createElement('span');
    titleSpan.className = 'conv-title';
    titleSpan.textContent = raw.replace(/\s+/g, ' ').trim() || '(untitled)';
    div.title = raw;
    div.appendChild(titleSpan);
    // Delete button — hidden by default, visible on hover (and
    // always visible on the active row). Click is a hard delete;
    // confirm() first because the action wipes images + history.
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'conv-del';
    del.setAttribute('aria-label', 'Delete chat');
    del.title = 'Delete chat';
    del.textContent = '×';
    del.onclick = (e) => {
      e.stopPropagation();  // don't also fire the row's openConversation
      deleteConversation(c.conversation_id, raw);
    };
    div.appendChild(del);
    div.onclick = () => openConversation(c.conversation_id);
    list.appendChild(div);
  }
}

// Hard-delete a conversation: confirms first because the server wipes
// messages + all attached images from disk + Redis. On success, drops
// the row from the cache and rerenders; if the deleted chat is the
// one currently on screen, also blanks the pane back to the empty
// state so the user isn't staring at a phantom conversation.
async function deleteConversation(id, label) {
  const display = (label || '').replace(/\s+/g, ' ').trim() || '(untitled)';
  if (!window.confirm(
    `Delete "${display}"?\n\nThis permanently removes the chat history ` +
    `and any generated images. This can't be undone.`,
  )) return;
  const r = await fetch('/api/conversations/' + encodeURIComponent(id), {
    method: 'DELETE',
  });
  if (!r.ok) {
    alert(`Couldn't delete: ${r.status} ${r.statusText}`);
    return;
  }
  msgCache.delete(id);
  // Drop any offline-queued sends that targeted this conversation —
  // re-POSTing them once we're online would hit a deleted conv_id and
  // produce confusing error bubbles for prompts the user has already
  // walked away from.
  offlineQueue.removeByConversation(id).catch(() => {});
  if (state.currentId === id) {
    state.currentId = null;
    stopReadAloud();
    document.getElementById('chat-pane').innerHTML =
      '<div class="empty"><h2>What\'s on your mind?</h2>' +
      '<div>Start a new conversation by typing below.</div></div>';
    setConvTokens(0);
  }
  await refreshSidebar();
}

document.getElementById('new-chat').onclick = () => {
  state.currentId = null;
  // Brand-new chat defaults to plain chat mode — don't inherit the
  // last conversation's search-on state. restoreModeFor is defined
  // below; function declarations are hoisted, so calling it from this
  // click handler is fine even before the file finishes parsing.
  searchModeByConv.delete(null);
  restoreModeFor(null);
  stopReadAloud();
  document.getElementById('chat-pane').innerHTML =
    '<div class="empty"><h2>What\'s on your mind?</h2><div>Start a new conversation by typing below.</div></div>';
  document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
  document.getElementById('sidebar').classList.remove('open');
  setConvTokens(0);
  document.getElementById('prompt').focus();
};

// Hamburger toggle for the mobile drawer. On desktop the hamburger is
// hidden via CSS so this handler is effectively unreachable there.
document.getElementById('hamburger').onclick = () => {
  document.getElementById('sidebar').classList.toggle('open');
};

// ---- conversation rendering ------------------------------------------
async function openConversation(id) {
  state.currentId = id;
  // Restore the per-conversation search-mode state. If the user had
  // search on the last time they were in this conversation, the
  // checkbox flips back on so a follow-up automatically searches —
  // no need to remember to re-check it.
  restoreModeFor(id);
  // Cancel any in-flight stream for the conversation we're leaving;
  // renderMessages may start a new one if the conversation we're
  // opening has a pending turn of its own.
  state.activeStream = null;
  // Stop any read-aloud playback from the conversation we're leaving —
  // the speaker button it was attached to is about to be removed from
  // the DOM, so without this the audio would keep talking with no
  // visible way to stop it.
  stopReadAloud();
  document.getElementById('submit').disabled = false;
  document.getElementById('prompt').disabled = false;
  // On mobile, close the drawer once a conversation is picked. CSS
  // makes this a no-op on desktop where the sidebar is persistent.
  document.getElementById('sidebar').classList.remove('open');
  // Highlight the right sidebar entry.
  document.querySelectorAll('.conv-item').forEach((el, i) => {
    el.classList.toggle('active', state.conversations[i] && state.conversations[i].conversation_id === id);
  });
  // Cache hit: render immediately, no network. We still kick off a
  // background fetch to pick up turns added from another tab/device.
  // Cache may be stale if a pending message has since completed;
  // refreshConversation re-renders if the server-side version differs.
  if (msgCache.has(id)) {
    renderMessages(msgCache.get(id));
    refreshConversation(id);  // background; will re-render if it changed
    return;
  }
  await refreshConversation(id, {render: true});
}

async function refreshConversation(id, {render = false} = {}) {
  const r = await fetch('/api/conversations/' + id);
  if (!r.ok) return;
  const body = await r.json();
  const messages = body.messages || [];
  msgCache.set(id, messages);
  if (render || state.currentId === id) renderMessages(messages);
}

function renderMessages(messages) {
  const pane = document.getElementById('chat-pane');
  pane.innerHTML = '';
  setConvTokens(sumMessageTokens(messages));
  if (messages.length === 0) {
    pane.innerHTML = '<div class="empty"><h2>(empty)</h2></div>';
    return;
  }
  let pendingEl = null;
  let pendingJobId = null;
  let pendingMessageId = null;
  for (const m of messages) {
    const el = messageEl(m.role, m.text, {
      status: m.status,
      message_id: m.message_id,
      image_path: m.image_path,
    });
    pane.appendChild(el);
    if (m.role === 'assistant' && m.status === 'pending') {
      pendingEl = el;
      pendingJobId = m.job_id;
      pendingMessageId = m.message_id;
    }
  }
  pane.scrollTop = pane.scrollHeight;
  // If the latest assistant turn was still streaming when this
  // conversation got loaded (the user closed the tab / locked their
  // phone mid-generation), pick up polling where we left off so the
  // remaining tokens stream in. message_id is threaded through so an
  // eventual error state can offer a retry button.
  if (pendingEl && pendingJobId) {
    streamIntoBubble(pendingJobId, pendingEl, null, null, pendingMessageId);
  }
}

// ---- composer ---------------------------------------------------------
const textarea = document.getElementById('prompt');
textarea.addEventListener('input', () => {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 12 * 16) + 'px';
});
textarea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    document.getElementById('composer').requestSubmit();
  }
});

document.getElementById('composer').onsubmit = async (e) => {
  e.preventDefault();
  const prompt = textarea.value.trim();
  if (!prompt) return;
  const submitBtn = document.getElementById('submit');
  const statusEl = document.getElementById('status');
  submitBtn.disabled = true;
  textarea.disabled = true;
  // Stop any audio still playing from the previous answer — voice
  // mode will auto-fire on this new response's completion, and a new
  // question while the old reply is still talking would otherwise
  // leave the prior clip running with no obvious way to stop it.
  stopReadAloud();

  // If this is a brand-new chat (no currentId), create one first.
  // Hold the pre-create checkbox state so we can copy it onto the
  // freshly-minted conversation id below — without this, a "+ New
  // chat → check search → submit" flow would lose the sticky bit.
  const wasNewChat = !state.currentId;
  const newChatSearchState = wasNewChat ? searchCheckbox.checked : null;
  if (!state.currentId) {
    let cr;
    try {
      cr = await fetch('/api/conversations', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({}),
      });
    } catch (_netErr) {
      // Brand-new chats can't be queued — we need a conversation_id
      // before we have somewhere to attach the queued send. Drafts
      // into an *existing* conversation queue fine below; this case
      // only loses if the user opens the app, hits "+ New chat", and
      // submits while offline. Telling them to try again is honest.
      statusEl.textContent = "can't start a new chat while offline";
      submitBtn.disabled = false; textarea.disabled = false;
      return;
    }
    if (!cr.ok) {
      statusEl.textContent = 'failed to create conversation';
      submitBtn.disabled = false; textarea.disabled = false;
      return;
    }
    state.currentId = (await cr.json()).conversation_id;
    if (newChatSearchState !== null) {
      searchModeByConv.set(state.currentId, newChatSearchState);
    }
  }

  // Snapshot the checkbox state at submit time. Mid-stream toggling
  // shouldn't change what this turn is.
  //
  // Image is one-shot — auto-clear after submit so an accidental
  // left-on can't burn 5 contributor GPU jobs in a row. Search
  // is STICKY per-conversation (see the comment on searchModeByConv
  // above); the checkbox state is preserved across submits and only
  // resets when the user manually unchecks, opens a different
  // conversation, or starts a new chat.
  let submitTool = 'chat';
  if (imageCheckbox.checked) submitTool = 'image';
  else if (searchCheckbox.checked) submitTool = 'search';
  const submitSearchMode = submitTool === 'search' ? selectedSearchMode() : null;
  // Snapshot before auto-clearing the image checkbox so the resolution
  // pick that the user saw on submit is what we actually send.
  const submitImageSize = submitTool === 'image' ? selectedImageSize() : null;
  imageCheckbox.checked = false;
  // searchCheckbox stays — sticky.
  refreshComposerUI();

  // Append the user message optimistically so it shows up right away.
  const pane = document.getElementById('chat-pane');
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();
  pane.appendChild(messageEl('user', prompt));
  const typing = messageEl('assistant', '', {
    status: 'pending',
    pending_kind: submitTool,
  });
  pane.appendChild(typing);
  pane.scrollTop = pane.scrollHeight;
  textarea.value = '';
  textarea.style.height = 'auto';

  // Submit + stream.
  if (submitTool === 'image') statusEl.textContent = 'rendering…';
  else if (submitTool === 'search') statusEl.textContent = 'searching…';
  else statusEl.textContent = 'submitting…';
  const start = Date.now();
  const body = {prompt, conversation_id: state.currentId};
  if (submitTool === 'image') {
    body.tool = 'image';
    // Always pin width/height so the worker doesn't fall through to
    // the model's native default (1024² on SDXL Lightning) — that
    // would silently bill the user the 4× large rate when they meant
    // to pick small.
    const preset = IMAGE_SIZE_PRESETS[submitImageSize] || IMAGE_SIZE_PRESETS.small;
    body.image = {width: preset.width, height: preset.height};
  } else if (submitTool === 'search') {
    body.tool = 'search';
    body.search_mode = submitSearchMode;
  }
  if (submitTool === 'chat' && state.voiceMode) {
    // Voice-mode chat: tell the coordinator+agent to pipeline TTS so
    // first-sentence audio ships alongside the streaming text. Without
    // this flag the agent falls through to its plain text path; the
    // client then has to fire a second /api/generate tool=tts (the
    // pre-Phase-A flow) which paid the LLM-complete-before-TTS-starts
    // tax. Search/image submits never carry voice_mode — search summary
    // streams the same way but speaking the synthesized summary plus
    // sources strip is out of scope for Phase A.
    body.voice_mode = true;
  }
  // Tag the typing bubble with a UUID up front so an offline-enqueue
  // can match the queue row back to its visible bubble on drain.
  const queueId = (crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : ('q-' + Date.now() + '-' + Math.random().toString(36).slice(2));
  typing.dataset.queueId = queueId;
  let gr;
  try {
    gr = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (_netErr) {
    // Network unreachable (offline, DNS down, captive portal). Stash
    // the send in IndexedDB and let the drain loop POST it once
    // connectivity comes back. We re-enable the composer immediately
    // so the user can keep drafting; the queued bubble stays put.
    try {
      await offlineQueue.enqueue({
        id: queueId,
        enqueued_at: Date.now(),
        conversation_id: state.currentId,
        payload: body,
      });
    } catch (_storeErr) {
      // IndexedDB refused (private window, quota, …). Surface the
      // original network failure instead of pretending the queue
      // worked — the user's prompt would otherwise vanish silently.
      const bubble = typing.querySelector('.bubble');
      bubble.classList.add('error');
      bubble.textContent = "Can't reach the server, and offline queue isn't available in this browser.";
      statusEl.textContent = '';
      submitBtn.disabled = false; textarea.disabled = false;
      return;
    }
    markBubbleQueued(typing);
    offlineQueue.registerSync();
    statusEl.textContent = 'queued — will send when online';
    submitBtn.disabled = false; textarea.disabled = false;
    return;
  }
  if (gr.status === 401) { location.href = '/login'; return; }
  if (!gr.ok) {
    // Try to read a JSON `detail` first — the coordinator and the
    // BFF proxy both wrap user-friendly errors that way (e.g. 503
    // "No community members are available right now"). Falls back
    // to the raw body if the response isn't JSON.
    let msg = '';
    try {
      const body = await gr.json();
      if (body && typeof body.detail === 'string') {
        msg = body.detail;
      } else if (body) {
        msg = JSON.stringify(body);
      }
    } catch (_e) {
      msg = await gr.text();
    }
    const bubble = typing.querySelector('.bubble');
    bubble.classList.add('error');
    // 503 means "no workers" — show the message verbatim, no status
    // prefix; the user doesn't need to see the HTTP code for that.
    bubble.textContent = gr.status === 503
      ? msg
      : ('error: ' + gr.status + (msg ? ' ' + msg : ''));
    statusEl.textContent = '';
    submitBtn.disabled = false; textarea.disabled = false;
    return;
  }
  const {job_id, assistant_message_id} = await gr.json();
  await streamIntoBubble(job_id, typing, statusEl, start, assistant_message_id);
  textarea.focus();
  // Invalidate this conversation's cache; next openConversation will
  // pull authoritative seq + message_ids from the server. The DOM
  // already reflects the final text via the streaming updates.
  msgCache.delete(state.currentId);
  // Refresh the sidebar so the title (set from the first prompt on
  // the coordinator) shows up.
  await refreshSidebar();
  document.querySelectorAll('.conv-item').forEach((el, i) => {
    el.classList.toggle('active', state.conversations[i] && state.conversations[i].conversation_id === state.currentId);
  });
};

// ---- offline send queue (Phase 5 of pwa-refactor.txt) ---------------
// When /api/generate fails because the browser is offline, the submit
// handler stashes the payload in IndexedDB and stamps the pending
// bubble with a queueId. The helpers below render the queued state
// and drain the queue once connectivity is restored.

function markBubbleQueued(wrap) {
  // Reuses the same .typing span the pending bubble normally shows so
  // styling stays consistent (muted italic). Adds a .queued class on
  // the bubble itself so a future stylesheet tweak could distinguish
  // them without restructuring the DOM.
  const bubble = wrap.querySelector('.bubble');
  if (!bubble) return;
  bubble.classList.add('queued');
  bubble.innerHTML = '<span class="typing">queued — will send when online</span>';
}

let drainInFlight = false;
async function drainQueue() {
  if (drainInFlight) return;
  if (!navigator.onLine) return;
  drainInFlight = true;
  try {
    let entries;
    try { entries = await offlineQueue.list(); }
    catch (_e) { return; }  // IDB unavailable; nothing we can do here
    if (!entries.length) return;
    // Oldest first so the user sees their sends drained in submit
    // order. getAll() doesn't guarantee insertion order across
    // browsers — sort explicitly.
    entries.sort((a, b) => (a.enqueued_at || 0) - (b.enqueued_at || 0));
    for (const entry of entries) {
      let typing = document.querySelector(
        `[data-queue-id="${entry.id}"]`,
      );
      // Bubble missing → either (a) we reloaded since enqueuing, or
      // (b) the user is looking at a different conversation. In case
      // (a) re-create the optimistic DOM if we're currently in the
      // matching conv; in case (b) skip this entry until they
      // navigate back (next openConversation triggers drainQueue
      // again).
      if (!typing) {
        if (state.currentId !== entry.conversation_id) continue;
        const pane = document.getElementById('chat-pane');
        const empty = document.getElementById('empty-state');
        if (empty) empty.remove();
        pane.appendChild(messageEl('user', entry.payload.prompt));
        typing = messageEl('assistant', '', {
          status: 'pending',
          pending_kind: entry.payload.tool || 'chat',
        });
        typing.dataset.queueId = entry.id;
        markBubbleQueued(typing);
        pane.appendChild(typing);
        pane.scrollTop = pane.scrollHeight;
      }
      let gr;
      try {
        gr = await fetch('/api/generate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(entry.payload),
        });
      } catch (_netErr) {
        // Still offline — leave this entry (and any after it) in the
        // queue for the next drain. Don't process the rest; if the
        // network just dropped again, hammering it is pointless.
        return;
      }
      if (gr.status === 401) {
        // Session expired between enqueue and drain. Surface as an
        // error bubble so the user understands the prompt didn't go
        // through; remove from queue (no amount of retrying fixes a
        // missing session).
        const bubble = typing.querySelector('.bubble');
        bubble.classList.remove('queued');
        bubble.classList.add('error');
        bubble.textContent = 'Session expired — please reload and sign in.';
        await offlineQueue.remove(entry.id).catch(() => {});
        continue;
      }
      if (!gr.ok) {
        // Server-side rejection (deleted conv, quota, validation, …).
        // Show the detail if there is one and drop the entry.
        let msg = '';
        try {
          const body = await gr.json();
          msg = (body && typeof body.detail === 'string')
            ? body.detail
            : JSON.stringify(body || {});
        } catch (_e) { try { msg = await gr.text(); } catch { msg = ''; } }
        const bubble = typing.querySelector('.bubble');
        bubble.classList.remove('queued');
        bubble.classList.add('error');
        bubble.textContent = 'error: ' + gr.status + (msg ? ' ' + msg : '');
        await offlineQueue.remove(entry.id).catch(() => {});
        continue;
      }
      const {job_id, assistant_message_id} = await gr.json();
      await offlineQueue.remove(entry.id).catch(() => {});
      const bubble = typing.querySelector('.bubble');
      bubble.classList.remove('queued');
      bubble.innerHTML = '<span class="typing">thinking…</span>';
      // Fire-and-forget — streamIntoBubble owns its own composer
      // lock and we want to start the next entry's POST while this
      // one is streaming (each is independent on the server side).
      streamIntoBubble(
        job_id, typing, null, Date.now(), assistant_message_id,
      );
    }
  } finally {
    drainInFlight = false;
  }
}

// Drain triggers, in priority order:
//   1. SW 'sync' wakeup → postMessage 'drain-queue' (Chrome/Android).
//   2. Browser 'online' event when connectivity comes back (everyone).
//   3. Tab regains focus while online (Safari especially — no Sync).
//   4. Initial page load (run after init() has resolved state.me).
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'drain-queue') drainQueue();
  });
}
window.addEventListener('online', () => drainQueue());
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') drainQueue();
});

// Bind the image-lightbox click delegate once at startup.
initImageGallery();

// PWA banners: push opt-in (when installed + push supported + permission
// default) and Android install prompt capture (beforeinstallprompt).
// No-op everywhere else. See installPrompt.js for the conditions.
initPWAPrompts();

// Kick off an initial drain so any sends queued from a previous tab
// session get retried as soon as the user opens the app. Deferred via
// setTimeout so init() (which is also async-scheduled at module load)
// has a chance to populate state.me + state.currentId before drain
// looks at them.
setTimeout(() => drainQueue(), 0);
