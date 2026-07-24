# `using-ocr-mcpserver` 全局 Skill 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建并安装一个个人全局 Codex Skill，使智能体只接收本地文件路径也能自动完成上传、MCP 解析、进度轮询、产物下载和安全解压。

**Architecture:** 仓库中的 `skills/using-ocr-mcpserver/` 是唯一源码，安装时复制到 `%USERPROFILE%\.codex\skills\using-ocr-mcpserver`。Skill 用固定工作流编排现有 `ocr-mcpserver` 三个 MCP 工具；PowerShell 辅助脚本只负责无法由远程 MCP 完成的本地字节上传、ZIP 下载和安全解压。

**Tech Stack:** Codex Skill、PowerShell 5.1+、.NET `HttpClient`、`System.IO.Compression.ZipArchive`、Python 3.11/pytest 测试驱动、FastAPI REST、Streamable HTTP MCP。

## Global Constraints

- Skill 名称固定为 `using-ocr-mcpserver`，使用小写字母和连字符。
- 个人安装目录固定为 `C:\Users\jiang_ren_1291992796\.codex\skills\using-ocr-mcpserver`。
- REST 默认基地址固定为 `http://127.0.0.1:18011`。
- 凭据只从 `OCR_MCP_API_KEY` 环境变量读取；命令行、配置、标准输出和日志不得包含凭据。
- 支持 PDF、PNG、JPG、JPEG；单文件最大 60 MiB，即 62,914,560 字节。
- 单批次最多 20 个来源；可预检的本地文件总量最大 1 GiB。
- 解析和状态查询必须使用 `parse_documents` 与 `get_task_status`，不得绕过 MCP 改用 REST 任务接口。
- 每 15 秒轮询一次，仅在状态、进度、阶段或完成/失败计数变化时汇报。
- 部分失败时下载所有成功产物，不删除已经成功的文件。
- ZIP 必须保留并安全解压；拒绝绝对路径、`..`、反斜杠伪装和目标目录逃逸。
- 不读取、打印或摘要 OCR 业务正文；测试只记录文件大小、哈希、页数、状态、阶段、结构计数和耗时。
- Skill 只改变 Windows 客户端，不同步到 Ubuntu，不重建 5090 镜像。

---

### Task 1: 建立 Skill 行为基线与失败测试

**Files:**
- Create: `tests/skill/test_using_ocr_mcpserver_contract.py`
- Create: `tests/skill/test_ocr_transfer.py`
- Create: `tests/skill/scenarios/local-files-request.txt`
- Create: `tests/skill/scenarios/resume-batch-request.txt`

**Interfaces:**
- Consumes: 已注册的 MCP 名称 `ocr-mcpserver`，用户环境变量 `OCR_MCP_API_KEY`。
- Produces: 对 Skill 文件结构、触发语义和 `ocr-transfer.ps1` 命令契约的失败测试。

- [ ] **Step 1: 记录无 Skill 时的真实基线**

确认全局目录尚不存在：

```powershell
Test-Path "$env:USERPROFILE\.codex\skills\using-ocr-mcpserver"
```

Expected: `False`

创建不含业务正文的场景提示：

```text
使用 ocr-mcpserver 解析 C:\test-data\document.pdf。不要让我手工上传；持续汇报进度，完成后下载并解压结果。
```

用全新临时 Codex 进程执行，但不提供该 Skill：

```powershell
$env:OCR_MCP_API_KEY = [Environment]::GetEnvironmentVariable('OCR_MCP_API_KEY', 'User')
codex exec --ephemeral --skip-git-repo-check --color never `
  "当前没有 using-ocr-mcpserver Skill。请处理 tests/skill/scenarios/local-files-request.txt 中的用户请求；禁止实际提交 OCR 任务。"
```

Expected: 输出至少暴露一项基线缺陷，例如直接把本地路径交给 MCP、要求用户手工上传、没有下载步骤或没有轮询策略。把不含密钥、正文和真实文件名的缺陷摘要记录到测试运行日志，不把完整 Codex 日志提交到 Git。

- [ ] **Step 2: 写 Skill 结构失败测试**

在 `tests/skill/test_using_ocr_mcpserver_contract.py` 写入：

```python
from pathlib import Path
import re
import yaml


ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "using-ocr-mcpserver"


