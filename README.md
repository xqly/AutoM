# AutoM

AutoM 是一个客服提交 MayCAD 铝型材绘图需求、后端创建任务、Worker 调用 `codex exec` 生成 MayCAD 录入/复核资料的网站 MVP。

当前实现刻意使用 Python 标准库、SQLite 和静态前端，减少 Linux/Windows 部署依赖。后续可以把 HTTP 层替换成 FastAPI、把前端替换成 React，但数据库和任务执行模型可以继续沿用。

## 功能范围

- 简单客服登录，用于记录任务由哪个客服提交。
- 客服填写客户名称、需求描述和参考图片后提交绘图需求。
- SQLite 存储用户、会话、任务、附件、Job、事件和产物元数据。
- 文件系统存储附件、Codex 工作目录、日志和输出文件。
- Worker 串行领取任务并调用 `codex exec`。
- 网站展示任务状态、事件日志、预览图，并提供结果文件下载。
- 支持 dry-run 模式，不调用 Codex，便于本地验证流程。

## 输出文件约定

每个任务完成后，Worker 期望在 `output/` 目录中找到：

- `maycad_plan.json`：面向 MayCAD 操作员的结构化铝型材方案，包含型材、连接件、附件、加工和装配步骤。
- `bom.csv`：物料清单。
- `cut_list.csv`：型材切割清单。
- `preview.png`：网站预览图。
- `manifest.json`：文件清单和生成摘要。

如果真实 Codex 运行没有生成这些文件，任务会标记为 `failed`，并保留 `codex.jsonl` 和 `stderr.log` 方便排查。

## 目录结构

```text
autom_app/
  config.py          配置和 .env 加载
  database.py        SQLite 初始化和基础访问
  security.py        密码 hash、session token
  server.py          HTTP API 和静态文件服务
  worker.py          Job 领取、Codex 执行、产物登记
schema.sql           SQLite 表结构
frontend/            静态页面、样式和 JS
scripts/
  setup.py           初始化目录、数据库、默认用户
  manage_user.py     创建客服账号、改密码、停用账号
  run_server.py      启动网站，可同时启动内置 Worker
  run_worker.py      单独启动 Worker
  smoke_test.py      基础冒烟测试
  run_server.sh      Linux/macOS 启动脚本
  run_worker.sh
  run_server.bat     Windows 启动脚本
  run_worker.bat
data/                运行后生成，默认不提交
```

## 快速开始

1. 初始化数据库和默认账号：

```bash
python3 scripts/setup.py
```

Windows:

```bat
python scripts\setup.py
```

默认账号：

```text
admin / admin123
support / support123
```

生产环境请立刻修改密码。当前 MVP 没有做复杂权限，`role` 主要用于后续扩展。

修改默认密码：

```bash
python3 scripts/manage_user.py set-password support
python3 scripts/manage_user.py set-password admin
```

创建客服账号：

```bash
python3 scripts/manage_user.py add alice "客服 Alice" --role support
```

查看账号：

```bash
python3 scripts/manage_user.py list
```

2. 本地 dry-run 启动，不调用 Codex：

```bash
AUTOM_CODEX_DRY_RUN=1 python3 scripts/run_server.py
```

Windows PowerShell:

```powershell
$env:AUTOM_CODEX_DRY_RUN="1"
python scripts\run_server.py
```

打开：

```text
http://127.0.0.1:8000
```

3. 使用真实 `codex exec`：

```bash
export CODEX_API_KEY="你的 OpenAI API Key"
export AUTOM_CODEX_DRY_RUN=0
python3 scripts/run_server.py
```

如果服务器上已经通过 `codex login` 配好账号，也可以不设置 `CODEX_API_KEY`，但生产环境更推荐用服务专用 API key，便于审计和轮换。

## 配置

配置可以通过环境变量或项目根目录 `.env` 设置。可参考 `.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTOM_HOST` | `127.0.0.1` | HTTP 监听地址 |
| `AUTOM_PORT` | `8000` | HTTP 端口 |
| `AUTOM_DATA_DIR` | `data` | SQLite、附件、Job 和产物目录 |
| `AUTOM_WORKER_ENABLED` | `1` | `run_server.py` 是否内置启动 Worker |
| `AUTOM_CODEX_COMMAND` | `codex` | Codex CLI 命令路径 |
| `AUTOM_CODEX_MODEL` | 空 | 可选，传给 `codex exec -m` |
| `AUTOM_CODEX_DRY_RUN` | `0` | `1` 时生成示例文件，不调用 Codex |
| `AUTOM_CODEX_TIMEOUT_SECONDS` | `1800` | 单任务 Codex 超时 |
| `AUTOM_CODEX_EARLY_ACCEPT_SECONDS` | `20` | 真实模式下产物通过校验并稳定多少秒后提前验收，防止 Codex 过度自检卡住 |
| `AUTOM_WORKER_POLL_SECONDS` | `3` | Worker 轮询间隔 |
| `AUTOM_MAX_ATTACHMENT_BYTES` | `10485760` | 单个附件最大字节数 |
| `AUTOM_SESSION_TTL_HOURS` | `72` | 登录有效期 |

