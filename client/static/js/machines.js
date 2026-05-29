// Machines page: per-card schedule editor.
//
// The card is server-rendered with current values; this script handles
// the interactive bits — show/hide the window fields, default the
// timezone to the browser's zone, and save via a same-origin PATCH that
// the client proxies to the coordinator. No framework; one listener per
// card.

(function () {
  "use strict";

  function hhmmToMinutes(value) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(value || "");
    if (!m) return null;
    const mins = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
    return mins >= 0 && mins < 1440 ? mins : null;
  }

  function browserTz() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (e) {
      return null;
    }
  }

  // Put the browser's zone at the top of the select if it isn't already
  // an option, and select the stored zone (or the browser zone for an
  // unscheduled machine) so "Save" persists something sensible.
  function primeTimezone(tzSelect, storedTz) {
    const bz = browserTz();
    if (bz && !Array.from(tzSelect.options).some((o) => o.value === bz)) {
      const opt = document.createElement("option");
      opt.value = bz;
      opt.textContent = bz + " (this device)";
      tzSelect.insertBefore(opt, tzSelect.firstChild);
    }
    if (storedTz) {
      tzSelect.value = storedTz;
    } else if (bz) {
      tzSelect.value = bz;
    }
  }

  function setPill(pill, status, text) {
    pill.className = "status-pill status-" + status;
    pill.textContent = text;
  }

  function wireCard(card) {
    const form = card.querySelector('[data-role="schedule"]');
    if (!form) return;
    const contributing = form.querySelector('[data-role="contributing"]');
    const windowEnabled = form.querySelector('[data-role="window-enabled"]');
    const windowFields = form.querySelector('[data-role="window-fields"]');
    const startInput = form.querySelector('[data-role="start"]');
    const endInput = form.querySelector('[data-role="end"]');
    const tzSelect = form.querySelector('[data-role="tz"]');
    const saveBtn = form.querySelector('[data-role="save"]');
    const saveMsg = form.querySelector('[data-role="save-msg"]');
    const pill = card.querySelector('[data-role="status-pill"]');

    primeTimezone(tzSelect, form.dataset.tz || "");

    function syncWindowVisibility() {
      windowFields.hidden = !windowEnabled.checked;
    }
    syncWindowVisibility();
    windowEnabled.addEventListener("change", syncWindowVisibility);

    function flash(msg, ok) {
      saveMsg.textContent = msg;
      saveMsg.className = "save-msg " + (ok ? "ok" : "err");
    }

    async function save() {
      const paused = !contributing.checked;
      const enabled = windowEnabled.checked;
      const body = { paused: paused, enabled: enabled };
      if (enabled) {
        const startMin = hhmmToMinutes(startInput.value);
        const endMin = hhmmToMinutes(endInput.value);
        if (startMin === null || endMin === null) {
          flash("Enter valid start and end times.", false);
          return;
        }
        if (startMin === endMin) {
          flash("Start and end can't be identical.", false);
          return;
        }
        body.start_min = startMin;
        body.end_min = endMin;
        body.tz = tzSelect.value;
      }

      saveBtn.disabled = true;
      flash("Saving…", true);
      try {
        const resp = await fetch(
          "/machines/" + encodeURIComponent(card.dataset.machineId) + "/schedule",
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        );
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          flash(data.detail || "Save failed.", false);
          return;
        }
        flash("Saved.", true);
        // Reflect the new state in the pill optimistically; a reload
        // shows the precise live status (online/offline).
        if (paused) {
          setPill(pill, "paused", "paused");
        } else if (data.allowed_now === false) {
          setPill(pill, "sleeping", "Sleeping");
        } else {
          setPill(pill, "online", "active");
        }
      } catch (e) {
        flash("Network error — try again.", false);
      } finally {
        saveBtn.disabled = false;
      }
    }

    saveBtn.addEventListener("click", save);
  }

  document.querySelectorAll(".machine-card").forEach(wireCard);
})();
