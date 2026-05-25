// ---- streaming engine + retry ----------------------------------------
// Polls /api/result/{jobId} and streams the accumulated text into a
// message element until the job reaches a terminal state. Owns the
// composer's enable/disable for the duration so both the fresh-submit
// path and the reload-resume path lock input the same way. Also
// owns the retry button + cooldown so an error bubble's "Retry" can
// re-enter the same loop under a new job_id.
//
// Circular import note: this module imports setBubbleContent /
// setImageBubble / renderSources from messageRenderer.js;
// messageRenderer.js imports makeRetryButton from here. ES module
// live bindings handle this fine — every cross-module reference is
// resolved at call time, never at module-evaluation time.

import {
  state, msgCache, searchModeByConv, setConvTokens,
} from './state.js';
import {
  makeReadAloudButton, onReadAloudClick,
} from './readAloud.js';
import {
  setBubbleContent, setImageBubble, renderSources,
} from './messageRenderer.js';
import { searchCheckbox, refreshComposerUI } from './composer.js';

export function makeRetryButton(messageId) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'retry-btn';
  btn.textContent = 'Retry';
  btn.onclick = () => retryMessage(messageId, btn);
  return btn;
}

export function startCooldown(btn, seconds, baseLabel) {
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

export async function retryMessage(messageId, btn) {
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
  if (state.currentId) msgCache.delete(state.currentId);
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
export async function streamIntoBubble(jobId, wrap, statusEl, startMs, messageId) {
  const myToken = Symbol('stream');
  state.activeStream = myToken;
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

  // Voice mode is handled entirely at terminal completion now (see the
  // auto-fire block down in the `terminal` branch). The streaming loop
  // itself stays read-aloud-agnostic — no per-tick chunking, no
  // sentence regex.

  function renderShown() {
    if (state.activeStream !== myToken) return;
    setBubbleContent(bubble, 'assistant', target.substring(0, shownChars));
    if (isPinnedToBottom) pane.scrollTop = pane.scrollHeight;
  }

  function typewriterTick(ts) {
    if (state.activeStream !== myToken) {
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
      if (state.activeStream !== myToken) return null;  // superseded
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
        // Per-message read-aloud button. Skip for image / error /
        // cancelled / empty turns. Uses the final target text (not
        // the markdown-rendered HTML) so Piper synthesizes from the
        // model's actual words. When voice mode is on, the button
        // also auto-fires — voice mode is just "auto-tap read-aloud
        // on every new response" now (see the read-aloud section
        // header for why we collapsed it to one path).
        const readMid = wrap.dataset.messageId;
        const finalText = (target || '').trim();
        if (readMid && finalText
            && res.status !== 'error'
            && res.status !== 'cancelled'
            && !res.image_path
            && !wrap.querySelector('.read-aloud-btn')) {
          const readBtn = makeReadAloudButton(readMid, finalText);
          wrap.appendChild(readBtn);
          if (state.voiceMode) {
            // Fire-and-forget — onReadAloudClick handles its own
            // loading state, errors, and playback. We don't await
            // because the surrounding streamIntoBubble has more
            // work (sources, statusEl, convTokens) and the audio is
            // independent of that bookkeeping.
            onReadAloudClick(readMid, finalText, readBtn);
          }
        }
        // Reverse-detection: if the rewrite classifier decided this
        // follow-up didn't need a search ("That's cool!" after a news
        // thread), the server rerouted to plain chat and set
        // search_was_skipped. Auto-uncheck the box so the next turn
        // defaults to chat — the user clearly winded down the search
        // topic and our sticky-mode bet should give up gracefully.
        if (res.search_was_skipped) {
          searchCheckbox.checked = false;
          searchModeByConv.set(state.currentId, false);
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
        // Roll this turn's tokens into the running chat total. Image jobs
        // report 0/0, so they no-op. The cache invalidation on submit
        // means the next conv-open recomputes from server-of-record, so
        // a wrong delta here self-heals on the next navigation.
        setConvTokens(
          state.convTokens
          + (res.prompt_tokens || 0)
          + (res.completion_tokens || 0),
        );
        return finalRes;
      }
    }
  } finally {
    clearStuckNotice();
    pane.removeEventListener('scroll', onPaneScroll);
    if (rafId !== null) cancelAnimationFrame(rafId);
    if (state.activeStream === myToken) {
      state.activeStream = null;
      submitBtn.disabled = false;
      ta.disabled = false;
    }
  }
}
