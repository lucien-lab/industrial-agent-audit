"""Phase 5：TUI ↔ 真实后端 端到端集成测试。

默认跳过。启用方式（需要本机 FastAPI + PostgreSQL + Ollama 都在运行）：

```bash
# 终端 1
scripts/start_api.sh
# 终端 2
IAA_TUI_E2E=1 pytest tests/test_tui_integration_real.py -q -m slow
```

与单元测试的区别：这里走**真实 HTTP**、真实数据库、真实模型，
断言的是后端真实返回（时间、数值、事件数都可能随数据变化），
不使用任何 Mock 响应。
"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx
import pytest

from app.tui.app import create_app
from app.tui.client import IaaApiClient
from app.tui.status import ComponentState

pytestmark = [
    pytest.mark.slow,
    pytest.mark.tui_e2e,
    pytest.mark.skipif(
        os.environ.get("IAA_TUI_E2E") != "1",
        reason="未设置 IAA_TUI_E2E=1，跳过 TUI 真实后端端到端验证",
    ),
]

BASE_URL = os.environ.get("IAA_TUI_E2E_BASE_URL", "http://127.0.0.1:8000")
DEMO_DATE = "2020-04-17"
STATE: dict[str, Any] = {}


def _api_available() -> bool:
    try:
        # trust_env=False：不受本机系统代理影响（TUI 客户端同样关闭了 trust_env）
        response = httpx.get(f"{BASE_URL}/health", timeout=3.0, trust_env=False)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_backend() -> None:
    if not _api_available():
        pytest.skip(f"后端 {BASE_URL} 未运行，跳过真实端到端测试")


def real_client() -> IaaApiClient:
    token = os.environ.get("IAA_AUDIT_TOKEN") or None
    return IaaApiClient(BASE_URL, audit_token=token)


async def wait_until(pilot, predicate: Callable[[], bool], *, timeout: float = 30.0) -> None:
    """轮询等待条件成立（真实模型可能耗时数十秒）。"""
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        await pilot.pause(0.25)
        waited += 0.25
    raise AssertionError(f"等待超时（{timeout}s）：条件未成立")


# --------------------------------------------------------------------------- #
# 1. 系统状态：真实探测
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_status_reflects_real_backend():
    app = create_app(BASE_URL)
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        status = app.system_status
        assert status is not None

        # 后端可达 => API 必须 ready
        assert status.state_of("api") is ComponentState.READY
        # 知识库与审计服务真实可用
        assert status.state_of("rag") is ComponentState.READY
        assert status.state_of("audit") is ComponentState.READY

        # 工业库状态必须与 /ready 的 schema_ok 一致：
        # 版本不一致时是 degraded，绝不允许被写成 ready
        client = real_client()
        try:
            readiness = await client.readiness()
        finally:
            await client.close()
        expected = ComponentState.READY if readiness.schema_ok else ComponentState.DEGRADED
        assert status.state_of("industrial_db") is expected
        # 未深度探测时 LLM 不能标成 ready
        assert status.state_of("llm") in {ComponentState.UNKNOWN, ComponentState.READY}
        STATE["readiness"] = readiness


# --------------------------------------------------------------------------- #
# 2. TUI → Agent API → 真实回答
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_agent_chat_against_real_llm():
    app = create_app(BASE_URL)
    question = f"{DEMO_DATE[:4]} 年 4 月 17 日的电机电流与排气压力情况如何？"
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-chat")
        await pilot.pause(0.5)
        page = app.page("chat")
        app.query_one("#chat-input").text = question
        await pilot.pause(0.2)
        await pilot.click("#chat-send")
        await wait_until(
            pilot, lambda: page.last_answer is not None, timeout=180
        )
        answer = page.last_answer
        assert answer is not None
        assert answer.answer.strip(), "真实回答不能为空"
        assert answer.trace_id and len(answer.trace_id) == 32
        assert answer.execution_status in {
            "completed",
            "insufficient_evidence",
            "failed",
            "timeout",
            "budget_exhausted",
            "refused",
        }
        # 数据类问题必须真的调用过工具，并且工具返回 ok/empty（不是客户端伪造）
        assert answer.tool_calls, "Agent 必须至少调用一次真实工具"
        assert all(call.status in {"ok", "empty", "error"} for call in answer.tool_calls)
        assert any(call.evidence_count >= 0 for call in answer.tool_calls)
        STATE["trace_id"] = answer.trace_id
        STATE["answer"] = answer

        # 界面必须显示工具与 trace
        from textual.widgets import Static

        statics = [
            child.renderable.plain if hasattr(child.renderable, "plain") else str(child.renderable)
            for child in app.query_one("#chat-log").children
            if isinstance(child, Static)
        ]
        joined = "\n".join(statics)
        assert answer.trace_id in joined
        assert any(tool in joined for tool in answer.tools_used)


# --------------------------------------------------------------------------- #
# 3. TUI → Trace API → 真实执行链
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_trace_explorer_reads_real_trace():
    trace_id = STATE.get("trace_id")
    if not trace_id:
        pytest.skip("没有上一步生成的 trace_id")
    app = create_app(BASE_URL)
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-traces")
        await pilot.pause(0.5)
        app.query_one("#trace-id").value = trace_id
        page = app.page("traces")
        await page.action_load_trace()
        await wait_until(pilot, lambda: page._events and len(page._events) > 0, timeout=60)

        from textual.widgets import DataTable

        table = app.query_one("#trace-events", DataTable)
        assert table.row_count >= 5, "真实 Trace 至少包含 run_start/tool_call/tool_result/answer/run_end"

        # 直接对比后端真实事件数
        client = real_client()
        try:
            detail = await client.audit_trace(trace_id)
        finally:
            await client.close()
        assert table.row_count == len(detail.trace.events)
        # 阶段 3 起 Trace 带 evidence manifest
        assert detail.has_evidence_manifest is True


# --------------------------------------------------------------------------- #
# 4. TUI → Audit API → Rule Only
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_rule_only_audit_persists_real_report():
    trace_id = STATE.get("trace_id")
    if not trace_id:
        pytest.skip("没有可审计的 trace_id")
    app = create_app(BASE_URL)
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-audit")
        await pilot.pause(0.5)
        page = app.page("audit")
        app.query_one("#audit-trace-id").value = trace_id
        await page.action_run_audit()
        await wait_until(pilot, lambda: page._report is not None, timeout=120)

        report = page._report
        assert report is not None
        assert report.audit_mode == "rule_only"
        assert report.audit_id.startswith("aud-")
        # 11 条确定性规则都必须给出结论（pass/fail/unknown/not_applicable）
        assert len(report.results) >= 11
        assert {item.verdict for item in report.results} <= {
            "pass",
            "fail",
            "unknown",
            "not_applicable",
        }
        STATE["rule_report"] = report

        # 幂等：相同 trace + 相同模式再审计一次应返回同一 audit_id
        client = real_client()
        try:
            again = await client.audit_evaluate(trace_id, mode="rule_only")
            stored = await client.audit_run(report.audit_id)
        finally:
            await client.close()
        assert again.audit_id == report.audit_id
        assert stored.total_findings == report.total_findings


# --------------------------------------------------------------------------- #
# 5. TUI → Audit API → Hybrid（真实 LLM Judge）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_hybrid_audit_against_real_judge():
    trace_id = STATE.get("trace_id")
    if not trace_id:
        pytest.skip("没有可审计的 trace_id")
    app = create_app(BASE_URL)
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-audit")
        await pilot.pause(0.5)
        page = app.page("audit")
        from textual.widgets import RadioButton, RadioSet

        radio = app.query_one("#audit-mode", RadioSet)
        radio.query_one("#mode-hybrid", RadioButton).value = True
        await pilot.pause(0.3)
        assert page._selected_mode() == "hybrid"
        app.query_one("#audit-trace-id").value = trace_id

        STATE["hybrid_progress_seen"] = False

        async def run_and_check_progress() -> None:
            await page.action_run_audit()
            # 审计进行中界面仍可切换页面（不阻塞事件循环）
            await pilot.pause(0.5)
            await pilot.click("#nav-status")
            await pilot.pause(0.5)
            assert app.current_page == "status"
            STATE["hybrid_progress_seen"] = True
            await pilot.click("#nav-audit")
            await pilot.pause(0.3)

        await run_and_check_progress()
        await wait_until(pilot, lambda: page._report is not None, timeout=300)

        report = page._report
        assert report is not None
        assert report.audit_mode == "hybrid"
        assert report.audit_id.startswith("aud-")
        assert report.duration_ms > 0
        # Judge 的结果只有 pass/fail/unknown/not_applicable，UNKNOWN 必须保持 UNKNOWN
        judge_results = [item for item in report.results if item.source == "judge"]
        for item in judge_results:
            assert item.verdict in {"pass", "fail", "unknown", "not_applicable"}
        assert STATE["hybrid_progress_seen"] is True


# --------------------------------------------------------------------------- #
# 6. TUI → Knowledge API → 真实文档检索
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_knowledge_search_returns_real_chunks():
    app = create_app(BASE_URL)
    async with app.run_test(size=(150, 46)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-knowledge")
        await pilot.pause(0.5)
        app.query_one("#knowledge-query").value = "TP2 TP3 传感器含义"
        page = app.page("knowledge")
        await page._search()
        await wait_until(pilot, lambda: len(page._results) > 0, timeout=120)

        from textual.widgets import DataTable

        table = app.query_one("#knowledge-table", DataTable)
        assert table.row_count == len(page._results)
        first = page._results[0]
        assert first.doc_key
        assert first.chunk_id > 0
        assert first.page_start and first.page_start >= 1
        assert first.content.strip()
        assert first.equipment_scope in {
            "metropt3_direct",
            "generic_compressor",
            "model_specific_other",
            "unknown",
        }

        # 与后端直接检索结果一致（同一查询同一方法）
        client = real_client()
        try:
            direct = await client.knowledge_search("TP2 TP3 传感器含义", method="hybrid", top_k=5)
        finally:
            await client.close()
        assert [hit.chunk_id for hit in page._results] == [hit.chunk_id for hit in direct.results]


# --------------------------------------------------------------------------- #
# 7. TUI → Equipment API → 真实传感器数据
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tui_equipment_page_reads_real_sensor_data():
    app = create_app(BASE_URL)
    async with app.run_test(size=(160, 48)) as pilot:
        await wait_until(pilot, lambda: app.system_status is not None, timeout=30)
        await pilot.click("#nav-equipment")
        await pilot.pause(0.5)
        from textual.widgets import Input, Static

        app.query_one("#equipment-start", Input).value = f"{DEMO_DATE}T00:00:00"
        app.query_one("#equipment-end", Input).value = "2020-04-18T00:00:00"
        app.query_one("#equipment-sensors", Input).value = "motor_current,dv_pressure"
        page = app.page("equipment")
        await page._query()
        await pilot.pause(1.5)

        from textual.widgets import DataTable

        stats_table = app.query_one("#equipment-stats-table", DataTable)
        sensors = [stats_table.get_row_at(i)[0] for i in range(stats_table.row_count)]
        assert "motor_current" in sensors

        # 与后端真实统计对比（数值必须一致，不允许客户端编造）
        client = real_client()
        try:
            stats = await client.stats(
                "metropt3-apu-01",
                start_time=f"{DEMO_DATE}T00:00:00",
                end_time="2020-04-18T00:00:00",
                sensors=["motor_current", "dv_pressure"],
            )
            quality = await client.quality(
                "metropt3-apu-01",
                start_time=f"{DEMO_DATE}T00:00:00",
                end_time="2020-04-18T00:00:00",
            )
            faults = await client.faults("metropt3-apu-01", limit=50)
        finally:
            await client.close()

        assert stats.row_count > 0
        row = next(item for item in stats.analog if item.sensor == "motor_current")
        # 表格里显示的 max 必须等于后端返回值
        table_row = [stats_table.get_row_at(i) for i in range(stats_table.row_count)]
        motor = next(cells for cells in table_row if cells[0] == "motor_current")
        assert str(motor[4]) == str(row.maximum).rstrip("0").rstrip(".")
        assert quality.actual_samples > 0
        quality_text = app.query_one("#equipment-quality", Static).renderable
        text = quality_text.plain if hasattr(quality_text, "plain") else str(quality_text)
        assert str(quality.actual_samples) in text.replace(",", "")
        assert faults.pagination.total == 4  # MetroPT-3 的 4 条官方故障区间
