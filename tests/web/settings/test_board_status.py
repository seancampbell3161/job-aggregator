"""The health badge: what the runtime stores say about a configured board."""
from src.web.settings.health import board_status


class _Health:
    def __init__(self, tracked=(), suppressed=(), backoff=()):
        self._t, self._s, self._b = set(tracked), set(suppressed), set(backoff)

    def tracked_names(self): return set(self._t)
    def suppressed_names(self): return set(self._s)
    def backoff_names(self, now_ms): return set(self._b)


class _Discovered:
    def __init__(self, rows=()): self._rows = list(rows)
    def list_all(self): return list(self._rows)


class _Stores:
    def __init__(self, health, discovered):
        self.health, self.discovered = health, discovered


def _row(name, status="ok", postings=7):
    from src.state import DiscoveredSlug
    return DiscoveredSlug(
        connector_name=name, ats_family=name.split(":")[0], slug=name.split(":")[-1],
        company_name=None, discovered_at="", last_validated_at=None,
        validation_status=status, consecutive_failures=0, last_posting_count=postings,
    )


def test_a_board_with_no_health_row_is_healthy():
    status = board_status(_Stores(_Health(), _Discovered()))
    assert status["greenhouse:acme"].state == "healthy"


def test_suppression_wins_over_everything():
    stores = _Stores(_Health(tracked={"greenhouse:acme"}, suppressed={"greenhouse:acme"},
                             backoff={"greenhouse:acme"}), _Discovered())
    assert board_status(stores)["greenhouse:acme"].state == "suppressed"


def test_a_backed_off_board_reads_as_paused():
    stores = _Stores(_Health(backoff={"lever:beta"}), _Discovered())
    assert board_status(stores)["lever:beta"].state == "paused"


def test_a_tracked_but_unsuppressed_board_reads_as_failing():
    stores = _Stores(_Health(tracked={"lever:beta"}), _Discovered())
    assert board_status(stores)["lever:beta"].state == "failing"


def test_posting_counts_come_from_the_discovered_row_when_there_is_one():
    stores = _Stores(_Health(), _Discovered([_row("greenhouse:acme", postings=42)]))
    assert board_status(stores)["greenhouse:acme"].postings == 42


def test_an_unreadable_store_degrades_to_none_rather_than_raising():
    class Boom:
        def tracked_names(self): raise RuntimeError("database is locked")
        def suppressed_names(self): return set()
        def backoff_names(self, now_ms): return set()

    assert board_status(_Stores(Boom(), _Discovered())) is None
