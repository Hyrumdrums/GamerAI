// ---- file attachments (document upload for chat context) -------------
// Paperclip button + hidden <input type=file> in the composer. Picking
// a file (or several) POSTs each to /api/uploads, which forwards to the
// coordinator: text gets extracted server-side and folded into this
// conversation's future chat turns as a <<document>> fence — see
// coordinator/uploads.py. The raw file bytes never persist anywhere;
// only the coordinator's extraction result (echoed back here as
// char_count/truncated) is kept, rendered as a small removable-looking
// chip row above the composer. "Removable-looking" because v1 has no
// per-file delete — see the note on removeChip below.
//
// Per-conversation, keyed the same way searchModeByConv/smartModeByConv
// are in state.js, so switching conversations shows the right chips.

import { state, ensureConversation } from './state.js';

const attachmentsByConv = new Map();

const row = document.getElementById('attachments-row');
const attachBtn = document.getElementById('attach-toggle');
const fileInput = document.getElementById('attach-input');

function chipEl(att) {
  const chip = document.createElement('span');
  chip.className = 'attachment-chip' + (att.pending ? ' pending' : '') + (att.failed ? ' failed' : '');
  const label = document.createElement('span');
  label.className = 'attachment-chip-label';
  label.textContent = att.pending
    ? `${att.filename} — uploading…`
    : att.failed
      ? `${att.filename} — ${att.error || 'upload failed'}`
      : `${att.filename}${att.truncated ? ' (truncated)' : ''}`;
  chip.title = label.textContent;
  chip.appendChild(label);
  return chip;
}

export function renderAttachments() {
  const list = attachmentsByConv.get(state.currentId) || [];
  row.innerHTML = '';
  row.hidden = list.length === 0;
  for (const att of list) row.appendChild(chipEl(att));
}

// Fetches whatever's already attached to a conversation being opened
// (e.g. after a page refresh, or switching back from another chat) so
// the chip row reflects server state, not just this tab's session.
export async function loadAttachmentsFor(conversationId) {
  if (!conversationId) {
    renderAttachments();
    return;
  }
  try {
    const r = await fetch('/api/uploads/' + encodeURIComponent(conversationId));
    if (!r.ok) return;  // leave whatever's cached; not worth surfacing an error for a background refresh
    const body = await r.json();
    attachmentsByConv.set(conversationId, body.uploads || []);
  } catch (_netErr) {
    // Offline — the chips just won't refresh; the conversation itself
    // still opens fine from cache.
  }
  if (state.currentId === conversationId) renderAttachments();
}

export function clearAttachmentsUI() {
  attachmentsByConv.delete(null);
  renderAttachments();
}

async function uploadOne(file) {
  const convId = state.currentId;
  const list = attachmentsByConv.get(convId) || [];
  const pendingEntry = {filename: file.name, pending: true};
  list.push(pendingEntry);
  attachmentsByConv.set(convId, list);
  renderAttachments();

  const form = new FormData();
  form.append('conversation_id', convId);
  form.append('file', file);
  let r;
  try {
    r = await fetch('/api/uploads', {method: 'POST', body: form});
  } catch (_netErr) {
    pendingEntry.pending = false;
    pendingEntry.failed = true;
    pendingEntry.error = 'offline';
    renderAttachments();
    return;
  }
  let body = {};
  try { body = await r.json(); } catch (_e) { /* fall through to generic error */ }
  const idx = list.indexOf(pendingEntry);
  if (idx === -1) return;  // conversation switched away mid-upload; drop it
  if (!r.ok) {
    pendingEntry.pending = false;
    pendingEntry.failed = true;
    pendingEntry.error = body.detail || `upload failed (${r.status})`;
    renderAttachments();
    return;
  }
  list[idx] = {
    upload_id: body.upload_id,
    filename: body.filename,
    char_count: body.char_count,
    truncated: body.truncated,
  };
  renderAttachments();
}

export function initAttachments() {
  if (!attachBtn || !fileInput) return;  // page has no composer (shouldn't happen on /chat)
  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = '';  // allow re-picking the same filename later
    if (files.length === 0) return;
    const ensured = await ensureConversation();
    if (!ensured.ok) {
      const list = attachmentsByConv.get(state.currentId) || [];
      list.push({
        filename: files[0].name, failed: true,
        error: ensured.reason === 'offline' ? "can't attach while offline" : 'failed to start conversation',
      });
      attachmentsByConv.set(state.currentId, list);
      renderAttachments();
      return;
    }
    // Sequential, not Promise.all — keeps upload order == attach
    // order in the chip row and avoids hammering the coordinator with
    // N parallel extraction jobs from one multi-file pick.
    for (const f of files) {
      await uploadOne(f);
    }
  });
}
