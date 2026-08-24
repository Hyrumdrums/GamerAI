// ---- image viewer / gallery -----------------------------------------
// Tap any inline <img.generated> in the chat to open the lightbox.
// Swipe (touch) or ← / → (desktop) cycles through every image in the
// current chat in DOM order — newest is bottom-most, like the chat
// itself. Esc or backdrop tap closes. The Save action is just a plain
// <a download>; long-pressing the image on mobile still works as the
// browser-native fallback if download isn't honored.
//
// Single overlay element, lazy-built on first open, so a session with
// no image messages adds zero DOM at startup.

import { setPickedEditFile } from './composer.js';

const imageViewer = (() => {
  let root, imgEl, counterEl, downloadEl, editEl, prevBtn, nextBtn;
  let images = [];
  let index = 0;

  function build() {
    root = document.createElement('div');
    root.className = 'image-viewer';
    root.hidden = true;
    root.setAttribute('aria-hidden', 'true');
    root.innerHTML = `
      <div class="iv-header">
        <span class="iv-counter"></span>
        <div class="iv-actions">
          <a class="iv-action iv-download" href="#" download
             title="Save image" aria-label="Save image">Save</a>
          <button class="iv-action iv-edit" type="button"
                  title="Use as the input for a new edit" aria-label="Edit this image">Edit</button>
          <button class="iv-action iv-close" type="button"
                  title="Close" aria-label="Close">×</button>
        </div>
      </div>
      <button class="iv-nav iv-prev" type="button"
              aria-label="Previous image">‹</button>
      <img class="iv-image" alt="">
      <button class="iv-nav iv-next" type="button"
              aria-label="Next image">›</button>
    `;
    document.body.appendChild(root);
    imgEl = root.querySelector('.iv-image');
    counterEl = root.querySelector('.iv-counter');
    downloadEl = root.querySelector('.iv-download');
    editEl = root.querySelector('.iv-edit');
    prevBtn = root.querySelector('.iv-prev');
    nextBtn = root.querySelector('.iv-next');

    // Backdrop tap closes. Children (image, header, nav buttons)
    // swallow their own clicks because e.target won't be the root.
    root.addEventListener('click', (e) => {
      if (e.target === root) close();
    });
    root.querySelector('.iv-close').addEventListener('click', close);
    prevBtn.addEventListener('click', () => step(-1));
    nextBtn.addEventListener('click', () => step(1));
    // Chain edits: re-fetch the currently-viewed PNG (same /api/images
    // URL the <img> already used, so the same ownership check applies)
    // and hand it to the composer as the next edit's input image. The
    // coordinator never sees "this came from a prior generation" as a
    // distinct concept — from its POV this is just another upload.
    editEl.addEventListener('click', async () => {
      const src = imgEl.src;
      const filename = decodeURIComponent(src.split('/').pop() || 'image.png');
      close();
      let blob;
      try {
        const resp = await fetch(src);
        if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
        blob = await resp.blob();
      } catch (_e) {
        return;  // best-effort — the viewer's already closed, nothing else to roll back
      }
      setPickedEditFile(new File([blob], filename, {type: blob.type || 'image/png'}));
      const ta = document.getElementById('prompt');
      if (ta) ta.focus();
    });

    // Esc / arrow keys — only while open, so they don't fight other
    // global handlers (or the composer textarea) when the viewer is
    // dismissed.
    document.addEventListener('keydown', (e) => {
      if (root.hidden) return;
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowLeft') step(-1);
      else if (e.key === 'ArrowRight') step(1);
    });

    // Swipe. Single-finger only, ≥50px horizontal travel, and the
    // horizontal delta must dominate the vertical one — that filters
    // out accidental swipes mid-scroll without making the gesture
    // feel finicky on real hardware.
    let startX = 0, startY = 0, tracking = false;
    root.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) { tracking = false; return; }
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      tracking = true;
    }, {passive: true});
    root.addEventListener('touchend', (e) => {
      if (!tracking) return;
      tracking = false;
      const t = e.changedTouches[0];
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;
      if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy)) return;
      step(dx < 0 ? 1 : -1);
    }, {passive: true});
  }

  function refreshGallery() {
    images = Array.from(
      document.querySelectorAll('#chat-pane img.generated'),
    );
  }

  function render() {
    if (!images.length) { close(); return; }
    // Wrap around so swiping past the last image returns to the first
    // (and vice versa) — feels right for a finite gallery; "you've
    // hit the end" UI would be more annoying than helpful here.
    const n = images.length;
    index = ((index % n) + n) % n;
    const cur = images[index];
    imgEl.src = cur.src;
    imgEl.alt = cur.alt || '';
    counterEl.textContent = `${index + 1} / ${n}`;
    counterEl.hidden = n < 2;
    downloadEl.href = cur.src;
    // Friendly filename — the proxy path is /api/images/<uuid>.png; a
    // "gamerai-" prefix makes the user's downloads folder searchable
    // later. Falls back to "image.png" only if the URL is malformed
    // (defensive — shouldn't happen for any image our coordinator
    // emits).
    const fname = (cur.src.split('/').pop() || 'image.png');
    downloadEl.download = `gamerai-${decodeURIComponent(fname)}`;
    const single = n < 2;
    prevBtn.hidden = single;
    nextBtn.hidden = single;
  }

  function open(srcImg) {
    if (!root) build();
    refreshGallery();
    index = images.indexOf(srcImg);
    if (index < 0) index = 0;
    render();
    root.hidden = false;
    root.setAttribute('aria-hidden', 'false');
    // Stop the underlying chat from scrolling while the viewer's
    // open — otherwise a swipe-gesture miss can scroll the chat
    // behind the backdrop, which looks broken.
    document.body.style.overflow = 'hidden';
  }

  function close() {
    if (!root) return;
    root.hidden = true;
    root.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  function step(delta) {
    if (!images.length) return;
    index += delta;
    render();
  }

  return {open};
})();

// Delegated click handler — one listener on the chat pane catches
// taps on any rendered image bubble, including images appended later
// (mid-stream completions, lazy-loaded history). No re-binding when
// new turns arrive.
export function initImageGallery() {
  document.getElementById('chat-pane').addEventListener('click', (e) => {
    const img = e.target.closest('img.generated');
    if (img) imageViewer.open(img);
  });
}
