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
  makeReadAloudButton, onReadAloudClick, setReadAloudState,
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
const TYPEWRITER_CHARS_PER_SECOND = 125;
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

// Voice-mode chat: the server emits N audio chunks (exponentially-
// batched sentences) as they're synthesized, and ships them on
// /result.audio_chunks. The client suppresses text reveal until the
// first chunk actually plays — beyond that, voice mode is no longer
// karaoke-synced (the audio is one batch ahead of where the eye is
// usually reading anyway). See the in-function voice block for the
// per-chunk play queue.

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

  // Voice-mode chat exponential-batched playback. The agent streams
  // sentence-batches (1, 2, 4, 8, ...) as they're synthesized; each
  // lands in res.audio_chunks as {seq, audio_b64, audio_seconds}. The
  // client maintains its own play queue and reveals text the moment
  // the first chunk starts playing — voice mode is the "buffered
  // until audio is ready" contract, not karaoke-synced.
  const VOICE_LOG_T0 = Date.now();
  const voiceLog = (msg) => {
    if (!voice || !voice.enabled) return;
    const dt = ((Date.now() - VOICE_LOG_T0) / 1000).toFixed(2);
    // Single namespaced console line so the user can filter the
    // browser console by "[voice]" and see the full timeline.
    console.log(`[voice +${dt}s] ${msg}`);
  };
  const voice = {
    enabled: !!state.voiceMode,
    queuedSeqs: new Set(),   // chunks we've enqueued so we never double-play
    playQueue: [],           // chunks pending playback (FIFO by arrival)
    playing: false,          // a chunk is currently in flight
    started: false,          // at least one chunk has fired 'play' — gates DOM paint
    audioEl: null,           // currently-playing element (for cancel)
    stopRequested: false,    // user clicked stop / navigated away
    readBtn: null,           // the bubble's read-aloud button (acts as stop)
    chunksDoneResolver: null,
    chunksDonePromise: null,
  };
  voice.chunksDonePromise = new Promise(r => { voice.chunksDoneResolver = r; });
  if (voice.enabled) voiceLog('voice mode enabled — buffering text until first audio');

  function stopVoicePlayback() {
    if (voice.stopRequested) return;
    voice.stopRequested = true;
    voiceLog('stopVoicePlayback() invoked');
    if (voice.audioEl) {
      try { voice.audioEl.pause(); } catch (_e) {}
      voice.audioEl = null;
    }
    // pause() doesn't fire 'ended', so the in-flight playOneChunk
    // promise won't resolve naturally. Force-resolve it so the pump's
    // await returns and the while-loop sees stopRequested=true.
    if (voice.currentChunkResolve) {
      const r = voice.currentChunkResolve;
      voice.currentChunkResolve = null;
      r();
    }
    // Drain any unplayed chunks so the pump's "wait for more" loop
    // exits immediately on the next tick.
    voice.playQueue.length = 0;
    // Reveal whatever text is in hand — the user stopped the audio,
    // but they probably still want to read the rest.
    voice.started = true;
    if (state.activeVoicePlayback && state.activeVoicePlayback.token === voice) {
      state.activeVoicePlayback = null;
    }
    // The pump's finishing block resets the button + onclick. If the
    // pump hasn't started yet (stopped before first chunk drew), do it
    // here so the button doesn't get stuck in 'playing'.
    if (voice.readBtn && !voice.playing) {
      setReadAloudState(voice.readBtn, 'idle');
    }
  }

  function ensureVoiceStopButton() {
    if (voice.readBtn) return;
    const readMid = wrap.dataset.messageId;
    if (!readMid) return;
    let btn = wrap.querySelector('.read-aloud-btn');
    if (!btn) {
      btn = makeReadAloudButton(readMid, '');
      wrap.appendChild(btn);
    }
    voice.readBtn = btn;
    setReadAloudState(btn, 'playing');
    // Override the default onclick: while voice chunks are playing,
    // tapping the speaker icon stops them. The default onReadAloudClick
    // behavior is restored when the chunk pump finishes (or is stopped).
    btn.onclick = (e) => {
      e.preventDefault();
      stopVoicePlayback();
    };
    // Publish the stop hook globally so chat switching / new-chat /
    // voice-mode-off can also trigger it (stopReadAloud calls .stop()).
    state.activeVoicePlayback = {
      token: voice,
      stop: stopVoicePlayback,
    };
  }

  function renderShown() {
    if (state.activeStream !== myToken) return;
    // Voice mode pre-audio: leave the pending "thinking…" placeholder
    // in place. The first chunk's 'play' event flips voice.started and
    // re-enters renderShown.
    if (voice.enabled && !voice.started) return;
    setBubbleContent(bubble, 'assistant', target.substring(0, shownChars));
    if (isPinnedToBottom) pane.scrollTop = pane.scrollHeight;
  }

  function playOneChunk(chunk) {
    return new Promise(resolve => {
      const a = new Audio('data:audio/wav;base64,' + chunk.audio_b64);
      voice.audioEl = a;
      // Save the resolver so stopVoicePlayback can force-resolve the
      // chunk's promise when the user stops mid-playback —
      // audio.pause() alone does NOT fire 'ended'.
      voice.currentChunkResolve = resolve;
      voiceLog(`chunk ${chunk.seq} loaded (audio_seconds=${(chunk.audio_seconds || 0).toFixed(2)}) — calling play()`);
      const done = (why) => {
        if (voice.audioEl === a) voice.audioEl = null;
        if (voice.currentChunkResolve === resolve) voice.currentChunkResolve = null;
        voiceLog(`chunk ${chunk.seq} ${why}`);
        resolve();
      };
      a.onended = () => done('ended');
      a.onerror = () => done('errored');
      a.onplay = () => {
        voice.started = true;
        // First-chunk play also unlocks text reveal up to whatever has
        // streamed in so far. Later chunks bump the ceiling as more
        // text arrives between play events.
        renderShown();
        voiceLog(`chunk ${chunk.seq} onplay fired`);
      };
      a.play().catch((e) => done(`play() rejected: ${e && e.message || e}`));
    });
  }

  async function voicePump() {
    // Drains the play queue in order. Started when the first chunk
    // arrives. Subsequent chunks just get appended to playQueue and
    // the pump picks them up after the current one ends. Exits early
    // on stopRequested so the user-stop path can short-circuit any
    // remaining audio (incl. chunks not yet pulled off the queue).
    voice.playing = true;
    while (!voice.stopRequested) {
      if (voice.playQueue.length === 0) {
        // Are more chunks coming? If serverDone and queue empty, we're
        // out. Otherwise wait briefly for the next chunk.
        if (serverDone) break;
        await new Promise(r => setTimeout(r, 100));
        continue;
      }
      const chunk = voice.playQueue.shift();
      await playOneChunk(chunk);
    }
    voice.playing = false;
    // Restore the read-aloud button to its default replay behavior.
    // After voice playback, tapping the speaker re-fires the standard
    // TTS path on the final message text (the existing onReadAloudClick).
    if (voice.readBtn) {
      const finalText = (target || '').trim();
      const mid = wrap.dataset.messageId;
      setReadAloudState(voice.readBtn, 'idle');
      voice.readBtn.onclick = () => onReadAloudClick(mid, finalText, voice.readBtn);
    }
    if (state.activeVoicePlayback && state.activeVoicePlayback.token === voice) {
      state.activeVoicePlayback = null;
    }
    if (voice.chunksDoneResolver) {
      voice.chunksDoneResolver();
      voice.chunksDoneResolver = null;
    }
  }

  function typewriterTick(ts) {
    if (state.activeStream !== myToken) {
      rafId = null;
      return;
    }
    if (!lastFrameTs) lastFrameTs = ts;
    const dtMs = ts - lastFrameTs;
    lastFrameTs = ts;
    // Voice mode: typewriter is gated by voice.started. Pre-first-
    // chunk, ceiling is 0 (so the "thinking…" placeholder holds). Once
    // first audio plays, reveal everything streamed so far and keep
    // racing target.length as more text arrives — exponential batching
    // means audio + text aren't strictly synced anyway, so prioritize
    // "user can read along while listening" over "reveal one sentence
    // at a time".
    const ceiling = voice.enabled
      ? (voice.started ? target.length : 0)
      : target.length;
    if (shownChars < ceiling) {
      const advance = Math.max(
        TYPEWRITER_MIN_CHARS_PER_FRAME,
        Math.round((TYPEWRITER_CHARS_PER_SECOND * dtMs) / 1000),
      );
      shownChars = Math.min(ceiling, shownChars + advance);
      renderShown();
    }
    const moreReveal = shownChars < ceiling
      || (voice.enabled && !voice.started);
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
      // Voice-mode audio chunks arrive on partials AND on the terminal
      // payload (the agent re-attaches all chunks at /complete so a
      // freshly-loaded client gets everything). For each new seq we
      // haven't queued yet, push onto the play queue. The pump runs
      // sequentially.
      if (voice.enabled && Array.isArray(res.audio_chunks)) {
        for (const chunk of res.audio_chunks) {
          if (!chunk || typeof chunk.seq !== 'number') continue;
          if (voice.queuedSeqs.has(chunk.seq)) continue;
          voice.queuedSeqs.add(chunk.seq);
          voice.playQueue.push(chunk);
          voiceLog(`received chunk ${chunk.seq} (${(chunk.audio_seconds || 0).toFixed(2)}s audio)`);
          // First arrival: hang a stop button (the read-aloud speaker)
          // off the bubble so the user has an obvious way to abort
          // playback mid-response without having to switch chats.
          ensureVoiceStopButton();
          if (!voice.playing) {
            voicePump();
          }
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
        voiceLog(`terminal | chunks_received=${voice.queuedSeqs.size} | text_len=${target.length}`);
        // Voice-mode fallback: terminal arrived with no chunks at all
        // (older agent, voice quota exhausted, synth error). Unlock
        // reveal so the user isn't left staring at "thinking…".
        if (voice.enabled && voice.queuedSeqs.size === 0) {
          voiceLog('no chunks ever arrived — falling back to text-only reveal');
          voice.started = true;
        }
        startTypewriter();
        // Wait for the chunk pump to drain. It exits when serverDone
        // is true and the queue is empty. Audio promises never reject —
        // playOneChunk swallows errors as 'ended'.
        if (voice.enabled && voice.playing) {
          voiceLog('waiting for chunk pump to drain…');
          await voice.chunksDonePromise;
          voiceLog('chunk pump drained');
        }
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
          // When inline audio shipped via audio_chunks we already
          // played it; firing a second TTS job here would double-bill
          // the user and double-play the response.
          const hasInlineAudio = Array.isArray(res.audio_chunks)
            && res.audio_chunks.length > 0;
          if (state.voiceMode && !hasInlineAudio) {
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
