# MinerU 空引用表格 Markdown 降级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MinerU 生成的精确 `images/` 空图片引用表格在 HTML 无效时只从最终 Markdown 省略，而不阻止 JSON 和 ZIP 产物发布。

**Architecture:** 保持候选收集、Paddle、合并和 JSON 产物不变，只在 `render_markdown` 的表格分支内捕获结构校验失败。只有同一节点的 `image_source.path` 精确等于 `images/` 时降级为省略并复用现有无正文警告；其他结构错误继续交给现有外层错误映射。

**Tech Stack:** Python 3.11、pytest、FastAPI、SQLite、Docker Compose、MinerU VLM HTTP、PaddleOCR `pp_structure_v3`。

## Global Constraints

- 只豁免 ASCII 精确字符串 `images/`；`images//`、绝对路径、父目录、反斜杠、缺失文件、目录和符号链接继续失败。
- 无效空引用节点只从 `final.md` 省略；原始 JSON、最终 JSON、审计文件和 MinerU 原始产物保持不变。
- HTML 有效的精确空引用表格仍必须渲染到 `final.md`。
- 不扫描图片目录，不把孤立图片猜测绑定到空引用节点。
- 不改变 REST、MCP、任务状态或恢复接口。
- 日志、测试输出和部署记录不得包含业务正文、识别文本、API Key 或恢复令牌。
- 单文件默认上限保持 `62914560` 字节，批次限制保持不变。
- 开发顺序固定为：本机测试、同步 Ubuntu、远程构建、立即启动验证、真实数据联调、固定镜像版本。

---

### Task 1: 用 TDD 实现 Markdown 窄降级

**Files:**
- Modify: `tests/test_artifacts.py`
- Modify: `src/ocr_mcp_server/services/artifacts.py:235-328`

**Interfaces:**
- Consumes: `render_markdown(manifest: object, *, image_names: Mapping[str, str], max_bytes: int) -> MarkdownRenderResult`
- Produces: 表格 HTML 无效且 `image_source.path == "images/"` 时返回成功结果，并在 `warning_codes` 中包含 `ArtifactErrorCode.UNSUPPORTED_NODE.value`。

- [ ] **Step 1: 写入三个渲染器回归测试**

在 `tests/test_artifacts.py` 的 Markdown 渲染测试组中加入：

```python
def test_markdown_omits_invalid_table_with_exact_empty_image_reference() -> None:
    rendered = render_markdown(
        [[
            {"type": "text", "content": {"text": "before"}},
            {
                "type": "table",
                "content": {
                    "html": "<table></table>",
                    "image_source": {"path": "images/"},
                },
            },
            {"type": "text", "content": {"text": "after"}},
        ]],
        image_names={},
        max_bytes=10_000,
    )

    assert rendered.content == b"before\n\nafter\n"
    assert rendered.warning_codes == (
        ArtifactErrorCode.UNSUPPORTED_NODE.value,
    )


def test_markdown_renders_valid_table_with_exact_empty_image_reference() -> None:
    rendered = render_markdown(
        [[{
            "type": "table",
            "content": {
                "html": "<table><tr><td>kept</td></tr></table>",
                "image_source": {"path": "images/"},
            },
        }]],
        image_names={},
        max_bytes=10_000,
    )

    assert b"<table><tr><td>kept</td></tr></table>" in rendered.content
    assert rendered.warning_codes == ()


def test_markdown_rejects_invalid_table_without_exact_empty_reference() -> None:
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(
            [[{
                "type": "table",
                "content": {
                    "html": "<table></table>",
                    "image_source": {"path": "images/used.png"},
                },
            }]],
            image_names={"images/used.png": "images/000000.png"},
            max_bytes=10_000,
        )

    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
```

- [ ] **Step 2: 运行目标测试并确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_artifacts.py::test_markdown_omits_invalid_table_with_exact_empty_image_reference `
  tests/test_artifacts.py::test_markdown_renders_valid_table_with_exact_empty_image_reference `
  tests/test_artifacts.py::test_markdown_rejects_invalid_table_without_exact_empty_reference `
  -q
```

预期：第一个测试因 `artifact_input_invalid` 失败；后两个测试通过。失败必须来自缺少降级行为，而不是夹具或导入错误。

- [ ] **Step 3: 写入最小实现**

将 `render_markdown` 的表格分支改为：

```python
                elif node_type == "table":
                    try:
                        block = validate_table_html(content.get("html"), limits)
                    except StructuredContentInvalid:
                        image_source = content.get("image_source")
                        raw_path = (
                            image_source.get("path")
                            if isinstance(image_source, Mapping)
                            else None
                        )
                        if raw_path != "images/":
                            raise
                        warned = True
                        block = None
```

