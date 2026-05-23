// ---- state ------------------------------------------------------------
let me = null;
let conversations = [];   // [{conversation_id, title, updated_at, ...}]
let currentId = null;     // active conversation_id, or null = brand-new
// 'chat' or 'image' — picked via the tool toggle. Defaults to chat so
// every existing user lands on the prior UX unless they explicitly
// switch. Persists in-memory only; we don't put it on the conversation
// because users want to flip mid-thread (write a paragraph then ask
// for an illustration).
// In-memory message cache so switching between already-opened
// conversations is instant. Keyed by conversation_id; value is the
// full messages[] array. Cleared on page refresh — not a substitute
// for offline persistence (see project-gaps.md). Each completed
// turn appends to the cache directly so we don't refetch on every
// new message.
const msgCache = new Map();

// Per-conversation search-mode state. Search is sticky within a
// conversation (unlike image, which is one-shot per turn) — once you
// check it for the first follow-up, the natural flow is to keep
// drilling in with more search-grounded questions. Auto-unchecking
// every turn was forcing the user to re-check and they kept
// forgetting. Keyed by conversation_id; the "brand new chat" state
// (currentId === null) gets its own slot via the null key.
const searchModeByConv = new Map();

// ---- bootstrap --------------------------------------------------------
async function init() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { location.href = '/login'; return; }
    me = await r.json();
    const whoEl = document.getElementById('who');
    whoEl.textContent = me.username || me.email || me.member_id || 'signed in';
    whoEl.href = '/account';
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
  if (currentId === id) {
    currentId = null;
    document.getElementById('chat-pane').innerHTML =
      '<div class="empty"><h2>What\'s on your mind?</h2>' +
      '<div>Start a new conversation by typing below.</div></div>';
  }
  await refreshSidebar();
}

