#!/usr/bin/env python3
"""Lint the governance config against the repository conventions
(AGENTS.md). Exits 1 with a list of violations."""

import json
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

# --- inference gateway: valid inputs, committed config.yaml == rendered output ---
sys.path.insert(0, str(ROOT / "inference-gateway"))
sys.dont_write_bytecode = True
import render  # noqa: E402  # pyright: ignore[reportMissingImports]

try:
    rendered = render.render()
except render.INPUT_ERRORS as e:
    err(f"inference-gateway: {e}")
else:
    for e in render.check_config(rendered):
        err(f"inference-gateway: {e}")
    gw_config = ROOT / "inference-gateway" / "config.yaml"
    if not gw_config.exists() or gw_config.read_text() != rendered:
        err("inference-gateway/config.yaml is stale: run python3 inference-gateway/render.py")

if errors:
    print("lint failures:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print("lint ok")
