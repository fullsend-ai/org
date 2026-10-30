#!/bin/bash
# Apply the committed inference gateway config to Cloud Run (manual phase).
#
# usage: inference-gateway/deploy.sh <step>
#   check   config.yaml rendered, allowlist ids match GitHub, agentgateway --validate-only
#   image   copy the pinned agentgateway release (by digest) into Artifact Registry
#   apply   from a clean checkout equal to fullsend-ai/org main: check, add config.yaml as a
#           config secret version if it changed, roll a revision that pins that version, the
#           image digest, MAX_INSTANCES, CONCURRENCY, CPU/MEMORY and request-based billing, and
#           send all traffic to it (the OpenAI key mount is left as it is)
#   post    fail unless a non-JWT bearer gets 401 text/plain and the newest revision is ready,
#           runs exactly what apply set, serves 100% of traffic and logged "started bind"
#
# Needs: curl, gcloud, gh, git, jq, python3 + pyyaml, shasum, skopeo + docker-credential-gcloud (image), podman or
# docker (check). Never reads the OpenAI key secret.
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
case "${1:-}" in
check | image | apply | post) ;;
*)
  # Print the header comment as usage.
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit 2
  ;;
esac
# eval is safe only because render.py rejects any value outside [A-Za-z0-9._:/-] (SAFE_VALUE).
envs=$(python3 "$D/render.py" --print-env)
eval "$envs"
SRC=ghcr.io/agentgateway/agentgateway@sha256:$DIGEST
AR=$R-docker.pkg.dev/$P/$N/agentgateway
URL=https://$N-$PNUM.$R.run.app

check() {
  python3 "$D/render.py" --check --verify-ids
  local engine V out
  engine=$(command -v podman || command -v docker || true)
  [ -n "$engine" ] || {
    echo "check needs podman or docker" >&2
    exit 1
  }
  V=$(mktemp -d)
  mkdir -p "$V/config" "$V/upstream"
  cp "$D/config.yaml" "$V/config/"
  printf x >"$V/upstream/key"
  chmod -R a+rX "$V"
  out=$("$engine" run --rm -v "$V/config:/etc/agw-config:ro" -v "$V/upstream:/etc/agw-upstream:ro" \
    "$SRC" -f /etc/agw-config/config.yaml --validate-only 2>&1) || {
    rm -rf "$V"
    echo "$out" >&2
    exit 1
  }
  rm -rf "$V"
  echo "$out" | tail -1
}

require_main() {
  # Compare against the canonical repo, whatever "origin" points at in this clone.
  git -C "$D" fetch -q https://github.com/fullsend-ai/org.git main
  [ -z "$(git -C "$D" status --porcelain)" ] || {
    echo "apply: working tree is not clean" >&2
    exit 1
  }
  [ "$(git -C "$D" rev-parse HEAD)" = "$(git -C "$D" rev-parse FETCH_HEAD)" ] ||
    {
      echo "apply: HEAD is not fullsend-ai/org main; apply only merged config" >&2
      exit 1
    }
}

case "${1:-}" in
check) check ;;
image)
  # gcloud signs the registry access through its credential helper; no token on the command line.
  A=$(mktemp)
  trap 'rm -f "$A"' EXIT
  printf '{"credHelpers":{"%s-docker.pkg.dev":"gcloud"}}' "$R" >"$A"
  skopeo copy --all --quiet --dest-authfile "$A" "docker://$SRC" "docker://$AR:$VERSION"
  got=$(skopeo inspect --raw --authfile "$A" "docker://$AR:$VERSION" | shasum -a 256 | cut -d' ' -f1)
  [ "$got" = "$DIGEST" ] || {
    echo "AR tag $VERSION resolves to $got, not $DIGEST" >&2
    exit 1
  }
  echo "image $AR@sha256:$DIGEST ($VERSION)"
  ;;
apply)
  require_main
  check
  # Fail before any write if the pinned image is not in Artifact Registry (run `image` first).
  gcloud artifacts docker images describe "$AR@sha256:$DIGEST" --format='value(image_summary.digest)' >/dev/null
  # Compare with, and reuse, one explicit version: the newest enabled one.
  cur=$(gcloud secrets versions list "$N-config" --project "$P" --filter=state=ENABLED \
    --sort-by=~createTime --limit 1 --format='value(name.basename())')
  T=$(mktemp)
  trap 'rm -f "$T"' EXIT
  if [ -n "$cur" ]; then
    gcloud secrets versions access "$cur" --secret "$N-config" --project "$P" --out-file="$T"
  fi
  if [ -n "$cur" ] && cmp -s "$T" "$D/config.yaml"; then
    ver=$cur
    echo "config unchanged: version $ver"
  else
    ver=$(gcloud secrets versions add "$N-config" --project "$P" --data-file="$D/config.yaml" \
      --format='value(name.basename())')
    echo "config version $ver added"
  fi
  gcloud run services update "$N" --project "$P" --region "$R" --image "$AR@sha256:$DIGEST" \
    --max "$MAX_INSTANCES" --max-instances "$MAX_INSTANCES" --concurrency "$CONCURRENCY" \
    --min 0 --min-instances 0 --cpu "$CPU" --memory "$MEMORY" --cpu-throttling \
    --update-secrets "/etc/agw-config/config.yaml=$N-config:$ver"
  # After a rollback traffic is pinned to an old revision; move it back to the new one.
  gcloud run services update-traffic "$N" --project "$P" --region "$R" --to-latest
  ;;
