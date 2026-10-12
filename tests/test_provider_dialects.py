"""Upstream protocol dialects: OpenAI (default) and Anthropic Messages.

The gateway keeps a single canonical (OpenAI) contract for callers while a
provider's `dialect` decides how the upstream is spoken to. These tests exercise
the Anthropic translation end to end through the real app + MockTransport, plus
the pure translation helpers.
"""

import json
from typing import Any

import httpx
import pytest
from tests.conftest import gateway_client, make_config

from gateway.infrastructure.providers.dialects import (
    AnthropicTranslator,
)

ANTHROPIC_PROVIDERS: dict[str, Any] = {
    "claude": {
        "base_url": "https://anthropic.test",
        "api_key": "sk-ant-test",
        "dialect": "anthropic",
        "api_version": "2023-06-01",
    }
}

ANTHROPIC_MODELS: dict[str, Any] = {
    "claude": {
        "strategy": "priority",
        "routes": [{"provider": "claude", "model": "claude-sonnet-4-5", "api": "chat"}],
    }
}

ANTHROPIC_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-4-5",'
    b'"usage":{"input_tokens":12,"output_tokens":0,"cache_read_input_tokens":4}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" Claude"}}\n\n'
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":'
    b'{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _config(tmp_path):
    return make_config(
        tmp_path / "gateway.db",
        providers=ANTHROPIC_PROVIDERS,
        models=ANTHROPIC_MODELS,
        pricing={"claude-sonnet-4-5": {"input_per_million": 3, "output_per_million": 15}},
    )


_ANTHRO = AnthropicTranslator()

def test_request_body_splits_system_and_defaults_max_tokens():
    body = {
        "model": "claude-sonnet-4-5",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "stop": "STOP",
    }

    translated = _ANTHRO.encode(body)

    assert translated["system"] == "be terse"
    assert translated["messages"] == [{"role": "user", "content": "hi"}]
    assert translated["max_tokens"] == 4096
    assert translated["stop_sequences"] == ["STOP"]


def test_anthropic_response_maps_content_and_usage():
    translated = _ANTHRO.decode(
        {
            "id": "msg_1",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3},
        }
    )
    assert translated["choices"][0]["message"]["content"] == "Hello"
    assert translated["choices"][0]["finish_reason"] == "end_turn"
    assert translated["usage"]["prompt_tokens"] == 11
    assert translated["usage"]["completion_tokens"] == 7
    assert translated["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


@pytest.mark.asyncio
async def test_anthropic_non_stream_request_and_usage(tmp_path, auth_headers):
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "Hello from Claude"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )

    config = make_config(
        tmp_path / "gateway.db", providers=ANTHROPIC_PROVIDERS, models=ANTHROPIC_MODELS
    )
    async with gateway_client(config, handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "claude",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "Hello from Claude"

        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["input_tokens"] == 11
        assert usage["output_tokens"] == 7

    assert seen["path"] == "/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["model"] == "claude-sonnet-4-5"
    assert seen["body"]["system"] == "be terse"
    assert seen["body"]["max_tokens"] == 4096
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_stream_is_translated_to_openai_sse(tmp_path, auth_headers):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=ANTHROPIC_SSE, headers={"content-type": "text/event-stream"}
        )

    config = make_config(
        tmp_path / "gateway.db", providers=ANTHROPIC_PROVIDERS, models=ANTHROPIC_MODELS
    )
    async with gateway_client(config, handler) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "claude", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as response:
            body = (await response.aread()).decode()
        assert response.status_code == 200
        assert '"content": "Hello"' in body
        assert '"content": " Claude"' in body
        assert '"completion_tokens": 5' in body
        assert "data: [DONE]" in body

        usage = (await client.get("/admin/usage", headers=auth_headers)).json()["data"][0]
        assert usage["input_tokens"] == 12
        assert usage["output_tokens"] == 5
        assert usage["cached_tokens"] == 4
        assert usage["status"] == "success"