不要修改外层 `except StructuredContentInvalid`；它继续把其他结构错误映射为 `artifact_input_invalid`。

- [ ] **Step 4: 运行三个目标测试并确认 GREEN**

运行 Step 2 的同一命令。

预期：三个测试全部通过，零失败。

- [ ] **Step 5: 扩充 ZIP 集成回归**

在现有 `test_zip_ignores_exact_mineru_empty_image_reference` 中把空引用表格 HTML 改为无效值：

```python
"html": "<table></table>",
```

在打开 ZIP 后增加：

```python
        final = json.loads(archive.read("content_list_v2.json"))
        manifest = json.loads(archive.read("artifact_manifest.json"))
        assert final[0][-1]["content"]["image_source"]["path"] == "images/"
        assert manifest["warning_codes"] == [
            ArtifactErrorCode.UNSUPPORTED_NODE.value
        ]
```

这证明 JSON 节点保留、孤立图片未新增、ZIP 成功且警告无正文。

- [ ] **Step 6: 运行 ZIP 集成回归**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_artifacts.py::test_zip_ignores_exact_mineru_empty_image_reference `
  -q
```

预期：通过，ZIP 中仍只有经验证的图片条目。

- [ ] **Step 7: 提交 TDD 修复**

```powershell
git add -- src/ocr_mcp_server/services/artifacts.py tests/test_artifacts.py
git commit -m "fix: omit invalid empty-reference tables from markdown"
```

预期：提交只包含上述生产代码和回归测试。

---

### Task 2: 本机完整验证与 GitHub 推送

**Files:**
- No source changes.

**Interfaces:**
- Consumes: Task 1 的提交。
- Produces: 本机打包模块及完整测试套件通过的 Git SHA。

- [ ] **Step 1: 运行打包模块测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_artifacts.py -q
```

预期：零失败；平台能力相关测试只出现项目已有的预期跳过。

- [ ] **Step 2: 运行完整测试套件**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

预期：进度达到 `100%`、零失败。若命令超时，先确认并终止仅由本次命令留下的 pytest 子进程，再重新完整运行。

- [ ] **Step 3: 检查并推送**

```powershell
git diff --check
git status --short
git push origin fix/mineru-empty-image-reference
```

预期：除已知未跟踪 `tmp/` 外无未提交源码；推送成功。

---

### Task 3: 同步 Ubuntu、构建并立即启动验证

**Files:**
- Build: `docker/ocr-gateway-ppstructure.Dockerfile`
- No source changes.

**Interfaces:**
- Consumes: Task 2 的完整 Git SHA。
- Produces: 基于同一 SHA 的 `ocr-mcp-server:production-dev` 健康容器；现有 MinerU API/VLM 容器保持运行。

- [ ] **Step 1: 通过 Git bundle 同步精确提交**

本机：

```powershell
git bundle create tmp/mineru-empty-table-fallback.bundle `
  fix/mineru-empty-image-reference
Get-FileHash tmp/mineru-empty-table-fallback.bundle -Algorithm SHA256
scp tmp/mineru-empty-table-fallback.bundle `
  ubuntu-server:/home/jiangren/
```

Ubuntu：

```bash
cd /home/jiangren/ocr-mcp-server
sha256sum /home/jiangren/mineru-empty-table-fallback.bundle
git status --short
git fetch /home/jiangren/mineru-empty-table-fallback.bundle \
  fix/mineru-empty-image-reference
git merge --ff-only FETCH_HEAD
git rev-parse HEAD
git status --short
```

预期：本地和远程 SHA256 一致；远程 HEAD 等于 Task 2 的 SHA，工作树干净。

- [ ] **Step 2: 安全继承运行环境并仅重建网关**

```bash
cd /home/jiangren/ocr-mcp-server
OCR_AUTH__API_KEYS="$(
  docker exec ocr-mcp-server-ocr-production-1 printenv OCR_AUTH__API_KEYS
)"
OCR_PUBLIC_BASE_URL="$(
  docker exec ocr-mcp-server-ocr-production-1 printenv OCR_PUBLIC_BASE_URL
)"
export OCR_AUTH__API_KEYS OCR_PUBLIC_BASE_URL
export OCR_GATEWAY_PORT=18011
docker compose --profile production build ocr-production
docker compose --profile production up -d --no-deps --force-recreate \
  ocr-production
```

不得打印两个环境变量的值。预期：生成新 `production-dev` 镜像，只重建网关。

- [ ] **Step 3: 立即验证健康和配置**

```bash
docker inspect ocr-mcp-server-ocr-production-1 \
  --format '{{.State.Health.Status}} {{.State.Running}} {{.Image}}'
