# Phase 2 报告：工业设备技术知识库、混合检索与 RAG 评估

- 执行日期：2026-10-22
- 范围：**知识库建设 + 混合检索 + 有证据的 RAG 问答 + 评估 + FastAPI 接口 + 测试 + 文档**
- 明确不包含：LangGraph Agent、多 Agent、Agent-Probe 审计、React 前端、真实设备控制
- 生产库状态：**仍为 `0004_coverage_missing_time`，本阶段未对其执行任何迁移或写入**
- 结论：Phase 2 已具备独立验收条件（§10），Phase 3 未启动（§11）

---

## 1. 开发前检查（实际状态）

| 检查项 | 实际结果 |
| --- | --- |
| FastAPI 现有接口 | 186 项 Phase 1B/1C 测试全部通过（`test_api_real_data` 因未配置全量副本而跳过） |
| PostgreSQL / pgvector | PostgreSQL 17.11，pgvector **0.8.7**，`pg_trgm 1.6` |
| 当前迁移版本 | 主库 `0004`；测试库 `0006` → 本阶段升到 `0007` |
| Service / Repository 结构 | Router → Service → Repository 分层完整，RAG 复用同一约定 |
| `knowledge/` 文档 | 只有空目录；`data/raw/metropt3/Data Description_Metro.pdf` 已存在且哈希已登记 |
| RAG 相关代码 | 无。`backend/app/rag/` 仅有 `.gitkeep` |
| Embedding / LLM 服务 | 本机 Ollama 可用，已有 `bge-m3`、`qwen2.5:7b-instruct`；**无外部 API Key** |

没有重复实现已有模块；本阶段所有新功能都建立在 Phase 1C 的会话、异常、Schema 与路由约定之上。

## 2. 环境与模型（真实调用，不是推测）

| 组件 | 实际版本/模型 |
| --- | --- |
| Python | 3.11.15（Conda `ms_pointcloud_midterm`） |
| PostgreSQL | 17.11（Docker，pgvector 0.8.7，pg_trgm 1.6） |
| PyMuPDF | 1.28.0 |
| SQLAlchemy / psycopg | 2.1.6 / 3.3.4 |
| pgvector（Python） | 0.4.2（本阶段新增） |
| Embedding | Ollama `bge-m3`，实测输出 **1024 维**（`/api/embed` 返回长度） |
| LLM | Ollama `qwen2.5:7b-instruct`（Q4_K_M） |
| 硬件 | Apple M5 Pro / 36 GB 统一内存 |

所有向量与回答都由真实模型生成；测试中的替身只用于单元/集成测试，**不用于验收结论**。

## 3. 数据库与迁移

### 3.1 三个库，互不串用

| 库 | 版本 | 内容 | 用途 |
| --- | --- | --- | --- |
| `industrial_agent_audit`（主库） | `0004`（未改动） | 1,516,948 条传感器记录、4 条故障 | 未连接写入 |
| `iaa_rag_dev`（隔离开发库） | `0007` | 6 文档 / 1,314 Chunk / 1,314 真实向量 | 本阶段端到端与评估 |
| `industrial_agent_audit_test`（测试库） | `0007` | 每个用例前 TRUNCATE | pytest（`conftest` 强制 `RAG_DATABASE_URL` 指向它） |

另外在空的临时库 `iaa_phase2_fresh_test` 上验证了 **0001 → 0007 全链路迁移**，
确认 CHECK 约束名、`vector(1024)` 与索引与 ORM 元数据一致，随后删除该临时库。

### 3.2 迁移 `0007_knowledge_rag`

- 新增 `documents`、`document_chunks`、`knowledge_ingest_runs`；
- 启用 `pg_trgm`；`vector` 由 0001 已启用；
- 索引：HNSW（`vector_cosine_ops`, m=16, ef_construction=64）、GIN(tsvector)、
  GIN(trgm)、`(embedding_model, embedding_version)`、`documents.doc_key` UNIQUE 等；
- `alembic check`：无漂移；
- **未触碰**任何工业传感器表；`downgrade` 只删除本阶段对象，不删除共享扩展。

开发中修复了一次迁移缺陷：CHECK 约束在迁移里传了全名，被命名约定二次加前缀
（`ck_documents_ck_documents_...`）。已在迁移中改为短名，并修复了本地 dev/test 库的约束名；
由于 `0007` 尚未部署到任何长期库，没有产生额外迁移版本或并行分支。

### 3.3 实测数据量

