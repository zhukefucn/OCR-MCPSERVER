# MinerU V2 标题与脚注渲染修复设计

## 背景

`report-002-xz.pdf` 的 MinerU 原始 Markdown 首行包含“资产负债表”，但最终产物
`final.md` 缺失该标题。真实产物核对表明：

- 标题仍存在于 MinerU V2 节点的 `table_caption` 字段中；
- 合并前后的 V2 JSON 内容一致，本次任务没有发生节点替换；
- 最终 Markdown 渲染器对表格只读取 `html`，没有渲染
  `table_caption` 和 `table_footnote`；
- 同一渲染器也没有处理图片节点的 `image_caption` 和
  `image_footnote`。

因此根因是 V2 Markdown 渲染契约不完整，不是 MinerU 识别失败，也不是合并器
删除了标题。

## 目标

完整保留 MinerU V2 中与表格、图片关联的标题和脚注，并继续保证最终 Markdown
仅由经过审计的 V2 JSON 确定性生成。

## 设计

### 渲染顺序

表格节点按以下顺序生成 Markdown：

1. `table_caption`
2. 经安全校验的 `html`
3. `table_footnote`

图片节点按以下顺序生成 Markdown：

1. `image_caption`
2. 原图片引用
3. `image_footnote`

标题、主体和脚注之间使用 Markdown 空行分隔。空的标题或脚注不产生额外内容。

### V2 行内内容

标题和脚注必须是 MinerU V2 行内 span 列表。支持：

- `text`
- `phonetic`
- `equation_inline`

文本执行 HTML 与 Markdown 转义；行内公式继续使用现有 LaTeX 安全校验。未知
span 不输出正文并记录现有的“不支持节点”警告；结构损坏、字段类型错误或危险
公式按 `artifact_input_invalid` 拒绝。

MinerU V2 会把多个标题块展平成同一个 span 列表，块边界不再可恢复。为了避免
把两个独立标题错误拼接成一个字符串，每个可渲染 span 独立成行。该策略会保留
全部文字，且与真实样例中 MinerU 原始 Markdown 的多行标题顺序一致。

### 安全与审计

- 不从 `mineru/original.md` 复制业务正文；
- 不对 Markdown 做字符串查找替换；
- `final.md` 仍由合并后的 V2 JSON 确定性重建；
- 不改变候选收集、Paddle 路由、节点替换和审计记录；
- 不把标题、脚注或 OCR 正文写入服务日志。

## 测试

先添加失败测试，再实现最小修复：

1. 真实 MinerU V2 表格结构能够输出标题、HTML 和脚注；
2. 图片标题、图片引用和图片脚注顺序正确；
3. 行内文本正确转义，行内公式继续校验；
4. 空标题、空脚注不产生多余空行；
5. 损坏的 caption/footnote 结构被安全拒绝；
6. 现有表格、图片、产物确定性和审计重建测试保持通过；
7. 在 Ubuntu 候选镜像中重新解析 `report-002-xz.pdf`，验证
   `final.md` 包含首个表格标题，且标题顺序位于表格 HTML 之前。

## 非目标

- 本修复不改变 Paddle 对普通文字图片的识别与替换策略；
- 不修改 MinerU 输出格式；
- 不改变 MCP 或 REST API；
- 不调整任务状态、重试、文件保留和部署配置。

普通文字图片的识别回填作为独立需求设计和提交。
