# Phase 15.2 — Live Verification Suite Report

**Date:** 2026-08-22
**Target:** `https://fpl-intelligence-engine-foundation.vercel.app`
**Deployed version (health):** 1.4.0 (db: connected)
**Deployed tag:** v1.5.3-cache-fix

## Result: ALL TESTS PASS

| Test | Status | Time | Key Data Points |
|------|--------|------|-----------------|
| 1 — Real-ID end-to-end (entry 794561) | PASS | POST 858 ms / GET 2481 ms | 15 real players; Haaland £15.5m (elem 411); entry "CAPTAIN OB"; xPTS range 1.07–7.9 (differentiated); `prediction_source: pre-season-proxy-v2` on all players; chain covered 600 players |
| 2 — Session isolation (new cache) | PASS | POST A 498 ms / POST B 253 ms / GET A 2028 ms / GET B 2020 ms | Squad A xi: Haaland(411),Mbeumo(427),Semenyo(397),Wirtz(366)…; Squad B xi: demo players 12,4,13,26,25,5…; no cross-contamination; per-instance cache isolates correctly |
| 3 — Manual entry flow | PASS | POST 410 ms / GET 2248 ms | 15 players (2 GK / 5 DEF / 5 MID / 3 FWD); captain Haaland(445) 7.74 xPTS; vice Isak(412); sensible XI; transfer plan: roll; no timeout |

## Test 1 — Real-ID End-to-end

- `POST /api/v1/squad/from-fpl {"entry_id": 794561}` → **200** in 858 ms
- `GET /api/v1/decisions?session_id=794561` → **200** in 2481 ms
- Decisions < 10 s: **yes** (2.5 s)
- 15 real player names: **yes** (Trafford, Mosquera, Shaw, Tarkowski, Semenyo, E.Le Fée, Mbeumo, Wirtz, João Pedro, Haaland, Richarlison, Kinsky, Sarr, Robertson, Ajer)
- Haaland official price £15.5m: **confirmed** (player 411, price 15.5)
- Differentiated xPTS (not flat): **yes** (range 1.07–7.9 across the squad)
- `prediction_source` label present: **yes** (`pre-season-proxy-v2` on every player)
- Chain metadata: source `pre-season-proxy-v2`, 600 covered players, Understat matched 338

## Test 2 — Session Isolation Under New Cache

- Squad A: `POST /api/v1/squad/from-fpl {"entry_id": 794561}` → session stored under `794561`
- Squad B: `POST /api/v1/squad/demo?session_id=demo-test-1` → isolated demo squad
- Interleaved `GET /api/v1/decisions` for both session_ids:
  - Session A starting xi: `411,397,427,165,366,542,527,229,11,385,502` (captain 411)
  - Session B starting xi: `12,4,13,26,25,5,14,15,6,1,24` (captain 12)
- Different player lists per session: **yes**
- No cross-contamination: **confirmed** — zero overlap between A and B starting XIs
- Per-instance cache does not leak: **confirmed**

## Test 3 — Manual Entry Flow

- Built valid 15-player manual squad: 2 GK (383,355) / 5 DEF (389,422,361,362,363) / 5 MID (465,399,400,401,491) / 3 FWD (445,412,500)
- Captain: Haaland (445), Vice: Isak (412)
- `POST /api/v1/squad?session_id=manual-test-3` → **200** in 410 ms
- `GET /api/v1/decisions?session_id=manual-test-3` → **200** in 2248 ms
- Completes without timeout: **yes**
- Sensible XI: **yes** (Wirtz, Gakpo, Szoboszlai, Barnes, B.Fernandes, Virgil, A.Becker, Woltemade, Haaland, Rodon, Guéhi)
- Captain applied: **yes** (445, 7.74 xPTS)
- Transfer plan: **roll** (sensible — no forced hits)

## Verdict

Phase 15.2 verification suite **passes** on live v1.5.3.
