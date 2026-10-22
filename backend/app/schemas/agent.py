"""Agent API 契约（阶段 3）。

校验要点：
- `question` 长度上限 500（与 Phase 2 的问答一致），服务层会再次校验；
- `equipment_id` 只允许白名单字符集（不允许注入尝试）；
- 时间参数只接受**无时区** ISO 8601，实际解析复用 Phase 1C 的校验函数；
- `sensors` 只能命中 Phase 1C 的传感器白名单；
- 客户端**不能**提交工具名、SQL 或任意代码：工具由服务端规划器选择。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.catalog import ALL_SENSORS

ExecutionStatus = Literal[
    "completed",
    "insufficient_evidence",
    "refused",
    "failed",
    "budget_exhausted",
    "timeout",
]
IntentKind = Literal[
    "knowledge_only",
    "data_only",
    "mixed_analysis",
    "out_of_scope",
    "unsupported",
    "unknown",
]

_NAIVE_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$"


class AgentChatRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        description="自然语言问题（中文即可，可包含日期、测点与“是否异常”等意图词）",
        examples=[
            "这台空气压缩机在 2020 年 4 月 17 日的电流和压力是否异常？"
            "结合公开技术资料，可能需要关注哪些问题？"
        ],
    )
    equipment_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
        description=(
            "设备逻辑编号（可选）。省略时使用服务端默认设备 "
            "`metropt3-apu-01`（数据集中唯一的逻辑设备编号），并在回答的执行说明中注明。"
        ),
        examples=["metropt3-apu-01"],
    )
    start_time: str | None = Field(
        default=None,
        max_length=32,
        pattern=_NAIVE_TIME_PATTERN,
        description="默认时间窗起点（含）。问题中出现日期时以问题为准。无时区。",
        examples=["2020-04-17T00:00:00"],
    )
    end_time: str | None = Field(
        default=None,
        max_length=32,
        pattern=_NAIVE_TIME_PATTERN,
        description="默认时间窗终点（不含）。必须与 start_time 同时提供。无时区。",
        examples=["2020-04-18T00:00:00"],
    )
    sensors: list[str] | None = Field(
        default=None,
        max_length=15,
        description=f"关注的传感器列名（可选），可选值：{list(ALL_SENSORS)}",
        examples=[["motor_current", "dv_pressure"]],
    )
    planner: Literal["rule", "llm"] | None = Field(
        default=None,
        description=(
            "工具规划器：rule（默认，确定性规则）/ llm（让模型在注册表内选工具，用于对比实验）。"
            "两者都受同一套预算、参数校验与证据校验约束。"
        ),
    )
    include_trace: bool = Field(
        default=False,
        description="是否在响应中附带本次执行的 Trace 摘要（默认不返回，可用 GET traces/{id} 单独查询）",
    )

    @model_validator(mode="after")
    def _check_time_pair(self) -> "AgentChatRequest":
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time 与 end_time 必须同时提供或同时省略")
        return self

    @field_validator("sensors")
    @classmethod
    def _check_sensors(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [name for name in value if name not in ALL_SENSORS]
        if unknown:
            raise ValueError(f"不支持的传感器：{unknown}；可选值：{list(ALL_SENSORS)}")
        return value

    @field_validator("question")
    @classmethod
    def _clean_question(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("question 不能为空")
        return text

    model_config = ConfigDict(
        # 只接受白名单字段：客户端不能提交 tools / sql / tool_call 等任意内容
        extra="forbid",
        json_schema_extra={
            "example": {
                "question": "2020 年 4 月 17 日电机电流的最大值是多少？",
                "equipment_id": "metropt3-apu-01",
                "start_time": None,
                "end_time": None,
                "planner": "rule",
            }
        }
    )


class ToolCallOut(BaseModel):
    tool: str = Field(description="工具名")
    params: dict[str, Any] = Field(default_factory=dict, description="工具实际入参（已校验）")
    status: Literal["ok", "empty", "error"] = Field(description="ok/empty/error（empty 是合法空结果）")
    attempts: int = Field(description="尝试次数（含重试）")
    elapsed_ms: float = Field(description="本次调用耗时")
    reason: str | None = Field(default=None, description="为什么调用这个工具（规划理由）")
    stage: str = Field(default="initial", description="initial=计划内；followup=运行中补充查询")
    error: dict[str, Any] | None = Field(default=None, description="失败时的错误分类与可公开信息")
    evidence_count: int = Field(default=0, description="该工具产生的证据条数")

    model_config = ConfigDict(extra="ignore")


class EvidenceOut(BaseModel):
    """证据条目：`kind=data` 为数据事实，`kind=knowledge` 为文档片段。"""

    label: str = Field(description="证据标签，如 D1 / K1，供回答中的引用使用")
    kind: Literal["data", "knowledge"]
    fact_type: str | None = Field(default=None, description="数据事实类型（equipment/statistics/quality/fault/...）")
    text: str | None = Field(default=None, description="数据事实的自然语言描述（含精确数值）")
    source: dict[str, Any] | None = Field(default=None, description="数据来源（工具、Service、表、时间窗）")
    chunk_id: int | None = Field(default=None, description="document_chunks.id")
    document_id: str | None = None
    doc_key: str | None = None
    document_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    equipment_scope: str | None = Field(default=None, description="metropt3_direct / generic_compressor 等")
    trust_level: str | None = None
    source_url: str | None = None
    citation_label: str | None = Field(default=None, description="如 metropt3-data-description:p2")
    score: float | None = None
    vector_similarity: float | None = None
    keyword_score: float | None = None
    quote: str | None = Field(default=None, description="文档逐字摘录（未改写）")

    model_config = ConfigDict(extra="ignore")


class AgentChatResponse(BaseModel):
    trace_id: str = Field(description="本次执行标识，可用于 GET /api/v1/agent/traces/{trace_id}")
    question: str
    answer: str = Field(description="最终回答（Markdown，含数据依据、知识依据、来源与不确定性说明）")
    execution_status: ExecutionStatus = Field(
        description=(
            "completed=有证据回答；insufficient_evidence=证据不足；refused=越界/注入请求被拒；"
            "failed=内部失败；budget_exhausted=预算耗尽；timeout=整轮超时"
        )
    )
    intent: IntentKind = Field(description="意图分类结果")
    answer_status: Literal["answered", "insufficient_evidence", "refused", "failed"] = Field(
        description="回答状态（answer 字段是否给出结论）"
    )
    answer_mode: Literal["llm", "deterministic", "refusal", "none"] = Field(
        description="回答生成方式：llm=模型叙述通过校验；deterministic=确定性渲染；refusal=拒答"
    )
    tools_used: list[str] = Field(default_factory=list, description="实际执行过的工具（按首次调用顺序去重）")
    tool_calls: list[ToolCallOut] = Field(default_factory=list, description="全部工具调用明细")
    plan: list[dict[str, Any]] = Field(default_factory=list, description="计划（含运行中补充查询）")
    evidence: list[EvidenceOut] = Field(default_factory=list, description="被引用的证据")
    citations: list[EvidenceOut] = Field(default_factory=list, description="引用列表（同 evidence）")
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    grounding: dict[str, Any] = Field(
        default_factory=dict,
        description="接地校验结果：数值违规、引用被拒原因、是否通过",
    )
    llm: dict[str, Any] = Field(default_factory=dict, description="模型调用状态与耗时（不含提示词全文）")
    facts: dict[str, Any] = Field(default_factory=dict, description="事实清单与知识片段（结构化证据）")
    sections: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "回答的分节内容：conclusion（模型叙述的结论）/ data（数据事实）/ knowledge（知识要点与逐字摘录）"
            "/ inference（推断）/ uncertainties（不确定性）/ mode（llm 或 deterministic）"
        ),
    )
    timings_ms: dict[str, float] = Field(default_factory=dict)
    total_duration_ms: float = Field(description="本次执行总耗时")
    rounds: int = Field(default=0, description="决策轮次")
    agent_version: str
    prompt_version: str = ""
    trace: dict[str, Any] | None = Field(
        default=None, description="仅当 include_trace=true 时返回的 Trace 摘要"
    )

    model_config = ConfigDict(extra="ignore")


class AgentTraceResponse(BaseModel):
    trace_id: str
    question: str
    execution_status: str
    intent: str
    tools_used: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    rounds: int = 0
    total_duration_ms: float = 0.0
    started_at: str
    ended_at: str
    evidence_refs: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    event_count: int = 0
    events: list[dict[str, Any]] | None = Field(
        default=None, description="仅当 include_events=true 时返回完整事件流"
    )

    model_config = ConfigDict(extra="ignore")


__all__ = [
    "AgentChatRequest",
    "AgentChatResponse",
    "AgentTraceResponse",
    "EvidenceOut",
    "ExecutionStatus",
    "IntentKind",
    "ToolCallOut",
]
