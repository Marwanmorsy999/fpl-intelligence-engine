/* ============================================================================
 * Phase 3.1 — fetchWithTimeout: every client-side fetch gets a hard deadline.
 *
 * Why: Differential Picks / News Radar / Live Feed used bare `fetch()`, so a
 * hanging request (CORS black-hole, stalled proxy) spun spinners forever.
 * This wrapper aborts after `timeout` ms and throws an honest Error whose
 * message discloses the deadline ("Request timed out after 6 seconds").
 *
 * Browser: exposes `window.FPLHttp` (no build step — loaded before page
 * scripts). Node (unit tests): CommonJS export.
 * ========================================================================== */
"use strict";
(function (root, factory) {
  var api = factory();
  /* istanbul ignore next */
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.FPLHttp = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  var DEFAULT_TIMEOUT_MS = 6000;

  function timeoutErrorMessage(ms) {
    return "Request timed out after " + Math.round(ms / 1000) + " seconds";
  }

  /**
   * fetch() with a hard deadline.
   * @param {string} url
   * @param {{signal?:AbortSignal}} [options]  Caller signal wins over ours.
   * @param {number} [timeout] Milliseconds (default 6000).
   * @returns {Promise<Response>}
   */
  function fetchWithTimeout(url, options, timeout) {
    var opts = options || {};
    var ms =
      typeof timeout === "number" && timeout > 0 ? timeout : DEFAULT_TIMEOUT_MS;

    var controller = null;
    var timer = null;
    var timedOut = false;

    if (typeof AbortController === "function" && !opts.signal) {
      controller = new AbortController();
      opts.signal = controller.signal;
      timer = setTimeout(function () {
        timedOut = true;
        try {
          controller.abort();
        } catch (e) {
          /* abort must never mask the timeout path */
        }
      }, ms);
    }

    function clearTimer() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    }

    return fetch(url, opts).then(
      function (response) {
        clearTimer();
        return response;
      },
      function (error) {
        clearTimer();
        if (
          timedOut ||
          (error && (error.name === "AbortError" || error.code === 20 /* DOMException.ABORT_ERR (older engines) */))
        ) {
          throw new Error(timeoutErrorMessage(ms));
        }
        throw error;
      }
    );
  }

  return {
    DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
    fetchWithTimeout: fetchWithTimeout,
    timeoutErrorMessage: timeoutErrorMessage
  };
});
