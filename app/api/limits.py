"""
Rate limiting: at most N questions per minute per chat session, and 3*N per client
address (so one person cannot flood the LLM by opening many sessions).
Kept in memory - it resets when the API restarts, which is fine for this purpose.
"""
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record one request for `key`; False if it is over the limit for the last 60 seconds."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest request for `key` leaves the 60-second window."""
        with self._lock:
            hits = self._hits.get(key)
            return max(1, int(60 - (time.monotonic() - hits[0])) + 1) if hits else 1
