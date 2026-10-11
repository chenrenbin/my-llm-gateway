# Spec 03 · application 层（编排 / 用例）

> 依赖 `00-overview.md` `01-domain.md` `02-infrastructure.md`。只依赖 **domain + ports 抽象** + DI，不得 import FastAPI / 具体 provider。

## 任务
`src/gateway/application/` 五个文件：路由、核心编排、结构化输出、成本、限流。

## 交付文件

### `application/router.py`
`ModelRouter(config: GatewayConfig)`：
- `candidates(model, api) -> list[RouteTarget]`：
  1. 别名不存在 → `GatewayError(404, "model_not_found", param="model")`。
  2. 过滤 `provider.enabled` 为 False、`route.api` 不含该协议（`api in (route.api, "both")`）、熔断打开 的 route。
  3. 过滤后为空 → `GatewayError(503, "no_healthy_route")`。
  4. `priority`：按配置序返回；`weighted_round_robin`：按 `weight` 重复后取当前计数起点为 primary，其余作 fallback。
- `record_success(provider)` / `record_failure(provider)`：进程内 `CircuitState(failures, opened_at)`；失败 ≥ `failure_threshold` → 开路，`cooldown_seconds` 后半开放行。
- `status() -> dict[provider, {failures, open}]`（供 `/admin/routes`）。

### `application/gateway.py`
`GatewayService(config, router, upstream, prompts, usage)`：
- `prepare_body(api, raw_body, prompt_ref) -> (body, (prompt_id, prompt_version))`：剥离 `gateway_prompt`；chat 按 `position`(prepend/append) 插入渲染后的 system 消息；responses 拼到 `instructions`。
- `schema_from_request(api, body)` / `content_from_response` / `validate_response` / `repair_instruction` 见 `application/structured.py`。
- `call_json(api, body, identity, prompt_meta) -> (result, request_id)`：
  - 求 `candidates` → 每 route 内 `max_attempts_per_route` 次调用（仅 `upstream_error.retryable` 才退避重试，指数退避 × 0.75~1.25 抖动）；成功 `record_success`，失败 `record_failure`。
  - 全部失败 → 抛最后错误（或 502 `All upstream routes failed`）。
  - 有 schema → `validate_response`；失败 `repair_instruction` 追加 assistant+user 消息回喂（`structured_output_retries` 次），仍失败抛 `StructuredOutputError`.
  - `finally` 填 `UsageEvent`（tokens/cost/latency/retries/fallbacks/status）并 best-effort 落库。
- `stream(api, body, identity, prompt_meta, is_disconnected) -> (async_iterator_bytes, request_id)`：
  - 内部 `generate()`：对候选评分同 call_json；`aiter_bytes()` 逐块原样 forward；**首块前**可重试/切路由；**已 emitted 后**失败仅发 `data: {"error":…}` + `data: [DONE]` 并 return，绝不续写。
  - `is_disconnected()` 为真或 `CancelledError` → 记 `cancelled`(499)。
  - 期间解析 SSE 提取增量文本（chat `choices[].delta.content`；responses `type=="response.output_text.delta"` 的 `delta`）用于 checkpoint；最终 usage 从嵌在流内的 usage 事件提取。
  - checkpoint 开关开启时按 `flush_interval_seconds` UPSERT 到账本。完成/失败/取消都在 `finally` 记账。

### `application/structured.py`
- `schema_from_request(api, body) -> dict|None`：chat 取 `response_format.json_schema.schema`；responses 取 `text.format.schema`；非 `json_schema` 返回 None。
- `content_from_response(api, payload) -> str`：chat `choices[0].message.content`；responses `output_text` 或 `output[].content[].text`；找不到抛 `StructuredOutputError`。
- `validate_response(api, payload, schema) -> parsed`：去 Markdown fence → `json.loads` → `jsonschema.validate`；失败抛 `StructuredOutputError`（附带 `lineno/colno/path/message` 详情）。
- `repair_instruction(error, schema) -> str`：生成回喂提示（含校验错误与 schema）。

### `application/costing.py`
```python
def calculate_cost(price: PriceConfig | None, input_tokens, output_tokens, cached_tokens=0) -> float:
    # price is None → 0.0
    # fresh_input = max(0, input - cached)
    # cached_rate = cached_input_per_million or input_per_million
    # cost = (fresh*in + cached*cached_rate + out*out) / 1e6,  round 10
```
- `price` 为 None 返回 0（未计价的模型不报错、不计费）；`cached_tokens` 超 input 时按 0 计。

### `application/rate_limit.py`
`InMemoryRateLimiter(config.rate_limit)`：令牌桶（按 identity 分桶，`asyncio.Lock` 保护）；
`async def check(identity) -> None`：超限抛 `GatewayError(429, "rate_limit_exceeded")`（含 retry_after 计算）；`enabled=False` 时直接放行。

## 约束
- 不依赖 FastAPI；`is_disconnected` 由 api 层注入。
- 保持语义：重试仅临时故障；首块后不重试；计费用实际模型；用量/checkpoint 落库失败不阻塞（catch + log）。
- `ModelRouter` 无 I/O：candidates 纯内存决策。
- SSE 的 `data:` 字节编码由 `GatewayService._sse_error` 产出，`stream()` 不感知 HTTP 框架。

## 验收（unit 要点）
- `candidates`：别名不存在 404；无健康路由 503；`api=responses` 过滤只含 both/responses 的 route；weighted 轮换确实会轮转 primary。
- 熔断：连错 N 次后 `candidates` 排除该 provider，冷却后恢复。
- `call_json`：primary 首次 retryable 失败、二次成功 → `attempts/retries` 记账正确；全链失败抛错且 event.status=="error"。
- `validate_response`：fenced JSON 去围栏成功；缺字段/非法 JSON 抛 `invalid_model_output`。
- `calculate_cost`：含 cached 折扣正确；price 缺失返回 0。