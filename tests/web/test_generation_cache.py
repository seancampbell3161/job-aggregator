from src.web.generation_cache import GenerationCache
from tests.settings_helpers import make_service


def test_builds_once_per_generation():
    svc = make_service({})
    cache = GenerationCache()
    builds = []

    def build(snap):
        builds.append(snap.generation)
        return f"built@{snap.generation}"

    first = svc.snapshot()
    assert cache.get("k", first, build) == f"built@{first.generation}"
    assert cache.get("k", svc.snapshot(), build) == f"built@{first.generation}"
    assert len(builds) == 1
    svc.save_settings({"schedules": {"ats_minutes": 3}}, source="cli")
    second = svc.snapshot()
    assert cache.get("k", second, build) == f"built@{second.generation}"
    assert len(builds) == 2


def test_keys_are_independent():
    snap = make_service({}).snapshot()
    cache = GenerationCache()
    assert cache.get("a", snap, lambda s: 1) == 1
    assert cache.get("b", snap, lambda s: 2) == 2
    assert cache.get("a", snap, lambda s: 99) == 1


def test_an_older_snapshot_never_replaces_a_newer_entry():
    svc = make_service({})
    old = svc.snapshot()
    svc.save_settings({}, source="cli")
    new = svc.snapshot()
    cache = GenerationCache()
    cache.get("k", new, lambda s: "new")
    assert cache.get("k", old, lambda s: "old") == "old"   # built for the older request...
    assert cache.get("k", new, lambda s: "rebuilt") == "new"  # ...without evicting the newer entry
