import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..domain.config import GatewayConfig, RouteTarget
from ..domain.errors import GatewayError, UpstreamError, error_payload
from ..domain.models import UsageEvent
from ..domain.ports import UpstreamClient, UsageRepository
from ..infrastructure.tracing.usage_repo import usage_from_payload
from .costing import calculate_cost
from .router import ModelRouter
from .structured import (
    content_from_response,
    repair_instruction,
    schema_from_request,
    validate_response,
)

logger = logging.getLogger(__name__)

IsDisconnected = Callable[[], Awaitable[bool]]


class GatewayService:
    """Orchestrates a single model call: alias routing, retry/fallback, structured
    output validation (with repair), streaming passthrough and usage accounting."""

    def __init__(
        self,
        config: GatewayConfig,
        router: ModelRouter,
        upstream: UpstreamClient,
        prompts: Any,
        usage: UsageRepository,
    ) -> None:
        self._config = config
        self._router = router
        self._upstream = upstream
        self._prompts = prompts
        self._usage = usage

    async def prepare_body(
        self,
        api: str,
        raw_body: dict[str, Any],
        prompt_ref: Any | None,
    ) -> tuple[dict[str, Any], tuple[str | None, int | None]]:
        body = dict(raw_body)
        body.pop("gateway_prompt", None)
        if prompt_ref is None:
            return body, (None, None)
        prompt, rendered = await self._prompts.render(
            prompt_ref.id, prompt_ref.variables, prompt_ref.version
        )
        if api == "chat":
            message = {"role": prompt.role, "content": rendered}
            messages = list(body.get("messages", []))
            if prompt_ref.position == "append":
                messages.append(message)
            else:
                messages.insert(0, message)
            body["messages"] = messages
        else:
            existing = body.get("instructions")
            body["instructions"] = f"{rendered}\n\n{existing}" if existing else rendered
        return body, (prompt.id, prompt.version)

    async def call_json(
        self,
        api: str,
        body: dict[str, Any],
        identity: str,
        prompt_meta: tuple[str | None, int | None],
    ) -> tuple[dict[str, Any], str]:
        request_id = f"req_{uuid.uuid4().hex}"
        started = time.perf_counter()
        requested_model = str(body["model"])
        event = UsageEvent(
            request_id=request_id,
            api_key_hash=identity,
            endpoint=f"/v1/{'chat/completions' if api == 'chat' else 'responses'}",
            requested_model=requested_model,
            prompt_id=prompt_meta[0],
            prompt_version=prompt_meta[1],
        )
        schema = schema_from_request(api, body)
        working_body = dict(body)
        last_error: GatewayError | None = None
        structured_attempt = 0
        try:
            while True:
                result: dict[str, Any] | None = None
                selected: RouteTarget | None = None
                candidates = self._router.candidates(requested_model, api)
                for route_index, target in enumerate(candidates):
                    selected = target
                    upstream_body = {**working_body, "model": target.model, "stream": False}
                    for attempt in range(self._config.retry.max_attempts_per_route):
                        try:
                            result = await self._upstream.request_json(
                                target.provider,
                                self._path(api),
                                upstream_body,
                                request_id,
                            )
                            self._router.record_success(target.provider)
                            input_tokens, output_tokens, cached_tokens = usage_from_payload(result)
                            event.input_tokens += input_tokens
                            event.output_tokens += output_tokens
                            event.cached_tokens += cached_tokens
                            break
                        except UpstreamError as exc:
                            last_error = exc
                            self._router.record_failure(target.provider)
                            if (
                                not exc.retryable
                                or attempt + 1 >= self._config.retry.max_attempts_per_route
                            ):
                                break
                            event.retries += 1
                            await self._backoff(attempt)
                    if result is not None:
                        event.fallbacks = route_index
                        break
                if result is None or selected is None:
                    raise last_error or GatewayError(
                        "All upstream routes failed", status_code=502
                    )

                event.provider = selected.provider
                event.upstream_model = selected.model
                if schema is None:
                    return result, request_id
                try:
                    validate_response(api, result, schema)
                    return result, request_id
                except GatewayError as exc:
                    if structured_attempt >= self._config.structured_output_retries:
                        raise
                    structured_attempt += 1
                    event.retries += 1
                    working_body = self._body_with_repair(
                        api, working_body, result, repair_instruction(exc, schema)
                    )
        except GatewayError as exc:
            event.status = "error"
            event.status_code = exc.status_code
            event.error_type = exc.error_type
            event.error_message = exc.message
            raise
        finally:
            event.latency_ms = round((time.perf_counter() - started) * 1000, 3)
            if event.upstream_model:
                event.cost_usd = calculate_cost(
                    self._config.pricing.get(event.upstream_model),
                    event.input_tokens,
                    event.output_tokens,
                    event.cached_tokens,
                )
            await self._record_usage_safely(event)

    async def stream(
        self,
        api: str,
        body: dict[str, Any],
        identity: str,
        prompt_meta: tuple[str | None, int | None],
        is_disconnected: IsDisconnected,
    ) -> tuple[AsyncIterator[bytes], str]:
        request_id = f"req_{uuid.uuid4().hex}"
        requested_model = str(body["model"])
        event = UsageEvent(
            request_id=request_id,
            api_key_hash=identity,
            endpoint=f"/v1/{'chat/completions' if api == 'chat' else 'responses'}",
            requested_model=requested_model,
            stream=True,
            prompt_id=prompt_meta[0],
            prompt_version=prompt_meta[1],
        )

        async def generate() -> AsyncIterator[bytes]:
            started = time.perf_counter()
            emitted = False
            completed = False
            last_error: GatewayError | None = None
            checkpoint_content = ""
            last_checkpoint_flush = started
            try:
                candidates = self._router.candidates(requested_model, api)
                for route_index, target in enumerate(candidates):
                    upstream_body = {**body, "model": target.model, "stream": True}
                    for attempt in range(self._config.retry.max_attempts_per_route):
                        opened = None
                        try:
                            opened = await self._upstream.open_stream(
                                target.provider,
                                self._path(api),
                                upstream_body,
                                request_id,
                            )
                            event.provider = target.provider
                            event.upstream_model = target.model
                            event.fallbacks = route_index
                            parse_buffer = ""
                            async for chunk in opened.response.aiter_bytes():
                                if await is_disconnected():
                                    event.status = "cancelled"
                                    event.status_code = 499
                                    return
                                if chunk and not emitted:
                                    emitted = True
                                    event.first_token_ms = round(
                                        (time.perf_counter() - started) * 1000, 3
                                    )
                                parse_buffer += chunk.decode("utf-8", errors="ignore")
                                parse_buffer, delta = self._collect_stream_data(parse_buffer, event)
                                if self._config.stream_checkpoint.enabled and delta:
                                    checkpoint_content = (
                                        checkpoint_content + delta
                                    )[-self._config.stream_checkpoint.max_chars :]
                                    now = time.perf_counter()
                                    if (
                                        now - last_checkpoint_flush
                                        >= self._config.stream_checkpoint.flush_interval_seconds
                                    ):
                                        await self._save_checkpoint_safely(
                                            request_id,
                                            api,
                                            requested_model,
                                            "streaming",
                                            checkpoint_content,
                                        )
                                        last_checkpoint_flush = now
                                yield chunk
                            self._router.record_success(target.provider)
                            completed = True
                            return
                        except Exception as exc:  # noqa: BLE001 - normalize to GatewayError
                            upstream_error = self._as_upstream_error(exc, target, emitted)
                            last_error = upstream_error
                            self._router.record_failure(target.provider)
                            if emitted:
                                event.status = "error"
                                event.status_code = 502
                                event.error_type = "stream_error"
                                event.error_message = upstream_error.message
                                yield self._sse_error(upstream_error)
                                yield b"data: [DONE]\n\n"
                                return
                            if (
                                not upstream_error.retryable
                                or attempt + 1 >= self._config.retry.max_attempts_per_route
                            ):
                                break
                            event.retries += 1
                            await self._backoff(attempt)
                        finally:
                            if opened is not None:
                                await opened.response.aclose()
                error = last_error or GatewayError(
                    "All upstream routes failed", status_code=502
                )
                event.status = "error"
                event.status_code = error.status_code
                event.error_type = error.error_type
                event.error_message = error.message
                yield self._sse_error(error)
                yield b"data: [DONE]\n\n"
            except asyncio.CancelledError:
                event.status = "cancelled"
                event.status_code = 499
                raise
            except GatewayError as exc:
                event.status = "error"
                event.status_code = exc.status_code
                event.error_type = exc.error_type
                event.error_message = exc.message
                yield self._sse_error(exc)
                yield b"data: [DONE]\n\n"
            finally:
                if completed:
                    event.status = "success"
                    event.status_code = 200
                event.latency_ms = round((time.perf_counter() - started) * 1000, 3)
                if event.upstream_model:
                    event.cost_usd = calculate_cost(
                        self._config.pricing.get(event.upstream_model),
                        event.input_tokens,
                        event.output_tokens,
                        event.cached_tokens,
                    )
                if self._config.stream_checkpoint.enabled:
                    await self._save_checkpoint_safely(
                        request_id,
                        api,
                        requested_model,
                        event.status,
                        checkpoint_content,
                    )
                await self._record_usage_safely(event)

        return generate(), request_id

    @staticmethod
    def _path(api: str) -> str:
        return "/v1/chat/completions" if api == "chat" else "/v1/responses"

    @staticmethod
    def _as_upstream_error(exc: Exception, target: RouteTarget, emitted: bool) -> UpstreamError:
        if isinstance(exc, UpstreamError):
            return exc
        return UpstreamError(
            f"{target.provider}: stream interrupted: {type(exc).__name__}",
            retryable=not emitted,
        )

    async def _backoff(self, attempt: int) -> None:
        base = self._config.retry.base_delay_seconds * (2**attempt)
        delay = min(base, self._config.retry.max_delay_seconds)
        await asyncio.sleep(delay * random.uniform(0.75, 1.25))

    async def _record_usage_safely(self, event: UsageEvent) -> None:
        try:
            await self._usage.record(event)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to persist usage event %s", event.request_id)

    async def _save_checkpoint_safely(
        self,
        request_id: str,
        api: str,
        model: str,
        status: str,
        content: str,
    ) -> None:
        try:
            await self._usage.save_checkpoint(request_id, api, model, status, content)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to persist stream checkpoint %s", request_id)

    @staticmethod
    def _body_with_repair(
        api: str, body: dict[str, Any], response: dict[str, Any], instruction: str
    ) -> dict[str, Any]:
        repaired = dict(body)
        previous = content_from_response(api, response)
        if api == "chat":
            repaired["messages"] = [
                *body.get("messages", []),
                {"role": "assistant", "content": previous},
                {"role": "user", "content": instruction},
            ]
        else:
            original = body.get("input", "")
            items = (
                list(original)
                if isinstance(original, list)
                else [{"role": "user", "content": original}]
            )
            repaired["input"] = [
                *items,
                {"role": "assistant", "content": previous},
                {"role": "user", "content": instruction},
            ]
        return repaired

    @staticmethod
    def _collect_stream_data(buffer: str, event: UsageEvent) -> tuple[str, str]:
        buffer = buffer.replace("\r\n", "\n")
        text_parts: list[str] = []
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            for line in raw_event.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage_payload = payload.get("response", payload)
                input_tokens, output_tokens, cached_tokens = usage_from_payload(usage_payload)
                if input_tokens or output_tokens:
                    event.input_tokens = input_tokens
                    event.output_tokens = output_tokens
                    event.cached_tokens = cached_tokens
                choices = payload.get("choices") or []
                if choices:
                    content = (choices[0].get("delta") or {}).get("content")
                    if isinstance(content, str):
                        text_parts.append(content)
                if payload.get("type") == "response.output_text.delta":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        text_parts.append(delta)
        return buffer[-65536:], "".join(text_parts)

    @staticmethod
    def _sse_error(error: GatewayError) -> bytes:
        return f"data: {json.dumps(error_payload(error), ensure_ascii=False)}\n\n".encode()
