# MinerU V2 列表节点产物渲染修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让产物打包器严格支持 MinerU V2 `list_items` 列表结构，使 `44pages.pdf` 能生成并发布经过校验的结果 ZIP。

**Architecture:** 只修改产物 Markdown 渲染边界。新增一个内部 V2 列表渲染函数，复用现有内联 span 渲染与公式校验；`render_markdown()` 优先处理 V2 结构，不存在 `list_items` 时才走旧式扁平列表兼容路径。

**Tech Stack:** Python 3.11、pytest、FastAPI/FastMCP、Docker Compose、MinerU VLM HTTP、Paddle PP-StructureV3。

## Global Constraints

- 不修改 MinerU、Paddle、任务状态机、MCP 工具集合或 Stage 0。
- `list_type` 仅接受 `text_list`、`reference_list`。
- `item_type` 仅接受 `text`。
- V2 结构存在但畸形时必须安全失败，不能回退到旧式字段。
- 旧式 `content.text` / `content.text_content` 继续兼容。
- 不把业务正文、识别文本、文件内容或凭据写入日志。
- 真实 PDF 不复制进 Git 仓库；自动化测试只使用合成内容和真实结构。
- 交付顺序固定为：本机测试、同步 Ubuntu、远程构建、立即启动验证、真实样例回归、固定镜像版本。

---

### Task 1: 为真实 MinerU V2 列表结构建立失败测试

**Files:**
- Modify: `tests/test_artifacts.py:350`

**Interfaces:**
- Consumes: `render_markdown(manifest: object, *, image_names: Mapping[str, str], max_bytes: int) -> MarkdownRenderResult`
- Produces: MinerU V2 列表正常路径和畸形路径的回归约束。

- [ ] **Step 1: 增加真实 V2 列表结构测试**

在 `test_markdown_renderer_accepts_mineru_v2_paragraph_spans` 后增加：

```python
def test_markdown_renderer_accepts_real_mineru_v2_list_shape() -> None:
    rendered = render_markdown(
        [[
            {
                "type": "list",
                "content": {
                    "list_type": "text_list",
                    "list_items": [
                        {
                            "item_type": "text",
                            "item_content": [
                                {"type": "text", "content": "A&B"},
                                {"type": "equation_inline", "content": "x_i"},
                            ],
                        },
                        {
                            "item_type": "text",
                            "item_content": [
                                {"type": "text", "content": "second"},
                            ],
                        },
                    ],
                },
            },
            {
                "type": "list",
                "content": {
                    "list_type": "reference_list",
                    "list_items": [
                        {
                            "item_type": "text",
                            "item_content": [
                                {"type": "text", "content": "reference"},
                            ],
                        }
                    ],
                },
            },
        ]],
        image_names={},
        max_bytes=1_000,
    )

    assert rendered.content == (
        b"- A&amp;B$x_i$\n- second\n\n- reference\n"
    )
    assert rendered.warning_codes == ()
```

- [ ] **Step 2: 增加 V2 畸形结构参数化测试**

```python
@pytest.mark.parametrize(
    "content",
    [
        {"list_type": "unsupported", "list_items": [{"item_type": "text", "item_content": [{"type": "text", "content": "x"}]}]},
        {"list_type": "text_list", "list_items": []},
        {"list_type": "text_list", "list_items": [{"item_type": "table", "item_content": [{"type": "text", "content": "x"}]}]},
        {"list_type": "text_list", "list_items": [{"item_type": "text", "item_content": []}]},
        {"list_type": "text_list", "list_items": [{"item_type": "text", "item_content": [1]}]},
    ],
)
def test_markdown_renderer_rejects_malformed_mineru_v2_lists(content) -> None:
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(
            [[{"type": "list", "content": content}]],
            image_names={},
            max_bytes=1_000,
        )

    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert caught.value.__cause__ is None
```

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```powershell
python -m pytest tests/test_artifacts.py -k "mineru_v2_list" -q
```

Expected: 正常结构测试在 `artifact_input_invalid` 失败；畸形结构测试保持失败或部分通过，但整体命令必须非零，证明真实结构缺少实现。

- [ ] **Step 4: 检查测试变更**

Run:

```powershell
git diff --check
git diff -- tests/test_artifacts.py
```

Expected: 无空白错误；只有本任务新增测试。

---

### Task 2: 实现严格的 MinerU V2 列表渲染

**Files:**
- Modify: `src/ocr_mcp_server/services/artifacts.py:177-216`
- Modify: `src/ocr_mcp_server/services/artifacts.py:288-292`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `_typed_inline_markdown(content, names, limits) -> tuple[str | None, bool]`
- Produces: `_typed_v2_list_markdown(content: object, limits: StructuredContentLimits) -> tuple[str | None, bool]`

