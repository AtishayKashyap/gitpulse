# ADR 0002: git for churn, the API for collaboration data

**Status:** accepted

## Decision
- **Commits and per-file churn come from `git log --numstat -M` on a bare, single-branch clone limited by `--shallow-since`.** The API needs one request per commit for line stats: thousands of requests and minutes of rate-limit budget. Locally it's one command.
- **PRs, reviews, workflow runs, and the sha→login map come from the REST API.** Only GitHub has them.
- **Graceful degradation:** an API failure for a repo doesn't fail the run. Git-derived metrics are still produced, and the report marks PR metrics as unavailable ("—", not 0).

## Two bugs that shaped this
1. **Fetch refspec.** The first incremental fetch used `+HEAD:HEAD`, which didn't advance the branch, so new commits never arrived. An extraction test that commits upstream and re-syncs caught it.
2. **Fetching every branch.** Switching to `refs/heads/*` made pydantic's incremental run take **37 s instead of ~1 s**: the repo has 85 branches, and new branches arrive *without* the shallow cut-off, so their whole history downloads. Analysis only uses the default branch, so the clone is now `--single-branch` and fetches only that branch. Incremental runs are a stable ~2.3 s for four large repos.
