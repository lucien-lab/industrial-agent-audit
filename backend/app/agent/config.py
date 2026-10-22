"""Agent 运行配置（全部来自环境变量 / 项目根 `.env`）。

设计原则：
- 预算类参数（步数、工具调用次数、超时）必须显式、可配置、有硬上限；
- 缺省值是“第一版默认配置”，不是调优结论；评估后可调整并记录在报告里；
- 源码内不保存任何凭据；连接串仍由 `DATABASE_URL` / `RAG_DATABASE_URL` 提供。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import PROJECT_ROOT, load_env_file

PLANNERS = ("rule", "llm")
ANSWER_MODES = ("auto", "llm", "deterministic")


class AgentConfigError(ValueError):
    """Agent 配置不合法（启动即失败，不静默回退）。"""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgentConfigError(f"{name} 必须是整数，当前为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise AgentConfigError(f"{name} 必须 >= {minimum}，当前为 {value}")
    return value


def _env_float(name: str, default: float, *, positive: bool = True) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise AgentConfigError(f"{name} 必须是数字，当前为 {raw!r}") from exc
    if positive and value <= 0:
        raise AgentConfigError(f"{name} 必须 > 0，当前为 {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise AgentConfigError(f"{name} 必须是布尔值，当前为 {raw!r}")


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env(name, default).lower()
    if value not in choices:
        raise AgentConfigError(f"{name} 必须是 {choices} 之一，当前为 {value!r}")
    return value


@dataclass(frozen=True)
class AgentSettings:
    """Agent 预算、超时与行为开关（不可变，便于测试注入）。"""

    # 预算
    max_tool_calls: int = 8
    max_rounds: int = 5
    max_graph_steps: int = 24
    total_timeout_s: float = 180.0
    data_tool_timeout_s: float = 20.0
    statistics_tool_timeout_s: float = 40.0
    knowledge_tool_timeout_s: float = 60.0
    tool_max_retries: int = 1

    # 输入与返回量限制
    max_question_chars: int = 500
    max_history_rows: int = 200
    max_fault_events: int = 20
    max_knowledge_chunks: int = 5
    max_knowledge_top_k: int = 8
    max_evidence_chars: int = 9000

    # 行为
    planner: str = "rule"
    answer_mode: str = "auto"
    strict_numeric_grounding: bool = True
    default_equipment_code: str = "metropt3-apu-01"
    default_year: int = 2020

    # Trace
    trace_dir: Path = PROJECT_ROOT / "data" / "processed" / "agent" / "traces"
    trace_memory_size: int = 200
    trace_write_files: bool = True

    def tool_timeout(self, tool_name: str) -> float:
        if tool_name == "get_sensor_statistics":
            return self.statistics_tool_timeout_s
        if tool_name == "search_technical_knowledge":
            return self.knowledge_tool_timeout_s
        return self.data_tool_timeout_s


def get_agent_settings(env_file: Path | str | None = None) -> AgentSettings:
    """读取 Agent 配置（缺省值即第一版默认预算）。"""
    load_env_file(env_file)

    trace_dir = Path(_env("AGENT_TRACE_DIR", str(PROJECT_ROOT / "data/processed/agent/traces")))
    if not trace_dir.is_absolute():
        trace_dir = (PROJECT_ROOT / trace_dir).resolve()

    max_tool_calls = _env_int("AGENT_MAX_TOOL_CALLS", 8, minimum=1)
    if max_tool_calls > 32:
        raise AgentConfigError("AGENT_MAX_TOOL_CALLS 硬上限为 32（防止无界循环）")
    max_rounds = _env_int("AGENT_MAX_ROUNDS", 5, minimum=1)
    if max_rounds > 20:
        raise AgentConfigError("AGENT_MAX_ROUNDS 硬上限为 20（防止无界循环）")

    return AgentSettings(
        max_tool_calls=max_tool_calls,
        max_rounds=max_rounds,
        max_graph_steps=_env_int("AGENT_MAX_GRAPH_STEPS", 24, minimum=4),
        total_timeout_s=_env_float("AGENT_TOTAL_TIMEOUT_S", 180.0),
        data_tool_timeout_s=_env_float("AGENT_DATA_TOOL_TIMEOUT_S", 20.0),
        statistics_tool_timeout_s=_env_float("AGENT_STATISTICS_TIMEOUT_S", 40.0),
        knowledge_tool_timeout_s=_env_float("AGENT_KNOWLEDGE_TOOL_TIMEOUT_S", 60.0),
        tool_max_retries=_env_int("AGENT_TOOL_MAX_RETRIES", 1, minimum=0),
        max_question_chars=_env_int("AGENT_MAX_QUESTION_CHARS", 500, minimum=10),
        max_history_rows=_env_int("AGENT_MAX_HISTORY_ROWS", 200, minimum=1),
        max_fault_events=_env_int("AGENT_MAX_FAULT_EVENTS", 20, minimum=1),
        max_knowledge_chunks=_env_int("AGENT_MAX_KNOWLEDGE_CHUNKS", 5, minimum=1),
        max_knowledge_top_k=_env_int("AGENT_MAX_KNOWLEDGE_TOP_K", 8, minimum=1),
        max_evidence_chars=_env_int("AGENT_MAX_EVIDENCE_CHARS", 9000, minimum=500),
        planner=_env_choice("AGENT_PLANNER", "rule", PLANNERS),
        answer_mode=_env_choice("AGENT_ANSWER_MODE", "auto", ANSWER_MODES),
        strict_numeric_grounding=_env_bool("AGENT_STRICT_NUMERIC_GROUNDING", True),
        default_equipment_code=_env("AGENT_DEFAULT_EQUIPMENT_CODE", "metropt3-apu-01"),
        default_year=_env_int("AGENT_DEFAULT_YEAR", 2020, minimum=1900),
        trace_dir=trace_dir,
        trace_memory_size=_env_int("AGENT_TRACE_MEMORY_SIZE", 200, minimum=1),
        trace_write_files=_env_bool("AGENT_TRACE_WRITE_FILES", True),
    )


__all__ = [
    "ANSWER_MODES",
    "PLANNERS",
    "AgentConfigError",
    "AgentSettings",
    "get_agent_settings",
]
