from src.tailor.endpoint.run import run_tailor
from src.tailor.endpoint.jd import PostingJD


class FakeStorage:
    def __init__(self):
        self.blobs = {}

    def exists(self, key):
        return key in self.blobs

    def put(self, key, data, content_type):
        self.blobs[key] = data


def test_run_tailor_cached_short_circuits():
    storage = FakeStorage()
    storage.blobs["j1.pdf"] = b"x"
    out = run_tailor(job_id="j1", regen=False, engine=None, content=None,
                     jd_reader=lambda _: PostingJD("body", "T", "C"), storage=storage)
    assert out == {"pdf_key": "j1.pdf", "cached": True}


def test_run_tailor_missing_jd_returns_error():
    out = run_tailor(job_id="j1", regen=False, engine=None, content=None,
                     jd_reader=lambda _: None, storage=FakeStorage())
    assert "error" in out
