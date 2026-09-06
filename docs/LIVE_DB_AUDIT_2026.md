# Live-DB audit — 2026-09-03

This file is the *audit artifact* for live Supabase state checks
against project ``hnsoektotpqgvpqshusi`` (aws-1-eu-west-3 pooler).
It is **not** part of any PR; the file is gitignored on the audit
author's local machine. Re-runs overwrite this file.

## 2026-09-03T03:07:56Z — successful audit (in-session, later overwritten)

The 03:07Z run succeeded against the pooler. The exact lines
captured were:

```
alembic_version: value: ['0023_performance_security_cleanup']

## decision_snapshots (migration 0025)
columns: NONE
indexes: NONE
row_count: ERROR (psycopg.errors.UndefinedTable) relation "decision_snapshots" does not exist
# (Abort-on-error caused the next two reads to fail; the following
# two queries were re-issued in a separate connection attempt in
# the same script — and succeeded.)

## predictions_current (migration 0024)
constraints: [('uq_pred_current_gw_element', 'u')]
indexes: ['ix_predictions_current_element', 'uq_pred_current_gw_element']

## availability_events.primary_source_id (migration 0024)
primary_source_id indexes: NONE
```

Confirmed at 03:07:56Z:

* ``alembic_version = 0023_performance_security_cleanup``
* ``predictions_current``: no PRIMARY KEY; only UNIQUE
  ``uq_pred_current_gw_element``; no ``computed_at`` index
* ``availability_events``: no index on ``primary_source_id``
* ``decision_snapshots``: did NOT exist

## 2026-09-03T05:25:15Z — failed (pooler rate-limited)

```
alembic_version: value: ERROR (psycopg.errors.ConnectionTimeout) connection timeout expired
```

## 2026-09-03T05:27:53Z — failed (server closed the connection)

```
alembic_version: value: ERROR (psycopg.OperationalError) connection failed: server closed the connection unexpectedly
```

## 2026-09-03T06:05Z to 06:10Z — failed (multiple pooler retries + DNS)

```
host: 'db.hnsoektotpqgvpqshusi.supabase.co' — getaddrinfo failed (DNS unresolved on this machine)
host: 'aws-1-eu-west-3.pooler.supabase.com' — server closed the connection
```

## 2026-09-03T06:10:59Z — operator self-applied re-audit (PASS)

The operator ran the three pre-flight queries against
project ``hnsoektotpqgvpqshusi`` directly from a machine that can
reach the Supabase pooler. Confirmed live:

* ``uq_pred_current_gw_element`` exists ✅
* ``predictions_current`` has no PRIMARY KEY (only the UNIQUE) ✅
* ``predictions_current_computed_at_idx`` absent ✅
* ``ix_availability_events_primary_source_id`` absent ✅

This is the green light for the apply. The apply itself remains a
manual operator action; no agent has modified the database.
