# 部署与运行说明（阶段 5）

本项目是个人 AI 应用工程作品，目标是**本机可运行、依赖清晰、启动简单、故障可排查**。
不使用 Kubernetes、微服务拆分、Redis、Kafka，也没有 Web 前端构建链。

> 安全前置：`/api/v1/audit/*`、Trace 与设备接口**没有完整认证与权限控制**
> （审计接口只有可选共享令牌）。只允许在本机或隔离网络演示，不要默认暴露到公网。

## 1. 环境要求

| 组件 | 实测版本 | 说明 |
| --- | --- | --- |
| Python | 3.11.15（Conda 环境 `ms_pointcloud_midterm`） | TUI 与后端共用同一环境 |
| PostgreSQL | 17.11 + pgvector 0.8.7 + pg_trgm | Docker Compose 单服务 |
| Ollama | 本机 `127.0.0.1:11434` | 模型：`bge-m3`（embedding，1024 维）、`qwen2.5:7b-instruct` |
| Textual | 1.0.0（约束 `>=1.0,<2.0`） | TUI 框架 |
| httpx | 0.28.1 | TUI → API |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | 测试 |

依赖清单：`backend/requirements.txt`；可安装的包元数据：`backend/pyproject.toml`。

## 2. 数据库布局（当前真实状态，未变更）

| 库名 | 用途 | Alembic 版本 | 本阶段是否改动 |
| --- | --- | --- | --- |
| `industrial_agent_audit` | 工业数据（1,516,948 行） | `0004_coverage_missing_time` | **未改动**（代码期望 `0008`，因此显示 degraded） |
| `iaa_rag_dev` | RAG 知识库（6 文档 / 1,314 chunk） | `0007_knowledge_rag` | 未改动 |
| `iaa_audit_dev` | 审计库（审计运行与发现） | `0008_agent_audit` | 新增审计记录（演示与测试产生，属正常写入） |
| `industrial_agent_audit_test` | 测试库 | `0008_agent_audit` | 由测试自行 TRUNCATE |

阶段 5 **没有**执行任何迁移、没有合并数据库、没有重建知识库、没有重新导入数据。
主库与代码期望版本的差异由状态页如实显示为 `degraded`。

若以后要统一版本，需单独评审后执行（**不要**在 TUI 或被测代码里自动迁移）：

```bash
scripts/migrate.sh target      # 只读：确认将要连接的库
scripts/migrate.sh current     # 只读：当前版本
scripts/migrate.sh up          # 需人工批准后再执行
```

## 3. 配置

配置来自项目根 `.env`（已被 `.gitignore` 忽略），脚本只填充未设置的环境变量。

| 变量 | 用途 | 本阶段是否新增 |
| --- | --- | --- |
| `DATABASE_URL` / `RAG_DATABASE_URL` / `AUDIT_DATABASE_URL` | 三个数据库 | 否 |
| `LLM_*` / `EMBEDDING_*` | Ollama 模型与超时 | 否 |
| `IAA_API_BASE_URL` | TUI 连接的后端地址（默认 `http://127.0.0.1:8000`） | **新增（可选）** |
| `IAA_AUDIT_TOKEN` | 审计接口共享令牌（对应后端 `AUDIT_API_TOKEN`） | **新增（可选）** |

`IAA_AUDIT_TOKEN` 只放在环境变量或 `.env`，不要写进命令历史或截图；
TUI 不会在任何日志、异常或界面文案里打印它。

## 4. 启动步骤

```bash
# 0) 一次性：安装依赖与 TUI 入口（在 Conda 环境中）
conda activate ms_pointcloud_midterm
pip install -r backend/requirements.txt
pip install -e backend            # 生成 iaa-tui 命令（只影响本地环境）

# 1) 数据库（若尚未运行）
scripts/db.sh up

# 2) 后端（不做迁移、不导入数据）
scripts/start_api.sh              # http://127.0.0.1:8000

# 3) TUI（另开终端）
scripts/start_tui.sh              # 等价于 iaa-tui
# 或
cd backend && python -m app.tui
```

