// ---- state ------------------------------------------------------------
let me = null;
let conversations = [];   // [{conversation_id, title, updated_at, ...}]
let currentId = null;     // active conversation_id, or null = brand-new
// In-memory message cache so switching between already-opened
// conversations is instant. Keyed by conversation_id; value is the
// full messages[] array. Cleared on page refresh — not a substitute
// for offline persistence (see project-gaps.md). Each completed
// turn appends to the cache directly so we don't refetch on every
// new message.
const msgCache = new Map();

// ---- bootstrap --------------------------------------------------------
async function init() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { location.href = '/login'; return; }
    me = await r.json();
    document.getElementById('who').textContent =
      `${me.email || me.member_id || 'signed in'} · ${me.role || 'member'}`;
    if (me.role === 'admin') {
      document.getElementById('adminlink').hidden = false;
    }
  } catch { location.href = '/login'; return; }
  await refreshSidebar();
  if (conversations.length > 0) {
    openConversation(conversations[0].conversation_id);
  }
}
init();

// ---- sidebar ----------------------------------------------------------
async function refreshSidebar() {
  const r = await fetch('/api/conversations');
  if (!r.ok) return;
  const body = await r.json();
  conversations = body.conversations || [];
  const list = document.getElementById('conv-list');
  list.innerHTML = '';
  for (const c of conversations) {
    const div = document.createElement('div');
    div.className = 'conv-item' + (c.conversation_id === currentId ? ' active' : '');
    // Collapse whitespace runs (including newlines) so a paste-bombed
    // title from the empty-state DOM still renders as a single ellipsised
    // line instead of leaking blank space into the layout.
    const raw = c.title || '(untitled)';
    div.textContent = raw.replace(/\s+/g, ' ').trim() || '(untitled)';
    div.title = raw;
    div.onclick = () => openConversation(c.conversation_id);
    list.appendChild(div);
  }
}

document.getElementById('new-chat').onclick = () => {
  currentId = null;
  document.getElementById('chat-pane').innerHTML =
    '<div class="empty"><h2>What\'s on your mind?</h2><div>Start a new conversation by typing below.</div></div>';
  document.querySelectorAll('.conv-item').forEach(el => el.classList.remove('active'));
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('prompt').focus();
};

// Hamburger toggle for the mobile drawer. On desktop the hamburger is
// hidden via CSS so this handler is effectively unreachable there.
document.getElementById('hamburger').onclick = () => {
  document.getElementById('sidebar').classList.toggle('open');
};

