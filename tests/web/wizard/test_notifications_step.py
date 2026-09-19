"""Generating an ntfy topic, showing its QR, and sending a real test."""
import re

from src.web.app import create_app
from src.web.settings.probes import ProbeResult
from src.web.wizard.ntfy_topic import suggest_topic, topic_qr_svg
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_suggested_topic_is_a_plausible_ntfy_url():
    url = suggest_topic()
    assert url.startswith("https://ntfy.sh/")
    assert re.fullmatch(r"[a-z0-9-]{16,}", url.rsplit("/", 1)[-1])


def test_suggested_topics_are_unguessable_and_unique():
    """The topic IS the credential on ntfy.sh — anyone who knows it can read
    the alerts."""
    assert len({suggest_topic() for _ in range(50)}) == 50


def test_suggest_topic_draws_from_secrets_token_urlsafe(monkeypatch):
    """Uniqueness alone doesn't prove unguessability — a mutant that swaps the
    CSPRNG source for something predictable (a counter, time.time_ns(),
    uuid4()'s node bits, ...) would still pass the two tests above as long as
    it happens not to collide across 50 draws. Pin the actual source and its
    entropy budget: suggest_topic must draw from secrets.token_urlsafe with
    at least 128 bits (16 bytes), and the value it returns must actually be
    derived from what that call produced."""
    calls = []

    def fake_token_urlsafe(nbytes):
        calls.append(nbytes)
        return "Fixed_Token-Value12345678901"

    monkeypatch.setattr("src.web.wizard.ntfy_topic.secrets.token_urlsafe", fake_token_urlsafe)
    url = suggest_topic()
    assert calls, "suggest_topic() must call secrets.token_urlsafe"
    assert calls[0] >= 16
    # The slug is truncated to 24 chars, so check a prefix well inside that.
    assert "fixed-token-value" in url


def test_qr_is_an_svg_carrying_nothing_but_the_url():
    svg = topic_qr_svg("https://ntfy.sh/abc")
    assert svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert "<svg" in svg


def test_qr_svg_embeds_no_remote_reference():
    """The whole point of rendering inline is that the page never asks a third
    party to draw the QR: a remote <image href="https://..."> inside the SVG
    would hand the topic to that server just as surely as an <img src=...>
    pointed at a QR-image API would. segno renders pure vector paths, so the
    topic itself should never appear as literal text (it's drawn as pixels),
    and nothing should carry an href a browser could fetch. (The SVG
    namespace declaration legitimately contains "http://www.w3.org/2000/svg"
    — that's not a fetch target — so this checks for the https:// topic
    itself and for any href, not for "http" as a raw substring.)"""
    svg = topic_qr_svg("https://ntfy.sh/abc-topic-credential")
    assert "https://" not in svg
    assert "href" not in svg


def test_step_offers_a_generated_topic(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/notifications")
    assert r.status_code == 200
    assert "https://ntfy.sh/" in r.text
    assert "<svg" in r.text


def test_suggested_topic_used_consistently_for_input_and_qr(tmp_path, monkeypatch):
    """suggest_topic() must be called exactly once per GET, and its result must
    be what both the pre-filled input and the QR encode — not two independent
    calls that could (rarely, but observably) disagree."""
    calls = []

    def fake_suggest():
        calls.append(1)
        return "https://ntfy.sh/job-alerts-fixed-for-test"

    monkeypatch.setattr("src.web.wizard.routes.suggest_topic", fake_suggest)
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/notifications")
    assert r.status_code == 200
    assert len(calls) == 1
    assert "https://ntfy.sh/job-alerts-fixed-for-test" in r.text


def test_suggested_topic_is_stable_across_two_gets(tmp_path, monkeypatch):
    """Two GETs of /wizard/notifications, both before anything is saved, must
    show the SAME suggested topic. A user can scan the QR with their phone
    and then have the page re-render for any unrelated reason (a second tab,
    a retry, htmx settling) before ever pressing Save — a fresh suggestion on
    that second render would silently point future alerts at a topic the
    phone was never subscribed to, and ntfy.sh has no way to notice or warn
    about that: the topic itself IS the credential.

    suggest_topic() is stubbed to hand out two DISTINCT, recognisable values
    in a row, so this proves the second GET reuses the cached suggestion
    rather than merely getting lucky — with the real CSPRNG-backed
    suggest_topic(), two independent draws practically never collide, so an
    un-stubbed version of this test would rarely fail even on the broken
    (regenerate-every-GET) behaviour."""
    topics = iter(["https://ntfy.sh/job-alerts-first-draw", "https://ntfy.sh/job-alerts-second-draw"])
    monkeypatch.setattr("src.web.wizard.routes.suggest_topic", lambda: next(topics))
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)

    first = client.get("/wizard/notifications").text
    second = client.get("/wizard/notifications").text

    assert "job-alerts-first-draw" in first
    assert "job-alerts-first-draw" in second
    assert "job-alerts-second-draw" not in second


