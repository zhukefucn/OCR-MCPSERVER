---
name: using-ocr-mcpserver
description: Use when local PDF or image paths must be processed through OCR MCP Server, especially when the remote MCP rejects local paths, files need REST upload first, OCR task progress must be monitored, artifacts must be downloaded, or an existing batch_id must be resumed.
---

# 使用 OCR MCP Server

严格按以下顺序执行。从会话技能清单取得本技能 `SKILL.md` 的绝对路径，将
`SKILL.md 所在目录`记为 `SKILL_ROOT`。不要假设当前工作目录包含 `scripts/`。
始终使用本技能自带的 `ocr-transfer.ps1` 完成本地字节上传和产物下载。

## 固定工作流

1. 执行输入分类和容量预检。
   - 确认本地路径、已批准的 HTTPS URL 和已有 `file_id` 三类 `sources` 合计 1 至 20 个，
     允许混合；把已有 `batch_id` 作为恢复输入。
   - 确认本地文件存在且为普通文件，扩展名为 `.pdf`、`.png`、`.jpg` 或 `.jpeg`，
     单文件不超过 60 MiB；确认可预先计算的本地文件总量不超过 1 GiB。
   - 不自行读取 PDF 页数、加密状态或 URL 内容；把这些检查交给服务器。
2. 为每个本地文件运行上传动作并收集返回的 `file_id`：

   ```powershell
   & "$SKILL_ROOT/scripts/ocr-transfer.ps1" -Action upload -Path '<本地路径>'
   ```

   只让脚本从 `OCR_MCP_API_KEY` 环境变量读取凭据。任一上传失败时停止创建新批次，
   保留已经取得的安全回执并报告稳定错误类别。
3. 将上传得到的 `file_id`、输入的 `file_id` 和已批准的 HTTPS URL 构造成一个
   `sources`。使用无内容随机标识作为幂等键；幂等键不得包含文件名，不得包含路径，
   不得包含业务正文，不得包含 OCR 正文。同一逻辑提交重试必须复用原幂等键。使用
   这个幂等键调用一次 `parse_documents`。
4. 保存 `batch_id`，立即按“已提交”契约汇报；后续任何中断都返回这个值。
5. 每 15 秒调用一次 `get_task_status`，直到状态成为 `completed`、
   `completed_with_errors`、`failed` 或 `cancelled`。只汇报变化：批次状态、批次
   百分比、任一文件的稳定阶段、已完成数或失败数没有变化时不发送更新。
6. 进入终态后，收集响应中所有 `artifact_id`，逐个运行下载动作：

   ```powershell
   & "$SKILL_ROOT/scripts/ocr-transfer.ps1" -Action download `
     -ArtifactId '<artifact_id>' `
     -OutputRoot '<输出根目录>/ocr-results/<batch_id>/'
   ```

   有本地来源时，把第一个本地源所在目录作为 `<输出根目录>`；只有 HTTPS URL 或
   `file_id` 时使用当前工作目录。让脚本校验 ZIP 非空且包含 `final.md`，并把每个
   产物保存为 `<artifact_id>.zip`、解压到 `<artifact_id>/`。
7. 汇报成功数、失败数、每个 ZIP 路径和解压目录。将服务器的稳定错误码和安全消息
   与文件序号关联，不输出未知扩展字段。

## 部分失败

把 `completed_with_errors` 视为部分成功。下载所有 `artifact_id`，保留已成功下载和
解压的产物；一个文件失败时不要删除其他成功产物。仅在没有任何可用产物时汇报
“无可下载产物”。

## 恢复

- 已有 `batch_id` 时，跳过上传和提交，从保存 `batch_id` 后的状态查询继续。
- 已有终止状态时，直接进入下载阶段。
- 目标 ZIP 已存在且验证成功时复用；验证失败时让下载动作以临时文件重新下载并在
  验证成功后替换。
- 中断或失败时保留 `batch_id`、最后状态和安全的下一步。
- 只有用户明确指定方向恢复时才调用 `reparse_with_page_orientation`。普通失败、
  重试、恢复或状态查询都不得调用它；用户明确要求重新解析时创建新的幂等键和任务。

## 输出契约

只使用以下形状汇报：

```text
已提交：batch_id、文件数
处理中：批次百分比、文件序号、稳定阶段
已完成：成功数、失败数、ZIP 路径、解压目录
可恢复：batch_id、最后状态、下一步
```

不要输出 API Key。不要读取或输出 OCR 正文。不要在命令、日志、进度或最终回复中
回显凭据、Markdown 正文、JSON 正文或业务文本。

MCP 工具边界仅包括 `parse_documents`、`get_task_status` 和
`reparse_with_page_orientation`。
