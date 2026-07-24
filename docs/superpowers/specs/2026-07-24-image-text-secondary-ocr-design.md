# 图片文字二次识别与 Markdown 回填设计

## 背景

现有生产流程会把 MinerU 导出的每个图片候选送给 PP-StructureV3，但结果归一化
只读取表格和公式：

- 单一有效表格返回结构化 HTML；
- 单一有效公式返回 LaTeX；
- 其他结果不读取 `overall_ocr_res`，统一归为 `OTHER` 并保留原图片。

因此，当 MinerU 把表名、表头、单位、日期或正文区域导出为普通图片时，Paddle
即使已经识别出文字，最终 `final.md` 也无法获得这些内容。

本设计把原规则“只有表格和公式允许替换”扩展为“表格、公式和可信普通文字均可
进入最终 Markdown；纯视觉图片保持原样”。

## 目标

1. 图片主要承载文字时，在 `final.md` 中用可信 OCR 文字代替图片引用。
2. 图片同时包含重要视觉信息和充分文字时，保留图片，并在其后附加可信 OCR
   文字。
3. logo、印章、照片、签名、图标、图表和示意图等纯视觉或仅含少量装饰文字的
   图片保持原样。
4. 优先避免表名、表头、单位、日期和正文丢失；低置信度结果不得覆盖原图。
5. 继续由服务端固定编排，MCP、REST 和 agent 不增加引擎或阈值选择参数。

## 总体流程

每个 MinerU 图片候选仍只执行一次固定的 PP-StructureV3 解析。归一化层同时读取：

- `doc_preprocessor_res.angle`
- `layout_det_res.boxes`
- `table_res_list`
- `formula_res_list`
- `overall_ocr_res.rec_texts`
- `overall_ocr_res.rec_scores`
- `overall_ocr_res.rec_boxes`

结果按以下优先级路由，禁止同一候选在多种结果之间循环：

1. 单一有效表格：沿用现有表格替换。
2. 单一有效公式：沿用现有公式替换。
3. 表格或公式证据存在但结构化结果不完整，同时存在可信普通 OCR：保留原图并
   生成带 warning 的 `IMAGE_WITH_TEXT` 降级结果。
4. 文字占主导：生成 `TEXT` 结果。
5. 视觉内容与充分文字并存：生成 `IMAGE_WITH_TEXT` 结果。
6. 纯视觉内容：生成 `OTHER` 结果并保留图片。
7. 证据冲突、低置信度或结构损坏：生成 `UNCERTAIN` 或 `INVALID`，保留图片并
   记录 warning。

结构化结果不完整时不得用普通文字替换图片，以免把表格或公式线性化后丢失结构；
但可以在保留原图的前提下附加可信 OCR，避免表名、表头或单位等关键信息完全
不可检索。

## 文字证据与分类

### OCR 文字清洗

仅接受字段长度一致、坐标有效、置信度有限且位于 `[0, 1]` 的 PP-StructureV3
结果。逐行处理时：

- 去掉首尾空白；
- 丢弃空行；
- 丢弃低于 `text_recognition_threshold` 的行；
- 保持 Paddle 给出的阅读顺序；
- 不在日志中输出识别文本；
- 对行数、字符数和 UTF-8 字节数设置硬上限。

默认 `text_recognition_threshold` 与现有分类阈值一致，均为 `0.8`。部署配置可调，
但不通过 MCP、REST 或 agent 暴露。

### 版面标签

固定使用当前 PP-DocLayout 模型的标签分组：

- 文字类：`paragraph_title`、`text`、`number`、`abstract`、`content`、
  `figure_title`、`reference`、`doc_title`、`footnote`、`header`、
  `algorithm`、`footer`、`formula_number`、`aside_text`、
  `reference_content`；
- 结构化类：`table`、`formula`；
- 视觉类：`image`、`seal`、`chart`。

未知标签不得自动归入文字类。

### 判定规则

默认规则如下：

- `TEXT`
  - 至少存在一个高置信度文字类版面框；
  - 不存在高置信度视觉类、表格或公式框；
  - 至少保留 1 行、4 个非空白字符。
- `IMAGE_WITH_TEXT`
  - 存在高置信度视觉类框；
  - 同时存在可信 OCR；
  - 至少保留 2 行或 8 个非空白字符。
- `OTHER`
  - 存在高置信度视觉类框，但普通文字证据不足；
  - 或没有可信 OCR，且不存在表格、公式冲突。
- `UNCERTAIN`
  - 有 OCR 文字但没有足够的版面证据；
  - 文字和视觉证据冲突且不满足混合图片门槛；
  - 只有低置信度结果。

单行短文字不会仅凭 OCR 自动把 logo 或印章改成正文。另一方面，明确被版面模型
识别为标题或正文的“资产负债表”等短表名，只需达到 4 个非空白字符即可进入
`TEXT`，以避免重要表头丢失。

## 领域契约

### 新增结果类型

`SecondaryResultKind` 新增：

