# `using-ocr-mcpserver` 全局 Skill 设计

## 目标

创建个人全局 Codex Skill `using-ocr-mcpserver`，把本地文件到 OCR 结果的完整流程封装成适合较弱模型执行的固定工作流：

1. 校验本地 PDF、PNG、JPG 或 JPEG 文件。
2. 通过 OCR Gateway REST 接口上传文件并取得 `file_id`。
3. 调用 `ocr-mcpserver.parse_documents` 提交任务。
4. 每 15 秒调用 `get_task_status`，仅在进度或阶段发生变化时向用户汇报。
5. 任务终止后下载所有可用产物 ZIP。
6. 校验 ZIP 并安全解压到确定的本地目录。
7. 向用户返回任务状态、失败摘要和本地产物路径。

Skill 安装到：

`C:\Users\jiang_ren_1291992796\.codex\skills\using-ocr-mcpserver`

安装后可在任意 Codex 项目和新任务中使用。

## 非目标

- 不增加远程 MCP 工具数量。
- 不让远程服务读取 Windows 本地路径。
- 不通过 MCP 参数传输 Base64 文件正文。
- 不改变 OCR Gateway、MinerU、PaddleOCR 或任务状态机。
- 不在 Skill 中保存 API Key。
- 不自动打开、摘要或输出 OCR 业务正文。
- 不取代服务器现有的 24 小时输入和结果保留策略。

## 方案选择

采用“Skill + 确定性 PowerShell 辅助脚本”。

纯文字 Skill 容易让模型重复拼装上传、认证和安全解压命令；本地 MCP 代理会增加常驻组件和运维负担。PowerShell 辅助脚本适合当前 Windows 客户端，能使用系统自带能力完成文件上传、下载和 ZIP 安全校验，不引入额外运行时依赖。

解析、查询状态和方向恢复仍由已经注册的 `ocr-mcpserver` MCP 工具执行。REST 只承担 MCP 无法表达的本地字节上传和产物下载。

## Skill 结构

```text
using-ocr-mcpserver/
├── SKILL.md
├── agents/
│   └── openai.yaml
└── scripts/
    └── ocr-transfer.ps1
```

`SKILL.md` 只保留触发条件、固定编排顺序、安全要求和错误处理规则。`ocr-transfer.ps1` 提供低自由度、可重复测试的本地 I/O 操作。

脚本提供两个动作：

- `upload`：校验并上传一个本地文件，向标准输出返回不含密钥和正文的 JSON 回执。
- `download`：按 `artifact_id` 下载 ZIP、验证并安全解压，向标准输出返回本地路径和结构计数。

脚本默认 REST 基地址为 `http://127.0.0.1:18011`，允许通过显式参数覆盖，以便未来使用不同的 SSH 隧道或 HTTPS 网关。

## 输入契约

Skill 接受：

- 1 至 20 个本地文件路径；
- 1 至 20 个已批准 HTTPS URL；
- 本地路径和 HTTPS URL 的混合批次；
- 已存在的 `file_id`，用于跳过重复上传；
- 已存在的 `batch_id`，用于恢复状态查询和产物下载。

本地文件预检规则：

- 扩展名必须为 `.pdf`、`.png`、`.jpg` 或 `.jpeg`；
- 单文件大小不得超过 60 MiB；
- 文件必须存在且为普通文件；
- 单批次本地文件和 URL 合计不得超过 20 个；
- 可预先计算的本地文件总量不得超过 1 GiB；
- 不读取或记录文件正文。

PDF 页数、加密状态和远程 URL 安全策略继续由服务器权威校验。

## 认证

REST 上传和下载只从环境变量 `OCR_MCP_API_KEY` 读取凭据。

脚本必须满足：

- 不接受命令行明文 API Key 参数；
- 不输出、记录或回显 API Key；
- 环境变量缺失时立即失败，并返回稳定的非零退出码和安全错误信息；
- MCP 认证继续由 Codex 的 `bearer_token_env_var` 配置完成。

## 主流程

### 1. 准备来源

对于本地文件，依次调用 `ocr-transfer.ps1 upload`，收集 `file_id`。上传失败时停止提交新的 OCR 批次，保留已经取得的回执并向用户报告安全错误。

对于 HTTPS URL，直接构造 MCP `sources`。对于用户提供的 `file_id`，直接复用。

### 2. 提交 OCR

调用 `ocr-mcpserver.parse_documents` 一次性提交整个批次。Skill 生成稳定且不含文件名正文的幂等键；同一次逻辑重试必须复用该幂等键。

记录返回的 `batch_id`，并立即向用户报告任务已提交。任何后续中断都必须保留并返回该 `batch_id`，使新任务可以继续查询。

### 3. 反馈进度

每 15 秒调用一次 `ocr-mcpserver.get_task_status`。

只有以下任一值变化时才发送进度更新：

- 批次状态；
- 批次进度；
- 任一文件处理阶段；
- 已完成或失败文件数量。

进度信息只包含数字、稳定阶段名、文件序号和安全错误码，不包含识别文本或业务正文。轮询持续到 `completed`、`completed_with_errors`、`failed` 或 `cancelled`。

