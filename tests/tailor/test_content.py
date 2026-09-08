import json
import pytest

from src.tailor.content import load_content


_GOOD = {
    "name": "Jane Example",
    "contact": {"email": "x@y.z"},
    "skills": [{"name": "C#/.NET", "category": "language", "tags": ["primary"]}],
    "experiences": [
        {"id": "exp-acme", "company": "Acme", "role": "SSE", "start": "2021-03", "end": "present",
         "bullets": [{"id": "exp-acme-b1", "text": "did X", "tags": ["backend"],
                      "metric_bearing": True, "evidence_refs": ["JIRA-1"]}]},
    ],
    "projects": [],
}


def _write(tmp_path, obj):
    p = tmp_path / "content.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_content_happy(tmp_path):
    c = load_content(_write(tmp_path, _GOOD))
    assert c.name == "Jane Example"
    assert c.experiences[0].bullets[0].id == "exp-acme-b1"
    assert c.bullet_ids() == {"exp-acme-b1"}


def test_load_content_duplicate_bullet_id_fails_loud(tmp_path):
    bad = json.loads(json.dumps(_GOOD))
    bad["experiences"][0]["bullets"].append(
        {"id": "exp-acme-b1", "text": "dup"})
    with pytest.raises(ValueError, match="duplicate bullet id"):
        load_content(_write(tmp_path, bad))


def test_load_content_missing_bullet_id_fails_loud(tmp_path):
    bad = json.loads(json.dumps(_GOOD))
    del bad["experiences"][0]["bullets"][0]["id"]
    with pytest.raises(ValueError, match="bullet missing 'id'"):
        load_content(_write(tmp_path, bad))


def test_load_content_missing_top_key_fails_loud(tmp_path):
    bad = json.loads(json.dumps(_GOOD))
    del bad["experiences"]
    with pytest.raises(ValueError, match="missing required key 'experiences'"):
        load_content(_write(tmp_path, bad))


def test_load_content_projects_with_bullets(tmp_path):
    obj = json.loads(json.dumps(_GOOD))
    obj["projects"] = [{"id": "proj-rc", "name": "Foo", "subtitle": "Go + React",
                        "dates": "2024 – Present",
                        "bullets": [{"id": "proj-rc-b1", "text": "Four Go microservices"}]}]
    c = load_content(_write(tmp_path, obj))
    assert c.projects[0].subtitle == "Go + React"
    assert c.projects[0].bullets[0].id == "proj-rc-b1"


def test_load_content_education_and_volunteer(tmp_path):
    obj = json.loads(json.dumps(_GOOD))
    obj["education"] = [{"degree": "B.S. Computer Science", "institution": "State University", "dates": "2016 – 2020"}]
    obj["volunteer"] = [{"role": "Mentor", "org": "Local Code Club", "dates": "2022 – Present"}]
    c = load_content(_write(tmp_path, obj))
    assert c.education[0].institution == "State University"
    assert c.volunteer[0].org == "Local Code Club"


def test_load_content_bullet_id_unique_across_experience_and_project(tmp_path):
    obj = json.loads(json.dumps(_GOOD))  # has experience bullet exp-acme-b1
    obj["projects"] = [{"id": "proj-x", "name": "X",
                        "bullets": [{"id": "exp-acme-b1", "text": "collides"}]}]
    with pytest.raises(ValueError, match="duplicate bullet id"):
        load_content(_write(tmp_path, obj))
