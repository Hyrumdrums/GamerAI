// ---- message rendering -----------------------------------------------
// DOM construction for chat message bubbles. Three flavors:
//   - user text bubble (literal text, no markdown)
//   - assistant text bubble (markdown via marked, sanitized via DOMPurify)
//   - assistant image bubble (PNG referenced via /api/images proxy)
//
// Also renders the numbered sources strip that hangs off search-tool
// answers. Retry buttons and read-aloud buttons live in their own
// modules (streamingEngine, readAloud) and are attached here.
//
// Circular import note: this module imports makeRetryButton from
// streamingEngine.js; streamingEngine.js imports setBubbleContent /
// setImageBubble / renderSources from this module. ES module live
// bindings handle this fine because every cross-module reference is
// used inside a function body (not at module-evaluation time).

import { makeReadAloudButton, isAssistantTextMessage } from './readAloud.js';
import { makeRetryButton } from './streamingEngine.js';

export function setBubbleContent(bubbleEl, role, text) {
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

export function setImageBubble(bubbleEl, imagePath, captionPrompt) {
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

export function messageEl(role, text, opts = {}) {
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
  if (role === 'assistant' && opts.message_id
      && isAssistantTextMessage(opts, text)) {
    wrap.appendChild(makeReadAloudButton(opts.message_id, text));
  }
  return wrap;
}

// Renders the numbered sources strip below an assistant bubble. Each
// entry links to the source URL; we render the domain (not the full
// URL) so a long URL doesn't blow out the bubble width. The model
// emits inline [1][2] citations that line up with these numbers.
export function renderSources(wrap, sources) {
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
