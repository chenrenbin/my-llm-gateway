import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ...domain.config import GatewayConfig, ProviderConfig
from ...domain.errors import UpstreamError
from ...domain.ports import OpenStream
from .dialects import (
    get_translator,
)


class _TranslatedStream:
    """Wraps an httpx response so its bytes are normalised to the OpenAI SSE shape."""

    def __init__(
        self,
        response: httpx.Response,
        translate: Callable[[AsyncIterator[bytes]], AsyncIterator[bytes]],
    ) -> None:
        self._response = response
        self._translate = translate

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self._translate(self._response.aiter_bytes()):
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class HttpxUpstreamClient:
    """Upstream client implementing domain.ports.UpstreamClient.

    Speaks OpenAI-compatible by default and translates the Anthropic Messages
    protocol to the same canonical shape. Plain `httpx`, so it can be tested
    with `httpx.MockTransport` injected via `client`. Provider API keys are read
    here and never leave this layer.
    """

    def __init__(self, config: GatewayConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _path(self, provider: ProviderConfig, api: str) -> str:
        return get_translator(provider).path(api)

    def _encode(self, provider: ProviderConfig, body: dict[str, Any]) -> dict[str, Any]:
        return get_translator(provider).encode(body)

    def _decode(self, provider: ProviderConfig, payload: dict[str, Any]) -> dict[str, Any]:
        return get_translator(provider).decode(payload)

    def _headers(self, provider: ProviderConfig, request_id: str) -> dict[str, str]:
        return get_translator(provider).headers(provider, request_id)

    def _timeout(self, provider: ProviderConfig) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=provider.timeout_seconds,
            connect=provider.connect_timeout_seconds,
        )

    async def request_json(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        provider = self._config.providers[provider_name]
        if provider.dialect == "anthropic" and path.endswith("/responses"):
            raise UpstreamError(
                f"{provider_name}: anthropic dialect does not support the responses API"
            )
        url = f"{provider.base_url}{self._path(provider, path)}"
        try:
            response = await self._client.post(
                url,
                headers=self._headers(provider, request_id),
                json=self._encode(provider, body),
                timeout=self._timeout(provider),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(f"{provider_name}: {type(exc).__name__}", retryable=True) from exc
        if response.is_error:
            raise self._http_error(provider_name, response)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"{provider_name}: upstream returned invalid JSON") from exc
        return self._decode(provider, payload)

    async def open_stream(
        self,
        provider_name: str,
        path: str,
        body: dict[str, Any],
        request_id: str,
    ) -> OpenStream:
        provider = self._config.providers[provider_name]
        if provider.dialect == "anthropic" and path.endswith("/responses"):
            raise UpstreamError(
                f"{provider_name}: anthropic dialect does not support the responses API"
            )
        url = f"{provider.base_url}{self._path(provider, path)}"
        request = self._client.build_request(
            "POST",
            url,
            headers={**self._headers(provider, request_id), "Accept": "text/event-stream"},
            json=self._encode(provider, body),
            timeout=self._timeout(provider),
        )
        try:
            response = await self._client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(f"{provider_name}: {type(exc).__name__}", retryable=True) from exc
        if response.is_error:
            await response.aread()
            error = self._http_error(provider_name, response)
            await response.aclose()
            raise error
        stream: httpx.Response | _TranslatedStream = response
        transform = get_translator(provider).stream_transform()
        if transform is not None:
            stream = _TranslatedStream(response, transform)
        return OpenStream(response=stream, provider_name=provider_name)

    def _http_error(self, provider_name: str, response: httpx.Response) -> UpstreamError:
        retryable = response.status_code in self._config.retry.retry_statuses
        message = f"{provider_name}: HTTP {response.status_code}"
        details: Any = None
        try:
            details = response.json()
            upstream_message = (
                details.get("error", {}).get("message") if isinstance(details, dict) else None
            )
            if upstream_message:
                message = f"{message}: {upstream_message}"
        except (json.JSONDecodeError, AttributeError):
            details = response.text[:1000]
        status = response.status_code if 400 <= response.status_code < 500 else 502
        return UpstreamError(message, status_code=status, retryable=retryable, details=details)
