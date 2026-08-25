/* v2.5.3-sync-truth — FPL squad-push bookmarklet (source form).
   The /connect page wraps this file's body into a javascript: URL.
   Contract with fantasy.premierleague.com:
     - entry id parsed from /entry/<id>/... in the URL
     - GET /api/entry/<id>/            -> entry metadata (name, entry->current_event)
     - GET /api/entry/<id>/event/<gw>/picks/ for current AND next unplayed GW
       (gameweek_clock) → store whichever contains the latest transfer
       (prefer next when it differs from saved or from current).
     - POST <ENGINE>/api/v1/sync/squad-push with Authorization: Bearer <token>
   Same-origin fetches only; the token is read from localStorage once. */
var BOOKMARKLET_VERSION = "2.5.3-sync-truth";
(function () {
  "use strict";
  var ENGINE = window.__FPL_ENGINE_BASE__ || "";
  var TOKEN = null;
  try { TOKEN = window.localStorage.getItem("fpl_sync_push_token"); } catch (e) { /* ignore */ }
  if (!TOKEN) {
    TOKEN = window.prompt("Paste your SYNC_PUSH_TOKEN (saved for next time):");
    if (TOKEN) {
      try { window.localStorage.setItem("fpl_sync_push_token", TOKEN); } catch (e2) { /* ignore */ }
    }
  }
  if (!TOKEN) { window.alert("No token supplied — sync aborted."); return; }

  var m = window.location.pathname.match(/\/entry\/(\d+)/);
  if (!m) { window.alert("Open your team page on fantasy.premierleague.com first (URL must contain /entry/<id>/)."); return; }
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
      // Try to refine nextGw via bootstrap deadline when available (best-effort).
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
        // If bootstrap next equals current, keep current+1 as transfer candidate.
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
          // Choose logic: prefer next when it exists and differs.
          if (picksCurrent && picksNext) {
            var idsCur = (picksCurrent.picks || []).map(function (p) { return p.element; }).sort().join(",");
            var idsNext = (picksNext.picks || []).map(function (p) { return p.element; }).sort().join(",");
            if (idsCur !== idsNext) {
              // When they differ, the next GW holds the transfer — that's truth.
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
            if (resp.ok) {
              window.alert("✅ Squad synced to FPL Intelligence — GW" + chosenGw + " (picks_gw=" + chosenGw + "), " + payload.picks.length + " players. [" + BOOKMARKLET_VERSION + "]");
            } else {
              resp.json().then(function (body) {
                window.alert("❌ Sync failed (" + resp.status + "): " + ((body && body.detail) || "unknown error"));
              }).catch(function () {
                window.alert("❌ Sync failed (" + resp.status + ")");
              });
            }
          });
        });
      });
    })
    .catch(function (err) {
      window.alert("❌ Bookmarklet sync error: " + (err && err.message ? err.message : err));
    });
})();
