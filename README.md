# LLM Gateway

面向 Agent Engineering Platform 的**统一模型入口**：向上提供 **OpenAI 兼容 API**，对下连接多个
OpenAI-compatible 供应商，在网关层集中处理模型路由、流式转发、结构化输出、Prompt 模板、用量与成本
记录、重试、限流和故障转移。

> **职责边界**：本服务不编排 Agent 推理循环（工具 / 记忆 / 规划属于上层 Harness）。它只负责把一次模型
> 调用安全、可控、可计量地发出去。

---

## 已实现能力

- `POST /v1/chat/completions`：Chat Completions 兼容入口（含 SSE 流式）
- `POST /v1/responses`：Responses API 兼容入口（含 SSE 流式）
- `GET /v1/models`：返回网关公开的模型别名
- 多供应商、模型别名、`priority` / `weighted_round_robin` 路由 + 轻量熔断器
- 首选失败后按路由重试并 fallback；仅对网络错误 / 超时 / 可配状态码重试
- SSE 逐块透明转发；客户端断开时取消上游连接
- `response_format.json_schema` / `text.format` 透传及本地 JSON Schema 二次校验，失败回喂模型修复
- Prompt 模板：SQLite 版本化 + 激活版本 + Jinja2 Sandbox 严格变量渲染
- SQLite 用量账本：Token（含 cached）、Cost、Latency、TTFT、重试、fallback、错误
- OpenAI 风格错误对象、Bearer / `X-Api-Key` 鉴权、进程内令牌桶限流
- `/healthz` `/readyz` 健康检查、`/admin/usage` `/admin/routes` 管理接口
- 可选流式 checkpoint：断线后按 `X-Request-ID` 找回已生成正文（默认关闭）

---

## 架构与分层

依赖方向严格单向，`application` 只依赖 `domain`，具体 I/O 实现经 ports/DI 注入：

```
        ┌──────────────────────────────────────────────┐
        │                    api/                       │  HTTP 适配：OpenAI 兼容 / SSE / 鉴权 / 限流
        └───────────────┬──────────────────────────────┘
                        │
        ┌───────────────▼──────────────────────────────┐
        │              application/                     │  用例编排：路由 / 重试降级 / 结构化修复 / 成本
        └───────┬───────────────────────┬──────────────┘
                │ 依赖抽象               │ 注入具体实现
        ┌───────▼────────┐     ┌────────▼──────────────────────────────┐
        │    domain/     │◄────│            infrastructure/             │
        │ 配置/错误/ports │     │ httpx 上游客户端 · SQLite 用量 · 配置加载 │
        └────────────────┘     └───────────────────────────────────────┘
             prompts/（SQLite Prompt 仓储）· observability/（JSON 日志）
```

- **domain**：纯领域：配置模型、错误、ports 协议；**不** import fastapi / httpx。
- **infrastructure**：唯一 I/O 边界：`HttpxUpstreamClient`、`SqliteUsageRepository`、`load_config`。
- **application**：路由、`GatewayService`（`call_json`/`stream`）、结构化校验/修复、成本、限流。
- **api**：HTTP 翻译、SSE 编码、鉴权、路由；不含业务编排。

---

## 目录结构

```
llm-gateway/
├── pyproject.toml
├── Dockerfile
├── .env.example
├── config/
│   └── gateway.yaml                # providers + models(routes) + pricing + retry/limits
├── src/gateway/
│   ├── main.py                     # create_app() / build_app()（lifespan 装配）
│   ├── api/
│   │   ├── routes.py               # chat/responses/models/prompts/admin/health
│   │   ├── requests.py             # OpenAI 兼容入参（extra=allow 透传）
│   │   ├── responses.py            # ModelList / ErrorResponse
│   │   └── security.py             # 鉴权 + 密钥指纹
│   ├── application/
│   │   ├── gateway.py              # GatewayService：call_json / stream
│   │   ├── router.py               # ModelRouter：route/filter/weighted/circuit
│   │   ├── structured.py           # schema 提取 / 校验 / 修复指令
│   │   ├── costing.py              # calculate_cost（含 cached 折扣）
│   │   └── rate_limit.py           # InMemoryRateLimiter
│   ├── domain/
│   │   ├── config.py               # GatewayConfig 等配置模型
│   │   ├── models.py               # Usage / UsageEvent
│   │   ├── contracts.py            # PromptCreate / PromptRecord
│   │   ├── errors.py               # GatewayError / UpstreamError / error_payload
│   │   └── ports.py                # UpstreamClient / UsageRepository
│   ├── infrastructure/
│   │   ├── providers/upstream.py   # httpx 上游客户端（json + stream）
│   │   ├── tracing/usage_repo.py   # SQLite 用量账本 + checkpoints
│   │   └── config.py               # load_config（yaml + ${ENV} 展开）
│   ├── prompts/repository.py       # 版本化 Prompt（SQLite + Jinja2 Sandbox）
│   └── observability/logging_setup.py
└── tests/
    ├── conftest.py                 # make_config / gateway_client / auth_headers
    ├── test_gateway.py             # 端到端：路由/修复/prompt/SSE/responses/限流
    └── test_units.py               # 配置/路由/成本/结构化 单测
```

---

## 快速开始

要求 Python ≥ 3.11。

```bash
# 1) 安装
uv venv --python 3.12 && uv pip install -e ".[dev]"

# 2) 配置密钥
cp .env.example .env
#   在 .env 填入 DEEPSEEK_API_KEY 等；或设置 GATEWAY_API_KEY 开启调用方鉴权

# 3) 启动
GATEWAY_CONFIG=config/gateway.yaml uvicorn gateway.main:build_app --factory --port 8000

# 4) 冒烟
curl -s localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"你好"}]}'
```