| 项 | 数量 |
| --- | ---: |
| 文档 | 6（131 页） |
| `structural-v1` Chunk | 750（平均 649 字符） |
| `fixed-v1` Chunk | 564（平均 859 字符） |
| 向量总数 | 1,314（全部 1024 维，`bge-m3`） |
| 入库任务记录 | 3（structural / fixed / 重复导入校验；状态均 `succeeded`） |

## 4. 文档与入库

来源、URL、SHA-256、许可与适用性见 [知识库来源](knowledge_sources.md)。摘要：

- 直接关联 MetroPT-3：2 份（数据集说明 3 页、Scientific Data 论文 8 页，均 CC BY 4.0）；
- 通用压缩空气资料：4 份（DOE Sourcebook 114 页 + 3 份 Tip Sheet，美国政府作品）。

入库实测（`scripts/ingest_knowledge.py`）：

| 阶段 | structural-v1 | fixed-v1 |
| --- | ---: | ---: |
| 解析 131 页 | 554 ms | 557 ms |
| 切分 | 9 ms | 16 ms |
| Embedding | 21.2 s（750 块） | 17.4 s（564 块） |
| 写库 | 1.3 s | 1.1 s |
| 合计 | 23.2 s | 19.2 s |

幂等与版本：
- 相同内容重复导入 → `skipped_unchanged`，Chunk 数不变（实测第二次 6/6 跳过，耗时 50 ms）；
- 内容变化 → `content_revision + 1`，旧 Chunk 删除后重建（集成测试覆盖）；
- 同一文档两套切分并存，互不覆盖（1,314 = 750 + 564）；
- 解析失败（无文字/扫描）→ 文档 `parse_status=failed`，**不写入任何 Chunk**；
- 文档中的疑似指令只标记 `metadata.suspected_prompt_injection`，原文不删改。

## 5. 混合检索

实现：向量（pgvector 余弦）+ 关键词（ASCII 精确包含 + 中文 bigram + tsvector 辅助）
+ RRF（k=60）。细节见 [RAG 设计](rag_design.md)。

评估（41 道可回答题，`bge-m3`）：

| 切分 | 方法 | Recall@5 | Hit@5 | MRR@10 | Precision@5 |
| --- | --- | ---: | ---: | ---: | ---: |
| structural-v1 | vector | 0.878 | 0.927 | 0.685 | 0.371 |
| structural-v1 | keyword | 0.561 | 0.585 | 0.486 | 0.258 |
| **structural-v1** | **hybrid** | **0.939** | **0.976** | **0.784** | **0.390** |
| fixed-v1 | vector | 0.829 | 0.854 | 0.647 | 0.278 |
| fixed-v1 | keyword | 0.561 | 0.585 | 0.468 | 0.245 |
| fixed-v1 | hybrid | 0.878 | 0.902 | 0.769 | 0.337 |

结论：混合检索在本库上稳定优于单路；结构感知切分优于固定切分；
纯关键词对中文语义问题几乎无效，但对 `TP2`、`acfm`、`DOE/EE-1340` 等标识符召回 100%。
完整分类别指标、失败案例与方法学限制见 [RAG 评估](rag_evaluation.md)。

## 6. RAG 问答

- 流程：混合检索 → 证据预算（12k 字符）→ Prompt（`rag-prompt-v1`）→ LLM JSON →
  `support_quotes` 逐字校验 → 引用装配 → 状态；
- 实测：52 题，37 题有证据回答、15 题证据不足、0 次模型调用失败；
- 引用标签合法率 **100%**；`support_quotes` 全部通过 61.5%（32/52）；
- 11 道必拒答题 **11/11 正确拒答**，**0** 条编造参数/型号/故障结论；
- 人工复核：有证据支持 42/52（80.8%），引用完全正确 36/52（69.2%）；
- 端到端平均 2.25 s/题（检索 123 ms + LLM 2.12 s）。

安全约束：
- 越界问题（天气/创作/编程/系统提示）不检索、不调用模型；
- 型号在证据中不存在（GA30、ASD 40）→ 确定性拒答，不拿通用资料顶替；
- 模型输出代码/SQL → 拒绝返回；
- 文档中的注入指令仅作为不可信文本，系统 Prompt 不被覆盖；
- LLM 超时/限流/不可达 → 返回真实状态 + 原文证据，不生成假答案。

## 7. FastAPI 接口

| 方法 | 路径 | 实测 |
| --- | --- | --- |
| POST | `/api/v1/knowledge/search` | 200；支持 3 种方法、Top-K、适用范围/类型/文档过滤 |
| POST | `/api/v1/knowledge/ask` | 200；返回回答、引用（文档+页码+Chunk ID）、证据、耗时、模型状态 |
| GET | `/api/v1/knowledge/documents` | 200；6 份文档与各切分版本 Chunk 数 |
| GET | `/api/v1/knowledge/health` | 200；`schema_ok=true`、1,314 Chunk、`bge-m3@bge-m3-1024-v1` |

