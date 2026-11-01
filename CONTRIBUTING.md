# Contributing

- Conventional Commits; PRs via the template; CI green plus one CODEOWNERS approval.
- `make lint typecheck test` (needs Postgres; see `GITPULSE_TEST_DATABASE_URL` in `tests/conftest.py`).
- **New metric:** add a view in `src/gitpulse/sql/metrics/` and a known-answer test in `tests/test_warehouse.py`.
- **New raw stream:** high-water mark + upsert key + a respx fixture covering pagination.
- Recommended branch protection on `main`: require a PR, one approval, and passing `test`, `pipeline` and `docker` checks.
