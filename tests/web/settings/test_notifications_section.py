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


def test_a_bad_timezone_is_an_inline_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/notifications", data={
        **ON, "quiet_hours.timezone": "Mars/Olympus"})
    assert "quiet_hours.timezone" in r.text
    assert app.state.service.snapshot().cfg.quiet_hours is None


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
