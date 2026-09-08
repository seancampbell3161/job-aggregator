"""AWS Lambda Function URL handler for the hosted tailor endpoint.

Two routes by query param: no `run` -> instant loading page; `run=1` -> the
~30s tailor+render returning JSON. The engine/config/content are built once at
cold start (`_bootstrap`) and reused across warm invocations."""

from __future__ import annotations

import json
import logging
import os

from src.tailor.endpoint.auth import verify_token
from src.tailor.endpoint.jd import read_jd
from src.tailor.endpoint.page import error_page, loading_page
from src.tailor.endpoint.run import run_tailor

log = logging.getLogger(__name__)

_BOOT: tuple | None = None  # (cfg, engine, content), built once at cold start


def _bootstrap() -> tuple:
    global _BOOT
    if _BOOT is None:
        from src.config import load_config
        from src.tailor import build_tailor_engine
        from src.tailor.content import load_content
        cfg = load_config(os.environ.get("JOB_AGG_CONFIG_PATH", "config.yaml"))
        engine = build_tailor_engine(cfg)
        content = load_content(cfg.tailoring.content_path)
        _BOOT = (cfg, engine, content)
    return _BOOT


def _html(body: str) -> dict:
    return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": body}


def _json(body: dict) -> dict:
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event: dict, context) -> dict:
    qs = event.get("queryStringParameters") or {}
    job_id = qs.get("job_id", "")
    token = qs.get("t", "")
    secret = os.environ["JOB_AGG_TAILOR_SIGNING_SECRET"]
    table = os.environ.get("JOB_AGG_SEEN_JOBS_TABLE", "seen_jobs")

    if not job_id or not verify_token(token, job_id, secret):
        return _html(error_page("This link has expired or is invalid."))

    if qs.get("run") != "1":
        jd = read_jd(table, job_id)
        return _html(loading_page(job_id, token, jd.title if jd else "", jd.company if jd else ""))

    cfg, engine, content = _bootstrap()
    import boto3
    from src.tailor.endpoint.storage import S3Storage
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    storage = S3Storage(s3, os.environ["JOB_AGG_TAILOR_BUCKET"])
    out = run_tailor(
        job_id=job_id, regen=qs.get("regen") == "1", engine=engine, content=content,
        jd_reader=lambda jid: read_jd(table, jid), storage=storage,
    )
    return _json(out)
