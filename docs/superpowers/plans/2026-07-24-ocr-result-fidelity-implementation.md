# OCR 结果完整性修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 MinerU V2 列表、表格/图片标题与脚注的 Markdown 渲染，并把 PP-StructureV3 识别出的可信普通图片文字安全回填到最终产物。

**Architecture:** 最终 Markdown 继续只由经过审计的 V2 JSON 确定性生成。先补齐 V2 渲染器，再扩展二次 OCR 领域契约和 PP-StructureV3 归一化，最后由合并器在原图片节点中写入受控的 `secondary_text` 字段，产物层验证审计后决定替换图片或在图片后附加文字。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、PP-StructureV3、pytest、SQLite、本地文件系统、Docker Compose、NVIDIA GPU。

## Global Constraints

- MinerU 主流程固定使用 `vlm-http-client`，本计划不改变 MinerU 推理路由。
- 二次检测固定使用 `pp_structure_v3`，不引入运行时引擎切换，不把阈值暴露给 agent。
- MCP 仍只暴露 `parse_documents`、`get_task_status`、`reparse_with_page_orientation`。
- REST API 继续使用现有 FastAPI 粗粒度入口，不增加底层编排参数。
- 支持 PDF/PNG/JPG/JPEG；单文件默认最大 60 MiB；单文件最多 500 页；单批最多 20 个文件；批次上限 1 GiB。
- 不引入 Redis、PostgreSQL 或外部消息队列。
- 日志不得包含业务正文、OCR 文本、标题、脚注、客户文件名或业务路径。
- 原始上传保持不可变；发布结果必须可以从原始 V2 快照和审计记录确定性重建。
- 纯视觉图片保持原图；低置信度、结构损坏和证据冲突必须失败关闭。
- 表格或公式结构化失败时不得用普通文字替换图片，但可信文字可以在保留原图的前提下附加。
- 所有代码修改先在本机 Python 3.11 环境通过测试，再同步 Ubuntu、构建候选镜像、立即启动验证，验证通过后才固定镜像版本。
- 本计划取代 `2026-07-24-mineru-v2-list-artifact-rendering.md` 的执行部分；原计划和三个设计规格继续作为审计资料保留。

## 本机测试命令

PowerShell 中统一使用已安装的 uv 可执行文件，避免系统 Python 占位程序和 Python 3.12：

```powershell
$uv = 'C:\Users\jiang_ren_1291992796\.workbuddy\binaries\uv\uv.exe'
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest -q
```

---

### Task 1: 支持 MinerU V2 列表节点

**Files:**
- Modify: `tests/test_artifacts.py`
- Modify: `src/ocr_mcp_server/services/artifacts.py:187-216`
- Modify: `src/ocr_mcp_server/services/artifacts.py:288-292`

**Interfaces:**
- Consumes: `_typed_inline_markdown(content, names, limits) -> tuple[str | None, bool]`
- Produces: `_typed_v2_list_markdown(content, limits) -> tuple[str | None, bool]`

- [ ] **Step 1: 添加真实 V2 列表和损坏结构测试**

在 `test_markdown_renderer_accepts_mineru_v2_paragraph_spans` 后添加：

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
                                {"type": "text", "content": "second"}
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
                                {"type": "text", "content": "reference"}
                            ],
                        }
                    ],
                },
            },
        ]],
        image_names={},
        max_bytes=1_000,
    )

    assert rendered.content == b"- A&amp;B$x_i$\n- second\n\n- reference\n"
    assert rendered.warning_codes == ()


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
def test_markdown_renderer_rejects_malformed_mineru_v2_list(content) -> None:
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(
            [[{"type": "list", "content": content}]],
            image_names={},
            max_bytes=1_000,
        )

    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert caught.value.__cause__ is None
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -k "mineru_v2_list" -q
```

Expected: FAIL；真实 V2 列表在旧分支中触发 `artifact_input_invalid`。

- [ ] **Step 3: 实现严格的 V2 列表渲染**

在 `_typed_inline_markdown` 后添加：

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
            item,
            ("item_content",),
            limits,
        )
        if value is None:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        lines.append("- " + value)
        warned = warned or item_warning
    return "\n".join(lines), warned
```

把列表分支替换为：

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

如果存在 `list_items` 但结构损坏，必须失败，不能回退到旧字段。

- [ ] **Step 4: 运行 GREEN 和完整产物测试**

Run:

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add -- tests/test_artifacts.py src/ocr_mcp_server/services/artifacts.py
git commit -m "fix: 支持 MinerU V2 列表产物渲染"
```

---

### Task 2: 渲染表格和图片标题、脚注

**Files:**
- Modify: `tests/test_artifacts.py`
- Modify: `src/ocr_mcp_server/services/artifacts.py:187-216`
- Modify: `src/ocr_mcp_server/services/artifacts.py:298-309`

**Interfaces:**
- Consumes: `_typed_inline_markdown` 和 Task 1 完成后的 `render_markdown`
- Produces: `_typed_v2_auxiliary_markdown(content, name, limits) -> tuple[str | None, bool]`

- [ ] **Step 1: 添加标题、脚注顺序和失败关闭测试**

```python
def test_markdown_renderer_preserves_mineru_table_and_image_captions() -> None:
    rendered = render_markdown(
        [[
            {
                "type": "table",
                "content": {
                    "table_caption": [
                        {"type": "text", "content": "Balance & Sheet"},
                        {"type": "text", "content": "page 1"},
                    ],
                    "html": "<table><tr><td>x</td></tr></table>",
                    "table_footnote": [
                        {"type": "equation_inline", "content": "x_i"}
                    ],
                },
            },
            {
                "type": "image",
                "content": {
                    "image_caption": [
                        {"type": "text", "content": "Figure"}
                    ],
                    "image_source": {"path": "images/a.png"},
                    "image_footnote": [
                        {"type": "text", "content": "source"}
                    ],
                },
            },
        ]],
        image_names={"images/a.png": "images/000000.png"},
        max_bytes=10_000,
    )

    assert rendered.content == (
        "Balance &amp; Sheet\npage 1\n\n"
        "<table><tr><td>x</td></tr></table>\n\n"
        "$x_i$\n\n"
        "Figure\n\n"
        "![](images/000000.png)\n\n"
        "source\n"
    ).encode()
    assert rendered.warning_codes == ()


