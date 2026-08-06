# Real-Data Entity Resolution

_Generated 2026-08-03T12:47:02.522974+00:00_

## Provider: real_fpl

- Matched players (external-id mappings): 866
- Matched teams (external-id mappings): 20
- Unmatched players: 0
- Ambiguous players: 0
- Unmatched teams: 0
- Manual overrides: 0

### Method
Canonical identity is the FPL `element` ID (provider-id mapping).
Players are NEVER merged on name alone. Cross-provider joins use
deterministic name normalization + explicit manual overrides, with an
unresolved queue so nothing is silently dropped.