// ---- conversation rendering ------------------------------------------
async function openConversation(id) {
  currentId = id;
  // Cancel any in-flight stream for the conversation we're leaving;
  // renderMessages may start a new one if the conversation we're
  // opening has a pending turn of its own.
  activeStream = null;
  document.getElementById('submit').disabled = false;
  document.getElementById('prompt').disabled = false;
  // On mobile, close the drawer once a conversation is picked. CSS
  // makes this a no-op on desktop where the sidebar is persistent.
  document.getElementById('sidebar').classList.remove('open');
  // Highlight the right sidebar entry.
  document.querySelectorAll('.conv-item').forEach((el, i) => {
    el.classList.toggle('active', conversations[i] && conversations[i].conversation_id === id);
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
  if (render || currentId === id) renderMessages(messages);
}

// Tracks the in-flight stream so a second submit (or a conversation
// switch) doesn't leave two pollers racing on the same bubble.
let activeStream = null;

function renderMessages(messages) {
  const pane = document.getElementById('chat-pane');
  pane.innerHTML = '';
  if (messages.length === 0) {
    pane.innerHTML = '<div class="empty"><h2>(empty)</h2></div>';
    return;
  }
  let pendingEl = null;
  let pendingJobId = null;
  let pendingMessageId = null;
  for (const m of messages) {
    const el = messageEl(m.role, m.text, {
      status: m.status, message_id: m.message_id,
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

function setBubbleContent(bubbleEl, role, text) {
  // Shared rendering for both first-render and streaming-update.
  // Assistant text goes through marked + DOMPurify; user text stays
  // literal so pasted code shows verbatim. DOMPurify is mandatory —
  // skipping it would let a malicious model emit <img onerror=...>.
  if (role === 'assistant' && window.marked && window.DOMPurify) {
    bubbleEl.innerHTML = window.DOMPurify.sanitize(
      window.marked.parse(text || ''),
    );
  } else {
    bubbleEl.textContent = text || '';
  }
}

function messageEl(role, text, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  wrap.dataset.role = role;
  if (opts.message_id) wrap.dataset.messageId = opts.message_id;
  const r = document.createElement('div');
  r.className = 'role';
  r.textContent = role;
  const b = document.createElement('div');
  b.className = 'bubble';
  if (opts.status === 'error') {
    b.classList.add('error');
    b.textContent = text || 'Generation failed.';
  } else if (opts.status === 'pending' && !text) {
    b.innerHTML = '<span class="typing">thinking…</span>';
  } else {
    setBubbleContent(b, role, text);
  }
  wrap.appendChild(r);
  wrap.appendChild(b);
  if (opts.status === 'error' && role === 'assistant' && opts.message_id) {
    wrap.appendChild(makeRetryButton(opts.message_id));
  }
  return wrap;
}

function makeRetryButton(messageId) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'retry-btn';
  btn.textContent = 'Retry';
  btn.onclick = () => retryMessage(messageId, btn);
  return btn;
}

function startCooldown(btn, seconds, baseLabel) {
  btn.disabled = true;
  let s = Math.max(1, seconds | 0);
  const tick = () => {
    if (s <= 0) {
      btn.textContent = baseLabel;
      btn.disabled = false;
      return;
    }
    btn.textContent = `${baseLabel} (${s}s)`;
    s -= 1;
    setTimeout(tick, 1000);
  };
  tick();
}

async function retryMessage(messageId, btn) {
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = 'Retrying…';
  let r;
  try {
    r = await fetch('/api/messages/' + messageId + '/retry', {method: 'POST'});
  } catch (e) {
    btn.textContent = originalLabel;
    btn.disabled = false;
    return;
  }
  if (r.status === 429) {
    const ra = parseInt(r.headers.get('Retry-After') || '10', 10);
    startCooldown(btn, ra, 'Retry');
    return;
  }
  if (!r.ok) {
    btn.textContent = 'Retry failed';
    setTimeout(() => { btn.textContent = originalLabel; btn.disabled = false; }, 1500);
    return;
  }
  const body = await r.json();
  // Convert the error bubble back to a pending bubble and resume
  // streaming under the new job_id. We deliberately drop the cache
  // entry for this conversation so future opens re-fetch from server.
  if (currentId) msgCache.delete(currentId);
  const wrap = btn.closest('.msg');
  const bubble = wrap.querySelector('.bubble');
  bubble.classList.remove('error');
  bubble.innerHTML = '<span class="typing">thinking…</span>';
  btn.remove();
  streamIntoBubble(body.job_id, wrap, null, Date.now(), messageId);
}

// Poll /api/result/{jobId} and stream the accumulated text into the
// given message element until the job reaches a terminal state. Owns
// composer enable/disable for the duration, so both the fresh-submit
// path and the reload-resume path lock input the same way. When
// statusEl + startMs are passed (the fresh-submit case), updates the
// status line with timing on completion.
async function streamIntoBubble(jobId, wrap, statusEl, startMs, messageId) {
  const myToken = Symbol('stream');
  activeStream = myToken;
  if (messageId) wrap.dataset.messageId = messageId;
  const bubble = wrap.querySelector('.bubble');
  const pane = document.getElementById('chat-pane');
  const submitBtn = document.getElementById('submit');
  const ta = document.getElementById('prompt');
  submitBtn.disabled = true;
  ta.disabled = true;
  // Stick to the bottom only if the user is already near the bottom;
  // otherwise let them scroll up to re-read prior turns without us
  // yanking the viewport on every token batch.
  const isNearBottom = () =>
    pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 64;
  try {
    while (true) {
      await new Promise(r => setTimeout(r, 200));
      if (activeStream !== myToken) return null;  // superseded
      let res;
      try {
        res = await fetch('/api/result/' + jobId).then(r => r.json());
      } catch (e) {
        continue;
      }
      if (res.text) {
        const stick = isNearBottom();
        setBubbleContent(bubble, 'assistant', res.text);
        if (stick) pane.scrollTop = pane.scrollHeight;
      }
      if (res.done || res.status === 'complete' || res.status === 'error') {
        if (res.status === 'error') {
          bubble.classList.add('error');
          bubble.textContent = res.text || res.error || 'Generation failed.';
          const mid = wrap.dataset.messageId;
          if (mid && !wrap.querySelector('.retry-btn')) {
            wrap.appendChild(makeRetryButton(mid));
          }
        } else if (!res.text) {
          setBubbleContent(bubble, 'assistant', '(empty)');
        }
        if (statusEl && startMs) {
          const dt = ((Date.now() - startMs) / 1000).toFixed(1);
          statusEl.textContent =
            res.status === 'error'
              ? `failed in ${dt}s`
              : `done in ${dt}s · ${res.completion_tokens || 0} tokens · ${res.worker_id || 'unknown worker'}`;
        }
        return res;
      }
    }
  } finally {
    if (activeStream === myToken) {
      activeStream = null;
      submitBtn.disabled = false;
      ta.disabled = false;
    }
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

  // If this is a brand-new chat (no currentId), create one first.
  if (!currentId) {
    const cr = await fetch('/api/conversations', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    if (!cr.ok) {
      statusEl.textContent = 'failed to create conversation';
      submitBtn.disabled = false; textarea.disabled = false;
      return;
    }
    currentId = (await cr.json()).conversation_id;
  }

  // Append the user message optimistically so it shows up right away.
  const pane = document.getElementById('chat-pane');
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();
  pane.appendChild(messageEl('user', prompt));
  const typing = messageEl('assistant', '', {status: 'pending'});
  pane.appendChild(typing);
  pane.scrollTop = pane.scrollHeight;
  textarea.value = '';
  textarea.style.height = 'auto';

  // Submit + stream.
  statusEl.textContent = 'submitting…';
  const start = Date.now();
  const gr = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, conversation_id: currentId}),
  });
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
  msgCache.delete(currentId);
  // Refresh the sidebar so the title (set from the first prompt on
  // the coordinator) shows up.
  await refreshSidebar();
  document.querySelectorAll('.conv-item').forEach((el, i) => {
    el.classList.toggle('active', conversations[i] && conversations[i].conversation_id === currentId);
  });
};
