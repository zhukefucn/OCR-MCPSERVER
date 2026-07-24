# 输入预处理 Stage 0 后续需求交接

## 状态与范围

- 状态：架构方向已由用户确认，等待当前
  `mineru-empty-table-markdown-fallback` 修复完成后进入正式设计评审。
- 本文只记录后续需求，不授权在当前修复提交中实现。
- 后续必须先检查 `ProductionFilePipeline`、方向恢复、页数、进度和结果合并
  的现有数据契约，再形成窄范围接入设计并按 TDD 实施。
- 预处理不得成为独立 MCP Server，也不得包装成生产 Skill。

## 总体架构决策

1. 将预处理整合进 `ProductionFilePipeline`，位于 MinerU 前面，作为 Stage 0
   `preprocessing`。
2. 原始上传文件保持不可变。
3. 派生文件写入：

   ```text
   intermediate/<file_id>/preprocessed/
   ```

4. 正常文件不复制，直接把原始路径交给 MinerU。
5. MCP 继续只暴露粗粒度整体任务能力，不暴露预处理、MinerU 重试或 Paddle
   路由细节。

## Stage 0 输入与行为

### 支持格式

- PDF
- JPG
- PNG

### 长图片分页

1. 先用高宽比做快速筛选。
2. 再按横向留白带寻找分页点。
3. 将长图片转换为多页 PDF。
4. 原始上传保持不变，派生 PDF 写入预处理目录。

### 整份 PDF 侧倒纠正

1. 只低分辨率渲染第一页做文档级方向判断。
2. 复用已经加载的 Paddle `DocImgOrientationClassification`。
3. 方向结果达到高置信度时，把同一确定性旋转无损写入全部 PDF 页，再交给
   MinerU。
4. 低置信度时保留原文件并记录无正文 warning。
5. 正常方向输入不复制。

### 预处理 Manifest

至少记录：

```text
preprocessor_version
classification
source_sha256
output_sha256
source_page_count
prepared_page_count
rotation
split_rows
```

任务重试只有在源文件哈希和 `preprocessor_version` 都一致时才允许复用派生
产物。

## 原始页数与派生页数契约

1. 长图片的 `source_page_count` 为 1，但 `prepared_page_count` 可以大于
   1。
2. MinerU、运行进度和候选收集必须使用 `prepared_page_count`。
3. 原始输入审计继续使用 `source_page_count`。
4. 旧方向恢复流程不得直接用原始 `page_count` 处理派生页面。
5. 后续设计必须显式定义原始页与派生页之间的映射，防止：
   - 进度分母错误；
   - 页码越界；
   - 恢复令牌指向错误页面；
   - 合并结果写回错误页面；
   - 审计记录混淆原始页和派生页。

## MinerU 首次解析后的少量旋转异常页

### 运行假设

Stage 0 纠正整份文档后，大部分页面应为正向，只剩少量页面或局部块存在
`±90°` 或 `180°` 异常。该假设只用于优化，不得成为正确性不变量。

### 路由原则

- 如果 MinerU 失败主要由方向造成，转正后优先重试 MinerU。
- 如果失败主要由表格或公式能力不足，则交给 Paddle。
- 任何 OCR 前都必须显式执行：

  ```text
  方向分类 -> 确定性旋转 -> 识别
  ```

- 不得把旋转处理隐含在 Paddle 黑盒内。
- 必须保留坐标变换信息，用于映射回原页面。

### 局部旋转块

对局部旋转图片、表格或公式：

1. 旋转裁剪区域。
2. 直接调用 Paddle 识别。
3. 不重跑整页 MinerU。

### 整页候选

整页候选的建议阈值：

```text
page_area_coverage >= 0.80
orientation_confidence >= 0.90
```

达到阈值后先显式无损转正，再分类处理：

1. 页面几乎纯表格：
   - 建议 `table_coverage >= 0.75`；
   - 非表格内容很少；
   - 使用 Paddle 表格识别后替换整页或表格结果。
2. 混合版面、正文和表格并存、多个表格、图文混排，或 MinerU 原页为空/
   整页图片：
   - 生成单页 PDF；
   - 最多重跑一次 MinerU；
   - 若仍缺失表格或公式，再把缺失区域交给 Paddle。

