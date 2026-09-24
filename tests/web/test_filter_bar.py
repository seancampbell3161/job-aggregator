import re

from tests.web.shell_helpers import client_for, make_app


def _form(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/").text
    return re.search(r'<form id="filters".*?</form>', html, re.S).group(0)


def test_filter_groups_are_labelled(tmp_path, monkeypatch):
    form = _form(tmp_path, monkeypatch)
    assert "<legend>Status</legend>" in form and "<legend>Workplace</legend>" in form
    assert 'placeholder="Search title or company…"' in form
    assert "Only with skill gaps" in form
    assert '<option value="score">Best match</option>' in form


def test_status_and_workplace_are_toggle_pills_with_same_values(tmp_path, monkeypatch):
    form = _form(tmp_path, monkeypatch)
    for v in ("new", "interested", "applied", "interviewing"):
        assert f'<label class="toggle"><input type="checkbox" name="status" value="{v}" checked>' in form
    assert '<label class="toggle"><input type="checkbox" name="status" value="dismissed">' in form
    assert 'value="onsite" checked><span>On-site</span>' in form


def test_min_score_is_a_select_whose_any_submits_empty(tmp_path, monkeypatch):
    form = _form(tmp_path, monkeypatch)
    assert '<select name="min_score">' in form
    assert '<option value="">Any</option>' in form
    for n in (5, 6, 7, 8):
        assert f'<option value="{n}">{n}+</option>' in form
    assert 'type="number"' not in form


def test_min_score_values_still_filter(tmp_path, monkeypatch):
    c = client_for(make_app(tmp_path, monkeypatch))
    assert c.get("/jobs", params={"min_score": ""}).status_code == 200
    assert c.get("/jobs", params={"min_score": "7"}).status_code == 200
