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

import { state, searchModeByConv, smartModeByConv } from './state.js';
import { stopReadAloud } from './readAloud.js';

export const imageCheckbox = document.getElementById('tool-image-cb');
export const searchCheckbox = document.getElementById('tool-search-cb');
export const smartCheckbox = document.getElementById('tool-smart-cb');
export const editCheckbox = document.getElementById('tool-edit-cb');
const imageWrap = document.getElementById('image-toggle-wrap');
const searchWrap = document.getElementById('search-toggle-wrap');
const smartWrap = document.getElementById('smart-toggle-wrap');
const editWrap = document.getElementById('edit-toggle-wrap');
const searchModeWrap = document.getElementById('search-mode-wrap');
const imageSizeWrap = document.getElementById('image-size-wrap');
const editControlsWrap = document.getElementById('edit-controls-wrap');
const editImageInput = document.getElementById('edit-image-input');
const editImagePickerBtn = document.getElementById('edit-image-picker-btn');
const editPickedFilename = document.getElementById('edit-picked-filename');
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

// The four tool checkboxes, mutually exclusive as a group — checking
// any one clears the other three and claims the tool-row for its own
// sub-controls. Array-driven (rather than four hand-written pairwise
// conditions) so adding a fifth tool later is a one-line change here
// instead of a new OR-clause in every existing wrap's visibility rule.
const TOOL_TOGGLES = [
  {checkbox: imageCheckbox, wrap: imageWrap},
  {checkbox: searchCheckbox, wrap: searchWrap},
  {checkbox: smartCheckbox, wrap: smartWrap},
  {checkbox: editCheckbox, wrap: editWrap},
];

export function refreshComposerUI() {
  // Visibility: whichever checkbox is checked claims the row alone.
  for (const {checkbox, wrap} of TOOL_TOGGLES) {
    wrap.hidden = TOOL_TOGGLES.some(
      (t) => t.checkbox !== checkbox && t.checkbox.checked,
    );
  }
  // Sub-toggles only show under their parent checkbox.
  searchModeWrap.hidden = !searchCheckbox.checked;
  imageSizeWrap.hidden = !imageCheckbox.checked;
  editControlsWrap.hidden = !editCheckbox.checked;
  // Placeholder cues the user about the active mode.
  const ta = document.getElementById('prompt');
  if (imageCheckbox.checked) {
    ta.placeholder = 'Describe the image you want…';
  } else if (searchCheckbox.checked) {
    ta.placeholder = 'Search the web for…';
  } else if (smartCheckbox.checked) {
    ta.placeholder = 'Ask the smart model (slower, smarter)…';
  } else if (editCheckbox.checked) {
    ta.placeholder = 'Describe how to change the image…';
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

// Same per-conversation stickiness for smart mode — a thread started
// on the 14B model wants its follow-ups on it too.
export function persistSmartMode() {
  smartModeByConv.set(state.currentId, smartCheckbox.checked);
}

// Restore the search + smart checkboxes for the conversation we're
// switching to. Image is always restored to off (one-shot, never
// sticky). Called from openConversation / new-chat / right after a
// brand-new conversation is created on submit.
export function restoreModeFor(convId) {
  const want = !!searchModeByConv.get(convId);
  searchCheckbox.checked = want;
  smartCheckbox.checked = !want && !!smartModeByConv.get(convId);
  // Image and edit are always restored to off (one-shot, never
  // sticky) — editing also drops whatever image was picked so a
  // stale filename doesn't linger into a different conversation.
  imageCheckbox.checked = false;
  editCheckbox.checked = false;
  clearPickedEditFile();
  refreshComposerUI();
}

// Shared mutual-exclusion handler for all four toggles: checking one
// clears the other three. persistSearchMode/persistSmartMode run on
// every change (not just their own checkbox's) because a toggle
// stealing the row also changes what search/smart's *effective*
// state is for this conversation — same behavior the three
// hand-written listeners had before edit mode was added, just
// generalized instead of repeated a fourth time.
for (const {checkbox} of TOOL_TOGGLES) {
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) {
      for (const other of TOOL_TOGGLES) {
        if (other.checkbox !== checkbox) other.checkbox.checked = false;
      }
    }
    if (!editCheckbox.checked) clearPickedEditFile();
    persistSearchMode();
    persistSmartMode();
    refreshComposerUI();
  });
}
refreshComposerUI();

