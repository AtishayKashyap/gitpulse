"""SQL models and metrics on a hand-built dataset where every answer is known."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
import sqlalchemy as sa

from gitpulse import metrics, warehouse

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
T0 = NOW - dt.timedelta(days=20)
R = "o/r"


def h(hours: float) -> str:
    return (T0 + dt.timedelta(hours=hours)).isoformat()


def load(engine: sa.Engine) -> None:
    commits: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    def add(
        sha: str, email: str, name: str, changes: dict[str, int], parents: int = 1, day: int = 0
    ) -> None:
        at = T0 + dt.timedelta(days=day)
        commits.append(
            {
                "repo": R,
                "sha": sha,
                "parents": [f"p{i}" for i in range(parents)],
                "author_name": name,
                "author_email": email,
                "authored_at": at,
                "committed_at": at,
                "subject": sha,
            }
        )
        files.extend(
            {"repo": R, "sha": sha, "path": p, "additions": n, "deletions": 0}
            for p, n in changes.items()
        )

    add("c1", "alice@corp.com", "Alice", {"src/core.py": 60, "uv.lock": 5000, "lib/a.py": 40})
    add(
        "c2", "alice@corp.com", "Alice A.", {"src/core.py": 20}, day=1
    )  # no API row: email->login mapping
    add(
        "c3",
        "77+bob@users.noreply.github.com",
        "Bob",
        {"src/core.py": 20, "docs/x.md": 10, "lib/b.py": 40},
        day=2,
    )
    add(
        "c4",
        "49699333+dependabot[bot]@users.noreply.github.com",
        "dependabot[bot]",
        {"src/core.py": 500},
        day=3,
    )
    add(
        "c5", "old@personal.com", "alice", {"src/core.py": 1}, day=4
    )  # merged into alice by override
    add("c6", "carol@corp.com", "Carol", {"lib/c.py": 20}, day=5)
    add(
        "m1", "alice@corp.com", "Alice", {"src/core.py": 999}, parents=2, day=6
    )  # merge: excluded from churn

    pulls = [
        {
            "number": 1,
            "user": {"login": "bob"},
            "created_at": h(0),
            "merged_at": h(10),
            "closed_at": h(10),
            "state": "closed",
            "title": "feat",
        },
        {
            "number": 2,
            "user": {"login": "alice"},
            "created_at": h(0),
            "merged_at": h(30),
            "closed_at": h(30),
            "state": "closed",
            "title": "fix",
        },
        {
            "number": 3,
            "user": {"login": "dependabot[bot]"},
            "created_at": h(0),
            "merged_at": h(1),
            "closed_at": h(1),
            "state": "closed",
            "title": "bump",
        },
        {
            "number": 4,
            "user": {"login": "carol"},
            "created_at": h(5),
            "merged_at": None,
            "closed_at": None,
            "state": "open",
            "title": "wip",
            "draft": True,
        },
    ]
    reviews = [
        {
            "pr": 1,
            "id": 11,
            "user": {"login": "bob"},
            "state": "COMMENTED",
            "submitted_at": h(1),
        },  # self: ignored
        {"pr": 1, "id": 12, "user": {"login": "Alice"}, "state": "APPROVED", "submitted_at": h(2)},
        {
            "pr": 2,
            "id": 13,
            "user": {"login": "carol"},
            "state": "PENDING",
            "submitted_at": None,
        },  # draft review: ignored
    ]
    runs = (
        [
            {
                "id": i,
                "name": "CI",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "success",
            }
            for i in (1, 2, 3)
        ]
        + [
            {
                "id": 4,
                "name": "CI",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        + [
            {
                "id": i,
                "name": "Deploy",
                "head_branch": "main",
                "status": "completed",
                "conclusion": "success",
            }
            for i in (5, 6)
        ]
        + [
            {
                "id": 7,
                "name": "CI",
                "head_branch": "feature",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        + [
            {
                "id": 8,
                "name": "CI",
                "head_branch": "main",
                "status": "in_progress",
                "conclusion": None,
            }
        ]
    )
    for r in runs:
        r.update(event="push", run_started_at=h(0), updated_at=h(0.5))

    with engine.begin() as c:
        warehouse.upsert(
            c,
            "raw.repos",
            [
                {
                    "repo": R,
                    "payload": json.dumps(
                        {"default_branch": "main", "language": "Python", "stargazers_count": 5}
                    ),
                }
            ],
            ["repo"],
        )
        warehouse.upsert(c, "raw.commits", commits, ["repo", "sha"])
        warehouse.upsert(c, "raw.commit_files", files, ["repo", "sha", "path"])
        warehouse.upsert(
            c,
            "raw.commit_authors",
            [{"repo": R, "sha": "c1", "author_login": "Alice", "author_email": "alice@corp.com"}],
            ["repo", "sha"],
        )
        warehouse.upsert(
            c,
            "raw.pulls",
            [
                {"repo": R, "number": p["number"], "payload": json.dumps(p), "updated_at": h(30)}
                for p in pulls
            ],
            ["repo", "number"],
        )
        warehouse.upsert(
            c,
            "raw.reviews",
            [
                {"repo": R, "pr_number": r["pr"], "review_id": r["id"], "payload": json.dumps(r)}
                for r in reviews
            ],
            ["repo", "review_id"],
        )
        warehouse.upsert(
            c,
            "raw.workflow_runs",
            [{"repo": R, "run_id": r["id"], "payload": json.dumps(r)} for r in runs],
            ["repo", "run_id"],
        )
    warehouse.transform(
        engine, identities={"old@personal.com": "alice"}, deploy_workflows={R: ["Deploy"]}
    )


@pytest.fixture
def loaded(engine: sa.Engine) -> sa.Engine:
    load(engine)
    return engine


def q(engine: sa.Engine, sql: str) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(sa.text(sql)).mappings())


def test_identity_resolution(loaded: sa.Engine) -> None:
    authors = {r["author_key"]: r for r in q(loaded, "SELECT * FROM mart.dim_author")}
    # alice@corp.com -> login via commits API; old@personal.com -> override; noreply -> login.
    # carol@corp.com (git) and carol (PR login) stay separate: nothing links them, and a
    # resolver must not guess. A `.mailmap`-style override would merge them.
    assert set(authors) == {"alice", "bob", "carol", "carol@corp.com", "dependabot[bot]"}
    assert authors["dependabot[bot]"]["is_bot"] and not authors["alice"]["is_bot"]
    assert authors["alice"]["display_name"] in {"Alice", "Alice A.", "alice"}
    c2 = q(loaded, "SELECT author_key FROM mart.fact_commit WHERE sha = 'c2'")[0]
    assert c2["author_key"] == "alice"  # no API row of its own: inherited via email


def test_facts_exclude_merges_and_flag_generated_files(loaded: sa.Engine) -> None:
    commit_rows = {r["sha"]: r for r in q(loaded, "SELECT * FROM mart.fact_commit")}
    assert commit_rows["m1"]["is_merge"] and commit_rows["m1"]["additions"] == 0
    assert commit_rows["c1"]["additions"] == 5100  # raw fact keeps everything...
    gen = q(loaded, "SELECT path FROM mart.fact_file_change WHERE is_generated")
    assert [g["path"] for g in gen] == ["uv.lock"]  # ...but lockfiles are flagged


def test_review_coverage_and_cycle_time(loaded: sa.Engine) -> None:
    cov = q(loaded, "SELECT * FROM mart.m_review_coverage")[0]
    # Human merged PRs: #1 (reviewed by alice) and #2 (no review). Bot PR #3 excluded.
    assert (cov["merged_prs"], cov["reviewed_prs"], float(cov["coverage"])) == (2, 1, 0.5)
    assert float(cov["cycle_p50_h"]) == 20.0  # median of 10h and 30h
    assert float(cov["first_review_p50_h"]) == 2.0  # bob's own comment at 1h doesn't count
    pr1 = q(loaded, "SELECT * FROM mart.fact_pr WHERE number = 1")[0]
    assert pr1["reviews"] == 1


def test_bus_factor_known_answers(loaded: sa.Engine) -> None:
    bf = {r["top_dir"]: r for r in q(loaded, "SELECT * FROM mart.m_bus_factor")}
    # src: alice 81 vs bob 20 (bot's 500 and the merge excluded) -> one person holds >50%.
    assert bf["src"]["bus_factor"] == 1 and bf["src"]["top_author"] == "alice"
    # lib: alice 40, bob 40, carol 20 -> two people needed to reach 50%.
    assert bf["lib"]["bus_factor"] == 2 and bf["lib"]["authors"] == 3
    assert "(root)" not in bf  # only uv.lock lives at the root: generated, excluded


def test_hotspots_and_delivery(loaded: sa.Engine) -> None:
    hot = q(loaded, "SELECT * FROM mart.m_hotspots ORDER BY rank")
    assert hot[0]["path"] == "src/core.py" and hot[0]["authors"] == 2  # alice + bob; bot excluded
    assert hot[0]["churn"] == 101
    assert "uv.lock" not in {h["path"] for h in hot}
    d = q(loaded, "SELECT * FROM mart.m_delivery")[0]
    # main branch, completed: 5 success + 1 failure; feature-branch and in-progress runs excluded.
    assert (d["deployments"], d["default_branch_runs"], d["failed_runs"]) == (2, 6, 1)
    assert float(d["failure_rate"]) == round(1 / 6, 3)


def test_weekly_activity_spine_and_contributors(loaded: sa.Engine) -> None:
    weeks = q(loaded, "SELECT * FROM mart.m_weekly_activity ORDER BY week")
    assert sum(w["commits"] for w in weeks) == 6  # non-merge commits
    assert any(w["commits"] == 0 for w in weeks)  # zero weeks present thanks to dim_date
    people = {r["author_key"]: r for r in q(loaded, "SELECT * FROM mart.m_contributors")}
    assert "dependabot[bot]" not in people
    assert people["alice"]["reviews_given"] == 1 and people["bob"]["prs"] == 1


def test_data_tests_pass_then_catch_corruption(loaded: sa.Engine) -> None:
    assert all(t.failures == 0 for t in warehouse.run_data_tests(loaded))
    with loaded.begin() as c:
        c.execute(
            sa.text(
                "UPDATE mart.fact_pr SET merged_at = created_at - interval '1 hour' WHERE number = 1"
            )
        )
    failed = {t.name for t in warehouse.run_data_tests(loaded) if t.failures}
    assert failed == {"fact_pr merged_at >= created_at"}


def test_transform_is_idempotent(loaded: sa.Engine) -> None:
    before = q(loaded, "SELECT count(*) AS n FROM mart.fact_commit")[0]["n"]
    timings = warehouse.transform(loaded, identities={"old@personal.com": "alice"})
    assert q(loaded, "SELECT count(*) AS n FROM mart.fact_commit")[0]["n"] == before
    assert set(timings) == {
        "010_reset.sql",
        "020_staging.sql",
        "030_dimensions.sql",
        "040_facts.sql",
        "010_metrics.sql",
    }


def test_metrics_reader_and_summary(loaded: sa.Engine) -> None:
    df = metrics.read(loaded, "bus_factor", repo=R, limit=1)
    assert len(df) == 1
    s = metrics.summary(loaded).iloc[0]
    assert (s["commits"], s["contributors"], s["merged_prs"], bool(s["has_api_data"])) == (
        6,
        3,
        3,
        True,
    )
    with pytest.raises(KeyError):
        metrics.read(loaded, "nope; DROP TABLE x")


def test_state_and_cache_roundtrip(engine: sa.Engine) -> None:
    with engine.begin() as c:
        assert warehouse.get_state(c, R, "git") is None
        warehouse.set_state(c, R, "git", "2026-01-01T00:00:00+00:00")
        warehouse.set_state(c, R, "git", "2026-02-01T00:00:00+00:00")
        assert warehouse.get_state(c, R, "git") == "2026-02-01T00:00:00+00:00"
        assert warehouse.upsert(c, "raw.repos", [], ["repo"]) == 0
    cache = warehouse.DbCache(engine)
    assert cache.get("u") is None
    cache.put("u", '"e1"', {"a": 1})
    cache.put("u", '"e2"', [1, 2])
    assert cache.get("u") == ('"e2"', [1, 2])
