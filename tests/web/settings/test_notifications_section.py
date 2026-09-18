"""Notification sinks, the optional quiet-hours group, and its Test buttons."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


ON = {
    "quiet_hours__enabled": "on",
    "quiet_hours.timezone": "America/Los_Angeles",
    "quiet_hours.start": "22:00",
    "quiet_hours.end": "07:00",
}


def test_enabling_quiet_hours_saves_the_group(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/notifications", data=ON)
    assert r.status_code == 200
    qh = app.state.service.snapshot().cfg.quiet_hours
    assert qh is not None
    assert str(qh.start) == "22:00:00"


def test_disabling_quiet_hours_removes_the_whole_group(tmp_path, monkeypatch):
    service = make_service({
        **WEB_TEST_SETTINGS,
        "quiet_hours": {"timezone": "America/Los_Angeles", "start": "22:00", "end": "07:00"},
    })
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post("/settings/notifications", data={
        k: v for k, v in ON.items() if k != "quiet_hours__enabled"})
    assert app.state.service.snapshot().cfg.quiet_hours is None
    _, doc = app.state.service.current_doc()
    assert "quiet_hours" not in doc


def test_a_partial_quiet_hours_is_an_inline_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/notifications", data={
        "quiet_hours__enabled": "on", "quiet_hours.timezone": "",
        "quiet_hours.start": "22:00", "quiet_hours.end": "07:00"})
    assert r.status_code == 200
    assert "quiet_hours.timezone" in r.text
    assert app.state.service.snapshot().cfg.quiet_hours is None


def _quiet_hours_toggle_checked(html: str) -> bool:
    """Whether the rendered "Enable quiet hours" checkbox came back checked —
    inspects the actual <input> tag's attributes rather than searching the
    whole page for the word "checked"."""
    start = html.index('name="quiet_hours__enabled"')
    end = html.index(">", start)
    return "checked" in html[start:end]


def test_a_bad_timezone_is_an_inline_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/notifications", data={
        **ON, "quiet_hours.timezone": "Mars/Olympus"})
    assert "quiet_hours.timezone" in r.text
    assert app.state.service.snapshot().cfg.quiet_hours is None
    # Regression: the toggle used to read cfg.quiet_hours directly instead of
    # echoing the submission, so a first-time enable that failed validation
    # re-rendered the checkbox unchecked even though the user had just ticked
    # it and the times/timezone around it echoed correctly.
    assert _quiet_hours_toggle_checked(r.text)


def test_fixing_a_bad_timezone_after_a_first_time_enable_still_saves_the_window(
    tmp_path, monkeypatch,
):
    """End-to-end regression for the bug: step 1 submits a bad timezone (an
    inline error); step 2 resubmits the form exactly AS A BROWSER WOULD
    RENDER IT — the toggle checkbox is included only if step 1's response
    rendered it checked. Before the fix, step 1 rendered it unchecked, so a
    real browser's step-2 submission would send no
    ``quiet_hours__enabled`` at all, decode() would read the group as
    disabled, and the whole quiet-hours window would be silently discarded
    even though the save appeared to succeed."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)

    r1 = client.post("/settings/notifications", data={
        **ON, "quiet_hours.timezone": "Mars/Olympus"})
    assert r1.status_code == 200
    assert app.state.service.snapshot().cfg.quiet_hours is None

    step2 = {**ON, "quiet_hours.timezone": "America/Los_Angeles"}
    if not _quiet_hours_toggle_checked(r1.text):
        step2 = {k: v for k, v in step2.items() if k != "quiet_hours__enabled"}

    r2 = client.post("/settings/notifications", data=step2)
    assert r2.status_code == 200
    assert "Saved" in r2.text

    qh = app.state.service.snapshot().cfg.quiet_hours
    assert qh is not None
    assert str(qh.start) == "22:00:00"
    assert str(qh.timezone) == "America/Los_Angeles"


