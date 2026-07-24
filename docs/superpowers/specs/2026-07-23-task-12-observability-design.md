# Task 12 可观测性、健康检查与故障注入设计

## 目标

为 `ocr-gateway` 增加适合单机 MVP 的 Prometheus 指标、结构化脱敏日志、存活/就绪检查和仅测试可用的故障注入能力。观测能力不得改变 OCR 业务结果、公开 MCP 工具数量或引擎选择策略，也不得引入 Redis、PostgreSQL、外部消息队列、OpenTelemetry 后端或日志平台。

## 总体方案

采用轻量、可注入的观测层：HTTP 边界通过 FastAPI 中间件记录请求指标，编排器、Paddle worker 和方向恢复协调器通过窄接口报告业务指标。默认实现使用独立的 Prometheus registry，测试可以注入隔离 registry、时钟、依赖探针和观测 sink，避免全局状态污染。

健康检查分为两个端点：

- `GET /health/live` 只表示 Python 进程能够响应，不访问数据库或 OCR 服务。
- `GET /health/ready` 并行检查 SQLite、MinerU 和部署选定的 Paddle worker。任一关键依赖不可用、超时或返回非法状态时，整体返回 `503`；全部可用时返回 `200`。

`GET /metrics` 免鉴权，便于 Prometheus 抓取。它只暴露低基数聚合指标，不包含业务内容或资源标识。

故障注入仅通过测试构造时注入的 fake probe、fake clock、failing sink 和现有依赖端口实现。生产 REST、MCP、配置文件和环境变量均不提供故障注入开关。

## 组件边界

### 观测接口

新增传输无关的观测接口与默认空实现/Prometheus 实现。接口只接受预定义枚举、有限标签、计数和持续时间，不接受任意字典、异常对象或自由文本。

应用工厂接收可选的观测组件；默认构建 Prometheus 实现。编排器和 worker 仍负责业务状态，观测失败必须被吞掉并转换为内部稳定事件，不能导致任务失败或改变幂等行为。

### HTTP 指标中间件

中间件使用路由模板而非原始 URL 作为 `route` 标签，未知路径统一标记为 `unmatched`。它记录完成的请求计数和耗时；客户端取消或未处理异常统一映射到有限的 `status_class`/`outcome` 值。`/metrics` 本身不计入 HTTP 指标，避免抓取行为反馈性地改变被抓取数据。请求体、查询字符串、请求头和异常正文不会进入指标或日志。

### 依赖探针

定义异步 `DependencyProbe` 协议，返回依赖名和 `ready`/`unavailable` 状态。默认组合包含：

- SQLite：执行有界时限的只读 `SELECT 1`。
- MinerU：调用配置的健康端点或适配器提供的轻量探针，不提交解析任务。
- Paddle：检查当前部署选定 worker 的启动状态、关闭状态和可接受任务能力，不加载另一种引擎。

单个探针有独立超时；聚合器并行运行所有探针。响应固定排序，内容只允许 `dependency`、`status` 和稳定 `code`。不返回主机、端口、URL、异常类型或异常消息。

## 指标契约

指标名称和标签固定如下：

- `ocr_http_requests_total{method,route,status_class}`
- `ocr_http_request_duration_seconds{method,route}`
- `ocr_tasks_total{outcome}`
- `ocr_task_duration_seconds{outcome}`
- `ocr_pipeline_stage_duration_seconds{stage,outcome}`
- `ocr_orchestration_queue_depth`
- `ocr_secondary_ocr_queue_depth`
- `ocr_dependency_ready{dependency}`
- `ocr_recovery_total{outcome}`

`method` 只允许已知 HTTP 方法或 `OTHER`；`route` 只允许已注册路由模板或 `unmatched`；`status_class` 只允许 `2xx`、`3xx`、`4xx`、`5xx`；连接取消或无法形成 HTTP 响应时使用独立的有限 `outcome` 内部事件，不创建含自由文本的标签。`outcome` 和 `stage` 使用已有领域枚举的有限子集。禁止使用 batch ID、file ID、artifact ID、文件名、URL、token、路径、错误消息或 OCR 文本作为标签。

