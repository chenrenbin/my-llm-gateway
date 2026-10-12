# Spec 02 · infrastructure 层（技术实现，实现 domain/ports）

> 依赖 `00-overview.md` 与 `01-domain.md`。本层是唯一的 I/O 边界：上游调用、用量/checkpoint 持久化、配置加载。

## 任务
`src/gateway/infrastructure/` 下的 provider、tracing、config 实现。

## 交付文件

### `infrastructure/providers/upstream.py`
`HttpxUpstreamClient`（实现 `domain.ports.UpstreamClient`）：

```python
class HttpxUpstreamClient:
    def __init__(self, config: GatewayConfig, client: httpx.AsyncClient | None = None): ...
    async def request_json(self, provider_name, path, body, request_id) -> dict: ...
    async def open_stream(self, provider_name, path, body, request_id) -> OpenStream: ...
    async def close(self) -> None: ...     # 只关闭自建的 AsyncClient
```

- 超时**每请求**从 `config.providers[name]` 构造（`timeout_seconds` 总超时、`connect_timeout_seconds` 连接超时），构造函数不再收 `timeout_seconds` 参数。

- **方言分派**：`HttpxUpstreamClient` 通过 `get_translator(provider)` 获取 `UpstreamTranslator`，按其 `path/api`、`headers`、`encode`、`decode`、`stream_transform()` 动态编排（不再用 `if dialect == "anthropic"` 的硬编码分支）。`anthropic` 方言不支持 `responses` API（命中即抛 `UpstreamError`）。
- `base_url`/`Authorization: Bearer <api_key>` 来自 `config.providers[provider_name]`（`SecretStr` 在本层 `get_secret_value()`），一并带上 `X-Request-ID`；`extra_headers` 可加自定义头。
- **测试**：构造函数注入 `client=httpx.AsyncClient(transport=httpx.MockTransport(handler))`，不触网。
- 错误归一化：上游 `is_error`、连接错误、超时一律抛 `UpstreamError`（含 `retryable` 判定：超时/网络/`retry_statuses` 内为 True）；非 retryable（如 400、401/403 鉴权）为 False，4xx 透传原状态码、5xx 归一 502，details 携带上游错误体。
- 响应读取自 `resp.json()`（JSON 接口）；流式返回 `OpenStream(response=resp, provider_name=prov)`，由调用方 `aiter_bytes()` 消费并 `aclose()`。Anthropic 流式用 `_TranslatedStream` 包裹原始 response：`aiter_bytes()` 产出归一后的 OpenAI SSE，`aclose()` 透传到原始 response。

### `infrastructure/providers/dialects.py`
**可扩展的方言（Translator）抽象**：把上游协议的「端点/头/请求体/响应体/SSE」封装为 `UpstreamTranslator` 实现，`get_translator(provider)` 基于 `ProviderConfig.dialect` 从注册表取实例，`register_translator(dialect, translator)` 可注册自定义方言。

内置：
- `OpenAITranslator`：默认实现（path 由 `api` 映射，headers Bearer + X-Request-ID + extra，透传 encode/decode，无流式 transform）。
- `AnthropicTranslator`：将 Anthropic Messages 归一为网关内部的 OpenAI 规范形状。

契约要点：
- `path(api)`: `api='chat' -> '/v1/chat/completions'`, `api='responses' -> '/v1/responses'`（Anthropic 覆盖为 `/v1/messages`，且 `responses` 时由 `UpstreamClient` 直接拦截）。
- `encode(body)`: Anthropic 抽取 `system`/`developer` → 顶层 `system`，`messages` 保留用户/助手，`max_tokens` 缺省 4096，`stop`→`stop_sequences`，仅透传 `temperature/top_p/stream`。
- `decode(payload)`: Anthropic `content[].text` 合并 → `choices[0].message.content`，`usage.input_tokens/output_tokens` → `prompt_tokens/completion_tokens`，`cache_read_input_tokens` → `prompt_tokens_details.cached_tokens`。
- `stream_transform()`: Anthropic 将事件流翻译为 OpenAI `data:` 块（`content_block_delta.text_delta` → `delta.content`），`message_stop` 前补 `usage` 收尾块并 `data: [DONE]`。

