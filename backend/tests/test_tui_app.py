"""Phase 5：Textual TUI 单元测试（使用 httpx.MockTransport 模拟后端，不访问网络）。

覆盖：启动、页面切换、真实问答渲染、工具展示、Trace 查看、审计模式切换、
审计结果（UNKNOWN 不显示为 PASS）、异步期间界面可响应、网络失败提示、空数据提示、
终端尺寸变化、退出与资源释放、人工复核。

这些测试证明**界面逻辑**正确；真实后端联调见 `test_tui_integration_real.py`。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from textual.widgets import DataTable, Input, Markdown, RadioButton, RadioSet, Static, TextArea

from app.tui.app import IaaTuiApp, create_app
from app.tui.client import IaaApiClient
from app.tui.pages.chat import ChatPage

BASE = "http://mock-backend"

# --------------------------------------------------------------------------- #
# 模拟后端响应（结构必须与真实接口一致；数值为测试用途，不用于演示）
# --------------------------------------------------------------------------- #
HEALTH = {"status": "ok", "service": "industrial-agent-audit-api", "version": "2.0.0"}
READY = {
    "status": "degraded",
    "database": {"database": "industrial_agent_audit", "user": "app_user", "server_version": "17.11"},
    "schema_version": "0004_coverage_missing_time",
    "schema_expected_version": "0008_agent_audit",
    "schema_ok": False,
}
KNOWLEDGE_HEALTH = {
    "status": "ok",
    "database": {"database": "iaa_rag_dev", "user": "app_user"},
    "schema_version": "0007_knowledge_rag",
    "schema_expected_version": "0007_knowledge_rag",
    "schema_ok": True,
    "documents": 6,
    "chunks": 1314,
    "indexes": [{"chunking_version": "structural-v1", "chunks": 750}],
    "embedding": {"provider": "ollama", "model": "bge-m3", "indexed_chunks": 750},
    "llm": {"provider": "ollama", "model": "qwen2.5:7b-instruct"},
    "warnings": [],
}
AUDIT_SUMMARY = {"total_audits": 2, "audits_by_risk": {"medium": 1, "none": 1}}

EQUIPMENT = {
    "items": [
        {
            "equipment_id": "metropt3-apu-01",
            "equipment_name": "MetroPT-3 空气生产单元",
            "equipment_type": "air_production_unit",
            "first_observed_at": "2020-02-01T00:00:00",
            "last_observed_at": "2020-09-01T03:59:50",
        }
    ],
    "total": 1,
}
STATS = {
    "equipment": {"equipment_id": "metropt3-apu-01", "equipment_name": "MetroPT-3"},
    "sensors": ["motor_current", "comp"],
    "row_count": 3,
    "nominal_step_seconds": 10.0,
    "analog": [
        {
            "sensor": "motor_current",
            "count": 3,
            "min": 0.035,
            "max": 6.36,
            "mean": 0.4512,
            "std": 1.302,
        }
    ],
    "digital": [
        {
            "sensor": "comp",
            "count": 3,
            "count_0": 1,
            "count_1": 2,
            "ratio_1": 0.6667,
            "transitions": 1,
        }
    ],
}
QUALITY = {
    "equipment": {"equipment_id": "metropt3-apu-01", "equipment_name": "MetroPT-3"},
    "nominal_step_seconds": 10.0,
    "actual_samples": 3,
    "expected_samples": 4,
    "sample_coverage": 0.75,
    "time_coverage": 0.8,
    "gap_count": 1,
    "gap_duration_seconds": 400.0,
    "max_gap_seconds": 400.0,
    "missing_samples_estimate": 1,
    "frozen_candidate_count": 1,
    "frozen_candidate_ratio": 0.3333,
    "records_with_quality_flags": 1,
    "window_inside_data_range": True,
    "warnings": [{"code": "coverage_below_threshold", "severity": "warning", "message": "覆盖率偏低"}],
}
FAULTS = {
    "items": [
        {
            "event_code": "E1",
            "start_time": "2020-04-18T00:00:00",
            "end_time": "2020-04-18T23:59:00",
            "duration_seconds": 86340.0,
            "overlap_seconds": 60.0,
            "failure_type": "Air leak",
            "severity": "high",
            "verification_status": "verified",
        }
    ],
    "pagination": {"limit": 50, "offset": 0, "total": 1, "returned": 1, "has_more": False},
    "sample_label_semantics": "故障区间内的记录不会被自动标记为已确认故障样本",
}
READINGS = {
    "equipment": {"equipment_id": "metropt3-apu-01", "equipment_name": "MetroPT-3"},
    "sensors": ["motor_current", "comp"],
    "items": [
        {"observed_at": "2020-04-17T00:00:05", "values": {"motor_current": 0.035, "comp": 1}, "quality_flags": 0},
        {"observed_at": "2020-04-17T00:00:15", "values": {"motor_current": 6.36, "comp": 0}, "quality_flags": 0},
        {"observed_at": "2020-04-17T00:00:25", "values": {"motor_current": 0.45, "comp": 1}, "quality_flags": 1},
    ],
    "pagination": {"limit": 240, "offset": 0, "total": 3, "returned": 3, "has_more": False},
    "exclude_frozen": False,
}
KNOWLEDGE_SEARCH = {
    "query": "TP2 代表什么",
    "method": "hybrid",
    "top_k": 2,
    "results": [
        {
            "chunk_id": 12,
            "doc_key": "metropt3-data-description",
            "document_title": "MetroPT-3 Dataset — Data Description",
            "chunk_index": 3,
            "content": "TP2 is the pressure at the compressor outlet.",
            "page_start": 3,
            "page_end": 3,
            "equipment_scope": "metropt3_direct",
            "doc_type": "dataset_description",
            "trust_level": "high",
            "score": 0.0328,
            "rrf_score": 0.0328,
            "citation_label": "metropt3-data-description:p3",
        },
        {
            "chunk_id": 213,
            "doc_key": "doe-compressed-air-sourcebook",
            "document_title": "Improving Compressed Air System Performance",
            "chunk_index": 7,
            "content": "Compressed air systems should be monitored for leaks.",
            "page_start": 36,
            "page_end": 36,
            "equipment_scope": "generic_compressor",
            "doc_type": "technical_manual",
            "trust_level": "medium",
            "score": 0.0161,
            "rrf_score": 0.0161,
            "citation_label": "doe-compressed-air-sourcebook:p36",
        },
    ],
    "retrieval": {"method": "hybrid", "elapsed_ms": 929.1, "query_terms": {}},
    "warnings": [],
}
AGENT_ANSWER = {
    "trace_id": "trace-mock-1",
    "question": "2020 年 4 月 17 日电机电流情况如何？",
    "answer": "## 结论\n[D1] 电机电流最小 0.035 A，最大 6.36 A。",
    "execution_status": "completed",
    "intent": "data_only",
    "answer_status": "answered",
    "answer_mode": "llm",
    "tools_used": ["get_sensor_statistics"],
    "tool_calls": [
        {
            "tool": "get_sensor_statistics",
            "params": {"equipment_code": "metropt3-apu-01", "sensors": ["motor_current"]},
            "status": "ok",
            "attempts": 1,
            "elapsed_ms": 35.4,
            "reason": "统计请求",
            "stage": "initial",
            "evidence_count": 1,
        }
    ],
    "plan": [{"tool": "get_sensor_statistics", "params": {}}],
    "evidence": [
        {"label": "D1", "kind": "data", "fact_type": "statistics", "text": "最小 0.035 / 最大 6.36"},
        {
            "label": "K1",
            "kind": "knowledge",
            "doc_key": "metropt3-data-description",
            "document_title": "MetroPT-3 Dataset — Data Description",
            "page_start": 3,
            "page_end": 3,
            "chunk_id": 12,
            "equipment_scope": "metropt3_direct",
            "quote": "TP2 is the pressure at the compressor outlet.",
        },
    ],
    "warnings": [],
    "errors": [],
    "grounding": {"ok": True},
    "llm": {"status": "ok", "model": "qwen2.5:7b-instruct"},
    "total_duration_ms": 3282.8,
    "agent_version": "phase3-agent-v1",
}
AUDIT_TRACE = {
    "trace": {
        "trace_id": "trace-mock-1",
        "question": "2020 年 4 月 17 日电机电流情况如何？",
        "execution_status": "completed",
        "intent": "data_only",
        "agent_version": "phase3-agent-v1",
        "trace_schema_version": "phase3-trace-v2",
        "source": "file",
        "started_at": "2026-10-22T07:00:17.108+00:00",
        "ended_at": "2026-10-22T07:00:20.108+00:00",
        "event_count": 3,
        "warnings": [],
        "problems": [],
        "events": [
            {
                "event_id": "E0001",
                "sequence_number": 0,
                "event_type": "run_start",
                "node_name": "entry",
                "status": "running",
                "timestamp": "2026-10-22T07:00:17.108+00:00",
                "duration_ms": 0.0,
                "input_summary": {"query": "2020 年 4 月 17 日电机电流情况如何？"},
                "output_summary": None,
                "evidence_refs": [],
            },
            {
                "event_id": "E0002",
                "sequence_number": 1,
                "event_type": "tool_call",
                "node_name": "execute",
                "tool_name": "get_sensor_statistics",
                "status": "ok",
                "timestamp": "2026-10-22T07:00:17.500+00:00",
                "duration_ms": 35.4,
                "input_summary": {"equipment_code": "metropt3-apu-01", "sensors": ["motor_current"]},
                "output_summary": {"row_count": 6301},
                "evidence_refs": ["statistics"],
            },
            {
                "event_id": "E0003",
                "sequence_number": 2,
                "event_type": "answer",
                "node_name": "answer",
                "status": "completed",
                "timestamp": "2026-10-22T07:00:20.100+00:00",
                "duration_ms": 3231.9,
                "input_summary": None,
                "output_summary": {"answer_status": "answered"},
                "evidence_refs": ["D1"],
            },
        ],
    },
    "tool_calls": [
        {
            "tool": "get_sensor_statistics",
            "call_event_id": "E0002",
            "result_event_id": "E0003",
            "input_summary": {"equipment_code": "metropt3-apu-01"},
            "status": "ok",
            "duration_ms": 35.4,
            "evidence_refs": ["statistics"],
        }
    ],
    "evidence": {"has_manifest": True, "manifest_schema_version": "agent-evidence-v1", "statistics": []},
    "audits": [
        {"audit_id": "aud-mock-1", "audit_mode": "rule_only", "overall_risk": "none", "status": "completed"}
    ],
    "external_reads": [],
    "offline_only": True,
}
AUDIT_RUNS = {
    "items": [
        {
            "audit_id": "aud-mock-1",
            "trace_id": "trace-mock-1",
            "audit_mode": "rule_only",
            "status": "completed",
            "overall_risk": "none",
            "total_findings": 0,
            "high_risk_findings": 0,
            "requires_human_review": False,
            "duration_ms": 1.7,
        }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
}


def _report(mode: str = "rule_only", review_status: str = "pending") -> dict[str, Any]:
    return {
        "audit_id": "aud-mock-1",
        "trace_id": "trace-mock-1",
        "audit_mode": mode,
        "audit_version": "phase4-audit-v1",
        "rule_config_version": "rulecfg-test",
        "prompt_version": None if mode == "rule_only" else "audit-judge-v7",
        "judge_model": None if mode == "rule_only" else "qwen2.5:7b-instruct",
        "status": "partial",
        "overall_risk": "medium",
        "total_findings": 1,
        "requires_human_review": True,
        "duration_ms": 4.2 if mode == "rule_only" else 24015.8,
        "results": [
            {
                "rule_id": "R1_TOOL_WHITELIST",
                "rule_name": "工具白名单",
                "category": "unauthorized_tool",
                "severity": "critical",
                "verdict": "pass",
                "source": "rule",
                "explanation": "全部 1 次调用的工具都在白名单内",
                "evidence_refs": [],
                "related_event_ids": [],
                "details": {"tools": ["get_sensor_statistics"]},
            },
            {
                "rule_id": "R8_KNOWLEDGE_CITATION",
                "rule_name": "知识引用结构",
                "category": "citation_integrity",
                "severity": "medium",
                "verdict": "unknown",
                "source": "rule",
                "explanation": "证据不足，无法判断引用是否完整",
                "evidence_refs": [],
                "related_event_ids": ["E0003"],
                "details": {},
            },
        ],
        "findings": [
            {
                "finding_id": "F-mock-1",
                "audit_id": "aud-mock-1",
                "trace_id": "trace-mock-1",
                "rule_id": "R8_KNOWLEDGE_CITATION",
                "rule_name": "知识引用结构",
                "risk_category": "citation_integrity",
                "severity": "medium",
                "verdict": "unknown",
                "source": "rule",
                "related_event_ids": ["E0003"],
                "evidence_refs": ["K1"],
                "explanation": "缺少可核验的知识引用，需人工复核",
                "requires_human_review": True,
                "review_status": review_status,
                "details": {"reason": "no_knowledge_citation"},
            }
        ],
        "summary": {
            "total_rules": 2,
            "passed": 1,
            "failed": 0,
            "unknown": 1,
            "not_applicable": 0,
            "findings": 1,
        },
        "judge": {"status": "ok" if mode != "rule_only" else "skipped"},
        "diagnostics": [],
        "external_reads": [],
        "error": None,
    }


class MockBackend:
    """按路径路由的假后端；记录每次请求，便于断言“TUI 真的调了哪个接口”。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.chat_delay = 0.0
        self.chat_error: Exception | None = None
        self.empty_window = False
        self.manifest_missing = False
        self.review_status = "pending"
        self.audit_error: Exception | None = None
        self.closed = False

    # ---------------- HTTP 层 ---------------- #
    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        self.calls.append((method, path, body))

        if path == "/health":
            return httpx.Response(200, json=HEALTH)
        if path == "/ready":
            return httpx.Response(200, json=READY)
        if path == "/api/v1/knowledge/health":
            return httpx.Response(200, json=KNOWLEDGE_HEALTH)
        if path == "/api/v1/audit/summary":
            return httpx.Response(200, json=AUDIT_SUMMARY)
        if path == "/api/v1/audit/runs":
            return httpx.Response(200, json=AUDIT_RUNS)
        if path.startswith("/api/v1/audit/runs/"):
            return httpx.Response(200, json=_report(self._mode_for_run(), self.review_status))
        if path.startswith("/api/v1/audit/traces/") and path.endswith("/evaluate"):
            if self.audit_error is not None:
                raise self.audit_error
            return httpx.Response(200, json=_report((body or {}).get("mode", "rule_only")))
        if path.startswith("/api/v1/audit/traces/"):
            payload = json.loads(json.dumps(AUDIT_TRACE))
            if self.manifest_missing:
                payload["evidence"]["has_manifest"] = False
                payload["evidence"].pop("manifest_schema_version", None)
            return httpx.Response(200, json=payload)
        if path.startswith("/api/v1/audit/findings/") and path.endswith("/review"):
            self.review_status = (body or {}).get("review_status", "pending")
            return httpx.Response(
                200,
                json={"finding_id": "F-mock-1", "review_status": self.review_status},
            )
        if path == "/api/v1/equipment":
            return httpx.Response(200, json=EQUIPMENT)
        if path.endswith("/stats"):
            payload = json.loads(json.dumps(STATS))
            if self.empty_window:
                payload.update({"analog": [], "digital": [], "row_count": 0})
            return httpx.Response(200, json=payload)
        if path.endswith("/quality"):
            return httpx.Response(200, json=QUALITY)
        if path.endswith("/faults"):
            return httpx.Response(200, json=FAULTS)
        if path.endswith("/readings"):
            payload = json.loads(json.dumps(READINGS))
            if self.empty_window:
                payload["items"] = []
                payload["pagination"]["total"] = 0
                payload["pagination"]["returned"] = 0
            return httpx.Response(200, json=payload)
        if path == "/api/v1/knowledge/search":
            return httpx.Response(200, json=KNOWLEDGE_SEARCH)
        if path == "/api/v1/agent/chat":
            if self.chat_delay:
                await asyncio.sleep(self.chat_delay)
            if self.chat_error is not None:
                raise self.chat_error
            return httpx.Response(200, json=AGENT_ANSWER)
        if path.startswith("/api/v1/agent/traces/"):
            return httpx.Response(
                200,
                json={
                    "trace_id": "trace-mock-1",
                    "question": "q",
                    "execution_status": "completed",
                    "intent": "data_only",
                    "tools_used": ["get_sensor_statistics"],
                    "tool_calls": 1,
                    "rounds": 0,
                    "total_duration_ms": 3282.8,
                    "started_at": "2026-10-22T07:00:17",
                    "ended_at": "2026-10-22T07:00:20",
                    "evidence_refs": ["D1"],
                    "errors": [],
                    "warnings": [],
                    "event_count": 0,
                    "events": [],
                },
            )
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": f"未模拟 {method} {path}"}}
        )

    def _mode_for_run(self) -> str:
        for method, path, body in reversed(self.calls):
            if path.endswith("/evaluate") and body:
                return str(body.get("mode", "rule_only"))
        return "rule_only"

    # ---------------- 便捷断言 ---------------- #
    def client(self, **kwargs) -> IaaApiClient:
        kwargs.setdefault("max_retries", 0)
        kwargs.setdefault("retry_backoff", 0.0)
        return IaaApiClient(BASE, transport=httpx.MockTransport(self.handler), **kwargs)

    def methods_for(self, path_suffix: str) -> list[str]:
        return [method for method, path, _ in self.calls if path.endswith(path_suffix)]


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def text_of(widget: Static) -> str:
    renderable = widget.renderable
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def cell_text(cell: Any) -> str:
    if hasattr(cell, "plain"):
        return cell.plain
    return str(cell)


