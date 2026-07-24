# MinerU V2 列表节点产物渲染修复设计

## 背景

真实样例 `44pages.pdf` 已连续两次稳定复现失败。MinerU 主解析、Paddle 二次检测和结果合并均已完成，失败发生在 `ArtifactBundler` 生成 `final.md` 时。

精确错误为 `artifact_input_invalid`，触发位置是 `render_markdown()` 对列表节点的处理分支。真实 MinerU V2 列表节点使用以下结构：

```text
content.list_type
content.list_items[].item_type
content.list_items[].item_content[]
```

当前代码却只读取旧式扁平字段 `content.text` 或 `content.text_content`。现有单元测试也只构造了扁平列表，因此没有覆盖真实 MinerU V2 数据契约。

## 目标

1. 严格支持 MinerU V2 的 `list`、`text_list` 和 `reference_list` 节点。
2. 将每个列表项的 `item_content` 内联 span 安全渲染为 Markdown。
3. 校验 MinerU 当前定义的 `text_list` 与 `reference_list` 两种列表语义，并输出稳定的 Markdown 项目列表。
4. 保持现有安全边界：未知、畸形、超限或不一致的列表结构必须返回内容无关的安全错误，不能容错拼接未经验证的值。
5. 保留现有扁平列表输入兼容性，避免破坏已经发布的调用方和历史测试。
6. 用真实样例的内容无关结构特征建立回归覆盖，并在 Ubuntu 候选镜像上重新完成端到端解析。

## 非目标

- 不修改 MinerU、Paddle 或任务状态机。
- 不改变表格、公式、图片替换规则。
- 不实现 Stage 0 输入预处理。
- 不扩大 MCP 工具集合。
- 不把业务正文、识别文本或列表内容写入日志。
- 本修复不改变对外错误响应格式；内部诊断改进另行评审。

## 方案选择

### 方案一：严格支持 V2，同时保留扁平格式兼容（采用）

优先识别 `list_items` V2 结构；不存在 `list_items` 时，继续使用现有 `text` / `text_content` 分支。

优点：

- 与当前 MinerU 输出契约一致。
- 不破坏已有兼容输入。
- 变更局限在 Markdown 渲染器和测试。

风险：

- 必须严格验证嵌套 span，避免将任意对象转换为字符串。

### 方案二：只支持 V2，删除扁平格式

契约更单一，但会破坏现有测试和潜在历史产物，MVP 当前没有必要承担此兼容性风险。

### 方案三：遇到列表节点时跳过并记录警告

可以避免任务失败，但会静默丢失有效正文，不符合银行文档结果完整性要求，因此不采用。

## 详细设计

### 列表节点路由

`render_markdown()` 遇到列表类型时：

1. 如果 `content.list_items` 存在，按 V2 契约处理。
2. 如果 `content.list_items` 不存在，使用现有扁平文本兼容分支。
3. 如果两种契约均不成立，抛出 `ArtifactFailure(artifact_input_invalid)`。

### V2 结构校验

- `list_type` 必须是 MinerU V2 当前定义的 `text_list` 或 `reference_list`。
- `list_items` 必须是非空列表。
- 每个列表项必须是映射。
- `item_type` 必须是 MinerU 当前定义的 `text`。
- `item_content` 必须是非空 span 列表。
- 每个 span 必须通过现有内联 span 渲染与结构化内容限制。
- 不允许布尔值冒充整数，不允许隐式 `str()` 转换。
- 任一结构错误都使用现有内容无关的 `artifact_input_invalid`。

### Markdown 输出

- `text_list` 与 `reference_list` 均使用 `- `。MinerU V2 当前契约没有提供可靠的有序/无序标记，不能从正文或列表项内容推断编号语义。
- 每个列表项独占一行。
- span 中的普通文本继续进行 Markdown/HTML 安全转义。
- span 中的行内公式复用现有 `$...$` 渲染规则。
- 输出仍受 `max_markdown_bytes` 限制。

### 兼容性

旧式 `{"type": "list", "content": {"text": "..."}}` 继续保持原行为。V2 与旧式格式不能混合解释：存在 `list_items` 时必须完整满足 V2 契约，不能在 V2 畸形时回退到扁平字段。

## 测试设计

按 TDD 顺序增加以下测试：

1. 真实 MinerU V2 无序列表结构能够渲染。
2. `text_list` 与 `reference_list` 都能稳定渲染为项目列表。
3. 列表项内的文字和行内公式复用现有内联渲染。
4. `list_items` 畸形、为空、`item_type` 非法、span 类型非法或 `list_type` 非法时安全失败。
5. 旧式扁平列表继续通过。
6. 使用 `44pages.pdf` 的内容无关结构形状构造回归夹具，证明旧实现失败、新实现通过。
7. 运行 `tests/test_artifacts.py`、相关产物/生产流水线测试以及全量 pytest。

真实 PDF 不复制进 Git 仓库；测试夹具不得包含业务正文，只使用合成文本和相同结构。

## 交付与验证

1. 本机完成 RED/GREEN 和全量测试。
2. 提交独立修复提交。
3. 同步到远程 Ubuntu。
4. 在 Ubuntu 构建新的候选镜像。
5. 构建完成后立即启动候选服务并检查健康状态。
6. 重新上传并通过 MCP 解析 `44pages.pdf`。
7. 验证任务完成、产物可下载、ZIP 必要条目齐全。
8. 所有验证通过后再固定镜像版本。
