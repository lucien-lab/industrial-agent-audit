# 阶段 5：工业智能运维 Agent TUI 设计

本文件描述终端界面（Textual TUI）的设计取舍、页面行为、API 集成方式、
异步与取消语义、降级规则和测试策略。所有数值均为本机实测结果。

> 边界：TUI 是**只读客户端**。它不直连数据库、不重新实现 RAG / Agent / 审计引擎、
> 不自动迁移数据库、不修改原始工业数据。所有展示内容来自真实 HTTP 响应。

## 1. 目标与非目标

**目标**

1. 一个命令启动（`iaa-tui` / `python -m app.tui`）即可在终端完成
   「提问 → 看回答 → 看工具与证据 → 看 Trace → 发起审计 → 看风险报告」闭环。
2. 复用阶段 1C–4 已实现的接口，不复制业务逻辑。
3. 界面诚实：状态 `degraded` 就显示 `degraded`，`UNKNOWN` 审计结论绝不显示为 `PASS`，
   未探测的服务显示「未探测」而不是「正常」。
4. 网络请求全部异步，界面在 Agent / LLM Judge 执行期间保持可响应。

**非目标**（本阶段明确不做）

- React / Vue / Next.js 等 Web 前端，不引入 Node 构建链；
- 流式 Token 输出（后端当前不提供流式事件，所以不做假的实时工具过程）；
- 在 TUI 内触发数据库迁移、知识库重建、数据重新导入；
- 多用户、权限体系、远程部署。

## 2. 技术选型

| 项目 | 选择 | 理由 |
| --- | --- | --- |
| TUI 框架 | Textual 1.0.0 + Rich 15.0.0 | 与现有 Python 栈一致；原生异步 Worker、滚动容器、数据表格、CSS 布局、headless 测试驱动 |
| HTTP 客户端 | httpx 0.28.1（`AsyncClient`） | 已在依赖中；支持 Transport 替换（测试用 MockTransport）与时序控制 |
| 契约校验 | Pydantic 2.13 | 与后端一致；只校验 TUI 渲染必需的字段，其余忽略以兼容后端演进 |
| 包入口 | `backend/pyproject.toml` + `pip install -e backend` | 提供真实的 `iaa-tui` console script，同时保留 `python -m app.tui` |

新增依赖仅 `textual`（含 `linkify-it-py`、`mdit-py-plugins`、`platformdirs` 间接依赖）
与测试用 `pytest-asyncio`。版本约束：`textual>=1.0,<2.0`。

## 3. 目录结构

```
backend/app/tui/
├── __init__.py
├── __main__.py        # python -m app.tui
├── cli.py             # iaa-tui 入口：参数、启动前健康检查、--check 模式
├── client.py          # 统一 API Client（唯一网络出口）
├── models.py          # TUI 侧 Pydantic 响应契约（只含渲染必需字段）
├── status.py          # 多接口状态聚合：ready / degraded / unavailable / unknown
├── formatting.py      # 状态配色、数值格式、Sparkline、事件详情 Markdown
├── app.py             # Textual App：状态栏、导航、页面调度、页面间跳转
├── app.tcss           # Textual CSS（深色主题、三栏布局、小终端适配）
└── pages/
    ├── base.py        # 页面基类：消息、Worker、取消、卸载
    ├── chat.py        # Agent Chat
    ├── equipment.py   # Equipment
    ├── knowledge.py   # Knowledge
    ├── traces.py      # Trace Explorer
    ├── audit.py       # Audit Reports（Agent-Probe）
    ├── status.py      # System Status
    └── modals.py      # 确认框 / 快速切换 / 帮助
```

## 4. 界面布局

