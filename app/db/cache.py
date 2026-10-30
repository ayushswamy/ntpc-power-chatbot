"""
A tiny in-memory cache with an expiry time (TTL).

Used for live data: when many users ask "current generation" within the same
minute, the database is queried once and the answer is reused for up to
LIVE_CACHE_TTL_SECONDS (max 60). Nothing is stored on disk - it is not a copy
of the data, just a short-lived memory of the last query.
"""
import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._data: dict = {}
        self._lock = threading.Lock()

    def get_or_load(self, key, loader):
        """Return the cached value for `key`, or call loader() and cache its result."""
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = loader()  # outside the lock: a slow query must not block other keys
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
