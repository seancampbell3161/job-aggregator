"""Enterprise ATS fingerprinting: locate a company's careers page, identify
the ATS behind it (including Workday/ORC triples that cannot be guessed from
a company name), and live-verify the identity. Matched boards merge into the
settings sources. Pure library — the CLI in scripts/discover_enterprise.py
and any future discovery-tier integration both import from here."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import AppConfig
from src.models import ConnectorState
from src.user_agent import headers as ua_headers

if TYPE_CHECKING:
    from src.settings.service import ConfigService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Seed:
    name: str
    domain: str


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEEDS = REPO_ROOT / "scripts" / "seeds" / "enterprise_companies.csv"
EU_SEEDS = REPO_ROOT / "scripts" / "seeds" / "eu_companies.csv"


def load_seeds(path: Path) -> list[Seed]:
    """Parse the seed CSV (header: name,domain). Domains must be bare
    (no scheme, no path). Malformed rows raise ValueError naming the line."""
    seeds: list[Seed] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["name", "domain"]:
            raise ValueError(f"{path}: expected header 'name,domain', got {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):  # line 1 is the header
            name = (row.get("name") or "").strip()
            domain = (row.get("domain") or "").strip().lower()
            if not name or not domain:
                raise ValueError(f"{path} line {i}: name and domain are both required")
            if domain.startswith("http") or "/" in domain or "." not in domain:
                raise ValueError(f"{path} line {i}: domain must be bare (e.g. acme.com), got {domain!r}")
            seeds.append(Seed(name=name, domain=domain))
    return seeds


_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_BARE_LOCALE_RE = re.compile(r"^[a-z]{2}$")

# Slug families: hostname → family. First path segment is the slug.
_SLUG_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "careers.smartrecruiters.com": "smartrecruiters",
    "ats.rippling.com": "rippling",
}

# Subdomain-slug families: the tenant slug is the hostname's first label
# ({slug}.recruitee.com), unlike _SLUG_HOSTS where it's the first path
# segment. Reserved labels are the vendors' own non-tenant hosts.
_SUBDOMAIN_SLUG_HOSTS = (
    (re.compile(r"^([\w-]+)\.recruitee\.com$"), "recruitee"),
    (re.compile(r"^([\w-]+)\.jobs\.personio\.(?:de|com)$"), "personio"),
    (re.compile(r"^([\w-]+)\.teamtailor\.com$"), "teamtailor"),
)
_RESERVED_SUBDOMAINS = frozenset(
    {"www", "app", "api", "docs", "blog", "help", "support", "career",
     "careers", "jobs", "auth", "status"}
)

# Unsupported families: hostname suffix → family (report-only, never merged).
_UNSUPPORTED_HOST_SUFFIXES = (
    (".icims.com", "icims"),
    (".eightfold.ai", "eightfold"),
    (".taleo.net", "taleo"),
    (".successfactors.com", "successfactors"),
    (".successfactors.eu", "successfactors"),
    (".avature.net", "avature"),
    (".phenompeople.com", "phenom"),
    (".phenom.com", "phenom"),
    (".talentbrew.com", "talentbrew"),
)

_WORKDAY_HOST_RE = re.compile(r"^([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_ORACLE_HOST_RE = re.compile(r"^([\w-]+)\.fa\.([\w-]+)\.oraclecloud\.com$")
_ORACLE_SITE_RE = re.compile(r"/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)")


def _path_segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def parse_ats_url(url: str) -> tuple[str, dict] | None:
    """Extract (family, identity) from a SUPPORTED ATS URL, else None.
    Identity is {"slug": ...} for slug families and
    {"tenant","region","site"} for workday/oraclecloud."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    m = _WORKDAY_HOST_RE.match(host)
    if m:
        tenant, region = m.group(1), m.group(2)
        segs = _path_segments(parsed.path)
        if segs and _LOCALE_RE.match(segs[0]):
            segs = segs[1:]  # skip en-US style locale prefix
        if segs and _BARE_LOCALE_RE.match(segs[0]):
            if len(segs) >= 2:
                segs = segs[1:]  # skip bare two-letter locale prefix (e.g. "es")
            else:
                return None  # bare locale-shaped segment alone is not a real site
        if not segs:
            return None  # bare tenant root: site unknown
        return "workday", {"tenant": tenant, "region": region, "site": segs[0]}

    m = _ORACLE_HOST_RE.match(host)
    if m:
        site_m = _ORACLE_SITE_RE.search(parsed.path)
        if not site_m:
            return None
        return "oraclecloud", {"tenant": m.group(1), "region": m.group(2), "site": site_m.group(1)}

    for pat, family in _SUBDOMAIN_SLUG_HOSTS:
        m = pat.match(host)
        if m:
            slug = m.group(1)
            if slug.lower() in _RESERVED_SUBDOMAINS:
                return None
            return family, {"slug": slug}

    family = _SLUG_HOSTS.get(host)
    if family:
        segs = _path_segments(parsed.path)
        if not segs:
            return None
        return family, {"slug": segs[0]}
    return None


