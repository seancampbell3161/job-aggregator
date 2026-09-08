from src.config import (
    AppConfig, AggregatorConfig, FiltersConfig, HnConfig, LocationFilterConfig,
    QuietHoursConfig, SchedulesConfig, Secrets, SourcesConfig,
)
from src.connectors.base import build_connectors
from datetime import time
from zoneinfo import ZoneInfo


def _cfg(**sources):
    return AppConfig(
        filters=FiltersConfig(
            titles=["software engineer"],
            seniority_allow=["mid", "senior"],
            location=LocationFilterConfig(remote_must_be_us=True, allowed_cities=["dallas", "seattle", "denver"], allow_unknown=True),
            comp_floor_usd=120_000,
            stack_any_of=["python"],
        ),
        quiet_hours=QuietHoursConfig(
            timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0)
        ),
        sources=SourcesConfig(**sources),
        schedules=SchedulesConfig(ats_minutes=2, slow_minutes=15),
        secrets=Secrets(ntfy_topic_url="x", discord_webhook_url="x"),
    )


def test_build_connectors_ats_tier_emits_one_per_company():
    cfg = _cfg(greenhouse=["stripe", "airbnb"], lever=["netflix"])
    conns = build_connectors(cfg, tier="ats")
    names = sorted(c.name for c in conns)
    assert names == ["greenhouse:airbnb", "greenhouse:stripe", "lever:netflix"]


def test_build_connectors_slow_tier_emits_hn_when_enabled():
    cfg = _cfg(
        hn_who_is_hiring=HnConfig(enabled=True),
        remotive=AggregatorConfig(enabled=False),
        remoteok=AggregatorConfig(enabled=False),
    )
    conns = build_connectors(cfg, tier="slow")
    assert [c.name for c in conns] == ["hn:who_is_hiring"]


def test_build_connectors_slow_tier_skips_hn_when_disabled():
    cfg = _cfg(
        hn_who_is_hiring=HnConfig(enabled=False),
        remotive=AggregatorConfig(enabled=False),
        remoteok=AggregatorConfig(enabled=False),
    )
    conns = build_connectors(cfg, tier="slow")
    assert conns == []


def test_build_connectors_ats_tier_excludes_hn():
    cfg = _cfg(greenhouse=["stripe"])
    conns = build_connectors(cfg, tier="ats")
    assert all(not c.name.startswith("hn:") for c in conns)


def test_build_connectors_slow_tier_includes_remotive_and_remoteok():
    cfg = _cfg(remotive=AggregatorConfig(enabled=True), remoteok=AggregatorConfig(enabled=True))
    conns = build_connectors(cfg, tier="slow")
    names = {c.name for c in conns}
    assert {"remotive", "remoteok"}.issubset(names)


def test_build_connectors_slow_tier_skips_remotive_when_disabled():
    cfg = _cfg(remotive=AggregatorConfig(enabled=False), remoteok=AggregatorConfig(enabled=True))
    conns = build_connectors(cfg, tier="slow")
    names = {c.name for c in conns}
    assert "remotive" not in names
    assert "remoteok" in names


def test_build_connectors_slow_tier_skips_remoteok_when_disabled():
    cfg = _cfg(remotive=AggregatorConfig(enabled=True), remoteok=AggregatorConfig(enabled=False))
    conns = build_connectors(cfg, tier="slow")
    names = {c.name for c in conns}
    assert "remoteok" not in names
    assert "remotive" in names


def test_aggregator_config_is_canonical_hn_config_is_alias():
    """AggregatorConfig is canonical; HnConfig is a backward-compat alias."""
    assert HnConfig is AggregatorConfig


def test_build_connectors_ats_tier_includes_workday():
    from src.config import WorkdayTenant
    from src.connectors.workday import WorkdayConnector

    cfg = _cfg(
        workday=[
            WorkdayTenant(tenant="salesforce", region="wd12", site="External_Career_Site"),
            WorkdayTenant(tenant="nvidia", region="wd5", site="NVIDIAExternalCareerSite"),
        ],
    )
    conns = build_connectors(cfg, tier="ats")
    workday_conns = [c for c in conns if isinstance(c, WorkdayConnector)]
    assert len(workday_conns) == 2
    by_name = {c.name: c for c in workday_conns}
    assert "workday:salesforce:External_Career_Site" in by_name
    assert "workday:nvidia:NVIDIAExternalCareerSite" in by_name
    assert by_name["workday:salesforce:External_Career_Site"].region == "wd12"


