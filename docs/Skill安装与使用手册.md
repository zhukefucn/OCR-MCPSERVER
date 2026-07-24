# using-ocr-mcpserver Skill 安装与使用手册

## 1. Skill 用途

`using-ocr-mcpserver` 让智能体处理本地 PDF 或图片时自动执行：

```text
容量预检 → REST 上传 → MCP 创建任务 → 进度轮询 → REST 下载 → ZIP 校验与解压
```

Skill 不增加 MCP 底层工具，也不允许智能体选择 OCR 引擎。

## 2. 仓库位置

```text
skills/using-ocr-mcpserver/
├── SKILL.md
├── agents/openai.yaml
└── scripts/ocr-transfer.ps1
```

这是完整可分发目录。安装时复制整个目录，不要只复制 `SKILL.md`。

## 3. Codex 自动安装

在另一个 Codex 会话中提出：

```text
请从 https://github.com/zhukefucn/OCR-MCPSERVER
安装 skills/using-ocr-mcpserver Skill。
```

安装器应把目录放到：

```text
<CODEX_HOME>/skills/using-ocr-mcpserver/
```

如果没有设置 `CODEX_HOME`，Windows 默认位于：

```text
%USERPROFILE%\.codex\skills\using-ocr-mcpserver\
```

安装或更新后新开一个 Codex 任务；旧任务的技能清单不会自动刷新。

## 4. 手工安装

Windows PowerShell：

```powershell
$source = '<仓库>\skills\using-ocr-mcpserver'
$target = Join-Path $env:USERPROFILE '.codex\skills\using-ocr-mcpserver'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'SKILL.md') -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $source 'agents') -Destination $target -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source 'scripts') -Destination $target -Recurse -Force
```

Linux：

```bash
target="${CODEX_HOME:-$HOME/.codex}/skills/using-ocr-mcpserver"
mkdir -p "$target"
cp -R skills/using-ocr-mcpserver/. "$target/"
```

## 5. 配置

在启动 Codex 的环境中设置：

```powershell
$env:OCR_MCP_API_KEY = '<密钥>'
```

传输脚本默认连接：

```text
http://127.0.0.1:18011
```

如果服务地址不同，调用脚本时传入 `-BaseUrl`。密钥只允许从
`OCR_MCP_API_KEY` 环境变量读取。

同时为 Codex 配置 OCR MCP 地址：

```text
http://127.0.0.1:18011/mcp
```

MCP 和 REST 必须指向同一套服务和数据目录。

## 6. 验证安装

新开 Codex 任务后检查技能清单，应出现：

```text
using-ocr-mcpserver
```

然后提出：

```text
使用 using-ocr-mcpserver 解析这个本地 PDF，并持续汇报进度、下载结果。
```

预期行为：

1. 智能体定位 Skill 自带的 `scripts/ocr-transfer.ps1`。
2. 不把本地路径直接传给 MCP。
3. 上传后取得 `file_id`。
4. 调用 `parse_documents` 并保存 `batch_id`。
5. 每 15 秒调用 `get_task_status`。
6. 终态后下载所有 `artifact_id`，验证并解压 ZIP。

## 7. 更新

从仓库重新复制同名目录即可更新。更新后重新打开 Codex 任务。

更新时不要修改：

- Skill 名称 `using-ocr-mcpserver`
- `agents/openai.yaml` 中的 `$using-ocr-mcpserver`
- `scripts/ocr-transfer.ps1` 的相对目录结构

## 8. 给其他 agent 使用

向其他 agent 提供以下三项：

1. 仓库 URL 和 Skill 子目录。
2. OCR MCP 地址。
3. 由环境变量安全注入的 API Key。

不要把 API Key 打包进 Skill、Git 仓库、安装说明或聊天消息。

其他 agent 即使不在 OCR 项目工作目录，也必须根据技能清单中的 `SKILL.md` 绝对路径
找到 `scripts/ocr-transfer.ps1`；不得假设当前目录存在 `scripts/`。

## 9. 验证命令

在仓库根目录执行：

```powershell
python -m pytest tests/skill/test_using_ocr_mcpserver_contract.py -q
python -m pytest tests/skill/test_ocr_transfer.py -q
```

验证内容包括元数据、三工具边界、上传安全、下载上限、ZIP 路径安全、缓存复用和
凭据不回显。
