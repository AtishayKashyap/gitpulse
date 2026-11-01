## What & why

## How it was tested
- [ ] `make lint typecheck test`
- [ ] New metric: SQL view + known-answer test in `tests/test_warehouse.py`
- [ ] New raw stream: incremental high-water mark + upsert key + respx fixture

## Risk
<!-- Does this change metric semantics (numbers in past reports would shift)? -->
