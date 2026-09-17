from src.tailor.endpoint.storage import LocalFileStorage


def test_local_file_storage_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path), base_url="/tailored")
    assert s.exists("j1.pdf") is False
    s.put("j1.pdf", b"%PDF-1.4", "application/pdf")
    assert s.exists("j1.pdf") is True
    assert (tmp_path / "j1.pdf").read_bytes() == b"%PDF-1.4"
    assert s.url("j1.pdf", 900) == "/tailored/j1.pdf"


def test_local_get_roundtrip(tmp_path):
    s = LocalFileStorage(root=str(tmp_path))
    assert s.get("nope.json") is None
    s.put("a.json", b'{"x":1}', "application/json")
    assert s.get("a.json") == b'{"x":1}'
