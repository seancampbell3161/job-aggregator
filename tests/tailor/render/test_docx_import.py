import io
import zipfile
from types import SimpleNamespace

import pytest

from src.llm.providers import LlmBinding
from src.tailor.render.docx_import import (
    DocxTemplateImporter, build_docx_importer, build_import_prompt, extract_docx,
)
from tests.conftest import requires_weasyprint

_TEMPLATE = "<html><body>{{ doc.name }}</body></html>"


def make_docx(*, with_font=True, font_entry="word/fonts/Play-regular.ttf") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>First Name Last Name</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Work History</w:t></w:r></w:p></w:body></w:document>')
        z.writestr("word/styles.xml",
            '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:rPr><w:rFonts w:ascii="Play"/></w:rPr></w:styles>')
        if with_font:
            z.writestr(font_entry, b"\x00\x01FAKEFONT")
    return buf.getvalue()


@requires_weasyprint
def test_extract_docx_html_fonts_hints():
    ex = extract_docx(make_docx())
    assert "First Name Last Name" in ex.html
    assert ex.fonts == [("Play-regular.ttf", b"\x00\x01FAKEFONT")]
    assert "Play" in ex.hints["font_names"]


@requires_weasyprint
def test_extract_docx_no_fonts_ok():
    assert extract_docx(make_docx(with_font=False)).fonts == []


@requires_weasyprint
def test_extract_docx_font_path_traversal_defused():
    """A malicious .docx can name a font member with directory-traversal
    segments (e.g. crafted by hand-editing the zip). extract_docx must keep
    only the basename (Path(name).name) so nothing downstream can be tricked
    into writing outside the pack's fonts/ dir."""
    ex = extract_docx(make_docx(font_entry="word/fonts/../../../../evil.ttf"))
    assert ex.fonts == [("evil.ttf", b"\x00\x01FAKEFONT")]


@requires_weasyprint
def test_prompt_includes_contract_html_and_fonts():
    system, user = build_import_prompt(extract_docx(make_docx()))
    assert "doc.experiences" in system     # contract present, in the system turn
    assert "First Name Last Name" in user  # source html present, in the user turn
    assert "Play-regular.ttf" in user      # font wiring instructions, in the user turn


def test_the_prompt_puts_the_rules_in_the_system_turn_and_the_document_in_the_user_turn():
    """The source document comes out of a file the user uploaded. It belongs
    in the user turn, not spliced into the instructions."""
    ex = type("E", (), {"html": "<p>Acme</p>", "fonts": [], "hints": {}})()
    system, user = build_import_prompt(ex)
    assert "doc.experiences" in system        # the RenderDoc contract
    assert "Output ONLY the complete HTML file" in system
    assert "<p>Acme</p>" in user
    assert "<p>Acme</p>" not in system


class _FakeClient:
    def __init__(self, text):
        self._text = text
    async def chat(self, **kwargs):
        return {"message": {"content": self._text}}


GOOD = "```html\n<!DOCTYPE html><html><body>{{ doc.name }}</body></html>\n```"


def _ollama_binding(text: str, *, timeout_seconds: int = 5) -> LlmBinding:
    return LlmBinding(provider="ollama", model="m", client=_FakeClient(text),
                      timeout_seconds=timeout_seconds)


@requires_weasyprint
async def test_importer_strips_fences_and_returns_template():
    imp = DocxTemplateImporter(binding=_ollama_binding(GOOD))
    text, ex = await imp.to_template(make_docx())
    assert text.startswith("<!DOCTYPE html>")
    assert "{{ doc.name }}" in text
    assert ex.fonts


@requires_weasyprint
async def test_importer_rejects_output_without_doc_refs():
    imp = DocxTemplateImporter(binding=_ollama_binding("<html>static</html>"))
    with pytest.raises(RuntimeError):
        await imp.to_template(make_docx())


async def test_the_importer_runs_on_anthropic(monkeypatch):
    class _FakeAnthropic:
        def __init__(self):
            self.messages, self.kwargs = self, None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            block = type("B", (), {"type": "text", "text": _TEMPLATE})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn"})()

    client = _FakeAnthropic()
    importer = DocxTemplateImporter(
        binding=LlmBinding(provider="anthropic", model="m", client=client,
                           timeout_seconds=120),
    )
    monkeypatch.setattr("src.tailor.render.docx_import.extract_docx",
                        lambda data: type("E", (), {"html": "<p>x</p>", "fonts": [],
                                                    "hints": {}})())
    text, _ = await importer.to_template(b"fake docx bytes")
    assert text == _TEMPLATE
    assert client.kwargs["max_tokens"] == 16384


def _cfg(*, tailoring_timeout: int) -> SimpleNamespace:
    return SimpleNamespace(
        tailoring=SimpleNamespace(provider=None, model=None, timeout_seconds=tailoring_timeout),
        relevance=SimpleNamespace(provider="ollama", model="gpt-oss:120b",
                                  ollama_host="https://ollama.com", ollama_is_local=False),
        secrets=SimpleNamespace(ollama_api_key="key123"),
    )


def test_build_docx_importer_floors_short_tailoring_timeout():
    """Template generation is a longer LLM call than a normal tailoring run
    — a tight tailoring.timeout_seconds (e.g. the 60s default) must not
    starve it; the importer's timeout floors at 120s."""
    imp = build_docx_importer(_cfg(tailoring_timeout=60))
    assert imp is not None
    assert imp._binding.timeout_seconds == 120


def test_build_docx_importer_respects_longer_tailoring_timeout():
    imp = build_docx_importer(_cfg(tailoring_timeout=180))
    assert imp is not None
    assert imp._binding.timeout_seconds == 180


def test_build_docx_importer_uses_a_local_host_without_a_key(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host=None, headers=None):
            captured.update(host=host, headers=headers)

    monkeypatch.setattr("ollama.AsyncClient", FakeClient)
    cfg = SimpleNamespace(
        tailoring=SimpleNamespace(provider=None, model=None, timeout_seconds=60),
        relevance=SimpleNamespace(provider="ollama", model="m",
                                  ollama_host="http://ollama:11434", ollama_is_local=True),
        secrets=SimpleNamespace(ollama_api_key=""),
    )
    assert build_docx_importer(cfg) is not None
    assert captured == {"host": "http://ollama:11434", "headers": {}}


def test_build_docx_importer_cloud_host_requires_a_key():
    cfg = _cfg(tailoring_timeout=60)
    cfg.secrets.ollama_api_key = ""
    assert build_docx_importer(cfg) is None


def test_the_factory_builds_for_a_non_ollama_provider():
    from src.config import AppConfig, RelevanceConfig, Secrets, TailoringConfig
    from src.tailor.render.docx_import import build_docx_importer

    cfg = AppConfig(
        tailoring=TailoringConfig(enabled=True, provider="anthropic"),
        relevance=RelevanceConfig(provider="ollama", model="m",
                                  ollama_host="https://ollama.com"),
        secrets=Secrets(anthropic_api_key="sk-test"),
    )
    assert build_docx_importer(cfg) is not None
