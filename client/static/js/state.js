// ---- shared mutable state --------------------------------------------
// Single state object so other modules can read AND write the same
// values via live property access — ES module exports are immutable
// bindings, so we can't `export let me = null` and have another module
// reassign it. The object pattern is the minimum syntactic change
// needed to share what used to be top-level `let` declarations in
// chat.js across the new modules.
//
// Each field below was previously a top-level `let`/`const` in chat.js
// — comments are preserved from the original.
//
// - me / conversations / currentId: session + chat selection
// - voiceMode: session-global toggle for auto-read-aloud
// - convTokens: running token total for the visible conversation
// - activeStream: Symbol token marking the in-flight streamIntoBubble
//   so a second submit (or a conversation switch) doesn't leave two
//   pollers racing on the same bubble
// - readAloudPlaying: {messageId, audio, btn} | null — currently-playing
//   per-message audio; reference held so we can stop / toggle
// - activeVoicePlayback: {stop, btn} | null — handle to the in-flight
//   voice-mode chat audio chain so a navigation-away (chat switch, new
//   chat, voice toggle off) can stop it. The streamIntoBubble loop
//   owns the audio elements; this just exposes a callable stop.
export const state = {
  me: null,
  conversations: [],   // [{conversation_id, title, updated_at, ...}]
  currentId: null,     // active conversation_id, or null = brand-new
  voiceMode: false,
  convTokens: 0,
  activeStream: null,
  readAloudPlaying: null,
  activeVoicePlayback: null,
};

// In-memory message cache so switching between already-opened
// conversations is instant. Keyed by conversation_id; value is the
// full messages[] array. Cleared on page refresh — not a substitute
// for offline persistence (see project-gaps.md). Each completed
// turn appends to the cache directly so we don't refetch on every
// new message.
export const msgCache = new Map();

// Per-conversation search-mode state. Search is sticky within a
// conversation (unlike image, which is one-shot per turn) — once you
// check it for the first follow-up, the natural flow is to keep
// drilling in with more search-grounded questions. Auto-unchecking
// every turn was forcing the user to re-check and they kept
// forgetting. Keyed by conversation_id; the "brand new chat" state
// (currentId === null) gets its own slot via the null key.
export const searchModeByConv = new Map();

// readAloudCache is keyed by message_id and stores the WAV base64 once
// fetched, so re-tapping the same speaker icon replays from memory
// without billing voice_minutes again. In-memory only — a page refresh
// re-bills, same trade-off as msgCache (see project-gaps.md on the
// IndexedDB-with-encryption plan that would cover both).
export const readAloudCache = new Map();

// Running token total (prompt + completion) for the visible conversation.
// Recomputed from messages on render; incremented per completed turn so
// the bottom-of-chat counter stays live mid-session.
export function setConvTokens(n) {
  state.convTokens = Math.max(0, n | 0);
  const el = document.getElementById('conv-tokens');
  if (!el) return;
  if (state.convTokens > 0) {
    el.textContent = state.convTokens.toLocaleString() + ' tokens this chat';
    el.hidden = false;
  } else {
    el.textContent = '';
    el.hidden = true;
  }
}

export function sumMessageTokens(messages) {
  let t = 0;
  for (const m of messages || []) {
    t += (m.prompt_tokens || 0) + (m.completion_tokens || 0);
  }
  return t;
}
