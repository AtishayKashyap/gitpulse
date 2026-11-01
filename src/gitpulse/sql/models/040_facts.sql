CREATE TABLE mart.fact_file_change AS
SELECT
  c.repo, c.sha, f.path,
  CASE WHEN position('/' IN f.path) > 0 THEN split_part(f.path, '/', 1) ELSE '(root)' END AS top_dir,
  coalesce(f.additions, 0) AS additions,
  coalesce(f.deletions, 0) AS deletions,
  f.additions IS NULL AS is_binary,
  mart.is_generated(f.path) AS is_generated,
  c.author_key, c.authored_at
FROM raw.commit_files f
JOIN mart.stg_commits c USING (repo, sha)
WHERE NOT c.is_merge;  -- merges repeat their parents' changes
CREATE INDEX ON mart.fact_file_change (repo, authored_at);

CREATE TABLE mart.fact_commit AS
SELECT
  c.repo, c.sha, c.author_key, c.authored_at, c.is_merge,
  count(f.path)::int AS files_changed,
  coalesce(sum(f.additions), 0)::int AS additions,
  coalesce(sum(f.deletions), 0)::int AS deletions
FROM mart.stg_commits c
LEFT JOIN mart.fact_file_change f USING (repo, sha)
GROUP BY c.repo, c.sha, c.author_key, c.authored_at, c.is_merge;
ALTER TABLE mart.fact_commit ADD PRIMARY KEY (repo, sha);

CREATE TABLE mart.fact_review AS
SELECT r.repo, r.number, r.review_id, r.reviewer_key, r.state, r.submitted_at,
       r.reviewer_key = p.author_key AS is_self
FROM mart.stg_reviews r
JOIN mart.stg_pulls p USING (repo, number);

CREATE TABLE mart.fact_pr AS
SELECT
  p.repo, p.number, p.title, p.author_key, p.created_at, p.closed_at, p.merged_at, p.is_draft,
  mart.is_bot(p.author_key, p.author_login) AS is_bot,
  fr.first_review_at,
  coalesce(fr.reviews, 0) AS reviews,
  extract(epoch FROM p.merged_at - p.created_at) / 3600 AS cycle_hours,
  extract(epoch FROM fr.first_review_at - p.created_at) / 3600 AS hours_to_first_review
FROM mart.stg_pulls p
LEFT JOIN (
  -- Only reviews by someone other than the author count as review.
  SELECT repo, number, min(submitted_at) AS first_review_at, count(*)::int AS reviews
  FROM mart.fact_review WHERE NOT is_self GROUP BY repo, number
) fr USING (repo, number);
ALTER TABLE mart.fact_pr ADD PRIMARY KEY (repo, number);

CREATE TABLE mart.fact_ci_run AS
SELECT
  w.repo,
  w.run_id,
  w.payload->>'name' AS workflow,
  w.payload->>'event' AS event,
  w.payload->>'head_branch' AS branch,
  w.payload->>'conclusion' AS conclusion,
  (w.payload->>'run_started_at')::timestamptz AS started_at,
  (w.payload->>'updated_at')::timestamptz AS finished_at,
  extract(epoch FROM (w.payload->>'updated_at')::timestamptz - (w.payload->>'run_started_at')::timestamptz) AS duration_s,
  w.payload->>'head_branch' = r.default_branch AS on_default_branch,
  EXISTS (SELECT 1 FROM raw.deploy_workflows d WHERE d.repo = w.repo AND d.workflow = w.payload->>'name') AS is_deploy
FROM raw.workflow_runs w
LEFT JOIN mart.dim_repo r USING (repo)
WHERE w.payload->>'status' = 'completed';
