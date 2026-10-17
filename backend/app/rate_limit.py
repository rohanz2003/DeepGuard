"""Small process-local limiter; use a shared gateway for multi-worker limits."""

from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class RateLimiter:
    def __init__(self, window_seconds: int = 60, max_keys: int = 4096):
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = monotonic()
        with self._lock:
            if key not in self._events and len(self._events) >= self.max_keys:
                self._events.pop(next(iter(self._events)))
            events = self._events[key]
            while events and now - events[0] >= self.window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
