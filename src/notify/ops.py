"""Lightweight ops-alert sender. Deliberately separate from the job-alert
sinks: NotificationPayload is posting-shaped (company/role/location), while an
ops alert is just title+body on a SEPARATE channel. Mirrors the sinks'
transport conventions (ntfy header set incl. the latin-1 Title guard; Discord
webhook JSON) without the payload contortion."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpsAlert:
    condition: str            # "pipeline_stopped" | "zero_yield" | "llm_degraded"
    title: str
    body: str
    recovered: bool = False   # recovery notice (calm priority) vs. alert


async def send_ops_alert(
    client: httpx.AsyncClient,
    alert: OpsAlert,
    *,
    ntfy_topic_url: str = "",
    discord_webhook_url: str = "",
) -> bool:
    """Send to whichever ops sinks are configured. Returns True iff at least
    one sink accepted the message. Each sink is fail-soft."""
    ok = False
    if ntfy_topic_url:
        try:
            headers = {
                # ntfy passes Title as an HTTP header; httpx rejects non-latin-1.
                "Title": alert.title.encode("ascii", "replace").decode("ascii"),
                "Priority": "default" if alert.recovered else "high",
                "Tags": "white_check_mark" if alert.recovered else "warning",
            }
            resp = await client.post(
                ntfy_topic_url, content=alert.body.encode("utf-8"),
                headers=headers, timeout=15.0,
            )
            resp.raise_for_status()
            ok = True
        except Exception as exc:  # noqa: BLE001 — ops alerts are best-effort
            log.warning("ops_ntfy_failed", extra={"condition": alert.condition, "error": str(exc)})
    if discord_webhook_url:
        try:
            prefix = "✅" if alert.recovered else "⚠️"
            resp = await client.post(
                discord_webhook_url,
                json={"content": f"{prefix} **{alert.title}**\n{alert.body}"},
                timeout=15.0,
            )
            resp.raise_for_status()
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("ops_discord_failed", extra={"condition": alert.condition, "error": str(exc)})
    return ok
