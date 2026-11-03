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


class RedisRateLimiter:
    """Shared fixed-window limiter for multi-worker deployments."""

    def __init__(self, url: str, *, prefix: str = "deepguard:rate", window_seconds: int = 60):
        if not str(url or "").strip():
            raise ValueError("REDIS_URL is required for the Redis rate limiter.")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError("Install the redis backend dependency to enable shared rate limits.") from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self.prefix = prefix
        self.window_seconds = window_seconds

    def allow(self, key: str, limit: int) -> bool:
        bucket = int(monotonic() // self.window_seconds)
        redis_key = f"{self.prefix}:{key}:{bucket}"
        try:
            count = self._client.incr(redis_key)
            if count == 1:
                self._client.expire(redis_key, self.window_seconds + 1)
            return count <= limit
        except Exception:
            # Fail closed when the shared limiter is configured but unavailable.
            return False


__all__ = ["RateLimiter", "RedisRateLimiter"]
