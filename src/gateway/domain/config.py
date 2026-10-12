from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts_per_route: int = Field(default=2, ge=1, le=5)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    max_delay_seconds: float = Field(default=4, ge=0, le=60)
    retry_statuses: set[int] = {408, 409, 429, 500, 502, 503, 504}


class CircuitBreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30, ge=0)


class RateLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)


class StreamCheckpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    flush_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    max_chars: int = Field(default=200_000, ge=1_000, le=10_000_000)


class RetentionConfig(BaseModel):
    """Bounds table growth by age; a background sweep deletes rows past their TTL."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    checkpoint_ttl_hours: int = Field(default=24, ge=1)
    usage_event_ttl_days: int = Field(default=90, ge=1)
    sweep_interval_seconds: float = Field(default=3600, ge=10, le=86_400)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: SecretStr = SecretStr("")
    enabled: bool = True
    timeout_seconds: float = Field(default=120, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_url(self) -> "ProviderConfig":
        self.base_url = self.base_url.rstrip("/")
        return self


class RouteTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    api: Literal["chat", "responses", "both"] = "both"


class ModelRouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["priority", "weighted_round_robin"] = "priority"
    routes: list[RouteTarget] = Field(min_length=1)


class PriceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(default=0, ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str = "llm-gateway"
    api_keys: list[SecretStr] = Field(default_factory=list)
    database_url: str = "data/gateway.db"
    structured_output_retries: int = Field(default=1, ge=0, le=3)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    stream_checkpoint: StreamCheckpointConfig = Field(default_factory=StreamCheckpointConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelRouteConfig]
    pricing: dict[str, PriceConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_routes(self) -> "GatewayConfig":
        unknown = {
            route.provider
            for model in self.models.values()
            for route in model.routes
            if route.provider not in self.providers
        }
        if unknown:
            raise ValueError(f"routes reference unknown providers: {sorted(unknown)}")
        return self