@pytest.mark.parametrize(
    ("node_type", "field", "value"),
    [
        ("table", "table_caption", "not-a-list"),
        ("table", "table_footnote", [1]),
        ("image", "image_caption", [{"type": "text", "content": 1}]),
        ("image", "image_footnote", [{"type": "equation_inline", "content": r"\input{x}"}]),
    ],
)
def test_markdown_renderer_rejects_malformed_caption_or_footnote(
    node_type,
    field,
    value,
) -> None:
    content = (
        {"html": "<table><tr><td>x</td></tr></table>"}
        if node_type == "table"
        else {"image_source": {"path": "images/a.png"}}
    )
    content[field] = value
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(
            [[{"type": node_type, "content": content}]],
            image_names={"images/a.png": "images/000000.png"},
            max_bytes=10_000,
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert caught.value.__cause__ is None
```

同时添加空列表测试，断言空 caption/footnote 不产生额外空行。

- [ ] **Step 2: 运行 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -k "caption or footnote" -q
```

Expected: FAIL；旧渲染器只输出表格 HTML 或图片引用。

- [ ] **Step 3: 实现辅助 span 的逐行渲染**

```python
def _typed_v2_auxiliary_markdown(
    content: Mapping[str, object],
    name: str,
    limits: StructuredContentLimits,
) -> tuple[str | None, bool]:
    if name not in content:
        return None, False
    spans = content.get(name)
    if not isinstance(spans, list):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if not spans:
        return None, False
    lines: list[str] = []
    warned = False
    for span in spans:
        if not isinstance(span, Mapping):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        span_type = span.get("type")
        span_content = span.get("content")
        if not isinstance(span_type, str) or not isinstance(span_content, str):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        if span_type in {"text", "phonetic"}:
            lines.append(_markdown_escape(span_content))
        elif span_type == "equation_inline":
            lines.append(f"${validate_formula_latex(span_content, limits)}$")
        else:
            warned = True
    return ("\n".join(lines) if lines else None), warned
```

表格和图片分支分别建立 `parts`，过滤 `None` 后用 `"\n\n".join(parts)` 生成单个
block。标题在主体前，脚注在主体后。必须继续执行现有图片路径和 HTML 安全校验。

- [ ] **Step 4: 运行 GREEN、产物测试和真实结构离线回放**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -q
```

Expected: PASS。

读取远端 `report-002-xz` 的发布 V2 JSON，在本机或容器内调用新渲染器，只断言：

- 第一个 `table_caption` 的文本出现在输出；
- 它位于第一个 `<table>` 之前；
- 不把正文打印到终端或日志。

- [ ] **Step 5: 提交**

```powershell
git add -- tests/test_artifacts.py src/ocr_mcp_server/services/artifacts.py
git commit -m "fix: 保留 MinerU V2 标题与脚注"
```

---

### Task 3: 增加普通图片文字领域契约和部署设置

**Files:**
- Modify: `src/ocr_mcp_server/domain/secondary_ocr.py:36-53`
- Modify: `src/ocr_mcp_server/domain/secondary_ocr.py:197-254`
- Modify: `src/ocr_mcp_server/domain/__init__.py`
- Modify: `src/ocr_mcp_server/settings.py:189-232`
- Modify: `config/example.yaml:70-80`
- Modify: `tests/test_secondary_ocr_domain.py`
- Modify: `tests/test_settings.py`

**Interfaces:**
- Produces: `SecondaryResultKind.TEXT`
- Produces: `SecondaryResultKind.IMAGE_WITH_TEXT`
- Produces: `SecondaryContentFormat.PLAIN_TEXT`
- Produces: `SecondaryTextOrigin`
- Extends: `SecondaryOcrResult.text_origin: SecondaryTextOrigin | None = None`

- [ ] **Step 1: 添加领域契约和设置 RED 测试**

测试必须覆盖：

```python
valid_text = SecondaryOcrResult(
    kind=SecondaryResultKind.TEXT,
    angle=OrthogonalAngle.DEG_0,
    content="Balance Sheet",
    content_format=SecondaryContentFormat.PLAIN_TEXT,
    confidence=0.93,
    engine=SecondaryOCREngine.PP_STRUCTURE_V3,
    model_versions={"pipeline": "PP-StructureV3"},
    state=SecondaryResultState.VALID,
    text_origin=SecondaryTextOrigin.TEXT_DOMINANT,
)
assert valid_text.text_origin is SecondaryTextOrigin.TEXT_DOMINANT
```

并参数化拒绝：

- `TEXT + MIXED_VISUAL`
- `TEXT + None`
- `IMAGE_WITH_TEXT + TEXT_DOMINANT`
- `TABLE/FORMULA/OTHER + 非 None text_origin`
- 非有效状态携带正文或 text_origin
- `PLAIN_TEXT` 与表格、公式 kind 组合

设置测试断言默认值：

```python
assert settings.secondary_ocr.text_recognition_threshold == 0.8
assert settings.secondary_ocr.text_min_characters == 4
assert settings.secondary_ocr.mixed_text_min_lines == 2
assert settings.secondary_ocr.mixed_text_min_characters == 8
assert settings.secondary_ocr.text_max_lines == 2_000
assert settings.secondary_ocr.text_max_characters == 200_000
assert settings.secondary_ocr.text_max_utf8_bytes == 800_000
```

并拒绝布尔值、零值、NaN、阈值大于 1，以及最小值大于最大值。

- [ ] **Step 2: 运行 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_secondary_ocr_domain.py tests/test_settings.py -q
```

Expected: FAIL；新枚举、字段和设置尚不存在。

- [ ] **Step 3: 实现枚举与结果组合校验**

```python
class SecondaryResultKind(StrEnum):
    TABLE = "table"
    FORMULA = "formula"
    TEXT = "text"
    IMAGE_WITH_TEXT = "image_with_text"
    OTHER = "other"
    UNCERTAIN = "uncertain"


class SecondaryContentFormat(StrEnum):
    HTML = "html"
    LATEX = "latex"
    PLAIN_TEXT = "plain_text"


class SecondaryTextOrigin(StrEnum):
    TEXT_DOMINANT = "text_dominant"
    MIXED_VISUAL = "mixed_visual"
    UNSTRUCTURED_FALLBACK = "unstructured_fallback"
```

在 `SecondaryOcrResult` 末尾增加：

```python
text_origin: SecondaryTextOrigin | None = None
```

有效结果的组合规则：

```python
if self.kind is SecondaryResultKind.TEXT:
    if (
        self.content is None
        or self.content_format is not SecondaryContentFormat.PLAIN_TEXT
        or self.text_origin is not SecondaryTextOrigin.TEXT_DOMINANT
    ):
        raise ValueError("valid text results require dominant plain text")
elif self.kind is SecondaryResultKind.IMAGE_WITH_TEXT:
    if (
        self.content is None
        or self.content_format is not SecondaryContentFormat.PLAIN_TEXT
        or self.text_origin not in {
            SecondaryTextOrigin.MIXED_VISUAL,
            SecondaryTextOrigin.UNSTRUCTURED_FALLBACK,
        }
    ):
        raise ValueError("valid mixed results require appendable plain text")
elif self.text_origin is not None:
    raise ValueError("non-text results cannot carry a text origin")
```

非 `VALID` 结果必须是 `UNCERTAIN` kind，且 content、format、origin 全为 `None`。
从 `domain/__init__.py` 导出 `SecondaryTextOrigin`。

- [ ] **Step 4: 实现部署设置**

在 `SecondaryOCRSettings` 添加：

```python
text_recognition_threshold: float = Field(default=0.8, gt=0, le=1)
text_min_characters: int = Field(default=4, ge=1, le=10_000)
mixed_text_min_lines: int = Field(default=2, ge=1, le=10_000)
mixed_text_min_characters: int = Field(default=8, ge=1, le=100_000)
text_max_lines: int = Field(default=2_000, ge=1, le=20_000)
text_max_characters: int = Field(default=200_000, ge=1, le=1_000_000)
text_max_utf8_bytes: int = Field(default=800_000, ge=1, le=4_000_000)
```

把这些数字字段加入布尔值拒绝 validator，并添加 after validator，要求所有最小值
不大于对应最大值。同步 `config/example.yaml`。

- [ ] **Step 5: 运行 GREEN**

运行 Step 2 的命令。

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add -- src/ocr_mcp_server/domain/secondary_ocr.py src/ocr_mcp_server/domain/__init__.py src/ocr_mcp_server/settings.py config/example.yaml tests/test_secondary_ocr_domain.py tests/test_settings.py
git commit -m "feat: 定义普通图片文字识别契约"
```

---

### Task 4: 严格归一化 PP-StructureV3 普通 OCR

**Files:**
- Modify: `src/ocr_mcp_server/infra/pp_structure_v3.py`
- Modify: `tests/test_pp_structure_v3.py`

**Interfaces:**
- Consumes: Task 3 的新枚举和 `SecondaryOCRSettings`
- Produces: `_TextRecognitionPolicy`
- Produces: `_recognized_plain_text(overall_ocr, policy) -> tuple[str | None, int, int, float]`
- Extends: `normalize_pp_structure_v3_result(raw_result: object, *, threshold: float, text_policy: _TextRecognitionPolicy, model_versions: Mapping[str, str]) -> SecondaryOcrResult`

- [ ] **Step 1: 扩展测试响应并添加分类 RED 测试**

把 `_response` 增加 `overall_ocr` 参数，并写入 `overall_ocr_res`。默认值必须是结构
完整的空 OCR 响应，保证已有只测试表格/公式的 fixture 不需要重复样板：

```python
def _response(
    *,
    boxes: list[dict[str, object]],
    tables: list[dict[str, object]] | None = None,
    formulas: list[dict[str, object]] | None = None,
    overall_ocr: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "layout_det_res": {"boxes": boxes},
        "table_res_list": tables or [],
        "formula_res_list": formulas or [],
        "overall_ocr_res": overall_ocr or {
            "rec_texts": [],
            "rec_scores": [],
            "rec_boxes": [],
        },
    }
```

测试以下响应：

1. `doc_title` 0.95 + 一行“Balance Sheet” 0.96 -> `TEXT/TEXT_DOMINANT`
2. `image` 0.94 + 两行可信文字 -> `IMAGE_WITH_TEXT/MIXED_VISUAL`
3. `seal` 0.95 + 一行短文字 -> `OTHER`
4. `table` 0.93、表格结果缺失、可信 OCR -> `IMAGE_WITH_TEXT/UNSTRUCTURED_FALLBACK`
5. 表格结果有效时仍优先 `TABLE`
6. 只有低分文字 -> `OTHER` 或 `UNCERTAIN`，但不得携带正文
7. `rec_texts`、`rec_scores`、`rec_boxes` 长度不一致 -> `INVALID`
8. NaN 分数、非法坐标、超限行数/字符数 -> `INVALID`
9. 未知高分标签不能触发 `TEXT`

可信 OCR fixture：

```python
{
    "rec_texts": ["Balance Sheet", "Unit: CNY"],
    "rec_scores": [0.96, 0.94],
    "rec_boxes": [[1, 2, 100, 20], [1, 25, 100, 45]],
}
```

- [ ] **Step 2: 运行 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_pp_structure_v3.py -q
```

Expected: FAIL；旧 normalizer 忽略 `overall_ocr_res`。

- [ ] **Step 3: 实现固定标签组和策略对象**

```python
@dataclass(frozen=True, slots=True)
class _TextRecognitionPolicy:
    recognition_threshold: float
    min_characters: int
    mixed_min_lines: int
    mixed_min_characters: int
    max_lines: int
    max_characters: int
    max_utf8_bytes: int

    @classmethod
    def from_settings(cls, settings: SecondaryOCRSettings):
        return cls(
            settings.text_recognition_threshold,
            settings.text_min_characters,
            settings.mixed_text_min_lines,
            settings.mixed_text_min_characters,
            settings.text_max_lines,
            settings.text_max_characters,
            settings.text_max_utf8_bytes,
        )
```

固定标签：

```python
_TEXT_LAYOUT_LABELS = frozenset({
    "paragraph_title", "text", "number", "abstract", "content",
    "figure_title", "reference", "doc_title", "footnote", "header",
    "algorithm", "footer", "formula_number", "aside_text",
    "reference_content",
})
_STRUCTURED_LAYOUT_LABELS = frozenset({"table", "formula"})
_VISUAL_LAYOUT_LABELS = frozenset({"image", "seal", "chart"})
```

- [ ] **Step 4: 实现 OCR 字段严格解析**

`_recognized_plain_text` 必须：

- 要求 `overall_ocr_res` 是 Mapping；
- 要求三个列表存在且长度相同；
- 每个文字是字符串；
- 每个分数是有限 `[0, 1]` 数；
- 每个框是四个非负有限数字，满足 `x0 <= x1`、`y0 <= y1`；
- 过滤空行和低分行；
- 逐行 `strip()`，保持顺序，用 `"\n"` 连接；
- 在任何过滤前先执行 raw 行数硬上限；
- 对保留结果执行 `max_lines`、`max_characters`、`max_utf8_bytes` 和严格 UTF-8
  编码检查；
- 返回文本、行数、非空白字符数和保留行最低置信度；
- 任何结构错误抛 `ValueError`，由 normalizer 转为 `INVALID`。

- [ ] **Step 5: 实现确定性分类优先级**

normalizer 的处理顺序固定为：先校验顶层响应和方向；再判断现有单一表格/公式是否
完整有效；完整时直接返回 `TABLE`/`FORMULA`，不要求普通 OCR 字段存在；只有结构化
结果不完整或普通图片路径才解析 `overall_ocr_res`。这样保留现有表格/公式 fixture
兼容性，也避免无关的普通 OCR 字段破坏已验证结构结果。

结构化证据存在但结果不完整时：

```python
high_confidence = [
    (label, score)
    for label, score in evidence
    if score >= threshold
]
text_labels = {
    label for label, _ in high_confidence if label in _TEXT_LAYOUT_LABELS
}
visual_labels = {
    label for label, _ in high_confidence if label in _VISUAL_LAYOUT_LABELS
}
structured_evidence = [
    (label, score)
    for label, score in evidence
    if label in _STRUCTURED_LAYOUT_LABELS
]
structured_confidence = max(
    (score for _, score in structured_evidence),
    default=0.0,
)
other_labels = {
    label
    for label, _ in high_confidence
    if label
    not in (
        _TEXT_LAYOUT_LABELS
        | _VISUAL_LAYOUT_LABELS
        | _STRUCTURED_LAYOUT_LABELS
    )
}

if structured_evidence:
    if plain_text is not None and char_count >= policy.min_characters:
        return _plain_text_result(
            kind=SecondaryResultKind.IMAGE_WITH_TEXT,
            origin=SecondaryTextOrigin.UNSTRUCTURED_FALLBACK,
            angle=angle,
            content=plain_text,
            confidence=min(structured_confidence, text_confidence),
            model_versions=model_versions,
        )
    return _safe_result(
        state=SecondaryResultState.UNCERTAIN,
        angle=angle,
        confidence=structured_confidence,
        model_versions=model_versions,
    )
```

普通图片：

```python
if (
    text_labels
    and not visual_labels
    and plain_text is not None
    and char_count >= policy.min_characters
):
    kind = SecondaryResultKind.TEXT
    origin = SecondaryTextOrigin.TEXT_DOMINANT
elif visual_labels and plain_text is not None and (
    line_count >= policy.mixed_min_lines
    or char_count >= policy.mixed_min_characters
):
    kind = SecondaryResultKind.IMAGE_WITH_TEXT
    origin = SecondaryTextOrigin.MIXED_VISUAL
elif visual_labels:
    kind = SecondaryResultKind.OTHER
elif plain_text is None and other_labels:
    kind = SecondaryResultKind.OTHER
else:
    return _safe_result(
        state=SecondaryResultState.UNCERTAIN,
        angle=angle,
        confidence=max((score for _, score in evidence), default=0.0),
        model_versions=model_versions,
    )

if kind is SecondaryResultKind.OTHER:
    return SecondaryOcrResult(
        kind=SecondaryResultKind.OTHER,
        angle=angle,
        content=None,
        content_format=None,
        confidence=max((score for _, score in high_confidence), default=0.0),
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions=model_versions,
        state=SecondaryResultState.VALID,
        text_origin=None,
    )
assert plain_text is not None
return _plain_text_result(
    kind=kind,
    origin=origin,
    angle=angle,
    content=plain_text,
    confidence=min(
        max((score for _, score in high_confidence), default=0.0),
        text_confidence,
    ),
    model_versions=model_versions,
)
```

文字结果的 `confidence` 使用“相关版面框最高分”和“保留 OCR 行最低分”的较小值；
纯视觉结果继续使用版面框最高分。未知标签不能产生 `TEXT`，但在没有可信文字时可
作为 `OTHER` 保留原图。

文字结果统一通过：

```python
def _plain_text_result(
    *,
    kind: SecondaryResultKind,
    origin: SecondaryTextOrigin,
    angle: OrthogonalAngle,
    content: str,
    confidence: float,
    model_versions: Mapping[str, str],
) -> SecondaryOcrResult:
    return SecondaryOcrResult(
        kind=kind,
        angle=angle,
        content=content,
        content_format=SecondaryContentFormat.PLAIN_TEXT,
        confidence=confidence,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions=model_versions,
        state=SecondaryResultState.VALID,
        text_origin=origin,
    )
```

`_safe_result` 必须显式设置 `text_origin=None`。Backend 保存
`_TextRecognitionPolicy.from_settings(settings)`，调用 normalizer 时传入，不执行第二次
Paddle 推理。

- [ ] **Step 6: 运行 GREEN 和二次 OCR worker 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_pp_structure_v3.py tests/test_secondary_ocr_worker.py tests/test_observability_faults.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add -- src/ocr_mcp_server/infra/pp_structure_v3.py tests/test_pp_structure_v3.py
git commit -m "feat: 归一化 PP-StructureV3 图片文字"
```

---

### Task 5: 校验纯文本并写入可审计 V2 图片节点

**Files:**
- Modify: `src/ocr_mcp_server/services/structured_content.py`
- Modify: `tests/test_structured_content.py`
- Modify: `src/ocr_mcp_server/domain/merge.py`
- Modify: `src/ocr_mcp_server/services/merge_publication.py`
- Modify: `tests/test_merge_publication.py`

**Interfaces:**
- Produces: `validate_plain_text(value, limits) -> str`
- Produces: `ReplacementReason.REPLACED_TEXT_IMAGE`
- Produces: `ReplacementReason.AUGMENTED_IMAGE_TEXT`
- Produces: `ReplacementReason.AUGMENTED_UNSTRUCTURED_FALLBACK`

- [ ] **Step 1: 添加纯文本安全校验 RED 测试**

测试 `validate_plain_text`：

- 接受 `"Balance Sheet\nUnit: CNY"`；
- 保持单个换行，去掉整体首尾空白；
- 拒绝空字符串、NUL、CR、Tab、Bidi/Cf 字符、代理字符；
- 按字符数和 UTF-8 字节数执行 `StructuredContentLimits`。

- [ ] **Step 2: 运行 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_structured_content.py -q
```

Expected: FAIL；`validate_plain_text` 尚不存在。

- [ ] **Step 3: 实现纯文本校验**

```python
def validate_plain_text(
    value: object,
    limits: StructuredContentLimits,
) -> str:
    if not isinstance(value, str):
        raise StructuredContentInvalid() from None
    selected = value.strip()
    try:
        encoded = selected.encode("utf-8", errors="strict")
    except UnicodeError:
        raise StructuredContentInvalid() from None
    if (
        not selected
        or len(selected) > limits.max_characters
        or len(encoded) > limits.max_utf8_bytes
        or any(
            character != "\n"
            and unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in selected
        )
    ):
        raise StructuredContentInvalid() from None
    return selected
```

运行 Step 2，Expected: PASS。

- [ ] **Step 4: 添加普通文字合并 RED 测试**

扩展 `_ocr` helper，使 `TEXT` 和 `IMAGE_WITH_TEXT` 携带 plain text 与 origin。

测试：

1. `TEXT` 在原图片 content 中写入：

```python
"secondary_text": [
    {"type": "text", "content": "Balance Sheet"},
    {"type": "text", "content": "Unit: CNY"},
],
"secondary_text_mode": "replace_image",
```

2. `IMAGE_WITH_TEXT/MIXED_VISUAL` 使用 `append_after_image`；
3. `IMAGE_WITH_TEXT/UNSTRUCTURED_FALLBACK` 同样使用 `append_after_image`，但审计
   reason 不同；
4. 原 `image_source`、caption、footnote、bbox 和未知安全字段保持；
5. 非 image 原始节点、危险控制字符、超限文本保留原节点并记录
   `INVALID_CONTENT`；
6. 同一图片的多个真实引用全部确定性更新；
7. 原始 V2 文件不可变。
8. MinerU 原始 V2 自带 `secondary_text` 或 `secondary_text_mode` 时按
   `merge_source_manifest_invalid` 拒绝，且不得创建 publication 目录。

- [ ] **Step 5: 运行合并 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_merge_publication.py -k "text_image or image_text or unstructured_fallback" -q
```

Expected: FAIL；合并器尚不支持新 kind。

- [ ] **Step 6: 实现审计原因和节点替换**

在 `ReplacementReason` 增加：

```python
REPLACED_TEXT_IMAGE = "replaced_text_image"
AUGMENTED_IMAGE_TEXT = "augmented_image_text"
AUGMENTED_UNSTRUCTURED_FALLBACK = "augmented_unstructured_fallback"
```

在读取源 manifest 后、执行候选合并前调用：

```python
def _reject_reserved_secondary_text_fields(manifest: list[list[dict]]) -> None:
    for page in manifest:
        for node in page:
            content = node.get("content")
            if isinstance(content, dict) and (
                "secondary_text" in content
                or "secondary_text_mode" in content
            ):
                _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
```

这两个字段只能由本服务的已审计合并步骤产生，不能信任 MinerU 或其他上游直接
提供。

在 `_replace_node` 的表格、公式分支后处理文字：

```python
if (
    recognized.kind in {
        SecondaryResultKind.TEXT,
        SecondaryResultKind.IMAGE_WITH_TEXT,
    }
    and recognized.content_format is SecondaryContentFormat.PLAIN_TEXT
    and node.get("type") == "image"
):
    validated = validate_plain_text(recognized.content, limits)
    lines = [
        {"type": "text", "content": line}
        for line in validated.split("\n")
        if line
    ]
    replacement = deepcopy(node)
    replacement["content"]["secondary_text"] = lines
    if recognized.kind is SecondaryResultKind.TEXT:
        replacement["content"]["secondary_text_mode"] = "replace_image"
        reason = ReplacementReason.REPLACED_TEXT_IMAGE
    else:
        replacement["content"]["secondary_text_mode"] = "append_after_image"
        reason = (
            ReplacementReason.AUGMENTED_UNSTRUCTURED_FALLBACK
            if recognized.text_origin is SecondaryTextOrigin.UNSTRUCTURED_FALLBACK
            else ReplacementReason.AUGMENTED_IMAGE_TEXT
        )
    return replacement, reason
```

在模块导入 `validate_plain_text` 和 `SecondaryTextOrigin`。已有有效 table/formula 的
`ALREADY_STRUCTURED` 判断继续优先于普通文字替换。

- [ ] **Step 7: 运行 GREEN 和完整合并测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_structured_content.py tests/test_merge_publication.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add -- src/ocr_mcp_server/services/structured_content.py tests/test_structured_content.py src/ocr_mcp_server/domain/merge.py src/ocr_mcp_server/services/merge_publication.py tests/test_merge_publication.py
git commit -m "feat: 合并可信图片文字并保留审计"
```

---

### Task 6: 渲染并验证审计后的图片文字

**Files:**
- Modify: `src/ocr_mcp_server/services/artifacts.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: Task 2 的图片 caption/footnote 渲染
- Consumes: Task 5 的 `secondary_text` 和 `secondary_text_mode`
- Extends: `_expected_node_for_audit`

- [ ] **Step 1: 添加 Markdown 和审计 RED 测试**

测试：

1. `replace_image` 输出 caption、OCR 文字、footnote，不输出 Markdown 图片引用；
2. `append_after_image` 输出 caption、图片、OCR 文字、footnote；
3. OCR 文本中的 `<script>`、`[link](...)`、`*`、`&` 被转义；
4. 缺失/未知 mode、空 secondary_text、非 text span、危险公式 span 被拒绝；
5. `REPLACED_TEXT_IMAGE` 只接受 `TEXT`、`replace_image` 和精确节点变更；
6. `AUGMENTED_IMAGE_TEXT` 只接受 `IMAGE_WITH_TEXT` 和 `append_after_image`；
7. `AUGMENTED_UNSTRUCTURED_FALLBACK` 同样只接受
   `IMAGE_WITH_TEXT/append_after_image`；
8. 篡改 OCR 文字、mode、原图片其他字段或审计 kind/reason 后，产物打包必须
   `artifact_input_invalid`；
9. 图片仍包含在 ZIP 中，即使 final.md 不引用它。
10. 原始 V2 快照自带保留字段时，即使调用者同时伪造 retained audit，产物也必须
    `artifact_input_invalid`。

- [ ] **Step 2: 运行 RED 测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -k "secondary_text or text_image or image_text" -q
```

Expected: FAIL。

- [ ] **Step 3: 实现图片二次文字渲染**

添加严格 helper：

```python
def _typed_secondary_text_markdown(
    content: Mapping[str, object],
    limits: StructuredContentLimits,
) -> tuple[str | None, str | None]:
    if "secondary_text" not in content and "secondary_text_mode" not in content:
        return None, None
    spans = content.get("secondary_text")
    mode = content.get("secondary_text_mode")
    if mode not in {"replace_image", "append_after_image"}:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if not isinstance(spans, list) or not spans:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    lines: list[str] = []
    for span in spans:
        if (
            not isinstance(span, Mapping)
            or span.get("type") != "text"
            or not isinstance(span.get("content"), str)
            or not span["content"]
            or span["content"] != span["content"].strip()
            or "\n" in span["content"]
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        lines.append(span["content"])
    validated = validate_plain_text("\n".join(lines), limits)
    return "\n".join(_markdown_escape(line) for line in validated.split("\n")), mode
```

图片分支先安全解析原图片路径，再根据 mode 选择是否把图片引用加入 parts。即使
`replace_image` 不输出图片引用，也不能跳过路径安全校验和 ZIP 图片收集。

- [ ] **Step 4: 扩展审计重建白名单**

在 `_expected_node_for_audit` 中把三个新 reason 作为可替换原因处理。必须从
`original_node` deepcopy，只允许增加精确的：

- `content.secondary_text`
- `content.secondary_text_mode`

校验 kind、mode 和 reason 的对应关系；任何额外字段变化失败。使用
`validate_plain_text` 重新验证把 spans 用换行连接后的文本。

`_reconstruct_audited_final` 在应用任何 audit 前先遍历原始快照，拒绝
`secondary_text` 和 `secondary_text_mode` 保留字段，形成打包层的第二道防线。

- [ ] **Step 5: 运行 GREEN 和完整产物测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add -- src/ocr_mcp_server/services/artifacts.py tests/test_artifacts.py
git commit -m "feat: 渲染并审计图片文字回填"
```

---

### Task 7: 本机相关测试、全量测试和代码审查

**Files:**
- Review only: all files modified by Tasks 1-6

**Interfaces:**
- Verifies the complete local deliverable.

- [ ] **Step 1: 运行相关测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest tests/test_artifacts.py tests/test_structured_content.py tests/test_secondary_ocr_domain.py tests/test_pp_structure_v3.py tests/test_merge_publication.py tests/test_secondary_ocr_worker.py tests/test_production_pipeline.py tests/test_production_gateway.py tests/test_settings.py -q
```

Expected: PASS，无未处理 warning。

- [ ] **Step 2: 运行全量测试**

```powershell
& $uv run --isolated --no-project --python 'C:\Users\jiang_ren_1291992796\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe' --with-editable . --with pytest --with pytest-asyncio --with respx python -m pytest -q
```

Expected: PASS；跳过项只能是仓库原有的环境相关 skip。

- [ ] **Step 3: 检查变更范围和敏感内容**

```powershell
git diff --check
git status --short
git log --oneline --decorate -12
```

确认：

- 没有测试 PDF、业务图片、OCR 正文、API key、PAT 或密码进入 Git；
- 没有触碰主 checkout 的未跟踪 Stage 0 原型；
- 各提交只包含各自任务文件；
- 没有修改 MCP/REST 粗粒度接口。

- [ ] **Step 4: 审查**

按以下不变量逐项人工检查：

- 原始 V2 快照不可变；
- 表格/公式优先级高于普通文字；
- 纯视觉和低置信度结果保留原图片；
- `replace_image` 也保留 ZIP 中的源图片；
- 审计重建只允许白名单字段变化；
- 日志不出现 OCR 文本。

若审查产生代码修改，先补 RED 测试，再单独提交。

---

### Task 8: 推送分支并在 Ubuntu 构建独立候选镜像

**Files:**
- Remote checkout only: `/home/jiangren/ocr-mcp-server`
- Dockerfile: `docker/ocr-gateway-ppstructure.Dockerfile`

**Interfaces:**
- Produces immutable candidate image: `ocr-mcp-server:production-$candidate_sha`
- Produces candidate endpoint on remote port `18012`

- [ ] **Step 1: 推送功能分支**

```powershell
git push -u origin fix/mineru-v2-list-artifact-rendering
git rev-parse HEAD
```

记录完整 commit SHA；不得在命令行、日志或文档中写 PAT。

- [ ] **Step 2: 远端获取精确提交并建立候选 worktree**

通过 `ssh ubuntu-server`：

```bash
cd /home/jiangren/ocr-mcp-server
git fetch origin fix/mineru-v2-list-artifact-rendering
candidate_sha="$(git rev-parse origin/fix/mineru-v2-list-artifact-rendering)"
candidate_dir="/home/jiangren/ocr-mcp-server-candidate-${candidate_sha:0:12}"
git worktree add --detach "$candidate_dir" "$candidate_sha"
```

先检查目标目录不存在；不得覆盖当前生产 checkout。

- [ ] **Step 3: 构建候选镜像**

```bash
cd "$candidate_dir"
docker build \
  --file docker/ocr-gateway-ppstructure.Dockerfile \
  --target ppstructure-gpu \
  --tag "ocr-mcp-server:production-$candidate_sha" \
  .
```

Expected: build exit 0。记录 image ID 和 repo digest，不输出环境变量。

- [ ] **Step 4: 启动独立候选容器**

要求：

- 使用独立 named volume，例如 `ocr-mcp-candidate-${candidate_sha:0:12}` 挂载 `/data`；
- 加入现有 `ocr-mcp-server_default` Docker network，以访问 MinerU 服务；
- 只读挂载候选 checkout 的 `config/example.yaml` 和现有 `/models`；
- 从当前生产容器安全复制环境到权限为 `0600` 的临时 env 文件，命令不得打印
  文件内容；
- 映射远端 `18012:8000`；
- 容器名 `ocr-mcp-server-${candidate_sha:0:12}-candidate`；
- 当前 `ocr-mcp-server-ocr-production-1` 保持运行。

启动后立即删除临时 env 文件。

- [ ] **Step 5: 启动验证**

轮询候选容器 health，最多 120 秒：

```bash
docker inspect --format '{{.State.Health.Status}}' "$candidate_name"
curl --fail --silent --show-error http://127.0.0.1:18012/health
```

Expected: `healthy` 和 HTTP 200。检查容器日志只看错误码、阶段和资源信息，不打印
任何 OCR 正文。

---

### Task 9: Ubuntu 真实 PDF 回归和镜像固定

**Files:**
- Test data, read only:
  - `C:\Users\jiang_ren_1291992796\Downloads\测试数据\report-002-xz.pdf`
  - `C:\Users\jiang_ren_1291992796\Downloads\测试数据\44pages.pdf`
- Create after successful validation:
  - `deployments/ocr-mcp-server/$candidate_sha.env`

**Interfaces:**
- Verifies candidate REST and MCP behavior.
- Produces a pinned deployment record only after success.

- [ ] **Step 1: 建立本地候选 SSH 隧道**

把本机 `127.0.0.1:18012` 转发到 Ubuntu `127.0.0.1:18012`。确认候选 `/health`
可访问，生产 `127.0.0.1:18011` 仍健康。

- [ ] **Step 2: 解析 `report-002-xz.pdf`**

通过候选 FastAPI 上传并提交解析，API key 只从安全环境读取，不写入脚本、Git 或
终端输出。轮询粗粒度任务状态直到终态。

Expected:

- 任务 `completed` 或 `completed_with_warnings`；
- 下载 ZIP 成功；
- `final.md` 首个表格标题存在，且在首个 `<table>` 前；
- 第 3 页的印章、签名等纯视觉图片仍以图片形式保留；
- `secondary_ocr_audit.json` 中没有未经审计的节点变化；
- 服务日志不包含标题或 OCR 正文。

- [ ] **Step 3: 解析 `44pages.pdf`**

Expected:

- 不再在产物校验/打包阶段失败；
- 真实 MinerU `text_list`/`reference_list` 节点正确生成 Markdown；
- ZIP 包包含 final、原始 V2、最终 V2、审计和所有受引用图片；
- 任务进度和状态正常。

- [ ] **Step 4: 验证图片文字正例**

从现有真实测试集中选择一个不需要复制进 Git 的图片文字候选。若没有稳定正例，
在本地生成不含客户信息的专用 PDF，图片中包含：

- 一行表名；
- 一行单位；
- 两行正文。

Expected:

- 文字占主导时 final.md 不输出图片引用，但输出全部可信文字；
- 图文混合时同时输出图片引用和 OCR 文字；
- ZIP 仍包含源图片；
- Markdown 特殊字符被转义；
- 审计 reason 与 kind、mode 一致。

- [ ] **Step 5: 比对 REST 与 MCP**

分别通过 REST 和 MCP 的粗粒度入口提交至少一个小样例，确认：

- 两个入口生成相同任务状态语义；
- agent 不需要选择引擎或编排底层步骤；
- 鉴权失败仍返回安全的 `authentication_failed`；
- 成功响应不泄漏内部路径或 OCR 正文。

- [ ] **Step 6: 固定镜像版本和部署记录**

只有 Steps 2-5 全部通过后，创建：

```text
deployments/ocr-mcp-server/$candidate_sha.env
```

内容只包含：

```dotenv
OCR_IMAGE=ocr-mcp-server:production-$candidate_sha
OCR_IMAGE_ID=$image_id
OCR_COMMIT_SHA=$candidate_sha
OCR_VERIFIED_AT=$verified_at
OCR_VERIFICATION=local-full,remote-health,report-002-xz,44pages,image-text,rest,mcp
```

不得包含 API key、密码、PAT、OCR 正文、文件名哈希映射或业务路径。

- [ ] **Step 7: 提交部署记录**

```powershell
git add -- "deployments/ocr-mcp-server/$candidate_sha.env"
git commit -m "chore: 固定 OCR 结果完整性镜像版本"
git push
```

- [ ] **Step 8: 清理候选资源**

确认固定镜像和部署记录存在后：

- 停止并删除本次候选容器；
- 删除本次独立候选数据卷；
- 移除本次远端 detached worktree；
- 保留固定镜像；
- 不删除当前生产数据、生产容器或其他历史候选资源，除非另行获得明确授权。

---

## 完成标准

- 本机全量 pytest 通过；
- `report-002-xz` 的表格标题、脚注不再从 final.md 丢失；
- `44pages.pdf` 的 MinerU V2 列表不会导致产物打包失败；
- 可信图片文字按 `TEXT` 或 `IMAGE_WITH_TEXT` 进入 final.md；
- 纯视觉、低置信度和损坏结果保留原图；
- 最终 V2 能由原始 V2 和审计记录精确重建；
- REST 与 MCP 都通过候选环境验证；
- Ubuntu 候选镜像健康并完成真实 PDF 回归；
- 只有验证通过的 commit SHA 被写入部署记录和固定镜像标签。
