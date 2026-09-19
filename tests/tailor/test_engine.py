import json

from src.tailor.engine import parse_tailor_result
from src.tailor.models import (
    Bullet, Experience, ResumeContent, Skill,
    EvidenceBank, EvidenceProject, EvidenceMetric,
)


def _content():
    return ResumeContent(
        name="S", contact={}, skills=[Skill(name="Go", category="language")],
        experiences=[Experience(id="exp-1", company="Acme", role="SSE", start="", end="",
                                bullets=[Bullet(id="exp-1-b1", text="orig")])],
        projects=[],
    )


def _evidence():
    return EvidenceBank(projects=[EvidenceProject(
        key="acme", summary="s", metrics=[EvidenceMetric(claim="c", ticket_refs=["JIRA-1"])])])


def _good_json():
    return json.dumps({
        "fit": {"matches": ["Go"], "gaps": ["Kafka"], "overall": "solid"},
        "experiences": [{"experience_id": "exp-1", "bullets": [
            {"source_bullet_id": "exp-1-b1", "text": "rewritten", "evidence_refs": ["JIRA-1"]}]}],
        "skills_ordered": ["Go"],
        "summary_placeholder": "[SUMMARY]",
        "cover_letter": "Dear team",
    })


def test_parse_happy():
    r = parse_tailor_result(json.loads(_good_json()), content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is False
    assert r.fit.matches == ["Go"] and r.fit.gaps == ["Kafka"]
    assert r.experiences[0].bullets[0].text == "rewritten"
    assert r.experiences[0].bullets[0].evidence_refs == ["JIRA-1"]
    assert r.cover_letter == "Dear team"


def test_parse_drops_fabricated_bullet_blank_id():
    obj = json.loads(_good_json())
    obj["experiences"][0]["bullets"].append({"source_bullet_id": "", "text": "made up"})
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    texts = [b.text for e in r.experiences for b in e.bullets]
    assert "made up" not in texts and "rewritten" in texts


def test_parse_drops_bullet_with_unknown_source_id():
    obj = json.loads(_good_json())
    obj["experiences"][0]["bullets"][0]["source_bullet_id"] = "exp-1-bNOPE"
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    # fabricated id dropped; the completeness backstop restores the real bullet in original wording
    assert [b.text for b in r.experiences[0].bullets] == ["orig"]
    assert r.experiences[0].bullets[0].source_bullet_id == "exp-1-b1"


def test_parse_filters_unknown_evidence_refs():
    obj = json.loads(_good_json())
    obj["experiences"][0]["bullets"][0]["evidence_refs"] = ["JIRA-1", "JIRA-FAKE"]
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.experiences[0].bullets[0].evidence_refs == ["JIRA-1"]


def test_parse_none_is_fallback():
    """None is complete_json's sentinel for "the provider answered, but not
    with usable JSON" — the contract TailorEngine.tailor relies on."""
    r = parse_tailor_result(None, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_skips_non_dict_experience_and_bullet_items():
    import json
    obj = {
        "fit": {"matches": [], "gaps": [], "overall": ""},
        "experiences": [
            "garbage-not-a-dict",
            None,
            {"experience_id": "exp-1", "bullets": [
                "also-garbage",
                None,
                {"source_bullet_id": "exp-1-b1", "text": "rewritten", "evidence_refs": ["JIRA-1"]},
            ]},
        ],
        "skills_ordered": ["Go"],
        "summary_placeholder": "[SUMMARY]",
        "cover_letter": "Dear team",
    }
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    # did not raise; the one valid bullet survived, the garbage items were skipped
    assert r.is_fallback is False
    surviving = [b.text for e in r.experiences for b in e.bullets]
    assert surviving == ["rewritten"]


def test_parse_non_dict_fit_is_safe():
    import json
    obj = {
        "fit": "not-an-object",
        "experiences": [],
        "skills_ordered": [],
        "summary_placeholder": "[SUMMARY]",
        "cover_letter": "x",
    }
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is False
    assert r.fit.matches == [] and r.fit.gaps == [] and r.fit.overall == ""


import pytest

from src.llm.providers import LlmBinding
from src.tailor.engine import TAILOR_SCHEMA, TailorEngine


class _FakeOllamaClient:
    def __init__(self, text):
        self.text, self.kwargs = text, None

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        return {"message": {"content": self.text}}


def _engine(client, *, provider="ollama", timeout=60):
    return TailorEngine(
        binding=LlmBinding(provider=provider, model="m", client=client,
                           timeout_seconds=timeout),
        content=_content(), evidence=_evidence(),
    )


@pytest.mark.asyncio
async def test_engine_success_returns_result():
    r = await _engine(_FakeOllamaClient(_good_json())).tailor(job_id="j", jd_text="Go role")
    assert r.is_fallback is False
    assert r.experiences[0].bullets[0].text == "rewritten"


@pytest.mark.asyncio
async def test_engine_asks_for_the_full_output_budget():
    """16384, not the seam's 4096 default: rewrite-all returns every bullet
    plus a cover letter, and 8192 truncated mid-JSON (see _TAILOR_NUM_PREDICT)."""
    client = _FakeOllamaClient(_good_json())
    await _engine(client).tailor(job_id="j", jd_text="Go role")
    assert client.kwargs["options"]["num_predict"] == 16384


@pytest.mark.asyncio
async def test_engine_network_error_is_fallback_not_raise():
    """complete_json propagates provider errors on purpose. The engine must
    absorb them: probe_llm detects failure by checking is_fallback, and a
    raise here would break a poll cycle."""
    import httpx

    class _Boom:
        async def chat(self, **kwargs):
            raise httpx.ConnectError("dns")

    r = await _engine(_Boom()).tailor(job_id="j", jd_text="Go role")
    assert r.is_fallback is True


@pytest.mark.asyncio
async def test_engine_timeout_is_fallback(caplog):
    """Asserts the logged error_type, not just is_fallback: TailorResult carries
    no error_type of its own (unlike Score/Gaps/CoachResult), and is_fallback
    alone can't distinguish a real timeout from _Slow.chat() actually running
    to completion (~10s) and returning None — both end at fallback. Pinning
    error_type == "TimeoutError" is what catches asyncio.wait_for being
    dropped from the seam."""
    import asyncio

    class _Slow:
        async def chat(self, **kwargs):
            await asyncio.sleep(10)

    with caplog.at_level("WARNING", logger="src.tailor.engine"):
        r = await _engine(_Slow(), timeout=0).tailor(job_id="j", jd_text="x")
    assert r.is_fallback is True
    assert caplog.records[-1].error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_engine_runs_on_anthropic():
    """The whole point of the port: a Claude user gets tailoring."""
    class _FakeAnthropic:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            block = type("B", (), {"type": "tool_use", "input": json.loads(_good_json())})()
            return type("R", (), {"content": [block], "stop_reason": "tool_use"})()

    r = await _engine(_FakeAnthropic(), provider="anthropic").tailor(job_id="j", jd_text="Go")
    assert r.is_fallback is False
    assert r.experiences[0].bullets[0].text == "rewritten"


@pytest.mark.asyncio
async def test_engine_runs_on_gemini():
    class _FakeGemini:
        def __init__(self):
            self.aio = type("A", (), {"models": self})()

        async def generate_content(self, **kwargs):
            return type("R", (), {"text": _good_json()})()

    r = await _engine(_FakeGemini(), provider="gemini").tailor(job_id="j", jd_text="Go")
    assert r.is_fallback is False


@pytest.mark.asyncio
async def test_a_provider_rejection_reaches_the_user_facing_text():
    """Anthropic models carry their own max_tokens ceilings, some far below
    the engine's fixed 16384 (see _TAILOR_NUM_PREDICT) — a real, permanent
    failure mode now that non-Ollama providers reach the engine. The fallback
    cover letter must name it rather than advise a retry that cannot help."""
    class _Rejects:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            raise RuntimeError("max_tokens: 16384 > 8192")

    engine = TailorEngine(
        binding=LlmBinding(provider="anthropic", model="m", client=_Rejects(),
                           timeout_seconds=60),
        content=_content(), evidence=_evidence(),
    )
    result = await engine.tailor(job_id="j", jd_text="Go role")
    assert result.is_fallback is True
    assert "16384" in result.cover_letter


def test_the_schema_names_the_ids_the_grounding_guards_check():
    """The guards key on source_bullet_id, experience_id, and project_id. A
    schema that omitted any of them would let a forced-tool provider return
    entries/bullets the parser then drops as fabricated — a silently empty
    tailoring run."""
    exp = TAILOR_SCHEMA["properties"]["experiences"]["items"]
    assert "experience_id" in exp["properties"]
    assert "experience_id" in exp["required"]
    bullet = exp["properties"]["bullets"]["items"]
    assert "source_bullet_id" in bullet["properties"]
    assert "source_bullet_id" in bullet["required"]

    proj = TAILOR_SCHEMA["properties"]["projects"]["items"]
    assert "project_id" in proj["properties"]
    assert "project_id" in proj["required"]


def test_parse_non_list_experiences_is_fallback():
    import json
    obj = {"experiences": 123, "fit": {}, "skills_ordered": [],
           "summary_placeholder": "[S]", "cover_letter": "x"}
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_non_list_bullets_is_fallback():
    import json
    obj = {"experiences": [{"experience_id": "exp-1", "bullets": 5}],
           "fit": {}, "skills_ordered": [], "summary_placeholder": "[S]", "cover_letter": "x"}
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_non_string_source_bullet_id_is_fallback():
    import json
    obj = {"experiences": [{"experience_id": "exp-1",
            "bullets": [{"source_bullet_id": 123, "text": "x"}]}],
           "fit": {}, "skills_ordered": [], "summary_placeholder": "[S]", "cover_letter": "x"}
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_non_list_evidence_refs_is_fallback():
    import json
    obj = {"experiences": [{"experience_id": "exp-1", "bullets": [
              {"source_bullet_id": "exp-1-b1", "text": "x", "evidence_refs": 5}]}],
           "fit": {}, "skills_ordered": [], "summary_placeholder": "[S]", "cover_letter": "x"}
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_summary_is_always_the_marker_never_model_prose():
    import json
    from src.tailor.models import SUMMARY_PLACEHOLDER
    obj = json.loads(_good_json())
    obj["summary_placeholder"] = "I am a seasoned engineer with 10 years of impact..."
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert r.summary_placeholder == SUMMARY_PLACEHOLDER
    assert r.is_fallback is False


def _content_with_project():
    from src.tailor.models import Project
    return ResumeContent(
        name="S", contact={}, skills=[Skill(name="Go", category="Languages")],
        experiences=[Experience(id="exp-1", company="A", role="R", start="", end="",
                                bullets=[Bullet(id="exp-1-b1", text="orig")])],
        projects=[Project(id="proj-1", name="P", subtitle="", dates="",
                          bullets=[Bullet(id="proj-1-b1", text="porig")])],
    )


def test_parse_grounds_project_bullets():
    import json
    obj = {"fit": {}, "experiences": [], "skills_ordered": [],
           "projects": [{"project_id": "proj-1", "bullets": [
               {"source_bullet_id": "proj-1-b1", "text": "ptailored", "evidence_refs": []},
               {"source_bullet_id": "FAKE", "text": "fabricated"}]}],
           "summary_placeholder": "[S]", "cover_letter": ""}
    r = parse_tailor_result(obj, content=_content_with_project(), evidence=_evidence(), job_id="j")
    assert len(r.projects) == 1 and r.projects[0].project_id == "proj-1"
    assert [b.text for b in r.projects[0].bullets] == ["ptailored"]   # fabricated dropped


def test_parse_grounds_skills_drops_invented():
    import json
    obj = {"fit": {}, "experiences": [], "skills_ordered": ["Go", "Rust"],
           "projects": [], "summary_placeholder": "[S]", "cover_letter": ""}
    r = parse_tailor_result(obj, content=_content_with_project(), evidence=_evidence(), job_id="j")
    assert r.skills_ordered == ["Go"]   # Rust not in candidate skills -> dropped


def test_parse_non_list_projects_is_fallback():
    import json
    obj = {"fit": {}, "experiences": [], "skills_ordered": [], "projects": 123,
           "summary_placeholder": "[S]", "cover_letter": ""}
    r = parse_tailor_result(obj, content=_content_with_project(), evidence=_evidence(), job_id="j")
    assert r.is_fallback is True


def test_parse_cross_section_bullet_ids_are_not_interchangeable():
    # a project bullet id in an experience (and vice-versa) must be dropped —
    # the two grounding sets are disjoint by construction
    import json
    obj = {"fit": {}, "skills_ordered": [], "summary_placeholder": "[S]", "cover_letter": "",
           "experiences": [{"experience_id": "exp-1",
                            "bullets": [{"source_bullet_id": "proj-1-b1", "text": "wrong section"}]}],
           "projects": [{"project_id": "proj-1",
                         "bullets": [{"source_bullet_id": "exp-1-b1", "text": "wrong section"}]}]}
    r = parse_tailor_result(obj, content=_content_with_project(), evidence=_evidence(), job_id="j")
    # cross-section emissions are dropped where they appeared and each bullet is
    # restored, original wording, in its home entry
    assert [b.text for b in r.experiences[0].bullets] == ["orig"]
    assert [b.text for b in r.projects[0].bullets] == ["porig"]


# ---- rewrite-all completeness backstop (content-driven parsing) ----

def _content_two_bullets():
    return ResumeContent(
        name="S", contact={}, skills=[Skill(name="Go", category="Languages")],
        experiences=[Experience(id="exp-1", company="Acme", role="SSE", start="", end="",
                                bullets=[Bullet(id="exp-1-b1", text="orig1",
                                                evidence_refs=["JIRA-1", "JIRA-GONE"]),
                                         Bullet(id="exp-1-b2", text="orig2")])],
        projects=[],
    )


def _content_two_exps():
    return ResumeContent(
        name="S", contact={}, skills=[Skill(name="Go", category="Languages")],
        experiences=[
            Experience(id="exp-1", company="A", role="R", start="", end="",
                       bullets=[Bullet(id="exp-1-b1", text="orig1")]),
            Experience(id="exp-2", company="B", role="R2", start="", end="",
                       bullets=[Bullet(id="exp-2-b1", text="orig2")]),
        ],
        projects=[],
    )


def _obj(experiences=None, projects=None):
    return {"fit": {}, "skills_ordered": [], "summary_placeholder": "[S]", "cover_letter": "",
            "experiences": experiences or [], "projects": projects or []}


def test_parse_restores_omitted_bullet_at_entry_tail_with_filtered_refs():
    obj = _obj(experiences=[{"experience_id": "exp-1", "bullets": [
        {"source_bullet_id": "exp-1-b2", "text": "rewritten b2", "evidence_refs": []}]}])
    r = parse_tailor_result(obj, content=_content_two_bullets(),
                          evidence=_evidence(), job_id="j")
    bullets = r.experiences[0].bullets
    # model's ranked pick first, then the omitted bullet restored in ORIGINAL wording
    assert [b.text for b in bullets] == ["rewritten b2", "orig1"]
    assert bullets[1].source_bullet_id == "exp-1-b1"
    assert bullets[1].evidence_refs == ["JIRA-1"]   # original refs, filtered to the bank


def test_parse_restores_whole_omitted_entry():
    obj = _obj(experiences=[{"experience_id": "exp-1", "bullets": [
        {"source_bullet_id": "exp-1-b1", "text": "t1", "evidence_refs": []}]}])
    r = parse_tailor_result(obj, content=_content_two_exps(),
                          evidence=_evidence(), job_id="j")
    assert [e.experience_id for e in r.experiences] == ["exp-1", "exp-2"]  # content order
    assert [b.text for b in r.experiences[1].bullets] == ["orig2"]         # restored whole


def test_parse_drops_duplicate_source_bullet_id_first_wins():
    obj = _obj(experiences=[{"experience_id": "exp-1", "bullets": [
        {"source_bullet_id": "exp-1-b1", "text": "first", "evidence_refs": []},
        {"source_bullet_id": "exp-1-b1", "text": "second", "evidence_refs": []}]}])
    r = parse_tailor_result(obj, content=_content(), evidence=_evidence(), job_id="j")
    assert [b.text for b in r.experiences[0].bullets] == ["first"]


def test_parse_bullet_under_wrong_entry_dropped_there_restored_home():
    obj = _obj(experiences=[
        {"experience_id": "exp-1", "bullets": [
            {"source_bullet_id": "exp-2-b1", "text": "wrong home", "evidence_refs": []}]},
        {"experience_id": "exp-2", "bullets": []}])
    r = parse_tailor_result(obj, content=_content_two_exps(),
                          evidence=_evidence(), job_id="j")
    assert [b.text for b in r.experiences[0].bullets] == ["orig1"]   # wrong-entry emission dropped
    assert [b.text for b in r.experiences[1].bullets] == ["orig2"]   # restored in home entry
    all_texts = [b.text for e in r.experiences for b in e.bullets]
    assert "wrong home" not in all_texts


def test_parse_result_covers_every_content_bullet_exactly_once():
    r = parse_tailor_result(_obj(), content=_content_two_exps(),
                          evidence=_evidence(), job_id="j")   # model returned nothing useful
    assert r.is_fallback is False
    per_entry = {e.experience_id: [b.source_bullet_id for b in e.bullets] for e in r.experiences}
    assert per_entry == {"exp-1": ["exp-1-b1"], "exp-2": ["exp-2-b1"]}
