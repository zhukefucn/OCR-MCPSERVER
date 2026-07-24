# OCR MCP Server 单文件 60MiB 上限扩容实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将默认单文件上限统一提升到 60MiB，并在本地和 5090 Ubuntu 上验证上传、MinerU 转发、方向恢复及现有 SQLite 数据库升级。

**Architecture:** 以 `DEFAULT_MAX_FILE_SIZE_BYTES` 作为网关和恢复链路的统一默认值，MinerU 固定代理使用同值的独立常量。SQLite 启动迁移把既有 `orientation_recoveries` 表的两个 30MiB CHECK 上限安全扩展为 60MiB，保留全部数据和约束。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、SQLAlchemy asyncio、SQLite、pytest、Docker Compose、MinerU 3.2.0。

## Global Constraints

- 默认单文件上限为 `60 * 1024 * 1024`，即 `62_914_560` 字节。
- 单文件最多 500 页、单批次最多 20 个文件、批次总量最多 `1024**3` 字节，均不改变。
- 超限上传继续返回 HTTP 413 和 `capacity_exceeded`，不得记录文件正文或识别文本。
- 正常解析不得自动旋转页面；方向恢复仍只由 REST/MCP 粗粒度恢复入口触发。
- 先完成本地测试，再同步 Ubuntu；远程构建后立即启动验证，全部通过后才固定 Git SHA 镜像标签。

---

### Task 1: 统一网关和 MinerU 固定代理的 60MiB 默认值

**Files:**
- Modify: `tests/test_domain.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_mineru_fixed_api.py`
- Modify: `src/ocr_mcp_server/domain/constants.py`
- Modify: `scripts/mineru_fixed_api.py`
- Modify: `config/example.yaml`

**Interfaces:**
- Consumes: `DEFAULT_MAX_FILE_SIZE_BYTES: int`
- Produces: 网关默认值与 MinerU 固定代理文件上限均为 `62_914_560`

- [ ] **Step 1: 先写失败的默认值与 MinerU 边界测试**

将默认值断言改为：

```python
assert DEFAULT_MAX_FILE_SIZE_BYTES == 60 * 1024 * 1024
assert settings.limits.max_file_size_bytes == 60 * 1024 * 1024
```

将 MinerU 固定代理关闭上传文件的参数化测试改为用
`60 * 1024**2 + 1` 构造 `size_rejection`，并新增恰好
`60 * 1024**2` 时能够转发的逻辑文件测试。

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```bash
uv run pytest -q tests/test_domain.py tests/test_settings.py tests/test_mineru_fixed_api.py
```

Expected: 默认值仍为 30MiB，且 MinerU 代理在 60MiB 边界前拒绝，测试失败。

- [ ] **Step 3: 实现统一的 60MiB 默认值**

修改服务常量：

```python
DEFAULT_MAX_FILE_SIZE_BYTES = 60 * 1024 * 1024
```

在 `scripts/mineru_fixed_api.py` 定义并使用：

```python
MAX_FILE_SIZE_BYTES = 60 * 1024 * 1024

if size > MAX_FILE_SIZE_BYTES:
    raise HTTPException(status_code=413, detail="file_too_large")
```

将 `config/example.yaml` 的 `max_file_size_bytes` 改为 `62914560`。

- [ ] **Step 4: 运行聚焦测试并确认通过**

Run:

```bash
uv run pytest -q tests/test_domain.py tests/test_settings.py tests/test_mineru_fixed_api.py
```

Expected: 全部通过。

- [ ] **Step 5: 提交默认值变更**

```bash
git add tests/test_domain.py tests/test_settings.py tests/test_mineru_fixed_api.py src/ocr_mcp_server/domain/constants.py scripts/mineru_fixed_api.py config/example.yaml
git commit -m "feat(limits): raise default file size to 60MiB"
```

### Task 2: 扩展方向恢复模型和既有 SQLite 数据库约束

