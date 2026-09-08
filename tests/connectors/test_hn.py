import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.hn import HnWhoIsHiringConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "hn_thread_example.json").read_text()
)


@pytest.mark.asyncio
async def test_hn_fetch_pulls_top_level_comments():
    conn = HnWhoIsHiringConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://hacker-news.firebaseio.com/v0/user/whoishiring.json"
            ).respond(200, json=FIXTURE["user"])
            respx.get(
                "https://hacker-news.firebaseio.com/v0/item/50000001.json"
            ).respond(200, json=FIXTURE["thread"])
            for c in FIXTURE["comments"]:
                respx.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{c['id']}.json"
                ).respond(200, json=c)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    # Deleted, empty-body, and non-job-format comments are all skipped.
    assert len(postings) == 2
    ids = {p.external_id for p in postings}
    assert "50000013" not in ids, "non-job-format comment must be skipped"
    assert "50000014" not in ids, "empty-body comment must be skipped"
    stripe_p = next(p for p in postings if "Stripe" in p.title)
    assert stripe_p.source == "hn:who_is_hiring"
    assert stripe_p.external_id == "50000010"
    assert "Backend Engineer" in stripe_p.title
    assert stripe_p.location and "Remote" in stripe_p.location
    assert stripe_p.remote is True
    assert "Python" in stripe_p.description
    assert stripe_p.apply_url == "https://news.ycombinator.com/item?id=50000010"
