# Spec 05 · prompts 层（运行时受控模板）

> 依赖 `00-overview.md` `01-domain.md`。管理网关**运行时**的受控系统提示词（与 `spec/` 生成规格无关）。

## 任务
`src/gateway/prompts/repository.py`：版本化模板仓储 + 渲染；REST 入口在 `04-api.md`。

## 交付文件

### `prompts/repository.py` — `class PromptRepository`
构造注入 `database_path: str`（与用量账本同库，另一张表）。

**存储（SQLite `prompts` 表）**
- 列：`id, version, name, description, role, content, is_active(0/1), created_at`；PK `(id, version)`；索引 `(id, is_active)`。

**API**
- `initialize()`：建表（幂等）。
- `create_version(prompt: PromptCreate) -> PromptRecord`：
  - `BEGIN IMMEDIATE` 下 `SELECT COALESCE(MAX(version),0)+1` 得新版本号（**自动递增**；`PromptCreate` 不含 `version` 字段，版本由服务端分配）。
  - `activate=True` 时先清空同 id 的激活标记，再插入 `is_active=1`。
- `get(prompt_id, version=None) -> PromptRecord`：`version` 缺省取激活版本（`is_active=1 ORDER BY version DESC`）；找不到抛 `GatewayError(404, "prompt_not_found")`。
- `list() -> list[PromptRecord]`：全量，按 `id, version DESC`。
- `render(prompt_id, variables, version=None) -> (prompt: PromptRecord, rendered: str)`：
  - `SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)` 编译模板并 `render(**variables)`；
  - 缺变量 / 语法错误 → `GatewayError(422, "prompt_render_error")`（**不静默**，模板不走 eval/sandbox 之外能力）。

### 渲染语义（调用方）
- `GatewayService.prepare_body`：chat → 按 `position`(prepend/append) 插入 `{role, content}` 到 `messages`；responses → 追加到 `instructions`。
- 变量内容来自请求 `gateway_prompt.variables`，全部为 `dict[str, Any]`。

## 约束
- 模板正文在 DB、不在代码（外部化）；版本化使「改提示词」=「新建版本并激活」，可回滚。
- 用 **Jinja2 Sandbox + StrictUndefined**：模板无法触达任意属性/全局函数，缺变量即报错。
- 调用方只能传 `id + version + variables`，不能提交任意模板（安全约束）。

## 验收（unit 要点）
- `create_version` 两次 → 版本 1、2，且 `activate=True` 的第二次后仅该版本 `is_active=1`。
- `render` 缺变量 → 422 `prompt_render_error`；模板含 `{{ mystery }}` 未传 → 同样报错（StrictUndefined）。
- `get` 未知 id → 404 `prompt_not_found`。
- Sandbox：模板调用 `{{ self.__class__.__mro__ }}` 之类被拒绝或渲染为空，不得执行。