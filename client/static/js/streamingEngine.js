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

// Voice-mode chat: server splits the response into two TTS chunks
// (first sentence + rest) and ships them with the streaming text. The
// client suppresses text reveal until first-sentence audio actually
// plays, then reveals up through the sentence boundary so listener +
// reader stay in lockstep. Ports the same VOICE_SENTENCE_RE the agent
// uses on its side so "first sentence" is the same span on both sides.
const VOICE_SENTENCE_RE = /[.!?]+["')\]]*(?:\s+|$)|\n+/;

function firstSentenceEndIndex(text) {
  if (!text) return 0;
  const m = VOICE_SENTENCE_RE.exec(text);
  if (!m) return 0;
  return m.index + m[0].length;
}

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

  // Voice-mode pipelining. When the user submitted with voice mode on,
  // the agent ships first-sentence audio as a partial result alongside
  // the streaming text and "rest" audio in the final result. We hold
  // text rendering until first audio actually plays — voice mode is
  // explicitly the "slower but synchronized" mode, and showing text
  // ahead of audio breaks the illusion the user opted into. Snapshot
  // at start so a mid-stream toggle of voiceMode doesn't change this
  // run's behavior.
  const voice = {
    enabled: !!state.voiceMode,
    firstB64: null,        // base64 audio for sentence 1 (or whole short response)
    restB64: null,         // base64 audio for the rest, when present
    firstStarted: false,   // first chunk's <audio> has fired 'play'
    restStarted: false,
    revealEndIdx: 0,       // how far text reveal is allowed to advance
    audioEl: null,         // currently-playing element (so we can stop on cancel)
  };

  function renderShown() {
    if (state.activeStream !== myToken) return;
    // Voice mode pre-audio: leave the pending "thinking…" placeholder
    // in place. The first audio's 'play' event lifts revealEndIdx and
    // re-enters renderShown, at which point the bubble swaps to the
    // model text.
    if (voice.enabled && !voice.firstStarted) return;
    const limit = voice.enabled
      ? Math.min(shownChars, voice.revealEndIdx)
      : shownChars;
    setBubbleContent(bubble, 'assistant', target.substring(0, limit));
    if (isPinnedToBottom) pane.scrollTop = pane.scrollHeight;
  }

  function playVoiceChunk(b64, which) {
    // Wraps an HTMLAudioElement around a base64 WAV and resolves when
    // playback ends (or errors — voice mode degrades to text-only on
    // any audio failure so the user isn't stranded). 'which' is 'first'
    // or 'rest' for state book-keeping. The promise resolution drives
    // the rest-chunk handoff and the final reveal.
    return new Promise(resolve => {
      const a = new Audio('data:audio/wav;base64,' + b64);
      voice.audioEl = a;
      const done = () => {
        if (voice.audioEl === a) voice.audioEl = null;
        resolve();
      };
      a.onended = done;
      a.onerror = done;
      a.onplay = () => {
        if (which === 'first') {
          voice.firstStarted = true;
          // Reveal up through the first sentence boundary in whatever
          // text has accumulated so far. If the agent's partial hasn't
          // caught up with the sentence yet (small race), fall back to
          // revealing everything we have — better to show extra than
          // to lock the bubble visually.
          const idx = firstSentenceEndIndex(target);
          voice.revealEndIdx = idx > 0 ? idx : target.length;
        } else {
          voice.restStarted = true;
          voice.revealEndIdx = target.length;
        }
        renderShown();
      };
      a.play().catch(done);
    });
  }

  async function runVoicePlayback() {
    // Sequencer: first chunk plays to completion, then rest chunk
    // (when available) plays. If first audio finishes before _rest
    // has landed yet (agent still synthesizing it while sentence 1
    // plays), poll briefly until either _rest arrives or the job
    // reaches terminal. The polling loop sets serverDone in the
    // terminal branch before awaiting voicePromise, so no deadlock.
    if (voice.firstB64) await playVoiceChunk(voice.firstB64, 'first');
    while (!voice.restB64 && !serverDone) {
      await new Promise(r => setTimeout(r, 100));
    }
    if (voice.restB64) await playVoiceChunk(voice.restB64, 'rest');
  }
  let voicePromise = null;

  function typewriterTick(ts) {
    if (state.activeStream !== myToken) {
      rafId = null;
      return;
    }
    if (!lastFrameTs) lastFrameTs = ts;
    const dtMs = ts - lastFrameTs;
    lastFrameTs = ts;
    // In voice mode the per-chunk audio 'play' event sets the reveal
    // ceiling; the typewriter just races toward that ceiling instead
    // of target.length. Pre-first-audio the ceiling is 0, so reveal
    // stays parked — letting the "thinking…" placeholder hold.
    const ceiling = voice.enabled
      ? Math.min(target.length, voice.revealEndIdx)
      : target.length;
    if (shownChars < ceiling) {
      const advance = Math.max(
        TYPEWRITER_MIN_CHARS_PER_FRAME,
        Math.round((TYPEWRITER_CHARS_PER_SECOND * dtMs) / 1000),
      );
      shownChars = Math.min(ceiling, shownChars + advance);
      renderShown();
    }
    // Keep ticking while either (a) we still have chars to reveal or
    // (b) the server hasn't said done yet (so more text may arrive).
    // In voice mode we also keep ticking until revealEndIdx catches up
    // to target.length, since audio events bump the ceiling between
    // ticks.
    const moreReveal = shownChars < ceiling
      || (voice.enabled && voice.revealEndIdx < target.length);
    if (moreReveal || !serverDone) {
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
      // Voice-mode chunks may arrive on a partial (audio_b64_first
      // mid-stream) or on the terminal payload (both _first and _rest).
      // Kick off the sequencer the moment _first lands; the rest chunk
      // is just stashed and the sequencer picks it up after _first ends.
      if (voice.enabled) {
        if (!voice.firstB64 && res.audio_b64_first) {
          voice.firstB64 = res.audio_b64_first;
          if (voicePromise === null) {
            voicePromise = runVoicePlayback();
          }
        }
        if (!voice.restB64 && res.audio_b64_rest) {
          voice.restB64 = res.audio_b64_rest;
        }
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
        // Voice-mode fallback: if we got terminal without ever seeing
        // an audio chunk (older agent, voice quota exhausted, synth
        // error), unlock reveal so the user isn't left staring at the
        // "thinking…" placeholder.
        if (voice.enabled && !voice.firstB64) {
          voice.firstStarted = true;
          voice.revealEndIdx = target.length;
        }
        startTypewriter();
        // Voice mode: wait for inline audio playback to finish (or
        // bail immediately if there isn't any). The sequencer's
        // while-loop on !restB64 && !serverDone unblocks now that
        // serverDone is set, so we never hang here. Audio promises
        // never reject — playVoiceChunk swallows errors as 'ended'.
        if (voicePromise) {
          try { await voicePromise; } catch (_e) { /* unreachable */ }
        }
        // After audio is done (or skipped), release any remaining text
        // reveal: short responses with no _rest chunk leave revealEndIdx
        // parked at the sentence-1 boundary, and trailing LLM tokens
        // that arrived after the 'play' event need to land too. Also
        // mark firstStarted true so renderShown actually paints — covers
        // the corner case where Audio.play() resolved without ever
        // firing 'play' (autoplay blocked, decoder error after .catch).
        voice.firstStarted = true;
        voice.revealEndIdx = target.length;
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
          // Voice-mode cancel: stop any inline audio that's still
          // playing. Without this the speaker keeps reading the
          // already-synthesized clip after the bubble is replaced
          // with the cancel message, which is jarring.
          if (voice.audioEl) {
            try { voice.audioEl.pause(); } catch (_e) {}
            voice.audioEl = null;
          }
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
          // Voice-mode auto-fire only when the agent did NOT supply
          // inline audio (e.g., older agent build, voice synth failed).
          // When inline audio shipped via _first/_rest we already played
          // it; firing a second TTS job here would double-bill the user
          // and double-play the response.
          if (state.voiceMode && !res.audio_b64_first && !res.audio_b64_rest) {
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
