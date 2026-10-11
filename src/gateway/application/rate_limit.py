import asyncio
import time
from dataclasses import dataclass

from ..domain.config import RateLimitConfig
from ..domain.errors import GatewayError


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    """Process-local token bucket keyed by caller identity.

    Use a shared store (e.g. Redis) when running multiple replicas.
    """

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, identity: str) -> None:
        if not self._config.enabled:
            return
        now = time.monotonic()
        refill_per_second = self._config.requests_per_minute / 60
        capacity = float(self._config.burst)
        async with self._lock:
            bucket = self._buckets.setdefault(identity, _Bucket(capacity, now))
            bucket.tokens = min(
                capacity, bucket.tokens + (now - bucket.updated_at) * refill_per_second
            )
            bucket.updated_at = now
            if bucket.tokens < 1:
                retry_after = max(1, int((1 - bucket.tokens) / refill_per_second))
                raise GatewayError(
                    f"Rate limit exceeded. Retry in {retry_after}s",
                    status_code=429,
                    error_type="rate_limit_error",
                    code="rate_limit_exceeded",
                )
            bucket.tokens -= 1
