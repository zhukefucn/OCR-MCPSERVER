# OCR MCP Server

这是面向 Ubuntu Server 的 Python 3.11 OCR 服务仓库。目前只包含仓库骨架、部署配置、基础领域契约、FastAPI 应用工厂和不依赖外部服务的 `/health/live`；尚未实现 OCR 业务处理、数据库、MCP 工具或业务 REST 接口。

## 本地安装

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
```

复制 `config/example.yaml` 为本地配置文件，并通过 `OCR_CONFIG_FILE` 指定它。所有设置也可使用 `OCR_` 前缀的环境变量覆盖；嵌套字段用双下划线分隔，例如：

```bash
export OCR_CONFIG_FILE=config/local.yaml
export OCR_SERVER__PORT=9000
export OCR_SECONDARY_OCR__ENGINE=pp_structure_v3
```

环境变量优先于 YAML。二次 OCR 引擎是进程启动时的部署选项，不是请求参数。

## 启动

```bash
python -m ocr_mcp_server
# 或
ocr-mcp-server
```

当前 FastAPI `api` 包是 REST 路由承载层。后续 REST API 与 MCP 接口会共用 `services` 服务层，避免把业务逻辑绑定到任一传输协议；本任务只提供存活探针。

## 文件接入安全部署

`data_root` 必须位于服务端受控文件系统，归服务账户所有，并且只允许该账户写入（Ubuntu 建议目录权限 `0700`、文件权限 `0600`）。不得让 Web 用户、共享组或其他进程修改 `data_root`、`.locks`、batch 或 `input` 路径；代码会拒绝 symlink/reparse 路径并使用目录/文件 handle 与原子 no-replace publication，但服务账户独占写权限仍是抵御祖先目录替换的必要边界。

远程导入在每次请求和每级 redirect 前重新校验 HTTPS URL、精确 hostname 白名单及全部 DNS 地址。HTTPX 默认传输不会把已校验 IP 固定到 socket，因此应用校验不能单独完全消除 DNS rebinding；生产环境必须再通过出口防火墙或受控代理禁止访问私网、loopback、link-local 和保留地址。

## 远程 Ubuntu 容器验证

当前容器镜像仅包含 FastAPI 网关，不包含 MinerU 或 Paddle 推理依赖。本机只运行测试，不执行 Docker 镜像构建；尚未在远程 Ubuntu 完成构建和启动验证。

代码同步到 Ubuntu Server 后，在仓库根目录执行：

```bash
docker compose build ocr-gateway
docker compose up -d ocr-gateway
docker compose ps
curl -fsS http://127.0.0.1:8000/health/live
```

确认 `docker compose ps` 显示容器健康，且存活探针返回成功响应后，再记录远程实际构建得到的镜像 ID 和 digest。
