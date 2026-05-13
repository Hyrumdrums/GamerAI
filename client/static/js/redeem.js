// Enable the submit button only once the ToS checkbox is ticked.
// Belt-and-suspenders: the HTML5 `required` attribute already blocks
// form submission without the check, but disabling the button removes
// the temptation to click it before reading the terms.
const cb = document.getElementById('tos_accepted');
const btn = document.getElementById('submit');
cb.addEventListener('change', () => { btn.disabled = !cb.checked; });
