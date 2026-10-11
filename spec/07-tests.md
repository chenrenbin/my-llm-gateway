# Spec 07 · tests 层（测试）

> 依赖全部前序 spec。目标：覆盖核心行为 + 1-7 兼容协议 + 工程化能力（路由/熔断/修复/SSE/responses/checkpoint/版本化 prompt）。**不得真打供应商**（`httpx.MockTransport`）。

## 目录
```
tests/
├── conftest.py            # make_config / gateway_client / auth_headers
├── test_gateway.py        # 端到端（走 app lifespan + ASGI 客户端 + MockTransport 上游）
└── test_units.py          # 配置/路由/成本/结构化 纯逻辑单测
```

## `tests/conftest.py`
- `make_config(database: Path, **overrides) -> GatewayConfig`：真实 `GatewayConfig`（默认 primary+fallback 两 provider、`smart` 别名 priority、pricing），测试可覆写任意段。
- `gateway_client(config, handler)`：`httpx.AsyncClient(transport=MockTransport(handler))` 注入 `create_app(config, http_client=…)`，用 `ASGITransport` 驱动真实 app。
- `auth_headers`：`{"Authorization": "Bearer test-key"}`。

## `tests/test_units.py`（纯逻辑，不触网不落库）
- config：`${ENV}` 展开 + db 路径解析；未知 provider → `gateway_misconfigured`。
- router：未知模型 404；`api` 过滤但为空 → 503 `no_healthy_route`；weighted 轮换 primary；熔断达阈值后排除 + `status()` open。
- costing：cached 折扣数值正确；price 缺失返回 0。
- structured：chat/responses 的 schema 提取；fenced JSON 去除；非法 JSON / 不符 schema → `invalid_model_output`；`repair_instruction` 前缀正确；responses `output_text` 提取与失败路径。

## `tests/test_gateway.py`（端到端，6 条）
1. `chat retries then falls back and records usage`：primary 两次退避重试均 429 → fallback 成功；断言调用序、`x-request-id`、`/admin/usage` 记账（provider/retries/fallbacks/tokens/cost）。
2. `structured output is locally validated and repaired`：首次上游返回 `{"name":123}` 不符 schema → 二次请求带 repair 指令 → 返回合规 JSON。
3. `prompt version render and injection`：`POST /v1/prompts` 建 v1 → render 校验 → chat 引用 `gateway_prompt` 后上游收到渲染后的 system 消息。
4. `streaming sse is forwarded and usage is captured`：SSE 逐块透传 + `[DONE]`；内嵌 usage 记账；开启 checkpoint 后 `/v1/streams/{id}/checkpoint` 找回正文。
5. `responses api and auth error`：无 key 访问 `/v1/models` → 401 `authentication_error`；`POST /v1/responses` happy 路由 `/v1/responses` 上游。
6. `rate limit uses openai error shape`：打满后 429 `rate_limit_exceeded`，/v1/models 不触上游。

## 约束
- 单测不触网不落库；端到端依赖 `MockTransport`（上游在进程内 mock）+ `ASGITransport` 驱动真实 app。
- 断言 OpenAI 错误结构 `{"error":{message,type,param,code}}`。
- 覆盖率目标：domain + application 核心 100%，每条外部端点至少 1 happy + 1 错误路径。

## 验收（整体）
- `pytest -q` 全绿；`ruff check src tests` 无错。
- 跑通即证明：重构未破坏协议（OpenAI 兼容）、修复/降级/限流/checkpoint/版本化 prompt 有背书。