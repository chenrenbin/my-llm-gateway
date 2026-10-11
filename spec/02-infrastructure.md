# Spec 02 · infrastructure 层（技术实现，实现 domain/ports）

> 依赖 `00-overview.md` 与 `01-domain.md`。本层是唯一的 I/O 边界：上游调用、用量/checkpoint 持久化、配置加载。

## 任务
`src/gateway/infrastructure/` 下的 provider、tracing、config 实现。

## 交付文件

### `infrastructure/providers/upstream.py`
`HttpxUpstreamClient`（实现 `domain.ports.UpstreamClient`）：

```python
class HttpxUpstreamClient:
    def __init__(self, config: GatewayConfig, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 120): ...
    async def request_json(self, provider_name, path, body, request_id) -> dict: ...
    async def open_stream(self, provider_name, path, body, request_id) -> OpenStream: ...
    async def close(self) -> None: ...     # 只关闭自建的 AsyncClient
```

- `base_url`/`Authorization: Bearer <api_key>` 来自 `config.providers[provider_name]`（`SecretStr` 在本层 `get_secret_value()`），一并带上 `X-Request-ID`；`extra_headers` 可加自定义头。
- **测试**：构造函数注入 `client=httpx.AsyncClient(transport=httpx.MockTransport(handler))`，不触网。
- 错误归一化：上游 `is_error`、连接错误、超时一律抛 `UpstreamError`（含 `retryable` 判定：超时/网络/`retry_statuses` 内为 True）；非 retryable（如 400、401/403 鉴权）为 False，4xx 透传原状态码、5xx 归一 502，details 携带上游错误体。
- 响应读取自 `resp.json()`（JSON 接口）；流式返回 `OpenStream(response=resp, provider_name=prov)`，由调用方 `aiter_bytes()` 消费并 `aclose()`。

### `infrastructure/tracing/usage_repo.py`
`SqliteUsageRepository`（实现 `UsageRepository`，aiosqlite）：

```python
class SqliteUsageRepository:
    def __init__(self, db_path: str, config: GatewayConfig): ...   # 无 checkpoint 时可不建 checkpoint 表
    async def initialize(self) -> None: ...    # 建两表
    async def record(self, event: UsageEvent) -> None: ...
    async def recent(self, limit: int = 100) -> list[dict]: ...
    async def save_checkpoint(self, request_id, api, model, status, content) -> None: ...
    async def get_checkpoint(self, request_id) -> dict | None: ...
    async def close(self) -> None: ...
```

- 表 `usage_events`：`request_id`(PK)、`api_key_hash`、`endpoint`、`requested_model`、`upstream_model`、`provider`、`prompt_id`、`prompt_version`、`stream`、`input_tokens`、`output_tokens`、`cached_tokens`、`cost_usd`、`latency_ms`、`first_token_ms`、`retries`、`fallbacks`、`status`、`status_code`、`error_type`、`error_message`、`created_at`。
- 表 `stream_checkpoints`：`request_id`(PK)、`api`、`model`、`status`、`content`、`updated_at`（UPSERT）。
- `usage_from_payload(payload) -> (input, output, cached)`：从 OpenAI 格式提取 token（`usage.prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`；或 `usage.input_tokens` / `output_tokens`）。

### `infrastructure/config.py`
```python
DEFAULT_CONFIG_PATH = "config/gateway.yaml"

def load_config(path: str | os.PathLike | None = None) -> GatewayConfig:
```

- `yaml.safe_load` → 字符串键 `$[{]ENV[}-]?[default]...` 展开（`os.path.expandvars` 或自实现 `${VAR:-default}`），api_key 用 `SecretStr`。
- 相对 `database_url` 按 **`Path.cwd()`** 解析为绝对路径。
- **fail-fast**：文件缺失、YAML 非法、`models[].routes[].provider` 未声明 → 抛 `GatewayError("gateway_misconfigured",…,503)`。

## 约束
- API Key 只在本层解密使用，绝不上抛到 domain/application。
- `UsageRepository` / `UpstreamClient` 均可替换（DI）；`usage_repo` 用 aiosqlite、stream_checkpoint 与用量同一 db。
- `usage_from_payload` 返回三元组，非流/流共用（SSE 内嵌 usage 亦兼容）。

## 验收（integration 要点）
- `HttpxUpstreamClient` 在 MockTransport 下：JSON 正常返回 / 超时抛 `UpstreamError(retryable=True)` / 401 抛 `UpstreamError(retryable=False)`。
- `SqliteUsageRepository` save→recent 取回字段一致；`save_checkpoint` 两次调用后为最新内容。
- `load_config` 在缺 provider 引用时抛错；`${GATEWAY_API_KEY:-default}` 展开正确。