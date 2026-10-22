#!/usr/bin/env python
"""阶段 5 演示脚本：在真实后端上跑完整业务闭环，并保存真实终端截图。

做的是**真实演示**：真实 FastAPI、真实 PostgreSQL、真实 Ollama。
脚本不伪造任何工具调用或审计结论，所有打印值都来自后端响应。

用法（先启动后端：`scripts/start_api.sh`）：

    python scripts/demo_phase5.py                 # 完整演示
    python scripts/demo_phase5.py --skip-hybrid   # 跳过 LLM 审计（约 20-40 秒）

产物：
    docs/figures/phase5/*.svg                    # Textual 渲染的终端截图（真实运行）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.tui.app import create_app  # noqa: E402
from app.tui.client import IaaApiClient  # noqa: E402

SHOT_DIR = PROJECT_ROOT / "docs" / "figures" / "phase5"
DEMO_QUESTION = "2020-04-17 当天电机电流是否异常？结合公开技术资料分析可能原因。"
HISTORICAL_TRACE = "32885113cbb04d3f92c15fb134d239ce"  # 阶段 4 评估用的真实历史 Trace
SIZE = (150, 46)


def hr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


async def wait_until(pilot, predicate, *, timeout: float, label: str) -> None:
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        await pilot.pause(0.25)
        waited += 0.25
    raise SystemExit(f"演示中止：等待“{label}”超时（{timeout}s）")


def save(app, name: str) -> None:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{name}.svg"
    path.write_text(app.export_screenshot(), encoding="utf-8")
    print(f"  [截图] {path.relative_to(PROJECT_ROOT)}")


async def run(base_url: str, skip_hybrid: bool) -> int:
    token = os.environ.get("IAA_AUDIT_TOKEN") or None
    client = IaaApiClient(base_url, audit_token=token)

    probe = httpx.get(f"{base_url}/health", timeout=5.0, trust_env=False)
    if probe.status_code != 200:
        print(f"后端 {base_url} 不可用，请先运行 scripts/start_api.sh", file=sys.stderr)
        return 2

    app = create_app(base_url, audit_token=token)
    async with app.run_test(size=SIZE) as pilot:
        # ---------------- 1. 系统状态 ----------------
        hr("步骤 1-2：启动 TUI 并检查系统状态")
        await wait_until(
            pilot, lambda: app.system_status is not None, timeout=30, label="系统状态探测"
        )
        status = app.system_status
        for component in status.components:
            print(f"  {component.label:<10} {component.state.value:<12} {component.detail}")
        for note in status.notes:
            print(f"  ! {note}")
        await pilot.click("#nav-status")
        await wait_until(
            pilot,
            lambda: app.query_one("#status-table").row_count > 0,
            timeout=20,
            label="状态表",
        )
        await pilot.pause(0.5)
        save(app, "01-system-status")

        # ---------------- 2. Agent Chat ----------------
        hr("步骤 3-7：Agent Chat 真实问答（LangGraph + 数据工具 + RAG）")
        await pilot.click("#nav-chat")
        await pilot.pause(0.5)
        chat = app.page("chat")
        app.query_one("#chat-input").text = DEMO_QUESTION
        await pilot.pause(0.3)
        print(f"  用户问题：{DEMO_QUESTION}")
        await pilot.click("#chat-send")
        await pilot.pause(3.0)
        print("  （执行中截图，展示忙碌状态与可切换页面）")
        await wait_until(
            pilot, lambda: chat.last_answer is not None, timeout=180, label="Agent 回答"
        )
        answer = chat.last_answer
        assert answer is not None
        print(f"  执行状态：{answer.execution_status} / 意图 {answer.intent} / 方式 {answer.answer_mode}")
        print(f"  服务端耗时：{answer.total_duration_ms:.0f} ms")
        print(f"  工具调用：{[(c.tool, c.status, round(c.elapsed_ms)) for c in answer.tool_calls]}")
        print(f"  证据条数：{len(answer.evidence)} / trace_id：{answer.trace_id}")
        print("  回答摘录：")
        for line in answer.answer.splitlines()[:12]:
            print(f"    {line}")
        await pilot.pause(0.5)
        save(app, "02-agent-chat")
        trace_id = answer.trace_id

        # ---------------- 3. Trace Explorer ----------------
        hr("步骤 8-9：Trace Explorer 查看工具执行过程")
        app.open_trace(trace_id)
        traces = app.page("traces")
        await wait_until(
            pilot, lambda: len(traces._events) > 0, timeout=60, label="Trace 事件"
        )
        await pilot.pause(0.5)
        for event in traces._events:
            print(f"  {event.event_id} {event.event_type:<12} {event.node_name or '':<10} "
                  f"{event.tool_name or '':<26} {event.status or ''}")
        detail = traces._detail
        print(f"  证据清单：{'有' if detail and detail.has_evidence_manifest else '无'}"
              f"（schema {detail.evidence.get('manifest_schema_version') if detail else '?'}）")
        save(app, "03-trace-explorer")

        # ---------------- 4. Rule Only 审计 ----------------
        hr("步骤 10-12：Agent-Probe 审计（Rule Only）")
        app.open_audit(trace_id)
        audit = app.page("audit")
        await pilot.pause(0.8)
        print(f"  审计模式：{audit._selected_mode()}（默认，确定性规则）")
        await audit.action_run_audit()
        await wait_until(pilot, lambda: audit._report is not None, timeout=120, label="规则审计")
        report = audit._report
        assert report is not None
        print(f"  审计 ID：{report.audit_id} · 状态 {report.status} · 总体风险 {report.overall_risk}")
        print(f"  规则结论：{report.summary}")
        for result in report.results:
            print(f"    {result.rule_id:<28} {result.verdict:<15} {result.explanation[:52]}")
        if report.findings:
            for finding in report.findings:
                print(f"  发现 {finding.finding_id} {finding.rule_id} "
                      f"[{finding.verdict}/{finding.severity}] {finding.explanation[:60]}")
        else:
            print("  未发现 FAIL/UNKNOWN 风险发现")
        await pilot.pause(0.5)
        save(app, "04-audit-rule-only")

        # ---------------- 5. Hybrid 审计 ----------------
        if not skip_hybrid:
            hr("步骤 10b：Agent-Probe 审计（Hybrid，真实 LLM Judge）")
            from textual.widgets import RadioButton, RadioSet

            radio = app.query_one("#audit-mode", RadioSet)
            radio.query_one("#mode-hybrid", RadioButton).value = True
            await pilot.pause(0.3)
            print(f"  审计模式：{audit._selected_mode()}（规则 + 语义 Judge）")
            await audit.action_run_audit()
            await pilot.pause(2.0)
            await pilot.click("#nav-status")
            print("  （审计进行中切到系统状态页，证明界面未被阻塞）")
            await pilot.pause(1.0)
            await pilot.click("#nav-audit")
            await wait_until(
                pilot,
                lambda: audit._report is not None and audit._report.audit_mode == "hybrid",
                timeout=300,
                label="Hybrid 审计",
            )
            report = audit._report
            assert report is not None
            print(f"  审计 ID：{report.audit_id} · 状态 {report.status} "
                  f"· 总体风险 {report.overall_risk} · 耗时 {report.duration_ms:.0f} ms")
            print(f"  规则结论：{report.summary}")
            judge = [item for item in report.results if item.source == "judge"]
            for item in judge:
                print(f"    Judge {item.rule_id:<28} {item.verdict:<15} {item.explanation[:50]}")
            save(app, "05-audit-hybrid")

        # ---------------- 6. 历史回放 ----------------
        hr("步骤 10c：历史回放（已有审计结论，并非本次实时执行）")
        app.open_audit(HISTORICAL_TRACE)
        await wait_until(
            pilot,
            lambda: audit._report is not None and audit._report.trace_id == HISTORICAL_TRACE,
            timeout=60,
            label="历史审计",
        )
        await pilot.pause(0.5)
        print(f"  【历史回放】trace {HISTORICAL_TRACE}")
        report = audit._report
        assert report is not None
        print(f"  已有审计：{report.audit_id} · {report.audit_mode} · 风险 {report.overall_risk}"
              f" · 发现 {report.total_findings}")
        for finding in report.findings:
            print(f"    {finding.rule_id} [{finding.verdict}/{finding.severity}] "
                  f"{finding.explanation[:70]}")
        save(app, "06-audit-historical")

        # ---------------- 7. Knowledge ----------------
        hr("步骤 13：Knowledge 检索（RAG）")
        await pilot.click("#nav-knowledge")
        await pilot.pause(0.6)
        knowledge = app.page("knowledge")
        app.query_one("#knowledge-query").value = "电机电流异常 与 储气压力 故障排查"
        await knowledge._search()
        await wait_until(
            pilot, lambda: len(knowledge._results) > 0, timeout=120, label="知识检索"
        )
        await pilot.pause(0.5)
        for index, hit in enumerate(knowledge._results, start=1):
            print(f"  {index}. {hit.doc_key} p{hit.page_start}-{hit.page_end} "
                  f"scope={hit.equipment_scope} score={hit.score:.5f} chunk={hit.chunk_id}")
        save(app, "07-knowledge")

        # ---------------- 8. Equipment ----------------
        hr("步骤 14：Equipment 设备数据（真实传感器统计与质量）")
        await pilot.click("#nav-equipment")
        await pilot.pause(0.8)
        equipment = app.page("equipment")
        app.query_one("#equipment-start").value = "2020-04-17T00:00:00"
        app.query_one("#equipment-end").value = "2020-04-18T00:00:00"
        app.query_one("#equipment-sensors").value = "motor_current,dv_pressure,tp2"
        await equipment._query()
        await pilot.pause(1.5)
        from textual.widgets import DataTable, Static

        table = app.query_one("#equipment-stats-table", DataTable)
        for index in range(table.row_count):
            print("  " + " | ".join(str(cell) for cell in table.get_row_at(index)))
        quality = app.query_one("#equipment-quality", Static).renderable
        text = quality.plain if hasattr(quality, "plain") else str(quality)
        for line in text.splitlines()[:12]:
            print(f"  {line}")
        save(app, "08-equipment")

    await client.close()
    hr("演示完成")
    print("浏览器/编辑器可打开 docs/figures/phase5/*.svg 查看真实终端截图。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段 5 端到端演示")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("IAA_API_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--skip-hybrid", action="store_true", help="跳过 LLM 混合审计")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.base_url.rstrip("/"), args.skip_hybrid))


if __name__ == "__main__":
    raise SystemExit(main())