接口在本地 `uvicorn app.main:app` 下可用；OpenAPI 已包含 4 个新路径，Phase 1C 的 8 个接口保持不变。
知识接口默认只读：请求结束统一 rollback；文档入库只通过离线 CLI。

## 8. 自动化测试

```
267 passed, 1 skipped in 11.75 s
覆盖率 91%（pytest-cov，整体 app/）
```

| 文件 | 数量 | 覆盖内容 |
| --- | ---: | --- |
| `test_rag_parsing.py` | 9 | 正常多页、页码映射、页眉页脚剔除、标题识别、单位/型号保留、单页失败、扫描件 failed、损坏 PDF、表格行 |
| `test_rag_chunking.py` | 9 | 章节边界、页码、重叠、容量预算、标题合并、两种策略、哈希、句子切分 |
| `test_rag_embedding_llm.py` | 12 | 维度校验、归一化、5xx/429/4xx/超时、LLM 超时/限流/不可达/空响应 |
| `test_rag_ingest.py` | 10 | 入库、幂等、版本更新、双切分并存、维度门禁、解析失败、注入标记、缺失文件 |
| `test_rag_retrieval.py` | 14 | 向量排序/范围过滤/模型版本、关键词标识符/中文 bigram、RRF 融合去重、切分版本隔离、无结果 |
| `test_rag_answer.py` | 17 | 引用校验、编造标签、`support_quotes`、标签重映射、无证据、型号不匹配、越界、代码拒绝、超时/不可达、注入防护 |
| `test_api_knowledge.py` | 10 | 参数校验、检索、问答、文档列表、健康检查、Phase 1C 回归 |

- 81 项为阶段 2 新增；原有 186 项（Phase 1B/1C）全部通过，**无回归**。
- `test_api_real_data.py` 因未设置 `IAA_FULLDATA_DATABASE_URL` 跳过（与阶段 1C 相同的条件）。
- 写入型测试全部在 `*_test` 库；`conftest` 强制把 `RAG_DATABASE_URL` 指向测试库，
  防止误写 `iaa_rag_dev` 或主库。

## 9. 实际新增与修改的文件

### 9.1 新增（后端）

```
backend/app/rag/__init__.py
backend/app/rag/config.py             # RAG 配置（模型/维度/切分/检索/上下文预算）
backend/app/rag/errors.py             # 可区分的解析/向量/检索/LLM 异常
backend/app/rag/http.py               # 带重试的 HTTP（429/5xx/超时/4xx 快速失败）
backend/app/rag/answer.py             # Prompt、证据选择、引用校验、越界与注入防护
backend/app/rag/ingest.py             # 解析→切分→向量→写库（幂等/版本/维度门禁）
backend/app/rag/parsing/pdf.py        # PyMuPDF 解析、页码、标题、页眉页脚
backend/app/rag/chunking/base.py      # fixed / structural 两种切分
backend/app/rag/embedding/{base,ollama,openai_compat}.py
backend/app/rag/llm/{base,ollama,openai_compat}.py
backend/app/rag/retrieval/{base,keyword,vector,hybrid}.py
backend/app/models/knowledge.py       # 3 张 RAG 表 ORM
backend/app/schemas/knowledge.py      # 请求/响应契约
backend/app/services/knowledge.py     # 检索/问答/文档/健康服务
backend/app/api/routes/knowledge.py   # 4 个知识库接口
backend/alembic/versions/0007_knowledge_rag.py
```

### 9.2 新增（脚本、数据、测试、文档）

```
scripts/fetch_knowledge_docs.py       # 下载 + SHA-256 核验（幂等）
scripts/ingest_knowledge.py           # 入库 CLI（dry-run / reindex / 隔离库保护）
scripts/evaluate_rag.py               # 检索对比 + 生成评估 + 人工标注汇总
knowledge/sources.json                # 6 份文档的来源清单
knowledge/eval/questions.json         # 52 题评估集（含预期页码与依据）
knowledge/eval/generation_manual_review.json
knowledge/eval/rag_eval_summary.json  # 真实评估结果（精简版）
backend/tests/{helpers_rag,test_rag_parsing,test_rag_chunking,test_rag_embedding_llm,
               test_rag_ingest,test_rag_retrieval,test_rag_answer,test_api_knowledge}.py
docs/rag_design.md
docs/knowledge_sources.md
docs/rag_evaluation.md
docs/phase2_report.md（本文件）
```

### 9.3 修改