def test_build_connectors_ats_tier_dedupes_workday_by_tenant_and_site():
    """Two entries with the same tenant+site should yield one connector."""
    from src.config import WorkdayTenant
    from src.connectors.workday import WorkdayConnector

    cfg = _cfg(
        workday=[
            WorkdayTenant(tenant="salesforce", region="wd12", site="External_Career_Site"),
            WorkdayTenant(tenant="salesforce", region="wd12", site="External_Career_Site"),
        ],
    )
    conns = build_connectors(cfg, tier="ats")
    workday_conns = [c for c in conns if isinstance(c, WorkdayConnector)]
    assert len(workday_conns) == 1


def test_build_connectors_ats_tier_includes_smartrecruiters():
    cfg = _cfg(greenhouse=["stripe"], smartrecruiters=["instacart", "bosch"])
    conns = build_connectors(cfg, tier="ats")
    names = sorted(c.name for c in conns)
    assert "smartrecruiters:instacart" in names
    assert "smartrecruiters:bosch" in names
    assert "greenhouse:stripe" in names


def test_build_connectors_unions_config_and_discovered_slugs():
    """Healthy discovered slugs join the config-listed slugs in the ATS tier."""
    from unittest.mock import MagicMock

    from src.state import DiscoveredSlug

    cfg = _cfg(greenhouse=["stripe"])
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name="greenhouse:newco",
            ats_family="greenhouse", slug="newco", company_name="NewCo",
            discovered_at="2026-05-02T00:00:00+00:00",
            last_validated_at="2026-05-02T00:00:00+00:00",
            validation_status="ok",
            consecutive_failures=0,
            last_posting_count=3,
        ),
        DiscoveredSlug(
            connector_name="lever:cohere",
            ats_family="lever", slug="cohere", company_name="Cohere",
            discovered_at="2026-05-02T00:00:00+00:00",
            last_validated_at="2026-05-02T00:00:00+00:00",
            validation_status="ok",
            consecutive_failures=0,
            last_posting_count=10,
        ),
    ]
    conns = build_connectors(cfg, tier="ats", discovered=discovered)
    names = {c.name for c in conns}
    assert "greenhouse:stripe" in names
    assert "greenhouse:newco" in names
    assert "lever:cohere" in names


def test_build_connectors_dedupes_when_slug_in_both_config_and_discovered():
    """If a slug appears in both, config wins; the discovered row contributes nothing extra."""
    from unittest.mock import MagicMock

    from src.state import DiscoveredSlug

    cfg = _cfg(greenhouse=["stripe"])
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name="greenhouse:stripe",
            ats_family="greenhouse", slug="stripe", company_name="Stripe",
            discovered_at="2026-05-02T00:00:00+00:00",
            last_validated_at="2026-05-02T00:00:00+00:00",
            validation_status="ok",
            consecutive_failures=0,
            last_posting_count=3,
        ),
    ]
    conns = build_connectors(cfg, tier="ats", discovered=discovered)
    names = [c.name for c in conns]
    assert names.count("greenhouse:stripe") == 1


def test_build_connectors_works_without_discovered_arg():
    """Backwards-compatible: missing `discovered` arg behaves like Plan 1."""
    cfg = _cfg(greenhouse=["stripe"])
    conns = build_connectors(cfg, tier="ats")
    names = {c.name for c in conns}
    assert names == {"greenhouse:stripe"}


def test_build_connectors_skips_discovered_with_unknown_ats():
    """Defensive: if a discovered row has an ats_family we don't have a connector for, skip it."""
    from unittest.mock import MagicMock

    from src.state import DiscoveredSlug

    cfg = _cfg(greenhouse=["stripe"])
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name="indeed:acme",
            ats_family="indeed", slug="acme", company_name="Acme",
            discovered_at="2026-05-02T00:00:00+00:00",
            last_validated_at="2026-05-02T00:00:00+00:00",
            validation_status="ok",
            consecutive_failures=0,
            last_posting_count=1,
        ),
    ]
    conns = build_connectors(cfg, tier="ats", discovered=discovered)
    names = {c.name for c in conns}
    assert "indeed:acme" not in names
    assert "greenhouse:stripe" in names


