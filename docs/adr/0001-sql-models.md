# ADR 0001: Versioned SQL models instead of dbt

**Status:** accepted (changes the original plan)

## Context
The transform layer is about 10 models and 8 metric views on one Postgres database.

## Decision
Plain `.sql` files in `src/gitpulse/sql/{models,metrics}`, executed in filename order **inside one transaction** that drops and rebuilds `mart`. Data tests live in `warehouse.DATA_TESTS` (each is a query returning offending rows) and run after every build. sentinel-dq adds an independent gate in CI.

## Why not dbt
dbt shines with many models, many authors, multiple warehouses and incremental materialisations. Here it would add a second toolchain (profiles, a Jinja dialect, a manifest) for a small DAG. The plain-SQL approach keeps every statement reviewable as-is. The single transaction gives atomic swaps for free: readers never see a half-built schema. Rebuilding everything takes 0.7 s on 8.7k commits and 42k file changes.

## When to revisit
More than about 30 models, models that need incremental materialisation, or several warehouses. At that point dbt's DAG, docs and test ecosystem pay for themselves.
