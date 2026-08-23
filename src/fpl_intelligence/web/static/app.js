/* Phase 19.0 — shared page chrome: nav, health pill, crests, fetch helpers.
   Loaded by every page; exposes window.FPLApp. No frameworks, no console noise. */
"use strict";
window.FPLApp = (function () {
  var NAV = [
    { href: "/dashboard", label: "Decisions" },
    { href: "/my-team", label: "My Team" },
    { href: "/track-record", label: "Track Record" },
    { href: "/live", label: "Live" },
    { href: "/sources", label: "Sources" },
    { href: "/connect", label: "Connect" }
  ];

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

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
      '<span id="healthPill" class="pill" style="margin-left:auto">● …</span>' +
      "</div></div>";
    checkHealth();
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

  function fmtPrice(v) { return v === null || v === undefined ? "£—" : "£" + Number(v).toFixed(1); }
  function fmtPts(v) { return v === null || v === undefined ? "–" : Number(v).toFixed(1); }

  return {
    esc: esc,
    renderNav: renderNav,
    crestHTML: crestHTML,
    hydrateCrests: hydrateCrests,
    CLUB_COLORS: CLUB_COLORS,
    fmtPrice: fmtPrice,
    fmtPts: fmtPts
  };
})();
