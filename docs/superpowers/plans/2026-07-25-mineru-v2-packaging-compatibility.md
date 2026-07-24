# MinerU V2 产物打包兼容性修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 修复 MinerU V2 列表和安全转义百分数导致的大文档产物打包失败，同时保持现有结构化内容安全边界。

**架构：** 在结构化 LaTeX 校验边界区分转义与裸百分号；在 Markdown 渲染边界增加一个仅处理 MinerU V2 `text_list` 的小型适配函数，并复用现有行内 span 渲染。原始 JSON、合并逻辑和 OCR 编排保持不变。

**技术栈：** Python 3.11、pytest、FastAPI 服务现有 Docker Compose 部署。

## 全局约束

- 只允许已转义的 `\%`，裸 `%` 必须继续被拒绝。
- 仅支持已确认的 V2 `text_list/list_items/item_content` 契约。
- 不记录、提交或输出真实 PDF 的业务正文。
- 不提交现有未跟踪的 `input_preprocessing.py`、`test_input_preprocessing.py` 和 `uv.lock`。

---

### 任务一：允许安全的 LaTeX 转义百分号

**文件：**
- 修改：`src/ocr_mcp_server/services/structured_content.py:304-307`
- 测试：`tests/test_structured_content_validation.py`

**接口：**
- 使用：`validate_formula_latex(value: object, limits: StructuredContentLimits) -> str`
- 产出：校验器接受 `r"12.5\%"`，拒绝 `"12.5%"`

- [ ] **步骤一：写失败测试**

```python
def test_formula_accepts_escaped_percent(limits) -> None:
    assert validate_formula_latex(r"12.5\%", limits) == r"12.5\%"


def test_formula_rejects_unescaped_percent(limits) -> None:
    with pytest.raises(StructuredContentInvalid):
        validate_formula_latex("12.5%", limits)
```

- [ ] **步骤二：验证红灯**

运行：`python -m pytest tests/test_structured_content_validation.py -q`

预期：`test_formula_accepts_escaped_percent` 因当前实现拒绝所有 `%` 而失败；裸百分号测试通过。

- [ ] **步骤三：最小实现**

将百分号判断改为逐位置检查，仅在 `_is_escaped(selected, index)` 为假时拒绝；危险命令、`^^` 和其他规则保持不变。

- [ ] **步骤四：验证绿灯**

运行：`python -m pytest tests/test_structured_content_validation.py -q`

预期：全部通过。

### 任务二：渲染 MinerU V2 文本列表

**文件：**
- 修改：`src/ocr_mcp_server/services/artifacts.py:187-216,288-292`
- 测试：`tests/test_artifacts.py`

**接口：**
- 使用：`_typed_inline_markdown(content, names, limits) -> tuple[str | None, bool]`
- 产出：`_typed_v2_list_markdown(content, limits) -> tuple[str | None, bool]`

- [ ] **步骤一：写失败测试**

```python
def test_render_markdown_supports_mineru_v2_text_list() -> None:
    manifest = [[{
        "type": "list",
        "content": {
            "list_type": "text_list",
            "list_items": [
                {"item_type": "text", "item_content": [{"type": "text", "content": "第一项"}]},
                {
                    "item_type": "text",
                    "item_content": [
                        {"type": "text", "content": "比例 "},
                        {"type": "equation_inline", "content": r"12.5\%"},
                    ],
                },
            ],
        },
    }]]

    rendered = render_markdown(manifest, image_names={}, max_bytes=10_000)

    assert rendered.content.decode("utf-8") == "- 第一项\n- 比例 $12.5\\%$\n"
```

- [ ] **步骤二：验证红灯**

运行：`python -m pytest tests/test_artifacts.py::test_render_markdown_supports_mineru_v2_text_list -q`

预期：抛出 `ArtifactFailure`，错误码为 `artifact_input_invalid`。

- [ ] **步骤三：最小实现**

新增 `_typed_v2_list_markdown`：

```python
def _typed_v2_list_markdown(
    content: Mapping[str, object],
    limits: StructuredContentLimits,
) -> tuple[str | None, bool]:
    if content.get("list_type") != "text_list":
        return None, False
    items = content.get("list_items")
    if not isinstance(items, list) or not items:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    rendered_items: list[str] = []
    warned = False
    for item in items:
        if not isinstance(item, Mapping) or item.get("item_type") != "text":
            _fail(ArtifactErrorCode.INVALID_INPUT)
        value, item_warning = _typed_inline_markdown(
            item, ("item_content",), limits
        )
        if value is None:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        rendered_items.append("- " + value)
        warned = warned or item_warning
    return "\n".join(rendered_items), warned
```

列表分支先保留旧 `text/text_content` 支持；旧字段不存在时调用新适配函数。

- [ ] **步骤四：验证绿灯**

运行：`python -m pytest tests/test_artifacts.py::test_render_markdown_supports_mineru_v2_text_list -q`

预期：通过。

### 任务三：全量验证、发布和真实回归

**文件：**
- 不新增生产代码文件
- 仅提交本计划明确列出的规格、计划、测试和实现文件

**接口：**
- 输入：本地 Git 提交、Ubuntu 5090 Docker Compose、`462pages.pdf`
- 产出：健康的新镜像版本和成功的真实 OCR 任务

- [ ] **步骤一：本机目标与全量测试**

运行：

```powershell
python -m pytest tests/test_structured_content_validation.py tests/test_artifacts.py -q
python -m pytest -q
```

预期：两个命令均退出码 0。

- [ ] **步骤二：提交并推送**

只暂存本计划涉及的文件，执行：

```powershell
git add docs/superpowers/specs/2026-07-25-mineru-v2-packaging-compatibility-design.md docs/superpowers/plans/2026-07-25-mineru-v2-packaging-compatibility.md tests/test_structured_content_validation.py tests/test_artifacts.py src/ocr_mcp_server/services/structured_content.py src/ocr_mcp_server/services/artifacts.py
git commit -m "fix: 兼容 MinerU V2 列表与百分数公式"
git push origin feat/repository-skeleton
```

预期：推送成功，未跟踪的 Stage 0 文件仍未进入提交。

- [ ] **步骤三：远程构建和启动**

在 Ubuntu 5090 拉取该提交，使用现有生产 Compose 配置构建 `ocr-production`，启动后检查容器健康状态和 `/health`。

预期：构建退出码 0，生产容器进入 `healthy`。

- [ ] **步骤四：462 页真实回归**

通过现有上传接口导入 `462pages.pdf`，创建解析任务并轮询到终态。只记录任务 ID、阶段、计数、耗时和产物结构，不输出正文。

预期：任务状态为完成；Markdown、JSON、审计和压缩产物均存在且可下载；不再出现 `artifact_input_invalid`。

- [ ] **步骤五：固定镜像版本**

使用通过回归的 Git 短 SHA 作为不可变镜像标签，并记录镜像 ID。

预期：生产 Compose 指向已验证标签，重启后仍健康。

## 执行结果与发布线修正

- TDD 已完成：安全转义百分号测试先失败，最小实现后通过；裸百分号仍被拒绝。
- 最新主线已经包含 MinerU V2 列表渲染、图片文字回填、空图片引用和 60 MiB 上限修复。
- 最终发布分支必须从 `cf8bd14` 之后创建，不使用较早的
  `fix/mineru-empty-image-reference` checkout 作为定型基线。
- 462 页真实回归已经成功完成并下载产物。
- 最终不可变镜像标签以发布分支的实际候选提交为准，部署记录写入
  `deployments/ocr-mcp-server/<git-sha>.env`。
