# gitpulse: interview walkthrough

## 30-second pitch
> "gitpulse is an ETL pipeline for engineering analytics. It pulls git history and GitHub data into a Postgres star schema and computes metrics like PR cycle time, review coverage, hotspots and bus factor in SQL. A weekly report goes to GitHub Pages. What I'm proudest of is the data engineering: incremental extraction with ETag caching and rate-limit handling, identity resolution across emails and logins, and cleaning that excludes bots and lockfiles. And it's validated in CI by sentinel-dq, my other project."

## Resume bullets (measured)
- Built **gitpulse**, a Python ETL (async httpx, git, SQLAlchemy, PostgreSQL) that turns git and GitHub history into a star schema and SQL metrics (cycle time, review coverage, hotspots, bus factor, change-failure rate), with a weekly matplotlib report auto-published to GitHub Pages.
- Extracts **8.7k commits / 42k file changes from 4 large repos in 15 s**, with incremental re-runs in **~2.3 s**. It uses ETag conditional requests (304s don't consume rate limit), rate-limit-aware retries, and high-water marks committed atomically with the data.
- Profiled a 16× slowdown in incremental runs (37 s → 2.3 s), caused by fetching 85 unbounded branches, and fixed it with single-branch shallow fetches.
- Wrote analytics in SQL with window functions (running `SUM() OVER` for bus factor, moving averages, `percentile_cont`), verified by **31 tests (94% coverage)** on a hand-built dataset with known answers. CI gates the warehouse with my own data-quality engine (sentinel-dq).

## Questions you'll get

**Why not just use the GitHub API for everything?**
Line stats need one request per commit, so thousands of requests. One shallow clone plus `git log --numstat` gets all of it locally in seconds. The API handles what only it has: PRs, reviews, CI runs and logins.

**How is extraction incremental and safe to re-run?**
Each repo and stream has a high-water mark saved *in the same transaction* as the rows. Every write is an upsert on the natural key, so re-fetching the boundary window is harmless. PRs are sorted by `updated desc` and pagination stops at the mark.

**How does bus factor work in SQL?**
Per directory, sum each author's churn over the last year and sort authors by contribution. A running `SUM() OVER (... ROWS UNBOUNDED PRECEDING)` gives the cumulative share. Bus factor is the first row number where the cumulative share reaches 50%. Known-answer test: 40/40/20 gives 2, and 81/20 gives 1.

**Why medians for cycle time?**
Distributions are long-tailed. One PR left open for three months drags the mean far away from anyone's real experience; p50 and p90 describe it honestly.

**What data cleaning did you need?**
Bots, merge commits (they duplicate their parents' diffs), binaries, and generated files. `uv.lock` was the #1 "hotspot" in Typer until I excluded lockfiles. Identity resolution merges emails and noreply addresses into logins without fuzzy guessing, because a false merge silently corrupts every per-person metric.

**What's the change-failure rate here, honestly?**
A proxy: failed / completed CI runs on the default branch. Real CFR needs incident data. The README says so.

**How do you know the numbers are right?**
A hand-built dataset where I computed every answer by hand (coverage 50% after excluding a self-review and a bot PR, median cycle time 20 h, CFR 1/6), plus 7 data tests after every transform and the sentinel-dq gate in CI.

**What would you add next?**
Deployment-linked lead time (commit → deploy), team-level rollups via CODEOWNERS, and incremental model materialisation once the data outgrows full rebuilds.
