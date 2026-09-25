"""Discovery routine: pulls yc-oss + manual_companies, derives slugs, probes
each across all supported ATS families (_SUPPORTED_ATS) concurrently, and
upserts the highest-posting-count winner. Also re-validates stale ok rows
weekly."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from src.fingerprint import Seed, connector_name, fingerprint_company, verify_identity
from src.models import ConnectorState
from src.slugging import labeled_slug_candidates, seed_domain, slug_candidates
from src.starter_pack import polled
from src.state import DiscoveredSlug, no_match_exhausted
from src.state_sqlite import SqliteDiscoveredSlugsStore
from src.user_agent import headers as ua_headers
from src.yc_oss import YcOssClient

log = logging.getLogger(__name__)

# Order matters: stable iteration for deterministic logging.
_SUPPORTED_ATS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    "rippling",
    "recruitee",
    "personio",
    "teamtailor",
)
_PROBES_PER_CANDIDATE = len(_SUPPORTED_ATS)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Runtime config for the discovery routine. Distinct from the Pydantic
    ``src.config.DiscoveryConfig``: that one validates YAML; this one is a
    small argument bundle."""
    max_validations_per_run: int = 500
    quarantine_after_failures: int = 5
    manual_companies: list[str] = field(default_factory=list)
    revalidate_after_days: int = 7
    no_match_revalidate_after_days: int = 90
    yc_oss_enabled: bool = True
    revalidate_reserve: int = 100
    board_quarantine_after_failures: int = 5


