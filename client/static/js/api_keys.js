// "Copy API key" button — same shape as account.js's copy-link handler,
// but copies a raw opaque value (not a relative href resolved to an
// absolute URL, which would mangle a bearer token).
document.querySelectorAll('button.copy-value').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const value = btn.dataset.value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      const prev = btn.textContent;
      btn.textContent = 'copied!';
      setTimeout(() => { btn.textContent = prev; }, 1200);
    } catch {
      // Clipboard API can fail under insecure contexts or when the
      // user has denied permission. Fall back to a visible selection
      // so they can copy manually.
      window.prompt('Copy this API key:', value);
    }
  });
});
