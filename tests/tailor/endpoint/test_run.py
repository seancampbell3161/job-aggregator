from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import src.tailor.render as render_mod
from src.tailor.endpoint.jd import PostingJD
from src.tailor.endpoint.run import run_tailor
from src.tailor.models import FitAnalysis, TailorResult
from src.tailor.render import RenderResult

CONTENT = MagicMock()


def _result():
    return TailorResult(fit=FitAnalysis(matches=["Go"], gaps=["Kafka"], overall="ok"),
                        experiences=[], skills_ordered=[], summary_placeholder="[S]",
                        cover_letter="Dear team", projects=[])


class FakeStorage:
    def __init__(self, exists=False, put_raises=None):
        self._exists = exists
        self._put_raises = put_raises
        self.put_calls = []
        self._blobs = {}

    def exists(self, key):
        return self._exists

    def put(self, key, data, content_type):
        if self._put_raises:
            raise self._put_raises
        self.put_calls.append((key, data, content_type))
        self._blobs[key] = data

    def get(self, key):
        return self._blobs.get(key)


def test_run_tailors_and_uploads(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF-x", trimmed=[]))
    engine = MagicMock(); engine.tailor = AsyncMock(return_value=_result())
    storage = FakeStorage(exists=False)
    out = run_tailor(job_id="j1", regen=False, engine=engine, content=MagicMock(),
                     jd_reader=lambda jid: PostingJD("jd text", "SWE", "Acme"), storage=storage)
    assert len(storage.put_calls) == 2         # render input (for re-render) + the PDF
    assert storage.get("j1.result.json") is not None
    assert out["pdf_key"] == "j1.pdf"
    assert out["cover_letter"] == "Dear team"
    assert out["fit"]["gaps"] == ["Kafka"]
    assert out["cached"] is False


def test_run_uses_cache_when_object_exists(monkeypatch):
    engine = MagicMock(); engine.tailor = AsyncMock(return_value=_result())
    storage = FakeStorage(exists=True)
    out = run_tailor(job_id="j1", regen=False, engine=engine, content=MagicMock(),
                     jd_reader=lambda jid: PostingJD("jd", "t", "c"), storage=storage)
    engine.tailor.assert_not_called()       # cache hit -> no LLM call
    assert len(storage.put_calls) == 0
    assert out["cached"] is True and out["pdf_key"] == "j1.pdf"


def test_run_missing_jd_returns_error():
    out = run_tailor(job_id="j1", regen=False, engine=MagicMock(), content=MagicMock(),
                     jd_reader=lambda jid: None, storage=FakeStorage())
    assert "error" in out


def test_run_fallback_when_engine_none(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF-x", trimmed=[]))
    out = run_tailor(job_id="j1", regen=False, engine=None, content=MagicMock(),
                     jd_reader=lambda jid: PostingJD("jd", "t", "c"), storage=FakeStorage())
    assert out["pdf_key"] == "j1.pdf" and out["cached"] is False   # static fallback still renders


def test_run_returns_error_dict_on_render_failure(monkeypatch):
    def boom(content, result, **kw):
        raise RuntimeError("render boom")
    monkeypatch.setattr(render_mod, "render_resume", boom)
    engine = MagicMock(); engine.tailor = AsyncMock(return_value=_result())
    out = run_tailor(job_id="j1", regen=False, engine=engine, content=MagicMock(),
                     jd_reader=lambda jid: PostingJD("jd", "t", "c"), storage=FakeStorage(exists=False))
    assert "error" in out          # did NOT raise
    assert "pdf_key" not in out


def test_run_returns_error_dict_on_put_failure(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF", trimmed=[]))
    engine = MagicMock(); engine.tailor = AsyncMock(return_value=_result())
    storage = FakeStorage(exists=False, put_raises=Exception("storage down"))
    out = run_tailor(job_id="j1", regen=False, engine=engine, content=MagicMock(),
                     jd_reader=lambda jid: PostingJD("jd", "t", "c"), storage=storage)
    assert "error" in out          # did NOT raise


def test_run_persists_render_input_and_reports_template(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF-x", trimmed=[]))
    storage = FakeStorage(exists=False)
    out = run_tailor(job_id="j1", regen=False, engine=None, content=CONTENT,
                     jd_reader=lambda j: PostingJD("desc", "T", "C"), storage=storage)
    assert storage.get("j1.result.json") is not None
    assert out["template"] == "classic"
    assert out["fit_warning"] is None


def test_template_rerender_uses_stored_result_without_engine(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF-x", trimmed=[]))
    storage = FakeStorage(exists=False)
    run_tailor(job_id="j1", regen=False, engine=None, content=CONTENT,
               jd_reader=lambda j: PostingJD("desc", "T", "C"), storage=storage)
    calls = []
    def renderer(content, result):
        calls.append(result)
        return SimpleNamespace(pdf=b"%PDF-fake", trimmed=[], fit_warning=None, template="headless")
    out = run_tailor(job_id="j1", regen=False, engine=None, content=CONTENT,
                     jd_reader=lambda j: PostingJD("desc", "T", "C"),
                     storage=storage, renderer=renderer, template="headless")
    assert out["template"] == "headless"
    assert out["pdf_key"] == "j1.headless.pdf"
    assert storage.get("j1.headless.pdf") == b"%PDF-fake"
    assert len(calls) == 1


def test_template_rerender_with_no_renderer_uses_requested_template(monkeypatch):
    monkeypatch.setattr(render_mod, "render_resume",
                        lambda content, result, **kw: RenderResult(pdf=b"%PDF-x", trimmed=[]))
    storage = FakeStorage(exists=False)
    run_tailor(job_id="j1", regen=False, engine=None, content=CONTENT,
               jd_reader=lambda j: PostingJD("desc", "T", "C"), storage=storage)

    captured = {}
    def fake(content, result, **kwargs):
        captured["pack"] = kwargs["pack"]
        return SimpleNamespace(pdf=b"%PDF-x", trimmed=[], fit_warning=None,
                               template=kwargs["pack"].slug)
    monkeypatch.setattr("src.tailor.render.render_resume", fake)

    out = run_tailor(job_id="j1", regen=False, engine=None, content=CONTENT,
                     jd_reader=lambda j: PostingJD("desc", "T", "C"),
                     storage=storage, renderer=None, template="headless")
    assert captured["pack"].slug == "headless"
    assert out["template"] == "headless"


def test_template_rerender_without_stored_result_errors():
    storage = FakeStorage(exists=False)
    out = run_tailor(job_id="never-ran", regen=False, engine=None, content=CONTENT,
                     jd_reader=lambda j: None, storage=storage,
                     renderer=lambda c, r: None, template="headless")
    assert "error" in out
