# Onboard a repository in the fullsend-ai organization

Follow these steps to give a repository in `fullsend-ai` its team access and
fullsend agents (`/fs-triage`, `/fs-code`, `/fs-review`). You open two pull
requests: one here (step 3) and one in `fullsend-ai/.fullsend` (step 4).
Steps 1, 3 and 4 are yours; maintainers do step 2, the gateway deploy and
the sync. Start with the checks below, then step 1.

## Before you start

- The repository is already in `fullsend-ai`, created or transferred, and
  has at least one commit on its default branch. The sync cannot install
  into an empty repository.
- You have `gh`, Python 3 with `pyyaml`, and a clone of this repository.
- Sign off every commit with `git commit -s`; every PR is checked for a
  Developer Certificate of Origin (DCO) sign-off.
- **Moved in from another org?** Check for `.fullsend/config.yaml` in the
  repository. If it does not start with
  `# This file is managed by 'fullsend repos': repos-config-v1`, remove it
  before step 4 merges:
  1. Note any settings that differ from the fleet defaults; they go in the
     repository's `repos.yaml` entry in step 4.
  2. Open a PR in the repository that deletes `.fullsend/config.yaml`, and
     merge it.

  Otherwise the sync skips the repository, and a file with old keys such as
  `create_issues` stops the sync for every repository (see
  [Troubleshooting](#troubleshooting)).
- `@fullsend-ai/maintainers` do step 2 (ask now), the gateway deploy (after
  step 3 merges), the sync (after step 4 merges) and the agent App installs.

## Steps

1. **Look up the repository's ids.** The gateway allowlist pins both:

   ```console
   $ gh api repos/fullsend-ai/<repo> --jq '"\(.id) \(.owner.id)"'
   1428851360 271249281
   ```

   The output shown is for `devlake`; use your repository's ids in step 3.

2. **Ask a maintainer for an inference provider.** They run
   `fullsend inference provision fullsend-ai/<repo> --project it-gcp-konflux-prod-fullsend`
   and send you the provider resource name it prints. For `devlake` it is
   `projects/155031062349/locations/global/workloadIdentityPools/fullsend-inference/providers/gh-fullsend-ai-devlake`.
   Keep it for step 4. Each repository has its own; never reuse another's.

3. **Open one PR here with access and gateway changes.** Put both in the
   same PR, so a second push does not reset approvals.
   - In `peribolos.yaml`, add the repository to the four teams, and add
     direct collaborators under `orgs.fullsend-ai.repos.<repo>.collaborators`
     if needed. Peribolos removes anything not listed, so keep all other
     entries.

     ```yaml
     # orgs.fullsend-ai.teams.admins.repos
     <repo>: admin
     # orgs.fullsend-ai.teams.maintainers.repos
     <repo>: maintain
     # orgs.fullsend-ai.teams.triagers.repos
     <repo>: triage
     # orgs.fullsend-ai.teams.writers.repos
     <repo>: write
     ```

   - Add the repository to `inference-gateway/allowlist.yaml` with the ids
     from step 1, then render the config:

     ```yaml
       - { name: fullsend-ai/<repo>, id: "<id>", owner_id: "<owner-id>" }
     ```

     ```console
     $ python3 inference-gateway/render.py --verify-ids
     all allowlist ids match GitHub
     wrote inference-gateway/config.yaml
     ```

   - Only if the repository needs its own default-branch ruleset, add
     `repos/<repo>.yml` and list `fullsend-ai-sync` (`actor_id: 4648700`,
     as in `settings.yml`) in `bypass_actors`. Without it, the sync opens a
     PR every time instead of pushing.
   - From the repository root, run `python3 scripts/lint.py`, then commit
     `peribolos.yaml`, `inference-gateway/allowlist.yaml`,
     `inference-gateway/config.yaml` and, if you added it,
     `repos/<repo>.yml`.
   - On a PR from a fork, the `peribolos-dry-run` and
     `safe-settings-dry-run` checks are skipped, so reviewers check
     `peribolos.yaml` by hand. Branches in this repository get both dry runs.

   After it merges, peribolos applies the access. Anyone listed who is not
   yet an org member gets an email invitation and has access only after
   accepting it. A maintainer then
   [deploys the gateway change](../inference-gateway/README.md#apply-a-change);
   until then, the repository's review subagents get a 403.

4. **Enroll the repository in the fleet manifest.** Open a PR against
   [`fullsend-ai/.fullsend`](https://github.com/fullsend-ai/.fullsend)
   `repos.yaml` that appends an entry under `github.repos`, with the
   provider from step 2 and any settings carried over from an old
   `.fullsend/config.yaml`:

   ```yaml
           - name: fullsend-ai/<repo>
             config:
               inference:
                 project: it-gcp-konflux-prod-fullsend
                 wif_provider: <provider-from-step-2>
   ```

   On a fork PR, **Manifest Check** waits until a maintainer approves the
   run. It must pass before merge.

5. **Sync and verify.** The sync runs on Mondays at 06:00 UTC and on each
   fullsend release. To run it sooner, a maintainer previews and then applies
   just this repository:

   ```bash
   gh workflow run sync-scaffold.yml -R fullsend-ai/.fullsend -f dry_run=true -f repos=fullsend-ai/<repo>
   gh workflow run sync-scaffold.yml -R fullsend-ai/.fullsend -f dry_run=false -f repos=fullsend-ai/<repo>
   ```

   The sync App pushes the shim workflows, variables, secrets and the managed
   `.fullsend/config.yaml` straight to the default branch. Change settings
   later in `repos.yaml`, not in the repository.

## What success looks like

- The Sync Scaffold run's last line, `✓ Install complete: … installed, …
  converged, … already current, … failed`, counts the repository as
  installed or converged and shows `0 failed`.
- The repository's `.fullsend/config.yaml` starts with
  `# This file is managed by 'fullsend repos': repos-config-v1`.
- The repository has the `FULLSEND_*` Actions variables (such as
  `FULLSEND_MINT_URL`) and secrets (`FULLSEND_GCP_PROJECT_ID`,
  `FULLSEND_GCP_WIF_PROVIDER`).
- `/fs-review` on a test PR finishes and posts a review, with no
  `403 model_authorization_denied` in the job log.

## Troubleshooting

| Error text or symptom | Cause | Fix |
|---|---|---|
| `configuration preflight: fullsend-ai/<repo>: existing .fullsend/config.yaml is invalid: create_issues is no longer supported` | The repository kept a config file with old keys. This stops the sync for **every** repository. | Move the settings into `repos.yaml`, delete the file with a PR in the repository, rerun the sync. |
| `adoption required: .fullsend/config.yaml exists without the managed-configuration ownership marker` | A valid config file without the managed header. The sync skips this repository. | Same fix: carry the settings into `repos.yaml`, delete the file, rerun the sync. |
| `FULLSEND_MINT_URL is not set` | The sync has not run on the repository yet. | Run step 5. |
| `403 model_authorization_denied` | The allowlist change merged but is not deployed. | Ask a maintainer to [deploy it](../inference-gateway/README.md#apply-a-change). |
| `Default branch is protected — creating scaffold PR instead` | The sync App cannot push to the default branch. | Merge the scaffold PR. To stop it recurring, add the `fullsend-ai-sync` bypass from step 3. |
| An agent App cannot read the repository | The [agent GitHub Apps](https://github.com/fullsend-ai/fullsend/blob/main/docs/guides/getting-started/configuring-github.md#installing-github-applications) do not cover it. | Ask a maintainer to add the repository to the triage, coder, review, retro, prioritize and sync App installations. |