def test_build_connectors_slow_tier_includes_hiringcafe_when_enabled():
    from unittest.mock import MagicMock

    from src.config import HiringCafeConfig

    cfg = _cfg(
        hiringcafe=HiringCafeConfig(enabled=True, max_postings_per_cycle=100),
    )
    discovered = MagicMock()
    discovered.list_healthy.return_value = []
    conns = build_connectors(cfg, tier="slow", discovered=discovered)
    names = {c.name for c in conns}
    assert "hiringcafe" in names


def test_build_connectors_slow_tier_skips_hiringcafe_when_disabled():
    from src.config import HiringCafeConfig

    cfg = _cfg(hiringcafe=HiringCafeConfig(enabled=False))
    conns = build_connectors(cfg, tier="slow")
    names = {c.name for c in conns}
    assert "hiringcafe" not in names


def test_build_connectors_slow_tier_hiringcafe_active_set_includes_config_and_discovered():
    """The HiringCafeConnector's active_set should be the union of config slugs
    and healthy discovered slugs across all 6 supported ATSs."""
    from unittest.mock import MagicMock

    from src.config import HiringCafeConfig
    from src.state import DiscoveredSlug

    cfg = _cfg(
        greenhouse=["stripe"],
        lever=["netflix"],
        hiringcafe=HiringCafeConfig(enabled=True),
    )
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name="ashby:linear",
            ats_family="ashby", slug="linear", company_name="Linear",
            discovered_at="2026-05-02T00:00:00+00:00",
            last_validated_at="2026-05-02T00:00:00+00:00",
            validation_status="ok",
            consecutive_failures=0,
            last_posting_count=5,
        ),
    ]
    conns = build_connectors(cfg, tier="slow", discovered=discovered)
    cafe_conn = next(c for c in conns if c.name == "hiringcafe")

    # The connector exposes its active_set as a private attribute; we read it
    # to verify the union was computed correctly.
    assert ("greenhouse", "stripe") in cafe_conn._active_set
    assert ("lever", "netflix") in cafe_conn._active_set
    assert ("ashby", "linear") in cafe_conn._active_set


def test_build_connectors_includes_rippling():
    cfg = _cfg(rippling=["acme"])
    conns = build_connectors(cfg, tier="ats")
    assert "rippling:acme" in [c.name for c in conns]


def test_build_connectors_rippling_discovered_row_built():
    from src.state import DiscoveredSlug

    class _Disc:
        def list_healthy(self):
            return [DiscoveredSlug(
                connector_name="rippling:globex", ats_family="rippling", slug="globex",
                company_name="Globex", discovered_at="2026-06-01T00:00:00+00:00",
                last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
                consecutive_failures=0, last_posting_count=3,
            )]

    cfg = _cfg()
    conns = build_connectors(cfg, tier="ats", discovered=_Disc())
    assert "rippling:globex" in [c.name for c in conns]


def test_build_connectors_skips_suppressed_config_slug():
    cfg = _cfg(greenhouse=["stripe", "deadco"])
    conns = build_connectors(cfg, tier="ats", suppressed={"greenhouse:deadco"})
    names = {c.name for c in conns}
    assert "greenhouse:stripe" in names
    assert "greenhouse:deadco" not in names


def test_build_connectors_skips_suppressed_workday_triple():
    from src.config import WorkdayTenant
    cfg = _cfg(workday=[WorkdayTenant(tenant="walmart", region="wd5", site="WalmartExternal")])
    conns = build_connectors(cfg, tier="ats", suppressed={"workday:walmart:WalmartExternal"})
    assert conns == []


def test_build_connectors_skips_suppressed_discovered_slug():
    from unittest.mock import MagicMock
    from src.state import DiscoveredSlug
    cfg = _cfg(greenhouse=["stripe"])
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name="ashby:dead", ats_family="ashby", slug="dead",
            company_name="Dead", discovered_at="2026-06-01T00:00:00+00:00",
            last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
            consecutive_failures=0, last_posting_count=3,
        ),
    ]
    conns = build_connectors(cfg, tier="ats", discovered=discovered, suppressed={"ashby:dead"})
    names = {c.name for c in conns}
    assert "ashby:dead" not in names
    assert "greenhouse:stripe" in names


