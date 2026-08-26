/* Phase 3.4 - First-visit guided tour (dependency-free). Runs once per
 * browser (localStorage flag). A11y: real <button>s w/ aria-labels, Esc to
 * close, Next/Back/Finish keyboard reachable. Skips missing targets silently
 * (zero console noise - required by the Phase 18 E2E console audit).
 * Browser: window.FPLTour. Node: CommonJS export. */
"use strict";
(function (root, factory) {
  var api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.FPLTour = api;
  }
})(typeof self !== "undefined" ? self : this, function (root) {
  var LS_KEY = "fpl_tour_done_v1";

  function defaultSteps() {
    return [
      { el: "#inputSection", text: "Enter your FPL Team ID once - your squad syncs automatically from the official API." },
      { el: "#syncRibbon", text: "Your gameweek, bank, squad value and rank are always visible here." },
      { el: "#newsRadarBox", text: "News Radar scans BBC Sport headlines against YOUR players for injury/suspension flags." },
      { el: "#pitch", text: "This is your main hub - tap any player for deep analysis, fixture runs and xPTS breakdowns." }
    ];
  }

  function markDone() {
    try { root.localStorage.setItem(LS_KEY, "1"); } catch (e) {}
  }

  function isDone() {
    try { return root.localStorage.getItem(LS_KEY) === "1"; } catch (e) { return false; }
  }

  function buildOverlay(doc) {
    var existing = doc.getElementById("fplTourHost");
    if (existing) return existing.__tour;

    var host = doc.createElement("div");
    host.id = "fplTourHost";
    host.setAttribute("data-testid", "tour-host");
    host.innerHTML =
      '<div id="fplTourBackdrop" class="tour-backdrop"></div>' +
      '<div id="fplTourCard" class="tour-card" role="dialog" aria-modal="true" aria-labelledby="fplTourText">' +
      '<p id="fplTourStepLabel" class="tour-step-label"></p>' +
      '<p id="fplTourText" class="tour-text"></p>' +
      '<div class="tour-actions">' +
      '<button type="button" id="fplTourSkip" class="btn-ghost" aria-label="Skip the guided tour">Skip</button>' +
      '<button type="button" id="fplTourBack" class="btn-ghost" aria-label="Previous tour step">Back</button>' +
      '<button type="button" id="fplTourNext" class="btn" aria-label="Next tour step">Next</button>' +
      "</div></div>";
    (doc.body || doc.documentElement).appendChild(host);

    var ctrl = { host: host };
    host.__tour = ctrl;
    return ctrl;
  }

  /* Start the tour. Steps whose element is absent are skipped (never crash). */
  function start(doc, steps, hooks) {
    doc = doc || (root && root.document);
    if (!doc || !doc.body) return null;
    var tourSteps = steps && steps.length ? steps : defaultSteps();
    var h = hooks || {};
    var overlay = buildOverlay(doc);
    var idx = -1;
    var lastHighlight = null;

    function clearHighlight() {
      if (lastHighlight) {
        lastHighlight.classList.remove("tour-highlight");
        lastHighlight = null;
      }
    }

    function close(finished) {
      clearHighlight();
      if (overlay.host.parentNode) overlay.host.parentNode.removeChild(overlay.host);
      doc.removeEventListener("keydown", onKey);
      markDone();
      if (h.onDone) h.onDone(!!finished);
    }

    function onKey(e) {
      if (e.key === "Escape") close(false);
    }

    function show(i) {
      var stepIdx = i;
      while (stepIdx < tourSteps.length && !doc.querySelector(tourSteps[stepIdx].el)) {
        stepIdx += 1;
      }
      if (stepIdx >= tourSteps.length) {
        close(true);
        return;
      }
      idx = stepIdx;
      var step = tourSteps[idx];
      var target = doc.querySelector(step.el);
      clearHighlight();
      if (target) {
        target.classList.add("tour-highlight");
        lastHighlight = target;
        if (typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "center" });
      }
      doc.getElementById("fplTourStepLabel").textContent = "Step " + (idx + 1) + " of " + tourSteps.length;
      doc.getElementById("fplTourText").textContent = step.text;
      var back = doc.getElementById("fplTourBack");
      var next = doc.getElementById("fplTourNext");
      back.disabled = idx === 0;
      next.textContent = idx === tourSteps.length - 1 ? "Finish" : "Next";
      next.focus();
    }

    doc.getElementById("fplTourSkip").addEventListener("click", function () { close(false); });
    doc.getElementById("fplTourBack").addEventListener("click", function () { if (idx > 0) show(idx - 1); });
    doc.getElementById("fplTourNext").addEventListener("click", function () { show(idx + 1); });
    doc.addEventListener("keydown", onKey);
    show(0);
    return overlay;
  }

  function autoStart(doc, force) {
    if (!force && isDone()) return false;
    start(doc, null, null);
    return true;
  }

  return {
    LS_KEY: LS_KEY,
    defaultSteps: defaultSteps,
    isDone: isDone,
    markDone: markDone,
    start: start,
    autoStart: autoStart
  };
});