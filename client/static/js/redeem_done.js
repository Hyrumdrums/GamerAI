// Copy-token button: writes the token to the clipboard, falling back
// to a programmatic selection when the Clipboard API isn't available
// (older browsers, http://localhost dev, etc.) so non-developer
// recruits don't have to fight a triple-click select.
const btn = document.getElementById('copy');
const tok = document.getElementById('tok');
btn.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(tok.textContent.trim());
    btn.textContent = 'Copied ✓';
    btn.classList.add('ok');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('ok'); }, 2000);
  } catch (e) {
    const r = document.createRange();
    r.selectNode(tok);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
    btn.textContent = 'Selected — Ctrl+C';
  }
});
