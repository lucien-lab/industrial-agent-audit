"""Agent State 与预算工具测试（不访问数据库）。"""

from __future__ import annotations

import time

import pytest

from app.agent.errors import AgentInputError
from app.agent.state import (
    BUDGET_EXHAUSTED,
    COMPLETED,
    RUNNING,
    AgentState,
    deadline_passed,
    initial_state,
    is_final,
    remaining_tool_budget,
    tools_used,
    validate_state,
)
from app.agent.trace import summarize


def _state(**overrides) -> AgentState:
    state = initial_state(
        trace_id="t-1",
        question="2020-04-17 电流最大值是多少？",
        request={"equipment_id": "metropt1", "planner": None},
        total_timeout_s=30,
    )
    state.update(overrides)
    return state


def test_initial_state_has_all_required_fields() -> None:
    state = _state()
    for key in (
        "trace_id",
        "question",
        "request",
        "intent",
        "plan",
        "pending",
        "tool_calls",
        "observations",
        "errors",
        "warnings",
        "rounds",
        "tool_call_count",
        "steps",
        "deadline_epoch",
        "status",
        "answer",
        "facts",
        "blocked_tools",
    ):
        assert key in state, key
    assert state["status"] == RUNNING
    assert state["tool_call_count"] == 0
    assert state["plan"] == []


def test_validate_state_accepts_initial_state() -> None:
    validate_state(_state())


def test_validate_state_rejects_missing_fields() -> None:
    state = _state()
    state.pop("question")
    with pytest.raises(AgentInputError):
        validate_state(state)


def test_validate_state_rejects_bad_budget_types() -> None:
    with pytest.raises(AgentInputError):
        validate_state(_state(tool_call_count=-1))
    with pytest.raises(AgentInputError):
        validate_state(_state(rounds="two"))  # type: ignore[arg-type]


def test_validate_state_rejects_unknown_status() -> None:
    with pytest.raises(AgentInputError):
        validate_state(_state(status="whatever"))


def test_deadline_and_budget_helpers() -> None:
    state = _state(deadline_epoch=time.time() + 5)
    assert deadline_passed(state) is False
    state["deadline_epoch"] = time.time() - 1
    assert deadline_passed(state) is True

    assert remaining_tool_budget(_state(tool_call_count=3), 8) == 5
    assert remaining_tool_budget(_state(tool_call_count=9), 8) == 0


def test_is_final_only_for_terminal_statuses() -> None:
    assert is_final(_state()) is False
    assert is_final(_state(status=COMPLETED)) is True
    assert is_final(_state(status=BUDGET_EXHAUSTED)) is True


def test_tools_used_dedupes_in_call_order() -> None:
    state = _state(
        tool_calls=[
            {"tool": "get_sensor_statistics"},
            {"tool": "get_data_quality"},
            {"tool": "get_sensor_statistics"},
        ]
    )
    assert tools_used(state) == ["get_sensor_statistics", "get_data_quality"]


def test_summarize_drops_unknown_keys_and_truncates() -> None:
    summary = summarize(
        {
            "tool": "get_sensor_statistics",
            "rows": 10,
            "password": "super-secret",
            "database_url": "postgresql://u:p@h/db",
            "query": "x" * 900,
        }
    )
    assert summary["tool"] == "get_sensor_statistics"
    assert summary["rows"] == 10
    assert "password" not in summary
    assert "database_url" not in summary
    assert len(summary["query"]) <= 481


def test_summarize_bounds_lists() -> None:
    summary = summarize({"labels": [f"D{i}" for i in range(20)]})
    assert isinstance(summary["labels"], list)
    assert len(summary["labels"]) == 6  # 5 项 + 省略标记