def test_stored_topic_is_not_regenerated_on_next_get(tmp_path, monkeypatch):
    """Once a topic is saved, a later GET must not call suggest_topic() again —
    doing so would silently swap in a topic the user never subscribed their
    phone to. Proven by making a regeneration blow up the request rather than
    by scraping rendered text for a "new" value, which a lucky mutant could
    dodge."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/wizard/notifications", data={"secret.ntfy_topic_url": "https://ntfy.sh/mine"})

    def boom():
        raise AssertionError("suggest_topic() must not be called once a topic is stored")

    monkeypatch.setattr("src.web.wizard.routes.suggest_topic", boom)
    r = client.get("/wizard/notifications")
    assert r.status_code == 200
    assert "stored" in r.text.lower()


def test_saving_neither_sink_leaves_the_step_incomplete_and_explains_why(tmp_path, monkeypatch):
    """Same class of bug as the LLM and review steps: a "Save and continue"
    that submits neither secret leaves the step incomplete (no_sink), and
    used to silently re-serve this page with no explanation. Skip llm,
    resume, review and companies first so /wizard actually lands back on
    notifications, matching what ?attempted=notifications requires."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review", "companies"):
        client.post(f"/wizard/{slug}/skip")
    r = client.post("/wizard/notifications", data={})
    assert r.status_code == 200
    assert str(r.url).endswith("/wizard/notifications?attempted=1")
    assert "never delivered anywhere" in r.text.lower()


def test_a_first_unattempted_visit_shows_no_no_sink_warning(tmp_path, monkeypatch):
    """Keep it honest: nothing posted yet, so no warning on a plain first GET
    even though the instance has no sink configured."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/wizard/notifications")
    assert r.status_code == 200
    assert "never delivered anywhere" not in r.text.lower()


def test_saving_stores_the_topic_secret(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/wizard/notifications",
        data={"secret.ntfy_topic_url": "https://ntfy.sh/mine"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert app.state.service.effective_secret("ntfy_topic_url") == "https://ntfy.sh/mine"


def test_test_button_sends_a_real_notification(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def fake(url):
        return ProbeResult(True, "Sent — check your phone.")

    monkeypatch.setattr("src.web.wizard.routes.probe_ntfy", fake)
    r = signed_in_client(app).post(
        "/wizard/notifications/test/ntfy",
        data={"secret.ntfy_topic_url": "https://ntfy.sh/mine"},
    )
    assert "check your phone" in r.text


def test_test_button_falls_back_to_the_stored_secret(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"ntfy_topic_url": "https://ntfy.sh/stored"})
    app = _app(tmp_path, monkeypatch, service)
    seen = {}

    async def fake(url):
        seen["url"] = url
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.wizard.routes.probe_ntfy", fake)
    signed_in_client(app).post(
        "/wizard/notifications/test/ntfy", data={"secret.ntfy_topic_url": ""}
    )
    assert seen["url"] == "https://ntfy.sh/stored"


def test_test_button_honours_a_pending_clear(tmp_path, monkeypatch):
    """Ticking clear then testing must not probe with the topic Save will
    delete — the same trap fixed for the LLM step's Test button in Task 7.
    Reachable here because a stored topic renders through secret_field, which
    offers a clear checkbox."""
    app = _app(tmp_path, monkeypatch)
    app.state.service.set_secret("ntfy_topic_url", "https://ntfy.sh/stored")
    called = []

    async def fake(url):
        called.append(url)
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.wizard.routes.probe_ntfy", fake)
    r = signed_in_client(app).post(
        "/wizard/notifications/test/ntfy",
        data={"secret.ntfy_topic_url": "", "clear.ntfy_topic_url": "on"},
    )
    assert not called, "must not probe the stored topic once clear is ticked"
    assert "nothing to test" in r.text.lower()


def test_unknown_probe_is_404(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).post("/wizard/notifications/test/nope", data={})
    assert r.status_code == 404
