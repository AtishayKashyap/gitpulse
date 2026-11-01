"""Extraction: GitHub API + local git -> raw.* tables. Incremental and resumable.

Each (repo, stream) keeps a high-water mark in raw.extract_state, advanced only after
that stream's rows are committed. A crash mid-run just re-fetches the last window, and
upserts make the overlap harmless.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog

from . import gitlog, warehouse
from .config import RepoSpec, Settings
from .github import GitHub, GitHubError

log = structlog.get_logger(__name__)
REVIEW_CONCURRENCY = 8


@dataclass
class ExtractReport:
    repo: str
    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    api_error: str | None = None


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Extractor:
    def __init__(
        self,
        settings: Settings,
        engine: sa.Engine,
        gh: GitHub,
        *,
        clone_url: str = "https://github.com/{repo}.git",
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.gh = gh
        self.clone_url = clone_url

    def _since(self, repo: str, stream: str) -> dt.datetime:
        with self.engine.connect() as c:
            hw = warehouse.get_state(c, repo, stream)
        if hw:
            return dt.datetime.fromisoformat(hw)
        return dt.datetime.now(dt.UTC) - dt.timedelta(days=self.settings.since_days)

    def _save(
        self,
        repo: str,
        stream: str,
        table: str,
        rows: list[dict[str, Any]],
        key: list[str],
        hw: dt.datetime | None,
    ) -> int:
        with self.engine.begin() as c:  # rows + high-water mark commit together
            n = warehouse.upsert(c, table, rows, key)
            if hw is not None:
                warehouse.set_state(c, repo, stream, hw.isoformat())
        return n

    async def repo_meta(self, spec: RepoSpec) -> int:
        body = await self.gh.get(f"/repos/{spec.name}")
        return self._save(
            spec.name,
            "repo",
            "raw.repos",
            [{"repo": spec.name, "payload": json.dumps(body)}],
            ["repo"],
            None,
        )

    def git_history(self, spec: RepoSpec) -> int:
        since = self._since(spec.name, "git")
        path = Path(self.settings.cache_dir) / spec.name.replace("/", "__")
        gitlog.sync_clone(
            self.clone_url.format(repo=spec.name), path, (since - dt.timedelta(days=1)).date()
        )
        commits = gitlog.read_commits(path, since)
        rows = [
            {
                "repo": spec.name,
                "sha": c.sha,
                "parents": c.parents,
                "author_name": c.author_name,
                "author_email": c.author_email,
                "authored_at": c.authored_at,
                "committed_at": c.committed_at,
                "subject": c.subject[:500],
            }
            for c in commits
        ]
        files = [
            {
                "repo": spec.name,
                "sha": c.sha,
                "path": f.path,
                "additions": f.additions,
                "deletions": f.deletions,
            }
            for c in commits
            for f in c.files
        ]
        # Dedupe rename collisions within one commit (same normalized path twice).
        files = list({(r["sha"], r["path"]): r for r in files}.values())
        hw = max((c.committed_at for c in commits), default=None)
        with self.engine.begin() as conn:
            warehouse.upsert(conn, "raw.commits", rows, ["repo", "sha"])
            for i in range(0, len(files), 5000):  # bounded statement size
                warehouse.upsert(
                    conn, "raw.commit_files", files[i : i + 5000], ["repo", "sha", "path"]
                )
            if hw:
                warehouse.set_state(conn, spec.name, "git", hw.isoformat())
        return len(rows)

    async def commit_authors(self, spec: RepoSpec) -> int:
        since = self._since(spec.name, "commit_authors")
        rows, newest = [], since
        async for c in self.gh.paginate(f"/repos/{spec.name}/commits", {"since": _iso(since)}):
            rows.append(
                {
                    "repo": spec.name,
                    "sha": c["sha"],
                    "author_login": (c.get("author") or {}).get("login"),
                    "author_email": ((c.get("commit") or {}).get("author") or {}).get("email"),
                }
            )
            committed = dt.datetime.fromisoformat(
                c["commit"]["committer"]["date"].replace("Z", "+00:00")
            )
            newest = max(newest, committed)
        return self._save(
            spec.name, "commit_authors", "raw.commit_authors", rows, ["repo", "sha"], newest
        )

    async def pulls_and_reviews(self, spec: RepoSpec) -> tuple[int, int]:
        """PRs sorted by updated desc: stop paginating at the high-water mark, then fetch
        reviews only for PRs that changed (bounded concurrency)."""
        since = self._since(spec.name, "pulls")
        changed: list[dict[str, Any]] = []
        async for pr in self.gh.paginate(
            f"/repos/{spec.name}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}
        ):
            if dt.datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00")) < since:
                break
            changed.append(pr)
        sem = asyncio.Semaphore(REVIEW_CONCURRENCY)

        async def reviews_of(n: int) -> list[dict[str, Any]]:
            async with sem:
                return [r async for r in self.gh.paginate(f"/repos/{spec.name}/pulls/{n}/reviews")]

        review_lists = await asyncio.gather(*(reviews_of(pr["number"]) for pr in changed))
        review_rows = [
            {
                "repo": spec.name,
                "pr_number": pr["number"],
                "review_id": r["id"],
                "payload": json.dumps(r),
            }
            for pr, rs in zip(changed, review_lists, strict=True)
            for r in rs
        ]
        pr_rows = [
            {
                "repo": spec.name,
                "number": pr["number"],
                "payload": json.dumps(pr),
                "updated_at": pr["updated_at"],
            }
            for pr in changed
        ]
        newest = max(
            (dt.datetime.fromisoformat(p["updated_at"].replace("Z", "+00:00")) for p in changed),
            default=None,
        )
        with self.engine.begin() as c:
            warehouse.upsert(c, "raw.pulls", pr_rows, ["repo", "number"])
            warehouse.upsert(c, "raw.reviews", review_rows, ["repo", "review_id"])
            if newest:
                warehouse.set_state(c, spec.name, "pulls", newest.isoformat())
        return len(pr_rows), len(review_rows)

    async def workflow_runs(self, spec: RepoSpec) -> int:
        since = self._since(spec.name, "runs")
        rows, newest = [], None
        async for run in self.gh.paginate(
            f"/repos/{spec.name}/actions/runs", {"created": f">={since.date().isoformat()}"}
        ):
            rows.append({"repo": spec.name, "run_id": run["id"], "payload": json.dumps(run)})
            created = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            newest = created if newest is None else max(newest, created)
        # Runs still in progress get re-fetched next time: keep the mark a day back.
        hw = newest - dt.timedelta(days=1) if newest else None
        return self._save(spec.name, "runs", "raw.workflow_runs", rows, ["repo", "run_id"], hw)

    async def repo(self, spec: RepoSpec) -> ExtractReport:
        """git history is mandatory; API streams degrade gracefully. If the API is
        unavailable (403/404, exhausted budget, private repo without a token), the
        git-derived metrics (activity, churn, hotspots, bus factor) are still produced."""
        t0 = time.perf_counter()
        report = ExtractReport(spec.name)
        # git (subprocess) runs in a thread so the API streams proceed concurrently.
        git_task = asyncio.create_task(asyncio.to_thread(self.git_history, spec))
        try:
            report.counts["metadata"] = await self.repo_meta(spec)
            authors, (prs, reviews), runs = await asyncio.gather(
                self.commit_authors(spec), self.pulls_and_reviews(spec), self.workflow_runs(spec)
            )
            report.counts.update(
                commit_authors=authors, pulls=prs, reviews=reviews, workflow_runs=runs
            )
        except GitHubError as exc:
            report.api_error = str(exc)
            log.warning(
                "extract.api_unavailable", repo=spec.name, status=exc.status, error=str(exc)[:200]
            )
        report.counts["commits"] = await git_task
        if "metadata" not in report.counts:  # minimal metadata from the clone itself
            path = Path(self.settings.cache_dir) / spec.name.replace("/", "__")
            payload = {"default_branch": gitlog.default_branch(path), "source": "git"}
            report.counts["metadata"] = self._save(
                spec.name,
                "repo",
                "raw.repos",
                [{"repo": spec.name, "payload": json.dumps(payload)}],
                ["repo"],
                None,
            )
        report.seconds = round(time.perf_counter() - t0, 2)
        log.info("extract.repo", repo=spec.name, seconds=report.seconds, **report.counts)
        return report

    async def run(self) -> list[ExtractReport]:
        return list(await asyncio.gather(*(self.repo(r) for r in self.settings.repos)))