```
┌ Industrial Agent Audit · 工业智能运维 Agent ─────────────────────────────────────┐
│ API ● 正常 │ 工业库 ◐ 降级 │ RAG ● 正常 │ Audit ● 正常 │ LLM ○ 未探测 │ 探测 09:00:11 │
├──────────────┬──────────────────────────────────────────────────────────────────┤
│ 功能导航      │ 当前页面                                                          │
│ 1 · Agent Chat│                                                                  │
│ 2 · Equipment │                                                                  │
│ 3 · Knowledge │                                                                  │
│ 4 · Trace     │                                                                  │
│ 5 · Audit     │                                                                  │
│ 6 · Status    │                                                                  │
│ 快捷键提示     │                                                                  │
├──────────────┴──────────────────────────────────────────────────────────────────┤
│ 状态行：最近一次操作结果 / 错误信息                                                 │
│ Footer：Ctrl+K 切换  Ctrl+R 刷新  Ctrl+L 清空  F1 帮助  Ctrl+Q 退出                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- 深色主题，无动画；表格与详情区都可滚动。
- 默认 150×46 截图见 `docs/figures/phase5/*.svg`；最小可用尺寸按 80×24 验证
  （测试 `test_terminal_resize_keeps_app_usable`）。

## 5. 快捷键

| 按键 | 行为 | 说明 |
| --- | --- | --- |
| `1`–`6` | 切换页面 | 焦点在输入框时不生效（避免与输入数字冲突），此时用侧栏或 `Ctrl+K` |
| `Ctrl+K` | 快速切换弹窗 | 列表选择页面，`Esc` 关闭 |
| `Ctrl+R` | 刷新当前页面 | 重新读取后端数据 |
| `Ctrl+L` | 清空当前页面视图 | 只清空界面，不修改服务端数据 |
| `Ctrl+S` | 当前页面主操作 | Chat 发送 / Equipment 查询 / Knowledge 检索 / Trace 加载 / Audit 发起审计 |
| `Ctrl+X` | Chat 取消界面等待 | 见 §8「取消语义」 |
| `F1` | 帮助弹窗 | 快捷键、页面说明、限制 |
| `Esc` | 关闭弹窗 | 含确认框（等价于取消） |
| `Tab` / `Shift+Tab` | 控件焦点切换 | Textual 默认 |
| `Ctrl+Q` | 退出 | 取消后台 Worker 并关闭 HTTP 连接 |
| `Ctrl+C` | Textual 默认中断处理 | 会提示如何退出；在输入框内为复制 |

多行输入使用 `TextArea`：**Enter 换行**，`Ctrl+S` 发送，避免与多行编辑冲突。

## 6. 页面设计

### 6.1 Agent Chat（核心页面）

| 交互 | 行为 |
| --- | --- |
| 发送 | `POST /api/v1/agent/chat`（可选 `equipment_id` / 时间窗 / 传感器，默认不传由后端判定） |
| 渲染 | 用户消息 → Agent 回答（Markdown，`textual.widgets.Markdown`）→ 工具摘要 → 可展开「工具调用明细」→ 可展开「证据」→ 执行元信息 |
| 工具 | 摘要行形如 `工具：✓ get_sensor_statistics (8ms)`；明细表格含阶段、耗时、入参 JSON、错误分类 |
| 证据 | `kind=data` 显示事实与来源；`kind=knowledge` 显示 doc_key、页码、chunk_id、逐字摘录与适用范围 |
| 元信息 | 执行状态、意图、回答方式、轮次、服务端耗时、端到端耗时、接地校验、trace_id、agent 版本 |
| 后续动作 | 「打开 Trace」「发起审计」把当前 trace_id 带到对应页面；「复制 Trace ID」「复制回答」走系统剪贴板（OSC 52） |

不伪造流式过程：本次 API 不返回流式事件，工具列表在执行结束后按真实响应展示。

### 6.2 Equipment

- 设备选择来自 `GET /api/v1/equipment`；默认时间窗由设备的真实观测范围推导
  （`last_observed_at` 当天与前一日），非硬编码。
- 四个标签页：统计（`/stats`）、质量（`/quality`）、故障（`/faults`）、采样（`/readings`）。
- 采样只取前 240 条（接口硬上限 1000），Sparkline 用真实值绘制，
  `None` 用空格占位——**不插值**；并标出 min/max/返回条数/limit。
- 质量页固定提示：`frozen_candidate`（冻结候选）不等于设备故障；
  缺失样本数是估计值；覆盖率 < 0.95 或 > 1 时给出说明。

### 6.3 Knowledge

- 输入问题、选择 `hybrid / vector / keyword`、`top_k`（1–20）。
- 结果表格：排名、分数、文档、页码、适用性、chunk_id。
- 选中行显示：文档标题、doc_key、类型、可信级别、页码、chunk_id、分数（明确 RRF 与余弦不是同一量纲）、
  适用范围警示（`generic_compressor` → 「同类设备通用资料，不是 MetroPT-3 参数」）、原文片段。

### 6.4 Trace Explorer

- 数据来源优先级：`GET /api/v1/audit/traces/{trace_id}`（受控回放，含规范化事件、工具时间线、
  证据清单）→ 失败时回退 `GET /api/v1/agent/traces/{id}?include_events=true`（进程内 Trace）。
- 左侧事件表（事件 ID / 类型 / 节点 / 工具 / 状态 / 耗时），右侧单事件详情
  （入参 JSON、输出 JSON、证据引用、错误分类）。支持按事件类型过滤。
- 「最近审计」下拉来自 `GET /api/v1/audit/runs`，可直接选择历史 trace。
- 顶部元信息显示来源、schema 版本、证据清单状态；**旧 Trace 缺 evidence manifest 时明确标注**，
  并说明依赖证据的规则会返回 UNKNOWN。TUI 不读取服务器文件路径，只使用 trace_id。

### 6.5 Audit Reports（Agent-Probe）

- 默认模式 `Rule Only`（毫秒级）；`LLM Only` / `Hybrid` 明确标注耗时（约 18–40 s）。
- 发起审计 → 进度行显示已等待秒数；期间可切换页面（真实 LLM Judge 不阻塞事件循环）。
- 结果区分五类结论：`PASS / FAIL / UNKNOWN / NOT_APPLICABLE / PARTIAL`（外加接口 `status`），
  颜色区分，`UNKNOWN` 用黄色并单独列在「UNKNOWN（缺少证据，不能视为通过）」小节。
- 左栏三个标签页：风险发现、规则结果（11 条规则的逐条结论）、历史运行。
- 人工复核：选中发现 → 「确认复核 / 驳回复核」→ 确认弹窗 → `PATCH /api/v1/audit/findings/{id}/review`
  → 重新 `GET /runs/{audit_id}` 读取服务端持久化结果（不本地猜测状态）。
- 审计失败（LLM 不可用等）明确写入详情区：「审计失败不会被当作『通过』」。

### 6.6 System Status

| 组件 | 数据来源 | ready 条件 |
| --- | --- | --- |
| API | `GET /health` | 2xx |
| 工业库 | `GET /ready` | `status=ok` 且 `schema_ok=true` |
| RAG | `GET /api/v1/knowledge/health` | `status=ok`（schema 与索引匹配、文档非空） |
| Audit | `GET /api/v1/audit/summary` | 2xx（401 → degraded，要求配置 Token） |
| Embedding | knowledge health `embedding.probe` | 仅「深度探测」后才判定，否则 `unknown` |
| LLM | knowledge health `llm.probe` | 同上；另外真实 Agent 回答成功后置为可用（有真实证据） |

「深度探测」按钮会真实调用一次 Embedding 与 `qwen2.5:7b-instruct`（实测 164 ms / 238 ms）。

## 7. 统一 API Client

`app/tui/client.py` 是 TUI 唯一的网络出口。

| 关注点 | 实现 |
| --- | --- |
| 超时 | 普通查询 30 s、知识检索/健康 90 s、Agent 问答 300 s、审计（含 Judge）600 s；启动前探测可覆盖 |
| 错误 | 统一 `ApiError`，保留后端 `{"error":{code,message,details,request_id}}`；`kind` ∈ `http/timeout/network/invalid_response` |
| 重试 | **只对幂等 GET** 在连接错误 / 超时 / 502 / 503 / 504 上重试，指数退避（0.25 s、0.5 s，最多 2 次）；POST / PATCH 从不自动重试 |
| 契约 | 必需字段缺失或非 JSON → `invalid_response`，界面显示为错误而不是渲染空值 |
| Token | `X-Audit-Token` 只加在 `/api/v1/audit/*`；不进日志、不进 `repr`、不进界面 |
| 代理 | `trust_env=False`，避免本机系统 HTTP 代理劫持 127.0.0.1 请求（实测 macOS 系统代理会导致 `httpx.get` 返回 502） |
| 分页 | `limit/offset` 原样透传，列表参数按重复查询参数编码 |

不复用后端 `app.schemas`：TUI 只需要渲染字段，导入后端 Schema 会把数据库/服务层依赖
带进客户端进程。字段语义与单位仍以后端为准，`docs/` 与接口文档是唯一口径来源。

## 8. 异步与取消语义

- 所有请求在 Textual async Worker 中执行（`run_worker(..., exclusive=True)`），
  每个页面一个 Worker 组，新请求取消同组旧请求。
- 页面被切换时不阻塞：Worker 挂在页面上，界面继续响应按键与页面切换。
- **取消只取消界面等待**：后端没有取消接口，取消后服务端任务可能继续执行并写入 Trace。
  界面在状态行与对话区都明确写出这一点，不宣称「已取消服务器任务」。
- 页面卸载时取消本页 Worker；应用退出时取消全部 Worker 并关闭 HTTP 连接。

## 9. 降级矩阵

| 故障 | 表现 | 仍可用的功能 |
| --- | --- | --- |
| 后端不可达 | 启动前健康检查失败 → 明确提示 + 返回码 2（`--start-anyway` 可进入界面看到 unavailable） | — |
| 工业库 schema 不一致（当前 `0004` ≠ `0008`） | 状态栏与状态页显示「工业库 ◐ 降级」+ 具体版本号 | 设备查询、Trace、审计（它们分别连各自库） |
| LLM / Embedding 不可用 | Agent 回答降级为 deterministic 或失败提示；Audit 的 Judge 返回 UNKNOWN | 设备查询、知识关键词检索、Rule Only 审计、历史审计 |
| 审计库不可用 | Audit 页面报错，Trace 页面仍可用进程内 Trace 回退 | Agent Chat、Equipment、Knowledge |
| 审计 Token 缺失/错误 | Audit 状态 degraded + 提示设置 `IAA_AUDIT_TOKEN` | 其它页面 |

主库迁移版本不一致**不会**被 TUI 自动修复：状态页给出提示，迁移仍需人工执行
`scripts/migrate.sh`（本阶段未执行）。

## 10. 测试策略

| 层次 | 文件 | 数量 | 说明 |
| --- | --- | --- | --- |
| API Client 单测 | `tests/test_tui_client.py` | 18 | MockTransport：正常/连接失败/超时/404/422/503/401/无效 JSON/缺字段/Token/分页/重试策略 |
| TUI 单测 | `tests/test_tui_app.py` | 21 | Textual `run_test`：启动、导航、问答、工具、Trace、审计模式与结果、异步响应性、取消、网络失败、空数据、尺寸变化、退出释放、静态守卫（TUI 不 import 数据库驱动） |
| 状态与 CLI 单测 | `tests/test_tui_status_cli.py` | 15 | 状态聚合（含 401 / 不可达 / 深度探测）、CLI 配置优先级、`--check`、启动失败返回码 |
| 真实端到端 | `tests/test_tui_integration_real.py` | 7（默认跳过） | `IAA_TUI_E2E=1`：真实 Agent、真实 Trace、Rule Only、Hybrid、真实 RAG、真实传感器数据、状态一致性 |

真实端到端测试断言的是与后端「直接调用」结果的一致性（例如知识检索 chunk_id 序列、
统计值与表格显示一致），避免出现「界面自己编一套数据」的可能。

## 11. 安全与使用限制

- 接口没有完整认证与权限控制：**只允许在本机或隔离网络演示**；
  `iaa-tui` 在 `--base-url` 非本机地址时会打印警告。
- TUI 不显示连接串、密码、Token；Trace / 审计响应本身已是白名单字段。
- 审计是离线可观测性审计，不是运行时拦截：不会阻止已经发生的 Agent 行为。
- 冻结候选不是故障标签；通用压缩机资料不是本设备参数；RRF 分数不是概率。

## 12. 已知限制

1. 无流式输出：Agent 回答与工具链在执行结束后一次性展示（后端无流式接口）。
2. 取消不终止服务端任务（后端无取消接口）。
3. 进程内 Trace 只保留最近 N 条；更早的 Trace 依赖审计库或 Trace 文件才能回放。
4. 「最近审计」下拉只列出审计库中已有的 trace；未审计过的新 trace 需手动粘贴 trace_id。
5. 主库停在 `0004`，状态页显示 degraded；需要迁移 `0005/0006` 才能与代码期望的 `0008` 一致
   （迁移需要单独批准，本阶段未执行）。
6. Textual 1.0 的 `Markdown` 组件不保留原始 markdown 文本，因此测试断言以数据模型 +
   渲染块文本为准（已在测试辅助函数中处理）。
