from __future__ import annotations

import asyncio
import json
import logging

import httpx

from src.notify.base import NotificationPayload

log = logging.getLogger(__name__)

_COLORS = {
    "senior": 0x2ECC71,
    "mid": 0x3498DB,
    "staff": 0x9B59B6,
    "manager": 0xE74C3C,
    "junior": 0x95A5A6,
}

# Discord embed limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
_TITLE_MAX = 256
_DESCRIPTION_MAX = 4096
_FIELD_VALUE_MAX = 1024

# Cap how long we'll sleep before retrying a 429. Discord webhook rate-limits
# typically come back as retry_after=1..3s; a buggy server returning a huge
# value shouldn't be allowed to consume the Lambda budget.
_RETRY_AFTER_CAP_SECONDS = 10.0
_RETRY_AFTER_DEFAULT_SECONDS = 2.0


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _parse_retry_after(resp: httpx.Response) -> float:
    """Extract retry_after seconds from a Discord 429 response.

    Discord returns the precise wait in the JSON body (`retry_after`, seconds,
    may be float). Fall back to the HTTP `Retry-After` header if the body is
    unparseable, then to a small default. Always capped."""
    try:
        body = json.loads(resp.text or "{}")
        v = body.get("retry_after")
        if isinstance(v, (int, float)) and v >= 0:
            return min(float(v), _RETRY_AFTER_CAP_SECONDS)
    except (ValueError, TypeError):
        pass
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), _RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            pass
    return _RETRY_AFTER_DEFAULT_SECONDS


class DiscordSink:
    name = "discord"

    def __init__(self, *, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, client: httpx.AsyncClient, payload: NotificationPayload) -> None:
        fields = [
            {"name": "Company", "value": _trunc(payload.company, _FIELD_VALUE_MAX), "inline": True},
            {"name": "Location", "value": _trunc(payload.location, _FIELD_VALUE_MAX), "inline": True},
        ]
        if payload.comp:
            fields.append({"name": "Comp", "value": _trunc(payload.comp, _FIELD_VALUE_MAX), "inline": True})
        fields.append(
            {"name": "Stack", "value": _trunc(", ".join(payload.stack_matched) or "-", _FIELD_VALUE_MAX), "inline": True}
        )
        fields.append({"name": "Posted", "value": _trunc(payload.posted, _FIELD_VALUE_MAX), "inline": True})
        source_value = (
            "[Jobs by Adzuna](https://www.adzuna.com)"
            if payload.source == "adzuna"
            else payload.source
        )
        fields.append({"name": "Source", "value": _trunc(source_value, _FIELD_VALUE_MAX), "inline": True})
        if payload.gaps:
            fields.append(
                {"name": "Stretch areas", "value": _trunc(", ".join(payload.gaps), _FIELD_VALUE_MAX), "inline": False}
            )
        if payload.tailor_url:
            fields.append({
                "name": "🪄 Tailor résumé",
                "value": _trunc(f"[Open tailored résumé]({payload.tailor_url})", _FIELD_VALUE_MAX),
                "inline": False,
            })

        score_prefix = ""
        if payload.relevance_score is not None:
            score_prefix = f"[{payload.relevance_score}/10] "
        elif payload.relevance_rationale == "(LLM unavailable)":
            score_prefix = "[?/10] "

        description = payload.relevance_rationale or ""

        embed = {
            "title": _trunc(f"{score_prefix}{payload.title}", _TITLE_MAX),
            "url": payload.apply_url,
            "color": _COLORS.get(payload.seniority or "mid", _COLORS["mid"]),
            "fields": fields,
        }
        if description:
            embed["description"] = _trunc(description, _DESCRIPTION_MAX)

        body = {"embeds": [embed]}
        resp = await client.post(self._webhook_url, json=body, timeout=15.0)

        # On 429, honor Discord's retry_after and try once more. Webhook rate
        # limits are 30/60s/channel; a busy notify cycle (e.g. many ATS hits in
        # one Lambda invocation) trips this and previously dropped the post
        # silently while ntfy succeeded. See orchestrator: any-sink-success
        # keeps the claim, so the failed sink never retries on its own.
        if resp.status_code == 429:
            wait = _parse_retry_after(resp)
            log.warning(
                "discord_rate_limited_retrying",
                extra={"status_code": 429, "retry_after_s": wait, "body": resp.text[:500]},
            )
            await asyncio.sleep(wait)
            resp = await client.post(self._webhook_url, json=body, timeout=15.0)

        if resp.status_code >= 400:
            log.warning(
                "discord_post_failed",
                extra={"status_code": resp.status_code, "body": resp.text[:500]},
            )
        resp.raise_for_status()
