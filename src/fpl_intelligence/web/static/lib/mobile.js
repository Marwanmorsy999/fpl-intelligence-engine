/**
 * mobile.js — shared mobile UX utilities (v2.9.0)
 * Loaded by every page via app.js or direct <script> tag.
 * No dependencies, no bundler required.
 */

/* =========================================================================
   FPL Photo URL helper (M3)
   Photo field from API is e.g. "100000.jpg"
   CDN: https://resources.premierleague.com/premierleague/photos/players/110x140/p{code}.png
   ========================================================================= */
function fplPhotoUrl(photoOrCode) {
  if (!photoOrCode) return null;
  // Accept either the raw code integer or "XXXXXX.jpg" string
  const code = String(photoOrCode).replace(/\.[^.]+$/, '');
  return `https://resources.premierleague.com/premierleague/photos/players/110x140/p${code}.png`;
}

function fplPhotoUrlSmall(photoOrCode) {
  if (!photoOrCode) return null;
  const code = String(photoOrCode).replace(/\.[^.]+$/, '');
  return `https://resources.premierleague.com/premierleague/photos/players/40x40/p${code}.png`;
}

/* =========================================================================
   Availability badge helper (C3)
   Renders the status + chance_of_playing as an FPL-style badge
   ========================================================================= */
function availBadgeHtml(status, chanceNext) {
  if (!status || status === 'a') return '';
  const statusMap = {
    'd': { cls: 'avail-d', icon: '⚠️', label: 'Doubt' },
    'i': { cls: 'avail-i', icon: '🤕', label: 'Injured' },
    's': { cls: 'avail-s', icon: '🚫', label: 'Suspended' },
    'u': { cls: 'avail-u', icon: '✖', label: 'Unavailable' },
    'n': { cls: 'avail-u', icon: '✖', label: 'Not in squad' },
  };
  const info = statusMap[status] || { cls: 'avail-u', icon: '?', label: status.toUpperCase() };
  const pct = chanceNext != null ? ` ${chanceNext}%` : '';
  return `<span class="avail-badge ${info.cls}">${info.icon} ${info.label}${pct}</span>`;
}

/* =========================================================================
   Pull-to-refresh (M6)
   Call initPullToRefresh(onRefresh) once per page.
   ========================================================================= */
function initPullToRefresh(onRefresh) {
  let startY = 0;
  let pulling = false;
  const threshold = 70;

  const indicator = document.createElement('div');
  indicator.className = 'ptr-indicator';
  indicator.innerHTML = '<div class="ptr-spinner"></div> Refreshing…';
  document.body.appendChild(indicator);

  document.addEventListener('touchstart', (e) => {
    if (window.scrollY === 0) {
      startY = e.touches[0].clientY;
      pulling = true;
    }
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 20) indicator.classList.add('visible');
  }, { passive: true });

  document.addEventListener('touchend', async (e) => {
    if (!pulling) return;
    pulling = false;
    const dy = e.changedTouches[0].clientY - startY;
    if (dy > threshold) {
      try { await onRefresh(); } catch (_) {}
    }
    indicator.classList.remove('visible');
  }, { passive: true });
}

/* =========================================================================
   Haptic feedback (M9)
   Thin wrapper — silently no-ops when not supported
   ========================================================================= */
function haptic(type = 'light') {
  if (!navigator.vibrate) return;
  const patterns = { light: [10], medium: [20], heavy: [40], success: [10, 30, 10] };
  navigator.vibrate(patterns[type] || [10]);
}

/* =========================================================================
   Swipe-to-dismiss drawer (M4)
   Call initSwipeDismiss(sheetEl, onDismiss)
   ========================================================================= */
function initSwipeDismiss(sheetEl, onDismiss) {
  let startY = 0;
  let currentY = 0;
  let dragging = false;

  sheetEl.addEventListener('touchstart', (e) => {
    startY = e.touches[0].clientY;
    dragging = true;
    sheetEl.classList.add('swiping');
  }, { passive: true });

  sheetEl.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    currentY = e.touches[0].clientY;
    const dy = Math.max(0, currentY - startY);
    sheetEl.style.transform = `translateY(${dy}px)`;
  }, { passive: true });

  sheetEl.addEventListener('touchend', () => {
    if (!dragging) return;
    dragging = false;
    sheetEl.classList.remove('swiping');
    const dy = currentY - startY;
    if (dy > 120) {
      sheetEl.style.transform = `translateY(100%)`;
      haptic('light');
      setTimeout(() => onDismiss?.(), 300);
    } else {
      sheetEl.style.transform = '';
    }
  }, { passive: true });
}

/* =========================================================================
   Risk profile (P1)
   Reads from localStorage or session; pages can call getRiskProfile()
   and adjust recommendation wording accordingly.
   ========================================================================= */
