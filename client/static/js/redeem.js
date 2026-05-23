// Enable the submit button only once the ToS checkbox is ticked AND
// the two password fields match. The HTML5 `required` attribute
// already blocks empty submits, but mismatched passwords would
// otherwise sail through to a server-side re-render.
const cb = document.getElementById('tos_accepted');
const btn = document.getElementById('submit');
const pw = document.getElementById('password');
const confirmPw = document.getElementById('password_confirm');

function updateButton() {
  const matches = pw.value === confirmPw.value;
  const longEnough = pw.value.length >= 8;
  btn.disabled = !(cb.checked && matches && longEnough);
  confirmPw.setCustomValidity(
    matches || !confirmPw.value ? '' : "Passwords don't match"
  );
}

cb.addEventListener('change', updateButton);
pw.addEventListener('input', updateButton);
confirmPw.addEventListener('input', updateButton);
