#!/usr/bin/env python3
"""Smoke-test the résumé-tailoring deep-link loop on the local Docker stack.

Seeds a synthetic notified job (with a job description) into seen_jobs, mints a
signed /tailor deep-link, and prints both a phone (whatever JOB_AGG_TAILOR_ENDPOINT_URL
points at) and an on-box (localhost) URL. Open either: loading page -> tailored
one-page PDF. Append &regen=1 to force a fresh render instead of the cached copy.

Resolves the signing secret and endpoint URL the way the web app does — a
non-empty JOB_AGG_* value (from .env or the environment) wins over the
settings database — and never prints the secret.

Usage:
    python scripts/tailor_smoke.py            # seed a fake job + print the link
    python scripts/tailor_smoke.py --clean    # delete the test row + cached PDF
    python scripts/tailor_smoke.py --job-id test:my-check
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_JOB_ID = "test:tailor-smoke"
DB_PATH = "data/job_aggregator.db"          # host path; container sees it via the ./data mount
TAILORED_DIR = "data/tailored"              # host path for the cached PDFs
TTL_DAYS = 30
SAMPLE_JD = (
    "Senior Frontend Engineer at Acme Robotics. React + TypeScript SPA, design system, "
    "real-time WebSocket dashboards, REST/GraphQL APIs, performance + Core Web Vitals, "
    "Jest/Playwright CI. 5+ yrs React/TS. Bonus: Next.js, Node, Docker, dataviz."
)


def load_env(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def clean(job_id: str) -> None:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    n = conn.execute("DELETE FROM seen_jobs WHERE job_id = ?", (job_id,)).rowcount
    pdf = Path(TAILORED_DIR) / f"{job_id}.pdf"
    removed_pdf = pdf.exists()
    pdf.unlink(missing_ok=True)
    print(f"deleted {n} row(s); cached PDF removed: {removed_pdf}")


def _signing_config(db_path: str = DB_PATH, env_file: str = ".env") -> tuple[str, str]:
    """(signing secret, endpoint URL) resolved like the web app resolves them."""
    from src.settings.service import ConfigService
    from src.settings.store import SqliteSettingsStore
    from src.sqlite_db import connect

    env = dict(os.environ)
    if Path(env_file).is_file():
        env.update(load_env(env_file))
    snap = ConfigService(SqliteSettingsStore(connect(db_path)), env=env).snapshot()
    if snap is None:
        raise SystemExit("not set up — import settings first: python -m src.settings import DIR")
    secrets = snap.cfg.secrets
    if not secrets.tailor_signing_secret:
        raise SystemExit("tailor_signing_secret is empty — start the web app once (it generates "
                         "one) or run: python -m src.settings set-secret tailor_signing_secret")
    if not secrets.tailor_endpoint_url:
        raise SystemExit("tailor_endpoint_url is not set — run: "
                         "python -m src.settings set-secret tailor_endpoint_url")
    return secrets.tailor_signing_secret, secrets.tailor_endpoint_url


def seed_and_mint(job_id: str) -> None:
    secret, endpoint = _signing_config()

    now = int(time.time())
    ttl = now + TTL_DAYS * 86400
    item = {
        "job_id": job_id, "first_seen": datetime.now(timezone.utc).isoformat(),
        "notified": True, "ttl": ttl, "score": 8, "title": "Senior Frontend Engineer",
        "company": "Acme Robotics", "location_text": "Remote (US)",
        "apply_url": "https://example.com/jobs/fe", "source": "test",
        "description_snapshot": SAMPLE_JD,
    }
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "INSERT OR REPLACE INTO seen_jobs (job_id, first_seen, notified, ttl, score, title, data) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, item["first_seen"], 1, ttl, 8, item["title"], json.dumps(item)),
    )

    exp = now + TTL_DAYS * 86400
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{job_id}|{exp}".encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    query = f"job_id={quote(job_id, safe='')}&t={exp}.{sig}"

    print(f"\nSeeded {job_id!r}. Open a link (loading page -> tailored PDF):\n")
    print(f"  phone / configured : {endpoint}?{query}")
    print(f"  this box (localhost): http://localhost:8000/tailor?{query}")
    print(f"\nForce a fresh render by appending &regen=1. Clean up with --clean.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job-id", default=DEFAULT_JOB_ID, help=f"test job id (default: {DEFAULT_JOB_ID})")
    ap.add_argument("--clean", action="store_true", help="delete the test row + its cached PDF, then exit")
    args = ap.parse_args()

    if args.clean:
        clean(args.job_id)
    else:
        seed_and_mint(args.job_id)


if __name__ == "__main__":
    main()
