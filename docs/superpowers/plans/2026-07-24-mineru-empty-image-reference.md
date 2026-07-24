# MinerU 空图片引用兼容实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让候选收集器安全跳过 MinerU 精确 `images/` 空图片引用，同时保持所有有效图片引用的 Paddle 二次检测和现有路径安全边界。

**Architecture:** 只在 `document_content_list_v2.json` 转换为候选引用的入口增加一个精确哨兵判断。该判断不修改 MinerU 原始产物、不扫描图片目录、不猜测孤立文件与节点的映射；其余路径继续走现有规范化、目录约束和图片校验。

**Tech Stack:** Python 3.11、pytest、Pillow、FastAPI、SQLite、MinerU `vlm-http-client`、PaddleOCR `pp_structure_v3`、Docker Compose、NVIDIA RTX 5090。

## Global Constraints

- 仅跳过精确 ASCII 字符串 `images/`；不接受前后空白、大小写变体或其他空路径段。
- `images//`、`images`、绝对路径、父目录、反斜杠、缺失文件、目录、符号链接和非普通文件继续失败。
- 不扫描 `images` 目录，不把未引用的孤立文件绑定到任何节点。
- 所有带有效文件路径的 MinerU 图片引用继续进入候选收集和 Paddle 二次检测。
- 只有通过有效性校验的表格和公式结果允许替换；其他图片保持原样。
- 不改变 REST、MCP、SQLite、旋转恢复或保留期合约。
- 日志和验证输出不得包含业务正文、识别文本、原始路径、API Key 或下载 URL。
- 交付节奏固定为：本机测试、同步 Ubuntu、构建镜像、立即启动验证、真实数据复测、部署门禁、固定镜像版本。

---

### Task 1: 候选收集器回归测试与最小修复

**Files:**
- Modify: `tests/test_candidate_collection.py`
- Modify: `src/ocr_mcp_server/services/candidate_collection.py:203-246`

**Interfaces:**
- Consumes: `collect_image_candidates(result, result_version, engine, standalone_image, max_image_pixels) -> CandidateCollection`
- Produces: `_manifest_entries(manifest) -> tuple[tuple[str, CandidateReference], ...]` 对精确 `images/` 不生成条目。

- [ ] **Step 1: 写入精确空引用的失败回归测试**

在 `tests/test_candidate_collection.py` 的结构化候选测试后加入：

```python
def test_skips_exact_mineru_empty_image_reference_and_keeps_valid_candidates(
    tmp_path: Path,
) -> None:
    manifest = [[
        {
            "type": "table",
            "content": {
                "image_source": {"path": "images/"},
                "html": "<table></table>",
            },
            "bbox": [166, 120, 843, 256],
        },
        {
            "type": "image",
            "content": {"image_source": {"path": "images/valid.png"}},
        },
    ]]
    result = _published_result(tmp_path, manifest)
    _write_image(result.images_directory / "valid.png")
    _write_image(result.images_directory / "orphan.png")

    collection = _collect(result)

    assert len(collection.candidates) == 1
    assert collection.candidates[0].primary_path == (
        result.images_directory / "valid.png"
    )
    assert [
        (reference.page_index, reference.node_index, reference.json_pointer)
        for reference in collection.candidates[0].references
    ] == [(0, 1, "/0/1")]
    assert collection.total_reference_count == 1
    assert all(
        result.images_directory / "orphan.png" not in candidate.alias_paths
        for candidate in collection.candidates
    )
```

- [ ] **Step 2: 运行目标测试并确认 RED**

在隔离工作树根目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest `
  tests/test_candidate_collection.py::test_skips_exact_mineru_empty_image_reference_and_keeps_valid_candidates `
  -q
```

预期：测试以 `CandidateCollectionFailure` 和
`candidate_path_unsafe_or_missing` 失败，证明复现了真实数据缺陷。

- [ ] **Step 3: 写入最小实现**

在 `src/ocr_mcp_server/services/candidate_collection.py` 的
`_manifest_entries` 中，完成字符串类型与非空校验后、创建
`CandidateReference` 前加入：

```python
            if path == "images/":
                continue
```

不得对 `_safe_posix_parts` 或 `_resolve_manifest_path` 增加一般性豁免。

- [ ] **Step 4: 运行目标测试并确认 GREEN**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest `
  tests/test_candidate_collection.py::test_skips_exact_mineru_empty_image_reference_and_keeps_valid_candidates `
  -q
```

预期：`1 passed`。

- [ ] **Step 5: 验证相邻安全边界**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest tests/test_candidate_collection.py -q
```

预期：整个文件通过；`images//empty.png`、缺失文件和目录路径测试仍通过其
“拒绝”断言。

- [ ] **Step 6: 运行本机完整测试**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& 'D:\codex-workspace\ocrzhengli\ocr-mcp-server\.venv\Scripts\python.exe' `
  -m pytest -q
```

预期：退出码 `0`，无失败；只允许仓库已有的环境型跳过。

- [ ] **Step 7: 提交实现**

