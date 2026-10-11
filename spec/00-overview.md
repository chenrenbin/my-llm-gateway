# LLM Gateway 工程化规格（Spec）

> **用途**：这份 spec 记录并经驱动过的重构：把单文件 `gateway.py` 重构为分层工程化项目，且对外协议对齐**OpenAI 兼容 API**（`/v1/chat/completions`、`/v1/responses`、`/v1/models`）。
> **读法**：按 `00 → 07` 顺序读取各层定义。各层 spec 依赖本总纲的接口契约与硬约束，请勿跨层破坏契约。
> **目标定位**：本服务是 **Agent 技术栈最底层的「统一模型调用 / 治理网关」**，向上提供受控、可计量、可治理的
> 模型调用；**不**编排 Agent 推理循环、不管工具/记忆/规划（那些是上层 Harness / Agent Loop 的职责）。

---

## 1. 技术栈

- Python ≥ 3.12
- FastAPI（HTTP 适配层） + uvicorn
- Pydantic v2
- httpx（上游 OpenAI 兼容客户端的底层传输；`HttpxUpstreamClient`）
- aiosqlite（用量账本 / prompt 仓储 / checkpoint 持久化）
- Jinja2（Sandbox 模板渲染，严格变量）
- jsonschema（结构化输出本地校验）
- PyYAML（`config/gateway.yaml` 配置）
- 测试：pytest + `httpx.MockTransport` 模拟上游 + FastAPI `TestClient`
- 代码质量：ruff（lint）

---

## 2. 目录结构（单一事实源）

```
llm-gateway/
├── pyproject.toml
├── README.md
├── Makefile
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore                         # 含 data/ 与 .env
├── config/
│   └── gateway.yaml                   # providers + models(routes) + pricing + retry/limits
└── src/gateway/
    ├── main.py                        # create_app() / build_app()（lifespan 装配、异常 handler）
    ├── api/
    │   ├── routes.py                  # /v1/chat/completions · /v1/responses · /v1/models ·
    │   │                              #   /v1/prompts · /admin/usage · /admin/routes ·
    │   │                              #   /v1/streams/{id}/checkpoint · /healthz · /readyz
    │   ├── requests.py                # ChatCompletionRequest/ResponsesRequest/...（extra=allow 透传）
    │   ├── responses.py               # ModelObject/ModelList/ErrorDetail/ErrorResponse
    │   └── security.py                # Bearer / X-Api-Key 鉴权 + 密钥指纹
    ├── application/
    │   ├── gateway.py                 # GatewayService：call_json / stream（路由→重试→修复→记账）
    │   ├── router.py                  # ModelRouter：candidates / 熔断 / weighted_round_robin
    │   ├── structured.py              # schema 提取 / 内容抽取 / JSON Schema 校验 / 修复指令
    │   ├── costing.py                 # calculate_cost（含 cached 折扣）
    │   └── rate_limit.py              # InMemoryRateLimiter（令牌桶）
    ├── domain/
    │   ├── config.py                  # GatewayConfig/ProviderConfig/RouteTarget/ModelRouteConfig/
    │   │                              #   RetryConfig/CircuitBreakerConfig/RateLimitConfig/
    │   │                              #   StreamCheckpointConfig/PriceConfig
    │   ├── models.py                  # UsageEvent
    │   ├── contracts.py               # PromptCreate / PromptRecord
    │   ├── errors.py                  # GatewayError/UpstreamError/StructuredOutputError/error_payload
    │   └── ports.py                   # UpstreamClient/OpenStream/UsageRepository（Protocol）
    ├── infrastructure/
    │   ├── providers/upstream.py      # HttpxUpstreamClient（request_json / open_stream）
    │   ├── tracing/usage_repo.py      # SqliteUsageRepository（usage_events + stream_checkpoints）+ usage_from_payload
    │   └── config.py                  # load_config（yaml + ${ENV} 展开 + fail-fast）
    ├── prompts/repository.py          # PromptRepository（版本化 + Jinja2 Sandbox）
    └── observability/logging_setup.py # 结构化 JSON 日志
└── tests/
    ├── conftest.py                    # make_config / gateway_client / auth_headers
    ├── test_gateway.py                # 端到端：chat/SSE/responses/prompt/限流/404/熔断
    └── test_units.py                  # 配置/路由/成本/结构化 单测
```

---

## 3. 跨层接口契约（必须遵守）

### 3.1 `domain/ports.py`（Protocol，无实现）

