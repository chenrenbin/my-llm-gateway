# Spec 01 · domain 层（纯领域，无框架依赖）

> 依赖总纲 `00-overview.md`。本层**不得** `import fastapi` / `httpx` / `aiosqlite` / 任何 I/O 库。它是整个项目的类型与规则基石。

## 任务
`src/gateway/domain/` 下五个文件：配置模型、统计模型、错误、ports 协议。

## 交付文件与签名

### `domain/config.py`
配置模型（Pydantic），字段全部有默认值，加载时校验：

```python
class RetryConfig(BaseModel):
    max_attempts_per_route: int = Field(default=2, ge=1, le=5)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    max_delay_seconds: float = Field(default=4, ge=0, le=60)
    retry_statuses: set[int] = {408, 409, 429, 500, 502, 503, 504}

class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30, ge=0)

class RateLimitConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)

class StreamCheckpointConfig(BaseModel):
    enabled: bool = False
    flush_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    max_chars: int = Field(default=200_000, ge=1_000, le=10_000_000)

class RetentionConfig(BaseModel):          # 背景清理：按 TTL 删除过期行，封住表增长
    enabled: bool = True
    checkpoint_ttl_hours: int = Field(default=24, ge=1)
    usage_event_ttl_days: int = Field(default=90, ge=1)
    sweep_interval_seconds: float = Field(default=3600, ge=10, le=86_400)

class ProviderConfig(BaseModel):
    base_url: str                                        # 尾部 "/" 在 validator 中归一化
    api_key: SecretStr = SecretStr("")
    enabled: bool = True
    dialect: Literal["openai", "anthropic"] = "openai"
    api_version: str = "2023-06-01"                      # 仅 anthropic 方言使用
    timeout_seconds: float = Field(default=120, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)

class RouteTarget(BaseModel):
    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    api: Literal["chat", "responses", "both"] = "both"

class ModelRouteConfig(BaseModel):
    strategy: Literal["priority", "weighted_round_robin"] = "priority"
    routes: list[RouteTarget] = Field(min_length=1)

class PriceConfig(BaseModel):
    input_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(default=0, ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)

class GatewayConfig(BaseModel):
    service_name: str = "llm-gateway"
    api_keys: list[SecretStr] = Field(default_factory=list)
    database_url: str = "data/gateway.db"
    structured_output_retries: int = Field(default=1, ge=0, le=3)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    stream_checkpoint: StreamCheckpointConfig = Field(default_factory=StreamCheckpointConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    providers: dict[str, ProviderConfig]                      # 必填
    models: dict[str, ModelRouteConfig]                       # 必填
    pricing: dict[str, PriceConfig] = Field(default_factory=dict)
```

约束：`providers` / `models` 必填；`model_validator` 检查 `models[].routes[].provider ∈ providers`，未声明即 `ValueError`（fail-fast，最终由 `load_config` 包装为 `gateway_misconfigured`）。全部模型 `extra="forbid"`。注：`retry_statuses` 为 `set[int]`。

### `domain/models.py`
```python
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
    status: str = "success"          # success | error | cancelled
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
```

### `domain/contracts.py`
```python
class PromptCreate(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    description: str | None = None
    role: Literal["system", "developer"] = "system"
    activate: bool = True            # True → 创建后设为激活版本

class PromptRecord(BaseModel):
    id: str
    version: int
    name: str
    description: str | None
    role: str
    content: str
    is_active: bool
    created_at: str
```

### `domain/errors.py`
```python
class GatewayError(Exception):
    def __init__(self, message, *, status_code=500, error_type="gateway_error",
                 code=None, param=None, details=None): ...

class UpstreamError(GatewayError):       # 默认 502，error_type="upstream_error"，retryable: bool = False
    ...

class StructuredOutputError(GatewayError):  # 422, error_type="structured_output_error", code="invalid_model_output"
    ...

def error_payload(error) -> dict:         # OpenAI 风格 {"error":{message,type,param,code}}
```

### `domain/ports.py`
见 `00-overview.md` §3.1：`OpenStream`、`UpstreamClient`、`UsageRepository`。

## 约束
- `errors.py` 只定义异常与 `error_payload`，不含 I/O。
- `models.py` 用 dataclass（`UsageEvent` 为账本输入，非外部契约）。
- `config.py` 不读文件，只承担解析后校验；读取在 `infrastructure/config.py`。

## 验收（unit 要点）
- `GatewayConfig` 构造时引用未声明 provider → 抛错。
- `UpstreamError(message, retryable=True)` 保留 `retryable`；默认 `retryable=False`、`error_type=="upstream_error"`。
- `RetentionConfig` 默认 `enabled=True`（checkpoint 24h / usage 90d），`sweep_interval_seconds ∈ [10, 86400]`。
- `error_payload` 输出结构含 `error.message/type/param/code`。
- `PromptCreate` 的 id 不匹配合法模式 → 校验失败；`activate=True` 语义为「创建并设为激活」。