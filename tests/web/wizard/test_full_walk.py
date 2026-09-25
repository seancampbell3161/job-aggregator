"""The whole wizard, driven start to finish the way a browser drives it.

Every other test in this directory exercises one step in isolation. This one
walks POST /setup/wizard -> ... -> /wizard/done, twice: once with an LLM
connected and a stubbed draft, and once with no LLM at all. It scrapes each
rendered page's own form and posts exactly what that form would post, so a
field the route needs but the template never renders (or vice versa) fails
here rather than in production."""
from __future__ import annotations

from html.parser import HTMLParser

from src.resume_intake.draft import Draft
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service

RESUME = (
    "Jane Smith - Platform Engineer\n"
    "Ten years building Python services on AWS and Kubernetes. "
    "Led the migration of a monolith to services at Acme Corp, ran the on-call "
    "rotation, and mentored four engineers. Postgres, Terraform, Go.\n"
) * 3

DRAFT = Draft(
    profile_md="## Quick summary\nPlatform engineer, ten years.",
    filters={"filters.titles": ["platform engineer"], "filters.max_age_days": 3},
)


class _FormScraper(HTMLParser):
    """The named controls of the <form> whose action matches, as a browser
    would submit them: repeated names collect, unchecked boxes are omitted,
    a <select> contributes its selected option (or its first)."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.fields: dict[str, list[str]] = {}
        self._in = False
        self._depth = 0
        self._select: str | None = None
        self._options: list[tuple[str, bool]] = []
        self._textarea: str | None = None

    def _add(self, name: str, value: str) -> None:
        self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if (a.get("action") or "") == self.action:
                self._in, self._depth = True, 1
            elif self._in:
                self._depth += 1
            return
        if not self._in:
            return
        if tag == "input":
            name, kind = a.get("name"), (a.get("type") or "text").lower()
            if not name or kind in ("submit", "button", "file") or "disabled" in a:
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self._add(name, a.get("value") or ("on" if kind == "checkbox" else ""))
        elif tag == "select":
            self._select, self._options = a.get("name"), []
        elif tag == "option" and self._select:
            self._options.append((a.get("value") or "", "selected" in a))
        elif tag == "textarea":
            self._textarea = a.get("name")
            if self._textarea:
                self._add(self._textarea, "")

    def handle_data(self, data):
        if self._in and self._textarea:
            self.fields[self._textarea][-1] += data

    def handle_endtag(self, tag):
        if tag == "form" and self._in:
            self._depth -= 1
            if self._depth == 0:
                self._in = False
        elif tag == "select" and self._select:
            chosen = next((v for v, sel in self._options if sel), None)
            if chosen is None and self._options:
                chosen = self._options[0][0]
            if chosen:
                self._add(self._select, chosen)
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def form_fields(html: str, action: str) -> dict[str, list[str]]:
    p = _FormScraper(action)
    p.feed(html)
    assert p.fields, f"no form with action={action!r} in the rendered page"
    return p.fields


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(None))


def _where(client) -> str:
    return client.get("/wizard", follow_redirects=False).headers["location"]


def _stub_preview(monkeypatch):
    async def fake_run_preview(app):
        app.state.stores.wizard.put(
            "preview", {"status": "ok", "fetched": 12, "matched": 1, "matches": [
                {"title": "Platform Engineer", "company": "Acme",
                 "location": "Remote (US)", "apply_url": "https://acme.test/1",
                 "score": 8}]},
        )
    monkeypatch.setattr("src.web.wizard.preview.run_preview", fake_run_preview)


def _walk_resume(client):
    page = client.get("/wizard/resume").text
    fields = form_fields(page, "/wizard/resume")
    fields["pasted"] = [RESUME]
    fields["target_titles"] = ["platform engineer", "infrastructure engineer"]
    fields["seniority"] = ["senior", "staff"]
    fields["ic_or_management"] = ["ic"]
    fields["countries"] = ["US"]
    fields["cities"] = []
    fields["remote_policy"] = ["anywhere"]
    fields["employment_types"] = ["full_time"]
    fields["comp_floor"] = ["180000"]
    r = client.post("/wizard/resume", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text[:2000]
    assert r.headers["location"] == "/wizard"


def _walk_review(client):
    page = client.get("/wizard/review").text
    fields = form_fields(page, "/wizard/review")
    assert "profile" in fields, "the review page renders no profile textarea"
    fields["profile"] = ["## Quick summary\nPlatform engineer, ten years."]
    fields["filters.titles"] = ["platform engineer"]
    fields["filters.max_age_days"] = ["3"]
    r = client.post("/wizard/review", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text[:3000]
    # ?attempted=review lets /wizard show step_warnings() if the save left
    # review incomplete; here it doesn't, so the flag is simply dropped on
    # the next hop (asserted via _where in the callers below).
    assert r.headers["location"] == "/wizard?attempted=review"


def _walk_notifications(client):
    page = client.get("/wizard/notifications").text
    fields = form_fields(page, "/wizard/notifications")
    assert "secret.ntfy_topic_url" in fields
    fields["secret.ntfy_topic_url"] = ["https://ntfy.sh/job-alerts-walkthrough"]
    r = client.post("/wizard/notifications", data=fields, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard?attempted=notifications"


def _walk_preview(client):
    assert client.get("/wizard/preview").status_code == 200
    r = client.post("/wizard/preview/start", follow_redirects=False)
    assert r.status_code == 303


def test_the_whole_wizard_completes_with_an_llm(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)

    async def fake_draft(cfg, *, resume_text, answers):
        assert resume_text.startswith("Jane Smith")
        return DRAFT

    monkeypatch.setattr("src.web.wizard.routes.draft_profile_and_filters", fake_draft)
    _stub_preview(monkeypatch)

    assert client.get("/setup").status_code == 200
    r = client.post("/setup/wizard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/wizard"

    assert _where(client) == "/wizard/llm"
    page = client.get("/wizard/llm").text
    fields = form_fields(page, "/wizard/llm")
    fields["relevance.enabled"] = ["on"]
    fields["relevance.provider"] = ["anthropic"]
    fields["relevance.model"] = ["claude-sonnet-4-5"]
    fields["secret.anthropic_api_key"] = ["sk-ant-walkthrough"]
    r = client.post("/wizard/llm", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text[:3000]

    assert _where(client) == "/wizard/resume"
    _walk_resume(client)

    assert _where(client) == "/wizard/review"
    _walk_review(client)

    assert _where(client) == "/wizard/companies"
    # Nothing in the wizard writes a board; the page's own escape hatches are
    # the full companies page and unticking both boxes below (Continue posts
    # /wizard/companies either way — Task 5 folds save-or-skip into one route).
    client.post("/wizard/companies", data={})

    assert _where(client) == "/wizard/notifications"
    _walk_notifications(client)

    assert _where(client) == "/wizard/preview"
    _walk_preview(client)

    assert _where(client) == "/wizard/done"
    assert client.get("/wizard/done").status_code == 200

    snap = app.state.service.snapshot()
    assert snap.cfg.filters.titles == ["platform engineer"]
    assert snap.cfg.filters.max_age_days == 3
    assert snap.documents.profile.startswith("## Quick summary")
    assert snap.documents.resume_text.startswith("Jane Smith")
    assert app.state.service.secret_source("ntfy_topic_url") == "stored"
    sources = {v.source for v in app.state.service.versions(None)}
    assert "wizard" in sources and "llm_draft" in sources


def test_the_whole_wizard_completes_with_no_llm_at_all(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    _stub_preview(monkeypatch)

    client.post("/setup/wizard")
    assert _where(client) == "/wizard/llm"
    client.post("/wizard/llm/skip")

    assert _where(client) == "/wizard/resume"
    _walk_resume(client)

    assert _where(client) == "/wizard/review"
    # No binding can be built, so drafting fails visibly and the page degrades
    # to hand-filled forms pre-filled from the interview answers.
    page = client.get("/wizard/review").text
    assert "platform engineer" in page
    _walk_review(client)

    assert _where(client) == "/wizard/companies"
    client.post("/wizard/companies", data={})
    assert _where(client) == "/wizard/notifications"
    _walk_notifications(client)
    assert _where(client) == "/wizard/preview"
    _walk_preview(client)
    assert _where(client) == "/wizard/done"

    snap = app.state.service.snapshot()
    assert snap.cfg.filters.titles == ["platform engineer"]
    assert snap.documents.profile
    assert {v.source for v in app.state.service.versions(None)} == {"wizard"}
