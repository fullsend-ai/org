# fullsend-ai/org

Declarative governance for the fullsend-ai organization: the org converges
to what is committed here. The config was snapshotted from the live org
and validated with zero planned mutations.

| Concern | Owner | Config |
|---|---|---|
| Members, admins, teams, grants, collaborators, fleet inventory | peribolos | `peribolos.yaml` |
| Member privileges, Actions allow policy (covered by neither tool) | `gh api` steps | `org/*.json` |
| Secret scanning + push protection on public repos (covered by neither tool) | `scripts/secret-scanning.sh` | `org/secret-scanning.json` |
| Repo settings, org-level rulesets | safe-settings | `settings.yml`, `repos/<repo>.yml` |
| Daily compliance verdicts | policy checks | `.github/workflows/policy.yml` |
| Fleet inference gateway (OpenAI via GitHub OIDC), repo allowlist | `inference-gateway/deploy.sh` (manual) | `inference-gateway/` |

Peribolos and safe-settings converge by deleting unlisted entries — the
guarantee wanted for access. OpenTofu was rejected for this stack: it only
corrects drift on resources it tracks, so rogue additions stay invisible.

This repo is public deliberately: it is the org's roster (GitHub lists team
rosters only to org members). Private repos appear by name and access list
— accepted exposure; source code stays protected by repository visibility.
GHSA advisory repos (`*-ghsa-*`) are not tracked.

Planned evolution: org-membership and team-roster management is being
contributed to safe-settings upstream
([#690](https://github.com/github-community-projects/safe-settings/issues/690));
when it lands, peribolos can be removed.

## Tool limitations worked around

- Peribolos only pins `members_can_create_repositories`; the other
  privilege flags are applied from `org/member-privileges.json` via
  `gh api` (not tracked upstream).
- Safe-settings has no Actions-policy support; the allow policy is
  pinned from `org/actions-*.json` via `gh api`
  ([#656](https://github.com/github-community-projects/safe-settings/issues/656)).
- Safe-settings has no secret-scanning support
  ([#583](https://github.com/github-community-projects/safe-settings/issues/583)), and the
  enterprise-enforced security configuration cannot be attached with an App token (it needs
  `admin:enterprise`). `scripts/secret-scanning.sh` sets each public repo's
  `security_and_analysis` to `org/secret-scanning.json` instead (dry run on PRs from main's
  script, applied by the peribolos workflow's apply job; keys not in the file are left alone).
  Repos already attached to the enterprise-enforced configuration are left to it; private and internal repos are reported only (their enterprise
  configuration governs them, and they need Secret Protection).
- Safe-settings runs in full-sync mode from Actions
  ([docs](https://github.com/github-community-projects/safe-settings/blob/main-enterprise/docs/github-action.md)),
  config at the repo root (`ADMIN_REPO=org`, `CONFIG_PATH=.`).
- Peribolos runs from a pinned `kubernetes-sigs/prow` checkout; the
  `gcr.io/k8s-prow` images are stale since the Aug 2024 repo move.

## GitHub Apps

- **App definitions** (registration, permissions, webhooks): not
  manageable as code — GitHub exposes no API for App registrations.
- **App installations** (repo access per App): rejected — mutating them
  needs an enterprise-level credential (safe-settings ADR 0001).
- **App grants in rulesets**: bypass actors are pinned in `repos/*.yml`.

## Role request notifications

The Slack notifier recognizes role-request labels and the issue forms' title prefixes,
including requests submitted without labels. Opening, editing, or applying a role-request
label checks the current issue. Successful sends add `role-request-slack-notified` so later
events skip delivery. This label records notification only; it grants no access.

To backfill a missed request after deployment, run **Notify Slack on role request** from
Actions with its `issue_number` (for example, `34`). Tests run with
`python3 -m unittest discover -s tests` and mock all external writes.
If Slack succeeds but recording the marker fails, check the channel before retrying:
Slack delivery and the GitHub marker cannot be updated atomically.

## Setup

### Governance App permissions

Minimal set, verified against the endpoints each workflow calls
(safe-settings' [full table](https://github.com/github-community-projects/safe-settings/blob/main-enterprise/docs/deploy.md#permissions--events)
is only needed for more plugins, e.g. labels):

| Scope | Permission | Level | Used for |
|---|---|---|---|
| Repository | Administration | Read & Write | repo settings, rulesets, collaborators + invitations, vulnerability alerts |
| Repository | Contents | Read-only | config reads, policy file checks |
| Repository | Metadata | Read-only | mandatory |
| Organization | Administration | Read & Write | org settings, member-privileges PATCH, Actions allow policy, org rulesets |
| Organization | Members | Read & Write | peribolos: membership, teams, grants |

### Creating the App

1. As an org owner: **github.com/organizations/fullsend-ai/settings/apps/new**.
2. Name it (e.g. `fullsend-ai-governance`), homepage = this repo, webhook
   **inactive** (scheduled full-syncs only, no webhooks).
3. Set the permissions above; installable **Only on this account**.
4. Note the **App ID**; generate a **private key** (`.pem`).
5. Install on **fullsend-ai → All repositories**.

### Wiring it into this repo

In Settings → Secrets and variables → Actions:

- Variable `GOVERNANCE_APP_ID` = the App ID
- Secret `GOVERNANCE_APP_PRIVATE_KEY` = the full `.pem` contents

Merges to main and the daily schedule apply for real; PRs get dry-run
checks that must report zero unexpected mutations. Until the App is
wired, the apply jobs fail at token minting — expected on the first
push. The Actions policy is pinned to the live permissive values
(`allowed_actions: all`); restricting it is a deliberate future change —
run a `uses:` inventory across the org first. All governance writes land
in the audit log as the App's bot user.

## Making changes

- People, teams, repo access → `peribolos.yaml`
- Repo settings and rulesets → `settings.yml` / `repos/<repo>.yml`
- Secret scanning on public repos → `org/secret-scanning.json`
- Every change lands through a PR with a lint check and a dry-run check.