def test_skill_has_required_structure() -> None:
    assert (SKILL / "SKILL.md").is_file()
    assert (SKILL / "agents" / "openai.yaml").is_file()
    assert (SKILL / "scripts" / "ocr-transfer.ps1").is_file()


def test_skill_metadata_triggers_local_ocr_workflow() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    metadata = yaml.safe_load(match.group(1))
    assert metadata["name"] == "using-ocr-mcpserver"
    description = metadata["description"]
    assert description.startswith("Use when")
    for keyword in ("local", "OCR", "upload", "progress", "download"):
        assert keyword.lower() in description.lower()


def test_skill_preserves_three_tool_boundary() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "parse_documents" in text
    assert "get_task_status" in text
    assert "reparse_with_page_orientation" in text
    assert "upload_document" not in text
```

- [ ] **Step 3: 写 PowerShell 命令契约失败测试**

在 `tests/skill/test_ocr_transfer.py` 建立通用执行器：

```python
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "skills" / "using-ocr-mcpserver" / "scripts" / "ocr-transfer.ps1"


def run_transfer(*args: str, api_key: str | None = "test-key"):
    env = os.environ.copy()
    if api_key is None:
        env.pop("OCR_MCP_API_KEY", None)
    else:
        env["OCR_MCP_API_KEY"] = api_key
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result, payload


def test_missing_api_key_fails_without_echoing_secrets(tmp_path: Path) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-test")
    result, payload = run_transfer(
        "-Action", "upload", "-Path", str(source), api_key=None
    )
    assert result.returncode != 0
    assert payload == {
        "ok": False,
        "error": {
            "code": "authentication_unavailable",
            "message": "OCR authentication is not configured.",
        },
    }
```

- [ ] **Step 4: 运行测试并确认因 Skill 尚不存在而失败**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_using_ocr_mcpserver_contract.py `
  tests/skill/test_ocr_transfer.py -q
```

Expected: FAIL，失败原因是 `skills/using-ocr-mcpserver`、`SKILL.md` 和 `ocr-transfer.ps1` 尚不存在，而不是测试语法或依赖错误。

- [ ] **Step 5: 提交 RED 测试**

```powershell
git add tests/skill
git commit -m "test: 定义 OCR MCP Skill 工作流契约"
```

---

### Task 2: 初始化 Skill 并实现安全上传

**Files:**
- Create: `skills/using-ocr-mcpserver/SKILL.md`
- Create: `skills/using-ocr-mcpserver/agents/openai.yaml`
- Create: `skills/using-ocr-mcpserver/scripts/ocr-transfer.ps1`
- Modify: `tests/skill/test_ocr_transfer.py`

**Interfaces:**
- Consumes: `OCR_MCP_API_KEY`、本地文件路径、REST 基地址。
- Produces: `upload` 动作；成功时输出 `{"ok":true,"file_id":...,"size_bytes":...,"media_type":...}`，失败时输出稳定安全错误并返回非零退出码。

- [ ] **Step 1: 读取 Skill 元数据规范并初始化目录**

完整读取：

```powershell
Get-Content -Raw `
  "$env:USERPROFILE\.codex\skills\.system\skill-creator\references\openai_yaml.md"
```

运行官方初始化脚本：

```powershell
$creator = "$env:USERPROFILE\.codex\skills\.system\skill-creator"
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  "$creator\scripts\init_skill.py" using-ocr-mcpserver `
  --path skills `
  --resources scripts `
  --interface 'display_name=OCR MCP 文档处理' `
  --interface 'short_description=上传本地文档并通过 OCR MCP 解析、跟踪和下载结果' `
  --interface 'default_prompt=使用 OCR MCP 处理这些本地文件，持续汇报进度并下载解压结果。'
```

Expected: 创建 `SKILL.md`、`agents/openai.yaml` 和 `scripts/`，目录中没有示例占位文件。
立即把 `SKILL.md` 收敛为满足 Task 1 初始契约测试的最小版本：保留正确
frontmatter，并明确列出 `parse_documents`、`get_task_status` 和
`reparse_with_page_orientation` 三个既有 MCP 工具。完整编排、安全规则和恢复说明
仍在 Task 4 通过新增 RED 测试后补齐。

- [ ] **Step 2: 扩展上传失败测试**

在 `tests/skill/test_ocr_transfer.py` 加入：

```python
def test_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("not OCR input", encoding="utf-8")
    result, payload = run_transfer("-Action", "upload", "-Path", str(source))
    assert result.returncode != 0
    assert payload["error"]["code"] == "unsupported_media_type"


