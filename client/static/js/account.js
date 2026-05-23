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
