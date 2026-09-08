from __future__ import annotations

import pytest

from src.models import ConnectorState, FetchResult, RawPosting


class _FakeContext:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


class _FakePage:
    def __init__(self): self.context = _FakeContext()
    async def goto(self, *a, **k): ...
    async def wait_for_selector(self, *a, **k): ...
    async def content(self): return ""
    async def close(self): ...


class _FakeBrowser:
    def __init__(self): self.pages = 0
    async def new_page(self):
        self.pages += 1
        return _FakePage()


class _HeadlessConn:
    tier = "headless"
    supports_enrich = False
    name = "avature:acme"
    def __init__(self): self.got_page = None
    async def fetch(self, page, state):
        self.got_page = page          # proves it received a page, not an httpx client
        return FetchResult(postings=[RawPosting(
            source="avature:acme", external_id="1", title="Staff Engineer",
            description="", apply_url="https://x/1", location="Remote", company="Acme")])


@pytest.mark.asyncio
async def test_fetch_one_routes_headless_to_page():
    from src.orchestrator import _fetch_one
    conn = _HeadlessConn()
    browser = _FakeBrowser()
    name, res = await _fetch_one(conn, client=None, state=ConnectorState(), browser=browser)
    assert name == "avature:acme"
    assert isinstance(conn.got_page, _FakePage)      # got a page
    assert browser.pages == 1                          # a page was created for it
    assert res.postings[0].title == "Staff Engineer"
