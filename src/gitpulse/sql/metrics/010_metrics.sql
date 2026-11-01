-- Metric views: the analytical layer the report and API read. Views (not tables) so
-- they always reflect the latest models; all heavy lifting is set-based SQL.

-- Weekly activity with a 4-week moving average. The calendar spine (dim_date) keeps
-- zero-activity weeks in the series instead of silently skipping them.
CREATE VIEW mart.m_weekly_activity AS
WITH weeks AS (SELECT DISTINCT week FROM mart.dim_date),
grid AS (SELECT r.repo, w.week FROM mart.dim_repo r CROSS JOIN weeks w),
commits AS (
  SELECT repo, date_trunc('week', authored_at)::date AS week, count(*) AS commits,
         count(DISTINCT author_key) AS authors, sum(additions + deletions) AS churn
  FROM mart.fact_commit WHERE NOT is_merge GROUP BY 1, 2
),
merged AS (
  SELECT repo, date_trunc('week', merged_at)::date AS week, count(*) AS merged_prs
  FROM mart.fact_pr WHERE merged_at IS NOT NULL GROUP BY 1, 2
)
SELECT
  g.repo, g.week,
  coalesce(c.commits, 0) AS commits,
  coalesce(c.authors, 0) AS active_authors,
  coalesce(c.churn, 0) AS churn,
  coalesce(m.merged_prs, 0) AS merged_prs,
  round(avg(coalesce(c.commits, 0)) OVER w, 2) AS commits_ma4,
  round(avg(coalesce(m.merged_prs, 0)) OVER w, 2) AS merged_prs_ma4
FROM grid g
LEFT JOIN commits c USING (repo, week)
LEFT JOIN merged m USING (repo, week)
WINDOW w AS (PARTITION BY g.repo ORDER BY g.week ROWS BETWEEN 3 PRECEDING AND CURRENT ROW);

-- PR cycle time and review latency, per repo per merge week. Medians/p90 rather than
-- means: one PR left open for months would dominate an average.
CREATE VIEW mart.m_pr_cycle AS
SELECT
  repo,
  date_trunc('week', merged_at)::date AS week,
  count(*) AS merged_prs,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_hours)::numeric, 2) AS cycle_p50_h,
  round(percentile_cont(0.9) WITHIN GROUP (ORDER BY cycle_hours)::numeric, 2) AS cycle_p90_h,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY hours_to_first_review)::numeric, 2) AS first_review_p50_h
FROM mart.fact_pr
WHERE merged_at IS NOT NULL AND NOT is_bot
GROUP BY 1, 2;

-- Review culture: share of merged human PRs reviewed by someone other than the author.
CREATE VIEW mart.m_review_coverage AS
SELECT
  repo,
  count(*) FILTER (WHERE merged_at IS NOT NULL) AS merged_prs,
  count(*) FILTER (WHERE merged_at IS NOT NULL AND reviews > 0) AS reviewed_prs,
  round(
    (count(*) FILTER (WHERE merged_at IS NOT NULL AND reviews > 0))::numeric
    / nullif(count(*) FILTER (WHERE merged_at IS NOT NULL), 0), 3) AS coverage,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_hours)::numeric, 2) AS cycle_p50_h,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY hours_to_first_review)::numeric, 2) AS first_review_p50_h
FROM mart.fact_pr
WHERE NOT is_bot
GROUP BY repo;

-- Hotspots: files that change a lot AND are touched by many people in the last 90
-- days. These are where defects and merge conflicts concentrate.
CREATE VIEW mart.m_hotspots AS
WITH recent AS (
  SELECT f.repo, f.path, count(DISTINCT f.sha) AS commits, count(DISTINCT f.author_key) AS authors,
         sum(f.additions + f.deletions) AS churn
  FROM mart.fact_file_change f
  JOIN mart.dim_author a USING (author_key)
  WHERE f.authored_at >= now() - interval '90 days' AND NOT f.is_binary AND NOT f.is_generated AND NOT a.is_bot
  GROUP BY f.repo, f.path
)
SELECT *, churn * authors AS score,
       row_number() OVER (PARTITION BY repo ORDER BY churn * authors DESC, path) AS rank
