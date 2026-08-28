# Stage 2B.2 Data Blocker

Status: blocked in the current operator environment (2026-08-28).

Real canonical historical validation cannot start because the existing
Supabase PostgreSQL pooler is not reachable over TCP. The configured
production `DATABASE_URL` was present, but its value was not printed or
changed.

## Connectivity evidence

The credential-safe diagnostic used the existing `validation_database_url()`
and Psycopg 3 SQLAlchemy configuration with no writes or schema changes:

```text
DATABASE_URL_PRESENT=True
DNS_HOST=aws-1-eu-west-3.pooler.supabase.com
DNS_RESULT=RESOLVED addresses=2
TCP_RESULT=FAILED error=TimeoutError: timed out
TCP_IPv4_13.36.13.135=FAILED TimeoutError: timed out
TCP_IPv4_52.47.148.215=FAILED TimeoutError: timed out
```

The failure is at **TCP connection establishment to port 5432**. Because
TCP did not complete:

- TLS was not reached.
- PostgreSQL authentication was not reached.
- `SELECT 1` was not reached.
- Canonical metadata queries were not reached.
- The team-strength evaluator was not run.

The per-address checks timed out for both DNS-resolved IPv4 addresses, which
is consistent with an outbound network, firewall, proxy, VPN, or network
allow-list block affecting the current operator environment. It is not
evidence of an authentication or SQL-query failure.

## Required operator/environment action

Restore permitted outbound TCP access from this environment to the existing
Supabase pooler host `aws-1-eu-west-3.pooler.supabase.com` on port `5432`.
The operator should check the local egress firewall, corporate proxy/VPN,
container or runner network policy, and any Supabase network restrictions.
Do not change connection settings, credentials, pool sizes, timeouts, or
database data as a workaround.

After access is restored, rerun the same connectivity diagnostic, then run
the existing read-only preflight and proceed to the evaluator only if all
three canonical seasons and temporal coverage are available:

```text
python scripts/preflight_minutes_validation.py
python scripts/evaluate_team_strength.py
```

No alternate provider, synthetic data, ingestion, migration, schema change,
or model change is permitted for this stage.

## Validation and promotion

Historical coverage for `2022-23`, `2023-24`, and `2024-25` is unverified
because the database could not be reached. No validation metrics or
calibration results exist. TeamStrengthEngine status remains:

`INSUFFICIENT EVIDENCE`

The promotion gate was not changed.