`pip install -e backend` 只安装本项目自身的 `app` 包与 `iaa-tui` 入口，不改动第三方依赖。
若不想安装，`python -m app.tui` 与 `scripts/start_tui.sh` 都能工作（脚本会自动回退）。

## 5. 启动前检查

```bash
scripts/check_system.sh           # 快速检查（API + 四库版本）
scripts/check_system.sh --deep    # 额外真实调用一次 Embedding 与 LLM
iaa-tui --check                   # 只探测并打印，不启动界面
iaa-tui --version
```

实测输出示例（本机）：

```
系统状态：降级（快速探测）
API：http://127.0.0.1:8000
  API        ready       industrial-agent-audit-api v2.0.0
  工业库      degraded    industrial_agent_audit schema 0004_coverage_missing_time ≠ 期望 0008_agent_audit
  RAG        ready       iaa_rag_dev 文档 6 / Chunk 1314 / schema 0007_knowledge_rag
  Audit      ready       审计记录 9 条（近 1 天）
  Embedding  unknown     配置 bge-m3（未探测）
  LLM        unknown     配置 qwen2.5:7b-instruct（未探测）
  ! 工业库迁移版本与代码期望不一致（未迁移，TUI 不会自动迁移）：0004… ≠ 0008…
```

`--check` 的返回码：API 可用 → 0；API 不可用 → 1。启动时若后端不可达，
`iaa-tui` 打印修复建议并返回 2（可用 `--start-anyway` 强制进入界面查看 unavailable 状态）。

## 6. 常见故障与排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `无法连接后端 …` 返回码 2 | 后端未启动 / 端口不同 | `scripts/start_api.sh`；或用 `--base-url` 指定端口 |
| 状态栏「工业库 ◐ 降级」 | 主库停在 `0004`，代码期望 `0008` | 属已知状态；迁移需单独批准，TUI 不会自动执行 |
| 状态栏「LLM ○ 未探测」 | 默认只做快速探测 | 状态页点「深度探测」（真实调用一次模型） |
| Agent 回答失败 / `llm_unavailable` | Ollama 未启动或模型未拉取 | `ollama serve`；`ollama list` 确认 `qwen2.5:7b-instruct` 与 `bge-m3` |
| 知识检索报 `index_model_mismatch` | 索引向量模型与配置不符 | 检查 `EMBEDDING_MODEL/VERSION`，必要时离线重建索引（本阶段不做） |
| Audit 页面 401 | 后端启用了 `AUDIT_API_TOKEN` | `IAA_AUDIT_TOKEN=<token> scripts/start_tui.sh` |
| Hybrid 审计很慢（约 18–40 s） | 本地 7B 模型 CPU 推理 | 预期行为；日常用 Rule Only，需要语义审计时再跑 Hybrid |
| 界面在小终端显示不全 | 终端过小 | 按 80×24 以上使用；表格与详情区支持滚动 |
| `httpx` 请求本机却返回 502 | macOS 系统 HTTP 代理（如 Clash）被 `trust_env` 采用 | 本项目客户端已设 `trust_env=False`；自写脚本请同样关闭 |

## 7. 停止与清理

- TUI：`Ctrl+Q`（会取消后台任务并关闭 HTTP 连接）。
- 后端：前台 `Ctrl+C`。
- 数据库：`scripts/db.sh down`（保留 named volume 数据；`docker volume rm` 会销毁 151 万行，禁止当作常规操作）。

## 8. 容器化范围

- 复用现有 `docker-compose.yml` 只跑 PostgreSQL + pgvector。
- Ollama 与 TUI 不容器化：TUI 需要真实终端（或 Textual 驱动），Ollama 需要本机模型缓存。
- 无反向代理、无 TLS、无鉴权网关：与「只在本机演示」的定位一致。

## 9. 可选：生成终端截图

```bash
python scripts/demo_phase5.py            # 完整演示（含 Hybrid，约 30–40 s）
python scripts/demo_phase5.py --skip-hybrid
```

会把 Textual 真实渲染结果保存为 SVG 到 `docs/figures/phase5/`，
可直接在 GitHub README 或本地浏览器中查看；截图内容全部来自本次真实运行。
