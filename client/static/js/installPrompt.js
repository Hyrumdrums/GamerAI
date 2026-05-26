// ---- PWA install detection + opt-in banner -------------------------
// Two jobs:
//
//   1. Detect whether we're running as an installed PWA (display-mode:
//      standalone, OR navigator.standalone on iOS). This gates the
//      push opt-in banner — iOS Safari only delivers Web Push to
//      installed PWAs (PWA_REFERENCE §8), so prompting otherwise is
//      wasted UX.
//
//   2. Manage the push opt-in banner: show it when we're a PWA, push
//      is supported, permission is 'default', and the user hasn't
//      dismissed it. Tapping Enable calls notifications.subscribe();
//      tapping Not now stashes a dismissal in localStorage.
//
// Android `beforeinstallprompt` capture (the "Install GamerAI" banner
// when NOT yet a PWA) is also handled here, but that's a separate
// banner from the push opt-in.

import { pushSupported, permissionState, subscribe, getCurrentSubscription } from './notifications.js';

// iOS-specific: Safari sets navigator.standalone when the page is
// launched from a Home Screen icon. matchMedia('(display-mode: standalone)')
// catches everything else (Chrome on Android, Edge desktop, …). Per
// PWA_REFERENCE §8 point 4, you need BOTH checks.
export function isStandalonePWA() {
  const matchStandalone = window.matchMedia
    && window.matchMedia('(display-mode: standalone)').matches;
  // iOS Safari has navigator.standalone but not display-mode media query
  // for it. true/false; undefined on non-iOS.
  const iosStandalone = window.navigator && window.navigator.standalone === true;
  return !!(matchStandalone || iosStandalone);
}

export function isIOS() {
  if (typeof navigator === 'undefined') return false;
  return /iPhone|iPad|iPod/i.test(navigator.userAgent);
}

// LocalStorage keys for dismissal state. Per PWA_REFERENCE §10 point 3:
// "Permission is requested only when they tap Enable. Store the
// dismissal either way" — so if the user says "not now" we don't keep
// pestering them on every page reload.
const KEY_PUSH_DISMISSED = 'gamerai.push_prompt_dismissed';
const KEY_INSTALL_DISMISSED = 'gamerai.install_prompt_dismissed';

function isDismissed(key) {
  try { return localStorage.getItem(key) === '1'; }
  catch (_) { return false; }
}
function setDismissed(key) {
  try { localStorage.setItem(key, '1'); }
  catch (_) { /* private mode → banner reappears next visit, OK */ }
}

// ---------- push opt-in banner ----------
// Builds a small banner element on demand and inserts it at the top
// of the chat layout. We render it from JS rather than putting it in
// the template so the cost is zero when conditions don't fire
// (anonymous browse, non-PWA, permission already decided, etc.).
async function maybeShowPushBanner() {
  if (!pushSupported()) return;
  if (!isStandalonePWA()) return;  // iOS Safari + privacy-first stance
  if (permissionState() !== 'default') return;
  if (isDismissed(KEY_PUSH_DISMISSED)) return;
  // Already subscribed in this browser? (Defensive — shouldn't happen
  // when permission is 'default', but a previous subscription that
  // outlived a permission reset would.)
  if (await getCurrentSubscription()) return;

  const banner = document.createElement('div');
  banner.className = 'pwa-banner';
  banner.innerHTML = `
    <span class="pwa-banner-text">
      Get a notification when your image or chat finishes.
    </span>
    <button type="button" class="pwa-banner-action">Enable</button>
    <button type="button" class="pwa-banner-dismiss" aria-label="Not now">Not now</button>
  `;
  document.body.appendChild(banner);
  const dismiss = () => {
    setDismissed(KEY_PUSH_DISMISSED);
    banner.remove();
  };
  banner.querySelector('.pwa-banner-dismiss').onclick = dismiss;
  banner.querySelector('.pwa-banner-action').onclick = async () => {
    banner.querySelector('.pwa-banner-action').disabled = true;
    try {
      await subscribe();
      banner.remove();
    } catch (e) {
      // Surface the error inline; user can try again or dismiss.
      const t = banner.querySelector('.pwa-banner-text');
      t.textContent = (e && e.message) || 'Subscribe failed.';
      banner.querySelector('.pwa-banner-action').disabled = false;
    }
  };
}

// ---------- Android install banner ----------
// Chrome / Edge / Samsung fire `beforeinstallprompt` when the page
// meets the installability criteria. We capture the event, prevent
// the default mini-infobar, and show our own banner so we can
// position it where it doesn't fight the chat UI.
let _deferredInstallPrompt = null;
function bindInstallPromptCapture() {
  window.addEventListener('beforeinstallprompt', (e) => {
    // Already a PWA → nothing to install. Already dismissed → don't
    // re-prompt this session.
    if (isStandalonePWA()) return;
    if (isDismissed(KEY_INSTALL_DISMISSED)) return;
    e.preventDefault();
    _deferredInstallPrompt = e;
    showInstallBanner();
  });
  // appinstalled fires after the user accepts. Drop the cached event
  // so any subsequent UI knows there's nothing to prompt for.
  window.addEventListener('appinstalled', () => {
    _deferredInstallPrompt = null;
    const existing = document.querySelector('.pwa-banner.install');
    if (existing) existing.remove();
  });
}

function showInstallBanner() {
  if (document.querySelector('.pwa-banner.install')) return;
  const banner = document.createElement('div');
  banner.className = 'pwa-banner install';
  banner.innerHTML = `
    <span class="pwa-banner-text">Install GamerAI for one-tap access.</span>
    <button type="button" class="pwa-banner-action">Install</button>
    <button type="button" class="pwa-banner-dismiss" aria-label="Not now">Not now</button>
  `;
  document.body.appendChild(banner);
  banner.querySelector('.pwa-banner-dismiss').onclick = () => {
    setDismissed(KEY_INSTALL_DISMISSED);
    banner.remove();
  };
  banner.querySelector('.pwa-banner-action').onclick = async () => {
    if (!_deferredInstallPrompt) return;
    _deferredInstallPrompt.prompt();
    await _deferredInstallPrompt.userChoice;
    _deferredInstallPrompt = null;
    banner.remove();
  };
}

// Entry point called once from chat.js on init.
export function initPWAPrompts() {
  bindInstallPromptCapture();
  // Defer the push banner check to next tick so the SW has a fighting
  // chance to register before we ask about subscriptions.
  setTimeout(() => { maybeShowPushBanner(); }, 0);
}
