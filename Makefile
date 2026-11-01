.PHONY: install test lint typecheck run dq serve

PY := .venv/bin

install:
	python3 -m venv .venv && $(PY)/pip install -e ".[dev]"

test:      ## needs Postgres (GITPULSE_TEST_DATABASE_URL)
	$(PY)/pytest

lint:
	$(PY)/ruff check src tests && $(PY)/ruff format --check src tests

typecheck:
	$(PY)/mypy

run:       ## extract + transform + report into ./site
	$(PY)/gitpulse run

dq:        ## validate the warehouse with sentinel-dq
	$(PY)/pip install -q "sentinel-dq @ git+https://github.com/AtishayKashyap/sentinel-dq"
	$(PY)/sentinel run -c dq/sentinel.yml

serve:
	$(PY)/gitpulse serve