// ---- edit-mode image picker --------------------------------------
const editPickedThumb = document.getElementById('edit-picked-thumb');
let _pickedEditFile = null;
let _thumbToken = 0;  // guards against a slow-decoding old thumb landing after a newer pick

// Small, actually-resized preview (not just a CSS-shrunk full image) —
// createImageBitmap decodes off the main thread and the canvas draw
// downsamples, so this stays cheap even for a multi-MB phone photo.
// Cover-cropped to a square so mixed aspect ratios still look tidy at
// this size.
async function renderEditThumb(file) {
  const myToken = ++_thumbToken;
  if (!editPickedThumb) return;
  try {
    const bitmap = await createImageBitmap(file);
    if (myToken !== _thumbToken) { bitmap.close(); return; }  // superseded mid-decode
    const size = 64;  // CSS displays it smaller; 2x for retina crispness
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const scale = Math.max(size / bitmap.width, size / bitmap.height);
    const sw = size / scale;
    const sh = size / scale;
    const sx = (bitmap.width - sw) / 2;
    const sy = (bitmap.height - sh) / 2;
    ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, size, size);
    bitmap.close();
    editPickedThumb.src = canvas.toDataURL('image/jpeg', 0.7);
    editPickedThumb.hidden = false;
  } catch (_e) {
    // Decode failure (corrupt file, unsupported format) — not fatal,
    // the filename label + upload attempt still proceed without a
    // preview.
    editPickedThumb.hidden = true;
  }
}

// Shared by both pick paths (local file input, and "edit this image"
// from a previously generated one — see setPickedEditFile below).
function showPickedEditFile(file) {
  _pickedEditFile = file;
  editPickedFilename.textContent = file ? file.name : '';
  if (file) renderEditThumb(file);
  else if (editPickedThumb) editPickedThumb.hidden = true;
}

if (editImagePickerBtn && editImageInput) {
  editImagePickerBtn.addEventListener('click', () => editImageInput.click());
  editImageInput.addEventListener('change', () => {
    showPickedEditFile((editImageInput.files && editImageInput.files[0]) || null);
  });
}

export function getPickedEditFile() {
  return _pickedEditFile;
}

export function clearPickedEditFile() {
  _thumbToken++;  // invalidate any in-flight decode
  _pickedEditFile = null;
  if (editImageInput) editImageInput.value = '';
  if (editPickedFilename) editPickedFilename.textContent = '';
  if (editPickedThumb) { editPickedThumb.hidden = true; editPickedThumb.src = ''; }
}

// Feeds a previously generated/edited image back in as the next
// edit's input — called from imageGallery.js's lightbox "Edit"
// action with a File built from the fetched PNG. Flips the composer
// into edit mode via a real 'change' event (not a direct assignment)
// so the existing mutual-exclusion/persist/refresh handler runs
// exactly as it would for a user click — one code path, not two.
export function setPickedEditFile(file) {
  if (editCheckbox.checked) {
    showPickedEditFile(file);
  } else {
    editCheckbox.checked = true;
    editCheckbox.dispatchEvent(new Event('change'));
    showPickedEditFile(file);
  }
}

export const EDIT_STRENGTH_PRESETS = {subtle: 0.3, moderate: 0.5, strong: 0.8};
export function selectedEditStrength() {
  const checked = document.querySelector('input[name="edit-strength"]:checked');
  return EDIT_STRENGTH_PRESETS[(checked && checked.value) || 'moderate'];
}

export function selectedSearchMode() {
  // Reads the radio group rather than tracking state — radios are
  // the source of truth and there are only two of them.
  const checked = document.querySelector(
    'input[name="search-mode"]:checked',
  );
  return checked ? checked.value : 'fast';
}
