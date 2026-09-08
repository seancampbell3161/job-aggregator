"""Oracle Recruiting Cloud (ORC) job-board connector.

ORC tenants expose an unauthenticated CandidateExperience REST API at
``https://{tenant}.fa.{region}.oraclecloud.com/hcmRestApi/resources/latest/
recruitingCEJobRequisitions``. Like Workday, identity is a (tenant, region,
site) triple; ``site`` is ORC's CE site number (usually CX_1). The list
endpoint returns coarse fields (Id, Title, PostedDate, locations); the real
JD lives in recruitingCEJobRequisitionDetails — see enrich()."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx

from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_PAGE_SIZE = 25
# Same rationale as Workday's cap: newest-first ordering (pinned via
# sortBy=POSTING_DATES_DESC in the finder) + max_age_days small means
# survivors sit on the first pages; uncapped pagination on big boards
# blows the cycle budget.
_MAX_PAGES = 5
_MAX_DESC = 30000  # match SeenJobsStore.claim_for_notify's description_snapshot cap
def _headers() -> dict[str, str]:
    return ua_headers(Accept="application/json")


def _parse_posted_date(s: str | None) -> datetime | None:
    """ORC PostedDate is an ISO date string ("2026-06-30"). Anything
    unparseable returns None — the age filter treats it as UNKNOWN. Only
    stamps UTC when the parsed value is naive — a future full timestamp with
    its own offset must not be shifted."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _locations_text(r: dict[str, Any]) -> str | None:
    locs: list[str | None] = [r.get("PrimaryLocation")]
    for sec in r.get("secondaryLocations") or []:
        if isinstance(sec, dict):
            locs.append(sec.get("Name"))
    out = "; ".join(x for x in locs if x)
    return out or None


class OracleCloudConnector:
    name: str
    tier: str = "ats"
    supports_enrich = True

    def __init__(
        self, tenant: str, region: str, site: str, company: str | None = None
    ) -> None:
        self.tenant = tenant
        self.region = region
        self.site = site
        self.company = company
        self.name = f"oraclecloud:{tenant}:{site}"

    @property
    def _base(self) -> str:
        return f"https://{self.tenant}.fa.{self.region}.oraclecloud.com"

    def _to_raw(self, r: dict[str, Any]) -> RawPosting:
        rid = str(r.get("Id", ""))
        return RawPosting(
            source=self.name,
            external_id=rid,
            # `or ""` not `.get(k, "")` — Oracle sends an explicit JSON null for
            # Title on some requisitions, and a default only applies to a MISSING
            # key. A None title reached _seniority() and killed the whole ats
            # cycle every 10 min for 12h (2026-08-02).
            title=r.get("Title") or "",
            description=_strip_html(r.get("ShortDescriptionStr") or ""),
            apply_url=(
                f"{self._base}/hcmUI/CandidateExperience/en/sites/{self.site}/job/{rid}"
            ),
            location=_locations_text(r),
            remote=None,
            department=None,
            company=self.company,
            posted_at=_parse_posted_date(r.get("PostedDate")),
            raw=r,
        )

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        # The finder syntax puts paging INSIDE the finder param, not in
        # top-level query params. Paginate until a partial/empty page, the
        # reported total, or the safety cap (mirrors WorkdayConnector.fetch).
        url = f"{self._base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        out: list[RawPosting] = []
        offset = 0
        total: int | None = None
        for _ in range(_MAX_PAGES):
            # Build the query string by hand and append it to the URL rather than
            # passing a params= dict: httpx's QueryParams percent-encodes ";", ",",
            # "=" inside values, but ORC's finder syntax requires those characters
            # literal (they're valid RFC 3986 sub-delims, so this is safe).
            query = (
                "onlyData=true"
                "&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber={self.site},sortBy=POSTING_DATES_DESC,limit={_PAGE_SIZE},offset={offset}"
            )
            try:
                resp = await client.get(f"{url}?{query}", headers=_headers(), timeout=20.0)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                if offset == 0:
                    raise  # first-page failure → connector failed this cycle
                log.warning(
                    "oraclecloud_page_failed",
                    extra={"source": self.name, "offset": offset},
                )
                break  # later-page failure → return what we have
            items = data.get("items") or []
            reqs = (items[0].get("requisitionList") if items else None) or []
            if not reqs:
                break
            out.extend(self._to_raw(r) for r in reqs)
            if len(reqs) < _PAGE_SIZE:
                break  # partial page → last page
            if items:
                total = items[0].get("TotalJobsCount", total)
            offset += _PAGE_SIZE
            if total is not None and offset >= int(total):
                break
        return FetchResult(postings=out, new_state=None, not_modified=False)

    async def enrich(
        self, client: httpx.AsyncClient, posting: NormalizedPosting
    ) -> NormalizedPosting:
        """Fetch the requisition detail and return a copy with the real JD.
        Best-effort: any failure returns the posting unchanged (the caller
        keeps the coarse list data). Mirrors WorkdayConnector.enrich."""
        rid = posting.job_id.rsplit(":", 1)[-1]
        url = f"{self._base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        # Same rationale as fetch(): build the query string by hand rather than
        # passing params= — httpx's QueryParams percent-encodes ";"/","/"=" inside
        # values, which breaks ORC's finder syntax. rid is digits from our own
        # job_id and self.site is trusted config, so manual interpolation stays
        # injection-safe.
        query = (
            "onlyData=true"
            "&expand=all"
            f'&finder=ById;siteNumber={self.site},Id="{rid}"'
        )
        try:
            resp = await client.get(f"{url}?{query}", headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            items = (resp.json() or {}).get("items") or []
            info = items[0] if items else {}
        except Exception as exc:  # noqa: BLE001 — enrich is best-effort
            log.warning(
                "oraclecloud_enrich_failed",
                extra={"source": self.name, "job_id": posting.job_id, "error": str(exc)},
            )
            return posting

        desc = _strip_html(info.get("ExternalDescriptionStr") or "")
        if not desc:
            return posting
        return replace(posting, description=desc[:_MAX_DESC])
