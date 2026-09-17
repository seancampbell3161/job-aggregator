from src.tailor.endpoint.storage import LocalFileStorage, pdf_key


def test_local_file_storage_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path))
    assert s.exists("j1.pdf") is False
    s.put("j1.pdf", b"%PDF-1.4", "application/pdf")
    assert s.exists("j1.pdf") is True
    assert (tmp_path / "j1.pdf").read_bytes() == b"%PDF-1.4"
    assert not hasattr(s, "url")  # URLs are the web route's job (token-checked)


def test_local_get_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path))
    assert s.get("nope.json") is None
    s.put("a.json", b'{"x":1}', "application/json")
    assert s.get("a.json") == b'{"x":1}'


def test_pdf_key():
    assert pdf_key("greenhouse:acme:1") == "greenhouse:acme:1.pdf"
    assert pdf_key("j1", "headless") == "j1.headless.pdf"