### 循环与升级限制

1. 每页最多执行一次 MinerU 方向纠正重试。
2. 禁止 MinerU 与 Paddle 循环调用。
3. 若异常旋转页数量达到：

   ```text
   max(3, ceil(total_pages * 0.15))
   ```

   则视为 Stage 0 可能漏判，返回文档级纠正，不继续逐页重试。

### 合并优先级

1. MinerU 单页重试结果替换原页面。
2. Paddle 只允许替换：
   - 通过有效性校验的表格；
   - 通过有效性校验的公式；
   - 明确识别为纯表格的整页结果。
3. 不得任意混合两个整页结果。

## 待审查原型

主 checkout 中存在尚未提交的原型，仅供后续审查和选择性移植：

```text
D:\codex-workspace\ocrzhengli\ocr-mcp-server\src\ocr_mcp_server\services\input_preprocessing.py
D:\codex-workspace\ocrzhengli\ocr-mcp-server\tests\test_input_preprocessing.py
```

原型接口：

```python
async classify_and_preprocess(...) -> PreprocessedInput
```

已报告的原型验证结果：

- `longpic-1.jpg` 自动切分 12 页，约 697 ms；
- 35 页侧倒 PDF 写入 90° 旋转，约 239 ms；
- 主 checkout 全量 pytest 通过。

这些结果需要在后续正式设计阶段重新验证。不得从另一 worktree 直接覆盖当前
实现，也不得把未提交原型视为已经接受的生产代码。

## 本机真实集成回归样例

以下两个文件位于同一台 Windows 开发机，可在 Stage 0 后续接入时直接读取，
不需要用户重新上传：

### 整份文档侧倒 PDF

```text
C:\Users\jiang_ren_1291992796\Downloads\测试数据\35pages-xz.pdf
```

- 35 页；
- 第一页及整份文档需要顺时针纠正 90°；
- 原 PDF 没有 `/Rotate` 元数据；
- 方向识别不得只依赖 PDF 元数据。

### 单图多页长图

```text
C:\Users\jiang_ren_1291992796\Downloads\测试数据\longpic-1.jpg
```

- 尺寸：`1588 x 26928`；
- 高宽比：`16.96`；
- 当前原型按横向留白自动切分为 12 页 PDF。

两个样例都不得复制进 Git 仓库。集成测试和日志只允许记录：

```text
文件字节数
图片尺寸
源页数与派生页数
SHA-256
分类结果
旋转角度
耗时
结果结构
```

不得记录、打印或持久化业务正文与识别文本。

## 后续设计必须回答的问题

1. `source_page_count`、`prepared_page_count` 和页映射的领域模型与持久化位置。
2. `preprocessing` 阶段的进度权重、状态恢复和失败错误码。
3. 预处理 manifest 的原子发布、哈希绑定、重试复用和 24 小时保留策略。
4. 方向恢复令牌如何表达原始页与派生页，避免旧接口误用。
5. 文档级纠正与逐页 MinerU 重试之间的升级状态机。
6. 坐标变换如何贯穿候选收集、Paddle 识别、结果合并和审计。
7. 单页 MinerU 重试结果与 Paddle 局部替换的确定性合并顺序。
8. 日志和指标如何保持无正文、无识别文本。
9. 正常文件零复制、派生文件安全写入和清理的边界。
10. 如何用真实长图、整份侧倒 PDF、少量异常页和混合版面构建分层回归测试。

## 后续验收方向

- 正常 PDF/JPG/PNG 保持零复制路径。
- 长图正确生成派生多页 PDF，并区分原始页数与派生页数。
- 高置信度整份侧倒 PDF 在 MinerU 前完成确定性纠正。
- 低置信度方向不改写输入，只产生无正文 warning。
- 重试仅在源哈希和预处理版本一致时复用。
- 少量异常页按一次 MinerU 重试或 Paddle 局部识别路由，不产生循环。
- 达到异常页阈值时升级为文档级纠正。
- 合并、恢复、进度和审计全部使用明确的页数与坐标契约。
- REST/MCP 仍保持当前粗粒度接口。