docker exec ocr-mcp-server-ocr-production-1 python -c \
  'from ocr_mcp_server.settings import load_settings; assert load_settings().limits.max_file_size_bytes == 62914560'
```

若健康状态仍为 `starting`，以 10 秒间隔检查，最长 120 秒。预期：`healthy true`。

本机 SSH 隧道：

```powershell
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:18011/health/ready' `
  -TimeoutSec 15
```

预期：SQLite、MinerU、Paddle 均为 `ready`。

---

### Task 4: 两份真实 PDF 回归

**Files:**
- Input: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-006.pdf`
- Input: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-002-xz.pdf`
- Output: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results\report-006-result-final.zip`
- Output: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results\report-002-xz-result-final.zip`

**Interfaces:**
- Consumes: FastAPI `POST /v1/uploads`、`POST /v1/tasks`、`GET /v1/tasks/{batch_id}`、`GET /v1/artifacts/{artifact_id}`。
- Produces: 两个终态成功任务和两个结构有效的 ZIP；旋转样本只报告恢复令牌是否存在。

- [ ] **Step 1: 通过现有安全客户端依次提交两份 PDF**

```powershell
$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:18011'
$rawKey = ssh ubuntu-server `
  'docker exec ocr-mcp-server-ocr-production-1 printenv OCR_AUTH__API_KEYS'
$apiKey = [string](($rawKey | ConvertFrom-Json)[0])
$auth = @{'X-API-Key' = $apiKey}

function Submit-OcrPdf([string]$PdfPath) {
  $uploadHeaders = @{
    'X-API-Key' = $apiKey
    'X-Document-Name' = [IO.Path]::GetFileName($PdfPath)
    'Idempotency-Key' = 'manual-u-' + [guid]::NewGuid().ToString('N')
  }
  $upload = Invoke-RestMethod `
    -Method Post `
    -Uri "$base/v1/uploads" `
    -Headers $uploadHeaders `
    -ContentType 'application/pdf' `
    -InFile $PdfPath `
    -TimeoutSec 120
  $body = @{
    sources = @(@{file_id = $upload.file_id})
    idempotency_key = 'manual-t-' + [guid]::NewGuid().ToString('N')
  } | ConvertTo-Json -Depth 5 -Compress
  $task = Invoke-RestMethod `
    -Method Post `
    -Uri "$base/v1/tasks" `
    -Headers $auth `
    -ContentType 'application/json' `
    -Body $body `
    -TimeoutSec 60
  $deadline = [DateTime]::UtcNow.AddSeconds(1200)
  do {
    $status = Invoke-RestMethod `
      -Uri "$base/v1/tasks/$($task.batch_id)" `
      -Headers $auth `
      -TimeoutSec 20
    if ($status.status -in @(
      'completed', 'completed_with_errors', 'failed', 'cancelled'
    )) {
      break
    }
    Start-Sleep -Seconds 2
  } while ([DateTime]::UtcNow -lt $deadline)
  if ($status.status -notin @('completed', 'completed_with_errors')) {
    throw "OCR task did not complete successfully: $($status.status)"
  }
  return $status
}

$simpleStatus = Submit-OcrPdf `
  'C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-006.pdf'
$rotatedStatus = Submit-OcrPdf `
  'C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-002-xz.pdf'
```

预期：

```text
report-006: completed 或 completed_with_errors
report-002-xz: completed 或 completed_with_errors
```

不得把任务的公开安全错误码当作成功；若失败，先查询无正文阶段事件，不打印业务内容。

- [ ] **Step 2: 下载并验证两个 ZIP**

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$outDir = 'C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

function Save-And-CheckArtifact(
  $Status,
  [string]$OutputName
) {
  if (@($Status.artifacts).Count -lt 1) {
    throw 'OCR task has no artifact'
  }
  $artifactId = $Status.artifacts[0].artifact_id
  $outputPath = Join-Path $outDir $OutputName
  Invoke-WebRequest `
    -Uri "$base/v1/artifacts/$artifactId" `
    -Headers $auth `
    -OutFile $outputPath `
    -TimeoutSec 120
  $zip = [IO.Compression.ZipFile]::OpenRead($outputPath)
  try {
    $entries = @($zip.Entries)
    if ($entries.Count -lt 1) { throw 'Artifact is empty' }
    if (@($entries | Where-Object FullName -Match '\.md$').Count -lt 1) {
      throw 'Artifact has no Markdown'
    }
    if (@($entries | Where-Object FullName -Match '\.json$').Count -lt 1) {
      throw 'Artifact has no JSON'
    }
  } finally {
    $zip.Dispose()
  }
  $env:OCR_ARTIFACT_PATH = $outputPath
  & 'C:\Users\jiang_ren_1291992796\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
    -c 'import os,zipfile; z=zipfile.ZipFile(os.environ["OCR_ARTIFACT_PATH"]); assert z.testzip() is None; z.close()'
}

Save-And-CheckArtifact `
  $simpleStatus `
  'report-006-result-final.zip'
Save-And-CheckArtifact `
  $rotatedStatus `
  'report-002-xz-result-final.zip'
```

