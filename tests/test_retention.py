from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from tests.conftest import make_config

from gateway.application.maintenance import RetentionWorker
from gateway.domain.models import UsageEvent
from gateway.infrastructure.tracing.usage_repo import SqliteUsageRepository


def _event(request_id: str) -> UsageEvent:
    return UsageEvent(
        request_id=request_id,
        api_key_hash="fingerprint",
        endpoint="/v1/chat/completions",
        requested_model="smart",
    )


async def _repo(tmp_path, **overrides):
    config = make_config(tmp_path / "gateway.db", **overrides)
    repo = SqliteUsageRepository(config.database_url, config)
    await repo.initialize()
    return config, repo


async def test_prune_checkpoints_honours_cutoff(tmp_path):
    _, repo = await _repo(tmp_path)
    await repo.save_checkpoint("req_a", "chat", "m", "success", "hi")
    await repo.save_checkpoint("req_b", "chat", "m", "cancelled", "ho")

    now = datetime.now(UTC)
    assert await repo.prune_checkpoints(now - timedelta(hours=1)) == 0
    assert await repo.get_checkpoint("req_a") is not None

    assert await repo.prune_checkpoints(now + timedelta(hours=1)) == 2
    assert await repo.get_checkpoint("req_a") is None


async def test_prune_usage_events_honours_cutoff(tmp_path):
    _, repo = await _repo(tmp_path)
    await repo.record(_event("req_1"))
    await repo.record(_event("req_2"))

    now = datetime.now(UTC)
    assert await repo.prune_usage_events(now - timedelta(hours=1)) == 0

    assert await repo.prune_usage_events(now + timedelta(hours=1)) == 2
    assert await repo.recent() == []


async def test_sweep_deletes_only_expired_rows(tmp_path):
    config, repo = await _repo(
        tmp_path,
        retention={"enabled": True, "checkpoint_ttl_hours": 1, "usage_event_ttl_days": 1},
    )
    await repo.save_checkpoint("fresh", "chat", "m", "success", "keep")
    expired = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    async with aiosqlite.connect(config.database_url) as db:
        await db.execute(
            "INSERT INTO stream_checkpoints(request_id, api, model, status, content, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("old", "chat", "m", "success", "drop", expired),
        )
        await db.commit()

    removed = await RetentionWorker(repo, config).sweep()

    assert removed["checkpoints"] == 1
    assert await repo.get_checkpoint("old") is None
    assert await repo.get_checkpoint("fresh") is not None


@pytest.mark.asyncio
async def test_worker_does_not_start_when_disabled(tmp_path):
    config, repo = await _repo(tmp_path, retention={"enabled": False})
    worker = RetentionWorker(repo, config)

    worker.start()
    assert worker.running is False
    await worker.stop()


async def test_worker_start_and_stop_manages_background_task(tmp_path):
    config, repo = await _repo(
        tmp_path, retention={"enabled": True, "sweep_interval_seconds": 3600}
    )
    worker = RetentionWorker(repo, config)

    worker.start()
    assert worker.running is True

    await worker.stop()
    assert worker.running is False
