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
  // Stats panel state. historyInfo is the most recent /generate
  // truncation report (messages_kept/dropped/tokens etc); summary
  // is the rolling summary text the coordinator generates for older
  // turns. statsExpanded persists the user's expand-collapse choice
  // across renders within a session.
  historyInfo: null,
  summary: null,
  statsExpanded: false,
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

// Per-conversation smart-mode state. Same stickiness contract as
// search: a conversation that started on the 14B smart model wants
// every follow-up on it too — silently dropping back to the 3B
// mid-thread would be a confusing quality cliff. The checkbox state
// is what carries smart across turns (each submit re-sends
// smart:true); the coordinator treats every /generate independently.
export const smartModeByConv = new Map();

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
  renderStatsPanel();
}

// Single render entry-point for the collapsible footer stats panel.
// Reads state.convTokens / historyInfo / summary / statsExpanded and
// re-paints the panel. Called whenever any of those fields change
// (conv switch, /generate response, expand toggle).
export function renderStatsPanel() {
  const panel = document.getElementById('stats-panel');
  if (!panel) return;
  const collapsedText = document.getElementById('stats-collapsed-text');
  const toggle = document.getElementById('stats-toggle');
  const expanded = document.getElementById('stats-expanded');
  const rowTokens = document.getElementById('stats-row-tokens');
  const rowContext = document.getElementById('stats-row-context');
  const sectionSummary = document.getElementById('stats-section-summary');
  const summaryText = document.getElementById('stats-summary-text');
  const summaryStatus = document.getElementById('stats-summary-status');

  // Nothing to show when there are no tokens and no conversation.
  if (state.convTokens <= 0) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const tokensFmt = state.convTokens.toLocaleString();
  collapsedText.textContent = `${tokensFmt} tokens this chat`;
  toggle.textContent = state.statsExpanded ? 'stats ▾' : 'stats ▴';
  panel.setAttribute('aria-expanded', String(state.statsExpanded));

  if (!state.statsExpanded) {
    expanded.hidden = true;
    return;
  }
  expanded.hidden = false;

  rowTokens.textContent = `${tokensFmt} tokens recorded in this conversation.`;

  if (state.historyInfo) {
    const h = state.historyInfo;
    const kept = h.messages_kept | 0;
    const dropped = h.messages_dropped | 0;
    const cap = h.cap_tokens | 0;
    const used = h.tokens_kept | 0;
    const summarized = !!h.summary_in_use;
    let line = `Last /generate sent ~${used.toLocaleString()} of ${cap.toLocaleString()} token cap to the model `;
    line += `(kept ${kept} turn${kept === 1 ? '' : 's'}`;
    if (dropped > 0 && summarized) {
      line += `, ${dropped} earlier turn${dropped === 1 ? '' : 's'} represented by summary`;
    } else if (dropped > 0) {
      line += `, dropped ${dropped} older turn${dropped === 1 ? '' : 's'}`;
    }
    line += ').';
    rowContext.textContent = line;
  } else {
    rowContext.textContent = 'Send a message to see what gets shipped to the model.';
  }

  // Summary section: visible when we have summary text in hand OR
  // when truncation has happened and a summary job is presumed to be
  // running. The "summarizing now…" indicator shows during the gap
  // between the summary job firing (history_info.messages_dropped > 0
  // && summary_in_use=false) and the next conversation fetch picking
  // up the new summary text. Inline so it doesn't take extra rows.
  const dropped = (state.historyInfo && state.historyInfo.messages_dropped) || 0;
  const inUse = !!(state.historyInfo && state.historyInfo.summary_in_use);
  const isSummarizing = dropped > 0 && !inUse;
  if (state.summary) {
    sectionSummary.hidden = false;
    summaryText.textContent = state.summary;
    summaryStatus.hidden = !isSummarizing;
  } else if (isSummarizing) {
    sectionSummary.hidden = false;
    summaryText.textContent = '';
    summaryStatus.hidden = false;
  } else {
    sectionSummary.hidden = true;
    summaryText.textContent = '';
    summaryStatus.hidden = true;
  }
}

export function sumMessageTokens(messages) {
  let t = 0;
  for (const m of messages || []) {
    t += (m.prompt_tokens || 0) + (m.completion_tokens || 0);
  }
  return t;
}
