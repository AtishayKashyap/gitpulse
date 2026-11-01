"""Commit and file-churn extraction from a local clone via `git log --numstat`.

Why a clone instead of the API? Line stats need one API call per commit (thousands of
requests, minutes of rate-limit budget). One shallow clone + `git log` gets every
commit's per-file churn in a single local command.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

RS, US = "\x1e", "\x1f"  # record / unit separators: can't appear in commit metadata
FORMAT = f"{RS}%H{US}%P{US}%an{US}%ae{US}%aI{US}%cI{US}%s"
# "src/{old => new}/f.py" or "old.py => new.py"
BRACE_RENAME = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


@dataclass
class FileChange:
    path: str
    additions: int | None  # None for binary files
    deletions: int | None


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    authored_at: dt.datetime
    committed_at: dt.datetime
    subject: str
    files: list[FileChange] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def normalize_path(raw: str) -> str:
    """Resolve numstat rename notation to the new path."""
    if "{" in raw and " => " in raw:
        return re.sub(r"/{2,}", "/", BRACE_RENAME.sub(lambda m: m.group(2), raw)).strip("/")
    if " => " in raw:
        return raw.split(" => ", 1)[1]
    return raw


def parse_log(output: str) -> Iterator[Commit]:
    for record in output.split(RS):
        record = record.strip("\n")
        if not record:
            continue
        header, _, body = record.partition("\n")
        sha, parents, name, email, authored, committed, subject = header.split(US)
        commit = Commit(
            sha=sha,
            parents=parents.split() if parents else [],
            author_name=name,
            author_email=email,
            authored_at=dt.datetime.fromisoformat(authored),
            committed_at=dt.datetime.fromisoformat(committed),
            subject=subject,
        )
        for line in body.splitlines():
            if not line.strip():
                continue
            adds, dels, path = line.split("\t", 2)
            commit.files.append(
                FileChange(
                    path=normalize_path(path),
                    additions=None if adds == "-" else int(adds),
                    deletions=None if dels == "-" else int(dels),
                )
            )
        yield commit


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv; git resolved from PATH on purpose
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def sync_clone(url: str, dest: Path, since: dt.date) -> Path:
    """Bare, single-branch, shallow-since clone of the default branch; later runs fetch.

    Single-branch matters: fetching `refs/heads/*` on a repo with dozens of branches pulls
    every *new* branch with its full history (no shallow cut-off applies to it), which
    turned a 1 s incremental run into 37 s on pydantic (85 branches).
    """
    if (dest / "HEAD").exists():
        branch = default_branch(dest)
        # Already shallow: a plain fetch transfers only new commits. Re-applying
        # --shallow-since here fails when the window selects nothing new.
        _git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/heads/{branch}", cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(
            "clone",
            "--quiet",
            "--bare",
            "--single-branch",
            f"--shallow-since={since.isoformat()}",
            url,
            str(dest),
        )
    return dest


def read_commits(repo: Path, since: dt.datetime, rev: str = "HEAD") -> list[Commit]:
    out = _git(
        "log",
        rev,
        f"--since={since.isoformat()}",
        "--numstat",
        "-M",
        f"--format={FORMAT}",
        cwd=repo,
    )
    return list(parse_log(out))


def default_branch(repo: Path) -> str:
    """Default branch of a bare clone (HEAD's symbolic ref)."""
    try:
        return _git("symbolic-ref", "--short", "HEAD", cwd=repo).strip() or "main"
    except subprocess.CalledProcessError:
        return "main"
