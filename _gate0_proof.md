# GATE 0 PROD PROOF (v2.6.0-sync-final)

## 1. fpl-view JSON
```json
{
    "current_event":  1,
    "picks_current":  {
                          "gw":  1,
                          "ids":  [
                                      4,
                                      32,
                                      109,
                                      173,
                                      289,
                                      399,
                                      411,
                                      423,
                                      426,
                                      455,
                                      463,
                                      473,
                                      480,
                                      529,
                                      552
                                  ],
                          "status":  200
                      },
    "picks_next":  {
                       "gw":  2,
                       "ids":  [

                               ],
                       "status":  404
                   },
    "entry_summary":  {
                          "name":  "banhawayaFC",
                          "id":  2295006,
                          "current_event":  1,
                          "last_deadline_bank":  0,
                          "last_deadline_total_transfers":  0,
                          "last_deadline_bank_tenths":  0
                      },
    "fpl_history":  {
                        "gw":  2,
                        "event_transfers":  null,
                        "event_transfers_cost":  null,
                        "latest_event":  1,
                        "latest_event_transfers":  0,
                        "note":  "FPL history: no GW2 row yet â GW not finished Â· latest GW1: 0 transfers"
                    }
}
```

## 2. Official history cross-check
FPL /api/entry/2295006/history/ current[0]: event 1, event_transfers 0 (via fpl_history.latest_event)
Fpl-view fpl_history.note: FPL history: no GW2 row yet â GW not finished Â· latest GW1: 0 transfers

## 3. Sync-now after one click (branch C: no confirmed transfer)
```json
{
    "job_id":  "ceb3853838bc",
    "state":  "done",
    "ok":  true,
    "session_id":  "2295006",
    "gameweek":  1,
    "picks_gw":  1,
    "banner":  "No confirmed transfer found on FPL for GW2 â finish it on FPL, then sync.",
    "before_ids":  [
                       4,
                       32,
                       109,
                       173,
                       289,
                       399,
                       411,
                       423,
                       426,
                       455,
                       463,
                       473,
                       480,
                       529,
                       552
                   ],
    "after_ids":  [
                      4,
                      32,
                      109,
                      173,
                      289,
                      399,
                      411,
                      423,
                      426,
                      455,
                      463,
                      473,
                      480,
                      529,
                      552
                  ],
    "transfers_in":  [

                     ],
    "transfers_out":  [

                      ],
    "detected_transfer":  null,
    "started_at":  "2026-08-25T17:43:57.607922+00:00",
    "finished_at":  "2026-08-25T17:44:04.469604+00:00",
    "synced_at":  "2026-08-25T17:44:04.469604+00:00",
    "chose_rule":  "no_confirmed_transfer",
    "picks_next_status":  404,
    "ids_hash_current":  7241977647431888182,
    "ids_hash_next":  null
}
```
Banner text: No confirmed transfer found on FPL for GW2 â finish it on FPL, then sync.
chose_rule: no_confirmed_transfer picks_next_status: 404

## 4. Health
```json
{
    "status":  "ok",
    "db":  "connected",
    "version":  "2.6.1"
}
```
