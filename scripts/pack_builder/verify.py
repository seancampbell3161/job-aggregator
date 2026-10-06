"""Live-check one candidate with the app's own connectors, so a packed board
is exactly what a poll will see. Polite by construction: per-family and
global concurrency limits, honest User-Agent, and 429/5xx/timeouts retried
with backoff and finally reported as "deferred", never as dead. A host that
does not resolve or refuses the connection is dead at once."""
from __future__ import annotations

import asyncio
import html
import logging
import re

import httpx

from scripts.pack_builder.cache import CheckResult
from scripts.pack_builder.candidates import Candidate
from scripts.pack_builder.locations import classify
from src.connectors.base import connector_from_identity
from src.connectors.workable import WorkableConnector
from src.discovery import ProbeThrottled
from src.fingerprint import verify_identity
from src.models import ConnectorState
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

PER_FAMILY_LIMIT = 4
FAMILY_LIMITS = {"workday": 8, "oraclecloud": 8}   # one host per tenant
GLOBAL_LIMIT = 32
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 60.0
# Families whose URL identity is incomplete or unvalidated until a live
# check: verify_identity resolves it (and mutates it, for eightfold).
_RESOLVE_FIRST = frozenset({"eightfold", "taleo", "jsonld"})
_OG_SITE_NAME = re.compile(r'<meta\s+property="og:site_name"\s+content="([^"]*)"', re.I)
# A site name made only of these words names an audience, not a company.
_GENERIC_SITE_WORDS = frozenset({
    "all", "jobs", "job", "careers", "career", "external", "internal", "candidate",
    "experience", "opportunities", "openings", "positions", "vacancies", "search",
    "site", "portal", "recruiting", "employment", "current", "our", "the", "and", "at",
    "join", "us", "work", "with", "cx", "en", "home", "page", "global", "english",
})


class _Retryable(Exception):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("retryable")
        self.retry_after = retry_after


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None


def humanize(family: str, identity: dict) -> str:
    raw = str(identity.get("slug") or identity.get("tenant") or family)
    words = [w for w in re.split(r"[-_.\s]+", raw) if w]
    return " ".join(w.capitalize() for w in words) or raw


class Verifier:
    def __init__(self, client: httpx.AsyncClient | None, *, global_limit: int = GLOBAL_LIMIT,
                 per_family: int = PER_FAMILY_LIMIT, family_limits: dict[str, int] | None = None,
                 sleep=asyncio.sleep) -> None:
        self._client = client
        self._global = asyncio.Semaphore(global_limit)
        self._per_family = per_family
        self._family_limits = FAMILY_LIMITS if family_limits is None else family_limits
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._sleep = sleep

    def _sem(self, family: str) -> asyncio.Semaphore:
        if family not in self._sems:
            self._sems[family] = asyncio.Semaphore(
                self._family_limits.get(family, self._per_family))
        return self._sems[family]

    async def check(self, cand: Candidate) -> CheckResult:
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._global, self._sem(cand.family):
                    return await self._check_once(cand)
            except _Retryable as exc:
                if attempt + 1 < MAX_ATTEMPTS:
                    await self._sleep(min(exc.retry_after or 5.0 * (attempt + 1), MAX_RETRY_AFTER))
        return CheckResult("deferred")

    async def _check_once(self, cand: Candidate) -> CheckResult:
        identity = dict(cand.identity)
        try:
            if cand.family in _RESOLVE_FIRST:
                if await verify_identity(self._client, cand.family, identity) <= 0:
                    return CheckResult("dead")
            conn = (WorkableConnector(identity["slug"]) if cand.family == "workable"
                    else connector_from_identity(cand.family, identity, cand.company_hint))
            if conn is None:
                return CheckResult("dead")
            res = await conn.fetch(self._client, ConnectorState())
        except ProbeThrottled as exc:
            raise _Retryable() from exc
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                raise _Retryable(_retry_after(exc.response)) from exc
            if code >= 500:
                raise _Retryable() from exc
            return CheckResult("dead")
        except httpx.ConnectError:
            # DNS failure or connection refused: a dead tenant host stays dead,
            # and deferring it would count against the partial-build guard on
            # every run. ConnectTimeout is a TimeoutException, not a
            # ConnectError, so it still falls through to the retry below.
            return CheckResult("dead")
        except httpx.TransportError as exc:
            raise _Retryable() from exc
        except Exception as exc:  # noqa: BLE001 — a parse error on one board must not stop the build
            log.debug("verify_error", extra={"key": cand.key, "error": repr(exc)})
            return CheckResult("dead")
        posts = res.postings
        if not posts:
            return CheckResult("dead")
        us = eu = 0
        for p in posts:
            regions = classify(p.location)
            us += "us" in regions
            eu += "eu" in regions
        company = (next((p.company for p in posts if p.company), None)
                   or await self._board_name(cand.family, identity)
                   or cand.company_hint or humanize(cand.family, identity))
        return CheckResult("live", len(posts), us, eu, company, conn.name, identity)

    async def _board_name(self, family: str, identity: dict) -> str | None:
        if self._client is None:
            return None
        if family == "oraclecloud":
            return await self._oracle_site_name(identity)
        if family != "greenhouse":
            return None
        try:
            r = await self._client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{identity['slug']}",
                headers=ua_headers(), timeout=20.0)
            if r.status_code == 200:
                name = r.json().get("name")
                return name.strip() if isinstance(name, str) and name.strip() else None
        except (httpx.HTTPError, ValueError):
            pass
        return None

    async def _oracle_site_name(self, identity: dict) -> str | None:
        """The careers site's og:site_name. Oracle tenants are opaque codes
        ("ebuu") and postings carry no company, but each careers page is
        server-rendered with its site's display name ("Westpac Group"). The
        tenant's default site 302s to its real one, so redirects are followed.
        Generic site names ("All Jobs" on a multi-brand tenant) are refused."""
        url = (f"https://{identity['tenant']}.fa.{identity['region']}.oraclecloud.com"
               f"/hcmUI/CandidateExperience/en/sites/{identity['site']}")
        try:
            r = await self._client.get(url, headers=ua_headers(), timeout=20.0,
                                       follow_redirects=True)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        m = _OG_SITE_NAME.search(r.text)
        name = html.unescape(m.group(1)).strip() if m else ""
        words = re.findall(r"[a-z]+", name.lower())
        if not words or set(words) <= _GENERIC_SITE_WORDS:
            return None
        return name