```powershell
git add -- tests/test_candidate_collection.py `
  src/ocr_mcp_server/services/candidate_collection.py
git commit -m "fix: tolerate MinerU empty image references"
```

---

### Task 2: GitHub 推送与 Ubuntu 源码同步

**Files:**
- No source changes.
- Temporary local artifact: `tmp/mineru-empty-image-reference.bundle`
- Remote target: `/home/jiangren/ocr-mcp-server`

**Interfaces:**
- Consumes: Task 1 的已测试提交。
- Produces: GitHub 分支与 Ubuntu 干净工作树指向同一提交。

- [ ] **Step 1: 检查本地工作树和提交**

```powershell
git status --short --branch
git rev-parse HEAD
```

预期：工作树无未提交变更；记录完整 Git SHA。

- [ ] **Step 2: 推送当前分支到 GitHub**

```powershell
git push -u origin HEAD
```

预期：`zhukefucn/OCR-MCPSERVER` 上的同名分支更新成功。

- [ ] **Step 3: 创建有限 Git bundle**

```powershell
New-Item -ItemType Directory -Force -Path 'tmp' | Out-Null
git bundle create 'tmp/mineru-empty-image-reference.bundle' HEAD
git bundle verify 'tmp/mineru-empty-image-reference.bundle'
Get-FileHash -Algorithm SHA256 `
  -LiteralPath 'tmp/mineru-empty-image-reference.bundle'
```

预期：bundle 验证成功并取得 SHA-256。

- [ ] **Step 4: 上传并校验 bundle**

```powershell
scp 'tmp/mineru-empty-image-reference.bundle' `
  ubuntu-server:/home/jiangren/mineru-empty-image-reference.bundle
ssh ubuntu-server `
  'sha256sum /home/jiangren/mineru-empty-image-reference.bundle'
```

预期：远端 SHA-256 与本地一致。

- [ ] **Step 5: 在 Ubuntu 快进到已测试提交**

```powershell
ssh ubuntu-server @'
set -eu
cd /home/jiangren/ocr-mcp-server
test -z "$(git status --porcelain)"
git fetch /home/jiangren/mineru-empty-image-reference.bundle HEAD
git merge --ff-only FETCH_HEAD
git status --short --branch
git rev-parse HEAD
'@
```

预期：远端完整 Git SHA 与本地一致，工作树干净。

---

### Task 3: Ubuntu 构建、立即启动与基础验证

**Files:**
- No source changes.
- Build: `docker/ocr-gateway-ppstructure.Dockerfile`

**Interfaces:**
- Consumes: Ubuntu 上与 Task 1 相同的提交。
- Produces: 新的 `ocr-mcp-server:production-dev` 运行镜像；现有
  `mineru-api-dev` 和 `mineru-vlm-dev` 可复用，因为本次只改网关代码。

- [ ] **Step 1: 安全继承当前部署环境**

在 Ubuntu shell 中执行，变量值不得打印：

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
```

预期：两个变量非空；终端不输出其内容。

- [ ] **Step 2: 重建生产网关镜像**

```bash
docker compose --profile production build ocr-production
```

预期：退出码 `0`，生成新的 `ocr-mcp-server:production-dev`。

- [ ] **Step 3: 立即重建生产网关容器**

```bash
docker compose --profile production up -d --no-deps --force-recreate \
  ocr-production
```

预期：只重建网关，MinerU API 和 VLM 保持运行。

- [ ] **Step 4: 有界等待容器健康**

```bash
for attempt in $(seq 1 40); do
  status="$(
    docker inspect ocr-mcp-server-ocr-production-1 \
      --format '{{.State.Health.Status}}'
  )"
  [ "$status" = healthy ] && break
  [ "$status" = unhealthy ] && exit 1
  sleep 3
done
test "$status" = healthy
```

预期：最多 120 秒内为 `healthy`。

- [ ] **Step 5: 验证服务依赖和 60 MiB 配置**

```bash
docker compose --profile production exec -T ocr-production \
  python -c \
  "from ocr_mcp_server.settings import load_settings; assert load_settings().limits.max_file_size_bytes == 62914560"
curl --fail --silent http://127.0.0.1:18011/health/ready
```

预期：配置断言退出码 `0`，readiness 为 `ready`，SQLite、MinerU、Paddle
均为 `ready`。

---

### Task 4: 真实 PDF 回归与旋转恢复检查

**Files:**
- Input: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-006.pdf`
- Input: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-002-xz.pdf`
- Output: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results\report-006-result-fixed.zip`
- Output: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results\report-002-xz-result.zip`

**Interfaces:**
- Consumes: FastAPI `POST /v1/uploads`、`POST /v1/tasks`、
  `GET /v1/tasks/{batch_id}`、`GET /v1/artifacts/{artifact_id}`。
- Produces: 两个终态批次、有效 ZIP，以及旋转样本的
  `recovery_token` 存在性结论。

- [ ] **Step 1: 确认本机 SSH 隧道和 readiness**

```powershell
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:18011/health/ready' `
  -TimeoutSec 10