**Files:**
- Modify: `tests/test_orientation_recovery.py`
- Modify: `tests/test_orientation_repository.py`
- Modify: `src/ocr_mcp_server/infra/document_orientation.py`
- Modify: `src/ocr_mcp_server/infra/task_models.py`
- Modify: `src/ocr_mcp_server/infra/database.py`

**Interfaces:**
- Consumes: `DEFAULT_MAX_FILE_SIZE_BYTES`
- Produces: `initialize_schema(engine)` 可幂等地把既有 30MiB 方向恢复 CHECK 约束升级为 60MiB

- [ ] **Step 1: 写方向恢复边界与数据库升级失败测试**

新增模型测试，构造 `accepted_input_size_bytes=45 * 1024 * 1024` 的
`FullRecoveryPipelineSubmission`，期望成功；构造
`DEFAULT_MAX_FILE_SIZE_BYTES + 1`，期望 `ValueError`。

新增数据库升级测试：

1. 创建当前结构数据库并保存一条方向恢复记录。
2. 在测试数据库中把 `sqlite_master` 的两个 `62914560` CHECK 文本改写成
   `31457280`，关闭连接后重新打开。
3. 调用 `initialize_schema(engine)`。
4. 断言表 SQL 不含 `31457280`、包含两个 `62914560`，原记录仍存在。
5. 再调用一次 `initialize_schema(engine)`，断言幂等且数据不变。

- [ ] **Step 2: 运行聚焦测试并确认失败**

Run:

```bash
uv run pytest -q tests/test_orientation_recovery.py tests/test_orientation_repository.py
```

Expected: 45MiB 恢复提交或旧约束迁移测试失败。

- [ ] **Step 3: 修改恢复默认值与 ORM CHECK**

`ImmutableDocumentCorrector.__init__` 使用：

```python
max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
```

`task_models.py` 的两个方向恢复 CHECK 上限均改为 `62914560`。

- [ ] **Step 4: 实现事务内 SQLite 表重建迁移**

在 `initialize_schema` 的既有加列迁移之后调用
`_migrate_orientation_recovery_file_limit`。迁移函数必须：

```python
old_limit = "31457280"
new_limit = "62914560"
table = "orientation_recoveries"
temporary = "orientation_recoveries__file_limit_migration"
```

读取 `sqlite_master.sql`；若不含旧上限则直接返回。只有在旧上限恰好出现两次、
临时表不存在时才继续。基于原始 CREATE TABLE SQL 创建临时表，将上限替换为
60MiB；使用 `PRAGMA table_info` 得到并安全双引号包裹完整列清单，执行
`INSERT INTO temporary (...) SELECT ... FROM orientation_recoveries`，然后删除旧表并
把临时表改名。任一结构不符合预期时抛出固定、无业务内容的 `RuntimeError`，
依赖 `engine.begin()` 回滚整个迁移。

- [ ] **Step 5: 运行恢复与迁移测试**

Run:

```bash
uv run pytest -q tests/test_orientation_recovery.py tests/test_orientation_repository.py
```

Expected: 全部通过，旧数据保留，迁移重复执行无变化。

- [ ] **Step 6: 提交方向恢复与迁移**

```bash
git add tests/test_orientation_recovery.py tests/test_orientation_repository.py src/ocr_mcp_server/infra/document_orientation.py src/ocr_mcp_server/infra/task_models.py src/ocr_mcp_server/infra/database.py
git commit -m "feat(recovery): support 60MiB corrected inputs"
```

