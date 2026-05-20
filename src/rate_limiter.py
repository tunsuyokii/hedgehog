from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict


@dataclass
class _Bucket:
    timestamps: Deque[float] = field(default_factory=deque)


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: Dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = self._buckets.setdefault(key, _Bucket())
        edge = now - self.window_seconds

        while bucket.timestamps and bucket.timestamps[0] < edge:
            bucket.timestamps.popleft()

        if len(bucket.timestamps) >= self.max_requests:
            return False

        bucket.timestamps.append(now)
        return True