FROM recent;

-- Bus factor per top-level directory: the fewest people who together authored at least
-- half of the last year's churn. A running SUM() OVER the authors sorted by
-- contribution finds the smallest prefix that crosses 50%.
CREATE VIEW mart.m_bus_factor AS
WITH per_author AS (
  SELECT f.repo, f.top_dir, f.author_key, sum(f.additions + f.deletions) AS churn
  FROM mart.fact_file_change f
  JOIN mart.dim_author a USING (author_key)
  WHERE f.authored_at >= now() - interval '365 days' AND NOT a.is_bot AND NOT f.is_binary AND NOT f.is_generated
  GROUP BY 1, 2, 3
  HAVING sum(f.additions + f.deletions) > 0
),
ranked AS (
  SELECT *,
    row_number() OVER (PARTITION BY repo, top_dir ORDER BY churn DESC, author_key) AS n,
    sum(churn) OVER (PARTITION BY repo, top_dir ORDER BY churn DESC, author_key
                     ROWS UNBOUNDED PRECEDING) AS running,
    sum(churn) OVER (PARTITION BY repo, top_dir) AS total
  FROM per_author
)
SELECT
  repo, top_dir,
  min(n) FILTER (WHERE running >= total * 0.5) AS bus_factor,
  count(*) AS authors,
  max(total) AS churn,
  round(max(churn)::numeric / max(total), 3) AS top_author_share,
  (array_agg(author_key ORDER BY n))[1] AS top_author
FROM ranked
GROUP BY repo, top_dir;

-- DORA-style delivery metrics from CI, per week. Deployment = successful run of a
-- configured deploy workflow on the default branch. Change-failure rate = failed /
-- completed runs on the default branch (a pragmatic proxy without incident data).
CREATE VIEW mart.m_delivery AS
SELECT
  repo,
  date_trunc('week', started_at)::date AS week,
  count(*) FILTER (WHERE is_deploy AND conclusion = 'success') AS deployments,
  count(*) AS default_branch_runs,
  count(*) FILTER (WHERE conclusion = 'failure') AS failed_runs,
  round((count(*) FILTER (WHERE conclusion = 'failure'))::numeric / nullif(count(*), 0), 3) AS failure_rate,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)::numeric / 60, 2) AS duration_p50_min
FROM mart.fact_ci_run
WHERE on_default_branch AND conclusion IN ('success', 'failure')
GROUP BY 1, 2;

-- Contributor leaderboard (humans only), combining commits, PRs and reviews given.
CREATE VIEW mart.m_contributors AS
WITH c AS (SELECT repo, author_key, count(*) AS commits, sum(additions + deletions) AS churn
           FROM mart.fact_commit WHERE NOT is_merge GROUP BY 1, 2),
p AS (SELECT repo, author_key, count(*) AS prs FROM mart.fact_pr GROUP BY 1, 2),
r AS (SELECT repo, reviewer_key AS author_key, count(*) AS reviews_given
      FROM mart.fact_review WHERE NOT is_self GROUP BY 1, 2)
SELECT repo, author_key, a.display_name,
       coalesce(c.commits, 0) AS commits, coalesce(c.churn, 0) AS churn,
       coalesce(p.prs, 0) AS prs, coalesce(r.reviews_given, 0) AS reviews_given
FROM c FULL JOIN p USING (repo, author_key) FULL JOIN r USING (repo, author_key)
JOIN mart.dim_author a USING (author_key)
WHERE NOT a.is_bot;

-- When does work happen? Commits by ISO weekday x hour (UTC).
CREATE VIEW mart.m_activity_heatmap AS
SELECT repo, extract(isodow FROM authored_at)::int AS iso_dow, extract(hour FROM authored_at)::int AS hour,
       count(*) AS commits
FROM mart.fact_commit
WHERE NOT is_merge
GROUP BY 1, 2, 3;
