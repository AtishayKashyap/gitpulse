"""Typed read access to the metric views (shared by the report and the API)."""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa

VIEWS = {
    "weekly_activity": "m_weekly_activity",
    "pr_cycle": "m_pr_cycle",
    "review_coverage": "m_review_coverage",
    "hotspots": "m_hotspots",
    "bus_factor": "m_bus_factor",
    "delivery": "m_delivery",
    "contributors": "m_contributors",
    "activity_heatmap": "m_activity_heatmap",
}


def read(
    engine: sa.Engine, name: str, repo: str | None = None, limit: int | None = None
) -> pd.DataFrame:
    """Read a metric view. `name` is checked against an allow-list (it becomes an
    identifier); filters are bound parameters."""
    view = VIEWS[name]  # KeyError for unknown names, before any SQL is built
    sql = f"SELECT * FROM mart.{view}"
    params: dict[str, str | int] = {}
    if repo:
        sql += " WHERE repo = :repo"
        params["repo"] = repo
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    with engine.connect() as c:
        return pd.read_sql(sa.text(sql), c, params=params)


def summary(engine: sa.Engine) -> pd.DataFrame:
    """One row per repo: the headline numbers for the report header."""
    q = """
    SELECT r.repo, r.language, r.stars,
      -- False when only git history was available: PR/review numbers are unknown, not 0.
      EXISTS (SELECT 1 FROM raw.repos x WHERE x.repo = r.repo AND x.payload->>'source' IS NULL) AS has_api_data,
      (SELECT count(*) FROM mart.fact_commit c WHERE c.repo = r.repo AND NOT c.is_merge) AS commits,
      (SELECT count(DISTINCT author_key) FROM mart.fact_commit c JOIN mart.dim_author a USING (author_key)
         WHERE c.repo = r.repo AND NOT a.is_bot) AS contributors,
      (SELECT count(*) FROM mart.fact_pr p WHERE p.repo = r.repo AND p.merged_at IS NOT NULL) AS merged_prs,
      v.coverage AS review_coverage, v.cycle_p50_h, v.first_review_p50_h,
      (SELECT min(bus_factor) FROM mart.m_bus_factor b WHERE b.repo = r.repo) AS min_bus_factor
    FROM mart.dim_repo r LEFT JOIN mart.m_review_coverage v USING (repo)
    ORDER BY r.repo
    """
    with engine.connect() as c:
        return pd.read_sql(sa.text(q), c)
