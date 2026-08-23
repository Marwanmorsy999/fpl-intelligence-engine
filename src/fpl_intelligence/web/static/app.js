/* Phase 19.0/20.0 — shared page chrome: nav, health pill, crests, fetch helpers,
   plus the Phase 20.0 SESSION BOOTSTRAP: every page restores the saved squad
   from localStorage and renders it without any typing; "Start Over" is the only
   clear path. Loaded by every page; exposes window.FPLApp. No frameworks. */
"use strict";
window.FPLApp = (function () {
  var NAV = [
    { href: "/dashboard", label: "Decisions" },
    { href: "/my-team", label: "My Team" },
    { href: "/assistant", label: "Assistant" },
    { href: "/track-record", label: "Track Record" },
    { href: "/live", label: "Live" },
    { href: "/sources", label: "Sources" },
    { href: "/connect", label: "Connect" }
  ];

  var LS_SESSION_V20 = "fpl_session_v20"; // {key, source, entry_name, synced_at}
  var LS_LEGACY_KEYS = ["fpl_session_id", "fpl_session_source", "fpl_session_source_label"];

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtPrice(v) { return v === null || v === undefined ? "£—" : "£" + Number(v).toFixed(1); }
  function fmtPts(v) { return v === null || v === undefined ? "–" : Number(v).toFixed(1); }

  /* ------------------------------------------------------------------ *
   * Phase 20.0 — persistent session                                     *
   * ------------------------------------------------------------------ */
  function readSession() {
    try {
      var raw = localStorage.getItem(LS_SESSION_V20);
      if (raw) {
        var obj = JSON.parse(raw);
        if (obj && obj.key) return obj;
      }
      /* Legacy one-key-per-item sessions migrate forward transparently. */
      var legacyId = localStorage.getItem(LS_LEGACY_KEYS[0]);
      if (legacyId) {
        var migrated = {
          key: legacyId,
          source: localStorage.getItem(LS_LEGACY_KEYS[1]) || "unknown",
          entry_name: "",
          synced_at: new Date().toISOString()
        };
        writeSession(migrated);
        LS_LEGACY_KEYS.forEach(function (k) { localStorage.removeItem(k); });
        return migrated;
      }
    } catch (e) { /* storage may be unavailable */ }
    return null;
  }

  function writeSession(session) {
    try { localStorage.setItem(LS_SESSION_V20, JSON.stringify(session)); } catch (e) {}
  }

  function saveSession(sessionId, source, entryName) {
    var s = {
      key: String(sessionId),
      source: source || "unknown",
      entry_name: entryName || "",
      synced_at: new Date().toISOString()
    };
    writeSession(s);
    updateSessionChip();
    return s;
  }

  /* Start Over is the ONLY clear path. */
  function clearSession() {
    try {
      localStorage.removeItem(LS_SESSION_V20);
      LS_LEGACY_KEYS.forEach(function (k) { localStorage.removeItem(k); });
    } catch (e) {}
    updateSessionChip();
  }

  function sessionChipHTML(session) {
    if (!session || !session.key) return "";
    var label = session.entry_name ? session.entry_name : (session.source === "manual" ? "Manual Squad" : "Squad");
    var synced = session.synced_at ? new Date(session.synced_at) : null;
    var timeTxt = synced && !isNaN(synced.getTime())
      ? synced.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "—";
    return (
      '<span id="sessionChip" class="pill ok" data-testid="session-chip"' +
      ' title="Saved session — restored automatically on every page">' +
      esc(label) + " · #" + esc(session.key) + " · synced " + timeTxt +
      "</span>"
    );
  }

  function updateSessionChip() {
    var host = document.getElementById("sessionChipHost");
    if (!host) return;
    host.innerHTML = sessionChipHTML(readSession());
  }

  /**
   * Bootstrap every page: restore the saved session and auto-fetch decisions.
   * handlers: { onReady(session, report), onNoSession(), onError(msg) }.
   * Resolves {session, report} so pages can chain their own rendering.
   */
  function bootstrapSession(handlers) {
    var h = handlers || {};
    var session = readSession();
    updateSessionChip();
    if (!session || !session.key) {
      if (h.onNoSession) h.onNoSession();
      return Promise.resolve({ session: null, report: null });
    }
    return fetch("/api/v1/decisions?session_id=" + encodeURIComponent(session.key))
      .then(function (res) {
        if (res.status === 404) {
          clearSession(); // stale squad row is gone — back to the entry screen
          if (h.onNoSession) h.onNoSession("Saved squad no longer exists — start over.");
          return { session: null, report: null };
        }
        if (!res.ok) throw new Error("decisions failed (" + res.status + ")");
        return res.json().then(function (report) {
          if (h.onReady) h.onReady(session, report);
          return { session: session, report: report };
        });
      })
      .catch(function (err) {
        if (h.onError) h.onError(err.message || "Could not load your saved squad.");
        return { session: session, report: null };
      });
  }

  /* ------------------------------------------------------------------ *
   * Nav + health                                                        *
   * ------------------------------------------------------------------ */
  function renderNav(activeHref, containerId) {
    var host = document.getElementById(containerId || "topnav");
    if (!host) return;
    var links = NAV.map(function (n) {
      var active = n.href === activeHref ? " active" : "";
      return '<a class="navlink' + active + '" href="' + n.href + '"' + (n.href === activeHref ? ' aria-current="page"' : "") + ">" + n.label + "</a>";
    }).join("");
    host.innerHTML =
      '<div class="topnav"><div class="topnav-inner">' +
      '<a class="brand" href="/dashboard"><span class="brand-dot">⚽</span>FPL Intelligence</a>' +
      links +
      '<span id="sessionChipHost" style="margin-left:auto;display:inline-flex;gap:6px"></span>' +
      '<span id="healthPill" class="pill">● …</span>' +
      "</div></div>";
    checkHealth();
    updateSessionChip();
  }

  function checkHealth() {
    var pill = document.getElementById("healthPill");
    if (!pill) return;
    fetch("/health")
      .then(function (r) {
        if (!r.ok) throw new Error("bad status");
        pill.className = "pill ok";
        pill.textContent = "● Engine healthy";
      })
      .catch(function () {
        pill.className = "pill bad";
        pill.textContent = "● Offline";
      });
  }

  /* --- FDR colour scale (Phase 20.0 fixture strips) -------------------- */
  function fdrColor(difficulty) {
    switch (Number(difficulty)) {
      case 1: return "#16a34a";
      case 2: return "#65c368";
      case 3: return "#9ca3af";
      case 4: return "#f97316";
      case 5: return "#ef4444";
      default: return "#475569";
    }
  }

  function fixtureStripHTML(runs) {
    if (!runs || !runs.length) return "";
    return (
      '<span class="fixture-strip">' +
      runs.map(function (r) {
        return (
          '<span class="fdr-chip" title="GW' + r.gw + ": " + r.opponent +
          (r.is_home ? " (H)" : " (A)") + ' · FDR ' + r.difficulty + '"' +
          ' style="background:' + fdrColor(r.difficulty) + '">' +
          r.opponent.slice(0, 3) + "</span>"
        );
      }).join("") +
      "</span>"
    );
  }

  /* --- crests: real club colors always; TheSportsDB badge as enhancement -- */
  var CREST_CACHE_KEY = "fpl_crests_v1"; // {teamId: {url, at}}
  function crestHTML(teamId, shortNameOverride) {
    if (!teamId) return "";
    var entry = CLUB_COLORS[teamId];
    var label = esc(shortNameOverride || (entry ? entry[2] : "T" + teamId));
    return (
      '<span class="crest" data-team="' + Number(teamId) + '" title="Team crest" style="' + clubGradient(teamId) + '">' +
      label + "</span>"
    );
  }

  function clubGradient(teamId) {
    var colors = CLUB_COLORS[teamId];
    if (!colors) colors = ["#334155", "#1e293b"];
    return "background:linear-gradient(135deg," + colors[0] + "," + colors[1] + ")";
  }

  var CLUB_COLORS = {
    1: ["#da291c", "#7a1611", "MUN"], 2: ["#241f20", "#4b4b4b", "NEW"], 3: ["#da291c", "#111111", "BOU"],
    4: ["#670e36", "#95bfe5", "AVL"], 5: ["#fdb913", "#231f20", "WOL"], 6: ["#003399", "#00246b", "EVE"],
    7: ["#003090", "#001d5c", "LEI"], 8: ["#ef0107", "#8f0006", "ARS"], 9: ["#7a263a", "#1bb1e7", "WHU"],
    10: ["#132257", "#2e4596", "TOT"], 11: ["#0057b8", "#ffcd00", "BHA"], 12: ["#c8102e", "#7c0a20", "LIV"],
    13: ["#034694", "#022a5e", "CHE"], 14: ["#1b458f", "#c4122e", "CRY"], 15: ["#6cabdd", "#1c2c5b", "MCI"],
    16: ["#6c1d45", "#99d6ea", "BUR"], 17: ["#111111", "#cc0000", "FUL"], 18: ["#eb172b", "#8f0e1b", "SUN"],
    19: ["#1d428a", "#ffd700", "LEE"], 20: ["#dd0000", "#0a4a38", "NFO"]
  };

  function hydrateCrests(rootEl) {
    /* Swap gradient chips for TheSportsDB badges where a URL is cached or
       fetchable. Badge load failures leave the gradient chip untouched. */
    var nodes = (rootEl || document).querySelectorAll(".crest[data-team]");
    if (!nodes.length) return;
    var cache = readCache();
    var needed = [];
    Array.prototype.forEach.call(nodes, function (el) {
      var id = el.getAttribute("data-team");
      /* Skip placeholder chips without a real team id — /crests/0 is 404. */
      if (!id || id === "0") return;
      var hit = cache[id];
      if (hit && hit.url) attachBadge(el, hit.url);
      else needed.push(id);
    });
    if (!needed.length) return;
    Promise.all(
      needed.slice(0, 24).map(function (id) {
        return fetch("/api/v1/crests/" + id)
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) {
            if (data && data.badge_url) {
              cache[id] = { url: data.badge_url, at: Date.now() };
              return { id: id, url: data.badge_url };
            }
            return null;
          })
          .catch(function () { return null; });
      })
    ).then(function (results) {
      writeCache(cache);
      results.forEach(function (hit) {
        if (!hit) return;
        Array.prototype.forEach.call(nodes, function (el) {
          if (el.getAttribute("data-team") === String(hit.id)) attachBadge(el, hit.url);
        });
      });
    }).catch(function () { /* crest enhancement only — never break the page */ });
  }

  function attachBadge(el, url) {
    if (el.querySelector("img")) return;
    var img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = url;
    img.addEventListener("error", function () { img.remove(); }); // gradient stays
    el.appendChild(img);
  }

  function readCache() {
    try { return JSON.parse(localStorage.getItem(CREST_CACHE_KEY) || "{}"); }
    catch (e) { return {}; }
  }
  function writeCache(cache) {
    try { localStorage.setItem(CREST_CACHE_KEY, JSON.stringify(cache)); } catch (e) { /* ignore */ }
  }

  return {
    esc: esc,
    renderNav: renderNav,
    crestHTML: crestHTML,
    hydrateCrests: hydrateCrests,
    CLUB_COLORS: CLUB_COLORS,
    fmtPrice: fmtPrice,
    fmtPts: fmtPts,
    fdrColor: fdrColor,
    fixtureStripHTML: fixtureStripHTML,
    session: {
      read: readSession,
      save: saveSession,
      clear: clearSession,
      bootstrap: bootstrapSession,
      updateChip: updateSessionChip
    }
  };
})();