Gauge 必须反映当前值而非累计值。队列深度从编排 wake queue 和单所有者 Paddle worker 的现有只读状态获取，不为采集指标而消费队列。Histogram 使用固定 buckets，避免运行时动态配置。

## 结构化日志

默认输出单行 JSON。允许字段为：

- `timestamp`
- `level`
- `event`
- `batch_id`、`file_id` 或 `recovery_id` 等服务生成的 opaque ID
- `stage`
- `error_code`
- `duration_ms`
- 有界整数计数

日志 API 不接受任意 `extra` 字典。异常只转换为稳定错误码；不得序列化异常对象、堆栈中的业务值或 `str(exc)`。明确禁止记录请求体、响应业务正文、OCR 文本、文件名、URL、原始 recovery token、API key、Authorization header、文件路径和文档字节。

日志 sink 失败不能中断业务流程。启动期配置错误仍按现有 fail-closed 规则终止进程，但错误输出不包含敏感配置值。

## HTTP 与鉴权

- `/health/live`、`/health/ready` 和 `/metrics` 免 API Key 鉴权。
- 其他 REST 与 `/mcp` 保持现有鉴权规则。
- 三个免鉴权端点均不得回显输入数据或内部连接信息。
- MCP 仍只暴露 `parse_documents`、`get_task_status` 和 `reparse_with_page_orientation` 三个工具。

## 故障处理

- 指标记录失败：业务继续，最多产生一个稳定的内部日志事件。
- 单个就绪探针失败或超时：该依赖标记 `unavailable`，整体返回 `503`。
- readiness 响应不得因一个探针抛异常而返回通用 `500`。
- Prometheus exposition 生成失败：返回脱敏 `503`，不暴露异常正文。
- 日志 sink 失败：业务继续且不递归记录日志失败。

## 测试策略

严格按 TDD 实现：

1. 指标单元测试验证名称、标签白名单、低基数路由、计数、Histogram 和 Gauge 当前值。
2. 日志测试向请求、异常和依赖中注入敏感文件名、URL、OCR 文本、token、API key 与路径，断言所有输出均不包含这些值。
3. 健康检查测试覆盖全健康、单依赖失败、超时、异常、确定性排序和并行执行。
4. HTTP 测试验证三个观测端点免鉴权，其他端点仍 fail closed，MCP 工具仍恰好三个。
5. 故障注入测试验证 metrics/log sink 故障不影响任务，probe 故障只影响 readiness。
6. 并发测试验证 registry 隔离、计数准确和队列 Gauge 不消费工作。
7. 完整 Windows 与 Ubuntu 测试、`pip check`、`compileall`、`git diff --check`、Docker 启动和 Prometheus 抓取 smoke 作为交付门禁。

## 非目标

- 不实现分布式 tracing、OpenTelemetry collector、Grafana dashboard 或告警规则。
- 不增加生产故障注入端点、配置开关或 MCP 工具。
- 不把 OCR 正文、文件元数据或用户输入写入日志/指标。
- 不在 readiness 中执行真实 OCR、下载模型或触发任务。
- 不改变 MinerU、Paddle 或方向恢复的业务编排策略。

## 验收标准

- 可通过 `/metrics` 看到成功率、耗时、流水线阶段和两个队列深度。
- `/health/live` 不依赖外部组件；`/health/ready` 在任一关键依赖不可用时稳定返回 `503`。
- 所有观测输出不包含业务正文、文件名、URL、路径、密钥或原始 token。
- 故障注入仅存在于测试依赖中，生产 API schema 不增加相关字段。
- 观测组件故障不会改变任务状态、幂等性或结果。
- REST 鉴权边界保持不变，MCP 工具数量保持为三个。
