"""Streaming robustness: client disconnect / cancellation and checkpoint recovery.

These drive the real `GatewayService.stream` with a scripted `is_disconnected`
callback and the real `SqliteUsageRepository`, so no sleeps or timing hacks are
needed to deterministically cancel a stream mid-flight.
"""

from typing import Any

import pytest
from tests.conftest import make_config

from gateway.application.gateway import GatewayService
from gateway.application.router import ModelRouter
from gateway.domain.ports import OpenStream
from gateway.infrastructure.tracing.usage_repo import SqliteUsageRepository

CHUNK_1 = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
CHUNK_2 = b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'
CHUNK_DONE = b"data: [DONE]\n\n"

BODY = {"model": "smart", "messages": [{"role": "user", "content": "hi"}], "stream": True}


class FakeResponse:
    def __init__(self, chunks, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        self.closed = True


class FakeUpstream:
    """Minimal implementation of the `UpstreamClient` port."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def request_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("json path must not be used for a streaming request")

    async def open_stream(self, provider_name: str, path: str, body: Any, request_id: str):
        return OpenStream(response=self.response, provider_name=provider_name)

    async def close(self) -> None:
        return None


def scripted_disconnect(after: int):
    """is_disconnected: False for the first `after` polls, then True forever."""
    polls = {"n": 0}

    async def is_disconnected() -> bool:
        polls["n"] += 1
        return polls["n"] > after

    return is_disconnected


async def _service(tmp_path, response: FakeResponse, **overrides: Any):
    config = make_config(tmp_path / "gateway.db", **overrides)
    usage = SqliteUsageRepository(config.database_url, config)
    await usage.initialize()
    service = GatewayService(config, ModelRouter(config), FakeUpstream(response), None, usage)
    return service, usage


async def _drain(service, disconnect):
    body = {"model": "smart", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    iterator, request_id = await service.stream(
        "chat", body, "caller-fingerprint", (None, None), disconnect
    )
    chunks = [chunk async for chunk in iterator]
    return chunks, request_id


@pytest.mark.asyncio
async def test_client_disconnect_cancels_stream_and_closes_upstream(tmp_path):
    response = FakeResponse([CHUNK_1, CHUNK_2, b"data: [DONE]\n\n"])
    service, usage = await _service(
        tmp_path,
        response,
        stream_checkpoint={"enabled": True, "flush_interval_seconds": 1, "max_chars": 10_000},
    )

    chunks, request_id = await _drain(service, scripted_disconnect(after=1))

    assert request_id.startswith("req_")
    assert chunks == [CHUNK_1], "streaming must stop immediately on disconnect"
    assert b"data: [DONE]" not in b"".join(chunks)
    assert response.closed is True, "upstream response must be closed on disconnect"

    event = (await usage.recent())[0]
    assert event["status"] == "cancelled"
    assert event["status_code"] == 499
    assert event["first_token_ms"] is not None
    assert event["stream"] == 1

    checkpoint = await usage.get_checkpoint(request_id)
    assert checkpoint is not None
    assert checkpoint["status"] == "cancelled"
    assert checkpoint["content"] == "Hi"


@pytest.mark.asyncio
async def test_checkpoint_is_not_written_when_disabled(tmp_path):
    response = FakeResponse([CHUNK_1, CHUNK_2, b"data: [DONE]\n\n"])
    service, usage = await _service(tmp_path, response)  # stream_checkpoint defaults off

    _, request_id = await _drain(service, scripted_disconnect(after=1))

    assert await usage.get_checkpoint(request_id) is None


@pytest.mark.asyncio
async def test_failure_after_first_chunk_emits_error_and_stops(tmp_path):
    response = FakeResponse([CHUNK_1], error=RuntimeError("connection reset"))
    service, usage = await _service(tmp_path, response)

    chunks, _ = await _drain(service, scripted_disconnect(after=1_000))  # never disconnects
    body = b"".join(chunks)

    assert CHUNK_1 in body
    assert b'"error"' in body
    assert b"data: [DONE]" in body
    assert response.closed is True

    event = (await usage.recent())[0]
    assert event["status"] == "error"
    assert event["status_code"] == 502
    assert event["error_type"] == "stream_error"