```
backend/app/models/__init__.py        # 注册 RAG 模型
backend/app/api/app.py                # 注册 knowledge 路由
backend/app/api/app_meta.py           # 版本 2.0.0、知识库标签与描述
backend/app/api/errors.py             # RAG 错误 → HTTP 状态码
backend/app/api/deps.py               # 知识库会话依赖
backend/app/api/routes/health.py      # 期望 schema 版本 → 0007
backend/tests/conftest.py             # 测试库同时作为 RAG 库；清理 RAG 表
backend/requirements.txt              # 新增 pgvector==0.4.2
.env.example / .gitignore             # RAG 配置项；忽略 knowledge/raw/*.pdf
README.md / docs/architecture.md / docs/database_design.md / docs/roadmap.md
```

## 10. 验收标准对照

- [x] 至少一份真实且来源明确的工业技术文档成功入库（6 份，含 URL/SHA-256/许可）
- [x] PDF 文本与页码正确提取（131 页全部 `parsed`，页码单调且可回溯）
- [x] 文档切分合理，保留来源关系（Chunk → 文档/页码/章节/版本）
- [x] Embedding 真实生成成功（1,314 个 1024 维向量，`bge-m3`）
- [x] pgvector 检索正常（HNSW，实测 Recall@5 0.878）
- [x] 关键词检索正常（标识符 Recall@5 1.000）
- [x] 混合检索正常（Recall@5 0.939，优于两路单独）
- [x] RAG 可以回答有证据支持的问题（37 题回答；人工 80.8% 有证据支持）
- [x] 回答包含真实文档引用（文档 + 页码 + Chunk ID + 逐字引文）
- [x] 证据不足时不编造答案（11/11 必拒答题正确拒答，0 条禁用编造内容）
- [x] 型号不匹配时不会错误引用设备专属参数（GA30/GA37/ASD 40 确定性拒答）
- [x] FastAPI 知识检索接口正常（4 个接口，测试与手工验证）
- [x] 检索评估集已建立（52 题，人工标注预期证据页码）
- [x] 实际输出 Recall@5、MRR@10（见 §5）
- [x] 检索方法对比实验完成（2 种切分 × 3 种方法）
- [x] 新增测试通过（81 项新增，267 通过 / 1 条件跳过）
- [x] Phase 1C 原有功能无回归（186 项原有测试通过）
- [x] 生产工业数据未被修改（主库仍 `0004`，1,516,948 行，4 条故障）
- [x] Phase 2 开发报告完整（本文件 + 3 份专题文档）

**独立验收建议**：可以。验收时请重点抽查：
1. `python scripts/fetch_knowledge_docs.py --verify-only`（文档哈希）；
2. `python scripts/evaluate_rag.py --skip-generation`（检索指标复现，约 30 s）；
3. 抽 3–5 个问题手工调用 `/ask`，对照引用页码阅读原文；
4. 用 `GET /api/v1/knowledge/health` 确认索引模型与维度；
5. 确认主库 `alembic_version` 仍为 `0004`。

## 11. 遗留问题与限制

1. **多文档联合检索弱**：多文档题最高 Recall@5 0.625，需要查询分解/多轮检索（本阶段未做）。
2. **Chunk 级证据不足**：页面级命中不代表检索块内包含答案（Q15、Q41）；需要 Reranker 或
   section-aware 二次检索，本阶段按要求先验证混合检索，不引入 Reranker。
3. **中文问题 / 英文文档**：关键词检索几乎无效；若后续要求“中英文都能精确检索”，
   需要双语术语表或中文文档。
4. **引用逐字校验覆盖不足**：模型只有 61.5% 的输出提供了可验证的 `support_quotes`；
   未提供的引用只能靠人工复核。
5. **7B 模型不稳定**：存在错误拒答（Q15）和“不足以确认……但……”的保守句式（Q18/Q20）；
   需要更强模型或 few-shot/校验策略。
6. **评估方法学**：单评审人、52 题、无预注册阈值、无置信区间；页面级相关性是代理指标。
7. **解析限制**：无 OCR；跨页表格不重建；页眉检测要求“重复 + 位于页边”。
8. **未做**：Reranker、查询改写、多轮对话、文件上传 API、权限与鉴权、成本计量。
9. `test_api_real_data.py` 需要 `IAA_FULLDATA_DATABASE_URL` 才能运行全量真实数据回归；
   本次未配置，已在报告中如实标记为跳过。

## 12. 停止点

Phase 2 到此结束。**未创建** LangGraph Agent、Tool Calling、Agent-Probe、审计规则、
React 前端或任何设备控制代码；未对主库执行迁移；未修改 MetroPT-3 原始 CSV。
Phase 3 不在本次范围内，未启动。
