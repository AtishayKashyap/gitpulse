from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from gitpulse import warehouse

TEST_DB = os.environ.get(
    "GITPULSE_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/gitpulse_test",
)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    """A clean warehouse per test (schemas dropped and recreated)."""
    eng = warehouse.make_engine(TEST_DB)
    with eng.begin() as c:
        c.exec_driver_sql("DROP SCHEMA IF EXISTS raw CASCADE; DROP SCHEMA IF EXISTS mart CASCADE;")
    warehouse.init(eng)
    yield eng
    eng.dispose()


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", **(env or {})},
    ).stdout


def commit(
    repo: Path,
    files: dict[str, str | bytes],
    *,
    author: str,
    email: str,
    date: str,
    msg: str = "change",
) -> str:
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content)
    git(repo, "add", "-A")
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": date,
    }
    git(repo, "commit", "-q", "-m", msg, env=env)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A small real repo: two authors (one via a GitHub noreply address), a rename,
    a binary file, a lockfile and a merge commit."""
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "x@x")
    git(repo, "config", "user.name", "x")
    commit(
        repo,
        {"app/core.py": "a\n" * 10, "README.md": "hi\n"},
        author="Asha Rao",
        email="asha@corp.com",
        date="2026-09-01T10:00:00Z",
    )
    commit(
        repo,
        {"app/core.py": "a\n" * 14, "uv.lock": "x\n" * 500},
        author="Asha Rao",
        email="asha@corp.com",
        date="2026-09-02T10:00:00Z",
    )
    git(repo, "checkout", "-q", "-b", "feature")
    commit(
        repo,
        {"app/util.py": "u\n" * 5, "logo.png": bytes(range(256))},
        author="Ravi",
        email="4242+ravi-dev@users.noreply.github.com",
        date="2026-09-03T10:00:00Z",
    )
    git(repo, "checkout", "-q", "main")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "feature",
        "-m",
        "Merge feature",
        env={
            "GIT_AUTHOR_DATE": "2026-09-04T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-09-04T10:00:00Z",
        },
    )
    git(repo, "mv", "app/util.py", "app/helpers.py")
    commit(
        repo,
        {},
        author="Ravi",
        email="4242+ravi-dev@users.noreply.github.com",
        date="2026-09-05T10:00:00Z",
        msg="rename",
    )
    return repo
