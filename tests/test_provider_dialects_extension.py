"""Dialect extension point: registering custom translators."""

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from tests.conftest import gateway_client, make_config

from gateway.infrastructure.providers import dialects


class DummyTranslator(dialects.OpenAITranslator):
    def path(self, api: str) -> str:
        return "/custom/chat"

    def headers(self, provider, request_id: str) -> dict[str, str]:
        h = super().headers(provider, request_id)
        h["x-dummy"] = "1"
        return h

    def encode(self, body: dict[str, Any]) -> dict[str, Any]:
        b = dict(body)
        b["dummy"] = True
        return b

    def decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        p = dict(payload)
        p["decoded_by_dummy"] = True
        return p

    def stream_transform(
        self,
    ) -> Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]] | None:
        return None


@pytest.mark.asyncio
async def test_register_translator_extends_dialects(tmp_path, auth_headers):
    dialects.register_translator("dummy", DummyTranslator())
    providers = {
        "dummy": {
            "base_url": "https://dummy.test",
            "api_key": "k",
            "dialect": "dummy",
        }
    }
    models = {
        "dummy": {
            "strategy": "priority",
            "routes": [{"provider": "dummy", "model": "m", "api": "chat"}],
        }
    }
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["headers"] = req.headers
        seen["body"] = req.content
        return httpx.Response(200, json={"id": "x", "choices": [{"message": {"content": "hi"}}]})

    try:
        config = make_config(tmp_path / "g.db", providers=providers, models=models)
        async with gateway_client(config, handler) as client:
            r = await client.post(
                "/v1/chat/completions",
                headers=auth_headers,
                json={"model": "dummy", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code == 200
        assert r.json()["decoded_by_dummy"] is True
    finally:
        # restore default translators
        dialects._TRANSLATORS["dummy"] = dialects._TRANSLATORS.get("openai")
    assert seen["path"] == "/custom/chat"
    assert seen["headers"]["x-dummy"] == "1"