### `infrastructure/tracing/usage_repo.py`
`SqliteUsageRepository`（实现 `UsageRepository`，aiosqlite）：

```python
class SqliteUsageRepository:
    def __init__(self, db_path: str, config: GatewayConfig): ...
    async def initialize(self) -> None: ...    # 建两表 + 索引（幂等；总是建，含 checkpoint 表）
    async def record(self, event: UsageEvent) -> None: ...        # UPSERT by request_id
    async def recent(self, limit: int = 100) -> list[dict]: ...    # limit 上限 1000
    async def save_checkpoint(self, request_id, api, model, status, content) -> None: ...
    async def get_checkpoint(self, request_id) -> dict | None: ...
    async def prune_checkpoints(self, before: datetime) -> int: ...   # 删 updated_at < before，返回删除行数
    async def prune_usage_events(self, before: datetime) -> int: ...  # 删 created_at < before，返回删除行数
```

- 表 `usage_events`：`request_id`(PK)、`created_at`、`api_key_hash`、`endpoint`、`requested_model`、`provider`、`upstream_model`、`stream`、`status`、`status_code`、`input_tokens`、`output_tokens`、`cached_tokens`、`cost_usd`、`latency_ms`、`first_token_ms`、`retries`、`fallbacks`、`error_type`、`error_message`、`prompt_id`、`prompt_version`、`metadata_json`。索引 `created_at`、`requested_model`。
- 表 `stream_checkpoints`：`request_id`(PK)、`api`、`model`、`status`、`content`、`updated_at`（UPSERT）。索引 `updated_at`（供 TTL 清理走索引）。
- 时间戳统一存 `datetime.now(UTC).isoformat()`；`prune_*` 用字符串比较（同一 ISO 格式，字典序即时序）。
- `usage_from_payload(payload) -> (input, output, cached)`：从 OpenAI 格式提取 token（`usage.prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`；或 `usage.input_tokens` / `output_tokens`）。

### `infrastructure/config.py`
```python
DEFAULT_CONFIG_PATH = "config/gateway.yaml"

def load_config(path: str | os.PathLike | None = None) -> GatewayConfig:
```

- `yaml.safe_load` → 字符串键 `$[{]ENV[}-]?[default]...` 展开（`os.path.expandvars` 或自实现 `${VAR:-default}`），api_key 用 `SecretStr`。
- 相对 `database_url` 按 **`Path.cwd()`** 解析为绝对路径。
- **fail-fast**：文件缺失、YAML 非法、`models[].routes[].provider` 未声明 → 抛 `GatewayError(code="gateway_misconfigured", status_code=500)`。

## 约束
- API Key 只在本层解密使用，绝不上抛到 domain/application。
- `UsageRepository` / `UpstreamClient` 均可替换（DI）；`usage_repo` 用 aiosqlite、stream_checkpoint 与用量同一 db。
- 无迁移框架：建表/索引全用 `IF NOT EXISTS`；对既有库加列/改 schema 不自动迁移，需手工迁移或重建。
- `usage_from_payload` 返回三元组，非流/流共用（SSE 内嵌 usage 亦兼容）。

## 验收（integration 要点）
- `HttpxUpstreamClient` 在 MockTransport 下：JSON 正常返回 / 超时抛 `UpstreamError(retryable=True)` / 401 抛 `UpstreamError(retryable=False)`。
- `SqliteUsageRepository` save→recent 取回字段一致；`save_checkpoint` 两次调用后为最新内容（UPSERT）。
- `prune_checkpoints` / `prune_usage_events` 只删 `before` 之前的行并返回删除行数；新行保留。
- `load_config` 在缺 provider 引用时抛错；`${GATEWAY_API_KEY:-default}` 展开正确。