只输出路径、字节数、条目数和文件类型计数，不输出条目正文。

- [ ] **Step 3: 检查旋转恢复信息**

只记录：

```text
batch status
file status
artifact count
recovery_token 是否存在
```

不得打印恢复令牌。即使令牌存在，也不得自动调用 `reparse_with_page_orientation`；恢复重解析仍需用户确认页码。

---

### Task 5: 部署门禁、镜像固化与不可变记录

**Files:**
- Create: `deployments/ocr-mcp-server/${GIT_SHA}.env`

**Interfaces:**
- Consumes: Task 3 的镜像 ID、Task 4 的真实数据结果。
- Produces: 完整部署门禁通过的不可变镜像标签和不含敏感信息的部署记录。

- [ ] **Step 1: 运行完整部署门禁**

```bash
cd /home/jiangren/ocr-mcp-server
OCR_AUTH__API_KEYS="$(
  docker exec ocr-mcp-server-ocr-production-1 printenv OCR_AUTH__API_KEYS
)"
OCR_PUBLIC_BASE_URL="$(
  docker exec ocr-mcp-server-ocr-production-1 printenv OCR_PUBLIC_BASE_URL
)"
OCR_VERIFY_API_KEY="$(
  docker run --rm -i \
    -e OCR_AUTH__API_KEYS="$OCR_AUTH__API_KEYS" \
    ocr-mcp-server:ppstructure-cpu-dev \
    python -c \
    'import json,os; print(json.loads(os.environ["OCR_AUTH__API_KEYS"])[0])'
)"
export OCR_AUTH__API_KEYS OCR_PUBLIC_BASE_URL OCR_VERIFY_API_KEY
export OCR_GATEWAY_PORT=18011
GIT_SHA="$(git rev-parse HEAD)"
RUN_ID="$(
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$PWD:/workspace:ro" \
    -w /workspace \
    ocr-mcp-server:ppstructure-cpu-dev \
    python scripts/verify_deployment.py \
      --derive-run-id \
      --git-sha "$GIT_SHA"
)"
test -n "$RUN_ID"
docker run --rm \
  --network host \
  -e OCR_VERIFY_API_KEY \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:/workspace:ro" \
  -w /workspace \
  ocr-mcp-server:ppstructure-cpu-dev \
  python scripts/verify_deployment.py \
    --phase all \
    --run-id "$RUN_ID" \
    --base-url http://127.0.0.1:18011
```

预期：`static`、`isolation`、`runtime`、`e2e` 四阶段都返回 `"ok": true`。

- [ ] **Step 2: 固定三个镜像标签**

```bash
docker image tag ocr-mcp-server:production-dev \
  "ocr-mcp-server:production-$GIT_SHA"
docker image tag ocr-mcp-server:mineru-api-dev \
  "ocr-mcp-server:mineru-api-$GIT_SHA"
docker image tag ocr-mcp-server:mineru-vlm-dev \
  "ocr-mcp-server:mineru-vlm-$GIT_SHA"
```

预期：三个不可变标签解析到门禁使用的同一组镜像 ID。

- [ ] **Step 3: 写入部署记录**

用 `apply_patch` 新建 `deployments/ocr-mcp-server/${GIT_SHA}.env`，以现有记录为格式，至少记录 Git SHA、分支、UTC 验证时间、运行 ID、四阶段门禁结果、三个镜像标签与 ID、60 MiB 限制、模型清单摘要、驱动/工具包版本、三个服务健康状态，以及：

```text
REAL_PDF_REPORT_006=passed
REAL_PDF_REPORT_002_XZ=passed
EMPTY_REFERENCE_MARKDOWN_FALLBACK=passed
```

不得记录 API Key、正文、识别文本、恢复令牌或下载 URL。

- [ ] **Step 4: 提交并推送部署记录**

```powershell
git add -- "deployments/ocr-mcp-server/$GIT_SHA.env"
git commit -m "docs: record verified empty-table fallback deployment"
git push origin fix/mineru-empty-image-reference
git status --short --branch
```

预期：提交和推送成功；除本地临时 `tmp/` 外没有未提交项目文件。