const RISK_PROFILES = {
  aggressive: {
    label: 'Aggressive',
    desc: 'Chasing rank — take hits, back differentials',
    transferHitThreshold: 0.4,   // take hit if P(beat roll) > 40%
    captainDifferentialBonus: 0.15, // bonus weight to low-ownership captains
    chipActivationMultiplier: 0.85, // lower threshold to activate chips
  },
  balanced: {
    label: 'Balanced',
    transferHitThreshold: 0.55,
    captainDifferentialBonus: 0.05,
    chipActivationMultiplier: 1.0,
  },
  safe: {
    label: 'Safe',
    desc: 'Protecting rank — avoid hits, template captains',
    transferHitThreshold: 0.70,
    captainDifferentialBonus: -0.05, // slight penalty to differentials
    chipActivationMultiplier: 1.20,  // higher threshold before using chips
  },
};

function getRiskProfile() {
  const stored = localStorage.getItem('fpl_risk_profile') || 'balanced';
  return RISK_PROFILES[stored] || RISK_PROFILES.balanced;
}

function setRiskProfile(key) {
  if (RISK_PROFILES[key]) {
    localStorage.setItem('fpl_risk_profile', key);
    haptic('medium');
  }
}

function riskProfileBadgeHtml(key) {
  const p = RISK_PROFILES[key] || RISK_PROFILES.balanced;
  return `<span class="risk-badge ${key}">${p.label}</span>`;
}

/* =========================================================================
   Price change countdown (F3)
   FPL price updates run nightly ~1am UK time (UTC+1 BST / UTC+0 GMT)
   ========================================================================= */
function priceChangeCountdown() {
  const now = new Date();
  // Next 1am UK — approximate as UTC+0 in winter, UTC+1 in summer
  const isUKSummer = now.getMonth() >= 2 && now.getMonth() <= 9; // Mar–Oct
  const utcOffset = isUKSummer ? 1 : 0;
  const target = new Date(now);
  target.setUTCHours(1 - utcOffset, 0, 0, 0);
  if (target <= now) target.setUTCDate(target.getUTCDate() + 1);
  const diffMs = target - now;
  const h = Math.floor(diffMs / 3600000);
  const m = Math.floor((diffMs % 3600000) / 60000);
  const imminent = h === 0 && m < 30;
  return { h, m, imminent, label: h > 0 ? `${h}h ${m}m` : `${m}m` };
}

/* =========================================================================
   Injury / news watch (P7)
   Filters players with news added in last 24h from a players API response.
   ========================================================================= */
function recentInjuryAlerts(players, maxAgeHours = 24) {
  const cutoff = Date.now() - maxAgeHours * 3600_000;
  return players.filter(p => {
    if (!p.news || p.status === 'a') return false;
    // news_added is not yet stored — show all non-available players with news
    return !!p.news;
  }).map(p => ({
    player: p.web_name,
    status: p.status,
    chance: p.chance_of_playing_next_round,
    news: p.news,
  }));
}

/* =========================================================================
   FPL-compatible picks format (C6)
   Converts our squad response into FPL picks[] shape
   ========================================================================= */
function toFplPicksFormat(squadPlayers, captainId, viceCaptainId) {
  return squadPlayers.map((p, idx) => ({
    element: p.fpl_element_id ?? p.id,
    position: idx + 1,
    multiplier: p.fpl_element_id === captainId ? 2 : 1,
    is_captain: p.fpl_element_id === captainId || p.id === captainId,
    is_vice_captain: p.fpl_element_id === viceCaptainId || p.id === viceCaptainId,
  }));
}

/* =========================================================================
   Chip name normalizer (C5)
   FPL API uses 'wildcard','freehit','3xc','bboost'
   Our backend uses 'wildcard','free_hit','triple_captain','bench_boost'
   ========================================================================= */
const CHIP_NAMES = {
  'free_hit': 'freehit',
  'triple_captain': '3xc',
  'bench_boost': 'bboost',
  'wildcard': 'wildcard',
  // reverse
  'freehit': 'free_hit',
  '3xc': 'triple_captain',
  'bboost': 'bench_boost',
};
function normalizeChipName(name, toFpl = true) {
  if (toFpl) return CHIP_NAMES[name] || name;
  return CHIP_NAMES[name] || name;
}

function chipDisplayName(internalName) {
  const map = {
    'wildcard': 'Wildcard',
    'free_hit': 'Free Hit',
    'freehit': 'Free Hit',
    'triple_captain': 'Triple Captain',
    '3xc': 'Triple Captain',
    'bench_boost': 'Bench Boost',
    'bboost': 'Bench Boost',
  };
  return map[internalName] || internalName;
}

/* Export to window for page scripts */
Object.assign(window, {
  fplPhotoUrl, fplPhotoUrlSmall,
  availBadgeHtml,
  initPullToRefresh,
  haptic,
  initSwipeDismiss,
  getRiskProfile, setRiskProfile, riskProfileBadgeHtml, RISK_PROFILES,
  priceChangeCountdown,
  recentInjuryAlerts,
  toFplPicksFormat,
  normalizeChipName, chipDisplayName,
});
