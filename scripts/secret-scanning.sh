#!/bin/bash
# Converge secret scanning on public fullsend-ai repos to org/secret-scanning.json.
# Safe-settings has no secret-scanning support (#583), and an enterprise-enforced security
# configuration cannot be attached with a GitHub App token (admin:enterprise), so this sets the
# security_and_analysis of each unattached public repo directly (repo Administration: write).
#
# usage: scripts/secret-scanning.sh [--apply]   (default: dry run, print what would change)
#
# Not changed, only listed: repos attached to an enterprise-enforced configuration ("managed";
# the enterprise owns them), and private or internal repos (their enterprise configuration
# governs them, and secret scanning there needs a Secret Protection license). Archived
# (read-only) and *-ghsa-* repos are not listed. Exits 1 if any repo could not be read or
# written; the other repos are still processed.
set -euo pipefail
ORG=${ORG:-fullsend-ai}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WANT=${WANT:-$ROOT/org/secret-scanning.json}
case "${1:-}" in
"") apply=false ;;
--apply) apply=true ;;
*)
  echo "usage: $0 [--apply]" >&2
  exit 2
  ;;
esac
for tool in gh jq; do
  command -v "$tool" >/dev/null || {
    echo "needs $tool" >&2
    exit 1
  }
done

# Listed into a variable so a failed or partial listing stops the run under set -e.
repos=$(gh api --paginate "orgs/$ORG/repos" \
  --jq '.[] | select(.archived | not) | select(.name | contains("-ghsa-") | not) | [.name, .visibility] | @tsv' |
  sort)
[ -n "$repos" ] || {
  echo "no repositories listed for $ORG" >&2
  exit 1
}

changed=0
failed=()
while IFS=$'\t' read -r repo visibility; do
  # The block is returned only to callers with admin access; a missing block is an error,
  # never "all disabled".
  if ! have=$(gh api "repos/$ORG/$repo" --jq '.security_and_analysis // error("security_and_analysis not visible")'); then
    failed+=("$repo")
    continue
  fi
  # Keys whose live status differs from the wanted status.
  diff=$(jq -c --argjson have "$have" \
    'with_entries(select(.value.status != ($have[.key].status // "disabled")))' "$WANT")
  keys=$(jq -r 'keys | join(",")' <<<"$diff")
  if [ "$diff" = "{}" ]; then
    echo "ok       $repo"
    continue
  fi
  if [ "$visibility" != public ]; then
    echo "skip     $repo ($visibility; enterprise configuration, needs Secret Protection): $keys"
    continue
  fi
  # 204 (no body) = no configuration attached.
  if ! attached=$(gh api "repos/$ORG/$repo/code-security-configuration" --jq '.status // ""'); then
    failed+=("$repo")
    continue
  fi
  if [ "$attached" = enterprise_enforced ]; then
    echo "managed  $repo (enterprise-enforced configuration; not changed): $keys"
  elif ! $apply; then
    echo "change   $repo: $keys"
    changed=$((changed + 1))
  elif jq -n --argjson d "$diff" '{security_and_analysis: $d}' |
    gh api -X PATCH "repos/$ORG/$repo" --input - --jq '.name' >/dev/null &&
    # Read back: a write GitHub accepted but did not keep is a failure, not "applied".
    after=$(gh api "repos/$ORG/$repo" --jq '.security_and_analysis // {}') &&
    [ "$(jq -c --argjson have "$after" \
      'with_entries(select(.value.status != ($have[.key].status // "disabled")))' "$WANT")" = "{}" ]; then
    echo "applied  $repo: $keys"
    changed=$((changed + 1))
  else
    failed+=("$repo")
  fi
done <<<"$repos"

echo "$($apply && echo applied || echo "would change"): $changed public repo(s)"
if [ "${#failed[@]}" -gt 0 ]; then
  echo "failed: ${failed[*]}" >&2
  exit 1
fi
