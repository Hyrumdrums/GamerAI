// ---- composer controls + tool toggles --------------------------------
// Holds the composer's DOM refs (checkboxes, sub-toggle wrappers, voice
// button) and the small helpers that keep them in sync. Both chat.js
// (composer submit handler) and streamingEngine.js (search_was_skipped
// auto-reset) reach in here.
//
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

import { state, searchModeByConv } from './state.js';
import { stopReadAloud } from './readAloud.js';

export const imageCheckbox = document.getElementById('tool-image-cb');
export const searchCheckbox = document.getElementById('tool-search-cb');
const imageWrap = document.getElementById('image-toggle-wrap');
const searchWrap = document.getElementById('search-toggle-wrap');
const searchModeWrap = document.getElementById('search-mode-wrap');
const imageSizeWrap = document.getElementById('image-size-wrap');
const voiceToggleBtn = document.getElementById('voice-toggle');

// Voice mode toggle. The click is what unlocks the browser's audio
// autoplay restriction for this session — once the user has gestured
// to enable voice, subsequent audio.play() calls (when the auto-fire
// kicks in on response completion) work without further interaction.
// Flipping voice OFF stops any currently-playing clip so the user
// isn't stuck waiting for a long synth to finish playing back.
if (voiceToggleBtn) {
  voiceToggleBtn.addEventListener('click', () => {
    state.voiceMode = !state.voiceMode;
    voiceToggleBtn.setAttribute('aria-pressed', String(state.voiceMode));
    if (!state.voiceMode) stopReadAloud();
  });
}

// Image resolution buckets. The composer's three radios map to one of
// these; the coordinator clamps and the worker renders at whatever
// width/height we send. Cost in image-units (charged against the
// daily image quota) scales with pixel area — see
// coordinator.main.image_cost_multiplier. Small is the default
// because it's ~4× faster than Large on the SDXL Lightning model
// running on a 6 GB contributor GPU, and a one-shot left-on shouldn't
// silently chew through a BRONZE user's whole day of credits.
export const IMAGE_SIZE_PRESETS = {
  small:  {width:  512, height:  512},
  medium: {width:  768, height:  768},
  large:  {width: 1024, height: 1024},
};
// Sticky last choice across page loads. Validated against the preset
// table on read so a stale/malformed localStorage value can't crash
// the composer — it just falls through to the small default.
const IMAGE_SIZE_STORAGE_KEY = 'gamerai.image_size';
function loadImageSizePreference() {
  try {
    const v = localStorage.getItem(IMAGE_SIZE_STORAGE_KEY);
    if (v && Object.prototype.hasOwnProperty.call(IMAGE_SIZE_PRESETS, v)) {
      return v;
    }
  } catch (_) { /* private mode / disabled storage → just use default */ }
  return 'small';
}
function saveImageSizePreference(v) {
  try { localStorage.setItem(IMAGE_SIZE_STORAGE_KEY, v); }
  catch (_) { /* ignore — preference becomes per-session only */ }
}
export function selectedImageSize() {
  const checked = document.querySelector('input[name="image-size"]:checked');
  return (checked && checked.value) || 'small';
}

export function refreshComposerUI() {
  // Visibility: whichever checkbox is checked claims the row alone.
  imageWrap.hidden = searchCheckbox.checked;
  searchWrap.hidden = imageCheckbox.checked;
  // Sub-toggles only show under their parent checkbox.
  searchModeWrap.hidden = !searchCheckbox.checked;
  imageSizeWrap.hidden = !imageCheckbox.checked;
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

// Restore the sticky resolution radio before the first refresh — the
// HTML defaults to small=checked, but a returning user may have
// picked medium/large last time and expects that to stick.
(function applyStickyImageSize() {
  const want = loadImageSizePreference();
  const r = document.querySelector(`input[name="image-size"][value="${want}"]`);
  if (r) r.checked = true;
  // Persist any change immediately so a refresh mid-stream picks up
  // the latest choice instead of the value at page load.
  document.querySelectorAll('input[name="image-size"]').forEach((el) => {
    el.addEventListener('change', () => saveImageSizePreference(el.value));
  });
})();

// Persist the current search-checkbox state under the current
// conversation key. Null key is the "brand new chat" slot — most
// users start a search and immediately submit, so capturing this
// means the conv created on submit inherits the right mode.
export function persistSearchMode() {
  searchModeByConv.set(state.currentId, searchCheckbox.checked);
}

// Restore the search checkbox for the conversation we're switching
// to. Image is always restored to off (one-shot, never sticky).
// Called from openConversation / new-chat / right after a brand-new
// conversation is created on submit.
export function restoreModeFor(convId) {
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

export function selectedSearchMode() {
  // Reads the radio group rather than tracking state — radios are
  // the source of truth and there are only two of them.
  const checked = document.querySelector(
    'input[name="search-mode"]:checked',
  );
  return checked ? checked.value : 'fast';
}
