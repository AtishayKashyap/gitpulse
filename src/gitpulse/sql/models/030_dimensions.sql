CREATE TABLE mart.dim_repo AS
SELECT
  repo,
  payload->>'default_branch' AS default_branch,
  payload->>'language' AS language,
  (payload->>'stargazers_count')::int AS stars,
  payload->>'description' AS description
FROM raw.repos;
ALTER TABLE mart.dim_repo ADD PRIMARY KEY (repo);

-- Every identity seen as committer, PR author or reviewer.
CREATE TABLE mart.dim_author AS
WITH seen AS (
  SELECT author_key, author_name AS name, authored_at AS at FROM mart.stg_commits
  UNION ALL SELECT author_key, author_login, created_at FROM mart.stg_pulls
  UNION ALL SELECT reviewer_key, reviewer_key, submitted_at FROM mart.stg_reviews
)
SELECT
  author_key,
  mode() WITHIN GROUP (ORDER BY name) AS display_name,  -- most common spelling wins
  mart.is_bot(author_key, max(name)) AS is_bot,
  min(at) AS first_seen,
  max(at) AS last_seen
FROM seen
WHERE author_key IS NOT NULL
GROUP BY author_key;
ALTER TABLE mart.dim_author ADD PRIMARY KEY (author_key);

-- Calendar spine so weeks with zero activity still appear in trends.
CREATE TABLE mart.dim_date AS
SELECT
  d::date AS date,
  date_trunc('week', d)::date AS week,
  extract(isodow FROM d)::int AS iso_dow,
  to_char(d, 'YYYY-MM') AS month
FROM generate_series(
  coalesce((SELECT min(authored_at)::date FROM mart.stg_commits), current_date - 90),
  current_date, interval '1 day') AS d;
ALTER TABLE mart.dim_date ADD PRIMARY KEY (date);
