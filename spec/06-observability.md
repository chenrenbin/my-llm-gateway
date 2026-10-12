# Spec 06 · observability 层（可观测）

> 依赖 `00-overview.md`。提供结构化日志；用量/路由数据已入 SQLite（`/admin/usage`、`/admin/routes`），不再需要旧 `/v1/metrics` 聚合端点。

## 任务
`src/gateway/observability/logging_setup.py`。

## 交付文件

### `observability/logging_setup.py`
- `request_id_var: ContextVar[str | None]`：跨协程关联日志的请求 ID。
- `request_id(id) -> ContextManager`：`contextmanager`，设置并在退出时 reset `request_id_var`（`GatewayService` 直接 set/reset 亦可）。
- `class JsonFormatter(logging.Formatter)`：每行一条 JSON `{ts(UTC), level, logger, msg, request_id?, 其它 extra…, exc?}`；按 `record.request_id or request_id_var.get()` 关联，`_STANDARD_ATTRS` 之外的自定义属性一并输出。
- `init(level="INFO") -> None`：清空 root handlers，挂 `StreamHandler(sys.stdout)` + JSON formatter。
- `get_logger(name) -> Logger`。

### 埋点约定
- `GatewayService` 在合约失败/流式中断/checkpoint/usage 落库失败处 `logger.exception(...)`（带 `request_id`）。
- `RetentionWorker` 清理到行时打 `extra={"event": "retention_sweep", …}` 的 info；后台循环异常 `logger.exception`（不退出）。
- 正常主链路不逐请求打 info（避免噪音）；错误/降级/熔断事件为 info/warning。

## 约束
- 日志采集**不得**阻塞主链路；不打明文密钥或对话原文。
- `LOG_TRACE_CONTENT` 概念已成历史——不存对话文本，只存元数据。

## 验收（要点）
- `init()` 后一条 `logger.info("x", extra={"k": 1})` 输出单行合法 JSON，含 `ts/level/logger/msg/k`。
- `request_id_var` 设置后，同 coroutine 内日志含 `request_id`。
- 不存在 `/v1/metrics` 端点（由 `/admin/usage` 取代）。