def test_module_imports_without_playwright():
    # Importing the module must NOT import Playwright (it's lazy inside the CM),
    # so the fast tiers / CI never need Chromium.
    import importlib
    import src.headless as h
    importlib.reload(h)
    assert hasattr(h, "browser_session")


def test_headless_tier_identifies_itself_honestly():
    """Regression guard. This tier drives a real browser and says so; it must
    not reacquire anti-detection code. Measured 2026-09-08 against a live
    Avature tenant: a plain headless Chromium (its own UA, no webdriver mask,
    no AutomationControlled flag) returned the identical full job list, so the
    evasion that used to live here bought nothing."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "headless.py"
    text = src.read_text()
    for banned in ("navigator.webdriver", "AutomationControlled", "Mozilla/5.0", "stealth"):
        assert banned not in text, f"anti-detection code reintroduced in src/headless.py: {banned!r}"


def test_headless_available_is_true_when_playwright_is_importable(monkeypatch):
    import src.headless as h
    monkeypatch.setattr(h.importlib.util, "find_spec", lambda name: object())
    assert h.headless_available() is True


def test_headless_available_is_false_without_playwright(monkeypatch):
    import src.headless as h
    monkeypatch.setattr(h.importlib.util, "find_spec", lambda name: None)
    assert h.headless_available() is False


def test_headless_available_does_not_import_playwright(monkeypatch):
    """It must stay a spec lookup: importing Playwright to find out whether it
    is importable would defeat the laziness the module exists to preserve."""
    import src.headless as h
    seen = []
    monkeypatch.setattr(h.importlib.util, "find_spec", lambda name: seen.append(name) or None)
    h.headless_available()
    assert seen == ["playwright"]