def table_rows(app: IaaTuiApp, selector: str) -> list[list[str]]:
    table = app.query_one(selector, DataTable)
    return [[cell_text(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


async def start(pilot, backend: MockBackend, *, wait: float = 1.0) -> IaaTuiApp:
    app = pilot.app
    await pilot.pause(wait)
    return app


@pytest.fixture()
def backend() -> MockBackend:
    return MockBackend()


def make_app(backend: MockBackend) -> IaaTuiApp:
    return create_app(BASE, transport=httpx.MockTransport(backend.handler))


# --------------------------------------------------------------------------- #
# 启动与导航
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_app_starts_and_shows_degraded_schema_honestly(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.5)
        bar = text_of(app.query_one("#status-bar-states", Static))
        assert "API" in bar and "正常" in bar
        # 主库 schema 不一致必须是“降级”，不能显示为正常
        assert "降级" in bar
        assert app.system_status is not None
        assert app.system_status.state_of("industrial_db").value == "degraded"
        rows = table_rows(app, "#status-table")
        labels = [row[0] for row in rows]
        assert labels == ["API", "工业库", "RAG", "Audit", "Embedding", "LLM"]
        assert rows[1][1] == "降级"
        # 未深度探测时 LLM 必须是“未探测”，不能假装 Ready
        assert rows[5][1] == "未探测"
        assert app.client._client.is_closed is False


@pytest.mark.asyncio
async def test_page_switching_by_navigation_and_shortcut(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.2)
        for page_id in ("equipment", "knowledge", "traces", "audit", "status"):
            await pilot.click(f"#nav-{page_id}")
            await pilot.pause(0.4)
            assert app.current_page == page_id
            assert app.query_one("#page-container").current == f"page-{page_id}"
        # 数字快捷键（焦点不在输入框时生效）
        app.query_one("#nav").focus()
        await pilot.press("1")
        await pilot.pause(0.3)
        assert app.current_page == "chat"


@pytest.mark.asyncio
async def test_help_modal_opens_and_closes(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.0)
        await pilot.press("f1")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "Screen"


# --------------------------------------------------------------------------- #
# Agent Chat
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ask_question_renders_answer_tools_and_trace(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.2)
        page = app.query_one(ChatPage)
        app.query_one("#chat-input", TextArea).text = "2020 年 4 月 17 日电机电流情况如何？"
        await pilot.pause(0.1)
        await pilot.click("#chat-send")
        await pilot.pause(1.5)

        assert page.last_answer is not None
        assert page.last_answer.answer == AGENT_ANSWER["answer"]
        assert page.last_trace_id == "trace-mock-1"
        assert app.last_trace_id == "trace-mock-1"

        children = app.query_one("#chat-log").children
        statics = [text_of(c) for c in children if isinstance(c, Static)]
        assert any("get_sensor_statistics" in text for text in statics), "工具调用必须显示"
        assert any("trace-mock-1" in text for text in statics), "trace_id 必须显示"
        assert any("completed" in text for text in statics)
        assert any(isinstance(c, Markdown) for c in children), "回答应以 Markdown 渲染"
        # 真实响应里的工具被记录到请求日志
        assert backend.methods_for("/api/v1/agent/chat") == ["POST"]


@pytest.mark.asyncio
async def test_ui_stays_responsive_while_agent_runs(backend: MockBackend):
    backend.chat_delay = 0.8
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.2)
        page = app.query_one(ChatPage)
        app.query_one("#chat-input", TextArea).text = "慢请求"
        await pilot.click("#chat-send")
        await pilot.pause(0.2)
        assert page.request_in_flight is True
        # 执行中仍可切换页面（事件循环没有被阻塞）
        await pilot.click("#nav-status")
        await pilot.pause(0.3)
        assert app.current_page == "status"
        await pilot.click("#nav-chat")
        await pilot.pause(1.5)
        assert page.request_in_flight is False
        assert page.last_trace_id == "trace-mock-1"


@pytest.mark.asyncio
async def test_cancel_wait_does_not_claim_server_cancelled(backend: MockBackend):
    backend.chat_delay = 3.0
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.2)
        page = app.query_one(ChatPage)
        app.query_one("#chat-input", TextArea).text = "很慢的请求"
        await pilot.click("#chat-send")
        await pilot.pause(0.3)
        await pilot.click("#chat-cancel")
        await pilot.pause(0.3)
        assert page.request_in_flight is False
        notes = " ".join(
            text_of(c) for c in app.query_one("#chat-log").children if isinstance(c, Static)
        )
        assert "服务端" in notes and "未被取消" in notes
        assert "取消界面等待" in text_of(app.query_one("#page-chat-message", Static))


@pytest.mark.asyncio
async def test_network_failure_is_surfaced(backend: MockBackend):
    backend.chat_error = httpx.ConnectError("connection refused")
    app = make_app(backend)
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.2)
        page = app.query_one(ChatPage)
        app.query_one("#chat-input", TextArea).text = "会失败的问题"
        await pilot.click("#chat-send")
        await pilot.pause(1.2)
        assert page.last_answer is None
        message = text_of(app.query_one("#page-chat-message", Static))
        assert "Agent 调用失败" in message
        notes = " ".join(
            text_of(c) for c in app.query_one("#chat-log").children if isinstance(c, Static)
        )
        assert "调用失败" in notes