### 4. 下载产物

终止状态中存在产物时，对每个 `artifact_id` 调用 `ocr-transfer.ps1 download`。

默认输出根目录：

- 存在本地来源时：第一个本地源文件所在目录下的 `ocr-results/<batch_id>/`；
- 只有 URL 或 `file_id` 时：当前工作目录下的 `ocr-results/<batch_id>/`。

每个产物保存为 `<artifact_id>.zip`，并解压到 `<artifact_id>/` 子目录，避免批量产物重名。

返回的 `download_url` 只作为结果元数据。实际下载使用配置的 REST 基地址和 `artifact_id`，避免本地 SSH 隧道与服务器公开 HTTPS 地址不一致。

## ZIP 安全与完整性

下载动作必须先写入同目录临时文件，校验成功后再原子重命名为最终 ZIP。

解压前必须：

- 拒绝绝对路径；
- 拒绝包含 `..` 的路径；
- 拒绝反斜杠伪装的路径；
- 确认每个解压目标仍位于指定产物目录内；
- 遍历读取所有文件条目，发现损坏即失败；
- 至少确认 ZIP 非空且包含 `final.md`；
- 不在校验日志中输出 Markdown 或 JSON 正文。

校验失败时保留失败 ZIP 的临时文件名供诊断，但不解压，不覆盖已有成功产物。

## 部分失败

`completed_with_errors` 不等于整批失败。

Skill 必须：

- 下载所有已经发布的成功产物；
- 保留已成功解压的结果；
- 按文件序号报告失败数量、稳定错误码和安全错误消息；
- 不因一个文件失败而删除其他文件的产物。

只有没有任何可用产物时，最终结果才标记为“无可下载产物”。

## 恢复与重复执行

- 已有 `batch_id`：跳过上传和提交，从状态查询继续。
- 已有终止状态：直接进入下载阶段。
- 目标 ZIP 已存在且通过校验：复用，不重复下载。
- 目标 ZIP 存在但校验失败：下载到新的临时文件，成功后替换失败文件。
- 同一逻辑提交重试：复用原幂等键。
- 用户明确要求重新解析：生成新的幂等键和新任务。

## 错误处理

辅助脚本只输出稳定、无内容的错误类别：

- `authentication_unavailable`
- `file_not_found`
- `unsupported_media_type`
- `file_too_large`
- `batch_limit_exceeded`
- `upload_failed`
- `download_failed`
- `unsafe_archive`
- `invalid_artifact`

HTTP 或 MCP 返回的业务安全错误可原样报告其 `code` 和 `message`，不得输出响应中的未知扩展字段。

## Skill 触发语义

Skill 应在用户提出以下意图时自动触发：

- 用 OCR MCP Server 解析本地 PDF 或图片；
- 本地路径不能直接传给 MCP；
- 需要上传后解析；
- 需要持续查看 OCR 进度；
- 需要下载或解压 OCR 结果；
- 需要用 `batch_id` 继续之前的 OCR 任务。

Skill 名称使用 `using-ocr-mcpserver`，避免与服务器实现 Skill 或通用 PDF Skill 混淆。

## 测试策略

### Skill 基线测试

在没有该 Skill 的全新 Codex 进程中运行至少以下场景，记录基线失败：

1. 用户只给本地 PDF 路径并要求用 MCP 解析；
2. 两个本地文件，其中一个超过限制或不存在；
3. 已有 `batch_id`，要求继续并下载结果。

基线预期暴露的问题包括：只把本地路径交给 MCP、要求用户手动上传、遗漏轮询、输出密钥、未安全解压或未下载部分成功产物。

### 脚本自动化测试

使用本地伪 HTTP 服务覆盖：

- 正常上传和回执解析；
- 缺少认证变量；
- 不支持扩展名；
- 60 MiB 边界；
- HTTP 非成功响应；
- 正常下载；
- ZIP 路径穿越；
- 损坏 ZIP；
- 缺少 `final.md`；
- 已有合法 ZIP 的幂等复用。

测试必须先失败，再实现最小脚本使其通过。

### Skill 前向测试

安装前后分别用全新 Codex 进程执行相同场景，确认安装后能够：

- 自动发现 Skill；
- 调用上传脚本取得 `file_id`；
- 使用 `parse_documents` 和 `get_task_status`；
- 增量汇报进度；
- 下载并安全解压结果；
- 不输出凭据或识别正文。

真实前向测试使用用户已批准的测试 PDF，只记录文件大小、哈希、页数、任务状态、进度、产物结构和耗时。

## 验收标准

- 用户只提供 Windows 本地路径即可发起完整 OCR 流程。
- 用户不需要手动执行上传命令或复制 `file_id`。
- MCP 对外仍只有原来的 3 个工具。
- 进度至少每 15 秒检查一次，且只在变化时汇报。
- 部分失败不会丢弃成功产物。
- ZIP 保留并安全解压到确定目录。
- 新任务可用 `batch_id` 恢复。
- 任何输出和日志都不包含 API Key 或识别正文。
- Skill 结构验证、脚本测试和真实端到端前向测试全部通过。
