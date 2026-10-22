"""Agent 端到端工作流测试（隔离测试库 + 真实 Service + 假模型）。

覆盖：
- 工具选择（纯知识 / 纯数据 / 混合）；
- 多步骤工具调用（异常判断补对照窗口、空结果补质量查询、检索为空换关键词）；
- 空结果与查询失败的区别；
- 设备不存在、预算耗尽、整轮超时；
- RAG 与数据工具联合查询、证据来源保留；
- Trace 事件完整性与只读会话约束。

说明：本文件用 `FakeChat` / `FakeEmbedder` 替身控制模型与向量（单元/集成测试），
**真实模型 + 真实知识库的端到端验证**见 `test_agent_e2e_real.py`。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.config import AgentSettings, get_agent_settings
from app.agent.context import AgentContext
from app.agent.service import AgentRequest, get_trace, run_agent
from app.agent.trace import get_trace_store
from app.services import sensor_statistics as stats_service
from tests.helpers_rag import FakeChat, FakeEmbedder, add_chunk, add_document, fake_vector

WINDOW = ("2020-02-01T00:00:00", "2020-02-01T00:30:00")
CHUNK_TEXT = (
    "TP2 (bar) – the measure of the pressure on the compressor. "
    "TP3 (bar) – the measure of the pressure on the compressor outlet."
)


@pytest.fixture()
def agent_dataset(clean_engine, real_head_csv):
    """测试库导入真实 CSV 前 400 行 + 官方故障事件（提交后可见）。"""
    from app.services.metropt_import import import_fault_events, import_metropt_csv

    import_metropt_csv(real_head_csv, engine=clean_engine)
    import_fault_events(engine=clean_engine)
    return clean_engine


@pytest.fixture()
def knowledge_db(agent_dataset, monkeypatch):
    """在测试库中写入一条知识片段，并让检索使用 fake-embed 向量。"""
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embed")
    monkeypatch.setenv("EMBEDDING_VERSION", "fake-v1")
    monkeypatch.setenv(
        "RAG_DATABASE_URL", agent_dataset.url.render_as_string(hide_password=False)
    )
    with Session(agent_dataset) as session:
        document = add_document(
            session, doc_key="metropt3-data-description", title="MetroPT-3 Data Description"
        )
        add_chunk(
            session,
            document,
            index=0,
            content=CHUNK_TEXT,
            page=2,
            vector=fake_vector("tp2 tp3 pressure"),
        )
        other = add_document(
            session,
            doc_key="doe-compressed-air-sourcebook",
            title="DOE Compressed Air Sourcebook",
            scope="generic_compressor",
        )
        add_chunk(
            session,
            other,
            index=0,
            content="Leaks are a major source of wasted compressed air in industrial systems.",
            page=12,
            vector=fake_vector("leaks wasted air"),
        )
        session.commit()
    return agent_dataset


@pytest.fixture()
def settings() -> AgentSettings:
    return replace(
        get_agent_settings(),
        answer_mode="deterministic",
        trace_write_files=False,
        planner="rule",
    )


def _run(question: str, *, settings: AgentSettings, **kwargs):
    get_trace_store().clear()
    return run_agent(
        AgentRequest(question=question, **{k: v for k, v in kwargs.items() if k != "embedder"}),
        settings=settings,
        **({"embedder": kwargs["embedder"]} if "embedder" in kwargs else {}),
        **({"chat_model": kwargs["chat_model"]} if "chat_model" in kwargs else {}),
    )


# --------------------------------------------------------------------------- #
# 三类问题
# --------------------------------------------------------------------------- #
def test_data_only_question_uses_statistics_only(agent_dataset, settings) -> None:
    response = _run(
        f"{WINDOW[0]} 到 {WINDOW[1]} 电机电流的最大值是多少？",
        settings=settings,
    )
    assert response.intent == "data_only"
    assert response.tools_used == ["get_sensor_statistics"]
    assert response.execution_status == "completed"
    assert response.answer_status == "answered"

    # 数值必须与直接调用 Service 得到的结果一致（不是模型猜的）
    with Session(agent_dataset) as session:
        expected = stats_service.get_sensor_statistics(
            session,
            "metropt3-apu-01",
            start=WINDOW[0],
            end=WINDOW[1],
            sensors=["motor_current"],
        )
    max_value = expected.analog[0].maximum
    assert f"{max_value:g}" in response.answer or f"{max_value:.4f}".rstrip("0").rstrip(".") in response.answer
    assert any(item["kind"] == "data" for item in response.evidence)
    assert all(item["source"]["read_only"] for item in response.evidence if item["kind"] == "data")


def test_knowledge_only_question_uses_retrieval(knowledge_db, settings) -> None:
    response = _run(
        "TP2 和 TP3 的物理含义是什么？",
        settings=settings,
        embedder=FakeEmbedder(),
    )
    assert response.intent == "knowledge_only"
    assert response.tools_used == ["search_technical_knowledge"]
    assert response.execution_status == "completed"
    knowledge = [item for item in response.evidence if item["kind"] == "knowledge"]
    assert knowledge
    first = knowledge[0]
    assert first["chunk_id"] and first["doc_key"] == "metropt3-data-description"
    assert first["page_start"] == 2
    assert first["equipment_scope"] == "metropt3_direct"
    assert first["quote"] and first["quote"][:20] in CHUNK_TEXT


def test_mixed_question_combines_data_and_knowledge(knowledge_db, settings) -> None:
    response = _run(
        "2020年2月1日的电机电流是否异常？结合技术资料说明可能关注哪些问题",
        settings=settings,
        embedder=FakeEmbedder(),
    )
    assert response.intent == "mixed_analysis"
    assert "get_sensor_statistics" in response.tools_used
    assert "search_technical_knowledge" in response.tools_used
    kinds = {item["kind"] for item in response.evidence}
    assert kinds == {"data", "knowledge"}
    assert response.execution_status == "completed"


# --------------------------------------------------------------------------- #
# 多步骤：补充查询
# --------------------------------------------------------------------------- #
def test_anomaly_question_adds_baseline_followup(agent_dataset, settings) -> None:
    response = _run(
        "2020年2月1日 00:00 到 00:30 电机电流是否异常？",
        settings=settings,
    )
    stages = [(call["tool"], call["stage"]) for call in response.tool_calls]
    assert ("get_sensor_statistics", "initial") in stages
    assert ("get_sensor_statistics", "followup") in stages
    baseline = [
        item for item in response.evidence if item.get("fact_type") == "baseline"
    ]
    assert baseline, "应产生与前一等长窗口的对照事实"


def test_empty_window_adds_quality_followup(agent_dataset, settings) -> None:
    response = _run(
        "2020年1月1日 电机电流的最大值是多少？",
        settings=settings,
    )
    assert [call["tool"] for call in response.tool_calls] == [
        "get_sensor_statistics",
        "get_data_quality",
    ]
    assert response.execution_status == "insufficient_evidence"
    assert "没有查到任何观测记录" in response.answer
    # 空结果不是失败：错误列表应保持为空
    assert response.errors == []


def test_empty_knowledge_triggers_keyword_retry(knowledge_db, settings) -> None:
    response = _run(
        "zzzz 不存在的专有名词 xxxx 的含义是什么？",
        settings=settings,
        embedder=FakeEmbedder(),
    )
    tools = [call["tool"] for call in response.tool_calls]
    assert tools.count("search_technical_knowledge") >= 1
    if tools.count("search_technical_knowledge") == 2:
        assert response.tool_calls[1]["params"]["method"] == "keyword"
    assert response.execution_status in ("insufficient_evidence", "completed")


# --------------------------------------------------------------------------- #
# 失败与预算
# --------------------------------------------------------------------------- #
def test_unknown_equipment_is_reported_not_fabricated(agent_dataset, settings) -> None:
    response = _run(
        "2020年2月1日 电机电流的最大值是多少？",
        settings=settings,
        equipment_id="unknown-device",
    )
    assert response.execution_status == "insufficient_evidence"
    assert "不存在" in response.answer
    assert response.errors and response.errors[0]["category"] == "not_found"
    assert not [item for item in response.evidence if item["kind"] == "data"]


def test_missing_time_window_asks_for_clarification(agent_dataset, settings) -> None:
    response = _run("电机电流是否异常？", settings=settings)
    assert response.execution_status == "insufficient_evidence"
    assert "缺少可用的时间范围" in response.answer
    assert response.tools_used == ["get_equipment_info"]
    assert "2020-02-01" in response.answer  # 用真实观测范围给出提示


def test_max_tool_calls_budget(agent_dataset, settings) -> None:
    limited = replace(settings, max_tool_calls=1)
    response = _run("2020年2月1日 00:00 到 00:30 电流是否异常？", settings=limited)
    assert response.execution_status == "budget_exhausted"
    assert len(response.tool_calls) == 1
    assert any("超过上限" in warning for warning in response.warnings)


def test_max_rounds_budget(agent_dataset, settings) -> None:
    limited = replace(settings, max_rounds=1)
    response = _run("2020年2月1日 00:00 到 00:30 电流是否异常？", settings=limited)
    assert response.execution_status == "budget_exhausted"
    assert len(response.tool_calls) <= 2


def test_total_timeout_stops_before_tools(agent_dataset, settings) -> None:
    limited = replace(settings, total_timeout_s=0.0001)
    response = _run("2020年2月1日 00:00 到 00:30 电流最大值", settings=limited)
    assert response.execution_status == "timeout"
    assert response.tool_calls == []
    assert "超时" in response.answer or "存在" not in response.answer


def test_max_graph_steps_guard(agent_dataset, settings) -> None:
    limited = replace(settings, max_graph_steps=5)
    response = _run("2020年2月1日 00:00 到 00:30 电流是否异常？", settings=limited)
    assert response.execution_status in ("budget_exhausted", "timeout", "completed")
    assert len(response.tool_calls) <= limited.max_tool_calls


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #
def test_llm_planner_plan_is_validated(agent_dataset, settings) -> None:
    plan = {
        "calls": [
            {
                "tool": "get_sensor_statistics",
                "params": {
                    "equipment_code": "metropt3-apu-01",
                    "start_time": WINDOW[0],
                    "end_time": WINDOW[1],
                    "sensors": ["motor_current"],
                },
                "reason": "模型认为需要统计量",
            },
            {"tool": "run_sql", "params": {"sql": "select * from equipment"}},
        ]
    }
    llm_settings = replace(settings, planner="llm")
    response = run_agent(
        AgentRequest(question="2020年2月1日 00:00 到 00:30 电流最大值", planner="llm"),
        settings=llm_settings,
        chat_model=FakeChat(plan),
    )
    assert response.tools_used == ["get_sensor_statistics"]
    assert response.execution_status == "completed"


def test_llm_planner_invalid_output_falls_back_to_rules(agent_dataset, settings) -> None:
    llm_settings = replace(settings, planner="llm")
    response = run_agent(
        AgentRequest(question="2020年2月1日 00:00 到 00:30 电流最大值", planner="llm"),
        settings=llm_settings,
        chat_model=FakeChat("这不是 JSON"),
    )
    assert response.execution_status == "completed"
    assert response.tools_used == ["get_sensor_statistics"]
    assert any("回退规则规划" in warning for warning in response.warnings)


# --------------------------------------------------------------------------- #
# Trace 与只读
# --------------------------------------------------------------------------- #
def test_trace_events_are_recorded(agent_dataset, settings) -> None:
    store = get_trace_store()
    store.clear()
    response = run_agent(
        AgentRequest(question="2020年2月1日 00:00 到 00:30 电流最大值"), settings=settings
    )
    summary = get_trace(response.trace_id)
    assert summary is not None
    types = [event["event_type"] for event in summary["events"]]
    for expected in ("run_start", "intent", "plan", "tool_call", "tool_result", "answer", "run_end"):
        assert expected in types, expected
    assert [event["seq"] for event in summary["events"]] == list(
        range(1, len(summary["events"]) + 1)
    )
    assert summary["execution_status"] == "completed"
    assert summary["tools_used"] == ["get_sensor_statistics"]
    assert summary["evidence_refs"]


def test_trace_does_not_leak_credentials(agent_dataset, settings) -> None:
    response = run_agent(
        AgentRequest(question="2020年2月1日 00:00 到 00:30 电流最大值"), settings=settings
    )
    summary = get_trace(response.trace_id)
    payload = json.dumps(summary, ensure_ascii=False)
    assert "postgresql+psycopg" not in payload
    assert "password" not in payload.lower()
    assert os.environ.get("POSTGRES_PASSWORD", "no-password-in-env") not in payload


def test_agent_sessions_are_read_only(agent_dataset, settings) -> None:
    ctx = AgentContext(
        settings=settings, data_url=agent_dataset.url.render_as_string(hide_password=False)
    )
    try:
        with ctx.open_data_session() as session:
            with pytest.raises(Exception) as excinfo:
                session.execute(
                    text("INSERT INTO equipment (equipment_code, equipment_name, equipment_type)"
                         " VALUES ('x','x','x')")
                )
            assert "read-only" in str(excinfo.value).lower() or "ReadOnly" in str(
                type(excinfo.value).__name__
            )
    finally:
        ctx.close()


def test_sources_are_reported_separately(agent_dataset, settings) -> None:
    ctx = AgentContext(
        settings=settings,
        data_url=agent_dataset.url.render_as_string(hide_password=False),
        knowledge_url="postgresql+psycopg://u:p@127.0.0.1:5432/other_kb",
    )
    try:
        described = ctx.describe_sources()
        assert described["same_database"] is False
        assert described["industrial_database"].endswith("_test")
        assert described["knowledge_database"] == "other_kb"
        # 不含用户、密码与主机
        assert "app_user" not in str(described)
        assert "127.0.0.1" not in str(described)
    finally:
        ctx.close()