_JSONLD_ICIMS_RE = re.compile(r"^([\w-]+)\.icims\.com$")


def parse_jsonld_url(url: str) -> tuple[str, dict] | None:
    """('jsonld', {family:'icims', slug, base_url}) for an iCIMS careers host,
    else None. SF/TalentBrew live on branded domains not classifiable from the
    host — those boards are hand-curated into jsonld_boards."""
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return None
    m = _JSONLD_ICIMS_RE.match(u.netloc.lower())
    if not m:
        return None
    sub = m.group(1)
    slug = sub.split("-", 1)[1] if "-" in sub else sub  # careers-steeldynamics -> steeldynamics
    return "jsonld", {"family": "icims", "slug": slug, "base_url": f"{u.scheme}://{u.netloc}"}


_EIGHTFOLD_HOST_RE = re.compile(r"^([\w-]+)\.eightfold\.ai$")
# Negative lookbehind stops this from matching inside a longer word (e.g.
# `subdomain=`) or an unrelated `?domain=` query param prefix.
_EIGHTFOLD_DOMAIN_RE = re.compile(r"(?<![\w])domain=([a-zA-Z0-9.\-]+)")


def parse_eightfold_url(url: str) -> tuple[str, dict] | None:
    """('eightfold', {slug, base}) for an *.eightfold.ai careers host, else
    None. domain= and flavor are resolved live during verify (not
    host-derivable)."""
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return None
    m = _EIGHTFOLD_HOST_RE.match(u.netloc.lower())
    if not m:
        return None
    return "eightfold", {"slug": m.group(1), "base": f"{u.scheme}://{u.netloc}"}


_TALEO_HOST_RE = re.compile(r"^([\w-]+)\.taleo\.net$")
_TALEO_SECTION_RE = re.compile(r"/careersection/([^/?#]+)")
# Non-board section names that appear in login/nav/API links, not a real
# careers board. First-match-wins candidate scanning would otherwise lock in
# one of these and never reach the real board link.
_TALEO_NON_BOARD_SECTIONS = {"iam", "rest"}


def parse_taleo_url(url: str) -> tuple[str, dict] | None:
    """('taleo', {tenant, section}) for a {tenant}.taleo.net/careersection/{section}/
    URL, else None. Modern-vs-legacy is decided by _verify_taleo (a live probe)."""
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return None
    m = _TALEO_HOST_RE.match(u.netloc.lower())
    sm = _TALEO_SECTION_RE.search(u.path or "")
    if not m or not sm:
        return None
    section = sm.group(1)
    if section.lower() in _TALEO_NON_BOARD_SECTIONS:
        return None
    return "taleo", {"tenant": m.group(1), "section": section}


