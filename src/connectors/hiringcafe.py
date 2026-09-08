from __future__ import annotations

import httpx

from src.hiringcafe import HiringCafeJob
from src.models import ConnectorState, FetchResult, RawPosting
from src.sightings import Sighting


class HiringCafeConnector:
    """Slow-tier safety-net connector. Emits postings whose (ats_family, slug) is
    NOT already in our direct-poll active set, branded under
    ``hiringcafe:{ats}:{slug}``.

    Cross-source dedup happens here so we never alert twice for the same posting."""

    name: str = "hiringcafe"
    tier: str = "slow"

    def __init__(self, *, cafe, active_set: set[tuple[str, str]], max_postings: int,
                 sightings: list[Sighting] | None = None) -> None:
        self._cafe = cafe
        self._active_set = active_set
        self._max = max_postings
        self._sightings = sightings

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        # Deliberately unguarded. The orchestrator already isolates each
        # connector (_guarded in src/orchestrator.py), so a raise here cannot
        # crash the cycle — but it DOES reach classify_outcome, which an empty
        # FetchResult never does. Swallowing here read as "polled fine, nothing
        # new" and hid a 13-day hiring.cafe outage (Cloudflare 403 from
        # 2026-07-14) behind a healthy-looking pipeline.
        jobs: list[HiringCafeJob] = await self._cafe.fetch_jobs(client)

        out: list[RawPosting] = []
        for j in jobs:
            if not j.ats_family or not j.ats_slug:
                continue
            if (j.ats_family, j.ats_slug) in self._active_set:
                continue
            if self._sightings is not None:
                self._sightings.append(Sighting(
                    ats_family=j.ats_family, slug=j.ats_slug,
                    company=j.company, apply_url=j.apply_url,
                ))
            if not j.external_id:
                continue
            out.append(
                RawPosting(
                    source=f"hiringcafe:{j.ats_family}:{j.ats_slug}",
                    external_id=j.external_id,
                    title=j.title,
                    description=j.description,
                    # Hiring.cafe reports the real employer. Without it normalize
                    # falls back to slug derivation, which reads the ATS vendor
                    # out of the middle segment and files every Ashby-hosted
                    # startup under "Ashby:<slug>".
                    company=j.company or None,
                    apply_url=j.apply_url,
                    location=j.location,
                    remote=j.remote,
                    department=None,
                    posted_at=j.posted_at,
                    comp_min=None,
                    comp_max=None,
                    employment_type=j.employment_type,
                    raw={},
                )
            )
            if len(out) >= self._max:
                break

        return FetchResult(postings=out, new_state=None, not_modified=False)