```

预期：`status` 为 `ready`。

- [ ] **Step 2: 通过现有安全测试客户端依次提交两个文件**

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

预期：两个任务均为 `completed` 或 `completed_with_errors`，不得为
`failed`。

- [ ] **Step 3: 下载并验证 ZIP**

```powershell
$outDir = 'C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Save-And-CheckArtifact($Status, [string]$OutputName) {
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
  'report-006-result-fixed.zip'
Save-And-CheckArtifact `
  $rotatedStatus `
  'report-002-xz-result.zip'
```

不得向终端打印 Markdown、JSON 正文或图片内容。

- [ ] **Step 4: 检查旋转恢复信息**

记录 `report-002-xz.pdf` 的：

```text
batch status
file status
artifact count
recovery_token 是否存在
```

若存在 `recovery_token`，只报告“存在”，不得打印令牌。旋转恢复调用必须
等待用户确认页码；本任务不自动提交恢复重解析。

---

### Task 5: 部署门禁、镜像固化与记录

**Files:**
- Create: `deployments/ocr-mcp-server/${GIT_SHA}.env`

**Interfaces:**
- Consumes: Task 3 的三个候选镜像 ID 和 Task 4 的真实数据结果。
- Produces: 静态、隔离、运行时、端到端门禁均通过的不可变镜像标签和部署
  记录。

- [ ] **Step 1: 从现有部署密钥派生验证变量但不打印**

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
```

预期：变量均非空，终端不输出值。

- [ ] **Step 2: 生成绑定镜像 ID 的运行 ID**

```bash
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
```

预期：`RUN_ID` 匹配正则
`^[a-f0-9]{40}-images-[a-f0-9]{64}$`。

- [ ] **Step 3: 运行完整部署门禁**

```bash
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

预期依次输出无内容 JSON：

```json
{"image_count":3,"ok":true,"stage":"static"}
{"image_count":2,"ok":true,"stage":"isolation"}
{"ok":true,"stage":"runtime","tool_count":3}
{"count":1,"ok":true,"stage":"e2e"}
```

- [ ] **Step 4: 固定三个镜像标签**

```bash
docker image tag ocr-mcp-server:production-dev \
  "ocr-mcp-server:production-$GIT_SHA"
docker image tag ocr-mcp-server:mineru-api-dev \
  "ocr-mcp-server:mineru-api-$GIT_SHA"
docker image tag ocr-mcp-server:mineru-vlm-dev \
  "ocr-mcp-server:mineru-vlm-$GIT_SHA"
```

预期：三个标签均可由 `docker image inspect` 解析，并与门禁使用的镜像 ID
一致。

- [ ] **Step 5: 写入不可变部署记录**

使用 `apply_patch` 新增 `deployments/ocr-mcp-server/${GIT_SHA}.env`，至少
记录：

```text
DEPLOYMENT_SCHEMA_VERSION=1
SOURCE_GIT_SHA=${GIT_SHA}
SOURCE_BRANCH=fix/mineru-empty-image-reference
VERIFIED_AT_UTC=${VERIFIED_AT_UTC}
VERIFICATION_RUN_ID=${RUN_ID}
VERIFICATION_STATIC=passed
VERIFICATION_ISOLATION=passed
VERIFICATION_RUNTIME=passed
VERIFICATION_E2E=passed
VERIFICATION_E2E_ARTIFACT_COUNT=1
GATEWAY_HTTP_PORT=18011
DEFAULT_MAX_FILE_SIZE_BYTES=62914560
PRODUCTION_IMAGE_TAG=ocr-mcp-server:production-${GIT_SHA}
PRODUCTION_IMAGE_ID=${PRODUCTION_IMAGE_ID}
MINERU_API_IMAGE_TAG=ocr-mcp-server:mineru-api-${GIT_SHA}
MINERU_API_IMAGE_ID=${MINERU_API_IMAGE_ID}
MINERU_VLM_IMAGE_TAG=ocr-mcp-server:mineru-vlm-${GIT_SHA}
MINERU_VLM_IMAGE_ID=${MINERU_VLM_IMAGE_ID}
PP_STRUCTURE_MODEL_MANIFEST_SHA256=${PP_STRUCTURE_MODEL_MANIFEST_SHA256}
NVIDIA_DRIVER_VERSION=${NVIDIA_DRIVER_VERSION}
NVIDIA_CONTAINER_TOOLKIT_VERSION=${NVIDIA_CONTAINER_TOOLKIT_VERSION}
PRODUCTION_SERVICE_HEALTH=healthy
MINERU_API_SERVICE_HEALTH=healthy
MINERU_VLM_SERVICE_HEALTH=healthy
REAL_PDF_REPORT_006=passed
REAL_PDF_REPORT_002_XZ=passed
```

不得记录 API Key、文件正文、识别文本、恢复令牌或下载 URL。

- [ ] **Step 6: 提交并推送部署记录**

```powershell
git add -- "deployments/ocr-mcp-server/$GIT_SHA.env"
git commit -m "docs: record verified empty-image-reference deployment"
git push origin HEAD
git status --short --branch
```

预期：提交和推送成功，工作树干净。
