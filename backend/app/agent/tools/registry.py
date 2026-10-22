"""工具注册表：名称 → ToolSpec。

约束：
- 客户端只能通过**工具名 + 结构化参数**调用，不能提交任意代码或任意 SQL；
- 注册表是唯一入口，未知工具直接拒绝（`UnknownTool`）；
- 每个工具有独立超时与最大返回条数，默认来自 `AgentSettings`。
"""

from __future__ import annotations

from typing import Any

from app.agent.config import AgentSettings
from app.agent.errors import UnknownTool
from app.agent.tools.base import ToolSpec
from app.agent.tools.data_tools import (
    EquipmentInput,
    FaultEventsInput,
    HistoryInput,
    QualityInput,
    StatisticsInput,
    get_data_quality,
    get_equipment_info,
    get_fault_events,
    get_sensor_history,
    get_sensor_statistics,
)
from app.agent.tools.knowledge_tools import KnowledgeSearchInput, search_technical_knowledge

# 工具名常量（避免各处硬编码字符串）
TOOL_EQUIPMENT_INFO = "get_equipment_info"
TOOL_SENSOR_HISTORY = "get_sensor_history"
TOOL_SENSOR_STATISTICS = "get_sensor_statistics"
TOOL_FAULT_EVENTS = "get_fault_events"
TOOL_DATA_QUALITY = "get_data_quality"
TOOL_KNOWLEDGE = "search_technical_knowledge"

DATA_TOOL_NAMES = (
    TOOL_EQUIPMENT_INFO,
    TOOL_SENSOR_HISTORY,
    TOOL_SENSOR_STATISTICS,
    TOOL_FAULT_EVENTS,
    TOOL_DATA_QUALITY,
)
KNOWLEDGE_TOOL_NAMES = (TOOL_KNOWLEDGE,)
ALL_TOOL_NAMES = DATA_TOOL_NAMES + KNOWLEDGE_TOOL_NAMES


def build_registry(settings: AgentSettings) -> dict[str, ToolSpec]:
    """构造（每次调用都新建的）工具注册表。"""
    specs = [
        ToolSpec(
            name=TOOL_EQUIPMENT_INFO,
            category="data",
            description=(
                "查询设备档案与观测时间跨度（名称、类型、说明、最早/最晚观测时间）。"
                "在一次分析中用于确认设备存在并给出数据可用范围。"
            ),
            input_model=EquipmentInput,
            impl=get_equipment_info,
            requires_equipment=True,
            timeout_s=settings.data_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
        ToolSpec(
            name=TOOL_SENSOR_HISTORY,
            category="data",
            description=(
                "查询指定设备、指定时间窗内的原始传感器记录（最多 200 行）。"
                "用于查看具体时刻的取值、质量标记与冻结候选行。"
            ),
            input_model=HistoryInput,
            impl=get_sensor_history,
            requires_equipment=True,
            max_return_items=settings.max_history_rows,
            timeout_s=settings.data_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
        ToolSpec(
            name=TOOL_SENSOR_STATISTICS,
            category="data",
            description=(
                "计算时间窗内传感器的统计量：模拟量 min/max/mean/std（stddev_samp），"
                "数字量 0/1 计数、占比与状态切换次数。所有聚合在数据库内完成。"
            ),
            input_model=StatisticsInput,
            impl=get_sensor_statistics,
            requires_equipment=True,
            timeout_s=settings.statistics_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
        ToolSpec(
            name=TOOL_FAULT_EVENTS,
            category="data",
            description=(
                "查询与时间窗相交的官方故障事件（区间级事实，来自数据集 Failure Information）。"
                "返回失败类型、严重程度、故障描述、维护说明、来源与核验状态。"
            ),
            input_model=FaultEventsInput,
            impl=get_fault_events,
            requires_equipment=True,
            max_return_items=settings.max_fault_events,
            timeout_s=settings.data_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
        ToolSpec(
            name=TOOL_DATA_QUALITY,
            category="data",
            description=(
                "查询时间窗内的采样质量：实际/应有样本数、样本与时间覆盖率、缺口数量与时长、"
                "冻结候选行数、窗口是否落在观测范围内及质量提示。"
            ),
            input_model=QualityInput,
            impl=get_data_quality,
            requires_equipment=True,
            timeout_s=settings.data_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
        ToolSpec(
            name=TOOL_KNOWLEDGE,
            category="knowledge",
            description=(
                "在 Phase 2 知识库中做混合检索（向量 + 关键词 RRF），返回文档名、文档 ID、"
                "Chunk ID、页码、片段与设备适用范围。仅检索原始证据，不调用 RAG 生成。"
            ),
            input_model=KnowledgeSearchInput,
            impl=search_technical_knowledge,
            requires_equipment=False,
            max_return_items=settings.max_knowledge_top_k,
            timeout_s=settings.knowledge_tool_timeout_s,
            max_retries=settings.tool_max_retries,
        ),
    ]
    return {spec.name: spec for spec in specs}


def get_tool_spec(registry: dict[str, ToolSpec], name: str) -> ToolSpec:
    spec = registry.get(name)
    if spec is None:
        raise UnknownTool(
            f"工具 {name!r} 未注册；可用工具：{sorted(registry)}", tool=name
        )
    return spec


def tool_catalog(settings: AgentSettings) -> list[dict[str, Any]]:
    """供 LLM 规划器与文档使用的工具目录（不含实现细节）。"""
    return [spec.to_dict() for spec in build_registry(settings).values()]


__all__ = [
    "ALL_TOOL_NAMES",
    "DATA_TOOL_NAMES",
    "KNOWLEDGE_TOOL_NAMES",
    "TOOL_DATA_QUALITY",
    "TOOL_EQUIPMENT_INFO",
    "TOOL_FAULT_EVENTS",
    "TOOL_KNOWLEDGE",
    "TOOL_SENSOR_HISTORY",
    "TOOL_SENSOR_STATISTICS",
    "build_registry",
    "get_tool_spec",
    "tool_catalog",
]