def detect_unsupported(url: str) -> str | None:
    """Name the unsupported ATS family a URL points at, else None."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    for suffix, family in _UNSUPPORTED_HOST_SUFFIXES:
        if host.endswith(suffix) or host == suffix.lstrip("."):
            return family
    return None


def _headers() -> dict[str, str]:
    return ua_headers()
_TIMEOUT = 15.0
_CAREERS_LINK_RE = re.compile(r"career|jobs|join", re.IGNORECASE)


def _well_known_urls(domain: str) -> list[str]:
    return [
        f"https://{domain}/careers",
        f"https://{domain}/jobs",
        f"https://careers.{domain}",
        f"https://jobs.{domain}",
    ]


def _is_fingerprintable(url: str) -> bool:
    return parse_ats_url(url) is not None or detect_unsupported(url) is not None


async def locate_careers_urls(client: httpx.AsyncClient, domain: str) -> list[str]:
    """Candidate careers URLs for a domain: final URLs of well-known paths
    that respond, ATS links scanned out of those already-fetched bodies, then
    (only if nothing fingerprintable was found yet) careers-ish or ATS-host
    links scraped from the homepage. ≤4 well-known probes + ≤1 homepage
    fetch; failures skip that candidate."""
    out: list[str] = []
    for url in _well_known_urls(domain):
        try:
            resp = await client.get(url, headers=_headers(), timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001 — a dead candidate is just skipped
            continue
        if resp.status_code >= 400:
            continue
        final_url = str(resp.url)  # post-redirect final URL
        if final_url not in out:
            out.append(final_url)
        # The final URL can 200 with a landing page whose real ATS board is
        # only reachable via a link in the body (e.g. 3M's /careers embeds a
        # Workday link) — scan hrefs already downloaded, ZERO extra requests.
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            try:
                absolute = str(httpx.URL(final_url).join(a["href"]))
                if absolute not in out and _is_fingerprintable(absolute):
                    out.append(absolute)
            except Exception:  # noqa: BLE001 — malformed href in candidate's body skips only that href
                continue

    if any(_is_fingerprintable(u) for u in out):
        return out  # already fingerprintable — skip the homepage fetch

    try:
        resp = await client.get(f"https://{domain}", headers=_headers(), timeout=_TIMEOUT)
        if resp.status_code < 400:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(" ", strip=True)
                absolute = str(httpx.URL(str(resp.url)).join(href))
                if _CAREERS_LINK_RE.search(href) or _CAREERS_LINK_RE.search(text) or _is_fingerprintable(absolute):
                    if absolute not in out:
                        out.append(absolute)
    except Exception:  # noqa: BLE001
        pass
    return out


@dataclass(frozen=True)
class FingerprintResult:
    name: str
    domain: str
    status: str            # "matched" | "unsupported" | "not_found" | "error"
    family: str | None = None
    identity: dict | None = None
    posting_count: int = 0
    evidence_url: str | None = None
    note: str | None = None


async def _verify(client: httpx.AsyncClient, family: str, identity: dict) -> int:
    """One live check of a supported identity. Returns the posting count
    (0 = verified but empty). Exceptions propagate to the caller's guard."""
    if family in ("workday", "oraclecloud"):
        if family == "workday":
            from src.connectors.workday import WorkdayConnector as Conn
        else:
            from src.connectors.oraclecloud import OracleCloudConnector as Conn
        conn = Conn(tenant=identity["tenant"], region=identity["region"], site=identity["site"])
        result = await conn.fetch(client, ConnectorState())
        return len(result.postings)
    from src.discovery import _probe_one_ats
    ok, count = await _probe_one_ats(client=client, ats_family=family, slug=identity["slug"])
    return count if ok else 0


async def _verify_jsonld(client: httpx.AsyncClient, identity: dict) -> int:
    """Live check: build the connector, confirm >=1 coarse posting."""
    from src.connectors.jsonld import JsonLdBoardConnector
    conn = JsonLdBoardConnector(
        family=identity["family"], slug=identity["slug"], base_url=identity["base_url"],
    )
    result = await conn.fetch(client, ConnectorState())
    return len(result.postings)


