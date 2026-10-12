import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..domain.config import GatewayConfig
from ..domain.ports import UsageRepository

logger = logging.getLogger(__name__)


class RetentionWorker:
    """Periodically deletes aged stream checkpoints and usage events.

    `sweep()` is a plain coroutine so it can be driven directly (no sleeps) in tests;
    `start()`/`stop()` manage the background loop used by the app lifespan.
    """

    def __init__(self, usage: UsageRepository, config: GatewayConfig) -> None:
        self._usage = usage
        self._config = config.retention
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def sweep(self) -> dict[str, int]:
        now = datetime.now(UTC)
        removed = {
            "checkpoints": await self._usage.prune_checkpoints(
                now - timedelta(hours=self._config.checkpoint_ttl_hours)
            ),
            "usage_events": await self._usage.prune_usage_events(
                now - timedelta(days=self._config.usage_event_ttl_days)
            ),
        }
        if removed["checkpoints"] or removed["usage_events"]:
            logger.info(
                "Retention sweep removed aged rows",
                extra={"event": "retention_sweep", **removed},
            )
        return removed

    def start(self) -> None:
        if not self._config.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="retention-sweep")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._config.sweep_interval_seconds)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed sweep must not kill the loop
                logger.exception("Retention sweep failed")
