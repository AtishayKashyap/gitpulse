from __future__ import annotations

import datetime as dt
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from gitpulse.gitlog import (
    FORMAT,
    RS,
    US,
    default_branch,
    normalize_path,
    parse_log,
    read_commits,
    sync_clone,
)

from .conftest import commit, git

EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def test_normalize_rename_notations() -> None:
    assert normalize_path("app/{util.py => helpers.py}") == "app/helpers.py"
    assert normalize_path("{old => new}/mod.py") == "new/mod.py"
    assert normalize_path("src/{ => sub}/f.py") == "src/sub/f.py"
    assert normalize_path("a.py => b.py") == "b.py"
    assert normalize_path("plain/path.py") == "plain/path.py"


segment = st.text(alphabet="abcdefghij_-.", min_size=1, max_size=8).filter(
    lambda s: s not in {".", ".."}
)


@given(st.lists(segment, min_size=1, max_size=4), segment, segment)
def test_brace_rename_resolves_to_new_path(dirs: list[str], old: str, new: str) -> None:
    prefix = "/".join(dirs)
    assert normalize_path(f"{prefix}/{{{old} => {new}}}") == f"{prefix}/{new}"


def test_parse_log_handles_binary_and_merges() -> None:
    raw = (
        f"{RS}abc{US}p1 p2{US}A{US}a@x{US}2026-01-02T03:04:05+00:00{US}2026-01-02T03:04:05+00:00{US}Merge\n"
        f"{RS}def{US}p1{US}B{US}b@x{US}2026-01-01T00:00:00+05:30{US}2026-01-01T00:00:00+05:30{US}feat: x\n\n"
        "3\t1\tsrc/a.py\n-\t-\tlogo.png\n"
    )
    merge, c = list(parse_log(raw))
    assert merge.is_merge and merge.files == []
    assert not c.is_merge and c.authored_at.utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert [(f.path, f.additions, f.deletions) for f in c.files] == [
        ("src/a.py", 3, 1),
        ("logo.png", None, None),
    ]
    assert FORMAT.startswith(RS)


def test_clone_read_and_incremental_fetch(source_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "cache" / "o__r"
    sync_clone(str(source_repo), dest, dt.date(2026, 8, 1))
    assert default_branch(dest) == "main"
    commits = read_commits(dest, EPOCH)
    by_subject = {c.subject: c for c in commits}
    assert len(commits) == 5
    assert by_subject["Merge feature"].is_merge
    assert {f.path for f in by_subject["rename"].files} == {
        "app/helpers.py"
    }  # rename detected (-M)
    png = next(f for c in commits for f in c.files if f.path == "logo.png")
    assert png.additions is None
    # New upstream commit -> a re-sync fetches it.
    commit(
        source_repo,
        {"new.py": "n\n"},
        author="Asha Rao",
        email="asha@corp.com",
        date="2026-09-06T10:00:00Z",
        msg="later",
    )
    sync_clone(str(source_repo), dest, dt.date(2026, 8, 1))
    later = read_commits(dest, dt.datetime(2026, 9, 6, tzinfo=dt.UTC))
    assert [c.subject for c in later] == ["later"]


def test_default_branch_fallback(tmp_path: Path) -> None:
    assert default_branch(tmp_path) == "main"
    git(tmp_path, "init", "-q", "--bare", "-b", "trunk")
    assert default_branch(tmp_path) == "trunk"