def test_upload_rejects_file_over_60_mib(tmp_path: Path) -> None:
    source = tmp_path / "large.pdf"
    with source.open("wb") as stream:
        stream.truncate(62_914_561)
    result, payload = run_transfer("-Action", "upload", "-Path", str(source))
    assert result.returncode != 0
    assert payload["error"]["code"] == "file_too_large"
```

增加一个绑定 `127.0.0.1` 随机端口的 Python `ThreadingHTTPServer` fixture。处理器只记录请求头、请求长度和正文 SHA-256，返回：

```json
{
  "file_id": "11111111-1111-4111-8111-111111111111",
  "size_bytes": 9,
  "media_type": "application/pdf"
}
```

成功测试必须断言：

```python
def test_upload_streams_file_and_returns_safe_receipt(
    tmp_path: Path, upload_server
) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-test")
    result, payload = run_transfer(
        "-Action", "upload",
        "-Path", str(source),
        "-BaseUrl", upload_server.base_url,
    )
    assert result.returncode == 0
    assert payload == {
        "ok": True,
        "action": "upload",
        "file_id": "11111111-1111-4111-8111-111111111111",
        "size_bytes": 9,
        "media_type": "application/pdf",
    }
    assert upload_server.authorization == "test-key"
    assert "test-key" not in result.stdout + result.stderr
```

- [ ] **Step 3: 运行新增测试并确认按预期失败**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_ocr_transfer.py `
  -k "missing_api_key or unsupported_extension or over_60_mib or streams_file" -q
```

Expected: FAIL，因为脚本动作尚未实现。

- [ ] **Step 4: 实现最小上传脚本**

`ocr-transfer.ps1` 使用以下公开参数：

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('upload', 'download')]
    [string]$Action,
    [string]$Path,
    [string]$ArtifactId,
    [string]$OutputRoot,
    [string]$BaseUrl = 'http://127.0.0.1:18011'
)
```

实现以下内部函数，函数名和职责固定：

```powershell
function Write-SafeJson([hashtable]$Value, [int]$ExitCode)
function Get-ApiKey()
function Resolve-UploadFile([string]$InputPath)
function Get-MediaType([System.IO.FileInfo]$File)
function Get-SafeDisplayName([System.IO.FileInfo]$File)
function Invoke-Upload([System.IO.FileInfo]$File, [string]$ApiKey, [uri]$Endpoint)
```

`Resolve-UploadFile` 必须使用 `GetFullPath`、`Test-Path -PathType Leaf` 和精确字节上限。`Invoke-Upload` 必须用 `FileStream`、`StreamContent` 和 `HttpClient.SendAsync`，不得先把整个文件读入内存。

统一入口：

```powershell
try {
    $apiKey = Get-ApiKey
    if ($Action -eq 'upload') {
        $file = Resolve-UploadFile $Path
        $receipt = Invoke-Upload $file $apiKey ([uri]"$BaseUrl/v1/uploads")
        Write-SafeJson @{
            ok = $true
            action = 'upload'
            file_id = $receipt.file_id
            size_bytes = [long]$receipt.size_bytes
            media_type = [string]$receipt.media_type
        } 0
    }
} catch {
    $safe = Convert-ToSafeTransferError $_.Exception
    Write-SafeJson @{ ok = $false; error = $safe } 1
}
```

实现 `Convert-ToSafeTransferError`，只允许设计规格中的稳定错误码和固定英文消息；未知异常统一映射为 `upload_failed` 或 `download_failed`。

- [ ] **Step 5: 运行上传测试和结构测试**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_ocr_transfer.py `
  tests/skill/test_using_ocr_mcpserver_contract.py -q
```

Expected: 上传测试和当前 Skill 文字契约测试全部 PASS；Task 2 不带已知红灯提交。

- [ ] **Step 6: 提交上传实现**

```powershell
git add skills/using-ocr-mcpserver tests/skill/test_ocr_transfer.py
git commit -m "feat: 添加 OCR 文档安全上传助手"
```

---

### Task 3: 实现 ZIP 下载、校验和幂等解压

**Files:**
- Modify: `skills/using-ocr-mcpserver/scripts/ocr-transfer.ps1`
- Modify: `tests/skill/test_ocr_transfer.py`

