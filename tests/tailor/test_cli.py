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


def test_cli_returns_1_when_not_set_up(tmp_path, monkeypatch, capsys):
    jd = tmp_path / "jd.txt"
    jd.write_text("x")
    monkeypatch.setattr(cli, "_snapshot", lambda: None)
    assert cli.main(["--jd", str(jd), "--job-id", "x"]) == 1
    assert "not set up" in capsys.readouterr().err
