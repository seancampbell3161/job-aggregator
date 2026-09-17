"""Global backoff for failed password checks (login and change-password).

In memory, one per web process; a restart resets it. Global rather than
per-IP: behind Docker's port forwarding every client arrives from the same
bridge address. Signed-in browsers are unaffected — only new logins wait."""
from __future__ import annotations

import math
import threading
import time
from typing import Callable

FREE_FAILURES = 5
MAX_DELAY_SECONDS = 300


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._blocked_until = 0.0

    def check(self) -> int | None:
        """Whole seconds until the next attempt is allowed, or None when it is
        allowed now. A refused attempt must not verify the password."""
        with self._lock:
            remaining = self._blocked_until - self._clock()
        return math.ceil(remaining) if remaining > 0 else None

    def record_failure(self) -> int:
        """Count a failed check; returns the consecutive-failure count."""
        with self._lock:
            self._failures += 1
            over = self._failures - FREE_FAILURES
            if over > 0:
                # 2**9 = 512 already exceeds the cap; bounding the exponent
                # keeps a flood of failures from building huge integers.
                delay = min(2 ** min(over, 9), MAX_DELAY_SECONDS)
                self._blocked_until = self._clock() + delay
            return self._failures

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._blocked_until = 0.0