- [ ] **Step 1: 新增最小 V2 列表渲染函数**

紧邻 `_typed_inline_markdown()` 后增加：

```python
def _typed_v2_list_markdown(
    content: object,
    limits: StructuredContentLimits,
) -> tuple[str | None, bool]:
    if not isinstance(content, Mapping) or "list_items" not in content:
        return None, False
    items = content.get("list_items")
    if (
        content.get("list_type") not in {"text_list", "reference_list"}
        or not isinstance(items, list)
        or not items
    ):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    lines: list[str] = []
    warned = False
    for item in items:
        if not isinstance(item, Mapping) or item.get("item_type") != "text":
            _fail(ArtifactErrorCode.INVALID_INPUT)
        spans = item.get("item_content")
        if not isinstance(spans, list) or not spans:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        value, item_warning = _typed_inline_markdown(
            item, ("item_content",), limits
        )
        if value is None:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        lines.append("- " + value)
        warned = warned or item_warning
    return "\n".join(lines), warned
```

- [ ] **Step 2: 将列表分支接到 V2 函数并保留旧格式**

将列表分支替换为：

```python
elif node_type in _LIST_TYPES:
    value, inline_warning = _typed_v2_list_markdown(content, limits)
    if value is None:
        legacy = _typed_string(content, ("text", "text_content"))
        if legacy is None:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        value = "- " + _markdown_escape(legacy)
    warned = warned or inline_warning
    block = value
```

- [ ] **Step 3: 运行定向测试并确认 GREEN**

Run:

```powershell
python -m pytest tests/test_artifacts.py -k "markdown_renderer" -q
```

Expected: 所有 Markdown 渲染测试通过，包括新 V2 测试和旧式列表兼容测试。

- [ ] **Step 4: 运行完整产物测试**

Run:

```powershell
python -m pytest tests/test_artifacts.py -q
```

Expected: `tests/test_artifacts.py` 全部通过。

- [ ] **Step 5: 提交代码与测试**

```powershell
git add -- src/ocr_mcp_server/services/artifacts.py tests/test_artifacts.py
git commit -m "fix: 支持 MinerU V2 列表产物渲染"
```

---

### Task 3: 本机回归与源码交付验证

**Files:**
- Verify: `src/ocr_mcp_server/services/artifacts.py`
- Verify: `tests/test_artifacts.py`
- Verify: `docs/superpowers/specs/2026-07-24-mineru-v2-list-artifact-rendering-design.md`

**Interfaces:**
- Consumes: Task 2 的提交。
- Produces: 可交付的干净 Git 提交和本机测试证据。

- [ ] **Step 1: 运行相关流水线测试**

```powershell
python -m pytest tests/test_artifacts.py tests/test_production_pipeline.py tests/test_production_gateway.py -q
```

Expected: 全部通过。

- [ ] **Step 2: 运行全量测试**

```powershell
python -m pytest -q
```

Expected: 无失败。

- [ ] **Step 3: 检查工作树与提交内容**

```powershell
git diff --check HEAD^
git status --short --branch
git show --stat --oneline HEAD
```

Expected: 工作树干净；提交只包含产物渲染代码和测试。

---

### Task 4: 同步 Ubuntu 并构建候选镜像

**Files:**
- Remote checkout: `/home/jiangren/ocr-mcp-server`
- Docker Compose: `compose.yaml`

**Interfaces:**
- Consumes: 已通过本机全量测试的修复提交 SHA。
- Produces: 与该 SHA 绑定的 Ubuntu 候选镜像。

- [ ] **Step 1: 推送独立修复分支**

```powershell
git push -u origin fix/mineru-v2-list-artifact-rendering
```

Expected: GitHub 分支指向本机已验证提交。

- [ ] **Step 2: 在 Ubuntu 获取精确提交**

```powershell
ssh ubuntu-server "cd /home/jiangren/ocr-mcp-server && git fetch origin fix/mineru-v2-list-artifact-rendering"
```

Expected: fetch 成功，不修改当前运行容器。

- [ ] **Step 3: 创建远程候选工作树**

候选目录使用精确提交短 SHA：

```powershell
$sha = git rev-parse HEAD
$shortSha = $sha.Substring(0, 12)
ssh ubuntu-server "git -C /home/jiangren/ocr-mcp-server worktree add --detach /home/jiangren/ocr-mcp-server-candidate-$shortSha $sha"
```

Expected: 远程候选工作树处于 detached HEAD，HEAD 等于本机修复提交。

