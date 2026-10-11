# Spec 04 · api 层（HTTP 适配，OpenAI 兼容协议）

> 依赖 `00-overview.md` `01-domain.md` `03-application.md`。本层是 HTTP↔`GatewayService` 翻译、OpenAI 风格错误与 SSE 编码的唯一地方，**不含业务编排**。

## 任务
`src/gateway/api/` 四个文件 + `src/gateway/main.py` 装配。

## 交付文件

### `api/requests.py`（Pydantic 入参）
- `FlexibleModel`：`extra="allow"`，**未知字段透传上游**（OpenAI SDK 兼容的落点）。
- `ChatMessage(FlexibleModel)`：`role`（system/developer/user/assistant/tool/function）、`content: Any`、`name/tool_call_id` 可选。
- `PromptReference(FlexibleModel)`：`id`、`version?`、`variables`、`position: prepend|append`。
- `ChatCompletionRequest(FlexibleModel)`：`model`、`messages`、`stream=False`、`temperature/top_p/max_tokens/max_completion_tokens/response_format`、`gateway_prompt?`。
- `ResponsesRequest(FlexibleModel)`：`model`、`input: Any`、`stream`、`temperature/top_p/max_output_tokens/text`、`gateway_prompt?`。
- `PromptRender`：`extra="forbid"`，`version?` + `variables`；`PromptCreate` 复用 `domain/contracts`。

### `api/responses.py`
- `ModelObject(id, created, owned_by="llm-gateway")` / `ModelList(object="list", data: list[ModelObject])`（`GET /v1/models`）。
- `ErrorDetail(message, type, param, code[, details])` / `ErrorResponse(error: ErrorDetail)`（统一错误模型）。
- `PromptRecord` **re-export** 自 `domain.contracts`（供 `/v1/prompts` 响应）。

### `api/security.py`
- `key_fingerprint(key: str) -> str`：sha256 前 16 位（账本只存指纹，不存明文）。
- `authenticate(request) -> str`（Depends）：
  - 空 `api_keys`（开发模式）→ 返回 `key_fingerprint("dev")`。
  - 否则校验 `Authorization: Bearer <key>` 或 `X-Api-Key: <key>`（HMAC constant-time 比较）；失败抛 `GatewayError(401, "invalid_api_key", error_type="invalid_request_error")`。

### `api/routes.py`（全部端点）
每个受保护端点组依赖 `limited_identity = authenticate + InMemoryRateLimiter.check(identity)`（429 抛 `GatewayError`，由全局 handler 加 `Retry-After` 头）。

- `POST /v1/chat/completions`：`prepare_body("chat", …)`；`stream` → `StreamingResponse(gateway.stream("chat",…, request.is_disconnected))`（`media_type=text/event-stream`，禁缓冲头，`X-Request-ID`）；否则 `call_json` 返回 JSON + `X-Request-ID`。
- `POST /v1/responses`：同上，`api="responses"`。
- `GET /v1/models`：`data=[{id,created} for alias in config.models]`。
- `POST /v1/prompts`(201) / `GET /v1/prompts` / `GET /v1/prompts/{prompt_id}?version=` / `POST /v1/prompts/{id}/render`：委托 `app.state.prompts`。
- `GET /admin/usage?limit=`（默认 100, ≤1000）→ `{data:[…]}`。
- `GET /admin/routes` → `{models:{…}, circuits: router.status()}`。
- `GET /v1/streams/{request_id}/checkpoint`：缺失 → 404 `checkpoint_not_found`。
- `GET /healthz`（`{status:ok}`）/ `GET /readyz`（`{status,service}`，`include_in_schema=False`）。

### `main.py`（装配）
- `create_app(config|None, *, http_client|None)`：
  - lifespan：`mkdir(db parent)` → `PromptRepository` + `SqliteUsageRepository`（同 db）→ `initialize()` → `ModelRouter` → `HttpxUpstreamClient(config, http_client)`（可选注入 `http_client` 便于测试）→ 组装 `InMemoryRateLimiter` + `GatewayService` → 挂 `app.state.*`；退出时 `upstream.close()`。
  - `add_exception_handler(GatewayError, …)`：`status_code == 429` 时加 `Retry-After: 1`；响应 `error_payload`。
  - `add_exception_handler(RequestValidationError, …)`：422 + `type=invalid_request_error, code=validation_error, details=exc.errors()`。
- `build_app(config|None, *, http_client|None)`：生产工厂（`uvicorn gateway.main:build_app --factory`）：`load_config()`（fail-fast）→ `logging_setup.init()` → `create_app(...)`。

## 约束
- 入参宽松（passthrough）、错误响应统一 OpenAI 结构；不再出现旧 `/v1/llm`、`/v1/traces`、`/v1/metrics`。
- 所有账号主体信息只进指纹；api 层不写业务日志细节。
- 流式用 `request.is_disconnected` 注入 `GatewayService`，api 层不直接 import 上游 Provider。

## 验收（api 测试要点）
- `POST /v1/chat/completions`（chat happy）返回 API 结果 + `X-Request-ID`；`stream=true` 得 SSE 流（`content-type: text/event-stream`）。
- 未知模型 → 404 `{"error":{"code":"model_not_found",…}}`；无健康路由 → 503。
- `X-Api-Key` 与 `Authorization: Bearer` 两路鉴权均工作；错误密钥 → 401。
- 限流触发 → 429 + `Retry-After` 头。
- `/v1/models` 返回所有别名；`/admin/usage`、`/admin/routes`、checkpoint(含 404) 符合契约。