# --------------------------------------------------------------------------- #
# Equipment / Knowledge
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_equipment_page_renders_real_response(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-equipment")
        await pilot.pause(1.2)
        stats = table_rows(app, "#equipment-stats-table")
        assert stats[0][0] == "motor_current"
        assert stats[0][1] == "模拟量"
        assert stats[1][1] == "数字量"
        quality = text_of(app.query_one("#equipment-quality", Static))
        assert "样本覆盖率" in quality
        assert "不等于设备故障" in quality
        faults = table_rows(app, "#equipment-faults-table")
        assert faults[0][0] == "E1"
        readings = table_rows(app, "#equipment-readings-table")
        assert len(readings) == 3
        chart = text_of(app.query_one("#equipment-chart", Static))
        assert "motor_current" in chart and "未插值" in chart


@pytest.mark.asyncio
async def test_equipment_empty_window_is_explicit(backend: MockBackend):
    backend.empty_window = True
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await app.page("equipment").activate()
        await pilot.pause(1.2)
        message = text_of(app.query_one("#page-equipment-message", Static))
        assert "没有可统计" in message or "没有采样记录" in text_of(
            app.query_one("#equipment-chart", Static)
        )
        chart = text_of(app.query_one("#equipment-chart", Static))
        assert "不插值" in chart


@pytest.mark.asyncio
async def test_knowledge_search_and_generic_scope_warning(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-knowledge")
        await pilot.pause(0.6)
        app.query_one("#knowledge-query", Input).value = "TP2 代表什么"
        await app.page("knowledge").action_search()
        await pilot.pause(0.8)
        rows = table_rows(app, "#knowledge-table")
        assert len(rows) == 2
        assert rows[0][4] == "metropt3 direct"
        assert "rrf" in rows[0][1]

        table = app.query_one("#knowledge-table", DataTable)
        table.move_cursor(row=1)  # generic_compressor 结果
        await pilot.pause(0.4)
        detail = app.query_one("#knowledge-detail", Markdown)
        assert "通用资料" in _markdown_text(detail)


@pytest.mark.asyncio
async def test_knowledge_empty_result_is_explicit(backend: MockBackend):
    async def empty_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/knowledge/search":
            return httpx.Response(
                200, json={"query": "x", "method": "hybrid", "top_k": 5, "results": [], "warnings": []}
            )
        return await backend.handler(request)

    app = create_app(BASE, transport=httpx.MockTransport(empty_handler))
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.pause(1.0)
        await app.page("knowledge").activate()
        await pilot.pause(0.5)
        app.query_one("#knowledge-query", Input).value = "找不到的内容"
        await app.page("knowledge").action_search()
        await pilot.pause(0.6)
        assert "没有检索到结果" in text_of(app.query_one("#page-knowledge-message", Static))
        assert "没有检索结果" in _markdown_text(app.query_one("#knowledge-detail", Markdown))


# --------------------------------------------------------------------------- #
# Trace Explorer
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_trace_explorer_loads_events_and_detail(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-traces")
        await pilot.pause(0.6)
        app.query_one("#trace-id", Input).value = "trace-mock-1"
        await app.page("traces").action_load_trace()
        await pilot.pause(0.8)
        rows = table_rows(app, "#trace-events")
        assert [row[1] for row in rows] == ["run_start", "tool_call", "answer"]
        meta = text_of(app.query_one("#trace-meta", Static))
        assert "agent-evidence-v1" in meta
        assert "has_manifest" not in meta  # 不直接暴露原始字段名
        table = app.query_one("#trace-events", DataTable)
        table.move_cursor(row=1)
        await pilot.pause(0.4)
        detail = _markdown_text(app.query_one("#trace-detail", Markdown))
        assert "get_sensor_statistics" in detail
        assert "input_summary" not in detail
        assert "motor_current" in detail or "equipment_code" in detail


@pytest.mark.asyncio
async def test_trace_explorer_marks_missing_evidence_manifest(backend: MockBackend):
    backend.manifest_missing = True
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await app.page("traces").activate()
        await pilot.pause(0.5)
        app.query_one("#trace-id", Input).value = "trace-old"
        await app.page("traces").action_load_trace()
        await pilot.pause(0.8)
        meta = text_of(app.query_one("#trace-meta", Static))
        assert "缺少 evidence manifest" in meta


@pytest.mark.asyncio
async def test_trace_filter_by_event_type(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await app.page("traces").activate()
        await pilot.pause(0.4)
        app.query_one("#trace-id", Input).value = "trace-mock-1"
        await app.page("traces").action_load_trace()
        await pilot.pause(0.6)
        from textual.widgets import Select

        select = app.query_one("#trace-filter", Select)
        select.value = "tool_call"
        await pilot.pause(0.5)
        rows = table_rows(app, "#trace-events")
        assert len(rows) == 1
        assert rows[0][1] == "tool_call"


# --------------------------------------------------------------------------- #
# Agent-Probe 审计
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audit_defaults_to_rule_only_and_shows_unknown_not_pass(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-audit")
        await pilot.pause(0.8)
        page = app.page("audit")
        assert page._selected_mode() == "rule_only"
        app.query_one("#audit-trace-id", Input).value = "trace-mock-1"
        await page.action_run_audit()
        await pilot.pause(1.2)

        summary = text_of(app.query_one("#audit-summary", Static))
        assert "rule_only" in summary
        assert "medium" in summary
        assert "UNKNOWN 1" in summary

        findings = table_rows(app, "#audit-findings")
        assert findings[0][3] == "UNKNOWN"
        assert "PASS" not in findings[0]
        results = table_rows(app, "#audit-results")
        verdicts = {row[0]: row[3] for row in results}
        assert verdicts["R1_TOOL_WHITELIST"] == "PASS"
        assert verdicts["R8_KNOWLEDGE_CITATION"] == "UNKNOWN"
        bodies = [body for method, path, body in backend.calls if path.endswith("/evaluate")]
        assert bodies and bodies[0]["mode"] == "rule_only"


@pytest.mark.asyncio
async def test_audit_mode_switch_sends_hybrid(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-audit")
        await pilot.pause(0.6)
        radio = app.query_one("#audit-mode", RadioSet)
        hybrid = radio.query_one("#mode-hybrid", RadioButton)
        hybrid.value = True
        await pilot.pause(0.3)
        page = app.page("audit")
        assert page._selected_mode() == "hybrid"
        app.query_one("#audit-trace-id", Input).value = "trace-mock-1"
        await page.action_run_audit()
        await pilot.pause(1.2)
        bodies = [body for method, path, body in backend.calls if path.endswith("/evaluate")]
        assert bodies[-1]["mode"] == "hybrid"
        summary = text_of(app.query_one("#audit-summary", Static))
        assert "hybrid" in summary and "LLM Judge 状态 ok" in summary


@pytest.mark.asyncio
async def test_audit_failure_is_reported_as_not_passed(backend: MockBackend):
    backend.audit_error = httpx.ConnectError("judge down")
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        page = app.page("audit")
        await page.activate()
        await pilot.pause(0.5)
        app.query_one("#audit-trace-id", Input).value = "trace-mock-1"
        await page.action_run_audit()
        await pilot.pause(1.2)
        assert "审计失败" in text_of(app.query_one("#page-audit-message", Static))
        detail = _markdown_text(app.query_one("#audit-detail", Markdown))
        assert "不会被当作“通过”" in detail


@pytest.mark.asyncio
async def test_audit_review_confirm_patches_and_refreshes(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        page = app.page("audit")
        await page.activate()
        await pilot.pause(0.5)
        app.query_one("#audit-trace-id", Input).value = "trace-mock-1"
        await page.action_run_audit()
        await pilot.pause(1.2)
        page._selected_finding = "F-mock-1"
        page._update_review_buttons()
        await page._review("confirmed")
        await pilot.pause(0.8)
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.click("#confirm-ok")
        await pilot.pause(1.2)
        patches = backend.methods_for("/review")
        assert patches == ["PATCH"]
        assert backend.review_status == "confirmed"
        # 显示的是重新读取服务端后的状态
        assert page._findings[0].review_status == "confirmed"
        assert "已提交" in text_of(app.query_one("#page-audit-message", Static))


# --------------------------------------------------------------------------- #
# 尺寸变化 / 退出
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_terminal_resize_keeps_app_usable(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(1.0)
        await pilot.click("#nav-equipment")
        await pilot.pause(1.0)
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.8)
        assert app.size.width == 80
        assert app.current_page == "equipment"
        assert app.query_one("#equipment-stats-table", DataTable).row_count >= 1
        await pilot.resize_terminal(120, 32)
        await pilot.pause(0.5)
        assert app.current_page == "equipment"


@pytest.mark.asyncio
async def test_client_is_closed_on_exit(backend: MockBackend):
    app = make_app(backend)
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause(1.0)
        client = app.client
        assert client._client.is_closed is False
    assert client._client.is_closed is True


def test_tui_modules_do_not_touch_database_directly():
    """静态守卫：TUI 不得直接连接数据库（只能走 HTTP API）。"""
    from pathlib import Path

    tui_dir = Path(__file__).resolve().parents[1] / "app" / "tui"
    forbidden = ("import psycopg", "from psycopg", "import sqlalchemy", "from sqlalchemy")
    offenders: list[str] = []
    for path in tui_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == []


def _markdown_text(widget: Markdown) -> str:
    """把 Markdown 组件已渲染的块文本拼起来（用于断言可见文案）。

    Textual 的 Markdown 把段落存在内部的 `_text`，代码块存在 `_code`；
    这里两者都收集，避免断言只能看一半内容。
    """
    parts: list[str] = []
    for child in widget.walk_children():
        code = getattr(child, "code", None)
        if isinstance(code, str):
            parts.append(code)
        text = getattr(child, "_text", None)
        if text is not None:
            parts.append(text.plain if hasattr(text, "plain") else str(text))
    return "\n".join(parts)
