# Ubuntu 生产发布验证手册

只在代码评审完成、明确提交已同步到 Ubuntu 后运行本手册。所有门禁通过之前不得创建
不可变镜像标签。

## 前置条件

- Ubuntu 工作区干净，提交 SHA 与控制端一致。
- `OCR_AUTH__API_KEYS` 和 `OCR_VERIFY_API_KEY` 已通过安全环境注入。
- `OCR_PUBLIC_BASE_URL` 是正确的对外 HTTPS 地址。
- `OCR_GATEWAY_PORT` 已明确设置，当前验证端口为 `18011`。
- MinerU 和 PP-StructureV3 离线模型清单及全部 SHA-256 已验证。
- `ocr-production`、`mineru-api` 和 `mineru-vlm` 候选镜像已经构建。

## 启动

```bash
docker compose up -d mineru-vlm mineru-api ocr-production
docker compose ps
```

验证 60 MiB 限制：

```bash
docker compose exec -T ocr-production \
  python -c "from ocr_mcp_server.settings import load_settings; assert load_settings().limits.max_file_size_bytes == 62914560"
```

## 三阶段验证

```bash
if [ -z "${OCR_VERIFY_API_KEY:-}" ]; then
  read -rsp 'OCR verification API key: ' OCR_VERIFY_API_KEY
  export OCR_VERIFY_API_KEY
  printf '\n'
fi

GIT_SHA="$(git rev-parse HEAD)"
RUN_ID="$(
  python scripts/verify_deployment.py --derive-run-id --git-sha "$GIT_SHA"
)"

python scripts/verify_deployment.py --phase static --run-id "$RUN_ID"
python scripts/verify_deployment.py --phase runtime --run-id "$RUN_ID"
python scripts/verify_deployment.py --phase e2e --run-id "$RUN_ID"
```

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 配置错误 |
| 2 | 运行时或就绪错误 |
| 3 | 端到端错误 |
| 4 | 安全边界错误 |

验证器只输出无正文的有限 JSON，不打印 HTTP 正文、文档文本、日志、文件名、URL、
凭据或 OCR 结果，也不会创建镜像标签。

`RUN_ID` 绑定当前 Git 提交以及三个生产镜像的规范化镜像 ID。同一候选重试必须复用
相同 `RUN_ID`，以保持 REST 幂等性；代码或任一镜像变化后必须重新计算。

## 真实文件回归

至少验证：

1. 一个普通 PDF。
2. 一个包含侧倒页的 PDF。
3. 一个图片文字需要回填的 PDF。
4. 一个接近页数或文件大小上限的大 PDF。

只记录：

- 文件字节数和页数
- SHA-256
- `batch_id`
- 阶段、进度、候选计数和耗时
- 终态、成功数和失败数
- 产物文件数、大小和结构

不得记录正文、OCR 文本或文件业务名称。

## 失败处理

失败时：

1. 保存稳定错误码、阶段和有限计数。
2. 不创建镜像标签。
3. 本机修复并重新运行全量测试。
4. 重新同步、构建、启动和验证。

不要通过跳过产物校验、放宽归档限制或关闭结构化内容安全规则来使测试通过。

## 固定版本

全部门禁通过后：

```bash
GIT_SHA="$(git rev-parse HEAD)"
docker tag ocr-mcp-server:production-dev \
  "ocr-mcp-server:production-${GIT_SHA}"
docker image inspect "ocr-mcp-server:production-${GIT_SHA}" \
  --format '{{.Id}} {{.Created}}'
```

在 `deployments/ocr-mcp-server/<git-sha>.env` 中记录：

- `OCR_IMAGE`
- `OCR_IMAGE_ID`
- `OCR_COMMIT_SHA`
- `OCR_VERIFIED_AT`
- `OCR_VERIFICATION`

记录提交可以位于被验证提交之后；镜像标签必须指向实际构建并通过验证的代码提交。
