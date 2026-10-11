from dataclasses import dataclass
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class UsageEvent:
    request_id: str
    api_key_hash: str
    endpoint: str
    requested_model: str
    provider: str | None = None
    upstream_model: str | None = None
    stream: bool = False
    status: str = "success"
    status_code: int = 200
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0
    latency_ms: float = 0
    first_token_ms: float | None = None
    retries: int = 0
    fallbacks: int = 0
    error_type: str | None = None
    error_message: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    metadata: dict[str, Any] | None = None
