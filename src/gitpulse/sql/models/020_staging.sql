-- Any email that was ever linked to a GitHub login (via the commits API) inherits that
-- login, so a person's older commits from the same address merge into one identity.
CREATE TABLE mart.email_login AS
SELECT lower(author_email) AS email, (array_agg(lower(author_login) ORDER BY sha))[1] AS login
FROM raw.commit_authors
WHERE author_login IS NOT NULL AND author_email IS NOT NULL
GROUP BY 1;

CREATE TABLE mart.stg_commits AS
SELECT
  c.repo, c.sha, c.author_name, lower(c.author_email) AS author_email,
  mart.identity_key(c.author_email, coalesce(ca.author_login, el.login)) AS author_key,
  c.authored_at, c.committed_at, cardinality(c.parents) > 1 AS is_merge, c.subject
FROM raw.commits c
LEFT JOIN raw.commit_authors ca USING (repo, sha)
LEFT JOIN mart.email_login el ON el.email = lower(c.author_email);

CREATE TABLE mart.stg_pulls AS
SELECT
  repo,
  number,
  payload->>'title' AS title,
  mart.identity_key(NULL, payload->'user'->>'login') AS author_key,
  payload->'user'->>'login' AS author_login,
  (payload->>'created_at')::timestamptz AS created_at,
  (payload->>'closed_at')::timestamptz AS closed_at,
  (payload->>'merged_at')::timestamptz AS merged_at,
  coalesce((payload->>'draft')::boolean, false) AS is_draft,
  payload->>'state' AS state
FROM raw.pulls;

CREATE TABLE mart.stg_reviews AS
SELECT
  repo,
  pr_number AS number,
  review_id,
  mart.identity_key(NULL, payload->'user'->>'login') AS reviewer_key,
  payload->>'state' AS state,
  (payload->>'submitted_at')::timestamptz AS submitted_at
FROM raw.reviews
WHERE payload->>'state' <> 'PENDING' AND payload->>'submitted_at' IS NOT NULL;
