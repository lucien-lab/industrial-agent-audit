"""Trace Explorer：查看一次 Agent 执行的结构化事件链。

数据来源优先级：

1. `GET /api/v1/audit/traces/{trace_id}`（阶段 4 的受控回放接口，含规范化事件、
   工具调用时间线与证据清单，可读取历史 Trace 文件）；
2. 回退到 `GET /api/v1/agent/traces/{trace_id}?include_events=true`
   （阶段 3 的进程内 Trace；只保留最近 N 条，历史记录可能已不在内存中）。

TUI **不读取服务器文件路径**，只使用 trace_id 调用上述受控接口。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, Select, Static

from app.tui.client import ApiError
from app.tui.formatting import (
    event_detail_markdown,
    fmt_ms,
    fmt_ts,
    truncate,
)
from app.tui.models import AgentTraceSummary, AuditTraceDetail, TraceEvent
from app.tui.pages.base import BasePage

ALL_TYPES = "all"


class TracePage(BasePage):
    PAGE_ID = "traces"
    PAGE_TITLE = "Trace Explorer"
    PAGE_SUMMARY = "Trace 事件时间线（run_start → tool_call → answer）"

    BINDINGS = [Binding("ctrl+s", "load_trace", "加载", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._events: list[TraceEvent] = []
        self._detail: AuditTraceDetail | None = None
        self._source_kind = ""
        self._busy = False
        self._pending_trace_id: str | None = None
        self._updating_filter = False

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Trace Explorer · 执行链回放", classes="page-header")
            yield Static(
                "只显示受控接口返回的白名单字段（不含连接串/密码）。"
                "旧版 Trace 缺少 evidence manifest 时会明确标注，依赖证据的审计规则会返回 UNKNOWN。",
                classes="page-hint",
            )
            with Horizontal(classes="controls"):
                yield Input(placeholder="trace_id", id="trace-id")
                yield Select([], id="trace-pick", prompt="最近审计", allow_blank=True)
                yield Select([(t, t) for t in ALL_TYPES_OPTIONS], value=ALL_TYPES, id="trace-filter", allow_blank=False)
                yield Button("加载 Trace", id="trace-load", variant="primary")
            yield Static("", id="page-traces-message", classes="message")
            yield Static("", id="trace-meta")
            with Horizontal():
                yield DataTable(id="trace-events", cursor_type="row", classes="split-left")
                with VerticalScroll(classes="split-right"):
                    yield Markdown("_尚未加载 Trace。_", id="trace-detail")

    def on_mount(self) -> None:
        table = self.query_one("#trace-events", DataTable)
        table.add_columns("事件", "类型", "节点", "工具", "状态", "耗时")

    # ------------------------------------------------------------------ #
    # 外部入口（Chat / Audit 页面跳转）
    # ------------------------------------------------------------------ #
    def prefill(self, trace_id: str) -> None:
        self._pending_trace_id = trace_id
        try:
            self.query_one("#trace-id", Input).value = trace_id
        except Exception:  # pragma: no cover - 未挂载
            pass

    async def activate(self, *, force: bool = False) -> None:
        await super().activate(force=force)
        if self._pending_trace_id:
            trace_id = self._pending_trace_id
            self._pending_trace_id = None
            self.query_one("#trace-id", Input).value = trace_id
            await self._load(trace_id)

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    async def load(self, *, force: bool = False) -> None:
        await self._load_recent_runs()
        trace_id = self.query_one("#trace-id", Input).value.strip()
        if trace_id:
            await self._load(trace_id)

    async def _load_recent_runs(self) -> None:
        try:
            runs = await self.client.audit_runs(limit=20)
        except ApiError as exc:
            self.set_message(f"历史审计运行不可用：{exc}", "warn")
            return
        select = self.query_one("#trace-pick", Select)
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for run in runs.items:
            if run.trace_id in seen:
                continue
            seen.add(run.trace_id)
            options.append(
                (
                    f"{run.trace_id[:12]}… {run.audit_mode}/{run.overall_risk}",
                    run.trace_id,
                )
            )
        select.set_options(options)

    async def action_load_trace(self) -> None:
        trace_id = self.query_one("#trace-id", Input).value.strip()
        if not trace_id:
            self.notify_status("请输入 trace_id。", "warn")
            return
        await self._load(trace_id)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "trace-load":
            await self.action_load_trace()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "trace-pick" and isinstance(event.value, str) and event.value:
            self.query_one("#trace-id", Input).value = event.value
            await self._load(event.value)
        elif event.select.id == "trace-filter" and not self._updating_filter:
            await self._render_events()

    # ------------------------------------------------------------------ #
    # 真实请求
    # ------------------------------------------------------------------ #
    async def _load(self, trace_id: str) -> None:
        if self._busy:
            self.notify_status("上一次加载尚未结束。", "warn")
            return
        self._busy = True
        self.notify_status(f"加载 Trace {trace_id} …", "dim")
        try:
            detail = await self.client.audit_trace(trace_id)
        except ApiError as audit_exc:
            detail = None
            audit_error: ApiError | None = audit_exc
        else:
            audit_error = None
        if detail is not None:
            self._busy = False
            self._source_kind = "audit"
            self._detail = detail
            self._events = list(detail.trace.events)
            self._render_meta(detail)
            await self._render_events()
            self.notify_status(
                f"已加载：{detail.trace.event_count or len(self._events)} 个事件 · "
                f"来源 {detail.trace.source or '?'}",
                "ok",
            )
            return

        # 回退：阶段 3 进程内 Trace
        try:
            summary = await self.client.agent_trace(trace_id, include_events=True)
        except ApiError as agent_exc:
            self._busy = False
            detail_text = (
                f"审计回放接口：{audit_error}\n进程内 Trace 接口：{agent_exc}"
                if audit_error
                else str(agent_exc)
            )
            self.notify_status(f"Trace 加载失败：{detail_text}", "error")
            self.query_one("#trace-detail", Markdown).update(
                "### 无法加载该 Trace\n\n"
                f"- trace_id：`{trace_id}`\n"
                f"- 审计回放：{audit_error if audit_error else '—'}\n"
                f"- 进程内 Trace：{agent_exc}\n\n"
                "> 进程内 Trace 只保留最近若干条，历史 Trace 需要审计库或 Trace 文件才能回放。"
            )
            return
        self._busy = False
        self._source_kind = "agent"
        self._detail = None
        self._events = [_event_from_dict(raw) for raw in (summary.events or [])]
        self._render_agent_meta(summary)
        await self._render_events()
        self.notify_status(
            f"已加载（进程内 Trace）：{len(self._events)} 个事件；"
            "该来源不含 evidence manifest 与审计记录",
            "warn",
        )

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render_meta(self, detail: AuditTraceDetail) -> None:
        trace = detail.trace
        manifest = "有（agent-evidence-v1）" if detail.has_evidence_manifest else "**缺少 evidence manifest（旧版 Trace）**"
        audits = detail.audits or []
        audit_lines = (
            "；".join(
                f"{item.get('audit_mode')}/{item.get('overall_risk')} ({item.get('status')})"
                for item in audits
            )
            or "无"
        )
        lines = [
            f"trace_id：{trace.trace_id}",
            f"问题：{truncate(trace.question, 120)}",
            f"状态：{trace.execution_status} · 意图：{trace.intent} · 事件 {len(self._events)}",
            f"来源：{trace.source or '?'} · schema {trace.trace_schema_version or '?'} · "
            f"agent {trace.agent_version or '?'} · 时间 {fmt_ts(trace.started_at)} → {fmt_ts(trace.ended_at)}",
            f"证据清单：{manifest} · 已有审计：{audit_lines}",
        ]
        if detail.external_reads:
            lines.append(
                f"外部读取：{len(detail.external_reads)} 次（本次视图不是纯冻结回放，offline_only=false）"
            )
        if trace.problems:
            lines.append("问题：" + "；".join(trace.problems))
        self.query_one("#trace-meta", Static).update("\n".join(lines))

    def _render_agent_meta(self, summary: AgentTraceSummary) -> None:
        lines = [
            f"trace_id：{summary.trace_id}",
            f"问题：{truncate(summary.question, 120)}",
            f"状态：{summary.execution_status} · 意图：{summary.intent} · 事件 {len(self._events)}",
            f"工具：{', '.join(summary.tools_used) or '—'} · 轮次 {summary.rounds} · "
            f"耗时 {fmt_ms(summary.total_duration_ms)}",
            "证据清单：**不可用**（进程内 Trace 不含 evidence manifest，"
            "依赖证据的审计规则会返回 UNKNOWN）",
        ]
        self.query_one("#trace-meta", Static).update("\n".join(lines))

    def _current_type_filter(self) -> str:
        value = self.query_one("#trace-filter", Select).value
        return value if isinstance(value, str) and value else ALL_TYPES

    async def _render_events(self) -> None:
        table = self.query_one("#trace-events", DataTable)
        table.clear()
        wanted = self._current_type_filter()
        shown = [event for event in self._events if wanted == ALL_TYPES or event.event_type == wanted]
        for event in shown:
            table.add_row(
                event.event_id,
                event.event_type,
                event.node_name or "—",
                event.tool_name or "—",
                event.status or "—",
                fmt_ms(event.duration_ms),
                key=event.event_id,
            )
        if not shown:
            self.set_message("该过滤条件下没有事件。", "warn")

        # 过滤器选项按实际出现的类型重建（保持当前选择）
        select = self.query_one("#trace-filter", Select)
        types = sorted({event.event_type for event in self._events})
        options = [(ALL_TYPES, ALL_TYPES)] + [(f"类型：{t}", t) for t in types]
        if self._events:
            self._updating_filter = True
            try:
                select.set_options(options)
                select.value = wanted if (wanted in types or wanted == ALL_TYPES) else ALL_TYPES
            finally:
                self._updating_filter = False

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "trace-events":
            return
        event_id = event.row_key.value if event.row_key else None
        selected = next((item for item in self._events if item.event_id == str(event_id)), None)
        if selected is None:
            return
        self.query_one("#trace-detail", Markdown).update(event_detail_markdown(selected))

    async def reset_view(self) -> None:  # type: ignore[override]
        self._events = []
        self._detail = None
        self.query_one("#trace-events", DataTable).clear()
        self.query_one("#trace-meta", Static).update("")
        self.query_one("#trace-detail", Markdown).update("_已清空 Trace 视图。_")
        self.notify_status("已清空 Trace 视图（未修改任何记录）", "dim")


ALL_TYPES_OPTIONS = (
    ALL_TYPES,
    "run_start",
    "node_start",
    "node_end",
    "intent",
    "plan",
    "tool_call",
    "tool_result",
    "decision",
    "evidence",
    "answer",
    "run_end",
)


def _event_from_dict(raw: dict) -> TraceEvent:
    """把进程内 Trace 的原始事件字典转成展示模型（字段缺失时留空）。"""
    return TraceEvent(
        event_id=str(raw.get("event_id") or raw.get("event_type") or "?"),
        sequence_number=int(raw.get("sequence_number") or 0),
        event_type=str(raw.get("event_type") or "unknown"),
        node_name=raw.get("node_name"),
        tool_name=raw.get("tool_name"),
        timestamp=raw.get("timestamp") or raw.get("started_at"),
        duration_ms=raw.get("duration_ms"),
        status=raw.get("status"),
        input_summary=raw.get("input_summary"),
        output_summary=raw.get("output_summary"),
        evidence_refs=raw.get("evidence_refs") or [],
        error_type=raw.get("error_type"),
        error_code=raw.get("error_code"),
    )


__all__ = ["TracePage", "ALL_TYPES_OPTIONS"]