**Interfaces:**
- Consumes: `artifact_id: UUID`、`OutputRoot: directory`、REST 基地址。
- Produces: `<OutputRoot>/<artifact_id>.zip` 和 `<OutputRoot>/<artifact_id>/`；成功 JSON 包含 `zip_path`、`extract_path`、`entry_count`、`reused`。

- [ ] **Step 1: 写下载失败测试**

扩展伪 HTTP 服务，让 `/v1/artifacts/<uuid>` 返回内存 ZIP。加入辅助函数：

```python
from io import BytesIO
import zipfile


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()
```

加入以下测试：

```python
ARTIFACT_ID = "22222222-2222-4222-8222-222222222222"


def test_download_keeps_zip_and_extracts_final_markdown(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip({
        "final.md": b"# result",
        "artifact_manifest.json": b"{}",
    })
    result, payload = run_transfer(
        "-Action", "download",
        "-ArtifactId", ARTIFACT_ID,
        "-OutputRoot", str(tmp_path),
        "-BaseUrl", artifact_server.base_url,
    )
    assert result.returncode == 0
    assert Path(payload["zip_path"]).is_file()
    assert (Path(payload["extract_path"]) / "final.md").is_file()
    assert payload["entry_count"] == 2
    assert payload["reused"] is False


def test_download_rejects_path_traversal(tmp_path: Path, artifact_server) -> None:
    artifact_server.payload = make_zip({
        "../escape.txt": b"unsafe",
        "final.md": b"# result",
    })
    result, payload = run_transfer(
        "-Action", "download",
        "-ArtifactId", ARTIFACT_ID,
        "-OutputRoot", str(tmp_path),
        "-BaseUrl", artifact_server.base_url,
    )
    assert result.returncode != 0
    assert payload["error"]["code"] == "unsafe_archive"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_download_rejects_archive_without_final_md(
    tmp_path: Path, artifact_server
) -> None:
    artifact_server.payload = make_zip({"manifest.json": b"{}"})
    result, payload = run_transfer(
        "-Action", "download",
        "-ArtifactId", ARTIFACT_ID,
        "-OutputRoot", str(tmp_path),
        "-BaseUrl", artifact_server.base_url,
    )
    assert result.returncode != 0
    assert payload["error"]["code"] == "invalid_artifact"
```

再加入损坏 ZIP、反斜杠路径、绝对路径和合法 ZIP 二次执行 `reused=True` 测试。

- [ ] **Step 2: 运行下载测试并确认按预期失败**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_ocr_transfer.py -k download -q
```

Expected: FAIL，因为 `download` 尚未实现或返回 `download_failed`。

- [ ] **Step 3: 实现下载和安全解压**

增加固定函数：

```powershell
function Assert-CanonicalArtifactId([string]$Value)
function Invoke-ArtifactDownload([string]$ArtifactId, [string]$ApiKey, [uri]$Endpoint, [string]$TemporaryPath)
function Test-SafeArchive([string]$ZipPath, [string]$ExtractionRoot)
function Expand-SafeArchive([string]$ZipPath, [string]$ExtractionRoot)
function Invoke-Download([string]$ArtifactId, [string]$OutputRoot, [string]$ApiKey, [uri]$Endpoint)
```

`Test-SafeArchive` 的每个条目都必须执行：

```powershell
$name = $entry.FullName
if ([System.IO.Path]::IsPathRooted($name) -or $name.Contains('\')) {
    throw [UnsafeArchiveException]::new()
}
$segments = $name.Split('/')
if ($segments -contains '..') {
    throw [UnsafeArchiveException]::new()
}
$target = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($ExtractionRoot, $name)
)
$rootWithSeparator = [System.IO.Path]::GetFullPath($ExtractionRoot).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith(
    $rootWithSeparator,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw [UnsafeArchiveException]::new()
}
```

验证阶段遍历读取每个文件条目的流到结尾，统计条目并确认精确存在 `final.md`。下载先写 `<artifact_id>.partial.zip`，验证通过后移动为 `<artifact_id>.zip`。解压先写 `<artifact_id>.extracting/`，成功后移动为 `<artifact_id>/`。

若最终 ZIP 和解压目录已经存在且重新校验通过，直接返回 `reused = $true`，不得重新发送 HTTP 请求。

- [ ] **Step 4: 运行全部脚本测试**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest tests/skill/test_ocr_transfer.py -q
```

