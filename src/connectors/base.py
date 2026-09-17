from __future__ import annotations

import logging
from typing import Literal, Protocol

import httpx

from src.config import AppConfig
from src.models import ConnectorState, FetchResult, NormalizedPosting, Tier

log = logging.getLogger(__name__)


class Connector(Protocol):
    name: str
    tier: Tier

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult: ...


class Enrichable(Protocol):
    """Optional connector capability: fetch a posting's full detail (e.g. the
    real JD behind a thin list endpoint) and return an enriched copy. The
    orchestrator calls enrich() on filter-survivors before scoring, only when
    supports_enrich is True."""

    supports_enrich: bool

    async def enrich(
        self, client: httpx.AsyncClient, posting: NormalizedPosting
    ) -> NormalizedPosting: ...


# ATS families that poll on the slow (15-min) tier instead of ats (60s). Workable
# boards are tiny and apply.workable.com 429-rate-limits aggressive polling.
_SLOW_ATS_FAMILIES = frozenset({"workable"})

# Slug-addressable ATS families that fingerprint_company can also match (in
# addition to the enterprise families below). Identity is {"slug": ...}.
_SLUG_FAMILIES = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "rippling",
    "personio", "recruitee", "teamtailor",
})


def connector_from_identity(family: str | None, identity: dict, company: str | None = None) -> Connector | None:
    """Reconstruct a connector from a stored (family, identity) — built exactly as
    the config path builds each family. Unknown family → None."""
    identity = identity or {}
    if family in _SLUG_FAMILIES:
        # Match the ats slug constructors build_connectors uses for the same
        # families (ctor_for, ats tier) — a slug-family fingerprint match must
        # build a real, pollable connector, not fall through to None.
        from src.connectors.greenhouse import GreenhouseConnector
        from src.connectors.lever import LeverConnector
        from src.connectors.ashby import AshbyConnector
        from src.connectors.smartrecruiters import SmartRecruitersConnector
        from src.connectors.rippling import RipplingConnector
        from src.connectors.personio import PersonioConnector
        from src.connectors.recruitee import RecruiteeConnector
        from src.connectors.teamtailor import TeamtailorConnector
        _ctor = {
            "greenhouse": GreenhouseConnector, "lever": LeverConnector, "ashby": AshbyConnector,
            "smartrecruiters": SmartRecruitersConnector, "rippling": RipplingConnector,
            "personio": PersonioConnector, "recruitee": RecruiteeConnector,
            "teamtailor": TeamtailorConnector,
        }[family]
        return _ctor(identity["slug"])
    if family == "workday":
        from src.connectors.workday import WorkdayConnector
        return WorkdayConnector(tenant=identity["tenant"], region=identity["region"], site=identity["site"])
    if family == "oraclecloud":
        from src.connectors.oraclecloud import OracleCloudConnector
        return OracleCloudConnector(tenant=identity["tenant"], region=identity["region"],
                                    site=identity["site"], company=company)
    if family == "taleo":
        from src.connectors.taleo import TaleoConnector
        return TaleoConnector(tenant=identity["tenant"], section=identity["section"], company=company)
    if family == "eightfold":
        from src.connectors.eightfold import EightfoldConnector
        return EightfoldConnector(slug=identity["slug"], domain=identity["domain"],
                                  flavor=identity.get("flavor", "pcsx"), company=company)
    if family == "jsonld":
        from src.connectors.jsonld import JsonLdBoardConnector
        return JsonLdBoardConnector(family=identity["family"], slug=identity["slug"],
                                    base_url=identity["base_url"], company=company)
    return None


def _board_active_pair(family: str | None, identity: dict | None) -> tuple[str, str] | None:
    """(ats_family, board_token) as hiring.cafe would label this board's
    postings — used to suppress double-emission once we poll it directly.
    jsonld rows carry their real family (e.g. icims) inside identity."""
    identity = identity or {}
    if family == "jsonld":
        fam, token = identity.get("family"), identity.get("slug")
    elif family in ("workday", "oraclecloud", "taleo"):
        fam, token = family, identity.get("tenant")
    else:  # slug families + eightfold all key on "slug"
        fam, token = family, identity.get("slug")
    if not fam or not token:
        return None
    return fam, token