def test_a_stored_sink_url_never_leaks_even_after_a_failed_save(tmp_path, monkeypatch):
    """A validation failure re-renders the page with the submitted values —
    make sure that path can't accidentally echo back a secret that was never
    part of the submission."""
    service = make_service(WEB_TEST_SETTINGS, secrets={"ntfy_topic_url": "https://ntfy.sh/super-secret"})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post("/settings/notifications", data={
        **ON, "quiet_hours.timezone": "Mars/Olympus"})
    assert r.status_code == 200
    assert app.state.service.snapshot().cfg.quiet_hours is None  # the save did fail
    assert "https://ntfy.sh/super-secret" not in r.text


def test_sink_secrets_save(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post("/settings/notifications", data={
        **ON, "secret.ntfy_topic_url": "https://ntfy.sh/mine"})
    assert app.state.service.secret_source("ntfy_topic_url") == "stored"


def test_ntfy_test_button_probes_the_submitted_url(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen = {}

    async def fake(url):
        from src.web.settings.probes import ProbeResult
        seen["url"] = url
        return ProbeResult(True, "Sent — check your phone.")

    monkeypatch.setattr("src.web.settings.routes.probe_ntfy", fake)
    r = signed_in_client(app).post(
        "/settings/notifications/test/ntfy",
        data={"secret.ntfy_topic_url": "https://ntfy.sh/typed"})
    assert r.status_code == 200
    assert seen["url"] == "https://ntfy.sh/typed"
    assert "check your phone" in r.text
    assert app.state.service.secret_source("ntfy_topic_url") == "unset"  # not saved


def test_test_button_falls_back_to_the_stored_secret(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"ntfy_topic_url": "https://ntfy.sh/stored"})
    app = _app(tmp_path, monkeypatch, service)
    seen = {}

    async def fake(url):
        from src.web.settings.probes import ProbeResult
        seen["url"] = url
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.settings.routes.probe_ntfy", fake)
    signed_in_client(app).post("/settings/notifications/test/ntfy",
                               data={"secret.ntfy_topic_url": ""})
    assert seen["url"] == "https://ntfy.sh/stored"


def test_unknown_probe_is_404(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/notifications/test/nope", data={})
    assert r.status_code == 404


def test_a_malformed_bracket_url_reports_instead_of_500ing(tmp_path, monkeypatch):
    # urlparse raises ValueError("Invalid IPv6 URL") on an unbalanced
    # bracket; the Test button must report a ProbeResult, never crash the
    # request — driven through the route, not just probes._check_url.
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for bad_url in ("https://[", "http://[::1", "https://[abc]x:1]"):
        r = client.post("/settings/notifications/test/ntfy",
                        data={"secret.ntfy_topic_url": bad_url})
        assert r.status_code == 200, bad_url
        assert "does not look like a url" in r.text.lower()


def test_a_bad_stored_url_reports_instead_of_500ing_on_the_fallback_path(
    tmp_path, monkeypatch,
):
    """A malformed value saved earlier is picked up via the stored-secret
    fallback (the field renders blank, so a blank Test submission falls
    back to the stored value) — that path must not crash either."""
    service = make_service(WEB_TEST_SETTINGS, secrets={"ntfy_topic_url": "https://["})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post(
        "/settings/notifications/test/ntfy", data={"secret.ntfy_topic_url": ""})
    assert r.status_code == 200
    assert "does not look like a url" in r.text.lower()


def test_saving_an_untouched_quiet_hours_form_twice_writes_one_version(tmp_path, monkeypatch):
    """Regression: value_at() used to render a stored time as "HH:MM:SS"
    while <input type="time"> submits "HH:MM" — they never compared equal,
    so save_section's changed-fields filter kept re-writing quiet_hours on
    every resubmission of an untouched form, bumping the settings version
    each time."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)

    r1 = client.post("/settings/notifications", data=ON)
    assert r1.status_code == 200
    version_after_first = app.state.service.current_config()[0]

    r2 = client.post("/settings/notifications", data=ON)
    assert r2.status_code == 200
    version_after_second = app.state.service.current_config()[0]

    assert version_after_second == version_after_first
