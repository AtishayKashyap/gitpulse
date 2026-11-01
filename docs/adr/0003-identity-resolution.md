# ADR 0003: Identity resolution rules

**Status:** accepted

People commit with several emails, GitHub hides some behind noreply addresses, and PRs only carry logins. Without resolution, one person shows up as three contributors, and bus factor and leaderboards are wrong.

## Precedence (`mart.identity_key`)
1. An explicit override in `gitpulse.yml` (`identities:`), like git's `.mailmap`.
2. The GitHub login attached to the commit by the commits API.
3. A login recovered from `12345+login@users.noreply.github.com`.
4. Otherwise, the lower-cased email.

Additionally, any email that was linked to a login *once* inherits that login for every commit, so older commits from the same address merge into the same person.

## Deliberately not done
No fuzzy matching on names ("A. Rao" vs "Asha Rao"). A false merge silently corrupts every per-person metric, while a missed merge is visible and fixable with one override line. A test pins this: `carol@corp.com` (git) and `carol` (PR login) stay separate until an override links them.

Bots are flagged (`[bot]` suffix, dependabot, renovate, github-actions) and excluded from human metrics, but kept in raw data and in CI metrics.
