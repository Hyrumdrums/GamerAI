// "Copy invite link" buttons. The page renders relative paths
// (/invite/<code>) so we resolve to an absolute URL here, which is
// what the host actually wants to paste into a text / Slack / email.
document.querySelectorAll('button.copy-link').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const href = btn.dataset.href;
    if (!href) return;
    const absolute = new URL(href, window.location.origin).toString();
    try {
      await navigator.clipboard.writeText(absolute);
      const prev = btn.textContent;
      btn.textContent = 'copied!';
      setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
      // Clipboard API can fail under insecure contexts or when the
      // user has denied permission. Fall back to a visible selection
      // so they can copy manually.
      window.prompt('Copy this invite URL:', absolute);
    }
  });
});

// Live "passwords match" check on the change-password form. The submit
// button stays enabled (the form is short — friction would just annoy
// a real password change) but the custom validity message makes the
// mismatch surface immediately rather than after a round trip.
const pw = document.getElementById('new_password');
const confirmPw = document.getElementById('new_password_confirm');
if (pw && confirmPw) {
  function check() {
    const matches = pw.value === confirmPw.value;
    confirmPw.setCustomValidity(
      matches || !confirmPw.value ? '' : "Passwords don't match"
    );
  }
  pw.addEventListener('input', check);
  confirmPw.addEventListener('input', check);
}

// Invite-form "% of your daily allowance" tip. The form's data-* attrs
// carry the inviter's tier name and per-axis allowance (or are absent
// on unlimited tiers). As the host types a cap, the span right of the
// input shows "≈ NN% of cap" — pure local arithmetic, no round-trip.
(function inviteAllowanceTip() {
  const form = document.querySelector('form[action="/account/invites"]');
  if (!form) return;
  function wire(inputId, tipId, allowanceAttr) {
    const input = document.getElementById(inputId);
    const tip = document.getElementById(tipId);
    if (!input || !tip) return;
    const allowance = parseInt(form.dataset[allowanceAttr] || '', 10);
    if (!Number.isFinite(allowance) || allowance <= 0) return;
    function render() {
      const raw = parseInt(input.value, 10);
      if (!Number.isFinite(raw) || raw <= 0) {
        tip.textContent = '';
        return;
      }
      const pct = Math.round((raw / allowance) * 100);
      tip.textContent = `≈ ${pct}% of cap`;
    }
    input.addEventListener('input', render);
    render();
  }
  wire('daily_quota_tokens', 'daily_quota_tokens_tip', 'tierTokens');
  wire('daily_quota_images', 'daily_quota_images_tip', 'tierImages');
})();
