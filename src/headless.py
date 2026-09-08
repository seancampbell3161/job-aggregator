"""Playwright/Chromium session for the headless connector tier. This is the
ONLY module that imports Playwright, and it does so lazily (inside
browser_session) so the fast tiers, the web app, and CI never load it. A missing
Playwright/Chromium (image built without the [headless] extra) surfaces as an
ImportError from browser_session, which the headless cycle catches and no-ops.

This tier exists because some careers sites render their listings with
JavaScript, so a plain HTTP client sees nothing. It drives a real browser and
identifies itself honestly (src.user_agent); it does not attempt to disguise
that it is automation. Measured 2026-09-08 against a live Avature tenant: a
plain headless Chromium returns the full job list, so nothing is gained by
hiding.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from src.user_agent import user_agent

log = logging.getLogger(__name__)


class HeadlessBrowser:
    """Thin wrapper: hands out pages from one browser, each in its own context
    (no cookie bleed between tenants)."""

    def __init__(self, browser):
        self._browser = browser

    async def new_page(self):
        context = await self._browser.new_context(
            user_agent=user_agent(),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        return await context.new_page()


@asynccontextmanager
async def browser_session():
    """Launch one headless Chromium; yield a HeadlessBrowser. Playwright is
    imported here so nothing else in the app pulls it in."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            # --no-sandbox is required to run Chromium as root inside the
            # container image; it is not an anti-detection measure.
            args=["--no-sandbox"],
        )
        try:
            yield HeadlessBrowser(browser)
        finally:
            await browser.close()