- `TEXT`
- `IMAGE_WITH_TEXT`

`SecondaryContentFormat` 新增：

- `PLAIN_TEXT`

两类有效结果必须携带非空纯文本和 `PLAIN_TEXT` 格式。`OTHER` 继续禁止携带可替换
内容。领域对象必须拒绝不匹配的 kind、format、state 组合。

### 合并后的 V2 节点

为保持候选引用、JSON Pointer 和审计重建稳定，普通文字结果不把一个图片节点
拆成多个节点，也不把节点强制转换为 MinerU 段落节点。合并器复制原图片节点，
并只增加经过验证的服务端字段：

```json
{
  "secondary_text": [
    {"type": "text", "content": "第一行"},
    {"type": "text", "content": "第二行"}
  ],
  "secondary_text_mode": "replace_image"
}
```

混合图片使用：

```json
{
  "secondary_text_mode": "append_after_image"
}
```

原有 `image_source`、`image_caption` 和 `image_footnote` 保持不变。图片仍进入产物包，
即使 `final.md` 中由文字替代，以便审计和人工复核。

### Markdown 渲染

结合已审核的标题与脚注修复，图片节点按以下顺序输出：

- `replace_image`：
  1. 图片标题
  2. OCR 文字
  3. 图片脚注
- `append_after_image`：
  1. 图片标题
  2. 原图片引用
  3. OCR 文字
  4. 图片脚注
- 没有二次文字：
  1. 图片标题
  2. 原图片引用
  3. 图片脚注

OCR 文字以纯文本进入结构化字段，最终统一执行 HTML 与 Markdown 转义。不得让
Paddle 返回的文字直接成为可执行 HTML、链接或 Markdown 指令。

## 审计与可观测性

新增合并原因：

- `replaced_text_image`
- `augmented_image_text`
- `augmented_unstructured_fallback`

这三类节点变更都记录为 `REPLACED`，并进入现有可重建审计快照。SQLite 中的
长期审计元数据仍只保存哈希、分类、角度、置信度、引擎、版本和决策，不保存 OCR
正文。

日志和错误消息不得包含：

- OCR 文本；
- 图片标题或脚注；
- 文件名和业务路径；
- Paddle 原始响应正文。

允许记录候选哈希、文本行数、非空白字符数、分类、置信度和耗时。

## 失败与降级

- OCR 结果缺字段、长度不一致、坐标非法或超过限制：标为 `INVALID`，保留图片。
- 置信度不足或分类冲突：标为 `UNCERTAIN`，保留图片并产生 warning。
- 安全文本校验失败：不得回填，保留图片。
- 已有有效表格或公式节点仍遵循 `ALREADY_STRUCTURED`，普通文字结果不得覆盖。
- 每个候选只使用一次 PP-StructureV3 输出，不增加重试环。

## 测试

全部修改按 TDD 实施。

### 单元测试

1. PP-StructureV3 普通 OCR 字段的严格解析、顺序保持和低分行过滤；
2. `TEXT`、`IMAGE_WITH_TEXT`、`OTHER` 和 `UNCERTAIN` 的边界；
3. 短但明确的文字类表名能够进入 `TEXT`；
4. logo、印章和单行短装饰文字保持 `OTHER`；
5. 图文混合候选保留图片并附加 OCR；
6. 表格或公式结构化失败时不得用文字替换图片，但可信文字可在原图后附加；
7. 长度不一致、NaN、非法坐标、超限内容失败关闭；
8. 领域枚举、格式和状态组合校验；
9. 合并审计能够确定性重建新增字段；
10. Markdown 对 OCR 文本转义，并按标题、图片、文字、脚注顺序输出；
11. 原有表格、公式、纯图片和未知节点行为不回归。

### 本机集成测试

- 使用构造的 PP-StructureV3 JSON 响应覆盖纯文字、图文混合和纯视觉三类；
- 运行产物、生产流水线和网关相关测试；
- 运行完整测试集。

### Ubuntu 真实验证

1. 构建独立候选镜像和独立数据卷，不覆盖当前生产容器；
2. 用 `report-002-xz.pdf` 验证表格标题进入 `final.md`，第 3 页的印章和签名图片
   不被误替换成正文；
3. 从现有真实测试集中选取至少一个包含图片文字的样例，验证重要文字进入
   `final.md`；
4. 若现有样例没有稳定的正例，使用不含客户业务内容的专用合成 PDF 做正例，
   真实样例只做负例；
5. 检查产物结构、审计记录和 warning，不在部署日志中打印正文；
6. 验证通过后才固定镜像版本。

## 非目标

- 不引入 PaddleOCR-VL 到默认生产路径；
- 不让 agent 选择引擎、阈值或输出模式；
- 不对图片进行生成式描述；
- 不保证恢复图表、照片或示意图的视觉语义；
- 不改变 MCP 和 REST 的粗粒度接口；
- 不在本需求中实现整页 MinerU 方向纠正重试。
