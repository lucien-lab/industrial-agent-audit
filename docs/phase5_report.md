# Phase 5 开发报告：终端 TUI、系统集成与项目交付

日期：2026-10-25 · 环境：Python 3.11.15 / Conda `ms_pointcloud_midterm` · 本机 macOS
范围：仅阶段 5。未新增 AI 算法，未重写 Agent / RAG / 审计引擎，未执行任何数据库迁移。

## 1. 交付概览

| 交付项 | 状态 | 位置 |
| --- | --- | --- |
| Textual 终端界面（6 页面） | 完成 | `backend/app/tui/` |
| 统一 API Client | 完成 | `backend/app/tui/client.py` |
| CLI 入口 `iaa-tui` / `python -m app.tui` | 完成 | `backend/app/tui/cli.py`、`backend/pyproject.toml` |
| 启动与检查脚本 | 完成 | `scripts/start_api.sh`、`start_tui.sh`、`check_system.sh` |
| 真实业务演示脚本 | 完成 | `scripts/demo_phase5.py` → `docs/figures/phase5/*.svg` |
| 测试 | 完成 | `backend/tests/test_tui_{client,app,status_cli,integration_real}.py` |
| 文档 | 完成 | `docs/{tui_design,deployment,demo_guide,phase5_report}.md` + README/architecture/roadmap |

## 2. 新增与修改文件

**新增（代码）**

```
backend/app/tui/__init__.py
backend/app/tui/__main__.py
backend/app/tui/cli.py
backend/app/tui/client.py
backend/app/tui/models.py
backend/app/tui/status.py
backend/app/tui/formatting.py
backend/app/tui/app.py
backend/app/tui/app.tcss
backend/app/tui/pages/__init__.py
backend/app/tui/pages/base.py
backend/app/tui/pages/chat.py
backend/app/tui/pages/equipment.py
backend/app/tui/pages/knowledge.py
backend/app/tui/pages/traces.py
backend/app/tui/pages/audit.py
backend/app/tui/pages/status.py
backend/app/tui/pages/modals.py
backend/pyproject.toml
```

**新增（测试）**

```
backend/tests/test_tui_client.py             18 项
backend/tests/test_tui_app.py                21 项
backend/tests/test_tui_status_cli.py         15 项
backend/tests/test_tui_integration_real.py    7 项（默认跳过）
```

**新增（脚本 / 文档 / 截图）**

```
scripts/start_api.sh         启动后端（不迁移、不导入）
scripts/start_tui.sh         启动 TUI（缺失 iaa-tui 时回退 python -m app.tui）
scripts/check_system.sh      只读检查：API、深度探测、四库 Alembic 版本
scripts/demo_phase5.py       真实端到端演示 + 终端截图导出
docs/tui_design.md
docs/deployment.md
docs/demo_guide.md
docs/phase5_report.md
docs/figures/phase5/*.svg           8 张真实终端截图
docs/figures/phase5/demo_transcript.txt
```

**修改**

```
README.md                  核心功能、技术栈（前端改为 Textual）、目录、状态、阶段 5 交付、TUI 用法、测试、指标、限制
docs/architecture.md       范围与边界、架构图、模块表、新增「TUI 实现（阶段 5）」、数据流参与者
docs/roadmap.md            阶段 5 状态与实测结果、阶段依赖
backend/requirements.txt   新增 textual==1.0.0、pytest-asyncio==1.4.0
backend/pytest.ini         新增 tui_e2e marker、asyncio fixture loop scope
```

**未改动**：`backend/app/{api,services,rag,agent,audit,models,schemas}/`、`backend/alembic/`、
`docker-compose.yml`、原始数据、知识库索引。后端未做任何为其适配 TUI 的重构。

## 3. TUI 技术架构

```
IaaTuiApp（Textual App，app.py）
├── 状态栏：probe_system_status() 并发探测 /health /ready /knowledge/health /audit/summary
├── 侧边导航（ListView）+ ContentSwitcher（6 个 BasePage 子类）
├── 状态行 + Footer（快捷键）
└── 页面 → 每页一个 Worker 组 → IaaApiClient（唯一网络出口）→ FastAPI
```

