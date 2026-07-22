# Task 6 实施报告：单 owner Paddle worker 与 PP-StructureV3 provider

## 范围与提交

- 基线提交：`f7d795f23b06dd8c8ca6a0bf3e169ee21491f981`
- 实现提交：`619d840`（`feat: add single-owner Paddle OCR provider`）
- 本任务只实现单候选二次 OCR 执行层、PP-StructureV3 适配与浅层结果归一化；未实现 Task 7+ 的持久化、批次编排、替换/回滚、REST/MCP 接口或 Paddle 生产镜像。
- 未向基础依赖加入 PaddleOCR、PaddlePaddle、PaddleX、Torch、OpenCV 或 GPU 包。

## TDD 证据

### 第一轮 RED

先新增 settings、worker、PP-StructureV3 backend/normalizer 测试，再运行：

```text
.venv\Scripts\python.exe -m pytest \
  tests/test_secondary_ocr_worker.py \
  tests/test_pp_structure_v3.py \
  tests/test_settings.py -vv
```

结果：`35 failed, 48 passed`。失败原因是 worker/backend/error contracts 尚不存在，以及新增 settings 字段尚未实现，符合预期 RED。

### GREEN 与补强

实现最小功能后 focused 测试转为 GREEN。自审补充了以下覆盖：

- 启动期间取消、active/queued 调用取消和安全 queue accounting；
- close 的安全边界、owner thread join、backend close 异常脱敏；
- 实际 provider factory 保证 Paddle 构造、predict、close 同一 owner thread；
- 布尔型数值配置拒绝、MinerU node hint 不覆盖 Paddle 决策；
- 多个 target result（含空内容项）保持 `UNCERTAIN`。

独立只读审查随后发现三个 P1，并再次按 RED/GREEN 修复：

1. PaddleX V2 会省略空的 `table_res_list` / `formula_res_list`；新增真实 shape 测试，修复为“缺失视为空列表、存在但非列表仍 INVALID”。
2. close 与失败初始化并发时 lifecycle 会残留 `FAILED`；新增阻塞 factory 测试，修复为保留 `CLOSING` 并最终进入 `CLOSED`。
3. close 开始后，owner 可能已从 queue 取出但尚未开始的任务；新增 dequeue barrier 确定性测试，并把 dequeue-to-active 与 lifecycle transition 在线性化锁下判定。

这组回归测试修复前为 `5 failed`，修复后全部通过。最终 focused 验证：`96 passed in 0.67s`。

## 设计决策

- `SingleOwnerSecondaryOcrWorker` 使用一个有界 `queue.Queue` 和一个专用 owner thread。backend factory、每次同步 recognize、backend close 均只在该线程运行。
- async 边界使用 `concurrent.futures.Future` + `asyncio.wrap_future()` + `shield()`；owner thread 不直接完成 `asyncio.Future`，调用方取消不会取消正在运行的 native inference，也不会产生 `InvalidStateError`。
- close 停止接单、拒绝仍未 active 的 queued/dequeued job、等待 active inference 到安全边界、在 owner thread 关闭 backend，并通过非阻塞 event-loop 的 join 完成线程回收。
- 生命周期显式为 `created / starting / running / closing / closed / failed`，并公开只读 lifecycle、queue depth、owner-thread alive 状态。
- 所有 worker 边界异常使用稳定的非敏感 machine code；原始异常、文件路径、文件名和识别内容不保留在异常链或日志中。
- `paddleocr.PPStructureV3` 仅在 owner thread 执行的 backend 构造器内 lazy import。基础安装在没有 Paddle/PaddleX/Torch 时仍可导入模块。
- PP-StructureV3 constructor/predict 使用固定生产 flags，启用文档正交方向、表格和公式识别，禁用文档去畸变、文本行方向、印章、图表和区域检测；请求侧没有 engine/device/model/threshold/runtime option。
- 默认公式模型为 `PP-FormulaNet_plus-S`。engine 仍是进程级配置；PP-Structure provider/factory 明确拒绝 `paddleocr_vl`，且不做 fallback。
- normalizer 只读取 `.json["res"]` 或等价 JSON-safe mapping 的预期字段。table/formula 仅在单一明确 target detection、单一匹配 recognition 且 confidence 达阈值时 VALID；歧义/低置信度为 UNCERTAIN，畸形响应为 INVALID，predict 异常为 FAILED，均不携带替换内容。

## 独立审查

- 首轮结论：3 个 P1（真实 PaddleX 可选字段、失败初始化/close lifecycle、dequeue/close 竞态）。
- 修复后复审：Critical 0、Important 0、Minor 0；`43 passed`；结论 `READY`。
- 审查代理只读，未修改或提交文件。

## 最终验证

```text
.venv\Scripts\python.exe -m pytest
392 passed, 5 skipped in 3.64s

.venv\Scripts\python.exe -m pip check
No broken requirements found.

.venv\Scripts\python.exe -m compileall -q src tests
exit 0

git diff --check
exit 0（仅 Git 的 LF/CRLF 工作区提示，无 whitespace error）
```

## 剩余关注点

- 本机没有安装 Paddle/GPU runtime；本任务用 fake import boundary 验证 API 和并发契约。真实 Paddle CPU/GPU 镜像构建与 RTX 5090 启动验证属于 Task 13。
- PaddleX V2 JSON shape 已依据本地 3.x 源码覆盖，但生产镜像仍需固定 Paddle/PaddleX 版本，并在 Task 13 加真实模型 smoke test。
- HTML/LaTeX 深层有效性、结果持久化、替换资格和原节点回滚属于 Task 7，当前只做 shallow response-contract validation。