async def _verify_eightfold(client: httpx.AsyncClient, identity: dict) -> int:
    """Scrape domain= off the /careers page, probe the flavor, fetch. Mutates
    identity with the resolved domain+flavor so the merged config carries
    them. Returns the posting count (0 when domain can't be resolved or the
    board is empty → the CLI won't emit it)."""
    from src.connectors.eightfold import EightfoldConnector
    base = identity["base"]
    try:
        resp = await client.get(f"{base}/careers", headers=_headers(), timeout=20.0)
        m = _EIGHTFOLD_DOMAIN_RE.search(resp.text)
    except Exception:  # noqa: BLE001 — a fetch failure means we can't resolve; report 0
        return 0
    if not m:
        # Spec: this case is "unsupported, no scrapeable domain" — not a
        # verified-empty board. We stay within the int-return contract (the
        # caller still treats 0 as not_found), but log distinctly so an
        # operator reviewing a dry-run can tell the two apart.
        log.info("eightfold_no_domain", extra={"base": base})
        return 0
    identity["domain"] = m.group(1)
    # Probe each flavor via _fetch_flavor DIRECTLY — not conn.fetch(), which
    # has its own internal 403 flavor-fallback and would silently return
    # postings via that fallback even when the probed flavor is wrong for
    # this tenant (e.g. probing pcsx against a real apply_v2 tenant), causing
    # the wrong flavor to be recorded. _fetch_flavor raises _FlavorMismatch on
    # a genuine flavor-403 with no fallback, so the except below correctly
    # advances the loop and records whichever flavor actually returned
    # postings.
    for flavor in ("pcsx", "apply_v2"):
        conn = EightfoldConnector(slug=identity["slug"], domain=identity["domain"], flavor=flavor)
        try:
            postings = await conn._fetch_flavor(client, flavor)
        except Exception:  # noqa: BLE001 — try the other flavor
            continue
        if postings:
            identity["flavor"] = flavor
            return len(postings)
    identity.setdefault("flavor", "pcsx")
    return 0


async def _verify_taleo(client: httpx.AsyncClient, identity: dict) -> int:
    """Live check: build the connector and fetch. Modern tenants return >=1;
    legacy (searchjobs fails/empty) and migrated (no portalNo) return 0, so the
    CLI never emits them."""
    from src.connectors.taleo import TaleoConnector
    conn = TaleoConnector(tenant=identity["tenant"], section=identity["section"])
    try:
        result = await conn.fetch(client, ConnectorState())
    except Exception:  # noqa: BLE001 — a failed fetch means not-modern → 0
        return 0
    return len(result.postings)


async def verify_identity(client: httpx.AsyncClient, family: str, identity: dict) -> int:
    """One live check of a supported (family, identity) — the single dispatch
    point shared by fingerprint_company and board-candidate validation.
    Returns the posting count (0 = verified-empty or unresolvable). MUTATES
    identity for eightfold (resolves domain+flavor). The workday/oraclecloud/
    slug branch propagates exceptions to the caller's guard."""
    if family == "jsonld":
        return await _verify_jsonld(client, identity)
    if family == "eightfold":
        return await _verify_eightfold(client, identity)
    if family == "taleo":
        return await _verify_taleo(client, identity)
    return await _verify(client, family, identity)


