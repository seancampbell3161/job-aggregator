import os

import pytest


def _weasyprint_unavailable() -> str | None:
    """Why WeasyPrint can't be used here, or None if it can.

    NOT ``pytest.importorskip``: WeasyPrint imports fine as a Python package
    but raises **OSError** from cffi when its native libraries (pango, cairo,
    gobject) are absent — the usual case on a fresh macOS checkout. OSError is
    not an ImportError, so importorskip lets it through and the whole module
    errors during collection, which aborts the entire test session. Catching
    Exception here is deliberate."""
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — any failure means "unusable"
        first = (str(exc) or type(exc).__name__).strip().splitlines()[0]
        return first[:120]
    return None


WEASYPRINT_UNAVAILABLE = _weasyprint_unavailable()

requires_weasyprint = pytest.mark.skipif(
    WEASYPRINT_UNAVAILABLE is not None,
    reason=f"needs WeasyPrint + native libs ({WEASYPRINT_UNAVAILABLE})",
)


@pytest.fixture(autouse=True)
def _aws_creds(monkeypatch):
    """moto needs creds set even though they're not used."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _sqlite_path_isolated(monkeypatch, tmp_path):
    """Never let a test open ./data/job_aggregator.db in the checkout.

    ``src.sqlite_db`` defaults to that path when ``JOB_AGG_SQLITE_PATH`` is
    unset, and docker-compose bind-mounts ./data — so an unguarded test would
    open (and migrate) a live database. Point it at a per-test temp file
    instead. Tests that set the variable themselves still win: their
    ``monkeypatch.setenv`` runs after this autouse fixture.
    """
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "test.db"))


@pytest.fixture(params=["dynamodb", "sqlite"])
def seen_store(request, tmp_path):
    """A ready SeenJobs store for each backend. DynamoDB via moto; SQLite via a
    temp file. Lets one behavioral test prove both backends agree."""
    if request.param == "dynamodb":
        import boto3
        from moto import mock_aws
        from src.state import SeenJobsStore
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.create_table(
                TableName="seen_jobs",
                KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            yield SeenJobsStore(table_name="seen_jobs", region="us-east-1")
    else:
        from src.sqlite_db import connect
        from src.state_sqlite import SqliteSeenJobsStore
        yield SqliteSeenJobsStore(connect(str(tmp_path / "t.db")))
