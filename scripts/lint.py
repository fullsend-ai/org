#!/usr/bin/env python3
"""Lint the governance config against the repository conventions
(AGENTS.md) and print the planned mutations (config diff vs the base
branch). Exits 1 with a list of violations."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
errors = []


def err(msg):
    errors.append(msg)


def load_yaml(path):
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        err(f"{path.name}: invalid YAML: {e}")
        return None


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--base", help="git revision to diff against (default: PR base / origin)")
args = parser.parse_args()

# --- parse everything ---
peribolos = load_yaml(ROOT / "peribolos.yaml") or {}
settings = load_yaml(ROOT / "settings.yml") or {}

repo_files = sorted((ROOT / "repos").glob("*.yml"))
repo_files += sorted((ROOT / "repos").glob(".*.yml"))  # dot-named repos
repo_configs = {}
for p in sorted(repo_files):
    cfg = load_yaml(p)
    if cfg is not None:
        repo_configs[p.name] = cfg

for p in sorted((ROOT / "org").glob("*.json")):
    try:
        json.loads(p.read_text())
    except json.JSONDecodeError as e:
        err(f"org/{p.name}: invalid JSON: {e}")

# --- access boundary: safe-settings must not own access ---
for name, cfg in [("settings.yml", settings)] + list(repo_configs.items()):
    if not isinstance(cfg, dict):
        continue
    for key in ("teams", "collaborators"):
        if key in cfg:
            err(f"{name}: '{key}' key forbidden -- peribolos owns all access")

# --- peribolos conventions ---
org = peribolos.get("orgs", {}).get("fullsend-ai", {})
admins = org.get("admins") or []
if len(admins) < 2:
    err(f"peribolos.yaml: {len(admins)} admins listed; workflows run --min-admins=2")

teams = org.get("teams") or {}
for tname, team in teams.items():
    if not isinstance(team, dict):
        continue
    if team.get("privacy") != "closed":
        err(f"peribolos.yaml: team '{tname}' must set privacy: closed")

tracked = set((org.get("repos") or {}).keys())
for team in teams.values():
    if isinstance(team, dict):
        tracked |= set((team.get("repos") or {}).keys())
for name in tracked | set(repo_configs):
    if "-ghsa-" in name:
        err(f"GHSA advisory repo '{name}' must not be tracked")

# --- ruleset shape ---
VALID_ENFORCEMENT = {"active", "evaluate", "disabled"}
for name, cfg in [("settings.yml", settings)] + list(repo_configs.items()):
    for rs in (cfg or {}).get("rulesets") or []:
        label = f"{name}: ruleset '{rs.get('name')}'"
        if rs.get("target") != "branch":
            err(f"{label}: target must be branch")
        if rs.get("enforcement") not in VALID_ENFORCEMENT:
            err(f"{label}: enforcement must be one of {sorted(VALID_ENFORCEMENT)}")

# --- ruleset payload validity ---
# NOP dry-runs never send writes, so GitHub's payload validation only
# runs on the real apply. Mirror the rulesets REST API's required fields
# here so a payload the API would reject (422) fails before merge.
# Types mapped to [] need no parameters; unknown types fail.
RULE_PARAMS = {
    "creation": [],
    "update": [],
    "deletion": [],
    "non_fast_forward": [],
    "required_linear_history": [],
    "required_signatures": [],
    "copilot_code_review": [],
    "pull_request": [
        "dismiss_stale_reviews_on_push",
        "require_code_owner_review",
        "require_last_push_approval",
        "required_approving_review_count",
        "required_review_thread_resolution",
    ],
    "required_status_checks": [
        "required_status_checks",
        "strict_required_status_checks_policy",
    ],
    "merge_queue": [
        "check_response_timeout_minutes",
        "grouping_strategy",
        "max_entries_to_build",
        "max_entries_to_merge",
        "merge_method",
        "min_entries_to_merge",
        "min_entries_to_merge_wait_minutes",
    ],
    "required_deployments": ["required_deployment_environments"],
    "commit_message_pattern": ["operator", "pattern"],
    "commit_author_email_pattern": ["operator", "pattern"],
    "committer_email_pattern": ["operator", "pattern"],
    "branch_name_pattern": ["operator", "pattern"],
    "tag_name_pattern": ["operator", "pattern"],
    "file_path_restriction": ["restricted_file_paths"],
    "max_file_path_length": ["max_file_path_length"],
    "file_extension_restriction": ["restricted_file_extensions"],
    "max_file_size": ["max_file_size"],
    "workflows": ["workflows"],
    "code_scanning": ["code_scanning_tools"],
}
VALID_BYPASS_MODES = {"always", "pull_request", "exempt"}


def check_include_exclude(label, conditions, key):
    cond = conditions.get(key)
    if not isinstance(cond, dict):
        err(f"{label}: conditions.{key} is required")
        return
    for field in ("include", "exclude"):
        if not isinstance(cond.get(field), list):
            err(f"{label}: conditions.{key}.{field} must be a list (API rejects it missing)")


for name, cfg in [("settings.yml", settings)] + list(repo_configs.items()):
    for rs in (cfg or {}).get("rulesets") or []:
        label = f"{name}: ruleset '{rs.get('name')}'"
        if not rs.get("name"):
            err(f"{label}: name is required")
        conditions = rs.get("conditions") or {}
        check_include_exclude(label, conditions, "ref_name")
        if name == "settings.yml":  # org-level ruleset: must target repos
            check_include_exclude(label, conditions, "repository_name")
        for actor in rs.get("bypass_actors") or []:
            if not actor.get("actor_type"):
                err(f"{label}: bypass actor missing 'actor_type'")
            if actor.get("bypass_mode", "always") not in VALID_BYPASS_MODES:
                err(f"{label}: bypass_mode must be one of {sorted(VALID_BYPASS_MODES)}")
        for rule in rs.get("rules") or []:
            rtype = rule.get("type")
            if rtype not in RULE_PARAMS:
                err(f"{label}: unknown rule type '{rtype}'")
                continue
            params = rule.get("parameters") or {}
            for field in RULE_PARAMS[rtype]:
                if field not in params:
                    err(f"{label}: rule '{rtype}' missing required parameter '{field}'")
            if rtype == "required_status_checks":
                for check in params.get("required_status_checks") or []:
                    if not check.get("context"):
                        err(f"{label}: required status check missing 'context'")


# --- planned mutations: config diff vs the base branch ---
# Main converges to zero drift (apply on merge + daily reconcile), so the
# PR's diff against its base is what the apply jobs will mutate. Unlike
# the safe-settings dry-run (which reads main via the API), this reads
# the PR's own files.
def git(*cmd):
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *cmd], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def resolve_base():
    if args.base:
        return git("rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}")
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:  # PR CI: the shallow checkout lacks the base; fetch it
        if git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", f"refs/heads/{base_ref}") is None:
            return None
        return git("rev-parse", "FETCH_HEAD")
    for ref in ("origin/HEAD", "origin/main"):
        sha = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return git("merge-base", "HEAD", sha) or sha
    return None


def is_config(path):
    parts = path.split("/")
    if len(parts) == 1:
        return path in ("peribolos.yaml", "settings.yml", "deployment-settings.yml")
    return len(parts) == 2 and (
        (parts[0] == "repos" and parts[1].endswith(".yml"))
        or (parts[0] == "org" and parts[1].endswith(".json"))
    )


def parse(path, text):
    try:
        return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None


def as_map(items):
    """Key a list by item identity so it diffs per item, or None."""
    if not items:
        return {}
    for key in ("name", "type", "context", "actor_id"):
        if all(isinstance(i, dict) and key in i for i in items):
            ids = [str(i[key]) for i in items]
            if len(set(ids)) == len(ids):
                return dict(zip(ids, items))
    return None


def diff(old, new, path, out):
    if old == new:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        pairs = [(str(k), old.get(k), new.get(k), k in old, k in new) for k in set(old) | set(new)]
        sep = "."
    elif isinstance(old, list) and isinstance(new, list):
        if all(not isinstance(i, (dict, list)) for i in old + new):
            for v in sorted(set(map(str, new)) - set(map(str, old))):
                out.append(f"+ {path}: {v}")
            for v in sorted(set(map(str, old)) - set(map(str, new))):
                out.append(f"- {path}: {v}")
            return  # pure reorder: not a mutation
        mo, mn = as_map(old), as_map(new)
        if mo is None or mn is None:
            out.append(f"~ {path}: {show(old)} -> {show(new)}")
            return
        pairs = [(k, mo.get(k), mn.get(k), k in mo, k in mn) for k in set(mo) | set(mn)]
        sep = "["
    else:
        out.append(f"~ {path}: {show(old)} -> {show(new)}")
        return
    for k, o, n, in_old, in_new in sorted(pairs):
        p = f"{path}[{k}]" if sep == "[" else (f"{path}.{k}" if path else k)
        if not in_old:
            out.append(f"+ {p}: {show(n)}")
        elif not in_new:
            out.append(f"- {p}: {show(o)}")
        else:
            diff(o, n, p, out)


def show(v, limit=100):
    s = json.dumps(v, sort_keys=True, default=str)
    return s if len(s) <= limit else s[: limit - 3] + "..."


plan = {}
base = resolve_base()
if base:
    listed = git("ls-tree", "-r", "--name-only", base) or ""
    base_files = {p for p in listed.splitlines() if is_config(p)}
    cur_files = {
        str(p.relative_to(ROOT))
        for p in [ROOT / "peribolos.yaml", ROOT / "settings.yml", ROOT / "deployment-settings.yml"]
        + repo_files
        + sorted((ROOT / "org").glob("*.json"))
        if p.is_file()
    }
    for path in sorted(base_files | cur_files):
        old = parse(path, git("show", f"{base}:{path}") or "") if path in base_files else None
        new = parse(path, (ROOT / path).read_text()) if path in cur_files else None
        changes = []
        diff(old if old is not None else {}, new if new is not None else {}, "", changes)
        if changes:
            plan[path] = changes

lines = []
if base is None:
    lines.append("Planned mutations: skipped (no base revision; pass --base)")
else:
    total = sum(len(c) for c in plan.values())
    lines.append(f"Planned mutations vs {base[:12]}: {total}")
    for path, changes in plan.items():
        lines.append(f"  {path}")
        lines.extend(f"    {c}" for c in changes)
print("\n".join(lines))

summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    md = ["## Governance lint", ""]
    if errors:
        md += ["**Lint failures:**", ""] + [f"- {e}" for e in errors] + [""]
    else:
        md += ["Lint ok; ruleset payloads carry all API-required fields.", ""]
    if base is None:
        md.append("Planned mutations: skipped (base revision unavailable).")
    elif not plan:
        md.append(f"**Planned mutations vs `{base[:12]}`: none.**")
    else:
        md += [f"**Planned mutations vs `{base[:12]}`** (assumes the base has zero drift):", ""]
        for path, changes in plan.items():
            md += [f"### `{path}`", "", "```diff"] + changes + ["```", ""]
    with open(summary, "a") as f:
        f.write("\n".join(md) + "\n")

if errors:
    print("lint failures:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print("lint ok")