### Task 3: 本地完整验证和文档一致性

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-21-ocr-mcp-mvp-implementation.md`
- Modify: `docs/superpowers/plans/2026-07-23-task-13-production-images.md`
- Modify: `docs/deployment/task-13-runbook.md`

**Interfaces:**
- Consumes: 已通过的 60MiB 实现
- Produces: 中文限制说明及可重复执行的本地验证证据

- [ ] **Step 1: 更新所有仍描述 30MiB 的项目文档**

把当前有效文档中的单文件限制改为 60MiB；历史部署记录
`deployments/ocr-mcp-server/*.env` 保持不可变。

- [ ] **Step 2: 扫描残留硬编码**

Run:

```bash
rg -n "31457280|30\s*\*\s*1024|30MiB|30 MB|30MB" src scripts config README.md docs tests
```

Expected: 仅迁移测试、旧上限迁移常量和明确描述“从 30MiB 升级”的设计文本可以命中。

- [ ] **Step 3: 运行完整本地测试**

Run:

```bash
uv run pytest -q
```

Expected: 退出码 0，无失败。

- [ ] **Step 4: 检查差异并提交**

```bash
git diff --check
git status --short
git add README.md docs/superpowers/plans/2026-07-21-ocr-mcp-mvp-implementation.md docs/superpowers/plans/2026-07-23-task-13-production-images.md docs/deployment/task-13-runbook.md
git commit -m "docs: document the 60MiB file limit"
```

不得提交无关的未跟踪 `uv.lock`。

### Task 4: 同步 5090 Ubuntu、构建、启动验证并固定版本

**Files:**
- Create after verification: `deployments/ocr-mcp-server/<git-sha>.env`

**Interfaces:**
- Consumes: 本地完整测试通过的精确 Git SHA
- Produces: Ubuntu 上健康且经过静态、运行时、端到端验证的不可变镜像标签

- [ ] **Step 1: 推送已验证提交并在 Ubuntu 快进同步**

Run locally:

```bash
git push origin feat/repository-skeleton
```

Run remotely:

```bash
git -C /home/jiangren/ocr-mcp-server fetch origin
git -C /home/jiangren/ocr-mcp-server merge --ff-only origin/feat/repository-skeleton
git -C /home/jiangren/ocr-mcp-server rev-parse HEAD
```

Expected: 远程 SHA 与本地完全一致，工作区无受跟踪修改。

- [ ] **Step 2: 在 Ubuntu 构建受影响镜像**

加载部署环境后执行：

```bash
docker compose --profile production build mineru-api ocr-production
```

Expected: 两个镜像构建退出码 0；MinerU VLM 使用已验证且未变更的现有镜像。

- [ ] **Step 3: 构建后立即启动候选组合**

```bash
docker compose --profile production up -d mineru-vlm mineru-api ocr-production
docker compose --profile production ps
```

Expected: 三个服务最终均为 healthy。

- [ ] **Step 4: 执行健康、配置和部署门禁**

先在生产容器中断言：

```bash
python -c "from ocr_mcp_server.settings import load_settings; assert load_settings().limits.max_file_size_bytes == 62914560"
```

然后按 `docs/deployment/task-13-runbook.md` 生成绑定当前 Git SHA 和三个镜像 ID
的 `RUN_ID`，依次运行 `static`、`runtime`、`e2e` 三个 phase。

Expected: 三个 phase 都返回退出码 0；端到端产物数量至少为 1；输出不含文档正文。

- [ ] **Step 5: 固定镜像版本并记录证据**

仅在上一步全部通过后：

1. 记录精确 Git SHA、UTC 时间、验证 RUN_ID、镜像 ID、GPU 驱动、模型清单哈希、
   三个 phase 状态和产物数量。
2. 创建 `deployments/ocr-mcp-server/<git-sha>.env`。
3. 给 `ocr-production`、`mineru-api` 和复用的 `mineru-vlm` 镜像增加精确 Git SHA 标签。
4. 提交并推送部署记录。

- [ ] **Step 6: 最终远程复核**

Run:

```bash
curl --fail --silent http://127.0.0.1:18011/health/live
curl --fail --silent http://127.0.0.1:18011/health/ready
docker ps --filter name=ocr-mcp-server --format '{{.Names}}|{{.Image}}|{{.Status}}'
```

Expected: live/ready 成功，SQLite、MinerU、Paddle 都为 ready，当前生产容器使用已记录的镜像 ID。