def test_build_connectors_empty_suppressed_is_current_behavior():
    cfg = _cfg(greenhouse=["stripe"])
    conns = build_connectors(cfg, tier="ats")
    assert {c.name for c in conns} == {"greenhouse:stripe"}


def _workable_discovered(name="workable:blaze", slug="blaze"):
    from unittest.mock import MagicMock
    from src.state import DiscoveredSlug
    discovered = MagicMock()
    discovered.list_healthy.return_value = [
        DiscoveredSlug(
            connector_name=name, ats_family="workable", slug=slug,
            company_name="Blaze", discovered_at="2026-06-01T00:00:00+00:00",
            last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
            consecutive_failures=0, last_posting_count=3,
        ),
    ]
    return discovered


def test_build_connectors_workable_is_slow_tier_not_ats():
    """Workable polls on the slow tier (apply.workable.com 429s under fast
    polling); it must NOT appear in the ats build and MUST appear in slow."""
    cfg = _cfg(workable=["acme"])  # config slug
    ats = {c.name for c in build_connectors(cfg, tier="ats")}
    slow = {c.name for c in build_connectors(cfg, tier="slow")}
    assert "workable:acme" not in ats
    assert "workable:acme" in slow


def test_build_connectors_slow_tier_includes_discovered_workable():
    cfg = _cfg()
    conns = build_connectors(cfg, tier="slow", discovered=_workable_discovered())
    assert "workable:blaze" in {c.name for c in conns}


def test_build_connectors_ats_tier_excludes_discovered_workable():
    cfg = _cfg(greenhouse=["stripe"])
    conns = build_connectors(cfg, tier="ats", discovered=_workable_discovered())
    names = {c.name for c in conns}
    assert "workable:blaze" not in names
    assert "greenhouse:stripe" in names


def test_build_connectors_slow_tier_skips_suppressed_workable():
    cfg = _cfg()
    conns = build_connectors(
        cfg, tier="slow", discovered=_workable_discovered(), suppressed={"workable:blaze"})
    assert "workable:blaze" not in {c.name for c in conns}


def test_build_connectors_includes_oraclecloud_tenants():
    from src.connectors.oraclecloud import OracleCloudConnector
    cfg = _cfg(oraclecloud=[{"tenant": "egug", "region": "us2", "site": "CX_1"}])
    conns = build_connectors(cfg, tier="ats")
    orc = [c for c in conns if isinstance(c, OracleCloudConnector)]
    assert [c.name for c in orc] == ["oraclecloud:egug:CX_1"]


def test_build_connectors_skips_suppressed_oraclecloud():
    from src.connectors.oraclecloud import OracleCloudConnector
    cfg = _cfg(oraclecloud=[{"tenant": "egug", "region": "us2", "site": "CX_1"}])
    conns = build_connectors(cfg, tier="ats", suppressed=frozenset({"oraclecloud:egug:CX_1"}))
    assert not [c for c in conns if isinstance(c, OracleCloudConnector)]


def test_build_connectors_passes_oraclecloud_company_display_name():
    from src.connectors.oraclecloud import OracleCloudConnector
    cfg = _cfg(oraclecloud=[
        {"tenant": "egug", "region": "us2", "site": "CX_1", "company": "American Express"},
    ])
    conns = build_connectors(cfg, tier="ats")
    orc = [c for c in conns if isinstance(c, OracleCloudConnector)]
    assert [c.company for c in orc] == ["American Express"]


def test_build_connectors_ats_tier_includes_jsonld_boards():
    from src.config import JsonLdBoard
    from src.connectors.jsonld import JsonLdBoardConnector
    cfg = _cfg(jsonld_boards=[
        JsonLdBoard(family="icims", slug="steeldynamics",
                    base_url="https://careers-steeldynamics.icims.com", company="Steel Dynamics"),
    ])
    conns = build_connectors(cfg, tier="ats")
    jl = [c for c in conns if isinstance(c, JsonLdBoardConnector)]
    assert len(jl) == 1
    assert jl[0].name == "icims:steeldynamics"


