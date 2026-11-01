"""Extractor end to end: a real local git repo + a mocked GitHub API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
import sqlalchemy as sa

from gitpulse import warehouse
from gitpulse.config import Settings
from gitpulse.extract import Extractor
from gitpulse.github import API, GitHub

REPO = "o/r"


async def _noop(_: float) -> None:
    return None


def mock_api(updated: str = "2026-09-05T12:00:00Z") -> None:
    respx.get(f"{API}/repos/{REPO}").mock(
        return_value=httpx.Response(
            200, json={"default_branch": "main", "language": "Python", "stargazers_count": 3}
        )
    )
    respx.get(f"{API}/repos/{REPO}/commits").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "sha": "deadbeef",
                    "author": {"login": "asha"},
                    "commit": {
                        "author": {"email": "asha@corp.com"},
                        "committer": {"date": "2026-09-05T10:00:00Z"},
                    },
                },
            ],
        )
    )
    respx.get(f"{API}/repos/{REPO}/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 2,
                    "updated_at": updated,
                    "user": {"login": "ravi-dev"},
                    "created_at": "2026-09-03T10:00:00Z",
                    "merged_at": "2026-09-04T10:00:00Z",
                },
                {
                    "number": 1,
                    "updated_at": "2010-01-01T00:00:00Z",
                    "user": {"login": "x"},
                },  # older than the window: stops pagination
            ],
        )
    )
    respx.get(f"{API}/repos/{REPO}/pulls/2/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 99,
                    "user": {"login": "asha"},
                    "state": "APPROVED",
                    "submitted_at": "2026-09-03T12:00:00Z",
                },
            ],
        )
    )
    respx.get(f"{API}/repos/{REPO}/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 5,
                        "name": "CI",
                        "head_branch": "main",
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-09-05T10:00:00Z",
                        "run_started_at": "2026-09-05T10:00:00Z",
                        "updated_at": "2026-09-05T10:02:00Z",
                    },
                ],
            },
        )
    )


def make(engine: sa.Engine, source: Path, tmp: Path) -> Extractor:
    settings = Settings(repos=[{"name": REPO}], since_days=3650, cache_dir=str(tmp / "cache"))  # type: ignore[list-item]
    gh = GitHub(client=httpx.AsyncClient(), sleep=_noop)
    return Extractor(settings, engine, gh, clone_url=str(source))


def count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as c:
        return int(c.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0)


@respx.mock
async def test_full_extract_then_transform(
    engine: sa.Engine, source_repo: Path, tmp_path: Path
) -> None:
    mock_api()
    [report] = await make(engine, source_repo, tmp_path).run()
    assert report.api_error is None
    assert report.counts == {
        "metadata": 1,
        "commit_authors": 1,
        "pulls": 1,
        "reviews": 1,
        "workflow_runs": 1,
        "commits": 5,
    }
    assert count(engine, "raw.commit_files") >= 5
    assert not respx.calls.last.request.url.path.endswith(
        "/pulls/1/reviews"
    )  # stopped at high-water

    warehouse.transform(engine)
    with engine.connect() as c:
        keys = {r[0] for r in c.execute(sa.text("SELECT author_key FROM mart.dim_author"))}
        bf = c.execute(
            sa.text("SELECT top_dir, bus_factor FROM mart.m_bus_factor ORDER BY top_dir")
        ).all()
    # asha@corp.com is linked to login "asha" by the commits API; Ravi's noreply email
    # resolves to the login without any API data.
    assert {"asha", "ravi-dev"} <= keys and "asha@corp.com" not in keys
    assert ("app", 1) in [tuple(r) for r in bf]


@respx.mock
async def test_incremental_rerun_is_idempotent(
    engine: sa.Engine, source_repo: Path, tmp_path: Path
) -> None:
    mock_api()
    ex = make(engine, source_repo, tmp_path)
    await ex.run()
    first = count(engine, "raw.commits")
    [again] = await ex.run()
    assert count(engine, "raw.commits") == first  # upserts: overlap is harmless
    assert again.counts["commits"] <= 1  # only the boundary commit is re-read
    with engine.connect() as c:
        streams = {r[0] for r in c.execute(sa.text("SELECT stream FROM raw.extract_state"))}
    assert streams == {"git", "commit_authors", "pulls", "runs"}


@respx.mock
async def test_api_unavailable_degrades_to_git_only(
    engine: sa.Engine, source_repo: Path, tmp_path: Path
) -> None:
    respx.get(f"{API}/repos/{REPO}").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )
    [report] = await make(engine, source_repo, tmp_path).run()
    assert report.api_error and "not accessible" in report.api_error
    assert report.counts["commits"] == 5
    with engine.connect() as c:
        payload = c.execute(sa.text("SELECT payload FROM raw.repos")).scalar()
    assert payload == {"default_branch": "main", "source": "git"}
    warehouse.transform(engine)
    from gitpulse import metrics

    assert not bool(metrics.summary(engine).iloc[0]["has_api_data"])


def test_settings_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gitpulse.config import load_settings

    cfg = tmp_path / "g.yml"
    cfg.write_text("repos: [a/b, {name: c/d, deploy_workflows: [Deploy]}]\n")
    monkeypatch.setenv("GITPULSE_DATABASE_URL", "postgresql+psycopg://x/y")
    s = load_settings(cfg)
    assert [r.name for r in s.repos] == ["a/b", "c/d"] and s.database_url.endswith("/y")
    cfg.write_text("repos: [not-a-repo]\n")
    with pytest.raises(ValueError, match="owner/name"):
        load_settings(cfg)
    cfg.write_text(json.dumps({"repos": []}))
    with pytest.raises(ValueError):
        load_settings(cfg)
