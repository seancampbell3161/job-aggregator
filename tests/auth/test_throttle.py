"""LoginThrottle: global backoff after repeated failed password checks."""
from src.auth.throttle import FREE_FAILURES, MAX_DELAY_SECONDS, LoginThrottle


class Tick:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_constants():
    assert (FREE_FAILURES, MAX_DELAY_SECONDS) == (5, 300)


def test_the_first_five_failures_are_free():
    throttle = LoginThrottle(clock=Tick())
    for n in range(1, 6):
        assert throttle.record_failure() == n
        assert throttle.check() is None


def test_backoff_doubles_from_two_seconds_and_caps_at_300():
    clock = Tick()
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        throttle.record_failure()
    for delay in [2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]:
        throttle.record_failure()
        assert throttle.check() == delay
        clock.t += delay - 0.5
        assert throttle.check() == 1  # half a second left rounds up
        clock.t += 0.5
        assert throttle.check() is None


def test_success_resets():
    throttle = LoginThrottle(clock=Tick())
    for _ in range(7):
        throttle.record_failure()
    assert throttle.check() == 4
    throttle.record_success()
    assert throttle.check() is None
    assert throttle.record_failure() == 1
    assert throttle.check() is None


def test_a_flood_of_failures_stays_capped():
    throttle = LoginThrottle(clock=Tick())
    for _ in range(10_000):
        throttle.record_failure()
    assert throttle.check() == 300