post)
  got=$(curl -s --connect-timeout 10 --max-time 30 -o /dev/null -w '%{http_code} %{content_type}' \
    -H 'authorization: Bearer not-a-jwt' "$URL/v1/models")
  case "$got" in
  "401 text/plain"*) echo "non-JWT bearer: $got" ;;
  *)
    echo "non-JWT bearer: expected 401 text/plain, got '$got' (HTML 401 = invoker IAM check on)" >&2
    exit 1
    ;;
  esac
  st=$(gcloud run services describe "$N" --project "$P" --region "$R" --format=json)
  created=$(jq -r '.status.latestCreatedRevisionName // ""' <<<"$st")
  ready=$(jq -r '.status.latestReadyRevisionName // ""' <<<"$st")
  [ -n "$created" ] && [ "$created" = "$ready" ] || {
    echo "newest revision '$created' is not ready (ready: '$ready')" >&2
    exit 1
  }
  image=$(jq -r '.spec.template.spec.containers[0].image // ""' <<<"$st")
  [ "$image" = "$AR@sha256:$DIGEST" ] || {
    echo "service runs $image, not the pinned $AR@sha256:$DIGEST" >&2
    exit 1
  }
  # The config volume mounted at /etc/agw-config must pin the newest enabled config version.
  mounted=$(jq -r '.spec.template.spec as $t | ($t.containers[0].volumeMounts[] | select(.mountPath == "/etc/agw-config") | .name) as $v | $t.volumes[] | select(.name == $v) | .secret.items[0].key' <<<"$st")
  newest=$(gcloud secrets versions list "$N-config" --project "$P" --filter=state=ENABLED \
    --sort-by=~createTime --limit 1 --format='value(name.basename())')
  [ -n "$newest" ] && [ "$mounted" = "$newest" ] || {
    echo "service mounts config version '$mounted', not the newest enabled '$newest'" >&2
    exit 1
  }
  live_slots=$(jq -r '.spec.template.spec.containerConcurrency // ""' <<<"$st")
  [ "$live_slots" = "$CONCURRENCY" ] || {
    echo "service concurrency is '$live_slots', not CONCURRENCY=$CONCURRENCY" >&2
    exit 1
  }
  # The cost model in the README assumes these: request-based billing, scale to zero, CPU/MEMORY.
  # Both minimums: per revision (--min-instances) and service-wide (--min).
  sizing=$(jq -r '(.metadata.annotations["run.googleapis.com/minScale"] // "0") as $svcmin | .spec.template | [.spec.containers[0].resources.limits.cpu, .spec.containers[0].resources.limits.memory, (.metadata.annotations["run.googleapis.com/cpu-throttling"] // "true"), (.metadata.annotations["autoscaling.knative.dev/minScale"] // "0"), $svcmin] | join(" ")' <<<"$st")
  [ "$sizing" = "$CPU $MEMORY true 0 0" ] || {
    echo "service runs cpu/memory/cpu-throttling/revision-min/service-min '$sizing', not '$CPU $MEMORY true 0 0'" >&2
    exit 1
  }
  serving=$(jq -r --arg r "$created" '[.status.traffic[] | select(.revisionName == $r) | .percent] | add // 0' <<<"$st")
  [ "$serving" = 100 ] || {
    echo "$created serves $serving% of traffic, not 100%" >&2
    exit 1
  }
  # Search from the revision's creation (an unchanged apply keeps an old revision). Its startup
  # line is gone once the revision outlives Cloud Logging's default 30-day retention.
  since=$(gcloud run revisions describe "$created" --project "$P" --region "$R" --format='value(metadata.creationTimestamp)')
  [ -n "$since" ] || {
    echo "no creationTimestamp for revision $created" >&2
    exit 1
  }
  age_days=$(python3 -c 'import datetime, sys; t = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")); print((datetime.datetime.now(datetime.timezone.utc) - t).days)' "$since")
  if [ "$age_days" -ge 29 ]; then
    echo "$created is $age_days days old; its startup log is past retention (readiness and traffic checked above)"
    exit 0
  fi
  for _ in 1 2 3 4 5 6; do
    n=$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"$created\" AND textPayload:\"started bind\" AND timestamp>=\"$since\"" \
      --project "$P" --limit 1 --format 'value(textPayload)' | wc -l)
    [ "$n" -ge 1 ] && {
      echo "$created started"
      exit 0
    }
    sleep 10
  done
  echo "no 'started bind' log line for $created" >&2
  exit 1
  ;;
esac
