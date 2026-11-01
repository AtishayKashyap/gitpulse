-- Rebuilt from raw on every transform (inside one transaction), so models are always a
-- pure function of the raw data: no drift, no partial updates.
DROP SCHEMA IF EXISTS mart CASCADE;
CREATE SCHEMA mart;

-- Canonical identity for a person across git emails and GitHub logins.
-- Precedence: explicit override (login or email) > GitHub login > login recovered from a
-- noreply address (12345+octocat@users.noreply.github.com) > lower-cased email.
CREATE FUNCTION mart.identity_key(p_email text, p_login text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT canonical FROM raw.identity_overrides WHERE alias = lower(p_login)),
    (SELECT canonical FROM raw.identity_overrides WHERE alias = lower(p_email)),
    lower(p_login),
    lower(substring(p_email FROM '^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$')),
    lower(p_email)
  )
$$;

CREATE FUNCTION mart.is_bot(p_key text, p_name text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT p_key LIKE '%[bot]%' OR p_name LIKE '%[bot]' OR p_key IN ('dependabot', 'github-actions', 'renovate')
$$;

-- Generated / vendored files: lockfiles, minified bundles, snapshots. Their churn is
-- machine-made and would otherwise dominate hotspots and distort bus factor.
CREATE FUNCTION mart.is_generated(p_path text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT p_path ~* '(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock)$'
      OR p_path ~* '\.(min\.js|min\.css|map|snap|svg|ipynb)$'
      OR p_path ~* '(^|/)(vendor|node_modules|dist|__snapshots__)/'
$$;
