/* Phase 19.0 — FPL squad-push bookmarklet (source form).
   The /connect page wraps this file's body into a javascript: URL.
   Contract with fantasy.premierleague.com:
     - entry id parsed from /entry/<id>/... in the URL
     - GET /api/entry/<id>/            -> entry metadata (name, entry->current_event)
     - GET /api/entry/<id>/event/<gw>/picks/ -> picks[] {element, position, is_captain, is_vice}
     - POST <ENGINE>/api/v1/sync/squad-push with Authorization: Bearer <token>
   Same-origin fetches only; the token is read from localStorage once. */
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

  jget("/api/entry/" + entryId + "/")
    .then(function (entry) {
      var gw = (entry.entry && entry.entry.current_event) || entry.current_event || 1;
      return jget("/api/entry/" + entryId + "/event/" + gw + "/picks/").then(function (picksResp) {
        var rawPicks = picksResp.picks || [];
        var payload = {
          entry_id: entryId,
          entry_name: (entry.entry && entry.entry.name) || entry.name || null,
          gameweek: gw,
          bank: (picksResp.entry_history && picksResp.entry_history.bank) || 0,
          transfers: {
            made: (picksResp.entry_history && picksResp.entry_history.event_transfers) || 0,
            cost: (picksResp.entry_history && picksResp.entry_history.event_transfers_cost) || 0
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
            window.alert("✅ Squad synced to FPL Intelligence — GW" + gw + ", " + payload.picks.length + " players.");
          } else {
            resp.json().then(function (body) {
              window.alert("❌ Sync failed (" + resp.status + "): " + ((body && body.detail) || "unknown error"));
            }).catch(function () {
              window.alert("❌ Sync failed (" + resp.status + ")");
            });
          }
        });
      });
    })
    .catch(function (err) {
      window.alert("❌ Bookmarklet sync error: " + (err && err.message ? err.message : err));
    });
})();
