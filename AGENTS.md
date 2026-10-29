# AGENTS.md

Instructions and guidelines for autonomous and assistant agents operating in this repository.

## Overview

This repository is the declarative governance config for the fullsend-ai
GitHub organization: peribolos owns people and access, safe-settings owns
repo settings and rulesets, `org/*.json` pins what neither tool covers.
See README.md for the full boundary.

## Repository Conventions

- **`peribolos.yaml` is total-state**: anything unlisted — members, teams,
  grants, collaborators — is REMOVED on apply. Never drop entries to
  "clean up"; removal is a deliberate governance change.
- **Access boundary**: never add `teams:` or `collaborators:` keys to
  `settings.yml`. Those safe-settings plugins delete unlisted entries and
  would fight peribolos, which owns all access.
- `settings.yml` holds org-wide defaults; `repos/<repo>.yml` holds
  per-repo ruleset overrides. Rulesets exceeding the org baseline get a
  repo file; baseline-only repos need none.
- The enterprise-managed team and `*-ghsa-*` repos are deliberately out
  of scope; do not add them to the config.

## Validating changes

- Run `python3 scripts/lint.py` (needs pyyaml) — it enforces the
  conventions above: the access boundary, min admins, closed team
  privacy, no GHSA repos, ruleset shape and API-required ruleset fields
  (NOP dry-runs never hit GitHub's payload validation). It also prints
  the planned mutations -- the config diff against the base branch
  (`--base <rev>` to override) -- and writes them to the job summary.
  CI runs it on every push and PR.
- YAML must parse; the lint covers that too.
- Changes to `peribolos.yaml` must be validated with a peribolos dry run
  from a `kubernetes-sigs/prow` checkout; a snapshot-faithful change plans
  **zero mutations** (the `org: write` grant 404s until this repo
  exists on GitHub — validate after pushing):

  ```
  go run ./cmd/peribolos --config-path=peribolos.yaml \
    --github-token-path=<read:org token file> --min-admins=2 \
    --require-self=false --ignore-enterprise-teams \
    --fix-org --fix-org-members --fix-teams --fix-team-members \
    --fix-team-repos --fix-collaborators
  ```

- safe-settings changes are validated by the PR dry-run job
  (`FULL_SYNC_NOP=true`); it must report no unexpected changes. That job
  reads the config from main, not the PR -- review lint's mutation plan
  for the PR's own changes.

## Enforcement

Apply jobs run on merge to main and on the daily schedule. The Actions
policy is pinned to the live permissive values (`allowed_actions: all`);
restricting it is a deliberate, reviewed change — never flip it to
`selected` without an inventory of the `uses:` actions across the org.

## Commits & Git

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `<type>(<scope>): <description>`.
- When an AI agent assists with a commit, add the trailer:
  `Assisted-By: <model>(<runtime>)`

## Agent Behavior

- **Be brief**: Keep comments, documentation, and explanations concise.
- **Surgical changes**: Modify only the files and lines necessary.
