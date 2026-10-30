# Fleet inference gateway

One [agentgateway](https://github.com/agentgateway/agentgateway) on Cloud Run holds the fleet's
OpenAI key. Allowlisted repositories call `gpt-6.1-sol` with their GitHub Actions OIDC token, so no
repository needs an OpenAI key secret (fullsend ADR 0137). fullsend's
[operator guide](https://github.com/fullsend-ai/fullsend/blob/main/docs/guides/infrastructure/inference-gateway-operator.md)
explains each config block.

## Start here

| I want to… | Who | Go to |
|---|---|---|
| Let my repository use the gateway | Repository owner | [Enroll a repository](#enroll-a-repository) |
| Deploy a merged change | Maintainer with the deployer roles | [Apply a change](#apply-a-change) |
| Undo a bad deploy | Maintainer | [Roll back](#roll-back) |
| Check health, or decide whether to scale | Maintainer | [Health and capacity](#health-and-capacity) |
| Move to a new agentgateway release | Maintainer | [Upgrade agentgateway](#upgrade-agentgateway) |
| Replace the OpenAI key | Project owner | [Rotate the OpenAI key](#rotate-the-openai-key) |

Endpoint details, files, roles and the security model are in the [Reference](#reference) at the
end.

## Enroll a repository

You need: `gh`, Python 3 with `pyyaml` (CI uses 3.15), and a clone of this repository. The
examples use `fullsend-ai/experiments`, which is already enrolled: substitute your repository.

**1. Look up the repository's ids.** Names can be reused after a rename, ids cannot, so the
allowlist pins both:

```console
$ gh api repos/fullsend-ai/experiments --jq '"\(.id) \(.owner.id)"'
1213516245 271249281
```

**2. Add the entry** to `inference-gateway/allowlist.yaml`:

```yaml
  - { name: fullsend-ai/experiments, id: "1213516245", owner_id: "271249281" }
```

**3. Render the config** and check every entry against GitHub:

```console
$ python3 inference-gateway/render.py --verify-ids
all allowlist ids match GitHub
wrote inference-gateway/config.yaml
```

**4. Open a pull request** with `allowlist.yaml` and `config.yaml`. CI fails if `config.yaml` is
stale. After it merges, a maintainer runs [Apply a change](#apply-a-change).

**5. Point the repository at the gateway.** For fleet repositories, merge this into the
repository's entry in [`fullsend-ai/.fullsend`](https://github.com/fullsend-ai/.fullsend)
`repos.yaml`. If the entry already has `config:` or `config.inference:`, add these keys under
them instead of pasting a second block:

```yaml
          config:
            inference:
                gateway:
                    url: https://fullsend-inference-gateway-155031062349.us-central1.run.app
                    audience: fullsend-inference-gateway
                    models:
                        gpt-6.1-sol:
                            api: openai-responses
            agents:
                - name: review
                  subagents:
                    correctness: gateway/gpt-6.1-sol
                    security: gateway/gpt-6.1-sol
```

Moving every fleet repository at once means changing the existing review subagents in
`defaults.config` instead. Do that only once every manifest repository is on the allowlist;
any other repository gets 403.

For a repository outside the fleet manifest that already runs fullsend:

```bash
fullsend github setup <owner/repo> \
  --inference-gateway-url https://fullsend-inference-gateway-155031062349.us-central1.run.app \
  --inference-gateway-audience fullsend-inference-gateway \
  --inference-gateway-model gpt-6.1-sol=openai-responses
```

Then set the agents that should use the gateway to `gateway/gpt-6.1-sol` in that repository's
`.fullsend/config.yaml` (the `agents:` part of the snippet above), and check the result with
`fullsend inference gateway status`.

**6. Sync the change.** A merged `repos.yaml` change reaches the repository on the next Sync
Scaffold run (Mondays 06:00 UTC, or each fullsend release). To apply it now, a `.fullsend`
maintainer runs:

```bash
gh workflow run sync-scaffold.yml -R fullsend-ai/.fullsend -f dry_run=false -f repos=<owner/repo>
```

**7. Check it works.** Trigger a review (for example `/fs-review` on a pull request), then look for
the repository in the gateway's access log. This needs `roles/logging.viewer` on
`it-gcp-konflux-prod-fullsend`. Without it, ask a maintainer, or check that the review job's
log lists `gateway/gpt-6.1-sol` for the subagents.

```console
$ gcloud logging read 'resource.labels.service_name="fullsend-inference-gateway" AND textPayload:"jwt.sub=repo:fullsend-ai/experiments"' \
    --project it-gcp-konflux-prod-fullsend --freshness 1h --limit 1 --format 'value(textPayload)'
… http.path=/v1/responses http.status=200 jwt.sub=repo:fullsend-ai/experiments:ref:refs/heads/main
  gen_ai.request.model=gpt-6.1-sol gen_ai.usage.input_tokens=31711 … duration=1377ms
```

A `403 model_authorization_denied` means the repository is not on the deployed allowlist yet.

**To remove a repository**, point it away from the gateway first, then delete its entry and
repeat steps 3 and 4, and apply. A renamed or
transferred repository stops matching until its entry is updated.

## Apply a change

You need: a gcloud login with the [deployer roles](#deployer-roles), `gh`, `git`, `jq`, `curl`,
`skopeo`, Python 3 with `pyyaml`, and podman or docker. Start from a clean clone of
`fullsend-ai/org` at `main`: `apply` refuses anything else.

**1. Check** the rendered config, the allowlist ids and agentgateway's own validation:

```console
$ inference-gateway/deploy.sh check
all allowlist ids match GitHub
Configuration is valid!
```

**2. Apply.** This adds `config.yaml` as a new config secret version if it changed, then rolls a
revision that pins that version, the image digest and the size settings, and sends it all
traffic:

```console
$ inference-gateway/deploy.sh apply
…
Service [fullsend-inference-gateway] revision [fullsend-inference-gateway-00006-8f4] has been deployed and is serving 100 percent of traffic.
…
Traffic:
  100% LATEST (currently fullsend-inference-gateway-00006-8f4)
```

**3. Verify.** `post` fails unless the gateway answers a bad token with `401 text/plain` and the
new revision runs exactly what `apply` set, serves all traffic, and has started:

```console
$ inference-gateway/deploy.sh post
non-JWT bearer: 401 text/plain
fullsend-inference-gateway-00006-8f4 started
```

If `post` fails, [roll back](#roll-back).

## Roll back

Each revision keeps the config version and image it was created with, so moving traffic back to
the previous revision restores the previous state:

```bash
gcloud run revisions list --service fullsend-inference-gateway \
  --project it-gcp-konflux-prod-fullsend --region us-central1 --limit 3
gcloud run services update-traffic fullsend-inference-gateway \
  --project it-gcp-konflux-prod-fullsend --region us-central1 --to-revisions <previous-revision>=100
```

Then revert the change on `main` and [apply](#apply-a-change) again. That apply moves traffic back
to the latest revision. Disable an old config version only when no revision you may roll back to
still mounts it.

## Health and capacity

### Is it healthy?

- `inference-gateway/deploy.sh post` re-runs the checks from [Apply a change](#apply-a-change).
- The access log has one line per request, with `http.status`, `jwt.sub` (the calling repository),
  `gen_ai.request.model`, token counts and `duration`. Look for 429s (limits) and 5xx (upstream):

```bash
gcloud logging read 'resource.labels.service_name="fullsend-inference-gateway" AND (textPayload:"http.status=429" OR textPayload:"http.status=5")' \
  --project it-gcp-konflux-prod-fullsend --freshness 1d --limit 20 --format 'value(timestamp,textPayload)'
```

- Cloud Run's own 429s (every instance slot busy) never reach the gateway, so they are only in
  Cloud Run's request log:

```bash
gcloud logging read 'resource.labels.service_name="fullsend-inference-gateway" AND logName:"run.googleapis.com%2Frequests" AND httpRequest.status=429' \
  --project it-gcp-konflux-prod-fullsend --freshness 1d --limit 20 --format 'value(timestamp,httpRequest.status,httpRequest.latency)'
```

- CPU, memory, instance count and request latency are on the service's Cloud Run metrics page.

### The limits

All set in `gateway.env`. After changing one, run `render.py`, open a pull request, and apply.

| Setting | Value | What it limits | What you see when it is hit |
|---|---|---|---|
| `RPM` | 2000 | Requests per minute per instance from allowlisted repositories | 429 `rate limit exceeded` from the gateway |
| `TPM` | 20000000 | Tokens per minute per instance, split the same way as `RPM`. Charged when each call ends (input, including cached, plus output). A call starts as long as the bucket is not empty, so calls in flight can overshoot it | 429 from the gateway once the bucket is empty |
| `CONCURRENCY` | 250 | Requests one instance handles at the same time (at most 1000). A streamed model call holds its slot for its whole 1–13 s | Cloud Run queues new requests, then returns 429 "no available instance" |
| `MAX_INSTANCES` | 1 | Instances, per revision and service-wide. One keeps the rate-limit buckets in one place | — |
| `CPU` / `MEMORY` | 1 / 512Mi | Per instance. `apply` also sets request-based billing and a minimum of 0 instances (per revision and service-wide), and `post` checks them all | — |

Callers that are not on the allowlist share a separate bucket, so they cannot use up the fleet's.
The caps guard against bursts; they are not a spend limit. The buckets live in memory and reset
with each new instance or revision. The OpenAI project has no limit of its own: at the `TPM` cap
one instance can pass about 1.2 billion tokens an hour. **Set a budget alert on the OpenAI
project**, and lower `TPM` if a runaway caller is a concern.

### What one instance handles

Measured on 2026-10-30 on
[experiments#58](https://github.com/fullsend-ai/experiments/pull/58). One review run's
`correctness` and `security` subagents made 12 calls in about 40 s, with about 232k input and 2k
output tokens. Each call took 1.4–12.9 s, which is OpenAI's response time. The subagents took
20–40 s, the same as calling OpenAI directly. A whole review takes 7–12 minutes because of the
slower Gemini subagents and sandbox setup, not the gateway. agentgateway's own benchmarks
([agentgateway.dev](https://agentgateway.dev/blog/2026-06-26-benchmarking-agentgateway-vs-litellm-part-2/),
June 2026) show 0.44 ms p99 overhead at 3,000 requests per second on about 13% of one core.

| Limit | One review run uses | So one instance handles |
|---|---|---|
| `RPM` 2000 | 12 requests | about 160 runs per minute |
| `TPM` 20M | about 234k tokens | about 86 runs per minute (a refill rate; calls in flight can overshoot it) |
| `CONCURRENCY` 250 | up to about 4 calls at once | about 60 runs at the same time |

The fleet's busiest minutes today see a few runs.

### When to scale

| You see | Do this |
|---|---|
| Gateway 429s (`reason=RateLimit`) for allowlisted repositories | Raise `RPM` or `TPM` |
| Cloud Run 429 "no available instance", or latency rising while OpenAI time stays flat | Raise `CONCURRENCY` (up to 1000; agentgateway handles several hundred streams on one core) |
| CPU above about 70% or memory above about 400 MiB | Raise `CPU`/`MEMORY`, or set `MAX_INSTANCES=2` and halve `RPM`/`TPM` (each instance gets its own buckets) |
| The first request after an idle period is slow | Keep one instance warm. This needs a `MIN_INSTANCES` setting added to `gateway.env`, `render.py` and `deploy.sh` first: today `apply` sets 0 and `post` requires 0. A warm instance is billed at the lower idle rate |

### Cost

Cloud Run bills CPU and memory only while a request is in flight; overlapping requests on one
instance are billed once, and with no traffic it scales to zero. us-central1 prices (Cloud
Billing catalog, 2026-10-30): $0.000024 per vCPU-second, $0.0000025 per GiB-second, $0.40 per
million requests after the first 2 million. The table leaves out the monthly CPU and memory free
tier, which is per billing account and assumed to be used by other projects.

| Load | Busy time per month | Cloud Run cost per month |
|---|---|---|
| 100 review runs a day (about 40 s of gateway time each) | about 33 h | about $3 |
| Busy all month | 730 h | about $65, plus requests over 2 million |

OpenAI tokens are the real cost, about 232k input tokens per review run. Track them on the OpenAI
project's usage page.

## Upgrade agentgateway

1. Validate the release in the dev project first, from a private check repository: a streaming
   Responses call, a two-turn tool call, and pi with `pi-inference-gateway` through
   `gateway/gpt-6.1-sol`.
2. Set `VERSION` and `DIGEST` (the release's sha256 index digest) in `gateway.env`, run
   `render.py`, and open a pull request.
3. After merge: `deploy.sh image`, then [apply](#apply-a-change).

## Rotate the OpenAI key

The key lives in Secret Manager as `fullsend-inference-gateway-openai-key`. Only the gateway's
runtime service account can read it.

1. Add the new key as a version, in your own terminal (it is never echoed or written to disk):

   ```zsh
   read -rs 'K?OpenAI API key: ' && printf %s "$K" | gcloud secrets versions add \
     fullsend-inference-gateway-openai-key --project it-gcp-konflux-prod-fullsend --data-file=- ; unset K; echo
   ```

   In bash, start with `read -rsp 'OpenAI API key: ' K` instead.

2. Roll a revision that reads it:

   ```bash
   gcloud run services update fullsend-inference-gateway --project it-gcp-konflux-prod-fullsend \
     --region us-central1 --update-secrets /etc/agw-upstream/key=fullsend-inference-gateway-openai-key:latest
   ```

3. Trigger a review and [check the access log](#is-it-healthy) for 200s, then disable the old
   version and revoke the old key at OpenAI.

## Reference

### Endpoint

| | |
|---|---|
| URL | `https://fullsend-inference-gateway-155031062349.us-central1.run.app` |
| Audience | `fullsend-inference-gateway` |
| Auth | Strict `jwtAuth`: GitHub Actions OIDC only (`token.actions.githubusercontent.com` and its JWKS), no API keys |
| Allow rule | `repository`, `repository_id` and `repository_owner_id` must all match an `allowlist.yaml` entry |
| Models | `gpt-6.1-sol` (OpenAI Responses API) |

### Files

| File | Edit by hand? | Purpose |
|---|---|---|
| `allowlist.yaml` | yes | Repositories that may call the gateway |
| `gateway.env` | yes | Project, region, names, agentgateway version and digest, limits and instance size. Plain `KEY=VALUE` lines |
| `config.tmpl.yaml` | yes | agentgateway config template |
| `config.yaml` | **no** | Rendered by `render.py`; this is what is deployed |
| `render.py` | | Renders `config.yaml`. Refuses malformed input and any change that weakens the auth or the allow rule. `--verify-ids` checks the allowlist against GitHub |
| `test_render.py` | | Tests for those refusals: `python3 -m unittest discover -s inference-gateway` |
| `deploy.sh` | | `check`, `image`, `apply`, `post` |

### Deployer roles

Each role is bound on one resource:

| Role | Bound on |
|---|---|
| `roles/run.developer` | the `fullsend-inference-gateway` service |
| `roles/secretmanager.secretVersionManager` and `roles/secretmanager.secretAccessor` | `fullsend-inference-gateway-config` (no secret content; `apply` compares against the newest enabled version) |
| `roles/iam.serviceAccountUser` | the gateway's runtime service account |
| `roles/artifactregistry.reader` (`writer` for `image`) | the `fullsend-inference-gateway` repository |
| `roles/logging.viewer` | the project (`post` reads the startup log) |

The scripts never read the OpenAI key, but these roles are **key-equivalent**: whoever can deploy
an image as the runtime service account can read it. Grant them only to people trusted with the
key.

### Security model

- **Trust is per repository.** Any workflow in an allowlisted repository that can mint an OIDC
  token can call the gateway. A leaked token works until it expires (about 5 minutes observed).
  Fork pull-request runs get no `id-token: write`. Pinning `job_workflow_ref` to the fleet's
  dispatch workflow is planned hardening.
- **The endpoint is public.** The gateway does its own authentication, so Cloud Run's invoker IAM
  is off. Refused requests cost microseconds, but a large flood could still compete for the one
  instance. This is an accepted risk for now; Cloud Armor or a load balancer in front would close
  it.
- **Nothing here is a credential.** The project id and number, URL, audience, secret names and
  roles are identifiers. The fleet manifest in `fullsend-ai/.fullsend` already publishes the
  project.
- **Checks.** `pre-commit install` runs Betterleaks, ruff, shuck and the governance lint on each
  commit. CI also runs the render tests, agentgateway `--validate-only` on `config.yaml`, and a
  Betterleaks scan of the full history.

### Later: automated apply

A workflow on `main` can run `check` and `apply` through Workload Identity Federation, with no key
stored in this repository. The job exchanges its own OIDC token for a short-lived token of a
deployer service account. That account has the roles above, bound only to this repository's id,
`refs/heads/main`, that workflow file, and a protected environment with required reviewers.
Because those roles are key-equivalent, the workflow must deploy only the pinned digest from
`gateway.env`.
