"""Read a stored job description from the seen_jobs table.

`description_snapshot` is written by sub-project C-wiring at notify time; this
endpoint only reads it. Returns None when the row or the snapshot is absent
(an older alert, or a row that aged out of the 60-day TTL).

The snapshot is attacker-controlled text on its way into the tailor prompt —
the highest-severity LLM surface here, since injected instructions could steer
résumé bullets into claims that go out under the user's name. Sanitized on
read so rows written before the filter existed are covered too."""

from __future__ import annotations

from dataclasses import dataclass

import boto3

from src.sanitize import sanitize_description


@dataclass(frozen=True)
class PostingJD:
    description: str
    title: str
    company: str


def read_jd(table_name: str, job_id: str, *, region: str = "us-east-1") -> PostingJD | None:
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    item = table.get_item(Key={"job_id": job_id}).get("Item")
    if not item or not item.get("description_snapshot"):
        return None
    return PostingJD(
        description=sanitize_description(item["description_snapshot"])[0],
        title=item.get("title", ""),
        company=item.get("company", ""),
    )