- **契约层**：`models.py` 只声明渲染必需字段（`extra="ignore"`），缺字段 → `ApiError(invalid_response)`。
- **状态层**：`status.py` 把多接口结果翻译成 `ready/degraded/unavailable/unknown`，
  `degraded` 判定完全来自后端（schema 不一致、索引缺失、探测失败）。
- **展示层**：`formatting.py` 统一配色与数值口径；`UNKNOWN` 有独立文案与颜色，
  与 `PASS` 不可能混淆。
- **异步**：`BasePage.launch()` 包装 `run_worker(..., exclusive=True)`；
  `cancel_tasks()` 只取消界面等待。
- **依赖边界**：`tests/test_tui_app.py::test_tui_modules_do_not_touch_database_directly`
  静态检查 `app/tui/**` 不含 `psycopg` / `sqlalchemy` 导入。

## 4. 实现的主要界面

| 页面 | 主要能力 | 复用接口 |
| --- | --- | --- |
| Agent Chat | 多行输入、Markdown 回答、工具明细、证据、trace_id 复制、跳转 Trace/审计 | `POST /api/v1/agent/chat` |
| Equipment | 设备列表、统计、质量、故障、采样 Sparkline（240 条上限、不插值） | `/equipment`、`/stats`、`/quality`、`/faults`、`/readings` |
| Knowledge | vector/keyword/hybrid、top_k、来源与页码、适用范围警示 | `POST /api/v1/knowledge/search`、`GET /knowledge/health` |
| Trace Explorer | 事件时间线、类型过滤、单事件入参/输出、证据清单状态 | `GET /api/v1/audit/traces/{id}`、回退 `GET /api/v1/agent/traces/{id}` |
| Audit Reports | 三模式审计、进度、5 类结论、规则明细、风险发现、人工复核 | `/audit/traces/{id}/evaluate`、`/runs`、`/runs/{id}`、`/findings/{id}/review` |
| System Status | 六组件状态、schema 版本、深度探测 Embedding/LLM | `/health`、`/ready`、`/knowledge/health`、`/audit/summary` |

## 5. API 集成情况

- 全部走 HTTP，无数据库直连；TUI 未新增任何后端接口，**只使用既有接口**。
- 后端接口契约无需改动：TUI 侧以 `extra="ignore"` 兼容未来新增字段。
- 唯一写操作是人工复核 `PATCH /api/v1/audit/findings/{id}/review`（只改复核状态），
  提交后重新读取 `/runs/{audit_id}` 展示持久化结果。
- 权限：审计接口沿用可选 `X-Audit-Token`；TUI 从 `IAA_AUDIT_TOKEN` 读取，不落日志。

## 6. 终端启动命令

```bash
conda activate ms_pointcloud_midterm
pip install -r backend/requirements.txt
pip install -e backend        # 生成 iaa-tui

scripts/start_api.sh          # 终端 1
scripts/start_tui.sh          # 终端 2（或 iaa-tui / cd backend && python -m app.tui）
```

启动过程检查 Python 包路径（`app` 可导入）、读取 `.env`、做一次 `GET /health` 预检；
预检失败时给出修复提示并返回 2（`--start-anyway` 可继续）。**不迁移数据库、不重建知识库、
不重新导入数据。**

## 7. 真实运行演示（实测）

脚本：`python scripts/demo_phase5.py`（完整约 30–45 s，含 Hybrid）。
完整控制台记录：`docs/figures/phase5/demo_transcript.txt`。

