"""Upstream protocol dialects (translator-based)."""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from ...domain.config import ProviderConfig

DEFAULT_MAX_TOKENS = 4096


class _BaseTranslator:
    def path(self, api: str) -> str:
        return "/v1/chat/completions" if api == "chat" else "/v1/responses"

    def headers(self, provider: ProviderConfig, request_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
            **provider.extra_headers,
        }
        api_key = provider.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def encode(self, body: dict[str, Any]) -> dict[str, Any]:
        return body

    def decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def stream_transform(
        self,
    ) -> Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]] | None:
        return None


class OpenAITranslator(_BaseTranslator):
    pass


_ANTHROPIC_PATH = "/v1/messages"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _parse_event(raw_event: str) -> tuple[str, dict[str, Any]]:
    event_name = ""
    payload: dict[str, Any] = {}
    for line in raw_event.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                payload = {}
    return event_name, payload


def _delta_chunk(text: str) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def _usage_chunk(input_tokens: int, output_tokens: int, cached_tokens: int) -> bytes:
    chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


class AnthropicTranslator(_BaseTranslator):
    def path(self, api: str) -> str:
        return _ANTHROPIC_PATH

    def headers(self, provider: ProviderConfig, request_id: str) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": provider.api_key.get_secret_value(),
            "anthropic-version": provider.api_version,
            "x-request-id": request_id,
        }
        headers.update(provider.extra_headers)
        return headers

    def encode(self, body: dict[str, Any]) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in body.get("messages", []):
            role = message.get("role")
            if role in ("system", "developer"):
                system_parts.append(_text_of(message.get("content")))
                continue
            messages.append({"role": role, "content": message.get("content")})
        translated: dict[str, Any] = {
            "model": body["model"],
            "max_tokens": body.get("max_tokens")
            or body.get("max_completion_tokens")
            or DEFAULT_MAX_TOKENS,
            "messages": messages,
        }
        if system_parts:
            translated["system"] = "\n\n".join(part for part in system_parts if part)
        for key in ("temperature", "top_p", "stream"):
            if body.get(key) is not None:
                translated[key] = body[key]
        stop = body.get("stop")
        if stop is not None:
            translated["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        return translated

    def decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        return {
            "id": payload.get("id"),
            "object": "chat.completion",
            "model": payload.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _text_of(payload.get("content"))},
                    "finish_reason": payload.get("stop_reason"),
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": int(usage.get("cache_read_input_tokens") or 0)
                },
            },
        }

    def stream_transform(
        self,
    ) -> Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]] | None:
        async def _transform(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            buffer = ""
            input_tokens = 0
            cached_tokens = 0
            output_tokens = 0
            async for chunk in chunks:
                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n\n" in buffer:
                    raw_event, buffer = buffer.split("\n\n", 1)
                    event_name, payload = _parse_event(raw_event)
                    kind = payload.get("type") or event_name
                    if kind == "message_start":
                        usage = (payload.get("message") or {}).get("usage") or {}
                        input_tokens = int(usage.get("input_tokens") or 0)
                        cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
                    elif kind == "content_block_delta":
                        delta = payload.get("delta") or {}
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            yield _delta_chunk(text)
                    elif kind == "message_delta":
                        usage = payload.get("usage") or {}
                        output_tokens = int(usage.get("output_tokens") or output_tokens)
                    elif kind == "message_stop":
                        yield _usage_chunk(input_tokens, output_tokens, cached_tokens)
                        yield b"data: [DONE]\n\n"

        return _transform


_TRANSLATORS: dict[str, _BaseTranslator] = {
    "openai": OpenAITranslator(),
    "anthropic": AnthropicTranslator(),
}


def get_translator(provider: ProviderConfig) -> _BaseTranslator:
    return _TRANSLATORS.get(provider.dialect, _TRANSLATORS["openai"])


def register_translator(dialect: str, translator: _BaseTranslator) -> None:
    _TRANSLATORS[dialect] = translator