```python
@dataclass
class OpenStream:
    response: Any          # httpx 响应（可 aiter_bytes / aclose）
    provider_name: str

class UpstreamClient(Protocol):
    async def request_json(provider_name, path, body, request_id) -> dict: ...
    async def open_stream(provider_name, path, body, request_id) -> OpenStream: ...
    async def close(self) -> None: ...

class UsageRepository(Protocol):
    async def initialize(self) -> None: ...
    async def record(self, event: UsageEvent) -> None: ...
    async def recent(self, limit: int = 100) -> list[dict]: ...
    async def save_checkpoint(self, request_id, api, model, status, content) -> None: ...
    async def get_checkpoint(self, request_id) -> dict | None: ...
```

### 3.2 `domain/errors.py`

`GatewayError(message, *, status_code=500, error_type="gateway_error", code=None, param=None, details=None)`；
`UpstreamError`（默认 502，含 `retryable` 标志）与 `StructuredOutputError`（422, `invalid_model_output`）为子类；
`error_payload(error) -> {"error":{message,type,param,code[,details]}}` 统一 OpenAI 风格错误体。

### 3.3 `application/gateway.py`

`GatewayService(config, router, upstream, prompts, usage)`，暴露：

- `call_json(api, body, identity, prompt_meta) -> (dict, request_id)`（非流式）
- `stream(api, body, identity, prompt_meta, is_disconnected) -> (AsyncIterator[bytes], request_id)`（SSE 字节流）
  内部不 `import` FastAPI/具体 provider；流式断连通过 `is_disconnected: Callable[[], Awaitable[bool]]` 注入。

### 3.4 `infrastructure/config.py`

`load_config(path=None) -> GatewayConfig`：读 YAML，`${ENV}` / `${ENV:-default}` 展开，`SecretStr` 包密钥，
相对 db 路径按 **cwd** 解析；未知 provider / YAML 非法启动即抛（fail-fast）。

---

## 4. 硬约束（全局，任何层都不得违反）

1. 最外层（路由）对**未知字段透传**（`extra="allow"`），保证 OpenAI SDK 兼容；结构化输出能力透传上游（JSON Schema / text.format），本地做二次校验。
2. 分层隔离：`domain` 不依赖 FastAPI / httpx；`application` 只依赖 `domain` + `ports` + DI；唯一 I/O 边界在 `infrastructure`。
3. 配置 fail-fast、不热加载：未知 provider、非法 YAML 启动即失败；改配置需重启。
4. 路由/重试/熔断规则：`ModelRouter` 过滤不可用 provider → 每路由内重试（`max_attempts_per_route`，指数退避+抖动，仅 retryable）→ 降级下一候选。
5. 流式语义：**首块已发出后失败不重试/不切路由**，只发 `data: {"error":...}` + `[DONE]`，避免重复文本；客户端断连（`is_disconnected`）或 `CancelledError` → `cancelled`(499) 记账。
6. 结构化输出：`schema_from_request` 提取 → `validate_response` 校验 → 失败由 `repair_instruction` 回喂模型修复（`structured_output_retries` 次），最终失败 422。
7. 计费用**实际上游模型**价格（含 cached 折扣），`calculate_cost` 入 `UsageEvent`；用量落地失败不阻塞主链路（best-effort + 异常日志）。
8. 隐私与安全：账本存 `api_key_hash`（指纹），不存明文密钥/文本；API Key 只在 infrastructure 解密。
9. 错误标准化：内部统一 `GatewayError`，api 层映射 OpenAI 风格错误对象，不泄露内部堆栈。

---

## 5. 对外行为（OpenAI 兼容）

- `POST /v1/chat/completions`：OpenAI Chat 格式，`stream=true` 走 SSE；`gateway_prompt` 引用受控模板。
- `POST /v1/responses`：Responses 格式，路由需 `api: responses|both`；`text.format: {type: json_schema, schema}`。
- `GET /v1/models`：返回 `gateway.yaml` 的公开别名。
- Prompt 管理：`POST/GET /v1/prompts`、`GET /v1/prompts/{id}`、`POST /v1/prompts/{id}/render`（版本化 + activate）。
- 管理/运维：`GET /admin/usage`、`GET /admin/routes`、`GET /v1/streams/{request_id}/checkpoint`、`/healthz`、`/readyz`。
- 鉴权：Bearer / `X-Api-Key`（`api_keys` 配置，留空=开发模式）；限流 429 带 `Retry-After`。

---

## 6. 建议顺序

`01-domain` → `02-infrastructure` → `03-application` → `04-api` → `05-prompts` → `06-observability` → `07-tests`。每层单测通过后再进下一层。

---

## 7. 下一步路线（不在本版，标注即可）

- OTel trace 接入（替换/增强结构化日志）
- 多副本部署：限流 / 熔断迁 Redis、用量迁 PostgreSQL/ClickHouse
- 远程配置中心 / 动态路由 / FinOps 降本路由
- HITL / 护栏（参照 Agent 工程化调研）