async def _probe_one_ats(
    *,
    client: httpx.AsyncClient,
    ats_family: str,
    slug: str,
) -> tuple[bool, int]:
    """Probe a single ATS endpoint. Returns (ok, posting_count)."""
    url_for = {
        "greenhouse": (f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "GET", "json", "jobs"),
        "lever": (f"https://api.lever.co/v0/postings/{slug}", "GET", "json", None),
        "ashby": (f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "GET", "json", "jobs"),
        "workable": (f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", "POST", "json", "results"),
        "smartrecruiters": (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", "GET", "json", "content"),
        "rippling": (f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs", "GET", "json", None),
        # EU families. Personio probes .de only: unknown slugs 307-redirect to
        # the marketing site (so "xml" kind requires a direct 200), and the
        # rare .com-only tenant still converts via the fingerprint path.
        "recruitee": (f"https://{slug}.recruitee.com/api/offers/", "GET", "json", "offers"),
        "personio": (f"https://{slug}.jobs.personio.de/xml", "GET", "xml", "position"),
        "teamtailor": (f"https://{slug}.teamtailor.com/jobs.rss", "GET", "xml", "item"),
    }
    if ats_family not in url_for:
        return False, 0
    url, method, kind, key = url_for[ats_family]
    try:
        if method == "POST":
            resp = await client.post(url, json={}, headers=ua_headers(), timeout=20.0)
        else:
            resp = await client.get(url, headers=ua_headers(), timeout=20.0)
        if kind == "xml":
            # Redirects (3xx) mean "not a tenant" for these hosts — only a
            # direct 200 with ≥1 element counts.
            if resp.status_code != 200:
                return False, 0
            root = ET.fromstring(resp.text)
            count = sum(1 for _ in root.iter(key))
            return (count > 0), count
        if resp.status_code >= 400:
            return False, 0
        data = resp.json()
        # Count postings. Some ATSs (notably SmartRecruiters) return 200 OK with
        # an empty list for ANY slug, not just real accounts — so we require
        # ≥1 posting to count as a real match. A truly quiet legitimate company
        # falls through to no_match and is re-probed after the 90-day expiry.
        count = 0
        if key is None and isinstance(data, list):
            count = len(data)
        elif isinstance(data, dict):
            v = data.get(key or "")
            if isinstance(v, list):
                count = len(v)
        return (count > 0), count
    except Exception:  # noqa: BLE001 — defensive; a single ATS error must not crash the cycle
        log.exception("probe_ats_failed", extra={"ats": ats_family, "slug": slug})
        return False, 0


async def _probe_all_ats(
    client: httpx.AsyncClient,
    slug: str,
) -> tuple[str, int] | None:
    """Probe a slug across all supported ATS families (_SUPPORTED_ATS)
    concurrently. Returns (winning_ats_family, posting_count) for the
    highest-count match, or None if no ATS returned ok."""
    results = await asyncio.gather(
        *(_probe_one_ats(client=client, ats_family=ats, slug=slug) for ats in _SUPPORTED_ATS)
    )
    candidates = [
        (ats, count) for ats, (ok, count) in zip(_SUPPORTED_ATS, results) if ok
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])


async def _revalidate_one(
    *,
    client: httpx.AsyncClient,
    row: DiscoveredSlug,
    store: SqliteDiscoveredSlugsStore,
    quarantine_threshold: int,
) -> None:
    """Re-probe a single previously-ok row's ats:slug. Update on success,
    increment failures (and possibly quarantine) on failure."""
    ok, count = await _probe_one_ats(
        client=client, ats_family=row.ats_family, slug=row.slug
    )
    if ok:
        store.upsert_ok(
            row.connector_name,
            company_name=row.company_name,
            last_posting_count=count,
        )
    else:
        store.upsert_failed(
            row.connector_name,
            quarantine_threshold=quarantine_threshold,
        )


async def _validate_slug_candidate(*, client, row, store, budget: int) -> int:
    """Probe a staged slug candidate: claimed family first (1 probe), the
    remaining families on a miss (budget-charged). Resolves the staged row —
    ok (same or re-keyed family) or no_match in place. Returns remaining
    budget; if the budget can't cover the fallback, the row is left staged
    for the next run (the chain is idempotent)."""
    claimed = row.claimed_family or row.ats_family
    ok, count = await _probe_one_ats(client=client, ats_family=claimed, slug=row.slug)
    budget -= 1
    if ok:
        store.upsert_ok(f"{claimed}:{row.slug}", company_name=row.company_name,
                        last_posting_count=count, origin=row.origin)
        return budget
    others = [a for a in _SUPPORTED_ATS if a != claimed]
    if budget < len(others):
        return budget  # out of budget mid-candidate: retry next run
    results = await asyncio.gather(
        *(_probe_one_ats(client=client, ats_family=a, slug=row.slug) for a in others)
    )
    budget -= len(others)
    candidates = [(a, c) for a, (ok2, c) in zip(others, results) if ok2]
    if candidates:
        fam, count = max(candidates, key=lambda x: x[1])
        store.delete(row.connector_name)  # re-key: claimed family was wrong
        store.upsert_ok(f"{fam}:{row.slug}", company_name=row.company_name,
                        last_posting_count=count, origin=row.origin)
    else:
        store.resolve_candidate_no_match(row.connector_name)
        store.upsert_no_match(row.slug)
    return budget


async def _validate_board_candidate(*, client, row, boards, quarantine_threshold: int) -> bool:
    """Live-verify a staged board candidate via the shared fingerprint
    dispatcher (mutates identity for eightfold: resolves domain+flavor).
    ≥1 posting → ok; anything else → failure streak. Returns True on promote."""
    identity = dict(row.identity or {})
    try:
        count = await verify_identity(client, row.family, identity)
    except Exception:  # noqa: BLE001 — transient verify errors streak, not crash
        count = 0
    if count > 0:
        from src.connectors.base import connector_from_identity
        conn = connector_from_identity(row.family, identity, row.company or row.name)
        if conn is not None:
            boards.upsert_ok(row.domain, name=row.name, family=row.family,
                             identity=identity, connector_name=conn.name,
                             company=row.company, origin=row.origin)
            return True
    boards.upsert_candidate_failed(row.domain, quarantine_threshold=quarantine_threshold)
    return False


async def _run_candidate_chain(
    *,
    client,
    name: str,
    website: str | None,
    alt_slug: str | None,
    store,
    boards,
    active_set: set[tuple[str, str]],
    budget: int,
    origin: str | None = None,
) -> tuple[int, str]:
    """Run the full conversion chain for one name-based candidate: each slug
    variant across every supported family (_PROBES_PER_CANDIDATE probes per
    variant), then the careers-page fingerprint when a website exists
    (charged as _PROBES_PER_CANDIDATE). Persists exactly one
    outcome — upsert_ok / boards.upsert_ok / upsert_no_match-with-methods —
    or nothing on budget cut-off (the chain is idempotent; the candidate
    re-runs from the top next cycle). Returns (remaining_budget, outcome),
    outcome ∈ {"ok", "board_ok", "no_match", "budget", "active", "skipped"}.

    A match is discovery's own confirmation: it is written with ``origin``
    (None for yc-oss/manual, the staged row's for a VC candidate), which
    reclaims a gated-off starter row under the same key."""
    labeled = labeled_slug_candidates(name, website, alt_slug)
    if not labeled:
        return budget, "skipped"
    # A company already polled under ANY variant needs no probing at all.
    for _, variant in labeled:
        if any((ats, variant) in active_set for ats in _SUPPORTED_ATS):
            return budget, "active"

    domain = seed_domain(website) if website else None
    # A company already converted to a structured board needs no probing
    # either: an ok row for the seed domain means a prior chain run or the
    # enterprise sweep already resolved this company; quarantined means the
    # board repeatedly failed verification — re-fingerprinting would thrash.
    # A gated-off starter board is not polled, so it does not count.
    if boards is not None and domain:
        brow = boards.get(domain)
        if brow is not None and (brow.status == "quarantined"
                                 or (brow.status == "ok" and polled(boards, brow))):
            return budget, "active"

    methods_tried: list[str] = []
    for kind, variant in labeled:
        if budget < _PROBES_PER_CANDIDATE:
            return budget, "budget"
        winner = await _probe_all_ats(client, variant)
        budget -= _PROBES_PER_CANDIDATE
        if winner is not None:
            fam, count = winner
            store.upsert_ok(f"{fam}:{variant}", company_name=name,
                            last_posting_count=count, origin=origin)
            return budget, "ok"
        methods_tried.append(f"{kind}:{variant}")

    if domain:
        if budget < _PROBES_PER_CANDIDATE:
            return budget, "budget"
        result = await fingerprint_company(client, Seed(name=name, domain=domain))
        budget -= _PROBES_PER_CANDIDATE  # flat charge per spec: fingerprint = _PROBES_PER_CANDIDATE
        if result.status == "matched":
            if result.family in _SUPPORTED_ATS:
                store.upsert_ok(
                    f"{result.family}:{result.identity['slug']}",
                    company_name=name, last_posting_count=result.posting_count,
                    origin=origin,
                )
                return budget, "ok"
            if boards is not None:
                boards.upsert_ok(
                    domain, name=name, family=result.family,
                    identity=result.identity, connector_name=connector_name(result),
                    company=name, origin=origin,
                )
                return budget, "board_ok"
            # Structured match but no boards store (caller without a boards
            # store): nothing can hold the identity — record the definitive
            # slug-side miss so the row doesn't retry the fingerprint forever.
            methods_tried.append("fingerprint")
        elif result.status in ("not_found", "unsupported"):
            methods_tried.append("fingerprint")  # definitive miss → exhausted
        # status == "error": transient — leave "fingerprint" untried so the
        # row stays non-exhausted and the fallback retries after expiry.

    primary = labeled[0][1]
    store.upsert_no_match(primary, company_name=name, website=website,
                          methods_tried=methods_tried)
    return budget, "no_match"


async def run_discovery(
    *,
    client: httpx.AsyncClient,
    yc_oss,  # has .fetch(client) -> list[YcCompany]
    active_set: set[tuple[str, str]],
    store: SqliteDiscoveredSlugsStore,
    cfg: DiscoveryConfig,
    boards=None,
) -> None:
    """One pass, priority-ordered against one probe budget: revalidation
    reserve carved out first, then sighted slug candidates, board candidates,
    the yc-oss crawl, and finally revalidation (reserve + leftovers)."""

    # ---------- Phase 1: source aggregation ----------
    if cfg.yc_oss_enabled:
        yc_companies = await yc_oss.fetch(client)
        log.info("discovery_yc_oss_fetched", extra={"count": len(yc_companies)})
    else:
        yc_companies = []
        log.info("discovery_yc_oss_skipped", extra={"reason": "disabled in config"})

    # Build primary_slug → (display_name, website, alt_slug) chain inputs.
    # Manual list takes precedence on dedup so the user's curated entries
    # control display name in the rare case yc-oss + manual produce the same
    # slug. Manual entries are literal slugs: pass them as BOTH the name and
    # the alt variant, so the literal string is always probed even when
    # normalization would alter it (e.g. a trailing "-corp").
    candidates: dict[str, tuple[str, str | None, str | None]] = {}
    for entry in cfg.manual_companies:
        slug = entry.strip().lower()
        if not slug:
            continue
        variants = slug_candidates(entry, None, slug)
        candidates[variants[0]] = (entry, None, slug)
    for c in yc_companies:
        variants = slug_candidates(c.name, c.website, c.slug)
        if not variants:
            continue
        primary = variants[0]
        if primary in candidates:
            continue  # manual already won
        candidates[primary] = (c.name, c.website, c.slug)

    log.info("discovery_candidates_total", extra={"count": len(candidates)})

    # ---------- Phase 2a: budget + revalidation reserve ----------
    reserve = min(cfg.revalidate_reserve, cfg.max_validations_per_run)
    budget = cfg.max_validations_per_run - reserve

    # ---------- Phase 2b: sighted slug candidates (strongest signal) ----------
    drained = {"slug_ok": 0, "slug_no_match": 0, "board_ok": 0, "board_failed": 0,
               "name_ok": 0, "name_board_ok": 0, "name_no_match": 0, "name_moot": 0}
    for row in sorted(store.list_candidates(), key=lambda r: r.sighted_at or r.discovered_at):
        if budget < 1:
            break
        if row.ats_family == "candidate":
            # Name-based staging row (VC capture): no claimed family, so it
            # runs the full conversion chain. The chain persists the
            # resolution under its own keys; the staging row is deleted on
            # ANY resolution so it leaves the queue. alt_slug=row.slug keeps
            # the originally staged slug probed even if name derivation
            # drifts (deduped away when identical, as it is today).
            budget, outcome = await _run_candidate_chain(
                client=client, name=row.company_name or row.slug,
                website=row.website, alt_slug=row.slug, store=store,
                boards=boards, active_set=active_set, budget=budget,
                origin=row.origin,
            )
            if outcome == "budget":
                break  # nothing persisted; the row re-drains next run
            store.delete(row.connector_name)
            if outcome == "ok":
                drained["name_ok"] += 1
            elif outcome == "board_ok":
                drained["name_board_ok"] += 1
            elif outcome == "no_match":
                drained["name_no_match"] += 1
            else:  # "active" / "skipped": already polled or underivable — moot
                drained["name_moot"] += 1
            continue
        before = budget
        budget = await _validate_slug_candidate(client=client, row=row, store=store, budget=budget)
        resolved = store.get(row.connector_name)
        if resolved is not None and resolved.validation_status == "candidate":
            continue  # budget cut-off mid-candidate; probes spent, row left staged
        if resolved is None or resolved.validation_status == "ok":
            drained["slug_ok"] += 1
        else:
            drained["slug_no_match"] += 1

    # ---------- Phase 2c: sighted board candidates ----------
    if boards is not None:
        for brow in sorted(boards.list_candidates(), key=lambda b: b.sighted_at or b.last_swept_at):
            if budget < _PROBES_PER_CANDIDATE:
                break
            promoted = await _validate_board_candidate(
                client=client, row=brow, boards=boards,
                quarantine_threshold=cfg.board_quarantine_after_failures,
            )
            budget -= _PROBES_PER_CANDIDATE
            drained["board_ok" if promoted else "board_failed"] += 1

    log.info("discovery_candidates_drained", extra=drained)

    # ---------- Phase 2: probe-and-classify via the conversion chain ----------
    candidates_done = 0
    no_match_count = 0
    ok_count = 0
    board_ok_count = 0
    skipped_active = 0
    skipped_no_match = 0
    skipped_exhausted = 0

    for primary, (display_name, website, alt_slug) in candidates.items():
        if budget < _PROBES_PER_CANDIDATE:
            break
        nomatch_row = store.get(f"nomatch:{primary}")
        if nomatch_row is not None:
            if store.is_recent_no_match(
                primary, fresh_within_days=cfg.no_match_revalidate_after_days
            ):
                skipped_no_match += 1
                continue
            if no_match_exhausted(nomatch_row):
                # Expired but the whole chain already missed: drains in
                # Phase 4 only, so it never crowds out fresh candidates.
                skipped_exhausted += 1
                continue
        budget, outcome = await _run_candidate_chain(
            client=client, name=display_name, website=website, alt_slug=alt_slug,
            store=store, boards=boards, active_set=active_set, budget=budget,
        )
        if outcome == "active":
            skipped_active += 1
            continue
        if outcome == "budget":
            break  # mid-chain cut-off: nothing persisted, re-runs next cycle
        if outcome == "skipped":
            continue
        candidates_done += 1
        if outcome == "ok":
            ok_count += 1
        elif outcome == "board_ok":
            board_ok_count += 1
        else:
            no_match_count += 1

    log.info(
        "discovery_probe_phase_done",
        extra={
            "candidates_probed": candidates_done,
            "ok": ok_count,
            "board_ok": board_ok_count,
            "no_match": no_match_count,
            "skipped_active": skipped_active,
            "skipped_no_match": skipped_no_match,
            "skipped_exhausted": skipped_exhausted,
            "budget_remaining": budget,
        },
    )

    # ---------- Phase 3: revalidation ----------
    reval_budget = budget + reserve
    if reval_budget <= 0:
        log.info("discovery_revalidation_skipped", extra={"reason": "budget exhausted"})
        return

    stale = store.list_for_revalidation(
        stale_after_days=cfg.revalidate_after_days,
        limit=reval_budget,
    )
    for row in stale:
        await _revalidate_one(
            client=client, row=row, store=store,
            quarantine_threshold=cfg.quarantine_after_failures,
        )

    log.info(
        "discovery_revalidation_done",
        extra={"revalidated": len(stale), "budget_remaining": reval_budget - len(stale)},
    )

    # ---------- Phase 4: exhausted no_match drain (lowest priority) ----------
    # The treadmill still turns — companies do adopt new ATSs — but exhausted
    # rows only spend budget nobody else claimed.
    leftover = reval_budget - len(stale)
    if leftover < _PROBES_PER_CANDIDATE:
        return
    expiry = datetime.now(timezone.utc) - timedelta(
        days=cfg.no_match_revalidate_after_days
    )
    expired_exhausted: list[tuple[datetime, DiscoveredSlug]] = []
    for row in store.list_no_match():
        if not no_match_exhausted(row) or not row.last_validated_at:
            continue
        try:
            dt = datetime.fromisoformat(row.last_validated_at)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < expiry:
            expired_exhausted.append((dt, row))
    expired_exhausted.sort(key=lambda p: p[0])  # oldest first

    drained_exhausted = 0
    for _, row in expired_exhausted:
        if leftover < _PROBES_PER_CANDIDATE:
            break
        leftover, outcome = await _run_candidate_chain(
            client=client, name=row.company_name or row.slug, website=row.website,
            alt_slug=None, store=store, boards=boards, active_set=active_set,
            budget=leftover,
        )
        if outcome == "budget":
            break
        if outcome in ("active", "skipped"):
            # zero probes spent and nothing persisted — refresh the timer so
            # the row leaves the drain queue for another expiry cycle
            # (plain upsert preserves learned fields per preserve-on-None)
            store.upsert_no_match(row.slug)
            continue
        drained_exhausted += 1
    log.info(
        "discovery_exhausted_drain_done",
        extra={"drained": drained_exhausted, "budget_remaining": leftover},
    )


async def run_board_discovery(
    *, client, seeds, boards,
    max_sweeps_per_run: int, revalidate_after_days: int, quarantine_threshold: int,
) -> None:
    """One budgeted pass of the enterprise fingerprint sweep. Never-swept seeds
    first, then stale ones; matched → boards.upsert_ok, else upsert_result.
    Per-seed fail-soft (fingerprint_company contains its own errors)."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    stale_iso = (now - timedelta(days=revalidate_after_days)).isoformat()
    existing = {b.domain: b for b in boards.list_all()}

    fresh: list = []   # never swept — priority
    stale: list = []
    for seed in seeds:
        row = existing.get(seed.domain)
        if row is None:
            fresh.append(seed)
        elif row.status == "quarantined":
            continue
        elif row.last_swept_at < stale_iso:
            stale.append(seed)
    selected = (fresh + stale)[:max_sweeps_per_run]

    swept = matched = unsupported = not_found = errors = 0
    for seed in selected:
        result = await fingerprint_company(client, seed)
        swept += 1
        if result.status == "matched":
            # The sweep's own match: reclaims a starter row for this domain.
            boards.upsert_ok(
                seed.domain, name=seed.name, family=result.family,
                identity=result.identity, connector_name=connector_name(result),
                company=seed.name, origin=None,
            )
            matched += 1
        else:
            prior = existing.get(seed.domain)
            if prior is not None and prior.status == "ok":
                # was a live board; a transient re-fingerprint miss must not demote it.
                # poll-health suppresses it if it's actually dead at poll time.
                continue
            boards.upsert_result(
                seed.domain, name=seed.name, status=result.status,
                quarantine_threshold=quarantine_threshold,
            )
            if result.status == "unsupported":
                unsupported += 1
            elif result.status == "error":
                errors += 1
            else:
                not_found += 1
    log.info("board_discovery_done", extra={
        "swept": swept, "matched": matched, "unsupported": unsupported,
        "not_found": not_found, "errors": errors,
    })


def _known(store, row) -> bool:
    """A row that dedups a VC capture: any row, except a gated-off starter
    row — that company is not polled, so discovery may still find it."""
    return row is not None and polled(store, row)


def _vc_watermark_fresh(last_modified: str | None, refresh_days: int) -> bool:
    """True iff the vc:{firm} watermark timestamp is within refresh_days.
    Missing or unparseable → stale, so the fetch runs."""
    if not last_modified:
        return False
    try:
        dt = datetime.fromisoformat(last_modified)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc) - timedelta(days=refresh_days)


async def run_vc_discovery(
    *,
    client,
    firms,
    discovered,
    boards,
    source_state,
    active_set: set[tuple[str, str]],
    refresh_days: int,
    capture_cap: int,
    no_match_fresh_days: int,
) -> None:
    """Weekly VC-portfolio fetch → candidate staging rows (spec part 3).
    Zero probing: each portfolio company becomes at most one
    ``candidate:{primary_slug}`` row in discovered_slugs; validation happens
    later on the discovery tier's normal budget (Phase 2b conversion chain).
    Per-firm and per-entry fail-soft; a failed or EMPTY fetch does not
    advance the ``vc:{firm}`` watermark, so it retries the next daily run."""
    # In-function import: src.vc_portfolio imports _probe_one_ats from this
    # module, so a top-level import here would be circular.
    from src.vc_portfolio import a16z_portfolio, sequoia_portfolio

    drivers = {"a16z": a16z_portfolio, "sequoia": sequoia_portfolio}
    for firm in firms:
        fetcher = drivers.get(firm)
        if fetcher is None:
            log.warning("vc_unknown_firm", extra={"firm": firm, "known": sorted(drivers)})
            continue
        key = f"vc:{firm}"
        if _vc_watermark_fresh(source_state.get(key).last_modified, refresh_days):
            log.info("vc_discovery_done", extra={
                "firm": firm, "skipped_fresh": True,
                "fetched": 0, "staged": 0, "deduped": 0, "store_errors": 0,
            })
            continue
        try:
            companies = await fetcher(client)
            if not companies:
                # An empty portfolio page is a scrape break, not a signal.
                raise RuntimeError(f"{firm} returned an empty portfolio")
        except Exception:  # noqa: BLE001 — a broken driver must not kill the cycle
            log.exception("vc_fetch_failed", extra={"firm": firm})
            continue

        staged = deduped = store_errors = 0
        for company in companies:
            if staged >= capture_cap:
                break
            try:
                variants = slug_candidates(company.name, company.domain, None)
                if not variants:
                    deduped += 1  # nothing derivable to stage under
                    continue
                primary = variants[0]
                domain = seed_domain(company.domain) if company.domain else None
                if (
                    any((ats, primary) in active_set for ats in _SUPPORTED_ATS)
                    or any(_known(discovered, discovered.get(f"{ats}:{primary}"))
                           for ats in _SUPPORTED_ATS)
                    or discovered.get(f"candidate:{primary}") is not None
                    or discovered.is_recent_no_match(
                        primary, fresh_within_days=no_match_fresh_days)
                    or (boards is not None and domain
                        and _known(boards, boards.get(domain)))
                ):
                    deduped += 1
                    continue
                discovered.upsert_candidate(
                    f"candidate:{primary}", company_name=company.name,
                    website=company.domain, origin=f"vc:{firm}",
                )
                staged += 1
            except Exception:  # noqa: BLE001 — one bad entry/store hiccup must not kill the drain
                store_errors += 1
                log.exception("vc_stage_failed",
                              extra={"firm": firm, "company": company.name})
        source_state.put(key, ConnectorState(
            last_modified=datetime.now(timezone.utc).isoformat()))
        log.info("vc_discovery_done", extra={
            "firm": firm, "skipped_fresh": False, "fetched": len(companies),
            "staged": staged, "deduped": deduped, "store_errors": store_errors,
        })


def make_yc_oss_fetcher(*, min_team_size: int = 10) -> YcOssClient:
    """Production factory used by the handler."""
    return YcOssClient(min_team_size=min_team_size)
