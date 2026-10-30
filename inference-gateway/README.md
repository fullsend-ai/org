# Fleet inference gateway

One [agentgateway](https://github.com/agentgateway/agentgateway) on Cloud Run holds the fleet's
OpenAI key. Allowlisted repositories call `gpt-6.1-sol` with their GitHub Actions OIDC token, so no
repository holds an OpenAI key. This is the gateway route of fullsend ADR 0137; fullsend's
[operator guide](https://github.com/fullsend-ai/fullsend/blob/main/docs/guides/infrastructure/inference-gateway-operator.md)
explains each config block.

| | |
|---|---|
| URL | `https://fullsend-inference-gateway-155031062349.us-central1.run.app` |
| Audience | `fullsend-inference-gateway` |
| Auth | `jwtAuth` strict (issuer `token.actions.githubusercontent.com`), no API keys |
| Allow rule | `repository`, `repository_id` and `repository_owner_id` must all match an entry in `allowlist.yaml`. Trust is per repository: any workflow in a listed repo that can mint an OIDC token can call the gateway, and a leaked token works until it expires (about 5 minutes observed). Fork pull-request runs get no `id-token: write`. Pinning `job_workflow_ref` is a planned hardening |
| Models | `gpt-6.1-sol` (OpenAI Responses API) |
| Rate cap | Per Cloud Run instance: allowlisted callers share one bucket, everyone else another (`RPM`/`TPM`); `MAX_INSTANCES` (1) bounds the instances, per revision and service-wide. A burst guard, not a spend cap: buckets reset on each new instance |

## Files

| File | Edited by hand | Purpose |
|---|---|---|
| `allowlist.yaml` | yes | Repositories that may call the gateway |
| `gateway.env` | yes | Project, region, names, agentgateway version + digest, rate caps, instance limit (plain `KEY=VALUE`) |
| `config.tmpl.yaml` | yes | agentgateway config template |
| `config.yaml` | **no** | Rendered by `render.py`; what is deployed. `scripts/lint.py` fails when it is stale |
| `render.py` | | Renders `config.yaml`. Refuses malformed names, ids or env values, auth that is not strict GitHub OIDC for the audience, and any model whose allow rules are not exactly the allowlist test. `--verify-ids` checks the allowlist against the GitHub API |
| `test_render.py` | | Tests for the rejection paths (`python3 -m unittest discover -s inference-gateway`) |
| `deploy.sh` | | Manual apply: `check`, `image`, `apply`, `post` |

## Enroll a repository

1. Look up its ids: `gh api repos/<owner>/<name> --jq '"\(.id) \(.owner.id)"'`.
2. Add `- { name: <owner>/<name>, id: "<id>", owner_id: "<owner id>" }` to `allowlist.yaml`, run
   `python3 inference-gateway/render.py --verify-ids`, and open a pull request with both files.
3. After merge, a maintainer applies it (below).
4. Point the repository at the gateway: an `inference.gateway` block plus `gateway/gpt-6.1-sol`
   models in its `.fullsend/config.yaml`. For fleet repositories that comes from
   [`fullsend-ai/.fullsend`](https://github.com/fullsend-ai/.fullsend) `repos.yaml`.

Removing a repository is the same edit in reverse. A renamed or transferred repository stops
matching until its entry is updated.

## Apply (manual phase)

From a clean checkout equal to `fullsend-ai/org` `main` (`apply` refuses anything else), with a
gcloud account that holds the deployer roles:

```bash
inference-gateway/deploy.sh check   # rendered + ids match GitHub + agentgateway --validate-only
inference-gateway/deploy.sh apply   # runs check; new config version if changed; new revision gets 100%
inference-gateway/deploy.sh post    # fails unless 401 text/plain and the new revision serves and started
```

Each revision mounts a numbered config version and the image by digest, so a revision always runs
the config and image it was created with. (Revisions deployed before this script, by tag and
`:latest`, fail `post` until the first `apply`.)

**Rollback:** send traffic back to the previous revision, which still mounts its own config
version:
`gcloud run services update-traffic fullsend-inference-gateway --project it-gcp-konflux-prod-fullsend --region us-central1 --to-revisions <previous-revision>=100`.
This pins traffic to that revision; the next `apply` moves it back to the latest. Then revert the
change on `main` and `apply`. Disable an old config version only when no revision you may roll back
to mounts it.

Deployer roles, each bound on one resource:

| Role | Bound on |
|---|---|
| `roles/run.developer` | the `fullsend-inference-gateway` service |
| `roles/secretmanager.secretVersionManager` + `roles/secretmanager.secretAccessor` | `fullsend-inference-gateway-config` (no secret content; `apply` compares against, and reuses, the newest enabled version) |
| `roles/iam.serviceAccountUser` | the runtime service account |
| `roles/artifactregistry.reader` (`writer` for `image`) | the `fullsend-inference-gateway` repository |
| `roles/logging.viewer` | the project (`post` reads the revision's startup log) |

The scripts never read the OpenAI key secret, but these roles are **key-equivalent**: deploying
any image as the runtime service account (`run.developer` + `serviceAccountUser`) can read it.
Grant them only to people (or a later deploy workflow) trusted with the key.

## Upgrade agentgateway

Validate the new release first in the dev project with a private check repository: a streaming
Responses call, a two-turn tool call, and pi with `pi-inference-gateway` through
`gateway/gpt-6.1-sol`. Then set `VERSION` and `DIGEST` in `gateway.env`, run `deploy.sh image`
and `deploy.sh apply`.

## OpenAI key

Secret `fullsend-inference-gateway-openai-key`, added by a project owner and readable only by the
gateway's runtime service account. To rotate it, add a new version, run
`gcloud run services update fullsend-inference-gateway --project it-gcp-konflux-prod-fullsend --region us-central1 --update-secrets /etc/agw-upstream/key=fullsend-inference-gateway-openai-key:latest`,
check a model call, disable the old version, and revoke the old key at OpenAI.

## Later: automated apply

A workflow on `main` can run `check` and `apply` through Workload Identity Federation. The job
exchanges its own OIDC token for a short-lived token of a deployer service account. That account
has the roles above, bound only to this repository's id, `refs/heads/main`, that workflow file
and a protected environment with required reviewers. No key or secret is stored in this
repository. Because the deployer roles are key-equivalent, the workflow should deploy only the
pinned digest from `gateway.env`.

## Local checks and exposure

`pre-commit install` runs Betterleaks on staged changes, ruff (Python) and shuck (shell) lint and
format checks, and `scripts/lint.py`. CI runs the same plus the render tests, agentgateway
`--validate-only` on `config.yaml` (pinned digest) and a Betterleaks scan of the whole history. Everything here is an identifier, not a credential: the
project id and number, URL, audience, secret names and roles. The fleet manifest in
`fullsend-ai/.fullsend` already publishes the project. The gateway refuses any caller without an
allowlisted GitHub OIDC token. The endpoint is public (no Cloud Run invoker IAM; the gateway does
its own auth), so a flood of refused requests can compete with fleet calls for the one instance:
an accepted risk for now, closable with Cloud Armor or a load balancer in front.
