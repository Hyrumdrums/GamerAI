// ---- voice mode (voice-phase1) ----------------------------------------
// Session-global toggle (state.voiceMode). When ON, the streaming loop
// in streamIntoBubble segments the assistant response into sentences
// and fires a tool="tts" /generate per sentence, then plays each
// returned audio_b64 in submission order. Off by default; flipped via
// the mic button.
//
// Why session-global rather than per-conversation: voice mode is a UX
// preference (do I want audio right now?), not a conversation property
// the way search-mode is. Sticking it to a conversation would make a
// returning user wonder why some threads spontaneously start talking.

import { state, readAloudCache } from './state.js';

// Strip the markdown formatting that small chat models routinely
// emit. Without this, Piper reads "**bold**" as "asterisk asterisk
// bold asterisk asterisk" and "[OpenAI](https://…)" as the whole
// URL out loud. Aim: cover the constructs that actually show up in
// chat answers (bold, italic, inline + fenced code, links, images,
// bullet / numbered list markers, ATX headers, block quotes). Code
// FENCES are dropped but the inner code is kept on the assumption
// that a listener actually wants to hear the snippet.
export function normalizeForTTS(text) {
  if (!text) return '';
  let s = text;
  // Code fences — keep body, drop the ``` markers (and optional lang).
  s = s.replace(/```[\w-]*\s*\n?/g, '').replace(/```/g, '');
  // Inline code: drop backticks, keep contents.
  s = s.replace(/`([^`]+)`/g, '$1');
  // Images first (before plain links — same bracket shape).
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  // Links: keep visible text, drop URL.
  s = s.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  // Bold (**x** or __x__) — keep inner text.
  s = s.replace(/(\*\*|__)(.+?)\1/g, '$2');
  // Italic (*x* / _x_) — only when wrapping non-space text. The
  // lookahead/lookbehind keep us from eating standalone "*"s or
  // mid-word underscores like "snake_case".
  s = s.replace(/(\*|_)(?=\S)(.+?)(?<=\S)\1/g, '$2');
  // ATX headers: drop leading hashes; ensure terminal punctuation so
  // Piper inserts a sentence break after the title.
  s = s.replace(/^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$/gm, (_m, t) => {
    const trimmed = t.trim();
    return /[.!?:]$/.test(trimmed) ? trimmed : trimmed + '.';
  });
  // Bullet markers and numbered list prefixes.
  s = s.replace(/^[ \t]*[-*+][ \t]+/gm, '');
  s = s.replace(/^[ \t]*\d+\.[ \t]+/gm, '');
  // Block quotes.
  s = s.replace(/^[ \t]*>[ \t]?/gm, '');
  // Collapse runs of blank lines into a single blank line so the
  // paragraph chunker still sees a boundary.
  s = s.replace(/\n[ \t]*\n[\s]*/g, '\n\n');
  return s.trim();
}

// ---- read-aloud (single TTS path) -------------------------------------
// One code path for both manual ("tap the speaker icon") and voice-mode
// ("auto-fire on every completed response") read-aloud. Each invocation
// fires ONE tool=tts job for the whole assistant message text, so
// piper.exe pays its subprocess + ONNX cold-start exactly once per
// response. Voice mode used to stream sentence-by-sentence to chase
// "time to first audio", but the response itself isn't conversational
// (model latency + Piper synth means real wait time either way), and
// per-sentence dispatch produced audible cold-start gaps between
// bullets — see project-gaps.md "Piper cold-starts on every TTS job"
// for the agent-side warm-process fix that would actually make
// streaming worthwhile.

// Currently-playing per-message audio. Holding a reference lets us
// (a) stop the prior clip when a different message's button is tapped
// and (b) toggle stop-on-second-tap of the playing button.
// (Reference lives on state.readAloudPlaying so multiple modules can
// see/mutate it — was a top-level `let` in chat.js.)

export function stopReadAloud() {
  // Manual-speaker-icon playback: pause the audio element and reset
  // the button. The function used to short-circuit on no
  // readAloudPlaying, but now we also have to stop the voice-mode
  // chunk chain that streamingEngine owns, so we keep going.
  if (state.readAloudPlaying) {
    const {audio, btn} = state.readAloudPlaying;
    state.readAloudPlaying = null;
    try { audio.pause(); } catch (_e) {}
    setReadAloudState(btn, 'idle');
  }
  // Voice-mode chunked playback (streamingEngine.js). The handle was
  // published when the first chunk arrived; calling stop() flips the
  // pump's stopRequested flag and pauses whatever chunk is mid-play.
  if (state.activeVoicePlayback) {
    const handle = state.activeVoicePlayback;
    state.activeVoicePlayback = null;
    try { handle.stop(); } catch (_e) {}
  }
}

export function setReadAloudState(btn, state) {
  btn.classList.remove('is-loading', 'is-playing');
  if (state === 'loading') {
    btn.classList.add('is-loading');
    btn.setAttribute('aria-label', 'Loading audio…');
    btn.title = 'Loading…';
  } else if (state === 'playing') {
    btn.classList.add('is-playing');
    btn.setAttribute('aria-label', 'Stop reading');
    btn.title = 'Stop';
  } else {
    btn.setAttribute('aria-label', 'Read this message aloud');
    btn.title = 'Read aloud';
  }
}

export async function fetchReadAloudAudio(text) {
  // Strip markdown so Piper doesn't read "asterisk asterisk bold
  // asterisk asterisk" or speak link URLs out loud. Per-message
  // mode sends the whole bubble as one job, so one normalize pass
  // covers it (unlike voice mode, where chunks may split markdown
  // across the wire).
  const spoken = normalizeForTTS(text).trim();
  if (!spoken) throw new Error('nothing to read');
  const r = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: spoken, tool: 'tts'}),
  });
  if (!r.ok) {
    let msg = '';
    try {
      const body = await r.json();
      if (body && typeof body.detail === 'string') msg = body.detail;
    } catch (_e) { /* fallthrough */ }
    throw new Error(msg || ('tts dispatch failed: ' + r.status));
  }
  const {job_id} = await r.json();
  // Poll at the same 250ms cadence as voice mode. Piper finishes a
  // typical paragraph in 1-3s; a long message can run 5-10s on a
  // contributor CPU loop, which is why the README justifies the
  // user-initiated framing here.
  while (true) {
    await new Promise(r => setTimeout(r, 250));
    let res;
    try {
      res = await fetch('/api/result/' + job_id).then(r => r.json());
    } catch (_e) { continue; }
    if (res && res.audio_b64) return res.audio_b64;
    if (res && (res.status === 'error' || res.status === 'cancelled')) {
      throw new Error(res.error || ('tts job ' + res.status));
    }
  }
}

export function playReadAloudAudio(messageId, b64, btn) {
  return new Promise(resolve => {
    const a = new Audio('data:audio/wav;base64,' + b64);
    state.readAloudPlaying = {messageId, audio: a, btn};
    setReadAloudState(btn, 'playing');
    const done = () => {
      // Only reset state if we're still the active clip — stopReadAloud
      // may have already moved on to a different button.
      if (state.readAloudPlaying && state.readAloudPlaying.audio === a) {
        state.readAloudPlaying = null;
        setReadAloudState(btn, 'idle');
      }
      resolve();
    };
    a.onended = done;
    a.onerror = done;
    a.play().catch(done);
  });
}

export async function onReadAloudClick(messageId, text, btn) {
  // Tap on the button that's currently playing → stop.
  if (state.readAloudPlaying && state.readAloudPlaying.messageId === messageId) {
    stopReadAloud();
    return;
  }
  // Tap on a different button while another is playing → stop the
  // other, fall through to play this one.
  stopReadAloud();
  let b64 = readAloudCache.get(messageId);
  if (!b64) {
    setReadAloudState(btn, 'loading');
    btn.disabled = true;
    try {
      b64 = await fetchReadAloudAudio(text);
      readAloudCache.set(messageId, b64);
    } catch (e) {
      btn.disabled = false;
      setReadAloudState(btn, 'idle');
      // Surface quota / worker errors without an alert dialog — a
      // hover tooltip is enough for a non-destructive failure.
      btn.title = (e && e.message) ? `Read aloud failed: ${e.message}` : 'Read aloud failed';
      return;
    }
    btn.disabled = false;
  }
  await playReadAloudAudio(messageId, b64, btn);
}

export function makeReadAloudButton(messageId, text) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'read-aloud-btn';
  setReadAloudState(btn, 'idle');
  btn.innerHTML = `
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M3 9v6h4l5 5V4L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.5-4.03v8.05a4.5 4.5 0 0 0 2.5-4.02zM14 3.23v2.06A7 7 0 0 1 14 18.7v2.06A9 9 0 0 0 14 3.23z"/>
    </svg>
  `;
  btn.onclick = () => onReadAloudClick(messageId, text, btn);
  return btn;
}

// True when a completed assistant turn carries actual readable text
// (i.e. not an image bubble, not an error, not a cancelled bubble, not
// empty). Used by both messageEl (history-load) and streamIntoBubble
// (fresh completion) to decide whether to hang a speaker icon off the
// message.
export function isAssistantTextMessage(opts, text) {
  if (opts.image_path) return false;
  if (opts.status === 'error') return false;
  if (opts.status === 'pending') return false;
  if (opts.status === 'cancelled') return false;
  return !!(text && text.trim());
}