def test_build_connectors_ats_tier_includes_eightfold():
    from src.config import EightfoldTenant
    from src.connectors.eightfold import EightfoldConnector
    cfg = _cfg(eightfold=[
        EightfoldTenant(slug="bms", domain="bms.com", flavor="pcsx",
                        company="Bristol Myers Squibb"),
        EightfoldTenant(slug="albemarle", domain="albemarle.com", flavor="apply_v2"),
    ])
    conns = build_connectors(cfg, tier="ats")
    ef = [c for c in conns if isinstance(c, EightfoldConnector)]
    assert len(ef) == 2
    by_name = {c.name: c for c in ef}
    assert "eightfold:bms" in by_name
    assert by_name["eightfold:bms"].flavor == "pcsx"
    assert by_name["eightfold:albemarle"].flavor == "apply_v2"


def test_build_connectors_ats_tier_includes_phenom():
    from src.config import PhenomBoard
    from src.connectors.phenom import PhenomConnector
    cfg = _cfg(phenom=[
        PhenomBoard(careers_url="https://careers.fisglobal.com", company="FIS"),
        PhenomBoard(careers_url="https://jobs.gehealthcare.com", company="GE HealthCare"),
    ])
    conns = build_connectors(cfg, tier="ats")
    ph = [c for c in conns if isinstance(c, PhenomConnector)]
    assert len(ph) == 2
    names = {c.name for c in ph}
    assert names == {"phenom:fisglobal-com", "phenom:gehealthcare-com"}


def test_build_connectors_ats_tier_includes_taleo():
    from src.config import TaleoBoard
    from src.connectors.taleo import TaleoConnector
    cfg = _cfg(taleo=[
        TaleoBoard(tenant="cinfin", section="ex", company="Cincinnati Financial"),
        TaleoBoard(tenant="textron", section="textron", company="Textron"),
    ])
    conns = build_connectors(cfg, tier="ats")
    tl = [c for c in conns if isinstance(c, TaleoConnector)]
    assert len(tl) == 2
    assert {c.name for c in tl} == {"taleo:cinfin:ex", "taleo:textron:textron"}


def test_connector_from_identity_per_family():
    from src.connectors.base import connector_from_identity
    from src.connectors.workday import WorkdayConnector
    from src.connectors.taleo import TaleoConnector
    w = connector_from_identity("workday", {"tenant": "3m", "region": "wd1", "site": "Search"}, "3M")
    assert isinstance(w, WorkdayConnector) and w.name == "workday:3m:Search"
    t = connector_from_identity("taleo", {"tenant": "cinfin", "section": "ex"}, "Cincinnati Financial")
    assert isinstance(t, TaleoConnector) and t.name == "taleo:cinfin:ex"
    assert connector_from_identity("bogus", {"x": 1}, None) is None
    assert connector_from_identity(None, {"x": 1}, None) is None


def test_connector_from_identity_slug_families():
    """fingerprint_company also matches the 5 slug ATS families (greenhouse,
    lever, ashby, smartrecruiters, rippling) — connector_from_identity must
    build a real connector for each, not fall through to None (which would
    store the board as an inert 'ok' row that's never polled)."""
    from src.connectors.base import connector_from_identity
    from src.connectors.greenhouse import GreenhouseConnector
    from src.connectors.lever import LeverConnector
    from src.connectors.ashby import AshbyConnector
    from src.connectors.smartrecruiters import SmartRecruitersConnector
    from src.connectors.rippling import RipplingConnector

    g = connector_from_identity("greenhouse", {"slug": "stripe"}, "Stripe")
    assert isinstance(g, GreenhouseConnector) and g.name == "greenhouse:stripe"

    l = connector_from_identity("lever", {"slug": "netflix"}, "Netflix")
    assert isinstance(l, LeverConnector) and l.name == "lever:netflix"

    a = connector_from_identity("ashby", {"slug": "linear"}, "Linear")
    assert isinstance(a, AshbyConnector) and a.name == "ashby:linear"

    sr = connector_from_identity("smartrecruiters", {"slug": "bosch"}, "Bosch")
    assert isinstance(sr, SmartRecruitersConnector) and sr.name == "smartrecruiters:bosch"

    r = connector_from_identity("rippling", {"slug": "acme"}, "Acme")
    assert isinstance(r, RipplingConnector) and r.name == "rippling:acme"