| 步骤 | 实测结果 |
| --- | --- |
| 系统状态 | API ready；工业库 **degraded**（`0004` ≠ `0008`）；RAG ready（6 文档 / 1,314 chunk）；Audit ready；Embedding/LLM 未探测 |
| Agent 问答 | `completed` / `mixed_analysis` / `llm`；服务端 6.8 s；6 次工具调用（含 1 次 `empty`）；15 条证据 |
| 回答数值 | Motor_current 6,301 样本：min 0.035 / max 6.36 / mean 0.4512 / std 1.302；样本覆盖率 0.7293；缺口 2 处 14,594 s；冻结候选 4,378 行（69.48%） |
| Trace | 13 条事件（run_start → node/intent/plan → 6× tool_call/tool_result → decision/answer/evidence/run_end），证据清单 `agent-evidence-v1` |
| Rule Only 审计 | 11 条规则全部 PASS，约 4 ms，`overall_risk=none` |
| Hybrid 审计 | 16 项结论（15 PASS / 1 N/A），耗时 23.79 s，Judge 5 项（4 pass + 1 n/a） |
| 历史回放 | trace `32885113…`：`R8_KNOWLEDGE_CITATION` = **UNKNOWN**（medium），界面显示 UNKNOWN 并归入「缺少证据」小节 |
| Knowledge | 5 条结果（3 条通用资料 + 2 条 MetroPT-3 直接资料），页码/chunk_id/RRF 分数齐全 |
| Equipment | 3 路传感器统计（6,301 样本）+ 质量（覆盖率 0.729 / 缺口 4.05 h / 冻结候选 4,378） |

全部数值均为本次运行的真实返回值；演示中唯一使用历史数据的两处（历史 Trace 与其审计结论）
在界面与文档中都标注为「历史回放」。

## 8. 新增测试与回归结果

```
$ cd backend && python -m pytest -q
561 passed, 14 skipped in 90.03s

$ python -m pytest -q --cov=app --cov-report=term
TOTAL  11389  1325  88%
app/tui 合计 2270 语句 / 386 未覆盖 → 83%

$ IAA_TUI_E2E=1 python -m pytest tests/test_tui_integration_real.py -q -m slow
7 passed in 38.25s
```

- 阶段 4 基线为 507 passed / 7 skipped；本阶段新增 54 项 TUI 单测，
  跳过的 14 项中 7 项是新增的真实端到端测试（需 `IAA_TUI_E2E=1`），其余 7 项为既有阶段 1–4 的真实数据/模型测试。
- **回归**：阶段 1B–4 全部测试仍通过，未修改被测后端代码。
- TUI 单测覆盖了任务要求的：正常启动、页面切换、输入问题、展示回答、展示工具、Trace 查看、
  审计模式切换、审计结果展示、异步期间界面可响应、网络失败提示、空数据提示、终端尺寸变化、退出与资源释放。

## 9. 验收标准逐项核对

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Textual TUI 正常启动 | ✅ | `test_app_starts_and_shows_degraded_schema_honestly` + 真实截图 |
| 一个明确命令启动 | ✅ | `iaa-tui`；`python -m app.tui`；`scripts/start_tui.sh` |
| 主界面布局清晰 | ✅ | 状态栏 + 侧栏 + 主区 + 状态行 + Footer；8 张截图 |
| 能输入自然语言问题 | ✅ | `TextArea` + `Ctrl+S`；`test_ask_question_renders_answer_tools_and_trace` |
| 能显示真实 Agent 回答 | ✅ | 真实端到端 `test_tui_agent_chat_against_real_llm`（真实 LLM + 数据） |
| 能查看工具执行信息 | ✅ | 工具摘要 + 可展开明细（入参/耗时/阶段/错误）；真实 6 次调用 |
| 能查询工业设备数据 | ✅ | Equipment 四标签页；真实端到端数值比对 |
| 能检索 RAG 知识库 | ✅ | Knowledge 三模式；真实端到端 chunk_id 序列比对 |
| 能浏览历史 Trace | ✅ | Trace Explorer + 「最近审计」下拉；真实 13 事件 |
| 能触发 Rule Only 审计 | ✅ | 默认模式；真实 11 条规则 PASS（约 4 ms） |
| 能触发 Hybrid 审计 | ✅ | 真实 16 项结论、23.79 s |
| 能显示审计结果和风险证据 | ✅ | 规则/发现表 + 详情（证据引用、关联事件、details JSON） |
| 异步任务不阻塞 TUI | ✅ | 单测切换页面 + 真实 Hybrid 期间切换页面 |
| 正确处理后端异常 | ✅ | Client 18 项（超时/404/422/503/401/无效 JSON/缺字段）；UI 错误提示与「不当作通过」 |
| 能查看服务状态 | ✅ | System Status 6 组件 + 深度探测 |
| 不直接操作数据库 | ✅ | 静态守卫测试；TUI 无 `psycopg`/`sqlalchemy` 导入 |
| 不重新实现 Agent/RAG | ✅ | 全部通过既有 HTTP 接口 |
| 不修改主库与原始数据 | ✅ | 无迁移；仅审计库新增审计/复核记录 |
| 新增测试通过 | ✅ | 54 项新增单测 + 7 项真实端到端 |
| 原有测试无回归 | ✅ | 561 passed（含 507 基线） |
| 完整业务演示通过 | ✅ | `scripts/demo_phase5.py` 全流程 + 截图 + 逐条记录 |
| README 与部署文档完整 | ✅ | README + deployment + demo_guide + tui_design |

