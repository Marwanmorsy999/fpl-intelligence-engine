# Phase 10.4 — Personalized Squad Decision Engine

## Status

**INITIALIZED** (2026-08-19). Connects the user's FPL squad to the Phase 6
Decision Optimization Engine to produce personalized decisions.

## Overview

Phase 10.4 bridges the gap between the user's actual FPL squad and the Phase 6
optimization algorithms. Previously, the Phase 6 engine could evaluate
candidate actions in the abstract; now it can consume a concrete squad and
return a structured `DecisionReport`.

## Architecture

```
User Squad (POST /api/v1/squad)
    │
    ▼
SquadService (in-memory store)
    │
    ▼
DecisionOptimizerBridge
    │
    ├── StartingXIOptimizer  ──► optimized Starting XI + Bench Order
    ├── CaptainOptimizer     ──► Captain recommendation
    ├── MultiTransferPlanner ──► Transfer plan (Roll vs Hit)
    └── ChipSimulator        ──► Chip recommendation
    │
    ▼
DecisionReport (GET /api/v1/decisions)
```

## Modules

| Module | Responsibility |
|--------|----------------|
| `squad/models.py` | Pydantic models: `SquadStateCreate`, `SquadStateResponse`, `DecisionReport`, `TransferPlan`, `ChipRecommendation`, `CaptainRecommendation`. |
| `squad/service.py` | `SquadService` — in-memory, thread-safe store for squad state. |
| `squad/bridge.py` | `DecisionOptimizerBridge` — converts API squad payload to optimization-domain `SquadState`, delegates to Phase 6 optimizers, returns `DecisionReport`. |
| `api/routes/squad.py` | FastAPI router with `POST /squad`, `GET /squad`, `GET /decisions`. |
| `web/dashboard.py` | Extended with `/api/v1/dashboard/squad-decisions` proxy endpoint. |
| `web/static/dashboard.html` | Updated with Squad Decisions section. |

## API Endpoints

### `POST /api/v1/squad`

Set the user's squad state. Replaces any previously stored squad.

**Request body:**
```json
{
  "player_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
  "captain_id": 1,
  "vice_captain_id": 2,
  "bank": 1.5,
  "free_transfers": 2,
  "chips_available": ["wildcard", "free_hit"],
  "gameweek": 5,
  "player_positions": { "1": 1, "2": 2, "3": 3, "4": 4 },
  "player_prices": { "1": 5.0, "2": 5.0 },
  "player_teams": { "1": 1, "2": 2 }
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `player_ids` | list[int] | yes | Exactly 15 player IDs. |
| `captain_id` | int | yes | Current captain. |
| `vice_captain_id` | int | yes | Current vice-captain. |
| `bank` | float | no | Available funds in millions. |
| `free_transfers` | int | no | Number of free transfers available. |
| `chips_available` | list[str] | no | Chips still available. |
| `gameweek` | int | yes | Current FPL gameweek. |
| `player_positions` | dict | no | Optional: player_id → position_code (1=GK, 2=DEF, 3=MID, 4=FWD). |
| `player_prices` | dict | no | Optional: player_id → price in millions. |
| `player_teams` | dict | no | Optional: player_id → team_id. |

**Response (200):** `SquadStateResponse` with `updated_at` timestamp.

### `GET /api/v1/squad`

Retrieve the current squad state.

**Response (200):** `SquadStateResponse` or `null` if not set.

### `GET /api/v1/decisions`

Generate a personalized `DecisionReport` for the stored squad.

**Response (200):** `DecisionReport`:
```json
{
  "generated_at": "2026-08-19T...",
  "gameweek": 5,
  "starting_xi": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
  "bench_order": [14, 15],
  "captain": {
    "player_id": 1,
    "expected_points": 5.5,
    "expected_gain": 5.5,
    "probability_positive": 0.8,
    "confidence": 0.8,
    "main_reason": "Highest expected value captain.",
    "main_risk": "Low ceiling"
  },
  "vice_captain": 2,
  "transfer_plan": {
    "action_type": "roll",
    "transfers_in": [],
    "transfers_out": [],
    "hit_cost": 0,
    "expected_gain": 0.0,
    "probability_positive": 0.5,
    "confidence": 0.7,
    "main_reason": "Roll transfer provides better horizon EV.",
    "main_risk": "Missed points on bench."
  },
  "chip_recommendation": null,
  "meta": {}
}
```

**Response (404):** No squad configured.

## Dashboard Integration

The web dashboard at `GET /dashboard` now includes a **Squad Decisions** section
that proxies `GET /api/v1/decisions` and displays:

- **Starting XI** — optimized 11 players
- **Bench Order** — remaining 4 players
- **Captain** — recommended captain with EV and reason
- **Transfer Plan** — action type, net EV, confidence
- **Chip Recommendation** — chip name, expected gain, reason

If no squad is configured, the section shows a message directing the user to
`POST /api/v1/squad`.

## Testing & Quality

- `tests/unit/test_phase10_4_squad.py` — **17 tests** covering:
  - `SquadService` (set/get, replace, clear, empty state)
  - `DecisionOptimizerBridge` (with and without metadata, naive XI fallback,
    captain in starting XI, chip recommendation gating)
  - API endpoints (POST/GET squad, GET decisions, 404 handling, full metadata)
- **Full suite:** 772 tests passing (was 755, +17 new).
- `ruff` clean on all new modules.
- `mypy` clean on all new modules.

## Constraints honoured

- **No Phase 6 core algorithms modified.** The bridge only consumes the
  existing optimizer interfaces.
- **No live LLM API calls in `pytest`.** All tests use `StaticPredictionProvider`
  and `MockLLMProvider`.
- **No hardcoded API keys.** The bridge reads credentials from the injected
  provider only.
- **No database schema changes.** `SquadService` is in-memory; no migration
  required.

## Next

Phase 10.5 is unblocked.