document.getElementById('new-chat').onclick = () => {
  currentId = null;
  // Brand-new chat defaults to plain chat mode — don't inherit the
  // last conversation's search-on state. restoreModeFor is defined
  // below; function declarations are hoisted, so calling it from this
  // click handler is fine even before the file finishes parsing.
  searchModeByConv.delete(null);
  restoreModeFor(null);
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
  // Restore the per-conversation search-mode state. If the user had
  // search on the last time they were in this conversation, the
  // checkbox flips back on so a follow-up automatically searches —
  // no need to remember to re-check it.
  restoreModeFor(id);
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

function setImageBubble(bubbleEl, imagePath, captionPrompt) {
  // Replace the bubble's contents with an <img> referencing the
  // generated PNG. Path is the basename returned by the coordinator
  // (job_id.png) — we proxy through /api/images so the bearer is
  // attached server-side; <img> can't carry custom headers.
  bubbleEl.innerHTML = '';
  if (captionPrompt) {
    const p = document.createElement('div');
    p.className = 'img-prompt';
    p.textContent = captionPrompt;
    bubbleEl.appendChild(p);
  }
  const img = document.createElement('img');
  img.className = 'generated';
  img.alt = captionPrompt || 'generated image';
  img.src = '/api/images/' + encodeURIComponent(imagePath);
  bubbleEl.appendChild(img);
}

function messageEl(role, text, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  wrap.dataset.role = role;
  if (opts.message_id) wrap.dataset.messageId = opts.message_id;
  // Tag the bubble with the tool kind so the stuck-job UX can pick a
  // sensible "this is taking longer than normal" threshold — chat
  // streams visibly, image is opaque until the PNG arrives, so each
  // gets its own no-progress budget.
  if (opts.pending_kind === 'image') wrap.dataset.tool = 'image';
  else if (opts.pending_kind === 'search') wrap.dataset.tool = 'search';
  else if (opts.tool) wrap.dataset.tool = opts.tool;
  const r = document.createElement('div');
  r.className = 'role';
  r.textContent = role;
  const b = document.createElement('div');
  b.className = 'bubble';
  if (opts.status === 'error') {
    b.classList.add('error');
    b.textContent = text || 'Generation failed.';
  } else if (opts.status === 'pending' && !text) {
    // Pending bubble label by tool kind — image says "drawing…",
    // search says "searching the web…", chat says "thinking…". The
    // search label is a deliberate hint that the answer will take a
    // moment longer than a normal chat reply (DDG + optional fetch
    // before the model even starts streaming).
    let typing = 'thinking…';
    if (opts.pending_kind === 'image') typing = 'drawing…';
    else if (opts.pending_kind === 'search') typing = 'searching the web…';
    b.innerHTML = `<span class="typing">${typing}</span>`;
  } else if (opts.image_path) {
    // Persisted image turn — render the PNG bubble. The text field is
    // a "[image: <prompt>]" sentinel from the coordinator so a no-CSS
    // fallback still shows the prompt; we strip the wrapper to get
    // the original caption.
    const caption = (text || '').replace(/^\[image:\s*/, '').replace(/\]$/, '');
    setImageBubble(b, opts.image_path, caption);
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

// Min character-reveal rate for the typewriter render. The browser
// polls /api/result every 200ms and gets the latest accumulated text,
// but rendering all of it at once feels jarring on a fast model and
// invisible when the agent doesn't stream. The typewriter advances
// the visible substring toward the latest server text at >= this
// rate, so even an instant response shows a brief reveal animation;
// when partials arrive faster than this rate the typewriter just
// matches arrival (no artificial slowdown).
const TYPEWRITER_CHARS_PER_SECOND = 90;
// Stuck-job thresholds: how long to wait with zero progress before
// surfacing the "this may be taking longer than normal" notice with
// a Cancel button. Chat normally streams within a second or two so
// 60s of dead air is suspicious; image is opaque until the PNG
// finishes generating, so we give it 5min before warning. The
// warning itself doesn't cancel anything — the worker keeps grinding
// and may still finish — it just gives the user an opt-in escape
// hatch, per project policy that the reaper extends deadlines
// indefinitely on healthy heartbeats.
const STUCK_MS_CHAT = 60_000;
const STUCK_MS_IMAGE = 300_000;
// Search has a longer pre-stream phase (DDG + optional fetch+extract
// of 5 pages) before the LLM produces any partials. 2 minutes is
// generous enough that comprehensive-mode jobs on a slow link don't
// trip the "may be stuck" warning prematurely, while still bounded
// enough that a truly hung worker is flagged.
const STUCK_MS_SEARCH = 120_000;
// Minimum chars revealed per animation frame, so we don't get stuck
// at sub-pixel advance on a 60fps display when CPS is low. 1 char/
// frame at 60fps = 60 cps floor, plenty for readability.
const TYPEWRITER_MIN_CHARS_PER_FRAME = 1;

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
  // Scroll-pin state. Single boolean updated on every pane scroll
  // event (user OR programmatic — a programmatic scroll-to-bottom
  // just re-confirms pinned=true, which is harmless). Replaces the
  // old per-frame isNearBottom() check, which raced with the
  // typewriter's 60fps innerHTML rewrites and made it nearly
  // impossible to scroll up through a long streaming bubble — the
  // pane reflow would fire faster than the user could drag their
  // wheel past the 64px threshold.
  //
  // Initial state assumes pinned (we want auto-follow at the start
  // of a new message). The first user scroll-up event flips it; a
  // user scroll-back-to-bottom flips it back.
  let isPinnedToBottom = true;
  const onPaneScroll = () => {
    const distFromBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight;
    isPinnedToBottom = distFromBottom < 32;
  };
  pane.addEventListener('scroll', onPaneScroll, { passive: true });

  // Typewriter state. ``target`` is the most recent server-side text;
  // ``shownChars`` is how many characters we've revealed so far.
  // A requestAnimationFrame loop advances ``shownChars`` toward
  // ``target.length`` at TYPEWRITER_CHARS_PER_SECOND. When the server
  // signals done, we keep the rAF loop running until the visible
  // substring catches up to the final text, then finalize the bubble.
  let target = '';
  let shownChars = 0;
  let serverDone = false;
  let rafId = null;
  let lastFrameTs = 0;
  let finalizeResolver = null;
  const finalizePromise = new Promise(r => { finalizeResolver = r; });

  function renderShown() {
    if (activeStream !== myToken) return;
    setBubbleContent(bubble, 'assistant', target.substring(0, shownChars));
    if (isPinnedToBottom) pane.scrollTop = pane.scrollHeight;
  }

  function typewriterTick(ts) {
    if (activeStream !== myToken) {
      rafId = null;
      return;
    }
    if (!lastFrameTs) lastFrameTs = ts;
    const dtMs = ts - lastFrameTs;
    lastFrameTs = ts;
    if (shownChars < target.length) {
      const advance = Math.max(
        TYPEWRITER_MIN_CHARS_PER_FRAME,
        Math.round((TYPEWRITER_CHARS_PER_SECOND * dtMs) / 1000),
      );
      shownChars = Math.min(target.length, shownChars + advance);
      renderShown();
    }
    // Keep ticking while either (a) we still have chars to reveal or
    // (b) the server hasn't said done yet (so more text may arrive).
    if (shownChars < target.length || !serverDone) {
      rafId = requestAnimationFrame(typewriterTick);
    } else {
      rafId = null;
      if (finalizeResolver) {
        const r = finalizeResolver; finalizeResolver = null; r();
      }
    }
  }

  function startTypewriter() {
    if (rafId !== null) return;
    lastFrameTs = 0;
    rafId = requestAnimationFrame(typewriterTick);
  }

  // Stuck-job UX state. ``lastProgressMs`` is the wall-clock of the
  // last time we saw any progress (text or partial bytes). Once the
  // gap exceeds the per-tool threshold, the wrap grows a "may be
  // taking longer than normal" notice with a Cancel button. The
  // notice goes away if progress resumes (typewriter advances, server
  // returns text). Cancel POSTs /api/cancel/{jobId} and lets the
  // existing polling loop discover the terminal 'cancelled' status.
  let lastProgressMs = Date.now();
  let stuckNoticeShown = false;
  let cancelRequested = false;
  const isImage = wrap.dataset.tool === 'image';
  const isSearch = wrap.dataset.tool === 'search';
  let stuckThresholdMs = STUCK_MS_CHAT;
  if (isImage) stuckThresholdMs = STUCK_MS_IMAGE;
  else if (isSearch) stuckThresholdMs = STUCK_MS_SEARCH;

  function clearStuckNotice() {
    if (!stuckNoticeShown) return;
    const node = wrap.querySelector('.stuck-notice');
    if (node) node.remove();
    stuckNoticeShown = false;
  }

  function showStuckNotice() {
    if (stuckNoticeShown || cancelRequested) return;
    stuckNoticeShown = true;
    const notice = document.createElement('div');
    notice.className = 'stuck-notice';
    const txt = document.createElement('span');
    txt.className = 'stuck-text';
    if (isImage) {
      txt.textContent = 'Image generation is taking longer than normal. Something may be wrong, or your worker just needs more time.';
    } else if (isSearch) {
      txt.textContent = 'The web search is taking longer than normal. The page fetches may be slow or your worker may be stuck.';
    } else {
      txt.textContent = 'This is taking longer than normal. Something may be wrong, or your worker just needs more time.';
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cancel-btn';
    btn.textContent = 'Cancel';
    btn.onclick = async () => {
      if (cancelRequested) return;
      cancelRequested = true;
      btn.disabled = true;
      btn.textContent = 'Cancelling…';
      try {
        await fetch('/api/cancel/' + jobId, { method: 'POST' });
      } catch (e) {
        // Network failure here is OK — coordinator may still have
        // cancelled before the response was returned. If not, the
        // user can refresh and try again. We don't surface the
        // error because the polling loop will see the terminal
        // status either way.
      }
    };
    notice.appendChild(txt);
    notice.appendChild(btn);
    wrap.appendChild(notice);
  }

  let finalRes = null;
  try {
    while (true) {
      await new Promise(r => setTimeout(r, 200));
      if (activeStream !== myToken) return null;  // superseded
      let res;
      try {
        res = await fetch('/api/result/' + jobId).then(r => r.json());
      } catch (e) {
        // Network blip — don't reset the progress clock (a wedged
        // network shouldn't suppress the stuck warning), just keep
        // polling. The threshold check below still fires.
        if (Date.now() - lastProgressMs > stuckThresholdMs) {
          showStuckNotice();
        }
        continue;
      }
      const progressed = res.text && res.text.length > target.length;
      if (progressed) {
        target = res.text;
        lastProgressMs = Date.now();
        clearStuckNotice();
        startTypewriter();
      } else if (Date.now() - lastProgressMs > stuckThresholdMs) {
        showStuckNotice();
      }
      const terminal = res.done
        || res.status === 'complete'
        || res.status === 'error'
        || res.status === 'cancelled';
      if (terminal) {
        finalRes = res;
        clearStuckNotice();
        // Take the final server text as the authoritative target and
        // let the typewriter finish revealing.
        if (res.text) target = res.text;
        serverDone = true;
        startTypewriter();
        if (rafId !== null) {
          await finalizePromise;
        }
        if (res.status === 'cancelled') {
          // The user cancelled (or someone with the same session
          // cancelled in another tab). Render as a soft, non-retryable
          // bubble — no error styling, no retry button.
          bubble.classList.remove('error');
          bubble.classList.add('cancelled');
          bubble.textContent = res.text || 'Cancelled.';
        } else if (res.status === 'error') {
          bubble.classList.add('error');
          bubble.textContent = res.text || res.error || 'Generation failed.';
          const mid = wrap.dataset.messageId;
          if (mid && !wrap.querySelector('.retry-btn')) {
            wrap.appendChild(makeRetryButton(mid));
          }
        } else if (res.image_path) {
          // Image job complete — swap the typewriter contents for the
          // PNG bubble. text is the "[image: <prompt>]" sentinel; pull
          // the prompt back out for the caption.
          const caption = (res.text || '')
            .replace(/^\[image:\s*/, '').replace(/\]$/, '');
          setImageBubble(bubble, res.image_path, caption);
        } else if (!res.text) {
          setBubbleContent(bubble, 'assistant', '(empty)');
        }
        // Search jobs come back with a sources[] list. Render it once
        // here at completion (re-rendering on every partial would
        // flicker the bubble and the sources don't change mid-stream
        // anyway — the server side has them in hand before the LLM
        // starts).
        if (res.sources && res.sources.length) {
          renderSources(wrap, res.sources);
        }
        // Reverse-detection: if the rewrite classifier decided this
        // follow-up didn't need a search ("That's cool!" after a news
        // thread), the server rerouted to plain chat and set
        // search_was_skipped. Auto-uncheck the box so the next turn
        // defaults to chat — the user clearly winded down the search
        // topic and our sticky-mode bet should give up gracefully.
        if (res.search_was_skipped) {
          searchCheckbox.checked = false;
          searchModeByConv.set(currentId, false);
          refreshComposerUI();
        }
        if (statusEl && startMs) {
          const dt = ((Date.now() - startMs) / 1000).toFixed(1);
          let label;
          if (res.status === 'cancelled') label = `cancelled after ${dt}s`;
          else if (res.status === 'error') label = `failed in ${dt}s`;
          else label = `done in ${dt}s · ${res.completion_tokens || 0} tokens · ${res.worker_id || 'unknown worker'}`;
          statusEl.textContent = label;
        }
        return finalRes;
      }
    }
  } finally {
    clearStuckNotice();
    pane.removeEventListener('scroll', onPaneScroll);
    if (rafId !== null) cancelAnimationFrame(rafId);
    if (activeStream === myToken) {
      activeStream = null;
      submitBtn.disabled = false;
      ta.disabled = false;
    }
  }
}

// ---- image / search toggles ------------------------------------------
// Two checkboxes, mutually exclusive (checking one hides the other —
// there's no use case for "image of search results"). Different
// stickiness contracts:
//
// - Image: one-shot per turn. Auto-clears after submit so an
//   accidental left-on can't burn 5 contributor GPU jobs in a row.
// - Search: STICKY per conversation. Once you've checked it for a
//   topic, the natural drill-in flow ("what about Europe?" → "any
//   specific examples?") wants search on every follow-up. Auto-
//   unchecking forced re-checks every turn and the user kept
//   forgetting. Now the checkbox stays in whatever state the user
//   last set it for the current conversation, persisted in
//   searchModeByConv; switching to a different conversation restores
//   that conv's state; "+ New chat" resets to off.
//
// Search misfires are cheap (one DDG call + a normal chat turn), so
// the worst case of leaving it on is much milder than the image case.
const imageCheckbox = document.getElementById('tool-image-cb');
const searchCheckbox = document.getElementById('tool-search-cb');
const imageWrap = document.getElementById('image-toggle-wrap');
const searchWrap = document.getElementById('search-toggle-wrap');
const searchModeWrap = document.getElementById('search-mode-wrap');

function refreshComposerUI() {
  // Visibility: whichever checkbox is checked claims the row alone.
  imageWrap.hidden = searchCheckbox.checked;
  searchWrap.hidden = imageCheckbox.checked;
  // Sub-toggle (fast vs comprehensive) only relevant for search.
  searchModeWrap.hidden = !searchCheckbox.checked;
  // Placeholder cues the user about the active mode.
  const ta = document.getElementById('prompt');
  if (imageCheckbox.checked) {
    ta.placeholder = 'Describe the image you want…';
  } else if (searchCheckbox.checked) {
    ta.placeholder = 'Search the web for…';
  } else {
    ta.placeholder = 'Message GamerAI...';
  }
}

// Persist the current search-checkbox state under the current
// conversation key. Null key is the "brand new chat" slot — most
// users start a search and immediately submit, so capturing this
// means the conv created on submit inherits the right mode.
function persistSearchMode() {
  searchModeByConv.set(currentId, searchCheckbox.checked);
}

// Restore the search checkbox for the conversation we're switching
// to. Image is always restored to off (one-shot, never sticky).
// Called from openConversation / new-chat / right after a brand-new
// conversation is created on submit.
function restoreModeFor(convId) {
  const want = !!searchModeByConv.get(convId);
  searchCheckbox.checked = want;
  imageCheckbox.checked = false;
  refreshComposerUI();
}

imageCheckbox.addEventListener('change', () => {
  // Mutually exclusive — checking image clears search and vice versa.
  if (imageCheckbox.checked) searchCheckbox.checked = false;
  persistSearchMode();
  refreshComposerUI();
});
searchCheckbox.addEventListener('change', () => {
  if (searchCheckbox.checked) imageCheckbox.checked = false;
  persistSearchMode();
  refreshComposerUI();
});
refreshComposerUI();

function selectedSearchMode() {
  // Reads the radio group rather than tracking state — radios are
  // the source of truth and there are only two of them.
  const checked = document.querySelector(
    'input[name="search-mode"]:checked',
  );
  return checked ? checked.value : 'fast';
}

// Renders the numbered sources strip below an assistant bubble. Each
// entry links to the source URL; we render the domain (not the full
// URL) so a long URL doesn't blow out the bubble width. The model
// emits inline [1][2] citations that line up with these numbers.
function renderSources(wrap, sources) {
  if (!sources || !sources.length) return;
  const bubble = wrap.querySelector('.bubble');
  if (!bubble) return;
  // Replace any prior sources block (re-render on stream completion).
  const old = bubble.querySelector('.sources');
  if (old) old.remove();
  const box = document.createElement('div');
  box.className = 'sources';
  const label = document.createElement('div');
  label.className = 'sources-label';
  label.textContent = 'Sources';
  box.appendChild(label);
  const ol = document.createElement('ol');
  for (const s of sources) {
    const li = document.createElement('li');
    li.value = s.n || 0;
    if (s.url) {
      const a = document.createElement('a');
      a.href = s.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = s.title || s.domain || s.url;
      li.appendChild(a);
      if (s.domain && s.title && s.domain !== s.title) {
        const dom = document.createElement('span');
        dom.textContent = ' — ' + s.domain;
        li.appendChild(dom);
      }
    } else {
      li.textContent = s.title || '(unknown)';
    }
    ol.appendChild(li);
  }
  box.appendChild(ol);
  bubble.appendChild(box);
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
  // Hold the pre-create checkbox state so we can copy it onto the
  // freshly-minted conversation id below — without this, a "+ New
  // chat → check search → submit" flow would lose the sticky bit.
  const wasNewChat = !currentId;
  const newChatSearchState = wasNewChat ? searchCheckbox.checked : null;
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
    if (newChatSearchState !== null) {
      searchModeByConv.set(currentId, newChatSearchState);
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
  const body = {prompt, conversation_id: currentId};
  if (submitTool === 'image') {
    body.tool = 'image';
  } else if (submitTool === 'search') {
    body.tool = 'search';
    body.search_mode = submitSearchMode;
  }
  const gr = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
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