## 常用脚本

Linux/macOS:

```bash
python3 scripts/setup.py
python3 scripts/run_server.py
python3 scripts/run_worker.py
python3 scripts/smoke_test.py
python3 scripts/manage_user.py list
```

Windows:

```bat
python scripts\setup.py
python scripts\run_server.py
python scripts\run_worker.py
python scripts\smoke_test.py
python scripts\manage_user.py list
```

`run_server.py` 默认会同时启动一个内置 Worker。生产环境也可以拆成两个进程：

```bash
AUTOM_WORKER_ENABLED=0 python3 scripts/run_server.py
python3 scripts/run_worker.py
```

## Codex 执行方式

真实任务会在独立目录中运行：

```text
data/jobs/{job_id}/
  input/
    request.json
    attachments/
  output/
  logs/
    codex.jsonl
    stderr.log
  prompt.txt
  result.schema.json
  final.json
```

Worker 调用命令形态：

```bash
codex exec \
  --json \
  --sandbox workspace-write \
  --skip-git-repo-check \
  --cd data/jobs/{job_id} \
  --output-schema result.schema.json \
  -o final.json \
  -
```

如有参考图片，会追加 `-i input/attachments/xxx.png`。

Worker 在真实模式下会做基础产物校验：

- `maycad_plan.json` 必须是面向 MayCAD 的结构化 JSON，并包含单位、型材和装配步骤。
- `bom.csv` 必须包含表头和至少一行物料数据。
- `cut_list.csv` 必须包含表头和至少一行切割数据。
- `preview.png` 必须有 PNG 文件头。
- `manifest.json` 必须是 JSON 对象，并包含 `unit` 和 `files`。

如果四个产物通过校验并且文件大小稳定超过 `AUTOM_CODEX_EARLY_ACCEPT_SECONDS`，Worker 会提前验收并终止仍在继续深度自检的 `codex exec` 进程，避免任务长时间停在 `running`。

## 运行状态检查

登录后前端右上角会显示当前运行模式：

- `Dry-run`：不会调用 Codex，只生成示例产物。
- `Codex`：会调用 `codex exec`。
- `可用/未找到`：后端是否能找到 `AUTOM_CODEX_COMMAND` 指向的命令。

也可以直接请求：

```text
GET /api/health
```

该接口会返回 dry-run 状态、Codex 命令、数据目录、任务状态数量和 Job 状态数量。

## 数据库

SQLite schema 在 `schema.sql`。初始化脚本会执行该文件，并启用：

- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA busy_timeout = 5000`

SQLite 只存元数据。大文件不进数据库，统一放在 `data/` 下，数据库保存相对路径、文件大小、hash 和类型。

## Linux 部署建议

推荐路径：

```text
/opt/autom       应用代码
/var/lib/autom   数据目录
/etc/autom/.env  配置
```

示例：

```bash
export AUTOM_DATA_DIR=/var/lib/autom
export AUTOM_HOST=0.0.0.0
export AUTOM_PORT=8000
export CODEX_API_KEY=...
python3 scripts/setup.py
AUTOM_WORKER_ENABLED=0 python3 scripts/run_server.py
python3 scripts/run_worker.py
```

生产环境建议用 Nginx 反代到 `127.0.0.1:8000`，再用 systemd 分别管理 API 和 Worker。

## Windows 部署建议

可以原生运行：

```bat
set AUTOM_DATA_DIR=C:\autom-data
set AUTOM_HOST=0.0.0.0
set AUTOM_PORT=8000
set CODEX_API_KEY=...
python scripts\setup.py
python scripts\run_server.py
```

如果 Codex CLI 在 Windows 上依赖较难装，建议用 WSL2，把代码和数据放在 WSL 的 Linux 文件系统中运行。

## 下一步建议

当前已经具备：客服登录、任务提交、参考图片上传、`codex exec` Worker、PNG 预览、结果下载、任务重试、账号脚本、健康检查。

建议下一步继续做：

- 对 `maycad_plan.json` 执行更严格的 MayCAD 录入规范校验，例如型材系列、连接件数量和切割长度一致性。
- 增加任务重试时的 prompt 版本记录，便于回溯。
- 做数据库备份脚本和 `data/` 清理策略。
- 当每天任务耗时过长时，把 Worker 数量从 1 提升到 2 或更多。
- 增加 HTTPS/Nginx/systemd 生产部署样例文件。
