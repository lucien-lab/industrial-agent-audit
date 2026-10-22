"""Agent-Probe 审计页面：对指定 trace 发起审计、查看风险发现与人工复核。

语义边界（必须保留）：

- 这是**离线可观测性审计**，不是运行时拦截：审计针对已经完成的 Trace；
- `rule_only` 为默认（毫秒级、确定性）；`llm_only` / `hybrid` 会调用本机 Ollama，
  单次约 18–40 秒，期间界面保持可响应、可切换页面；
- `UNKNOWN`（缺少证据 / Judge 未成功）永远显示为 UNKNOWN，绝不显示为 PASS；
- 人工复核只写入找到的 finding 的复核状态，不覆盖历史审计结论。
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, RadioButton, RadioSet, Static, TabbedContent, TabPane

from app.tui.client import ApiError
from app.tui.formatting import (
    fmt_ms,
    fmt_ts,
    json_block,
    risk_style,
    severity_style,
    truncate,
    verdict_label,
    verdict_style,
)
from app.tui.models import AuditReport, Finding, RuleResult
from app.tui.pages.base import BasePage
from app.tui.pages.modals import ConfirmScreen

MODES = [
    ("Rule Only（默认，毫秒级）", "rule_only"),
    ("LLM Only（约 18–40 s）", "llm_only"),
    ("Hybrid（规则 + Judge，约 20–40 s）", "hybrid"),
]


class AuditPage(BasePage):
    PAGE_ID = "audit"
    PAGE_TITLE = "Audit Reports"
    PAGE_SUMMARY = "Agent-Probe 行为审计（rule / llm / hybrid）"

    BINDINGS = [Binding("ctrl+s", "run_audit", "发起审计", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._report: AuditReport | None = None
        self._findings: list[Finding] = []
        self._selected_finding: str | None = None
        self._pending_trace_id: str | None = None
        self._busy = False
        self._started_at: datetime | None = None
        self._timer: object | None = None

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Audit Reports · Agent-Probe 行为审计", classes="page-header")
            yield Static(
                "离线审计已完成的行为，不是运行时拦截。rule_only 为默认；"
                "LLM 模式会调用本机 qwen2.5:7b（约 18–40 秒）。UNKNOWN ≠ PASS。",
                classes="page-hint",
            )
            with Horizontal(classes="controls"):
                yield Input(placeholder="trace_id", id="audit-trace-id")
                yield Button("发起审计", id="audit-run", variant="primary")
                yield Button("刷新历史", id="audit-history")
                yield Button("确认复核", id="audit-review-confirm", variant="warning", disabled=True)
                yield Button("驳回复核", id="audit-review-dismiss", disabled=True)
            with Horizontal():
                yield RadioSet(
                    *[RadioButton(label, value=(value == "rule_only"), id=f"mode-{value}")
                      for label, value in MODES],
                    id="audit-mode",
                )
            yield Static("", id="audit-progress")
            yield Static("", id="page-audit-message", classes="message")
            yield Static("", id="audit-summary")
            with Horizontal():
                with TabbedContent(id="audit-tabs", classes="split-left"):
                    with TabPane("风险发现", id="tab-findings"):
                        yield DataTable(id="audit-findings", cursor_type="row")
                    with TabPane("规则结果", id="tab-results"):
                        yield DataTable(id="audit-results", cursor_type="row")
                    with TabPane("历史运行", id="tab-runs"):
                        yield DataTable(id="audit-runs", cursor_type="row")
                with VerticalScroll(classes="split-right"):
                    yield Markdown("_尚未发起审计。_", id="audit-detail")

    def on_mount(self) -> None:
        self.query_one("#audit-results", DataTable).add_columns(
            "规则", "类别", "严重度", "结论", "来源", "说明"
        )
        self.query_one("#audit-findings", DataTable).add_columns(
            "发现", "规则", "严重度", "结论", "复核", "说明"
        )
        self.query_one("#audit-runs", DataTable).add_columns(
            "审计 ID", "Trace", "模式", "状态", "风险", "发现", "耗时"
        )

    # ------------------------------------------------------------------ #
    # 外部入口
    # ------------------------------------------------------------------ #
    def prefill(self, trace_id: str) -> None:
        self._pending_trace_id = trace_id
        try:
            self.query_one("#audit-trace-id", Input).value = trace_id
        except Exception:  # pragma: no cover
            pass

    async def activate(self, *, force: bool = False) -> None:
        await super().activate(force=force)
        if self._pending_trace_id:
            trace_id = self._pending_trace_id
            self._pending_trace_id = None
            self.query_one("#audit-trace-id", Input).value = trace_id
            await self._load_trace_audits(trace_id)
        elif force:
            await self._load_history()

    async def load(self, *, force: bool = False) -> None:
        await self._load_history()

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "audit-run":
            await self.action_run_audit()
        elif button_id == "audit-history":
            await self._load_history()
        elif button_id == "audit-review-confirm":
            await self._review("confirmed")
        elif button_id == "audit-review-dismiss":
            await self._review("dismissed")
    def _selected_mode(self) -> str:
        pressed = self.query_one("#audit-mode", RadioSet).pressed_button
        if pressed is not None and pressed.id and pressed.id.startswith("mode-"):
            return pressed.id.removeprefix("mode-")
        return "rule_only"

    async def action_run_audit(self) -> None:
        if self._busy:
            self.notify_status("审计正在进行，请等待或切换页面。", "warn")
            return
        trace_id = self.query_one("#audit-trace-id", Input).value.strip()
        if not trace_id:
            self.notify_status("请输入 trace_id（可先在 Agent Chat 提问获取）。", "warn")
            return
        mode = self._selected_mode()
        self._busy = True
        self._started_at = datetime.now()
        self._set_buttons_running(True)
        hint = "（LLM Judge 约 18–40 秒，界面保持可响应）" if mode != "rule_only" else ""
        self.notify_status(f"发起 {mode} 审计 {trace_id} …{hint}", "dim")
        self.launch(self._evaluate(trace_id, mode), name="audit-run")
        self._start_progress(mode)

    async def _evaluate(self, trace_id: str, mode: str) -> None:
        try:
            report = await self.client.audit_evaluate(trace_id, mode=mode)
        except ApiError as exc:
            self._finish_busy()
            if self.is_cancelled(exc):
                return
            self.notify_status(f"审计失败：{exc}", "error")
            self.query_one("#audit-detail", Markdown).update(
                "### 审计未成功\n\n"
                f"- trace_id：`{trace_id}`\n- 模式：`{mode}`\n- 错误：{exc}\n\n"
                "> 审计失败不会被当作“通过”。若 LLM 不可用，可改用 Rule Only 模式。"
            )
            return
        except Exception as exc:  # pragma: no cover
            self._finish_busy()
            self.notify_status(f"Unexpected error: {type(exc).__name__}", "error")
            return
        self._finish_busy()
        self._show_report(report)
        self.notify_status(
            f"审计完成：{report.audit_mode} · 风险 {report.overall_risk} · "
            f"发现 {report.total_findings} · {fmt_ms(report.duration_ms)}",
            "ok",
        )
        await self._load_history()

    # ------------------------------------------------------------------ #
    # 进度与忙碌状态
    # ------------------------------------------------------------------ #
    def _start_progress(self, mode: str) -> None:
        if not self._busy:  # 规则审计可能在计时器建立前就已完成
            return
        progress = self.query_one("#audit-progress", Static)

        def tick() -> None:
            if not self._busy or self._started_at is None:
                return
            elapsed = (datetime.now() - self._started_at).total_seconds()
            progress.update(f"● 审计中（{mode}）已等待 {elapsed:.0f}s · 可切换页面 · Esc 关闭弹窗")

        tick()
        self._timer = self.set_interval(1.0, tick)

    def _set_buttons_running(self, running: bool) -> None:
        try:
            self.query_one("#audit-run", Button).disabled = running
        except Exception:  # pragma: no cover
            pass

    def _finish_busy(self) -> None:
        self._busy = False
        self._started_at = None
        if self._timer is not None:
            try:
                self._timer.stop()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - 已停止
                pass
            self._timer = None
        self._set_buttons_running(False)
        try:
            self.query_one("#audit-progress", Static).update("")
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _show_report(self, report: AuditReport) -> None:
        self._report = report
        self._findings = list(report.findings)
        summary = report.summary or {}
        verdicts = (
            f"PASS {summary.get('passed', 0)} · FAIL {summary.get('failed', 0)} · "
            f"UNKNOWN {summary.get('unknown', 0)} · N/A {summary.get('not_applicable', 0)}"
        )
        judge_status = (report.judge or {}).get("status") or "—"
        mode_note = (
            "确定性规则，无 LLM 调用"
            if report.audit_mode == "rule_only"
            else f"LLM Judge 状态 {judge_status}，模型 {report.judge_model or '?'}"
        )
        self.query_one("#audit-summary", Static).update(
            f"审计 {report.audit_id} · trace {report.trace_id}\n"
            f"模式 {report.audit_mode} · 状态 {report.status} · 总体风险 {report.overall_risk} · "
            f"发现 {report.total_findings} · 需人工复核 "
            f"{'是' if report.requires_human_review else '否'}\n"
            f"规则：{verdicts} · 耗时 {fmt_ms(report.duration_ms)} · {mode_note}\n"
            f"版本 audit {report.audit_version} · rulecfg {report.rule_config_version}"
            + (f" · prompt {report.prompt_version}" if report.prompt_version else "")
            + (f"\n⚠ {report.error}" if report.error else "")
        )
        self._render_results(report.results)
        self._render_findings(report.findings)
        self.query_one("#audit-detail", Markdown).update(self._report_markdown(report))

    def _render_results(self, results: list[RuleResult]) -> None:
        table = self.query_one("#audit-results", DataTable)
        table.clear()
        for item in results:
            label = Text(verdict_label(item.verdict), style=verdict_style(item.verdict))
            severity = Text(item.severity, style=severity_style(item.severity))
            table.add_row(
                item.rule_id,
                item.category,
                severity,
                label,
                item.source,
                truncate(item.explanation, 60),
                key=item.rule_id,
            )

    def _render_findings(self, findings: list[Finding]) -> None:
        table = self.query_one("#audit-findings", DataTable)
        table.clear()
        for item in findings:
            label = Text(verdict_label(item.verdict), style=verdict_style(item.verdict))
            severity = Text(item.severity, style=severity_style(item.severity))
            table.add_row(
                item.finding_id,
                item.rule_id,
                severity,
                label,
                item.review_status,
                truncate(item.explanation, 60),
                key=item.finding_id,
            )
        if not findings:
            table.add_row("—", "—", "—", "无 FAIL/UNKNOWN 发现", "—", "—")
        self._update_review_buttons()

    def _report_markdown(self, report: AuditReport) -> str:
        lines = [
            f"### 审计报告 {report.audit_id}",
            "",
            f"- Trace：`{report.trace_id}` · schema {report.audit_version}",
            f"- 模式：**{report.audit_mode}** · 状态 `{report.status}` · "
            f"总体风险 **{report.overall_risk}**",
            f"- 开始：{fmt_ts(report.started_at)} · 完成：{fmt_ts(report.completed_at)} · "
            f"耗时 {fmt_ms(report.duration_ms)}",
            "",
        ]
        if report.findings:
            lines.append("#### 风险发现")
            lines.append("")
            for item in report.findings:
                lines += [
                    f"**{item.rule_id} — {item.rule_name}** "
                    f"（{verdict_label(item.verdict)} / {item.severity} / "
                    f"来源 {item.source} / 复核 {item.review_status}）",
                    "",
                    f"- 类别：{item.risk_category}",
                    f"- 说明：{item.explanation}",
                ]
                if item.related_event_ids:
                    lines.append(f"- 关联事件：{', '.join(item.related_event_ids)}")
                if item.evidence_refs:
                    lines.append(f"- 证据引用：{', '.join(item.evidence_refs)}")
                if item.confidence is not None:
                    lines.append(f"- Judge 置信度：{item.confidence:.2f}")
                if item.details:
                    lines += ["", "```json", json_block(item.details, limit=1200), "```"]
                lines.append("")
        else:
            lines += [
                "#### 未发现 FAIL / UNKNOWN",
                "",
                "所有适用规则均通过，或规则不适用（NOT_APPLICABLE）。"
                "注意：`NOT_APPLICABLE` 表示该规则对本 Trace 不适用，不代表行为正确。",
            ]
        unknowns = [r for r in report.results if r.verdict == "unknown"]
        if unknowns:
            lines += ["", "#### UNKNOWN（缺少证据，不能视为通过）"]
            lines += [f"- `{item.rule_id}`：{truncate(item.explanation, 160)}" for item in unknowns]
        if report.diagnostics:
            lines += ["", "#### 诊断", "```json", json_block(report.diagnostics[:6], limit=1200), "```"]
        return "\n".join(lines)

    def _finding_markdown(self, finding: Finding) -> str:
        lines = [
            f"### {finding.rule_id} — {finding.rule_name}",
            "",
            f"- 发现 ID：`{finding.finding_id}`",
            f"- 结论：**{verdict_label(finding.verdict)}** · 严重度 {finding.severity} · "
            f"来源 {finding.source}",
            f"- 类别：{finding.risk_category} · 需人工复核：{'是' if finding.requires_human_review else '否'}",
            f"- 复核状态：{finding.review_status}",
            "",
            f"**说明**：{finding.explanation}",
        ]
        if finding.related_event_ids:
            lines.append(f"- 关联事件：{', '.join(finding.related_event_ids)}")
        if finding.evidence_refs:
            lines.append(f"- 证据引用：{', '.join(finding.evidence_refs)}")
        if finding.details:
            lines += ["", "```json", json_block(finding.details, limit=1500), "```"]
        return "\n".join(lines)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = event.data_table.id
        key = str(event.row_key.value) if event.row_key else None
        if table_id == "audit-findings" and key:
            finding = next((item for item in self._findings if item.finding_id == key), None)
            if finding is not None:
                self._selected_finding = finding.finding_id
                self.query_one("#audit-detail", Markdown).update(self._finding_markdown(finding))
                self._update_review_buttons()
        elif table_id == "audit-results" and key:
            result = next(
                (item for item in (self._report.results if self._report else []) if item.rule_id == key),
                None,
            )
            if result is not None:
                self.query_one("#audit-detail", Markdown).update(
                    f"### {result.rule_id} — {result.rule_name}\n\n"
                    f"- 结论：**{verdict_label(result.verdict)}** · 严重度 {result.severity} · "
                    f"来源 {result.source} · 类别 {result.category}\n\n"
                    f"**说明**：{result.explanation or '（无）'}\n\n"
                    + (
                        "```json\n" + json_block(result.details, limit=1200) + "\n```"
                        if result.details
                        else ""
                    )
                )

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "audit-runs":
            return
        audit_id = str(event.row_key.value) if event.row_key else None
        if not audit_id:
            return
        try:
            report = await self.client.audit_run(audit_id)
        except ApiError as exc:
            self.notify_status(f"读取审计结果失败：{exc}", "error")
            return
        self._show_report(report)
        self.notify_status(f"已加载历史审计 {audit_id}（服务端原始结果）", "ok")

    # ------------------------------------------------------------------ #
    # 历史与 trace 关联审计
    # ------------------------------------------------------------------ #
    async def _load_history(self) -> None:
        try:
            page = await self.client.audit_runs(limit=50)
        except ApiError as exc:
            self.notify_status(f"历史审计加载失败：{exc}", "error")
            return
        table = self.query_one("#audit-runs", DataTable)
        table.clear()
        for item in page.items:
            table.add_row(
                item.audit_id,
                item.trace_id[:12] + "…",
                item.audit_mode,
                item.status,
                Text(item.overall_risk, style=risk_style(item.overall_risk)),
                str(item.total_findings),
                fmt_ms(item.duration_ms),
                key=item.audit_id,
            )
        if not page.items:
            table.add_row("—", "—", "—", "—", "无审计记录", "0", "—")
        self.set_message(
            f"历史审计 {page.total} 条（显示最近 {len(page.items)} 条）。"
            "选中一行可加载完整报告（服务端原始结果）。",
            "dim",
        )

    async def _load_trace_audits(self, trace_id: str) -> None:
        try:
            page = await self.client.audit_runs(trace_id=trace_id, limit=10)
        except ApiError as exc:
            self.notify_status(f"该 Trace 的审计记录读取失败：{exc}", "error")
            return
        if page.items:
            try:
                report = await self.client.audit_run(page.items[0].audit_id)
            except ApiError as exc:
                self.notify_status(f"读取已有审计失败：{exc}", "error")
                return
            self._show_report(report)
            self.notify_status(
                f"该 Trace 已有 {page.total} 次审计，已加载最新一次"
                f"（{page.items[0].audit_mode} / {page.items[0].overall_risk}）"
                "——这是历史回放，不是本次实时结果",
                "ok",
            )
        else:
            self.notify_status("该 Trace 尚无审计记录，可点击“发起审计”。", "dim")
        await self._load_history()

    # ------------------------------------------------------------------ #
    # 人工复核
    # ------------------------------------------------------------------ #
    def _update_review_buttons(self) -> None:
        enabled = self._selected_finding is not None
        try:
            self.query_one("#audit-review-confirm", Button).disabled = not enabled
            self.query_one("#audit-review-dismiss", Button).disabled = not enabled
        except Exception:  # pragma: no cover
            pass

    async def _review(self, status: str) -> None:
        # push_screen_wait 必须在 Worker 中调用（Textual 要求），因此包一层任务
        self.launch(self._review_flow(status), name="audit-review")

    async def _review_flow(self, status: str) -> None:
        finding_id = self._selected_finding
        if finding_id is None:
            self.notify_status("请先在“风险发现”表中选择一条发现。", "warn")
            return
        finding = next((item for item in self._findings if item.finding_id == finding_id), None)
        label = "确认" if status == "confirmed" else "驳回"
        message = (
            f"对发现 {finding_id}"
            + (f"（{finding.rule_id} / {finding.verdict} / {finding.severity}）" if finding else "")
            + f" 提交人工复核结论：**{label}**。\n\n"
            "只更新复核状态，不修改 Trace、回答或审计结论；提交后会重新读取服务端结果。"
        )
        confirmed = await self.app.push_screen_wait(ConfirmScreen("提交人工复核", message, confirm_label=label))
        if not confirmed:
            self.notify_status("已取消复核提交。", "dim")
            return
        try:
            result = await self.client.audit_review(finding_id, status)
        except ApiError as exc:
            self.notify_status(f"复核提交失败：{exc}", "error")
            return
        self.notify_status(f"已提交：{result.finding_id} → {result.review_status}", "ok")
        # 重新读取服务端结果，确保显示的是持久化后的真实状态
        if self._report is not None:
            try:
                report = await self.client.audit_run(self._report.audit_id)
            except ApiError as exc:
                self.notify_status(f"复核已提交，但刷新失败：{exc}", "warn")
                return
            self._show_report(report)

    async def reset_view(self) -> None:  # type: ignore[override]
        self._report = None
        self._findings = []
        self._selected_finding = None
        for table_id in ("audit-results", "audit-findings", "audit-runs"):
            self.query_one(f"#{table_id}", DataTable).clear()
        self.query_one("#audit-summary", Static).update("")
        self.query_one("#audit-detail", Markdown).update("_已清空审计视图（历史审计记录未删除）。_")
        self._update_review_buttons()
        self.notify_status("已清空审计视图（未删除任何审计记录）", "dim")


__all__ = ["AuditPage", "MODES"]