- [ ] **Step 4: 构建候选生产镜像**

```powershell
ssh ubuntu-server "cd /home/jiangren/ocr-mcp-server-candidate-$shortSha && docker build --target ppstructure-gpu -f docker/ocr-gateway-ppstructure.Dockerfile -t ocr-mcp-server:production-$sha ."
```

Expected: 构建退出码为 0，镜像 ID 可查询。

---

### Task 5: 启动候选并执行 `44pages.pdf` 端到端回归

**Files:**
- Local sample: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\44pages.pdf`
- Local result directory: `C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results`

**Interfaces:**
- Consumes: Task 4 候选镜像。
- Produces: 健康候选服务、成功 MCP 批次和经过 ZIP 结构校验的结果包。

- [ ] **Step 1: 用独立数据卷和端口启动候选网关**

在 Ubuntu 上从当前生产容器复制环境变量到权限为 `0600` 的临时文件；候选使用独立数据卷，避免与现有 SQLite 和任务 worker 竞争。模型目录只读挂载，网络复用现有 MinerU 服务网络：

```powershell
$sha = git rev-parse HEAD
$shortSha = $sha.Substring(0, 12)
ssh ubuntu-server "env_file=`$(mktemp); chmod 600 \"`$env_file\"; docker inspect ocr-mcp-server-ocr-production-1 --format '{{range .Config.Env}}{{println .}}{{end}}' > \"`$env_file\"; docker run -d --name ocr-mcp-server-list-fix-$shortSha --env-file \"`$env_file\" --network ocr-mcp-server_default -p 127.0.0.1:18012:8000 -v ocr-mcp-server-list-fix-$shortSha-data:/data -v /home/jiangren/ocr-mcp-server/config/example.yaml:/app/config/example.yaml:ro -v /home/jiangren/ocr-mcp-server/models/pp-structure-v3:/models:ro ocr-mcp-server:production-$sha; rm -f \"`$env_file\""
```

Expected: 候选容器健康，SQLite、MinerU、Paddle readiness 全部为 `ready`。

- [ ] **Step 2: 建立候选 SSH 隧道并检查健康**

```powershell
Start-Process -WindowStyle Hidden ssh -ArgumentList "-N","-o","ExitOnForwardFailure=yes","-L","18012:127.0.0.1:18012","ubuntu-server"
Invoke-RestMethod http://127.0.0.1:18012/health/ready
```

Expected: `status=ready`。

- [ ] **Step 3: 重新上传真实样例**

通过候选 REST `/v1/uploads` 上传 `44pages.pdf`，使用新的幂等键，且不输出 API Key 或文件正文。

Expected: 返回新 `file_id`，大小为 `16140648` 字节。

- [ ] **Step 4: 通过候选 MCP 创建并监控任务**

调用 `/mcp` 的 `parse_documents`，再循环调用 `get_task_status`。

Expected:

```text
status=completed 或 completed_with_errors 中失败数为 0
completed_files=1
failed_files=0
artifacts=1
```

- [ ] **Step 5: 下载并校验结果包**

下载产物到：

```text
C:\Users\jiang_ren_1291992796\Downloads\测试数据\ocr-results\44pages-result-$shortSha.zip
```

校验 ZIP 必须包含：

```text
final.md
content_list_v2.json
original_content_list_v2.json
secondary_ocr_audit.json
artifact_manifest.json
```

Expected: ZIP 可打开、必要条目齐全、SHA-256 已记录。

---

### Task 6: 固定镜像版本并记录交付状态

**Files:**
- Create: `deployments/ocr-mcp-server/$sha.env`

**Interfaces:**
- Consumes: 候选镜像 ID、健康检查、真实回归和产物 SHA-256。
- Produces: 可追溯的固定镜像版本与部署记录。

- [ ] **Step 1: 固定候选镜像标签**

仅在 Task 5 全部通过后，将候选镜像标记为 Git SHA 固定版本，不覆盖未验证版本。

- [ ] **Step 2: 写入内容无关的部署记录**

记录源码 SHA、镜像 ID、构建时间、健康状态、测试数量、真实回归批次状态和产物数量；不得记录 API Key、OCR 正文或业务文件内容。

- [ ] **Step 3: 提交部署记录**

```powershell
git add -- "deployments/ocr-mcp-server/$sha.env"
git commit -m "chore: 记录 V2 列表修复部署验证"
```

- [ ] **Step 4: 最终验证**

```powershell
git status --short --branch
git log -3 --oneline
```

Expected: 工作树干净，规格、代码/测试和部署记录提交边界清晰。