## 10. 已知问题

1. **无流式输出**：后端不提供流式事件，工具过程在回答返回后一次性展示（不伪造实时进度）。
2. **取消语义有限**：`Ctrl+X` 只取消界面等待；后端无取消接口，服务端任务可能继续并写入 Trace。
3. **Hybrid 审计较慢**（17–25 s/条）：本地 7B Judge，属既有成本，未在本阶段优化。
4. **进程内 Trace 上限 200 条**：更早的 Trace 必须通过审计库/文件回放；无审计记录的旧 Trace 只能手填 trace_id。
5. **主库仍为 `0004`**：状态页显示 degraded；迁移 `0005/0006`（及 `0007/0008`）仍待人工批准。
6. **无认证**：审计/Trace/设备接口只适合本机或隔离网络；公网部署前需补齐鉴权。
7. **Textual 1.0 的 Markdown 组件不保留原文**，测试改用数据模型 + 渲染块文本断言（已在辅助函数处理）。
8. **小终端可用但不理想**：80×24 可操作，信息密度最佳在 150×46 左右。
9. **审计库 schema 版本未由 API 暴露**：状态页对 Audit 只给「可用 + 审计条数」，
   精确版本需 `scripts/check_system.sh`；未为了界面完整而编造版本号。

## 11. 当前数据库状态（本阶段结束时实测）

| 库 | 版本 | 说明 |
| --- | --- | --- |
| `industrial_agent_audit` | `0004_coverage_missing_time` | 未改动；代码期望 `0008` → 显示 degraded |
| `iaa_rag_dev` | `0007_knowledge_rag` | 未改动（6 文档 / 1,314 chunk） |
| `iaa_audit_dev` | `0008_agent_audit` | 新增演示/测试产生的审计运行与发现；一条手工复核测试记录已回滚为 `pending` |
| `industrial_agent_audit_test` | `0008_agent_audit` | 测试用例自带 TRUNCATE，未影响其它库 |

校验方式：`scripts/check_system.sh`（只读）。

## 12. 是否具备最终项目验收条件

**具备**，前提是验收方接受以下两条已知状态（二者都已在界面与文档中显式呈现，未隐藏）：

1. 主库迁移版本 `0004` 与代码期望 `0008` 不一致 —— 状态栏/状态页显示 `degraded`，
   TUI 不自动迁移；这不影响设备查询、知识检索、Agent 与审计功能（各自连接对应库）。
2. 阶段 2 未做独立验收（历史遗留），其指标仍为自测结果；阶段 5 只消费其检索能力，未改变该状态。

阶段 5 范围内：TUI 可一键启动、六页面可用、真实闭环演示通过、
新增 54 项单测 + 7 项真实端到端通过、全量 561 项无回归、文档齐备。

## 13. 复现命令

```bash
conda activate ms_pointcloud_midterm
scripts/check_system.sh                     # 只读检查
scripts/start_api.sh &                      # 或另开终端
python scripts/demo_phase5.py               # 真实演示 + 截图
IAA_TUI_E2E=1 python -m pytest backend/tests/test_tui_integration_real.py -q -m slow
cd backend && python -m pytest -q --cov=app
```
