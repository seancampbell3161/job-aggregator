import json
import logging

from src.logging_setup import configure_logging


def test_configure_logging_emits_json(capsys):
    configure_logging()
    log = logging.getLogger("test")
    log.info("fetch_done", extra={"source": "greenhouse:stripe", "duration_ms": 42})
    err = capsys.readouterr().err
    line = err.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["message"] == "fetch_done"
    assert parsed["source"] == "greenhouse:stripe"
    assert parsed["duration_ms"] == 42
    assert parsed["level"] == "INFO"
