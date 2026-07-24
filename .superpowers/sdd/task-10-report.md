# Task 10 本机实现报告

## 状态与范围

Task 10A–10D 的本机实现已完成，等待 controller 独立审查和规定的远程部署节奏。本实现只新增严格 DTO、`DocumentGateway` 注入端口、FastAPI REST、纯 ASGI API Key 鉴权和 3 个 FastMCP 工具；没有实现 Task 11 整页方向算法，也没有组合真实 MinerU/Paddle 服务。

## 交付内容

- 严格公开 DTO：来源恰好为上传 `file_id` 或 HTTPS URL；规范 UUID；1–20 个来源；有界幂等键；恢复请求只接受令牌和唯一正页码；所有 DTO `extra="forbid"`。
- `DocumentGateway` async port：上传流保留有界 `display_name` 和可选上传幂等键，REST/MCP 共享解析、状态、恢复 DTO。
- REST：`POST /v1/uploads`、`POST /v1/tasks`、`GET /v1/tasks/{batch_id}`、`POST /v1/orientation-reparse`，显式 OpenAPI operation ID；上传按流转交且执行类型、元数据、声明/实际字节上限。
- 鉴权：纯 ASGI，不缓冲 MCP 流；除 `/health/live` 外全部 fail-closed；支持 Bearer 和 `X-API-Key`；重复、格式错误、无效或冲突凭据不会进入 gateway；使用常量时间比较。
- MCP：仅 `parse_documents`、`get_task_status`、`reparse_with_page_orientation`；FastMCP `http_app(path="/mcp")` lifespan 传给 FastAPI；进度单调、无 message/正文；无 progress token 仍成功。
- 错误：REST 返回稳定 code + 通用消息；MCP 返回稳定 code + 通用消息；FastMCP validation 由中间件屏蔽 Pydantic input diagnostics，避免回显文件名、URL 或业务值。

## TDD 证据

- 10A RED：`tests/test_api_contracts.py` 收集失败，缺少 `ocr_mcp_server.api.contracts`；GREEN：`17 passed`。
- 10B RED：auth 配置被 `extra_forbidden`，REST/auth 测试 `11 failed`；GREEN：contracts/auth/REST/settings `106 passed`。
- 上传元数据 RED：fake gateway 报缺少 `display_name`/`idempotency_key`；GREEN 后传输名和上传幂等键保持在输入边界且不进入响应。
- 规范任务 ID RED：`/v1/tasks/not-a-uuid` 返回 500；GREEN 后非 UUID、36 字符伪 UUID和大写非规范 UUID均在 gateway 前返回安全 422。
- 10C RED：缺少 FastMCP 依赖和 `api.mcp`，`5 failed`；GREEN：FastMCP in-process 初始化、list-tools、call-tools、progress 和 HTTP fail-closed 全部通过。
- MCP validation 泄漏 RED：FastMCP 原始 Pydantic 错误包含 `recognized-private-customer-name.pdf`；GREEN：安全 validation middleware 返回通用 `invalid_request`，不含输入值。
- 全套首次回归发现两个旧骨架断言仍限定原依赖集合/禁止出现配置字段名：`2 failed, 712 passed, 13 skipped`。根因是 Task 10 有意增加 FastMCP 和空 `auth.api_keys` 配置；更新契约测试后全套通过。

## 本机验证

最终命令：

```text
.\.venv\Scripts\python.exe -m pytest -rs
714 passed, 13 skipped in 12.02s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 1624c6b..HEAD
exit 0, no output
```

## 依赖说明

- 项目锁定 `fastmcp>=2.13.1,<3`；本机解析为 FastMCP 2.14.7。
- FastMCP 要求 `uvicorn>=0.35`，项目约束已调整为 `uvicorn>=0.35,<0.52`，`pip check` 为绿。
- FastMCP 2.14.7 自身强依赖 `fakeredis[lua]`，因此传递安装 `redis`；应用源码没有 `redis`/`fakeredis` import、没有 Redis URL/配置，也没有外部 Redis 运行依赖。测试显式证明这些边界。

## 提交

- `d5b1cbb` — `feat: define strict document gateway contracts`
- `d6b2593` — `feat: expose authenticated REST workflow`
- `27eaf6c` — `feat: expose three authenticated MCP tools`
- 最终报告/回归测试更新使用后续文档提交。

## 待 controller 完成

- 独立审查安全、工具数量、lifespan、进度和错误脱敏。
- 审查 READY 后再 push、bundle/checksum 同步 Ubuntu、Linux 全套、镜像构建、立即启动和认证 REST/MCP smoke、不可变版本固定。

本 worker 未 push、未 SSH、未部署、未修改父实施计划完成状态。

## 最终安全自审收口

