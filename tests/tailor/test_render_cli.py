from unittest.mock import AsyncMock, MagicMock

import src.tailor.__main__ as cli
import src.tailor.render as render_mod
from src.tailor.models import FitAnalysis, TailorResult
from src.tailor.render import RenderResult


def _result():
    return TailorResult(fit=FitAnalysis(), experiences=[], skills_ordered=[],
                        summary_placeholder="[S]", cover_letter="cl", is_fallback=False)


def _fake_render_with_fallback(content, result, *, pack, settings):
    # Reflects the resolved pack back in `template` so tests can assert on it,
    # mirroring what a real render (or fallback) would report.
    return RenderResult(b"%PDF-fake", [], template=pack.slug)


def _setup(monkeypatch, tmp_path, *, pdf=True):
    jd = tmp_path / "jd.txt"; jd.write_text("role")
    fake_engine = MagicMock(); fake_engine.tailor = AsyncMock(return_value=_result())
    monkeypatch.setattr(cli, "load_config", lambda: MagicMock())
    monkeypatch.setattr(cli, "build_tailor_engine", lambda cfg: fake_engine)
    monkeypatch.setattr(cli, "load_content", lambda path: MagicMock())
    if pdf:
        # render_with_fallback is imported locally (lazy, keeps WeasyPrint out of
        # CLI startup) so it must be patched at its source, not on `cli`.
        monkeypatch.setattr(render_mod, "render_with_fallback", _fake_render_with_fallback)
    monkeypatch.chdir(tmp_path)
    return jd


def test_cli_writes_resume_pdf(tmp_path, monkeypatch):
    jd = _setup(monkeypatch, tmp_path)
    rc = cli.main(["--jd", str(jd), "--job-id", "acme-1"])
    assert rc == 0
    assert (tmp_path / "tailored" / "acme-1" / "resume.pdf").read_bytes() == b"%PDF-fake"


def test_cli_no_pdf_flag_skips_render(tmp_path, monkeypatch):
    jd = _setup(monkeypatch, tmp_path)
    cli.main(["--jd", str(jd), "--job-id", "acme-1", "--no-pdf"])
    assert not (tmp_path / "tailored" / "acme-1" / "resume.pdf").exists()
    assert (tmp_path / "tailored" / "acme-1" / "content.json").exists()  # JSON still written


def test_cli_template_flag_selects_pack(tmp_path, monkeypatch, capsys):
    jd = _setup(monkeypatch, tmp_path)
    rc = cli.main(["--jd", str(jd), "--job-id", "j1", "--template", "headless"])
    assert rc == 0
    # the run prints which template rendered
    captured = capsys.readouterr().out
    assert "headless" in captured
    assert (tmp_path / "tailored" / "j1" / "resume.pdf").read_bytes() == b"%PDF-fake"


def test_cli_settings_default_without_db(tmp_path, monkeypatch):
    """A store-backed settings lookup failure (e.g. an unusable SQLite path)
    must fall back to defaults, not just get swallowed by the outer PDF
    try/except — the render should still complete via _builder_settings()'s
    own fail-soft, not silently abort the whole PDF step."""
    jd = _setup(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # a file where connect()'s makedirs expects a directory
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(blocker / "settings.db"))

    rc = cli.main(["--jd", str(jd), "--job-id", "j2"])

    assert rc == 0  # settings failure must never break the CLI
    assert (tmp_path / "tailored" / "j2" / "resume.pdf").read_bytes() == b"%PDF-fake"