def test_connector_from_identity_oraclecloud():
    from src.connectors.base import connector_from_identity
    from src.connectors.oraclecloud import OracleCloudConnector
    c = connector_from_identity(
        "oraclecloud", {"tenant": "egug", "region": "us2", "site": "CX_1"}, "American Express"
    )
    assert isinstance(c, OracleCloudConnector)
    assert c.name == "oraclecloud:egug:CX_1"
    assert c.company == "American Express"


def test_connector_from_identity_eightfold():
    from src.connectors.base import connector_from_identity
    from src.connectors.eightfold import EightfoldConnector
    c = connector_from_identity(
        "eightfold", {"slug": "bms", "domain": "bms.com", "flavor": "pcsx"}, "Bristol Myers Squibb"
    )
    assert isinstance(c, EightfoldConnector)
    assert c.name == "eightfold:bms"
    assert c.flavor == "pcsx"
    assert c.company == "Bristol Myers Squibb"


def test_connector_from_identity_jsonld():
    from src.connectors.base import connector_from_identity
    from src.connectors.jsonld import JsonLdBoardConnector
    c = connector_from_identity(
        "jsonld",
        {"family": "icims", "slug": "steeldynamics", "base_url": "https://careers-steeldynamics.icims.com"},
        "Steel Dynamics",
    )
    assert isinstance(c, JsonLdBoardConnector)
    assert c.name == "icims:steeldynamics"
    assert c.company == "Steel Dynamics"


class _FakeBoards:
    def __init__(self, rows): self._rows = rows
    def list_healthy(self): return self._rows


def test_build_connectors_unions_discovered_boards():
    from src.connectors.base import build_connectors, connector_from_identity  # noqa: F401
    from src.connectors.workday import WorkdayConnector
    from src.state import DiscoveredBoard
    rows = [DiscoveredBoard(domain="3m.com", name="3M", status="ok", family="workday",
                            identity={"tenant": "3m", "region": "wd1", "site": "Search"},
                            connector_name="workday:3m:Search", company="3M",
                            last_swept_at="2026-01-01", failure_streak=0)]
    cfg = _cfg()  # no config boards
    conns = build_connectors(cfg, tier="ats", boards=_FakeBoards(rows))
    wds = [c for c in conns if isinstance(c, WorkdayConnector) and c.name == "workday:3m:Search"]
    assert len(wds) == 1


def test_discovered_board_deduped_against_config():
    from src.connectors.base import build_connectors
    from src.connectors.workday import WorkdayConnector
    from src.config import WorkdayTenant
    from src.state import DiscoveredBoard
    # same identity in config AND discovered → built once (config wins)
    cfg = _cfg(workday=[WorkdayTenant(tenant="3m", region="wd1", site="Search")])
    rows = [DiscoveredBoard(domain="3m.com", name="3M", status="ok", family="workday",
                            identity={"tenant": "3m", "region": "wd1", "site": "Search"},
                            connector_name="workday:3m:Search", company="3M",
                            last_swept_at="2026-01-01", failure_streak=0)]
    conns = build_connectors(cfg, tier="ats", boards=_FakeBoards(rows))
    wds = [c for c in conns if isinstance(c, WorkdayConnector) and c.name == "workday:3m:Search"]
    assert len(wds) == 1  # not duplicated


def test_build_connectors_headless_tier_includes_avature():
    from src.config import AvatureBoard
    from src.connectors.avature import AvatureConnector
    cfg = _cfg(avature=[
        AvatureBoard(careers_url="https://careers.jacobs.com/en_US/careers/SearchJobs", company="Jacobs"),
        AvatureBoard(careers_url="https://careers.cbre.com/en_US/careers/SearchJobs", company="CBRE"),
    ])
    conns = build_connectors(cfg, tier="headless")
    av = [c for c in conns if isinstance(c, AvatureConnector)]
    assert {c.name for c in av} == {"avature:jacobs", "avature:cbre"}


