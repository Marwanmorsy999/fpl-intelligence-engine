/* Phase 19.0/20.0 — shared page chrome: nav, health pill, crests, fetch helpers,
   plus the Phase 20.0 SESSION BOOTSTRAP: every page restores the saved squad
   from localStorage and renders it without any typing; "Start Over" is the only
   clear path. Loaded by every page; exposes window.FPLApp. No frameworks. */
"use strict";
window.FPLApp = (function () {
  /* Phase 25 Gate 1 (U3): primary surfaces first; everything else lives in
     the mobile "More" sheet and stays reachable from desktop nav too. */
  var NAV_PRIMARY = [
    { href: "/dashboard", label: "Decisions", icon: "⚡" },
    { href: "/targets", label: "Targets", icon: "🎯" },
    { href: "/league", label: "League", icon: "🏆" },
    { href: "/live", label: "Live", icon: "🔴" }
  ];
  var NAV_SECONDARY = [
    { href: "/my-team", label: "My Team", icon: "👥" },
    { href: "/planner", label: "Planner", icon: "🗓️" },
    { href: "/assistant", label: "Assistant", icon: "🤖" },
    { href: "/track-record", label: "Track Record", icon: "📈" },
    { href: "/compare", label: "Compare", icon: "⚖️" },
    { href: "/chips", label: "Chips", icon: "🎲" },
    { href: "/crunch", label: "Crunch", icon: "⏰" },
    { href: "/sources", label: "Sources", icon: "🔍" },
    { href: "/connect", label: "Connect", icon: "🔗" }
  ];
  var NAV = NAV_PRIMARY.concat(NAV_SECONDARY);

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
    /* Mobile bottom tabs: Decisions · Targets · League · Live · More (sheet). */
    var bottomLinks = NAV_PRIMARY.slice(0, 4).map(function (n) {
      var active = n.href === activeHref ? " active" : "";
      return '<a class="' + active.trim() + '" href="' + n.href + '"' + (n.href === activeHref ? ' aria-current="page"' : "") + '><span class="icon">' + (n.icon || "•") + "</span>" + n.label + "</a>";
    }).join("");
    var moreActive = NAV_SECONDARY.some(function (n) { return n.href === activeHref; }) ? " active" : "";
    host.innerHTML =
      '<div class="topnav"><div class="topnav-inner">' +
      '<a class="brand" href="/dashboard"><span class="brand-dot">⚽</span>FPL Intelligence</a>' +
      links +
      '<span id="sessionChipHost" style="margin-left:auto;display:inline-flex;gap:6px"></span>' +
      '<span id="bellHost" style="display:inline-flex"></span>' +
      '<span id="healthPill" class="pill">● …</span>' +
      "</div></div>" +
      '<div id="execRibbonHost"></div>' +
      '<nav class="bottomnav" aria-label="Mobile navigation" data-testid="bottom-nav">' +
      bottomLinks +
      '<button type="button" id="moreBtn"' + (moreActive ? ' aria-current="true"' : "") + ' aria-haspopup="dialog"><span class="icon">⋯</span>More</button>' +
      "</nav>" +
      '<div id="moreSheetBackdrop" class="more-sheet-backdrop"></div>' +
      '<div id="moreSheet" class="more-sheet" role="dialog" aria-label="More pages" data-testid="more-sheet">' +
      "<h3>More</h3>" +
      '<div class="more-sheet-grid">' +
      NAV_SECONDARY.map(function (n) {
        var active = n.href === activeHref ? " active" : "";
        return '<a class="' + active.trim() + '" href="' + n.href + '"><span aria-hidden="true">' + n.icon + "</span>" + n.label + "</a>";
      }).join("") +
      "</div></div>";
    var moreBtn = document.getElementById("moreBtn");
    if (moreBtn) {
      moreBtn.addEventListener("click", function () { toggleSheet(true); });
    }
    var backdrop = document.getElementById("moreSheetBackdrop");
    if (backdrop) backdrop.addEventListener("click", function () { toggleSheet(false); });
    ensureMainLandmark();
    checkHealth();
    updateSessionChip();
    renderBell();
    renderRibbon(activeHref);
    registerSW();
  }

  function toggleSheet(open) {
    var sheet = document.getElementById("moreSheet");
    var backdrop = document.getElementById("moreSheetBackdrop");
    if (!sheet || !backdrop) return;
    sheet.classList.toggle("open", open);
    backdrop.classList.toggle("open", open);
  }

  /* Accessibility: every page gets a <main> landmark around its content
     (Lighthouse landmark-one-main). Idempotent, applies site-wide. */
  function ensureMainLandmark() {
    if (document.querySelector("main")) return;
    var wrap =
      document.querySelector(".wrap") ||
      document.querySelector(".crunch-wrap") ||
      document.querySelector(".max-w-4xl");
    if (!wrap || !wrap.parentNode) return;
    var main = document.createElement("main");
    wrap.parentNode.insertBefore(main, wrap);
    main.appendChild(wrap);
  }

  /* ------------------------------------------------------------------ *
   * Phase 25 Gate 1 (U2) — sticky executive ribbon                      *
   * GW · bank · value · league rank · hit deficit. Every value is       *
   * fetched from an honest source; missing data renders as "–".         *
   * ------------------------------------------------------------------ */
  var RIBBON_CACHE_KEY = "fpl_ribbon_v1";
  var RIBBON_TTL_MS = 5 * 60 * 1000;

  function ribbonCache() {
    try {
      var raw = JSON.parse(localStorage.getItem(RIBBON_CACHE_KEY) || "{}");
      if (raw && raw.at && Date.now() - raw.at < RIBBON_TTL_MS && raw.data) return raw.data;
    } catch (e) {}
    return null;
  }
  function ribbonCacheSave(data) {
    try { localStorage.setItem(RIBBON_CACHE_KEY, JSON.stringify({ at: Date.now(), data: data })); } catch (e) {}
  }

  function fetchJson(url) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }

  function renderRibbon(activeHref) {
    var host = document.getElementById("execRibbonHost");
    if (!host) return;
    var cached = ribbonCache();
    var shell =
      '<div class="exec-ribbon"><div class="exec-ribbon-inner" data-testid="exec-ribbon">' +
      '<span class="ribbon-item"><span class="ribbon-label">GW</span><span class="ribbon-value num" id="rbGw">–</span></span>' +
      '<span class="ribbon-item"><span class="ribbon-label">Bank</span><span class="ribbon-value num" id="rbBank">–</span></span>' +
      '<span class="ribbon-item"><span class="ribbon-label">Value</span><span class="ribbon-value num" id="rbValue">–</span></span>' +
      '<span class="ribbon-item"><span class="ribbon-label">Rank</span><span class="ribbon-value num" id="rbRank">–</span></span>' +
      '<span class="ribbon-item"><span class="ribbon-label">Hits</span><span class="ribbon-value num" id="rbHits">–</span></span>' +
      "</div></div>";
    host.innerHTML = shell;
    if (cached) paint(cached);
    loadRibbonData(function (data) { paint(data); ribbonCacheSave(data); });

    function paint(d) {
      if (!d) return;
      set("rbGw", d.gw != null ? String(d.gw) : "–");
      set("rbBank", d.bank != null ? "£" + Number(d.bank).toFixed(1) + "m" : "–");
      set("rbValue", d.value != null ? "£" + Number(d.value).toFixed(1) + "m" : "–");
      set("rbRank", d.rank != null ? "#" + d.rank : "–");
      set("rbHits", d.hits != null ? "-" + d.hits : "–");
    }
    function set(id, txt) {
      var el = document.getElementById(id);
      if (el) el.textContent = txt;
    }
  }

  function loadRibbonData(done) {
    var session = readSession();
    var data = {};
    fetchJson("/api/v1/sync/target-gameweek").then(function (d) {
      if (d && d.gameweek) data.gw = Number(d.gameweek);
      if (!session || !session.key) return done(data);
      return fetchJson("/api/v1/decisions?session_id=" + encodeURIComponent(session.key)).then(function (rep) {
        var summary = rep && rep.meta && rep.meta.squad_summary;
        if (summary) {
          if (summary.bank != null) data.bank = Number(summary.bank);
          if (summary.team_value != null) data.value = Number(summary.team_value);
        }
        return fetchJson("/api/v1/league?session_id=" + encodeURIComponent(session.key));
      }).then(function (lg) {
        if (lg && lg.your_rank != null) data.rank = Number(lg.your_rank);
        return fetchJson("/api/v1/transfers/ledger?entry_id=" + encodeURIComponent(session.key));
      }).then(function (led) {
        if (led && Array.isArray(led.transfers)) {
          var hits = led.transfers.reduce(function (sum, t) { return sum + Number(t.cost || 0); }, 0);
          if (led.transfers.length) data.hits = hits;
        }
        done(data);
      });
    }).catch(function () { done(data); });
  }

  /* ------------------------------------------------------------------ *
   * Phase 25 Gate 1 (U2) — inline SVG sparkline for form data            *
   * ------------------------------------------------------------------ */
  function sparklineSVG(points, labels) {
    var vals = (points || []).map(function (p) { return Number(p) || 0; });
    if (!vals.length) return "";
    var w = 240, h = 48, pad = 6;
    var max = Math.max.apply(null, vals.concat([2]));
    var step = vals.length > 1 ? (w - pad * 2) / (vals.length - 1) : 0;
    var coords = vals.map(function (v, i) {
      var x = pad + i * step;
      var y = h - pad - ((v / max) * (h - pad * 2));
      return [Math.round(x), Math.round(y)];
    });
    var poly = coords.map(function (c) { return c.join(","); }).join(" ");
    var dots = coords.map(function (c) {
      return '<circle class="spark-dot" cx="' + c[0] + '" cy="' + c[1] + '" r="3"></circle>';
    }).join("");
    var svg =
      '<svg class="form-spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" role="img"' +
      ' aria-label="Last gameweeks points: ' + vals.join(", ") + '">' +
      '<polyline points="' + poly + '"></polyline>' + dots + "</svg>";
    if (labels && labels.length) {
      svg += '<div class="form-spark-labels">' + labels.map(function (l) {
        return "<span>" + esc(String(l)) + "</span>";
      }).join("") + "</div>";
    }
    return svg;
  }

  /* ------------------------------------------------------------------ *
   * Phase 23 (L2) — in-app notification bell (notifications_log backed) *
   * Works even when browser push permission was denied.                 *
   * ------------------------------------------------------------------ */
  function renderBell() {
    var host = document.getElementById("bellHost");
    if (!host) return;
    var session = readSession();
    if (!session || !session.key) { host.innerHTML = ""; return; }
    fetch("/api/v1/push/unread-count?session_id=" + encodeURIComponent(session.key))
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        var unread = d && typeof d.unread === "number" ? d.unread : 0;
        host.innerHTML =
          '<button id="bellBtn" class="pill' + (unread ? ' ok' : '') + '" style="cursor:pointer"' +
          ' title="Notifications (in-app bell — independent of browser permission)">🔔' +
          (unread ? '<strong id="bellCount">' + unread + '</strong>' : '') +
          '</button><div id="bellPanel" data-testid="bell-panel" style="display:none;' +
          'position:absolute;right:12px;top:52px;z-index:60;background:#0f172a;border:1px solid #334155;' +
          'border-radius:12px;padding:10px 14px;width:min(92vw,360px);box-shadow:0 18px 40px rgba(0,0,0,.5)"></div>';
        document.getElementById("bellBtn").addEventListener("click", toggleBellPanel);
      });
  }

  function toggleBellPanel() {
    var panel = document.getElementById("bellPanel");
    if (!panel) return;
    if (panel.style.display !== "none") { panel.style.display = "none"; return; }
    panel.style.display = "block";
    panel.innerHTML = '<p class="faint small">Loading…</p>';
    var session = readSession();
    fetch("/api/v1/push/log?limit=15&session_id=" + encodeURIComponent(session.key))
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        if (!d) { panel.innerHTML = '<p class="faint small">Log unavailable.</p>'; return; }
        var items = (d.items || []).map(function (it) {
          return '<div style="padding:6px 0;border-bottom:1px solid #1e293b">' +
            '<div><span class="pill' + (it.read_at ? '' : ' ok') + '">' + esc(it.kind) + '</span> ' +
            '<span class="faint small">' + esc(String(it.created_at || "").slice(0, 16).replace("T", " ")) + '</span></div>' +
            '<div class="small"><strong>' + esc(it.title) + '</strong></div>' +
            '<div class="faint small">' + esc(it.body) + '</div></div>';
        }).join("");
        panel.innerHTML =
          '<div style="display:flex;align-items:center;margin-bottom:4px"><strong>Notifications</strong>' +
          '<button id="bellMarkRead" class="pill ok" style="margin-left:auto;cursor:pointer;border:none">Mark all read (' + Number(d.unread) + ')</button></div>' +
          (items || '<p class="faint small">No notifications yet.</p>');
        var btn = document.getElementById("bellMarkRead");
        btn.addEventListener("click", function () {
          fetch("/api/v1/push/mark-all-read?session_id=" + encodeURIComponent(session.key), { method: "POST" })
            .then(function () { renderBell(); toggleBellPanel(); });
        });
      });
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

  /* ------------------------------------------------------------------ *
   * Phase 24 — PWA: service worker + install prompt                    *
   * ------------------------------------------------------------------ */
  var _deferredPrompt = null;
  if (typeof window !== "undefined") {
    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      _deferredPrompt = e;
      var host = document.getElementById("pwaInstallHost");
      if (host) {
        host.innerHTML = '<button id="pwaInstallBtn" class="btn" type="button" data-testid="pwa-install-btn">📲 Install FPL Intelligence</button>';
        var btn = document.getElementById("pwaInstallBtn");
        if (btn) btn.addEventListener("click", function () { triggerInstall(); });
      }
    });
    window.addEventListener("appinstalled", function () { _deferredPrompt = null; });
  }
  function triggerInstall() {
    if (!_deferredPrompt) return;
    _deferredPrompt.prompt();
    _deferredPrompt.userChoice.then(function () { _deferredPrompt = null; });
  }
  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    // avoid double-register on navigation
    if (window.__fpl_sw_registered) return;
    window.__fpl_sw_registered = true;
    navigator.serviceWorker.register("/static/sw.js").catch(function () {});
  }

  /* ------------------------------------------------------------------ *
   * Phase 24 — share / export helpers (M2)                             *
   * ------------------------------------------------------------------ */
  function shareOrCopy(opts) {
    var title = opts.title || "FPL Intelligence";
    var text = opts.text || "";
    var url = opts.url || window.location.href;
    if (navigator.share) {
      return navigator.share({ title: title, text: text, url: url }).catch(function () {
        return copyToClipboard(text + " " + url);
      });
    }
    return copyToClipboard(text + " " + url);
  }
  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () {
        return { copied: true };
      }).catch(function () {
        return fallbackCopy(text);
      });
    }
    return fallbackCopy(text);
  }
  function fallbackCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      return Promise.resolve({ copied: true });
    } catch (e) {
      return Promise.resolve({ copied: false, notSupported: true });
    }
  }
  function shareNotSupported() {
    return !(navigator.share || (navigator.clipboard && navigator.clipboard.writeText));
  }
  function downloadBlob(filename, blob) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 500);
  }
  function exportTxt(filename, text) {
    downloadBlob(filename, new Blob([text], { type: "text/plain;charset=utf-8" }));
  }
  function exportCsv(filename, rows, headers) {
    var lines = [];
    if (headers && headers.length) lines.push(headers.map(csvEsc).join(","));
    rows.forEach(function (r) {
      lines.push(r.map(csvEsc).join(","));
    });
    exportTxt(filename, lines.join("\n"));
  }
  function csvEsc(v) {
    var s = String(v === null || v === undefined ? "" : v);
    if (s.indexOf(",") !== -1 || s.indexOf('"') !== -1 || s.indexOf("\n") !== -1) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  return {
    esc: esc,
    renderNav: renderNav,
    renderBell: renderBell,
    crestHTML: crestHTML,
    hydrateCrests: hydrateCrests,
    CLUB_COLORS: CLUB_COLORS,
    fmtPrice: fmtPrice,
    fmtPts: fmtPts,
    fdrColor: fdrColor,
    fixtureStripHTML: fixtureStripHTML,
    sparklineSVG: sparklineSVG,
    ensureMainLandmark: ensureMainLandmark,
    NAV_PRIMARY: NAV_PRIMARY,
    NAV_SECONDARY: NAV_SECONDARY,
    registerSW: registerSW,
    triggerInstall: triggerInstall,
    shareOrCopy: shareOrCopy,
    copyToClipboard: copyToClipboard,
    shareNotSupported: shareNotSupported,
    exportTxt: exportTxt,
    exportCsv: exportCsv,
    downloadBlob: downloadBlob,
    session: {
      read: readSession,
      save: saveSession,
      clear: clearSession,
      bootstrap: bootstrapSession,
      updateChip: updateSessionChip
    }
  };
})();
