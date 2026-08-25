/* v2.5.5-ribbon-always — FPL squad-push bookmarklet (source form).
   The /connect page wraps this file's body into a javascript: URL.
   v2.5.5 ribbon always visible; v2.5.4 CSP fallback: entire execution is wrapped in try/catch and a 3s
   watchdog. If FPL's Content-Security-Policy blocks javascript: execution
   or any fetch times out, we alert "FPL blocked the sync. Please use the
   Sync Now button on your dashboard instead." instead of redirecting.
   Contract with fantasy.premierleague.com:
     - entry id parsed from /entry/<id>/... in the URL
     - GET /api/entry/<id>/            -> entry metadata (name, entry->current_event)
     - GET /api/entry/<id>/event/<gw>/picks/ for current AND next unplayed GW
       (gameweek_clock) -> store whichever contains the latest transfer
       (prefer next when it differs from saved or from current).
     - POST <ENGINE>/api/v1/sync/squad-push with Authorization: Bearer <token>
   Same-origin fetches only; the token is read from localStorage once. */
var BOOKMARKLET_VERSION = "2.5.5-ribbon-always";
(function () {
  "use strict";
  var CSP_MSG = "FPL blocked the sync. Please use the Sync Now button on your dashboard instead.";
  // 3s watchdog — if nothing completes in time, CSP likely blocked execution.
  var _cspTimer = null;
  var _cspDone = false;
  function _clearCspWatchdog() {
    _cspDone = true;
    if (_cspTimer) { try { clearTimeout(_cspTimer); } catch (e) {} _cspTimer = null; }
  }
  try {
    _cspTimer = setTimeout(function () {
      if (_cspDone) return;
      _cspDone = true;
      try { window.alert(CSP_MSG); } catch (e2) {}
    }, 3000);

    var ENGINE = window.__FPL_ENGINE_BASE__ || "";
    var TOKEN = null;
    try { TOKEN = window.localStorage.getItem("fpl_sync_push_token"); } catch (e) { /* ignore */ }
    if (!TOKEN) {
      TOKEN = window.prompt("Paste your SYNC_PUSH_TOKEN (saved for next time):");
      if (TOKEN) {
        try { window.localStorage.setItem("fpl_sync_push_token", TOKEN); } catch (e2) { /* ignore */ }
      }
    }
    if (!TOKEN) { _clearCspWatchdog(); window.alert("No token supplied — sync aborted."); return; }

    var m = window.location.pathname.match(/\/entry\/(\d+)/);
    if (!m) { _clearCspWatchdog(); window.alert("Open your team page on fantasy.premierleague.com first (URL must contain /entry/<id>/)."); return; }
    var entryId = parseInt(m[1], 10);

    function jget(path) {
      return fetch(path, { credentials: "include" }).then(function (r) {
        if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
        return r.json();
      });
    }
    function jgetOptional(path) {
      return fetch(path, { credentials: "include" }).then(function (r) {
        if (!r.ok) return null;
        return r.json().catch(function () { return null; });
      }).catch(function () { return null; });
    }

    jget("/api/entry/" + entryId + "/")
      .then(function (entry) {
        var currentGw = (entry.entry && entry.entry.current_event) || entry.current_event || 1;
        var nextGw = currentGw + 1;
        var bootstrapP = jgetOptional("/api/bootstrap-static/").then(function (bs) {
          if (!bs || !bs.events) return nextGw;
          var now = Date.now();
          var best = null;
          var bestTime = null;
          for (var i = 0; i < bs.events.length; i++) {
            var ev = bs.events[i];
            if (!ev || !ev.deadline_time) continue;
            var dl = Date.parse(ev.deadline_time);
            if (isNaN(dl)) continue;
            if (dl <= now) continue;
            if (bestTime === null || dl < bestTime) { bestTime = dl; best = ev.id; }
          }
          if (best !== null && best !== currentGw) return best;
          if (best === currentGw) return currentGw + 1;
          return best || nextGw;
        }).catch(function () { return nextGw; });

        return bootstrapP.then(function (resolvedNextGw) {
          nextGw = resolvedNextGw;
          var pCurrent = jgetOptional("/api/entry/" + entryId + "/event/" + currentGw + "/picks/");
          var pNext = (nextGw && nextGw !== currentGw) ? jgetOptional("/api/entry/" + entryId + "/event/" + nextGw + "/picks/") : Promise.resolve(null);
          return Promise.all([pCurrent, pNext]).then(function (arr) {
            var picksCurrent = arr[0];
            var picksNext = arr[1];
            var chosen = picksCurrent;
            var chosenGw = currentGw;
            if (picksCurrent && picksNext) {
              var idsCur = (picksCurrent.picks || []).map(function (p) { return p.element; }).sort().join(",");
              var idsNext = (picksNext.picks || []).map(function (p) { return p.element; }).sort().join(",");
              if (idsCur !== idsNext) {
                chosen = picksNext; chosenGw = nextGw;
              }
            } else if (!picksCurrent && picksNext) {
              chosen = picksNext; chosenGw = nextGw;
            }
            if (!chosen) throw new Error("No picks found for GW" + currentGw + " nor GW" + nextGw);
            var rawPicks = chosen.picks || [];
            var entryHistory = chosen.entry_history || {};
            var payload = {
              entry_id: entryId,
              entry_name: (entry.entry && entry.entry.name) || entry.name || null,
              gameweek: chosenGw,
              picks_gw: chosenGw,
              bank: entryHistory.bank || 0,
              transfers: {
                made: entryHistory.event_transfers || 0,
                cost: entryHistory.event_transfers_cost || 0
              },
              picks: rawPicks.map(function (p) {
                return {
                  element_id: p.element,
                  position: p.position,
                  element_type: p.element_type !== undefined ? p.element_type : undefined,
                  is_captain: !!p.is_captain,
                  is_vice: !!p.is_vice
                };
              })
            };
            return fetch(ENGINE + "/api/v1/sync/squad-push", {
              method: "POST",
              headers: { "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN },
              body: JSON.stringify(payload)
            }).then(function (resp) {
              _clearCspWatchdog();
              if (resp.ok) {
                window.alert("✅ Squad synced to FPL Intelligence — GW" + chosenGw + " (picks_gw=" + chosenGw + "), " + payload.picks.length + " players. [" + BOOKMARKLET_VERSION + "]");
              } else {
                resp.json().then(function (body) {
                  if (resp.status === 403 || resp.status === 429) {
                    window.alert(CSP_MSG);
                  } else {
                    window.alert("❌ Sync failed (" + resp.status + "): " + ((body && body.detail) || "unknown error"));
                  }
                }).catch(function () {
                  window.alert("❌ Sync failed (" + resp.status + ")");
                });
              }
            });
          });
        });
      })
      .catch(function (err) {
        _clearCspWatchdog();
        var msg = err && err.message ? err.message : String(err);
        if (/Failed to fetch|Load failed|NetworkError|CSP|Content Security Policy/i.test(msg)) {
          window.alert(CSP_MSG);
        } else {
          window.alert("❌ Bookmarklet sync error: " + msg);
        }
      });
  } catch (outerErr) {
    _clearCspWatchdog();
    try { window.alert(CSP_MSG); } catch (e3) {}
  }
})();
