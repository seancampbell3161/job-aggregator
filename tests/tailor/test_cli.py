import json
from unittest.mock import AsyncMock, MagicMock

import src.tailor.__main__ as cli
from src.tailor.models import FitAnalysis, TailorResult, TailoredBullet, TailoredExperience


def _result():
    return TailorResult(
        fit=FitAnalysis(matches=["Go"], gaps=["Kafka"], overall="solid"),
        experiences=[TailoredExperience(experience_id="exp-1",
                     bullets=[TailoredBullet(source_bullet_id="exp-1-b1", text="rewritten", evidence_refs=["JIRA-1"])])],
        skills_ordered=["Go"], summary_placeholder="[SUMMARY]", cover_letter="Dear team",
    )


def test_cli_writes_three_files(tmp_path, monkeypatch):
    jd = tmp_path / "jd.txt"
    jd.write_text("Go backend role")
    fake_engine = MagicMock()
    fake_engine.tailor = AsyncMock(return_value=_result())
    monkeypatch.setattr(cli, "_snapshot", lambda: MagicMock())
    monkeypatch.setattr(cli, "build_tailor_engine", lambda cfg, content, evidence: fake_engine)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["--jd", str(jd), "--job-id", "acme-1"])

    assert rc == 0
    out = tmp_path / "tailored" / "acme-1"
    render = json.loads((out / "content.json").read_text())
    assert render["experiences"][0]["bullets"][0]["text"] == "rewritten"
    # lock the full render-input contract sub-project B consumes
    assert render["skills_ordered"] == ["Go"]
    assert render["summary_placeholder"] == "[SUMMARY]"
    assert (out / "cover_letter.md").read_text() == "Dear team"
    assert "Kafka" in (out / "fit.md").read_text()


def test_cli_returns_1_when_engine_unavailable(tmp_path, monkeypatch):
    jd = tmp_path / "jd.txt"
    jd.write_text("x")
    monkeypatch.setattr(cli, "_snapshot", lambda: MagicMock())
    monkeypatch.setattr(cli, "build_tailor_engine", lambda cfg, content, evidence: None)
    assert cli.main(["--jd", str(jd), "--job-id", "x"]) == 1


def test_the_unavailable_message_names_the_reasons_that_are_actually_real(
        tmp_path, monkeypatch, capsys):
    """This is the CLI's only tailoring error path, and it made three claims
    this sub-project falsified: that the missing key is an Ollama one (the
    engine runs on any configured provider now, src/tailor/engine.py), that
    an evidence document is required (an absent bank degrades to an empty
    one, src/tailor/__init__.py), and — by naming them together —
    "resume_content/evidence" as if either would do. Only content is
    required.

    Asserted positively on the reasons that are real rather than only on the
    absence of "Ollama": a message trimmed down to nothing would satisfy the
    negative on its own."""
    jd = tmp_path / "jd.txt"
    jd.write_text("x")
    monkeypatch.setattr(cli, "_snapshot", lambda: MagicMock())
    monkeypatch.setattr(cli, "build_tailor_engine", lambda cfg, content, evidence: None)
    assert cli.main(["--jd", str(jd), "--job-id", "x"]) == 1
    err = capsys.readouterr().err
    assert "Ollama" not in err
    assert "no API key" in err
    assert "no resume_content document" in err
    assert "An evidence bank is optional." in err


def test_cli_returns_1_when_not_set_up(tmp_path, monkeypatch, capsys):
    jd = tmp_path / "jd.txt"
    jd.write_text("x")
    monkeypatch.setattr(cli, "_snapshot", lambda: None)
    assert cli.main(["--jd", str(jd), "--job-id", "x"]) == 1
    assert "not set up" in capsys.readouterr().err