def build_connectors(cfg: AppConfig, tier: Tier, *, discovered=None, boards=None, suppressed=frozenset(), sightings=None) -> list[Connector]:
    """Build connectors for a tier, unioning configured slugs with healthy
    discovered slugs (when a store exposing ``list_healthy()`` is provided),
    and skipping any connector whose name is in ``suppressed`` (the poll-health
    circuit breaker — dead 404/410 connectors).

    Imports concrete connectors lazily so this module stays free of cycles."""
    out: list[Connector] = []

    if tier == "ats":
        from src.connectors.greenhouse import GreenhouseConnector
        from src.connectors.lever import LeverConnector
        from src.connectors.ashby import AshbyConnector
        from src.connectors.smartrecruiters import SmartRecruitersConnector
        from src.connectors.rippling import RipplingConnector
        from src.connectors.personio import PersonioConnector
        from src.connectors.recruitee import RecruiteeConnector
        from src.connectors.teamtailor import TeamtailorConnector
        from src.connectors.workday import WorkdayConnector
        from src.connectors.oraclecloud import OracleCloudConnector
        from src.connectors.eightfold import EightfoldConnector
        from src.connectors.jsonld import JsonLdBoardConnector
        from src.connectors.phenom import PhenomConnector
        from src.connectors.taleo import TaleoConnector

        # NB: "workable" is intentionally absent — it polls on the slow tier (see
        # _SLOW_ATS_FAMILIES). Discovered workable rows are skipped below by the
        # `row.ats_family not in ctor_for` guard.
        ctor_for = {
            "greenhouse": GreenhouseConnector,
            "lever": LeverConnector,
            "ashby": AshbyConnector,
            "smartrecruiters": SmartRecruitersConnector,
            "rippling": RipplingConnector,
            "personio": PersonioConnector,
            "recruitee": RecruiteeConnector,
            "teamtailor": TeamtailorConnector,
        }

        seen: set[str] = set()
        # Config slugs first ("win" on dedup)
        for ats, slugs in [
            ("greenhouse", cfg.sources.greenhouse),
            ("lever", cfg.sources.lever),
            ("ashby", cfg.sources.ashby),
            ("smartrecruiters", cfg.sources.smartrecruiters),
            ("rippling", cfg.sources.rippling),
            ("personio", cfg.sources.personio),
            ("recruitee", cfg.sources.recruitee),
            ("teamtailor", cfg.sources.teamtailor),
        ]:
            for slug in slugs:
                key = f"{ats}:{slug}"
                if key in seen or key in suppressed:
                    continue
                seen.add(key)
                out.append(ctor_for[ats](slug))

        # Workday tenants are (tenant, region, site) triples — separate loop
        # because their identity isn't a single slug. Two entries differing only
        # in region collapse to one (key uses tenant+site); the first wins.
        for wt in cfg.sources.workday:
            key = f"workday:{wt.tenant}:{wt.site}"
            if key in seen or key in suppressed:
                continue
            seen.add(key)
            out.append(WorkdayConnector(tenant=wt.tenant, region=wt.region, site=wt.site))

        # Oracle Recruiting Cloud tenants are (tenant, region, site) triples
        # like Workday's; same dedup/suppression identity rules.
        for ot in cfg.sources.oraclecloud:
            key = f"oraclecloud:{ot.tenant}:{ot.site}"
            if key in seen or key in suppressed:
                continue
            seen.add(key)
            out.append(
                OracleCloudConnector(
                    tenant=ot.tenant, region=ot.region, site=ot.site, company=ot.company
                )
            )

        # Modern Taleo career sections: (tenant, section); identity taleo:tenant:section.
        for tb in cfg.sources.taleo:
            key = f"taleo:{tb.tenant}:{tb.section}"
            if key in seen or key in suppressed:
                continue
            seen.add(key)
            out.append(TaleoConnector(tenant=tb.tenant, section=tb.section, company=tb.company))

        # Phenom People career sites (branded domains; hand-curated).
        for pb in cfg.sources.phenom:
            conn = PhenomConnector(careers_url=pb.careers_url, company=pb.company)
            if conn.name in seen or conn.name in suppressed:
                continue
            seen.add(conn.name)
            out.append(conn)

        # Eightfold.ai tenants: {slug, domain, flavor}; identity is eightfold:slug.
        for ef in cfg.sources.eightfold:
            key = f"eightfold:{ef.slug}"
            if key in seen or key in suppressed:
                continue
            seen.add(key)
            out.append(
                EightfoldConnector(
                    slug=ef.slug, domain=ef.domain, flavor=ef.flavor, company=ef.company
                )
            )

        # JSON-LD scraper boards (SuccessFactors / iCIMS / TalentBrew) — a
        # unified list; identity is family:slug, same dedup/suppression rules.
        for jb in cfg.sources.jsonld_boards:
            key = f"{jb.family}:{jb.slug}"
            if key in seen or key in suppressed:
                continue
            seen.add(key)
            out.append(
                JsonLdBoardConnector(
                    family=jb.family, slug=jb.slug, base_url=jb.base_url, company=jb.company
                )
            )

        # Discovered slugs (only those marked healthy)
        if discovered is not None:
            for row in discovered.list_healthy():
                key = row.connector_name
                if key in seen or key in suppressed:
                    continue
                if row.ats_family not in ctor_for:
                    continue
                seen.add(key)
                out.append(ctor_for[row.ats_family](row.slug))

        # Discovered enterprise boards (structured; only healthy). Config boards
        # were built above, so config wins the connector-name dedup.
        if boards is not None:
            for row in boards.list_healthy():
                conn = connector_from_identity(row.family, row.identity, row.company)
                if conn is None or conn.name in seen or conn.name in suppressed:
                    continue
                seen.add(conn.name)
                out.append(conn)

    elif tier == "slow":
        # Slow-tier ATS families (workable): config slugs ∪ healthy discovered
        # slugs, de-duped, skipping suppressed/backed-off names.
        from src.connectors.workable import WorkableConnector

        slow_ctor_for = {"workable": WorkableConnector}
        slow_seen: set[str] = set()
        for fam in _SLOW_ATS_FAMILIES:
            for slug in getattr(cfg.sources, fam, []):
                key = f"{fam}:{slug}"
                if key in slow_seen or key in suppressed:
                    continue
                slow_seen.add(key)
                out.append(slow_ctor_for[fam](slug))
        if discovered is not None:
            for row in discovered.list_healthy():
                if row.ats_family not in _SLOW_ATS_FAMILIES:
                    continue
                key = row.connector_name
                if key in slow_seen or key in suppressed:
                    continue
                slow_seen.add(key)
                out.append(slow_ctor_for[row.ats_family](row.slug))

        # Direct-poll active set — shared by aggregator connectors that
        # cross-source dedup against boards we already poll (hiring.cafe,
        # adzuna). Built only when a consumer is enabled.
        adz = cfg.sources.adzuna
        active_set: set[tuple[str, str]] = set()
        if cfg.sources.hiringcafe.enabled or adz.enabled:
            for ats, slugs in [
                ("greenhouse", cfg.sources.greenhouse),
                ("lever", cfg.sources.lever),
                ("ashby", cfg.sources.ashby),
                ("workable", cfg.sources.workable),
                ("smartrecruiters", cfg.sources.smartrecruiters),
                ("rippling", cfg.sources.rippling),
                ("personio", cfg.sources.personio),
                ("recruitee", cfg.sources.recruitee),
                ("teamtailor", cfg.sources.teamtailor),
            ]:
                for slug in slugs:
                    active_set.add((ats, slug))
            if discovered is not None:
                for row in discovered.list_healthy():
                    active_set.add((row.ats_family, row.slug))
            if boards is not None:
                for brow in boards.list_healthy():
                    pair = _board_active_pair(brow.family, brow.identity)
                    if pair is not None:
                        active_set.add(pair)

        if cfg.sources.hn_who_is_hiring.enabled:
            from src.connectors.hn import HnWhoIsHiringConnector
            out.append(HnWhoIsHiringConnector())
        if cfg.sources.remotive.enabled:
            from src.connectors.remotive import RemotiveConnector
            out.append(RemotiveConnector())
        if cfg.sources.remoteok.enabled:
            from src.connectors.remoteok import RemoteOkConnector
            out.append(RemoteOkConnector())
        if cfg.sources.hiringcafe.enabled:
            from src.config import HiringCafeSearch
            from src.connectors.hiringcafe import HiringCafeConnector
            from src.hiringcafe import DEFAULT_QUERY, HiringCafeClient, fetch_jobs_multi

            class _CafeFetcher:
                def __init__(self, searches: list[tuple[str, str | None]]) -> None:
                    self._client = HiringCafeClient()
                    self._searches = searches

                async def fetch_jobs(self, client):
                    return await fetch_jobs_multi(self._client, client, self._searches)

            hc = cfg.sources.hiringcafe
            searches: list[tuple[str, str | None]] = [(DEFAULT_QUERY, hc.location)]
            for entry in hc.extra_queries:
                if isinstance(entry, HiringCafeSearch):
                    searches.append((entry.query, entry.location))
                else:
                    searches.append((entry, None))
            out.append(HiringCafeConnector(
                cafe=_CafeFetcher(searches),
                active_set=active_set,
                max_postings=hc.max_postings_per_cycle,
                sightings=sightings,
            ))

        if adz.enabled:
            if not (cfg.secrets.adzuna_app_id and cfg.secrets.adzuna_app_key):
                log.error(
                    "adzuna_missing_secrets — sources.adzuna.enabled is true but "
                    "JOB_AGG_ADZUNA_APP_ID / JOB_AGG_ADZUNA_APP_KEY are not set; "
                    "skipping the adzuna connector"
                )
            else:
                from src.connectors.adzuna import AdzunaConnector

                out.append(AdzunaConnector(
                    app_id=cfg.secrets.adzuna_app_id,
                    app_key=cfg.secrets.adzuna_app_key,
                    countries=adz.countries,
                    queries=adz.queries,
                    max_days_old=adz.max_days_old,
                    results_per_page=adz.results_per_page,
                    daily_call_budget=adz.daily_call_budget,
                    active_set=active_set,
                    sightings=sightings,
                ))

    elif tier == "headless":
        from src.connectors.avature import AvatureConnector
        out: list[Connector] = []
        seen: set[str] = set()
        for ab in cfg.sources.avature:
            conn = AvatureConnector(careers_url=ab.careers_url, company=ab.company)
            if conn.name in seen or conn.name in suppressed:
                continue
            seen.add(conn.name)
            out.append(conn)
        return out
    # tier == "discovery" returns []; the discovery routine doesn't use connectors.

    return out
