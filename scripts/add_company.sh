#!/usr/bin/env bash
#
# Append a company slug to the named ATS list in config.yaml.
# Usage: scripts/add_company.sh greenhouse stripe
#
set -euo pipefail

ATS="${1:?usage: $0 <ats> <slug>}"
SLUG="${2:?usage: $0 <ats> <slug>}"
CFG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config.yaml"

python3.12 - "$ATS" "$SLUG" "$CFG" <<'PY'
import sys
import yaml
ats, slug, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    cfg = yaml.safe_load(f)
existing = cfg["sources"].setdefault(ats, [])
if slug in existing:
    print(f"{ats}:{slug} already in config")
    sys.exit(0)
existing.append(slug)
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print(f"Added {ats}:{slug}")
PY
