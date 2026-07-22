# Task 7 实施报告：结构化替换、版本发布与回滚

## 范围

- Base：`434dc42d5f1e42db8937f1f8e94a486740c973c1`
- 实现提交：`d002b31f1c83a2b2b1d1da33b5e0f80eb6b2ac73`
- 安全复审修复提交：`f5c0d37503e10933e663bf2c1ee0d6511289dcb8`
- 未推送、未执行远程部署、未修改父设计或实施计划。

## 已交付

- 深度表格 HTML 验证：严格受限标签/属性、原始结束标签与实体 token、深度/元素/行/单元格/span/字符/字节限制。
- 保守 LaTeX 验证：命令与环境白名单、括号/数学定界符/环境嵌套检查、危险原语与字符重写拒绝、重复上限。
- 不可变审计领域契约、确定性 ID/顺序/快照/哈希。
- 仅对有效 TABLE/FORMULA 的 MinerU V2 精确节点替换；其余逐引用保留并审计；跨类型替换清除旧的不安全结构化字段。
- exact coverage、task/version/engine、pointer/type/path/alias、候选 identity/size/SHA-256 全局校验。
- 固定版本目录的原子无覆盖发布、精确幂等、冲突拒绝、独立 `publication_manifest.json` 文件名/哈希绑定。
- staging/root/target/file identity gate；Linux 使用 root/stage fd、openat/no-follow 与 renameat2；Windows 拒绝 reparse 并在关键边界复核 identity。
- 回滚从经固定路径、精确文件集合、独立哈希清单与 schema 验证的原始快照发布全新版本，不覆盖合并版本。
- 部署安全上限已加入 `AppSettings` 与 `config/example.yaml`；未新增 Paddle/MinerU/Torch/OpenCV 依赖。

## TDD 证据

1. 初始 RED：两个新测试模块在收集阶段失败，分别缺少 `structured_content` 服务与 merge 错误/领域契约。
2. 首轮 GREEN：focused `132 passed`，随后 settings RED/GREEN 完成部署配置。
3. 自审 RED/GREEN：补入控制/格式字符、实体解码、危险 TeX、重复 JSON key、源 manifest symlink、父链、回滚 schema、target symlink、异常脱敏与 hard cap。
4. 独立复审 RED：一次集中重放得到 `17 failed`，覆盖危险公式、父目录替换、候选变更、alternate rollback、staging replacement、畸形结束标签与跨类型残留。
5. 后续确定性 RED/GREEN：existing root/target/file name swap、fdopen ownership、无界冲突读取、严格实体与冲突分类。

## 复审闭环

- 独立只读 reviewer 首轮：NOT READY，报告 2 Critical、4 Important、1 Minor。
- 全部原始复现闭环后，reviewer 继续发现 existing target/name identity 与严格实体边界；均补确定性回归并修复。
- 最终 reviewer 结论：`READY`。

## 最终本地验证

- Focused：`179 passed in 0.98s`
- Full：`522 passed, 5 skipped in 4.13s`
- `python -m pip check`：`No broken requirements found.`
- `python -m compileall -q src tests`：退出码 0
- `git diff --check`：退出码 0（仅 Git 的 LF/CRLF 工作树提示）

## 剩余关注

- 当前机器是 Windows；Linux 专用 openat/renameat2 分支需要控制层按既定节奏在 Ubuntu 同步后运行全套测试与容器启动验证。
- LaTeX 使用保守白名单；真实 Paddle 输出若出现新但无副作用的数学命令，应先加验证样本和安全评估，再显式扩充白名单。

## Controller 复审修复（2026-07-22）

Controller 复审提出 2 Critical、3 Important，修复提交：

- `53a476a17bb2cbb39cd46d8e9787ba74b37614e2` — `fix: bind published versions to verified identities`

新增回归位于：

- `tests/test_merge_publication.py`
  - staging 名称在最终检查后被替换时不得返回成功；
  - 清理删除窗口发生目录替换时保留 victim；
  - rollback publication manifest 拒绝额外顶层字段；
  - candidate ID 与 processing record ID 必须按 Task 5 公式重算匹配。
- `tests/test_structured_content_validation.py`
  - 单元格正文中的裸 `<` 必须拒绝，文本小于号只能通过批准实体表达。

RED 命令：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_structured_content_validation.py tests/test_merge_publication.py -q
```

RED 结果：`7 failed`，失败项与 controller 五项发现一致：2 个裸 `<`、2 个伪造 ID、staging 名称替换、清理删除窗口替换、rollback manifest 额外字段。

GREEN focused 命令：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_structured_content_validation.py tests/test_merge_publication.py -q
```

GREEN focused 结果：`122 passed in 1.08s`。

提交前完整门禁命令：