Expected: PASS，且测试临时目录之外没有任何解压文件。

- [ ] **Step 5: 提交下载实现**

```powershell
git add skills/using-ocr-mcpserver/scripts/ocr-transfer.ps1 `
  tests/skill/test_ocr_transfer.py
git commit -m "feat: 添加 OCR 产物安全下载与解压"
```

---

### Task 4: 编写 Skill 固定编排与恢复规则

**Files:**
- Modify: `skills/using-ocr-mcpserver/SKILL.md`
- Modify: `skills/using-ocr-mcpserver/agents/openai.yaml`
- Modify: `tests/skill/test_using_ocr_mcpserver_contract.py`

**Interfaces:**
- Consumes: 本地路径、HTTPS URL、`file_id` 或 `batch_id`。
- Produces: 固定的“上传 → MCP 提交 → 15 秒轮询 → 下载 → 汇报”行为。

- [ ] **Step 1: 扩展 Skill 内容失败测试**

增加：

```python
def test_skill_encodes_complete_workflow_and_security_rules() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    required = [
        "ocr-transfer.ps1",
        "OCR_MCP_API_KEY",
        "parse_documents",
        "get_task_status",
        "15",
        "batch_id",
        "artifact_id",
        "completed_with_errors",
        "ocr-results",
        "final.md",
    ]
    for token in required:
        assert token in text
    assert "不要输出 API Key" in text
    assert "不要读取或输出 OCR 正文" in text


