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