健康检查：`/healthz`、`/readyz`；OpenAPI：`/docs`。

### 使用 OpenAI SDK

> 说明：`openai` 仅出现在下述**客户端示例**中，供你的调用方使用；本网关服务端依赖只有 `httpx`（见 `pyproject.toml`）。

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="<GATEWAY_API_KEY 或任意值>")

resp = client.chat.completions.create(
    model="general-primary",
    messages=[{"role": "user", "content": "解释什么是 Agent Loop"}],
)
print(resp.choices[0].message.content)
```

---

## 配置说明（`config/gateway.yaml`）

```yaml
service_name: llm-gateway
api_keys: ["${GATEWAY_API_KEY:-}"]     # 留空=开发模式不校验调用方
database_url: data/gateway.db
structured_output_retries: 1

retry:
  max_attempts_per_route: 2
  base_delay_seconds: 0.25
  max_delay_seconds: 4
  retry_statuses: [408, 409, 429, 500, 502, 503, 504]

circuit_breaker: {failure_threshold: 5, cooldown_seconds: 30}
rate_limit: {enabled: true, requests_per_minute: 60, burst: 10}
stream_checkpoint: {enabled: false, flush_interval_seconds: 1, max_chars: 200000}
# 后台清理：按 TTL 删除过期 checkpoint / usage 记录，防止行数无限增长
retention: {enabled: true, checkpoint_ttl_hours: 24, usage_event_ttl_days: 90, sweep_interval_seconds: 3600}

providers:
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY:-}

models:
  general-primary:
    strategy: priority            # priority | weighted_round_robin
    routes:
      - {provider: deepseek, model: deepseek-v4-flash, api: both}
      - {provider: deepseek_backup, model: deepseek-chat, api: both}

pricing:                          # USD / 1M tokens
  deepseek-v4-flash: {input_per_million: 1.0, output_per_million: 4.0}
```

- `${ENV}` / `${ENV:-default}` 在加载时展开；`api_key` 用 `SecretStr`，只在 infrastructure 层解密。
- **fail-fast**：YAML 非法、路由指向未声明的 provider 都会导致启动失败。
- `api` 取值 `chat` / `responses` / `both`，决定该路由可服务的协议。

---

## API 参考

### `POST /v1/chat/completions`

标准 OpenAI Chat Completions 请求体（未知字段原样透传）。`stream=true` 时返回 `text/event-stream`，
逐块透明转发上游 SSE。`gateway_prompt` 可引用一个受控模板：

```json
{
  "model": "general-primary",
  "messages": [{"role": "user", "content": "请审查以下代码……"}],
  "gateway_prompt": {"id": "code-reviewer", "variables": {"language": "Python"}}
}
```

### `POST /v1/responses`

Responses API 兼容入口，路由目标需声明 `api: responses` 或 `both`。

### `GET /v1/models`

返回 `gateway.yaml` 中所有公开别名。

### Prompt 管理

`POST /v1/prompts`（创建新版本，`activate` 切换激活）· `GET /v1/prompts` · `GET /v1/prompts/{id}` ·
`POST /v1/prompts/{id}/render`。模板用 Jinja2 Sandbox + 严格变量，缺变量报错。

### 管理

`GET /admin/usage?limit=`（用量账本）· `GET /admin/routes`（路由 + 熔断状态）·
`GET /v1/streams/{request_id}/checkpoint`（开启 checkpoint 后找回正文）。

---

## 路由 / 重试 / 降级语义

1. `ModelRouter.candidates(alias, api)`：按别名取路由，过滤 `enabled=False`、协议不匹配、熔断打开的 provider。
2. `priority` 按配置顺序；`weighted_round_robin` 按 `weight` 轮转首选，其余作为 fallback。
3. 每个 route 内最多重试 `max_attempts_per_route` 次；仅 `retryable`（网络 / 超时 / `retry_statuses`）才退避重试，指数退避 + 抖动。
4. 非流式：结构化校验失败会把校验错误回喂模型修复（`structured_output_retries`），最终失败返回 422。
5. 流式：首个数据块前可重试 / 切路由；**一旦已发送内容**，失败只发 `data: {"error": ...}` + `[DONE]`，**不续写**，避免重复文本。
6. 熔断：provider 连续失败达阈值后短期开路，冷却后半开放行。

## 错误语义

统一 OpenAI 风格：`{"error": {"message", "type", "param", "code"}}`。

| HTTP | 场景 |
| --- | --- |
| 401 | 网关 API Key 无效 |
| 404 | 模型别名 / Prompt / checkpoint 不存在 |
| 422 | 请求校验失败 / 模板渲染失败 / 结构化输出不合规 |
| 429 | 网关限流（带 `Retry-After`） |
| 502/503 | 上游失败 / 无健康路由 |

---

## 测试

```bash
pytest -q
ruff check src tests
```

使用 `httpx.MockTransport` 模拟上游，不消耗真实额度；覆盖路由回退、重试、结构化修复、Prompt、SSE、
Responses API、鉴权、限流与 checkpoint。

---

## 生产化建议

- 密钥放 Secrets Manager，不提交真实值。
- 多副本部署时把限流 / 熔断状态迁移到 Redis。
- 高吞吐把 SQLite 换成 PostgreSQL/ClickHouse 并异步批量写入用量。
- 按供应商能力维护路由（Responses API、JSON Schema、工具调用、context window）。
- 公网入口增加 TLS / WAF / 审计；管理接口拆分到内网。
