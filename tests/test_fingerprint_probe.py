"""probe_target: one pasted careers URL or domain -> a FingerprintResult."""
import httpx
import pytest

from src.fingerprint import normalize_target, probe_target


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_normalize_strips_scheme_and_path_for_a_bare_domain():
    assert normalize_target("https://www.acme.com/careers/") == ("url", "https://www.acme.com/careers/")
    assert normalize_target("acme.com") == ("domain", "acme.com")
    assert normalize_target("https://acme.com") == ("domain", "acme.com")
    assert normalize_target("  WWW.Acme.com  ") == ("domain", "acme.com")


def test_a_blank_value_is_rejected():
    with pytest.raises(ValueError):
        normalize_target("   ")


def test_a_non_http_scheme_is_rejected():
    # A scheme this app will never fetch (e.g. mailto:, ftp://) is a form
    # error, not a probe outcome -- it must raise, same as a blank value.
    with pytest.raises(ValueError):
        normalize_target("ftp://acme.com/careers")


def test_a_value_with_no_dot_is_rejected():
    # No "." anywhere means it can't be a domain or a host -- reject inline
    # rather than let it become a nonsense "domain" kind.
    with pytest.raises(ValueError):
        normalize_target("localhost")


@pytest.mark.asyncio
async def test_a_direct_ats_url_skips_location_and_verifies(monkeypatch):
    """boards.greenhouse.io/acme is already an identity: no careers-page
    discovery should happen, only the verify probe."""
    async def never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("locate_careers_urls should not be called")

    monkeypatch.setattr("src.fingerprint.locate_careers_urls", never)

    async def fake_verify(client, family, identity):
        assert family == "greenhouse" and identity == {"slug": "acme"}
        return 12

    monkeypatch.setattr("src.fingerprint.verify_identity", fake_verify)
    async with _client(lambda r: httpx.Response(200)) as client:
        result = await probe_target(client, "https://boards.greenhouse.io/acme")
    assert result.status == "matched"
    assert result.family == "greenhouse"
    assert result.posting_count == 12
    assert result.name == "acme"


@pytest.mark.asyncio
async def test_a_direct_url_that_verifies_empty_is_not_a_match(monkeypatch):
    async def fake_verify(client, family, identity):
        return 0

    monkeypatch.setattr("src.fingerprint.verify_identity", fake_verify)
    async with _client(lambda r: httpx.Response(200)) as client:
        result = await probe_target(client, "https://jobs.lever.co/beta")
    assert result.status == "not_found"
    assert result.family == "lever"
    assert "verified empty" in (result.note or "")


@pytest.mark.asyncio
async def test_an_unsupported_ats_url_is_reported_not_guessed(monkeypatch):
    # avature.net is a real, already-detected "unsupported" family (see
    # tests/test_fingerprint.py's detect_unsupported coverage) -- unlike
    # bamboohr.com, which isn't in _UNSUPPORTED_HOST_SUFFIXES at all and
    # would silently fall through to the bare-domain sweep path instead.
    async def never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("locate_careers_urls should not be called")

    monkeypatch.setattr("src.fingerprint.locate_careers_urls", never)

    async with _client(lambda r: httpx.Response(200)) as client:
        result = await probe_target(client, "https://acme.avature.net/careers")
    assert result.status == "unsupported"
    assert result.family == "avature"
    assert result.identity is None


@pytest.mark.asyncio
async def test_a_bare_domain_goes_through_fingerprint_company(monkeypatch):
    seen = {}

    async def fake_company(client, seed):
        seen["seed"] = seed
        from src.fingerprint import FingerprintResult
        return FingerprintResult(name=seed.name, domain=seed.domain, status="not_found")

    monkeypatch.setattr("src.fingerprint.fingerprint_company", fake_company)
    async with _client(lambda r: httpx.Response(200)) as client:
        result = await probe_target(client, "acme.com", name="Acme Inc")
    assert seen["seed"].domain == "acme.com"
    assert seen["seed"].name == "Acme Inc"
    assert result.status == "not_found"


@pytest.mark.asyncio
async def test_a_network_failure_becomes_an_error_result(monkeypatch):
    async def boom(client, family, identity):
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr("src.fingerprint.verify_identity", boom)
    async with _client(lambda r: httpx.Response(200)) as client:
        result = await probe_target(client, "https://boards.greenhouse.io/acme")
    assert result.status == "error"
    assert "ConnectTimeout" in (result.note or "")