def test_openai_metadata_matches_skill() -> None:
    metadata = yaml.safe_load(
        (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    interface = metadata["interface"]
    assert interface["display_name"] == "OCR MCP 文档处理"
    assert "上传" in interface["short_description"]
    assert "进度" in interface["default_prompt"]
```

- [ ] **Step 2: 运行契约测试并确认按预期失败**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_using_ocr_mcpserver_contract.py -q
```

Expected: FAIL，指出初始化模板仍包含占位内容或缺少固定工作流规则。

- [ ] **Step 3: 编写最小 `SKILL.md`**

Frontmatter 固定为：

```yaml
---
name: using-ocr-mcpserver
description: Use when local PDF or image paths must be processed through OCR MCP Server, especially when the remote MCP rejects local paths, files need REST upload first, OCR task progress must be monitored, artifacts must be downloaded, or an existing batch_id must be resumed.
---
```

正文使用命令式中文，按以下顺序定义：

1. 输入分类和容量预检；
2. 用 `scripts/ocr-transfer.ps1 -Action upload` 上传所有本地文件；
3. 构造 `sources` 并调用 MCP `parse_documents`；
4. 保存 `batch_id`；
5. 每 15 秒调用 `get_task_status`，只汇报变化；
6. 终止后对所有 `artifact_id` 调用 `download`；
7. 返回 ZIP、解压目录、成功/失败计数；
8. 已有 `batch_id` 时从第 4 步恢复；
9. 用户明确指定方向恢复时才调用 `reparse_with_page_orientation`。

正文明确以下正向输出契约：

```text
已提交：batch_id、文件数
处理中：批次百分比、文件序号、稳定阶段
已完成：成功数、失败数、ZIP 路径、解压目录
可恢复：batch_id、最后状态、下一步
```

正文不得包含实际 API Key 示例、Base64 上传建议或第四个 MCP 工具。

- [ ] **Step 4: 重新生成 `agents/openai.yaml`**

Run:

```powershell
$creator = "$env:USERPROFILE\.codex\skills\.system\skill-creator"
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  "$creator\scripts\generate_openai_yaml.py" `
  skills/using-ocr-mcpserver `
  --interface 'display_name=OCR MCP 文档处理' `
  --interface 'short_description=上传本地文档并通过 OCR MCP 解析、跟踪和下载结果' `
  --interface 'default_prompt=使用 OCR MCP 处理这些本地文件，持续汇报进度并下载解压结果。'
```

Expected: `agents/openai.yaml` 只包含生成器支持的字段，路径不逃出 Skill 目录。

- [ ] **Step 5: 运行 Skill 契约和快速验证**

Run:

```powershell
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' -m pytest `
  tests/skill/test_using_ocr_mcpserver_contract.py -q

$creator = "$env:USERPROFILE\.codex\skills\.system\skill-creator"
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  "$creator\scripts\quick_validate.py" `
  skills/using-ocr-mcpserver
```

Expected: pytest PASS；`quick_validate.py` 返回成功。

- [ ] **Step 6: 提交 Skill 编排**

```powershell
git add skills/using-ocr-mcpserver tests/skill/test_using_ocr_mcpserver_contract.py
git commit -m "feat: 编排 OCR MCP 本地文件完整工作流"
```

---

### Task 5: 安装全局 Skill 并做前向行为测试

**Files:**
- Create at deployment: `C:\Users\jiang_ren_1291992796\.codex\skills\using-ocr-mcpserver\`
- Test only, do not commit: `D:\codex-workspace\ocrzhengli\.skill-e2e\`

**Interfaces:**
- Consumes: 仓库 Skill 源码、全局 Codex Skill 目录、已配置的 `ocr-mcpserver`。
- Produces: 可被全新 Codex 进程自动发现的个人全局 Skill。

- [ ] **Step 1: 运行仓库完整测试**

Run:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest -q
```

Expected: 100% 完成，退出码 0。

- [ ] **Step 2: 安装前检查目标**

Run:

```powershell
$source = (Resolve-Path 'skills\using-ocr-mcpserver').Path
$target = Join-Path $env:USERPROFILE '.codex\skills\using-ocr-mcpserver'
if (Test-Path -LiteralPath $target) {
    throw "Global skill target already exists: $target"
}
```

Expected: 目标不存在；若存在则停止，先比较来源和目标，不覆盖未知用户文件。

- [ ] **Step 3: 复制并验证安装内容**

Run:

```powershell
Copy-Item -LiteralPath $source -Destination $target -Recurse

$sourceHashes = Get-ChildItem $source -File -Recurse | ForEach-Object {
    [pscustomobject]@{
        Relative = $_.FullName.Substring($source.Length)
        Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    }
}
$targetHashes = Get-ChildItem $target -File -Recurse | ForEach-Object {
    [pscustomobject]@{
        Relative = $_.FullName.Substring($target.Length)
        Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    }
}
Compare-Object $sourceHashes $targetHashes -Property Relative, Hash
```

Expected: `Compare-Object` 无输出。

- [ ] **Step 4: 用新 Codex 进程验证 Skill 发现**

Run:

```powershell
$env:OCR_MCP_API_KEY = [Environment]::GetEnvironmentVariable(
  'OCR_MCP_API_KEY', 'User'
)
codex exec --ephemeral --skip-git-repo-check --color never `
  "只做只读检查：确认 using-ocr-mcpserver Skill 是否会在用户提供本地 PDF 路径、要求 OCR 进度和下载结果时触发。列出它要求使用的脚本和 MCP 工具；不要提交任务。"
```

Expected: 明确加载 `using-ocr-mcpserver`，列出 `ocr-transfer.ps1`、`parse_documents`、`get_task_status`，不要求用户手工复制 `file_id`。

- [ ] **Step 5: 准备真实前向测试目录**

使用用户已批准的 `44pages.pdf`，只复制到工作区临时目录，不提交：

```powershell
$e2e = 'D:\codex-workspace\ocrzhengli\.skill-e2e'
New-Item -ItemType Directory -Path $e2e | Out-Null
Copy-Item -LiteralPath `
  'C:\Users\jiang_ren_1291992796\Downloads\测试数据\44pages.pdf' `
  -Destination (Join-Path $e2e 'document.pdf')
Get-FileHash (Join-Path $e2e 'document.pdf') -Algorithm SHA256
```

Expected SHA-256:

```text
6b9710a2b0b1e3cdda06a1c81d098a59f8ac41091140f58984bac3f8c5d69082
```

- [ ] **Step 6: 用全新 Codex 进程执行真实完整流程**

Run:

```powershell
$env:OCR_MCP_API_KEY = [Environment]::GetEnvironmentVariable(
  'OCR_MCP_API_KEY', 'User'
)
codex exec --ephemeral --skip-git-repo-check `
  -C 'D:\codex-workspace\ocrzhengli\.skill-e2e' `
  --sandbox workspace-write --color never `
  "使用 using-ocr-mcpserver 处理本目录 document.pdf。自动上传，调用 ocr-mcpserver，每 15 秒只在进度变化时汇报；完成后下载并安全解压。不要读取或输出 OCR 正文，也不要输出密钥。"
```

Expected:

- 自动运行上传脚本并取得 `file_id`；
- 调用 `parse_documents`；
- 至少报告已提交和终止状态，任务超过 15 秒时报告中间进度；
- 调用 `get_task_status` 直至终止；
- 在 `.skill-e2e\ocr-results\<batch_id>\` 保留 ZIP 和解压目录；
- 解压目录包含 `final.md`；
- 输出不包含 API Key 或 OCR 正文。

- [ ] **Step 7: 验证产物结构和服务器隐私**

Run:

```powershell
$results = Get-ChildItem `
  'D:\codex-workspace\ocrzhengli\.skill-e2e\ocr-results' `
  -Directory
$final = Get-ChildItem $results.FullName -Filter final.md -Recurse
$zips = Get-ChildItem $results.FullName -Filter *.zip -Recurse
if ($final.Count -lt 1 -or $zips.Count -lt 1) {
    throw 'Forward test artifacts are incomplete.'
}

ssh ubuntu-server `
  "docker inspect ocr-mcp-server-ocr-production-1 --format '{{.State.Health.Status}}|{{.Image}}'"
```

Expected: 至少一个 `final.md`、至少一个 ZIP；正式容器仍为 `healthy` 且镜像 ID 为 `sha256:56d0f319f6e986a02d787b145bdc1cd76711abf396b7ecd99770b0912b048d9e`。

- [ ] **Step 8: 清理临时前向测试副本**

先解析并验证目标路径：

```powershell
$resolved = [System.IO.Path]::GetFullPath(
  'D:\codex-workspace\ocrzhengli\.skill-e2e'
)
$allowed = [System.IO.Path]::GetFullPath(
  'D:\codex-workspace\ocrzhengli'
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolved.StartsWith(
  $allowed,
  [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Unsafe cleanup target: $resolved"
}
Remove-Item -LiteralPath $resolved -Recurse -Force
```

Expected: 只删除 `.skill-e2e` 测试副本；服务器任务和 24 小时产物保留策略不变。

- [ ] **Step 9: 提交安装验证记录**

在现有设计文档末尾增加不含业务正文的验证记录：Skill 源提交 SHA、安装目标、测试文件 SHA、批次终态、ZIP/解压结构计数和验证时间。

```powershell
git add docs/superpowers/specs/2026-07-24-using-ocr-mcpserver-skill-design.md
git commit -m "docs: 记录 OCR MCP Skill 前向验证"
```

---

### Task 6: 最终复核与发布

**Files:**
- Verify: `skills/using-ocr-mcpserver/**`
- Verify: `tests/skill/**`
- Verify: `docs/superpowers/specs/2026-07-24-using-ocr-mcpserver-skill-design.md`

**Interfaces:**
- Consumes: 所有已提交 Skill、测试和验证记录。
- Produces: 可复现、已安装、可被新 Codex 任务发现的最终 Skill。

- [ ] **Step 1: 运行静态和完整测试**

Run:

```powershell
git diff --check
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest tests/skill -q
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest -q
```

Expected: 所有命令退出码 0。

- [ ] **Step 2: 验证 Skill 目录**

Run:

```powershell
$creator = "$env:USERPROFILE\.codex\skills\.system\skill-creator"
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  "$creator\scripts\quick_validate.py" `
  skills/using-ocr-mcpserver
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  "$creator\scripts\quick_validate.py" `
  "$env:USERPROFILE\.codex\skills\using-ocr-mcpserver"
```

Expected: 源目录和安装目录都验证成功。

- [ ] **Step 3: 验证 Git 范围和安装一致性**

Run:

```powershell
git status --short
git log --oneline --decorate -8
```

确认没有暂存或提交用户现有的：

```text
src/ocr_mcp_server/services/input_preprocessing.py
tests/test_input_preprocessing.py
uv.lock
```

重新运行 Task 5 的 SHA-256 比较，Expected: 无差异。

- [ ] **Step 4: 按完成分支流程交付**

**REQUIRED SUB-SKILL:** Use superpowers:finishing-a-development-branch.

报告：

- Skill 源码目录；
- 全局安装目录；
- 触发示例；
- 脚本测试与仓库完整测试结果；
- 真实前向测试的批次终态和本地产物结构；
- 5090 正式容器未改动且保持健康。