def test_eu_slug_families_build_from_config():
    cfg = _cfg(personio=["everphone"], recruitee=["sendcloud"], teamtailor=["tibber"])
    conns = build_connectors(cfg, "ats")
    names = {c.name for c in conns}
    assert {"personio:everphone", "recruitee:sendcloud", "teamtailor:tibber"} <= names


def test_eu_slug_families_from_discovered_rows():
    from src.state import DiscoveredSlug

    class _Store:
        def list_healthy(self):
            return [
                DiscoveredSlug(
                    connector_name="recruitee:hotjar", ats_family="recruitee", slug="hotjar",
                    company_name="Hotjar", discovered_at="2026-06-01T00:00:00+00:00",
                    last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
                    consecutive_failures=0, last_posting_count=3,
                ),
                DiscoveredSlug(
                    connector_name="personio:snocks", ats_family="personio", slug="snocks",
                    company_name="Snocks", discovered_at="2026-06-01T00:00:00+00:00",
                    last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
                    consecutive_failures=0, last_posting_count=3,
                ),
                DiscoveredSlug(
                    connector_name="teamtailor:voi", ats_family="teamtailor", slug="voi",
                    company_name="Voi", discovered_at="2026-06-01T00:00:00+00:00",
                    last_validated_at="2026-06-16T00:00:00+00:00", validation_status="ok",
                    consecutive_failures=0, last_posting_count=3,
                ),
            ]
    cfg = _cfg()
    conns = build_connectors(cfg, "ats", discovered=_Store())
    names = {c.name for c in conns}
    assert {"recruitee:hotjar", "personio:snocks", "teamtailor:voi"} <= names


def test_connector_from_identity_eu_families():
    from src.connectors.base import connector_from_identity
    for family, expected in [
        ("recruitee", "recruitee:acme"),
        ("personio", "personio:acme"),
        ("teamtailor", "teamtailor:acme"),
    ]:
        conn = connector_from_identity(family, {"slug": "acme"})
        assert conn is not None and conn.name == expected and conn.tier == "ats"


def test_hiringcafe_fetcher_receives_default_plus_extra_queries():
    from src.hiringcafe import DEFAULT_QUERY

    cfg = _cfg(hiringcafe={"enabled": True, "extra_queries": ["software engineer europe"]})
    conns = build_connectors(cfg, "slow")
    cafe = next(c for c in conns if c.name == "hiringcafe")
    assert cafe._cafe._searches == [
        (DEFAULT_QUERY, None),
        ("software engineer europe", None),
    ]


def test_build_connectors_slow_tier_builds_adzuna_when_enabled_with_secrets():
    from src.config import AdzunaConfig

    cfg = _cfg(
        hn_who_is_hiring=HnConfig(enabled=False),
        remotive=AggregatorConfig(enabled=False),
        remoteok=AggregatorConfig(enabled=False),
        adzuna=AdzunaConfig(enabled=True, queries=["staff engineer"]),
    )
    cfg = cfg.model_copy(update={"secrets": Secrets(
        ntfy_topic_url="x", discord_webhook_url="x",
        adzuna_app_id="my-id", adzuna_app_key="my-key",
    )})
    conns = build_connectors(cfg, tier="slow")
    assert [c.name for c in conns] == ["adzuna"]


def test_build_connectors_slow_tier_skips_adzuna_without_secrets(caplog):
    from src.config import AdzunaConfig

    cfg = _cfg(
        hn_who_is_hiring=HnConfig(enabled=False),
        remotive=AggregatorConfig(enabled=False),
        remoteok=AggregatorConfig(enabled=False),
        adzuna=AdzunaConfig(enabled=True, queries=["staff engineer"]),
    )
    with caplog.at_level("ERROR"):
        conns = build_connectors(cfg, tier="slow")
    assert conns == []
    assert "adzuna_missing_secrets" in caplog.text


def test_build_connectors_slow_tier_adzuna_disabled_by_default():
    cfg = _cfg(
        hn_who_is_hiring=HnConfig(enabled=False),
        remotive=AggregatorConfig(enabled=False),
        remoteok=AggregatorConfig(enabled=False),
    )
    assert build_connectors(cfg, tier="slow") == []