```text
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

结果：

- Full：`529 passed, 5 skipped in 4.16s`
- pip check：`No broken requirements found.`
- compileall：退出码 0
- diff-check：退出码 0，仅 Git LF/CRLF 提示

修复要点：

- 原子 rename 后、设置 `published=True` 前，重新从 root fd/安全路径打开目标，要求目标 identity 等于原始 open staging identity，并验证精确文件集合、每个固定文件 identity、长度与字节哈希。
- 失败清理不再调用 `shutil.rmtree`；POSIX 使用已持有的 root/stage fd 做目录相对的精确 unlink/rmdir，Windows 逐文件复核 identity 后仅删除已知文件。任何名称或 identity 不一致均保留目录。
- rollback 独立 manifest 采用精确顶层 schema，并要求磁盘字节等于重新生成的 canonical JSON。
- merge 边界按 Task 5 既定 SHA-256 公式重算 candidate/record ID，任何不匹配全局失败且不发布。

## Controller 清理竞态跟进（2026-07-22）

Controller 再次复审发现：即使先校验 identity，校验与 `unlink`/`rmdir` 之间仍存在同名对象替换窗口。修复提交：

- `05a229423a08870b1738c75aed5647b736beab67` — `fix: preserve failed publication staging`

本节结论取代上一节关于“失败清理逐文件删除”的描述。现在的安全策略是：

- 发布失败或尚未确认发布时，不对 staging 执行任何基于名称或路径的删除；
- staging 作为 `.merge-stage-*` 孤儿目录保留，等待后续独立、受控的保留期清理；
- 发布成功时 staging 已被原子重命名为目标版本目录，无需额外清理；
- 本任务不实现保留期清理，也不扩展到 Task 8。

新增两个确定性故障注入回归，分别在 identity 校验后、删除动作前替换同名文件和空目录。修改前 RED：

```text
.venv\Scripts\python.exe -m pytest tests/test_merge_publication.py::test_failed_publication_never_unlinks_same_name_file_replacement tests/test_merge_publication.py::test_failed_publication_never_removes_same_name_directory_replacement -q
```

RED 结果：`2 failed in 1.8s`；两个 victim 均被旧清理逻辑删除。

修改后 focused 验证：

```text
.venv\Scripts\python.exe -m pytest tests/test_structured_content_validation.py tests/test_merge_publication.py -q
```

GREEN focused 结果：`123 passed in 2.0s`。

完整门禁结果：

- Full：`530 passed, 5 skipped in 5.6s`
- 收集：`535 tests collected in 0.73s`
- pip check：`No broken requirements found.`
- compileall：退出码 0
- diff-check：退出码 0，仅 Git LF/CRLF 提示

## Ubuntu 幂等发布绑定跟进（2026-07-22）

Ubuntu 首轮门禁为 `2 failed, 529 passed, 4 skipped`。两项失败最初均显示 `DID NOT RAISE MergeFailure`。系统化调用链检查确认：

- 原测试只 monkeypatch 了 Windows 路径的 `_existing_target_is_unsafe` 和 `_existing_matches`；POSIX 现有目标路径使用 `_classify_existing_target_anchored`，因此 Ubuntu 上测试根本没有执行交换动作。
- 将故障注入移动到 POSIX 实际边界后，确认存在两个真实的最终绑定窗口：
  - root 路径完成唯一一次 identity 检查后被替换，后续 target 校验仍绑定旧 root fd，最终却返回指向新 root 的路径；
  - target 名称首次校验并打开 fd 后被替换，后续文件校验仍绑定旧 target fd，但没有再次校验 target 名称。

TDD 测试提交：

- `50c5a2344b944aec5295b0f89805adc81642c715` — `test: reproduce idempotent publication swaps`

Ubuntu RED 命令：

```text
.venv/bin/python -m pytest tests/test_merge_publication.py::test_idempotent_retry_rejects_publication_root_swap_between_checks tests/test_merge_publication.py::test_idempotent_retry_rejects_target_swap_after_byte_match -q
```

Ubuntu RED 结果：两项均稳定失败，错误均为 `DID NOT RAISE MergeFailure`。本地 Windows 对公共 root 最终绑定窗口也得到预期 RED：`1 failed, 1 passed`；另一项使用 Linux 专用 `openat` 注入。

最小生产修复提交：

- `c7a83119ece27d7bf4774e394091e5a5b172732e` — `fix: recheck idempotent publication bindings`

修复内容：

- path 与 anchored target binding 在文件校验完成后再次校验 target 名称 identity；
- 所有现有目标和 `FileExistsError` 幂等返回前再次校验 publication root 路径 identity。

本地 Windows GREEN 与门禁：

- 两项定向回归：`2 passed`
- Task 7 focused：`123 passed`
- Full：`530 passed, 5 skipped`
- pip check：`No broken requirements found.`
- compileall：退出码 0
- diff-check：退出码 0，仅 Git LF/CRLF 提示

Controller 在 Ubuntu 同步 `424699f` 后的最终验证结果：

- 两项定向回归：`2 passed`
- Full：`531 passed, 4 skipped`
- pip check：`No broken requirements found.`
- compileall：退出码 0
- remote diff-check：退出码 0