- RED 证明：使用默认 `TestClient(raise_server_exceptions=True)` 时，gateway 抛出的包含业务文本的未知异常虽生成 500 响应，仍被 Starlette `ServerErrorMiddleware` 重抛，存在服务器日志记录 cause 的风险。REST 现在在每个 gateway await 边界捕获未知异常并 `raise GatewayFailure() from None`；同一回归返回安全 `internal_error` 且不再重抛。
- MCP list-tools schema 原先没有显示 sources/page 数量约束。新增 schema 断言先以 `KeyError: minItems` 失败；工具签名现显式发布 sources `1..20` 和 pages `1..500`，运行时仍复用严格 DTO 处理唯一页码等不易用 JSON Schema 表达的约束。
- 鉴权不再保存/比较可变长度原始 key。配置 key 与来访凭据先计算固定 32-byte SHA-256 digest，再对每个已配置 digest 执行 `hmac.compare_digest`；冲突的双凭据也使用固定长度 digest 比较。
- 最终 focused API/MCP/settings/app：`119 passed`。
- 最终提交后的仓库全套：`715 passed, 13 skipped in 12.01s`；`pip check`、`compileall -q src tests`、`git diff --check 1624c6b..HEAD` 均 exit 0，worktree clean。

## 独立审查 Important 收口

独立审查在 `97af3f8` 返回 NOT READY（5 Important）。所有问题均先以确定性回归复现，再在 `6887183` 修复：

1. FastMCP SDK-level JSON Schema 校验在 middleware 外回显输入值，unknown tool 回显工具名，未验证的 gateway dict 还能把额外业务字段写入 MCP content。RED 原样出现 `SENSITIVE_INPUT_VALUE`、`unknown-SENSITIVE_TOOL_VALUE` 和 `SENSITIVE_OUTPUT_VALUE`。服务现禁用外层 SDK prevalidation，让相同公开 schema 在 FunctionTool/safe middleware 内验证；`_SafeFastMCP` 统一封装 middleware 外错误；每个工具在返回框架前显式用对应严格 DTO 验证 gateway 输出。三类协议错误现在分别稳定为安全 `invalid_request`、`not_found`、`internal_error`，不含原值。
2. FastAPI `response_model` 在 route wrapper 外验证，恶意 gateway 额外字段触发的 `ResponseValidationError` 包含 `SENSITIVE_GATEWAY_OUTPUT` 并被 ServerErrorMiddleware 重抛。每条 REST gateway 调用现于 route 内显式 `model_validate` 对应响应 DTO，并将任何输出异常转成无 cause `GatewayFailure`；默认 `raise_server_exceptions=True` 回归返回安全 500 而不重抛。
3. `AuthenticationSettings` 的 before validator 会在 Pydantic `str(exc)`、`errors()` 和 `json()` 中保留完整无效/重复 key。配置模型现在在进入 Pydantic 错误构造前，把合法 key 包装成 `SecretStr`，把任何非法集合替换为无秘密哨兵；三种错误表示均不含原 key。
4. 方向恢复页码原先会把 Boolean、整值 float 和数字字符串转换为 int。公开 DTO 和 MCP schema 现在使用 `StrictInt`；REST/MCP 回归证明 `true`、`1.0`、`"1"` 均在 gateway 前拒绝。
5. 上传路由原先通过 `Headers.get()` 合并/隐藏重复元数据。现在直接枚举 ASGI `scope["headers"]`，对重复 `X-Document-Name`、`Idempotency-Key`、`Content-Type`（即使值相同）返回安全 422，且 gateway 调用数为 0。

审查修复后的验证：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_api_contracts.py tests/test_api_auth.py tests/test_rest_api.py tests/test_mcp_api.py tests/test_settings.py tests/test_app.py -q
129 passed

.\.venv\Scripts\python.exe -m pytest -rs
725 passed, 13 skipped in 12.38s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0

git diff --check 97af3f8..HEAD
exit 0
```

## 第二次复审收口

复审 `f9ab6f2` 发现 2 个剩余 Important，均在 `35e3e0d` 以 TDD 修复：

1. `_safe_call` 原先无条件重新抛出 `ToolError`。恶意/错误 gateway 因而可用 `ToolError("private-customer-filename.pdf")` 绕过安全错误映射。协议 Client RED 原样收到该文件名。现在 gateway await 边界只信任本项目的 `GatewayFailure`；包括 `ToolError` 在内的其余异常全部成为无 cause `internal_error`。`_require_gateway` 在 awaitable 构造前产生的服务未配置安全错误仍由外层保留。
2. 上传路由虽拒绝三个业务 metadata header 的重复值，但仍通过合并视图读取 `Content-Length`。双 `Content-Length: 4` RED 到达 gateway 并返回 201。现在 `Content-Length` 同样从 ASGI raw header 列表读取，超过一个值即在读取正文和调用 gateway 前返回安全 422。

最终证据：focused `131 passed in 2.43s`；全套 `727 passed, 13 skipped in 12.18s`；`pip check`、`compileall -q src tests`、`git diff --check f9ab6f2..HEAD` 均通过。
