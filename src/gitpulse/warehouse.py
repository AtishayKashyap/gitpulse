"""Warehouse: raw landing tables (append/upsert, idempotent), extract state, and the
SQL model runner that rebuilds the star schema + metric views from raw data."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any

import sqlalchemy as sa
import structlog

log = structlog.get_logger(__name__)

RAW_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS raw.commits (
  repo text NOT NULL, sha text NOT NULL, parents text[] NOT NULL,
  author_name text NOT NULL, author_email text NOT NULL,
  authored_at timestamptz NOT NULL, committed_at timestamptz NOT NULL, subject text NOT NULL,
  loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, sha)
);
CREATE TABLE IF NOT EXISTS raw.commit_files (
  repo text NOT NULL, sha text NOT NULL, path text NOT NULL,
  additions integer, deletions integer,
  PRIMARY KEY (repo, sha, path)
);
-- sha -> GitHub login, from the commits API: links git emails to GitHub identities.
CREATE TABLE IF NOT EXISTS raw.commit_authors (
  repo text NOT NULL, sha text NOT NULL, author_login text, author_email text,
  PRIMARY KEY (repo, sha)
);
CREATE TABLE IF NOT EXISTS raw.pulls (
  repo text NOT NULL, number integer NOT NULL, payload jsonb NOT NULL,
  updated_at timestamptz NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS raw.reviews (
  repo text NOT NULL, pr_number integer NOT NULL, review_id bigint NOT NULL, payload jsonb NOT NULL,
  PRIMARY KEY (repo, review_id)
);
CREATE TABLE IF NOT EXISTS raw.workflow_runs (
  repo text NOT NULL, run_id bigint NOT NULL, payload jsonb NOT NULL,
  PRIMARY KEY (repo, run_id)
);
CREATE TABLE IF NOT EXISTS raw.repos (
  repo text PRIMARY KEY, payload jsonb NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS raw.extract_state (
  repo text NOT NULL, stream text NOT NULL, high_water text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, stream)
);
CREATE TABLE IF NOT EXISTS raw.http_cache (
  url text PRIMARY KEY, etag text NOT NULL, body jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS raw.deploy_workflows (
  repo text NOT NULL, workflow text NOT NULL, PRIMARY KEY (repo, workflow)
);
CREATE TABLE IF NOT EXISTS raw.identity_overrides (
  alias text PRIMARY KEY, canonical text NOT NULL
);
"""


def make_engine(url: str) -> sa.Engine:
    return sa.create_engine(url, pool_pre_ping=True)


def init(engine: sa.Engine) -> None:
    with engine.begin() as c:
        c.exec_driver_sql(RAW_DDL)


def upsert(conn: sa.Connection, table: str, rows: list[dict[str, Any]], key: list[str]) -> int:
    """INSERT ... ON CONFLICT DO UPDATE: re-extracting the same window is harmless."""
    if not rows:
        return 0
    cols = list(rows[0])
    updates = [c for c in cols if c not in key]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates) or None
    stmt = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT ({', '.join(key)}) "
        + (f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING")
    )
    conn.execute(sa.text(stmt), rows)
    return len(rows)


def get_state(conn: sa.Connection, repo: str, stream: str) -> str | None:
    return conn.execute(
        sa.text("SELECT high_water FROM raw.extract_state WHERE repo = :r AND stream = :s"),
        {"r": repo, "s": stream},
    ).scalar()


def set_state(conn: sa.Connection, repo: str, stream: str, value: str) -> None:
    upsert(
        conn,
        "raw.extract_state",
        [{"repo": repo, "stream": stream, "high_water": value}],
        ["repo", "stream"],
    )


@dataclass
class DbCache:
    """ETag cache persisted in Postgres, so conditional requests survive between CI runs."""

    engine: sa.Engine

    def get(self, url: str) -> tuple[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(
                sa.text("SELECT etag, body FROM raw.http_cache WHERE url = :u"), {"u": url}
            ).first()
        return (row.etag, row.body) if row else None

    def put(self, url: str, etag: str, body: Any) -> None:
        with self.engine.begin() as c:
            upsert(
                c, "raw.http_cache", [{"url": url, "etag": etag, "body": json.dumps(body)}], ["url"]
            )


def _sql_files(package: str) -> list[tuple[str, str]]:
    root = resources.files("gitpulse").joinpath("sql", package)
    return sorted((f.name, f.read_text()) for f in root.iterdir() if f.name.endswith(".sql"))


def transform(
    engine: sa.Engine,
    identities: dict[str, str] | None = None,
    deploy_workflows: dict[str, list[str]] | None = None,
) -> dict[str, float]:
    """Rebuild staging + marts + metric views from raw, atomically.

    Everything runs in ONE transaction: readers (API, report) see either the previous
    complete build or the new one, never a half-built schema.
    """
    timings: dict[str, float] = {}
    with engine.begin() as c:
        c.execute(sa.text("DELETE FROM raw.identity_overrides"))
        upsert(
            c,
            "raw.identity_overrides",
            [{"alias": k.lower(), "canonical": v.lower()} for k, v in (identities or {}).items()],
            ["alias"],
        )
        c.execute(sa.text("DELETE FROM raw.deploy_workflows"))
        upsert(
            c,
            "raw.deploy_workflows",
            [{"repo": r, "workflow": w} for r, ws in (deploy_workflows or {}).items() for w in ws],
            ["repo", "workflow"],
        )
        for group in ("models", "metrics"):
            for name, body in _sql_files(group):
                t0 = time.perf_counter()
                # Raw cursor with no params: SQL files are static, and psycopg must not
                # treat literal % (LIKE patterns) as placeholders. Same transaction.
                cur = c.connection.dbapi_connection.cursor()  # type: ignore[union-attr]
                try:
                    cur.execute(body)
                finally:
                    cur.close()
                timings[name] = round((time.perf_counter() - t0) * 1000, 1)
    log.info("transform.done", models=len(timings), total_ms=round(sum(timings.values()), 1))
    return timings


@dataclass
class TestResult:
    name: str
    failures: int


# Data tests: each query returns the offending rows; zero rows = pass.
DATA_TESTS: dict[str, str] = {
    "fact_commit.sha unique per repo": "SELECT repo, sha FROM mart.fact_commit GROUP BY 1, 2 HAVING count(*) > 1",
    "fact_commit.author_key not null": "SELECT sha FROM mart.fact_commit WHERE author_key IS NULL",
    "fact_commit -> dim_author": "SELECT f.sha FROM mart.fact_commit f LEFT JOIN mart.dim_author a USING (author_key) WHERE a.author_key IS NULL",
    "fact_pr -> dim_repo": "SELECT f.number FROM mart.fact_pr f LEFT JOIN mart.dim_repo r USING (repo) WHERE r.repo IS NULL",
    "fact_pr merged_at >= created_at": "SELECT number FROM mart.fact_pr WHERE merged_at < created_at",
    "fact_review -> fact_pr": "SELECT r.review_id FROM mart.fact_review r LEFT JOIN mart.fact_pr p USING (repo, number) WHERE p.number IS NULL",
    "fact_file_change additions >= 0": "SELECT sha FROM mart.fact_file_change WHERE additions < 0 OR deletions < 0",
}


def run_data_tests(engine: sa.Engine) -> list[TestResult]:
    with engine.connect() as c:
        return [TestResult(n, len(c.execute(sa.text(q)).all())) for n, q in DATA_TESTS.items()]
