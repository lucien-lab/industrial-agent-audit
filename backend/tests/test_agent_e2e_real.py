"""真实端到端验证：真实模型（Ollama）+ 真实 RAG 索引 + 真实工业数据。

默认**跳过**（需要本机 Ollama 与已入库的 RAG 库，且会读取真实数据），启用方式：

    IAA_AGENT_E2E=1 pytest tests/test_agent_e2e_real.py -q -m slow

注意：
- 该测试使用 `.env` 中的 `DATABASE_URL` / `RAG_DATABASE_URL`（**不是**测试库），
  数据库会话在连接层强制只读，因此不会修改任何生产数据；
- Mock 测试通过 ≠ 真实能力验证通过：本文件才是“真实 Agent 能力”的验证入口。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.agent.config import get_agent_settings
from app.agent.service import AgentRequest, run_agent
from app.core.config import PROJECT_ROOT

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("IAA_AGENT_E2E") != "1",
        reason="未设置 IAA_AGENT_E2E=1，跳过真实模型端到端验证",
    ),
]


def _dotenv_url(key: str) -> str:
    """直接从项目根 .env 读取连接串（避免被测试会话的环境变量覆盖）。"""
    env_file = Path(PROJECT_ROOT) / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    raise AssertionError(f".env 缺少 {key}")


@pytest.fixture(scope="module")
def real_settings():
    data_url = _dotenv_url("DATABASE_URL")
    rag_url = _dotenv_url("RAG_DATABASE_URL") or data_url
    settings = replace(
        get_agent_settings(),
        answer_mode="auto",
        planner="rule",
        trace_write_files=False,
        total_timeout_s=300.0,
        knowledge_tool_timeout_s=120.0,
    )
    return settings, data_url, rag_url


def _run(question: str, real_settings, **kwargs):
    settings, data_url, rag_url = real_settings
    from app.agent.context import AgentContext

    ctx = AgentContext(
        settings=settings, data_url=data_url, knowledge_url=rag_url, readonly=True
    )
    try:
        return run_agent(
            AgentRequest(question=question, **kwargs), settings=settings, ctx=ctx
        )
    finally:
        ctx.close()


def test_real_mixed_analysis_end_to_end(real_settings) -> None:
    response = _run(
        "这台空气压缩机在 2020 年 4 月 17 日的电流和压力是否异常？"
        "结合公开技术资料，可能需要关注哪些问题？",
        real_settings,
    )
    assert response.execution_status in ("completed", "insufficient_evidence")
    assert response.intent == "mixed_analysis"
    assert "get_sensor_statistics" in response.tools_used
    assert "search_technical_knowledge" in response.tools_used
    assert response.trace_id

    # 真实数值：必须与直接调用 Service 的结果一致
    from app.agent.context import AgentContext
    from app.services import sensor_statistics as stats_service

    settings, data_url, _ = real_settings
    ctx = AgentContext(settings=settings, data_url=data_url)
    try:
        with ctx.open_data_session() as session:
            expected = stats_service.get_sensor_statistics(
                session,
                "metropt3-apu-01",
                start="2020-04-17T00:00:00",
                end="2020-04-18T00:00:00",
                sensors=["motor_current"],
            )
    finally:
        ctx.close()
    max_current = expected.analog[0].maximum
    assert max_current is not None
    candidates = {f"{max_current:g}", f"{max_current:.4f}".rstrip("0").rstrip(".")}
    assert any(token in response.answer for token in candidates)

    # 真实知识片段：必须带真实 Chunk ID 与页码
    knowledge = [item for item in response.evidence if item["kind"] == "knowledge"]
    assert knowledge
    assert all(item["chunk_id"] for item in knowledge)
    assert all(item["doc_key"] for item in knowledge)
    assert any(item["page_start"] >= 1 for item in knowledge)

    # 不得把可能性写成确诊
    assert "不构成故障诊断结论" in response.answer
    # 数值接地：回答中的数字必须来自事实清单
    assert response.grounding.get("numeric_violations", []) == [] or response.answer_mode == "deterministic"


def test_real_knowledge_question_end_to_end(real_settings) -> None:
    response = _run("TP2 和 TP3 的物理含义是什么？", real_settings)
    assert response.intent == "knowledge_only"
    assert response.tools_used == ["search_technical_knowledge"]
    assert response.execution_status in ("completed", "insufficient_evidence")
    knowledge = [item for item in response.evidence if item["kind"] == "knowledge"]
    assert knowledge
    assert knowledge[0]["equipment_scope"] in ("metropt3_direct", "generic_compressor")
    assert knowledge[0]["quote"]
    assert "答" in response.answer or "文档" in response.answer


def test_real_trace_is_complete(real_settings) -> None:
    from app.agent.service import get_trace

    response = _run("2020年4月17日电机电流的最大值是多少？", real_settings)
    summary = get_trace(response.trace_id)
    assert summary is not None
    types = {event["event_type"] for event in summary["events"]}
    assert {"run_start", "intent", "plan", "tool_call", "tool_result", "answer", "run_end"} <= types
    assert summary["total_duration_ms"] > 0
    payload = str(summary)
    assert "postgresql+psycopg" not in payload
