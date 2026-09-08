from src.tailor.endpoint.auth import sign_token, verify_token

SECRET = "test-secret"


def test_round_trip_valid():
    tok = sign_token("greenhouse:stripe:1", exp=2_000_000_000, secret=SECRET)
    assert verify_token(tok, "greenhouse:stripe:1", SECRET, now=1_000_000_000) is True


def test_expired_rejected():
    tok = sign_token("j", exp=1_000, secret=SECRET)
    assert verify_token(tok, "j", SECRET, now=2_000) is False


def test_tampered_signature_rejected():
    tok = sign_token("j", exp=2_000_000_000, secret=SECRET)
    bad = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    assert verify_token(bad, "j", SECRET, now=1) is False


def test_wrong_job_id_rejected():
    tok = sign_token("j", exp=2_000_000_000, secret=SECRET)
    assert verify_token(tok, "other", SECRET, now=1) is False


def test_malformed_token_rejected():
    assert verify_token("garbage", "j", SECRET, now=1) is False
    assert verify_token("", "j", SECRET, now=1) is False
    assert verify_token("notanint.sig", "j", SECRET, now=1) is False


def test_build_tailor_url_round_trips_and_encodes():
    from urllib.parse import urlparse, parse_qs
    from src.tailor.endpoint.auth import build_tailor_url, verify_token
    url = build_tailor_url("greenhouse:stripe:1", endpoint_url="https://ep.example/tailor",
                           secret=SECRET, ttl_days=30, now=1_000_000_000)
    assert url.startswith("https://ep.example/tailor?")
    assert "greenhouse%3Astripe%3A1" in url          # ':' percent-encoded
    qs = parse_qs(urlparse(url).query)
    assert qs["job_id"] == ["greenhouse:stripe:1"]   # decodes back
    assert verify_token(qs["t"][0], "greenhouse:stripe:1", SECRET, now=1_000_000_001) is True
