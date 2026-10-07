# tests/test_sqlite_connect.py
from src.sqlite_db import connect


def test_connect_sets_30s_busy_timeout():
    # Writers must outlast the hourly full integrity_check on a large DB.
    assert connect(":memory:").execute("PRAGMA busy_timeout").fetchone()[0] == 30000