async def fingerprint_company(client: httpx.AsyncClient, seed: Seed) -> FingerprintResult:
    """Locate → fingerprint → verify one company. Never raises: any escaping
    exception becomes status='error'."""
    try:
        candidates = await locate_careers_urls(client, seed.domain)

        supported: list[tuple[str, dict, str]] = []   # (family, identity, url)
        unsupported: list[tuple[str, str]] = []        # (family, url)
        for url in candidates:
            parsed = parse_ats_url(url)
            if parsed and not any(parsed[0] == f for f, _, _ in supported):
                supported.append((parsed[0], parsed[1], url))
                continue
            jsonld_parsed = parse_jsonld_url(url)
            if jsonld_parsed:
                # A second same-host jsonld URL (e.g. an iCIMS nav/login link
                # surfaced by the body scan) must be absorbed, not demoted to
                # unsupported — otherwise it trips the ambiguous/ unsupported
                # branch below and the probe never runs.
                if not any(f == "jsonld" for f, _, _ in supported):
                    supported.append((jsonld_parsed[0], jsonld_parsed[1], url))
                continue
            eightfold_parsed = parse_eightfold_url(url)
            if eightfold_parsed:
                # Same absorb rule as jsonld: a second .eightfold.ai URL (e.g.
                # a featured-job link off the landing page) must be absorbed
                # into the existing match, not fall through to
                # detect_unsupported and get demoted to "unsupported".
                if not any(f == "eightfold" for f, _, _ in supported):
                    supported.append((eightfold_parsed[0], eightfold_parsed[1], url))
                continue
            taleo_parsed = parse_taleo_url(url)
            if taleo_parsed:
                # Same absorb rule as jsonld/eightfold: a second .taleo.net
                # URL (e.g. a login/nav link surfaced by the body scan) must
                # be absorbed into the existing match, not fall through to
                # detect_unsupported and get demoted to "unsupported".
                if not any(f == "taleo" for f, _, _ in supported):
                    supported.append((taleo_parsed[0], taleo_parsed[1], url))
                continue
            fam = detect_unsupported(url)
            if fam and not any(fam == f for f, _ in unsupported):
                unsupported.append((fam, url))

        families_seen = [f for f, _, _ in supported] + [f for f, _ in unsupported]
        if len(supported) == 1 and not unsupported:
            family, identity, url = supported[0]
            count = await verify_identity(client, family, identity)
            if count > 0:
                return FingerprintResult(
                    name=seed.name, domain=seed.domain, status="matched",
                    family=family, identity=identity, posting_count=count,
                    evidence_url=url,
                )
            return FingerprintResult(
                name=seed.name, domain=seed.domain, status="not_found",
                family=family, identity=identity, evidence_url=url,
                note="fingerprinted but board verified empty",
            )
        if supported or unsupported:
            # Ambiguous (multiple families) or unsupported-only: report, don't merge.
            fam, url = (unsupported or [(supported[0][0], supported[0][2])])[0]
            note = None
            if len(families_seen) > 1:
                note = f"multiple ATS fingerprints seen: {', '.join(sorted(set(families_seen)))}"
            return FingerprintResult(
                name=seed.name, domain=seed.domain, status="unsupported",
                family=fam, evidence_url=url, note=note,
            )
        return FingerprintResult(name=seed.name, domain=seed.domain, status="not_found")
    except Exception as exc:  # noqa: BLE001 — per-company fail-soft
        log.warning("fingerprint_failed", extra={"domain": seed.domain, "error": str(exc)})
        return FingerprintResult(
            name=seed.name, domain=seed.domain, status="error",
            note=f"{type(exc).__name__}: {exc}",
        )


async def fingerprint_many(
    seeds: list[Seed], *, concurrency: int = 5, client_factory=None,
) -> list[FingerprintResult]:
    """Sweep many companies with a concurrency cap. One shared client;
    per-company failures are contained by fingerprint_company."""
    factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=True))
    sem = asyncio.Semaphore(concurrency)
    async with factory() as client:
        async def _one(seed: Seed) -> FingerprintResult:
            async with sem:
                return await fingerprint_company(client, seed)
        return list(await asyncio.gather(*(_one(s) for s in seeds)))


def connector_name(result: FingerprintResult) -> str:
    ident = result.identity or {}
    if result.family in ("workday", "oraclecloud"):
        return f"{result.family}:{ident['tenant']}:{ident['site']}"
    if result.family == "jsonld":
        return f"{ident['family']}:{ident['slug']}"
    if result.family == "eightfold":
        return f"eightfold:{ident['slug']}"
    if result.family == "taleo":
        return f"taleo:{ident['tenant']}:{ident['section']}"
    return f"{result.family}:{ident['slug']}"


