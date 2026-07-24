# OCR MCP Server

面向智能体的银行文档 OCR 通用能力层。系统以 MinerU 为主解析引擎，
使用 PP-StructureV3 对 MinerU 图片候选进行二次检测，通过 FastAPI 同时提供
REST API 和 MCP 接口。

REST 与 MCP 共享同一服务层，任务状态和业务规则不绑定传输协议。项目已经从初始
仓库骨架发展为可构建、可部署、可回归验证的生产形态 MVP。

## 当前能力

- 支持 PDF、PNG、JPG、JPEG。
- 单文件最大 60 MiB、最多 500 页。
- 单批次最多 20 个文件、批次总量最大 1 GiB。
- MinerU 固定使用 `vlm-http-client`，VLM 推理由独立容器提供。
- 默认二次检测引擎为 PP-StructureV3。
- 表格、公式和可信图片文字通过有效性校验后回填；普通图片保持原样。
- 使用 SQLite 和本地挂载目录，不依赖 Redis、PostgreSQL 或外部消息队列。
- 输入、中间文件和结果默认保留 24 小时；无正文审计元数据保留 30 天。
- 日志、指标和进度信息不记录文件正文、OCR 文本、凭据或恢复令牌。

## 对外接口

REST API 负责文件上传、任务查询和产物下载。MCP 只暴露三个粗粒度工具：

- `parse_documents`
- `get_task_status`
- `reparse_with_page_orientation`

智能体不选择 OCR 引擎，也不编排 MinerU、Paddle、合并或打包细节。

## 快速导航

- [安装与部署手册](docs/安装与部署手册.md)
- [REST、MCP 与产物使用手册](docs/使用手册.md)
- [Skill 安装与使用手册](docs/Skill安装与使用手册.md)
- [Ubuntu 发布验证手册](docs/deployment/task-13-runbook.md)
- [系统设计规格](docs/系统设计规格.md)
- [实施状态与验收](docs/实施状态与验收.md)

## 本机开发

要求 Python 3.11。推荐使用 `uv`：

```powershell
uv venv --python 3.11
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
python -m pytest -q
python -m ocr_mcp_server
```

Linux：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -q
python -m ocr_mcp_server
```

配置项使用 `OCR_` 前缀和双下划线表示嵌套字段，例如
`OCR_LIMITS__MAX_FILE_SIZE_BYTES=62914560`。示例配置见
`config/example.yaml`。

## 健康检查

- `GET /health/live`：进程存活检查，不需要 API Key。
- `GET /health/ready`：依赖就绪检查，只有 SQLite、MinerU 和 Paddle 均就绪时成功。
- `GET /metrics`：Prometheus 指标，只应暴露在可信内网或基础设施访问控制之后。

生产流量不得只依据存活检查进行路由。

## PP-StructureV3 GPU 镜像

RTX 5090 使用 CUDA 12.9 对应的 GPU 镜像，固定安装
`paddlepaddle-gpu==3.3.0`。GPU 构造参数不传入仅适用于 CPU 的
`enable_mkldnn`。

```bash
docker compose --profile ppstructure-gpu build ocr-gateway-ppstructure-gpu
docker compose --profile ppstructure-gpu up -d ocr-gateway-ppstructure-gpu
docker compose --profile ppstructure-gpu exec ocr-gateway-ppstructure-gpu \
  python /app/scripts/smoke_pp_structure.py --device gpu:0 \
  --fixture /app/scripts/fixtures/pp_structure_smoke.json
```

真实 smoke 必须验证 CUDA Paddle、一个可见 GPU、`paddle.utils.run_check()` 和一次
结构化预测。通过前不得固定镜像版本。

## 远程 Ubuntu 容器验证

本机只运行测试和静态门禁，GPU Docker 镜像在 Ubuntu 5090 构建。默认
`ocr-gateway` 仅包含 FastAPI 网关，不包含 MinerU 或 Paddle 推理依赖；推理依赖
位于独立镜像。

基础网关验证命令：

```bash
docker compose build ocr-gateway
docker compose up -d ocr-gateway
docker compose ps
curl -fsS http://127.0.0.1:8000/health/live
```

当前版本已在远程 Ubuntu 完成构建和启动验证；完整生产门禁见
[Ubuntu 发布验证手册](docs/deployment/task-13-runbook.md)。

## 安全边界

- API Key 只通过环境变量或密钥管理系统注入。
- 远程 URL 导入必须使用 HTTPS 白名单、重定向重校验、私网地址拒绝和字节上限。
- `data_root` 只允许服务账号写入，推荐目录权限 `0700`、文件权限 `0600`。
- 模型目录以只读方式挂载；生产启动期间不下载模型。
- CPU 客户端不安装 PaddleOCR-VL、CUDA、vLLM 或 GPU 权重。
- 不将测试 PDF、业务正文或 OCR 结果提交到 Git。

## 已验证基线

2026-07-25 的发布回归包括：

- 本机全量 pytest。
- Ubuntu RTX 5090 镜像构建和容器健康检查。
- 462 页、56,767,563 字节 PDF 的完整解析。
- 811 个图片候选处理、结果合并、Markdown 打包、ZIP 下载和结构校验。
- MinerU V2 列表、图片文字回填和安全转义百分数公式。

不可变镜像和提交信息记录在 `deployments/ocr-mcp-server/`。
