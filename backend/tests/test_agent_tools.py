"""工具注册、参数校验、超时/重试与错误分类测试（不访问数据库）。"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.agent.config import AgentSettings, get_agent_settings
from app.agent.errors import UnknownTool, is_retryable
from app.agent.tools.base import ToolResult, ToolSpec, classify_exception, run_tool
from app.agent.tools.data_tools import (
    EquipmentInput,
    HistoryInput,
    StatisticsInput,
)
from app.agent.tools.knowledge_tools import KnowledgeSearchInput
from app.agent.tools.registry import (
    ALL_TOOL_NAMES,
    DATA_TOOL_NAMES,
    KNOWLEDGE_TOOL_NAMES,
    build_registry,
    get_tool_spec,
    tool_catalog,
)
from app.rag.errors import EmbeddingError, LLMTimeout, RetrievalError
from app.services.errors import (
    EquipmentNotFound,
    InvalidParameter,
    TimezoneNotSupported,
    UnsupportedSensor,
    WindowTooLarge,
)


class Boom(Exception):
    """测试用异常。"""


def _spec(impl, *, timeout_s: float = 2.0, max_retries: int = 0, name: str = "fake_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        category="data",
        description="fake",
        input_model=EquipmentInput,
        impl=impl,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )


def test_registry_contains_expected_tools() -> None:
    registry = build_registry(AgentSettings())
    assert set(registry) == set(ALL_TOOL_NAMES)
    assert set(DATA_TOOL_NAMES) == {
        "get_equipment_info",
        "get_sensor_history",
        "get_sensor_statistics",
        "get_fault_events",
        "get_data_quality",
    }
    assert KNOWLEDGE_TOOL_NAMES == ("search_technical_knowledge",)
    for spec in registry.values():
        assert spec.read_only is True
        assert spec.timeout_s > 0


def test_tool_catalog_exposes_input_schema_without_impl() -> None:
    catalog = tool_catalog(AgentSettings())
    names = [item["name"] for item in catalog]
    assert "get_sensor_statistics" in names
    stats = next(item for item in catalog if item["name"] == "get_sensor_statistics")
    assert "input_schema" in stats
    assert set(stats["input_schema"]["properties"]) >= {"equipment_code", "start_time", "end_time"}
    assert "impl" not in stats


def test_unknown_tool_is_rejected() -> None:
    registry = build_registry(AgentSettings())
    with pytest.raises(UnknownTool):
        get_tool_spec(registry, "run_sql")
    with pytest.raises(UnknownTool):
        get_tool_spec(registry, "../../etc/passwd")


@pytest.mark.parametrize(
    "payload",
    [
        {"equipment_code": "x'; DROP TABLE equipment;--"},
        {"equipment_code": "metropt3-apu-01 OR 1=1"},
        {"equipment_code": ""},
        {"equipment_code": "a" * 65},
        {"equipment_code": "metropt3-apu-01", "unexpected": 1},
    ],
)
def test_equipment_input_rejects_bad_values(payload) -> None:
    with pytest.raises(Exception):
        EquipmentInput.model_validate(payload)


@pytest.mark.parametrize(
    "start,end",
    [
        ("2020-04-17T00:00:00+08:00", "2020-04-18T00:00:00"),
        ("2020-04-17'", "2020-04-18"),
        ("2020-13-01", "2020-13-02"),
        ("", "2020-04-18"),
    ],
)
def test_statistics_input_rejects_bad_time(start: str, end: str) -> None:
    with pytest.raises(Exception):
        StatisticsInput.model_validate(
            {"equipment_code": "metropt3-apu-01", "start_time": start, "end_time": end}
        )


def test_statistics_input_rejects_unknown_sensor() -> None:
    with pytest.raises(Exception):
        StatisticsInput.model_validate(
            {
                "equipment_code": "metropt3-apu-01",
                "start_time": "2020-04-17",
                "end_time": "2020-04-18",
                "sensors": ["motor_current; DROP TABLE"],
            }
        )


def test_history_input_caps_limit() -> None:
    ok = HistoryInput.model_validate(
        {
            "equipment_code": "metropt3-apu-01",
            "start_time": "2020-04-17",
            "end_time": "2020-04-18",
            "limit": 200,
        }
    )
    assert ok.limit == 200
    with pytest.raises(Exception):
        HistoryInput.model_validate(
            {
                "equipment_code": "metropt3-apu-01",
                "start_time": "2020-04-17",
                "end_time": "2020-04-18",
                "limit": 5000,  # 超过工具硬上限
            }
        )


def test_knowledge_input_validates_scope_and_query() -> None:
    with pytest.raises(Exception):
        KnowledgeSearchInput.model_validate({"query": "x", "equipment_scopes": ["secret_scope"]})
    with pytest.raises(Exception):
        KnowledgeSearchInput.model_validate({"query": "   "})
    with pytest.raises(Exception):
        KnowledgeSearchInput.model_validate({"query": "TP2", "doc_keys": ["../etc/passwd"]})
    ok = KnowledgeSearchInput.model_validate({"query": "TP2 含义", "top_k": 8})
    assert ok.top_k == 8


def test_run_tool_happy_path() -> None:
    def impl(ctx, payload):
        return ToolResult(status="ok", data={"equipment": {"equipment_code": payload.equipment_code}})

    outcome = run_tool(_spec(impl), {"equipment_code": "metropt3-apu-01"}, object())
    assert outcome.status == "ok"
    assert outcome.ok is True
    assert outcome.attempts == 1
    assert outcome.data["equipment"]["equipment_code"] == "metropt3-apu-01"


def test_run_tool_invalid_input_is_not_retried() -> None:
    calls = {"n": 0}

    def impl(ctx, payload):  # pragma: no cover - 不应被调用
        calls["n"] += 1
        return ToolResult(status="ok", data={})

    outcome = run_tool(_spec(impl, max_retries=3), {"equipment_code": "bad id!"}, object())
    assert outcome.status == "error"
    assert outcome.error_category == "invalid_input"
    assert outcome.error["retryable"] is False
    assert calls["n"] == 0


def test_run_tool_empty_is_not_error() -> None:
    def impl(ctx, payload):
        return ToolResult(status="empty", data={"rows": 0})

    outcome = run_tool(_spec(impl), {"equipment_code": "metropt3-apu-01"}, object())
    assert outcome.status == "empty"
    assert outcome.error is None
    assert outcome.ok is True  # empty 是合法结果，不是失败


def test_run_tool_maps_not_found_and_blocks_dependents() -> None:
    def impl(ctx, payload):
        raise EquipmentNotFound("设备 'x' 不存在", equipment_code="x")

    outcome = run_tool(_spec(impl), {"equipment_code": "x"}, object())
    assert outcome.status == "error"
    assert outcome.error_category == "not_found"
    assert outcome.error["blocks_dependents"] is True


def test_run_tool_retries_transient_error_then_fails() -> None:
    calls = {"n": 0}

    def impl(ctx, payload):
        calls["n"] += 1
        raise EmbeddingError("embedding 服务不可用")

    outcome = run_tool(
        _spec(impl, max_retries=2), {"equipment_code": "metropt3-apu-01"}, object()
    )
    assert calls["n"] == 3  # 1 次 + 2 次重试
    assert outcome.attempts == 3
    assert outcome.error_category == "transient"
    assert outcome.error["retryable"] is True


def test_run_tool_retries_then_succeeds() -> None:
    state = {"n": 0}

    def impl(ctx, payload):
        state["n"] += 1
        if state["n"] == 1:
            raise RetrievalError("瞬时失败")
        return ToolResult(status="ok", data={"rows": 3})

    outcome = run_tool(_spec(impl, max_retries=1), {"equipment_code": "metropt3-apu-01"}, object())
    assert outcome.status == "ok"
    assert outcome.attempts == 2


def test_run_tool_timeout() -> None:
    def impl(ctx, payload):
        time.sleep(1.5)
        return ToolResult(status="ok", data={})

    outcome = run_tool(
        _spec(impl, timeout_s=0.2), {"equipment_code": "metropt3-apu-01"}, object()
    )
    assert outcome.status == "error"
    assert outcome.error_category == "timeout"
    assert is_retryable("timeout") is True


def test_run_tool_respects_deadline_before_retry() -> None:
    calls = {"n": 0}

    def impl(ctx, payload):
        calls["n"] += 1
        raise EmbeddingError("不可用")

    outcome = run_tool(
        _spec(impl, max_retries=3),
        {"equipment_code": "metropt3-apu-01"},
        object(),
        deadline_epoch=time.time() + 0.05,
    )
    assert outcome.status == "error"
    assert outcome.error_category in ("transient", "budget")
    assert calls["n"] <= 2


def test_run_tool_is_keyword_only_and_never_writes() -> None:
    """工具实现只接收 (ctx, payload)，没有写库或执行任意 SQL 的入口。"""
    spec = build_registry(AgentSettings())["get_sensor_statistics"]
    assert list(spec.impl.__code__.co_varnames[:2]) == ["ctx", "payload"]
    assert spec.read_only is True


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (EquipmentNotFound("不存在"), "not_found"),
        (InvalidParameter("参数不合法"), "invalid_input"),
        (TimezoneNotSupported("带时区"), "invalid_input"),
        (UnsupportedSensor("未知传感器"), "invalid_input"),
        (WindowTooLarge("窗口过大"), "invalid_input"),
        (LLMTimeout("模型超时"), "timeout"),
        (EmbeddingError("embedding 失败"), "transient"),
        (Boom("未知"), "internal"),
    ],
)
def test_classify_exception(exc: BaseException, category: str) -> None:
    assert classify_exception(exc)[0] == category


def test_settings_tool_timeouts_are_per_tool() -> None:
    settings = replace(
        get_agent_settings(),
        data_tool_timeout_s=5.0,
        statistics_tool_timeout_s=7.0,
        knowledge_tool_timeout_s=9.0,
    )
    assert settings.tool_timeout("get_sensor_statistics") == 7.0
    assert settings.tool_timeout("search_technical_knowledge") == 9.0
    assert settings.tool_timeout("get_fault_events") == 5.0