def config_entry(result: FingerprintResult) -> tuple[str, str | dict]:
    """(sources key, entry) for one matched result, in the shape SourcesConfig
    expects under that key. jsonld results go under jsonld_boards."""
    ident = result.identity or {}
    if result.family in ("workday", "oraclecloud"):
        entry: dict = {"tenant": ident["tenant"], "region": ident["region"], "site": ident["site"]}
        if result.family == "oraclecloud":
            entry["company"] = result.name
        return result.family, entry
    if result.family == "jsonld":
        return "jsonld_boards", {"family": ident["family"], "slug": ident["slug"],
                                 "base_url": ident["base_url"], "company": result.name}
    if result.family == "eightfold":
        return "eightfold", {"slug": ident["slug"], "domain": ident["domain"],
                             "flavor": ident.get("flavor", "pcsx"), "company": result.name}
    if result.family == "taleo":
        # str(): a bare-number section ("2") must stay a string for TaleoBoard.
        return "taleo", {"tenant": ident["tenant"], "section": str(ident["section"]),
                         "company": result.name}
    return result.family, ident["slug"]


def gather_already_polled(cfg: AppConfig) -> set[str]:
    """Connector names for every source in a settings config.

    Delegates to src.settings.boards for the one naming rule shared with
    build_connectors, connector_health and discovered_slugs — this used to
    build 14 of the 16 families' names inline and missed phenom/avature."""
    from src.settings.boards import board_entries

    return {entry.key for entry in board_entries(cfg)}


def merge_results_into_settings(
    service: "ConfigService", results: list[FingerprintResult], *, label: str,
    already_polled: Iterable[str] = (),
) -> tuple[int, str]:
    """Append matched results to the settings' sources as ONE new version
    (source=cli, note "<label>: merged N entries"), deduped against the sources
    already in settings, ``already_polled`` (discovered/suppressed connector
    names), and within the batch. A concurrent save is retried once
    (ConfigService.update_settings). Returns (added, summary lines).
    Raises NotConfigured before setup."""
    extra = set(already_polled)
    added: list[str] = []

    def mutate(doc: dict) -> str | None:
        added.clear()  # may run twice on a StaleWrite retry
        seen = gather_already_polled(AppConfig.model_validate(doc)) | extra
        sources = doc.setdefault("sources", {})
        for r in results:
            if r.status != "matched" or not r.family or not r.identity:
                continue
            name = connector_name(r)
            if name in seen:
                continue
            family, entry = config_entry(r)
            sources.setdefault(family, []).append(entry)
            seen.add(name)
            added.append(name)
        return f"{label}: merged {len(added)} entries" if added else None

    service.update_settings(mutate, source="cli")
    return len(added), "\n".join(f"  + {name}" for name in added)


def _store_names_fail_soft() -> set[str]:
    """Discovered-slug + suppressed connector names, or empty set if the local
    stores aren't reachable (e.g. no data dir on this machine)."""
    try:
        from src.stores import build_stores
        stores = build_stores()
        names = {row.connector_name for row in stores.discovered.list_all()}
        names |= set(stores.health.suppressed_names())
        return names
    except Exception as exc:  # noqa: BLE001 — dedupe degrades to config-only
        print(f"note: local stores unavailable ({type(exc).__name__}) — deduping against config only")
        return set()


def format_report(results: list[FingerprintResult]) -> str:
    order = ("matched", "unsupported", "not_found", "error")
    grouped = {s: [r for r in results if r.status == s] for s in order}
    out: list[str] = []
    for status in order:
        rows = grouped[status]
        out.append(f"\n=== {status.upper()} ({len(rows)}) ===")
        for r in sorted(rows, key=lambda x: x.name.lower()):
            if status == "matched":
                out.append(f"  {r.name:<30} {connector_name(r):<45} {r.posting_count:>4} postings")
                family, entry = config_entry(r)
                out.append(f"    sources.{family}: {json.dumps(entry)}")
            elif status == "unsupported":
                note = f"  ({r.note})" if r.note else ""
                out.append(f"  {r.name:<30} {r.family:<15} {r.evidence_url or ''}{note}")
            else:
                out.append(f"  {r.name:<30} {r.note or ''}")
    return "\n".join(out)
