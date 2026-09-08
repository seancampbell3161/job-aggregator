"""Generic Avature careers connector. Avature renders its listings client-side
and serves an empty body to plain HTTP clients, so this connector runs on the
`headless` tier — the orchestrator hands it a Playwright `page`, not an httpx
client. One parser handles every tenant (Avature's shared careers theme:
article.article--result cards; selectors confirmed live 2026-07-03).

Measured 2026-09-08 on a live tenant: a plain headless browser identifying
itself honestly returns the full job list (HTTP 202 is Avature's normal
response here, not a challenge). Boards are hand-curated via sources.avature,
which ships empty.
"""
from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.connectors.jsonld import _jobposting_from_jsonld
from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting

_JOB_CARD = "article.article--result"
_HEADING_LINK = "h1 a, h2 a, h3 a, h4 a"
_LOCATION = "span.list-item-location"
_MAX_DESC = 30000


def _tenant_from_url(careers_url: str) -> str:
    """careers.jacobs.com/... -> jacobs; jacobs.avature.net/... -> jacobs."""
    host = (urlparse(careers_url).netloc or "").lower()
    labels = [l for l in host.split(".") if l and l not in ("careers", "www", "jobs")]
    return labels[0] if labels else host or "unknown"


def _job_external_id(href: str) -> str:
    """Avature JobDetail URLs end in the numeric job id
    (/careers/JobDetail/<slug>/<id>). Fall back to the last path segment."""
    nums = re.findall(r"\d{3,}", href)
    if nums:
        return nums[-1]
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return tail or href


def parse_avature_results(html: str, *, base_url: str, company: str, source: str) -> list[RawPosting]:
    """Extract postings from a rendered Avature SearchJobs page. Pure — no IO."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawPosting] = []
    for card in soup.select(_JOB_CARD):
        link = card.select_one(_HEADING_LINK) or card.select_one('a[href*="JobDetail"]')
        if link is None:
            continue
        title = link.get_text(strip=True)
        href = link.get("href") or ""
        if not title or not href:
            continue
        loc_el = card.select_one(_LOCATION) or card.select_one('[class*="ocation"]')
        location = loc_el.get_text(strip=True) if loc_el else None
        out.append(RawPosting(
            source=source, external_id=_job_external_id(href), title=title,
            description="", apply_url=urljoin(base_url, href),
            location=location, company=company, raw={},
        ))
    return out


def _extract_jd(html: str) -> str | None:
    """Best-effort JD text for enrich: the first job-description content region
    (JSON-LD preferred, reliable; else the first DOM match), stripped."""
    jp = _jobposting_from_jsonld(html)
    if jp is not None:
        desc = jp.get("description")
        if desc:
            desc_str = _strip_html(str(desc))[:_MAX_DESC]
            if desc_str.strip():
                return desc_str
    soup = BeautifulSoup(html, "html.parser")
    region = soup.select_one('[class*="escription"], [class*="job-details"], main, article')
    if region is not None:
        text = _strip_html(str(region))[:_MAX_DESC]
        if text.strip():
            return text
    return None


class AvatureConnector:
    tier = "headless"
    supports_enrich = True

    def __init__(self, careers_url: str, company: str) -> None:
        self.careers_url = careers_url
        self.company = company
        self.name = f"avature:{_tenant_from_url(careers_url)}"
        u = urlparse(careers_url)
        self._base = f"{u.scheme}://{u.netloc}"

    async def fetch(self, page, state: ConnectorState) -> FetchResult:
        await page.goto(self.careers_url, wait_until="domcontentloaded", timeout=30000)
        # A bot-challenge / empty page never renders the list → wait_for_selector
        # raises → this cycle's fetch fails for this tenant (logged); it is
        # retried next cycle (not health-suppressed).
        await page.wait_for_selector(_JOB_CARD, timeout=15000)
        # wait_for_selector only guarantees the FIRST card is present — Avature
        # lazy-renders the rest. Poll the card count until it stabilizes so we
        # capture the full list, not a partial render.
        prev = -1
        for _ in range(10):
            cards = await page.query_selector_all(_JOB_CARD)
            count = len(cards)
            if count == prev and count > 0:
                break
            prev = count
            await page.wait_for_timeout(500)
        html = await page.content()
        postings = parse_avature_results(
            html, base_url=self._base, company=self.company, source=self.name,
        )
        return FetchResult(postings=postings, new_state=None, not_modified=False)

    async def enrich(self, page, posting: NormalizedPosting) -> NormalizedPosting:
        try:
            await page.goto(posting.apply_url, wait_until="domcontentloaded", timeout=30000)
            html = await page.content()
        except Exception:  # noqa: BLE001 — enrich is best-effort
            return posting
        desc = _extract_jd(html)
        if not desc:
            return posting
        return replace(posting, description=desc)
