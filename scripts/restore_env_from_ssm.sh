#!/bin/bash
# Restore alert/scoring secrets from AWS SSM into .env — values are never
# printed. Backup written to .env.bak-2026-07-02 (if not already present).
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=us-east-1
get() {
  aws ssm get-parameter --region "$REGION" --with-decryption \
    --name "/job-aggregator/$1" --query Parameter.Value --output text
}

export _NTFY="$(get ntfy_topic_url)"
export _DISCORD="$(get discord_webhook_url)"
export _OLLAMA_KEY="$(get ollama_api_key)"
export _SIGNING="$(get tailor_signing_secret)"

cp -n .env .env.bak-2026-07-02 2>/dev/null || true

python3 <<'PY'
import os, re

updates = {
    "JOB_AGG_NTFY_TOPIC_URL": os.environ["_NTFY"],
    "JOB_AGG_DISCORD_WEBHOOK_URL": os.environ["_DISCORD"],
    "JOB_AGG_OLLAMA_HOST": "https://ollama.com",
    "JOB_AGG_OLLAMA_API_KEY": os.environ["_OLLAMA_KEY"],
    "JOB_AGG_OPS_NTFY_TOPIC_URL": os.environ["_NTFY"],
    "JOB_AGG_TAILOR_SIGNING_SECRET": os.environ["_SIGNING"],
}
lines = open(".env").read().splitlines()
seen = set()
for i, line in enumerate(lines):
    m = re.match(r"^([A-Z_]+)=", line)
    if m and m.group(1) in updates:
        lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
        seen.add(m.group(1))
missing = [k for k in updates if k not in seen]
if missing:
    lines += ["", "# --- restored from AWS SSM 2026-07-02 ---"]
    lines += [f"{k}={updates[k]}" for k in missing]
open(".env", "w").write("\n".join(lines) + "\n")
print("updated in place:", ", ".join(sorted(seen)) or "(none)")
print("appended:", ", ".join(sorted(missing)) or "(none)")
PY
echo "OK — .env updated; values not shown. Backup: .env.bak-2026-07-02"
