"""Agent Chat 页面：本项目 TUI 的核心页面。

交互约定（与 `docs/tui_design.md` 一致）：

- 输入框是多行 `TextArea`：**Enter 换行**，`Ctrl+S` 发送（避免与多行输入冲突）；
- 提交后立即把用户消息写入对话区，然后启动异步 Worker 调用 `POST /api/v1/agent/chat`；
- 请求期间界面保持可响应：可以切换到其他页面、可以点“取消等待”；
  **取消只取消界面等待**，后端没有取消接口，服务端任务可能仍在执行（界面会明确说明）；
- 不伪造流式工具过程：本次 API 不返回流式事件，因此工具调用在执行结束后按真实响应展示；
- 工具调用、证据与 trace_id 全部来自真实响应，不使用任何硬编码演示数据。
"""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Button, Collapsible, Markdown, Static, TextArea

from app.tui.client import ApiError
from app.tui.formatting import fmt_ms, json_block, scope_label, truncate
from app.tui.models import AgentAnswer, Evidence, ToolCall
from app.tui.pages.base import BasePage

PLACEHOLDER = "例：2020 年 4 月 17 日电机电流与排气压力情况如何？结合技术文档分析需要关注的问题"


class ChatPage(BasePage):
    PAGE_ID = "chat"
    PAGE_TITLE = "Agent Chat"
    PAGE_SUMMARY = "自然语言问答（LangGraph Agent）"

    BINDINGS = [
        Binding("ctrl+s", "send", "发送", show=True, priority=False),
        Binding("ctrl+x", "cancel", "取消等待", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._in_flight = False
        self.last_trace_id: str | None = None
        self.last_answer: AgentAnswer | None = None

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Agent Chat · 工业设备智能运维助手", classes="page-header")
            yield Static(
                "输入中文问题。Enter 换行，Ctrl+S 发送，Ctrl+X 取消界面等待。"
                "工具调用与证据来自后端真实响应（本版本 API 不提供流式事件）。",
                classes="page-hint",
            )
            yield ScrollableContainer(id="chat-log")
            yield TextArea(id="chat-input")
            yield Static(
                "输入提示：" + PLACEHOLDER,
                classes="message",
                id="chat-input-hint",
            )
            with Horizontal(classes="chat-actions"):
                yield Button("发送 (Ctrl+S)", id="chat-send", variant="primary")
                yield Button("取消等待", id="chat-cancel", disabled=True)
                yield Button("打开 Trace", id="chat-open-trace", disabled=True)
                yield Button("发起审计", id="chat-open-audit", disabled=True)
                yield Button("复制 Trace ID", id="chat-copy-trace", disabled=True)
                yield Button("复制回答", id="chat-copy-answer", disabled=True)
            yield Static("", id="page-chat-message", classes="message")

    def on_mount(self) -> None:
        self.query_one("#chat-log", ScrollableContainer).mount(
            Static(
                "尚未提问。示例问题：2020 年 4 月 17 日 Motor_current 的统计特征是什么？"
                "（数据来自真实数据库，回答耗时约 3–10 秒）",
                classes="message",
            )
        )
        self.query_one("#chat-input", TextArea).focus()
        self.set_message("就绪", "dim")

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "chat-send":
            await self.action_send()
        elif button_id == "chat-cancel":
            await self.action_cancel()
        elif button_id == "chat-open-trace":
            if self.last_trace_id:
                self.app.open_trace(self.last_trace_id)  # type: ignore[attr-defined]
        elif button_id == "chat-open-audit":
            if self.last_trace_id:
                self.app.open_audit(self.last_trace_id)  # type: ignore[attr-defined]
        elif button_id == "chat-copy-trace":
            if self.last_trace_id:
                self.app.copy_to_clipboard(self.last_trace_id)
                self.notify_status(f"已复制 trace_id：{self.last_trace_id}", "ok")
        elif button_id == "chat-copy-answer":
            if self.last_answer and self.last_answer.answer:
                self.app.copy_to_clipboard(self.last_answer.answer)
                self.notify_status("已复制最新回答（Markdown 原文）", "ok")

    async def action_send(self) -> None:
        if self._in_flight:
            self.notify_status("上一次执行尚未结束；可先取消等待或等待完成。", "warn")
            return
        text = self.query_one("#chat-input", TextArea).text.strip()
        if not text:
            self.notify_status("请输入问题后再发送。", "warn")
            return
        self.query_one("#chat-input", TextArea).text = ""
        await self._append_user(text)
        self._set_running(True)
        self.notify_status("Agent 执行中…（可切换页面；取消等待不影响服务端任务）", "dim")
        self.launch(self._ask(text), name="agent-chat")

    async def action_cancel(self) -> None:
        if not self._in_flight:
            self.notify_status("当前没有正在等待的请求。", "dim")
            return
        cancelled = self.cancel_tasks()
        if cancelled:
            self._set_running(False)
            self.notify_status(
                "已取消界面等待（服务端任务未被取消：后端未提供取消接口，"
                "其结果可能仍会写入服务端 Trace）",
                "warn",
            )
            await self._append_note(
                "已取消界面等待：服务端任务未被取消（后端无取消接口），"
                "本次 Agent 执行可能仍在服务端继续，"
                "其 trace 可能仍可在 Trace Explorer 中查询到。"
            )

    # ------------------------------------------------------------------ #
    # 真实调用
    # ------------------------------------------------------------------ #
    async def _ask(self, question: str) -> None:
        started = datetime.now()
        try:
            answer = await self.client.agent_chat(question)
        except ApiError as exc:
            self._set_running(False)
            if self.is_cancelled(exc):
                return
            self.notify_status(f"Agent 调用失败：{exc}", "error")
            await self._append_note(f"调用失败：{exc}", level="error")
            return
        except Exception as exc:  # pragma: no cover - 防御性
            self._set_running(False)
            self.notify_status(f"Unexpected error: {type(exc).__name__}", "error")
            return
        elapsed = (datetime.now() - started).total_seconds() * 1000
        self.last_answer = answer
        self.last_trace_id = answer.trace_id
        self.app.last_trace_id = answer.trace_id  # type: ignore[attr-defined]
        try:
            await self._render_answer(answer, client_elapsed_ms=elapsed)
        except Exception as exc:  # noqa: BLE001 - 渲染失败不能吞掉真实回答
            self.notify_status(
                f"回答已返回，但渲染失败（{type(exc).__name__}）：原始回答已保存在页面状态中",
                "error",
            )
            await self._append_note(
                f"渲染异常：{type(exc).__name__}: {exc}；"
                f"trace_id={answer.trace_id}，可按 Ctrl+S 前先复制回答。",
                level="error",
            )
        self._set_running(False)
        self.notify_status(
            f"完成：{answer.execution_status} · {fmt_ms(answer.total_duration_ms)} · "
            f"trace {answer.trace_id}",
            "ok",
        )
        # Agent 有真实回答，说明 LLM 通路正常；把状态栏 LLM 从“未探测”更新为“可用”
        self.app.note_llm_success()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    async def _append_user(self, text: str) -> None:
        log = self.query_one("#chat-log", ScrollableContainer)
        stamp = datetime.now().strftime("%H:%M:%S")
        await log.mount(Static(f"▌你 · {stamp}", classes="chat-user"))
        await log.mount(Markdown(text, classes="chat-answer"))
        log.scroll_end(animate=False)

    async def _append_note(self, text: str, *, level: str = "warn") -> None:
        log = self.query_one("#chat-log", ScrollableContainer)
        style = "message-error" if level == "error" else "message-warn"
        await log.mount(Static(text, classes=f"message {style}"))
        log.scroll_end(animate=False)

    async def _render_answer(self, answer: AgentAnswer, *, client_elapsed_ms: float) -> None:
        log = self.query_one("#chat-log", ScrollableContainer)
        stamp = datetime.now().strftime("%H:%M:%S")
        await log.mount(Static(f"● Agent · {stamp}", classes="chat-agent"))
        await log.mount(Markdown(answer.answer or "_（无回答文本）_", classes="chat-answer"))

        # 工具调用：一行摘要 + 可展开明细
        summary = self._tool_summary(answer.tool_calls)
        await log.mount(Static(summary, classes="chat-tools"))
        if answer.tool_calls:
            detail = Markdown(self._tool_detail_markdown(answer.tool_calls))
            details = Collapsible(detail, title=f"工具调用明细（{len(answer.tool_calls)}）", collapsed=True)
            await log.mount(details)

        # 证据：可展开
        if answer.evidence:
            evidence_block = Markdown(self._evidence_markdown(answer.evidence))
            await log.mount(
                Collapsible(
                    evidence_block,
                    title=f"证据（{len(answer.evidence)}）",
                    collapsed=True,
                )
            )

        # 执行元信息
        await log.mount(Static(self._meta_text(answer, client_elapsed_ms), classes="chat-meta"))
        if answer.warnings or answer.errors:
            await log.mount(
                Static(self._warning_text(answer), classes="message message-warn")
            )
        log.scroll_end(animate=False)
        self._set_running(False)

    @staticmethod
    def _tool_summary(tool_calls: list[ToolCall]) -> str:
        if not tool_calls:
            return "工具：未调用任何工具"
        parts = []
        for call in tool_calls:
            icon = {"ok": "✓", "empty": "∅", "error": "✗"}.get(call.status, "?")
            parts.append(f"{icon} {call.tool} ({fmt_ms(call.elapsed_ms)})")
        return "工具：" + " · ".join(parts)

    @staticmethod
    def _tool_detail_markdown(tool_calls: list[ToolCall]) -> str:
        lines = ["| # | 工具 | 状态 | 阶段 | 耗时 | 证据数 |", "| - | ---- | ---- | ---- | ---- | ------ |"]
        for index, call in enumerate(tool_calls, start=1):
            lines.append(
                f"| {index} | `{call.tool}` | {call.status} | {call.stage} | "
                f"{fmt_ms(call.elapsed_ms)} | {call.evidence_count} |"
            )
        for index, call in enumerate(tool_calls, start=1):
            lines += ["", f"**{index}. {call.tool}**"]
            if call.reason:
                lines.append(f"- 规划理由：{call.reason}")
            lines.append("- 入参：")
            lines.append("```json")
            lines.append(json_block(call.params, limit=900))
            lines.append("```")
            if call.error:
                lines.append(f"- 错误：`{json_block(call.error, limit=400)}`")
        return "\n".join(lines)

    @staticmethod
    def _evidence_markdown(evidence: list[Evidence]) -> str:
        lines: list[str] = []
        for item in evidence:
            if item.kind == "knowledge":
                title = item.document_title or item.doc_key or "?"
                lines.append(
                    f"**[{item.label}] 知识证据 · {title}** "
                    f"（{scope_label(item.equipment_scope)}）"
                )
                lines.append(
                    f"- 定位：doc_key=`{item.doc_key}` · 页码 {item.page_start}–{item.page_end}"
                    f" · chunk_id={item.chunk_id}"
                )
                if item.quote:
                    lines.append(f"- 摘录：{truncate(item.quote, 220)}")
            else:
                lines.append(f"**[{item.label}] 数据证据 · {item.fact_type or 'fact'}**")
                if item.text:
                    lines.append(f"- 事实：{truncate(item.text, 240)}")
                if item.source:
                    lines.append(f"- 来源：{truncate(json_block(item.source, limit=200), 200)}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _meta_text(answer: AgentAnswer, client_elapsed_ms: float) -> str:
        grounding = answer.grounding or {}
        grounding_ok = grounding.get("ok")
        grounding_text = "接地校验通过" if grounding_ok else f"接地校验：{grounding_ok}"
        return (
            f"状态 {answer.execution_status} · 意图 {answer.intent} · "
            f"回答方式 {answer.answer_mode} · 轮次 {answer.rounds} · "
            f"服务端耗时 {fmt_ms(answer.total_duration_ms)} · "
            f"端到端 {fmt_ms(client_elapsed_ms)} · {grounding_text}\n"
            f"trace_id {answer.trace_id} · agent {answer.agent_version}"
        )

    @staticmethod
    def _warning_text(answer: AgentAnswer) -> str:
        lines: list[str] = []
        for warning in answer.warnings:
            lines.append(f"⚠ {warning}")
        for error in answer.errors:
            lines.append(f"✗ {json_block(error, limit=200)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _set_running(self, running: bool) -> None:
        self._in_flight = running
        try:
            self.query_one("#chat-send", Button).disabled = running
            self.query_one("#chat-cancel", Button).disabled = not running
            self.query_one("#chat-open-trace", Button).disabled = self.last_trace_id is None
            self.query_one("#chat-open-audit", Button).disabled = self.last_trace_id is None
            self.query_one("#chat-copy-trace", Button).disabled = self.last_trace_id is None
            self.query_one("#chat-copy-answer", Button).disabled = self.last_answer is None
        except Exception:  # pragma: no cover - 卸载期间
            pass

    def reset_view(self) -> None:
        log = self.query_one("#chat-log", ScrollableContainer)
        for child in list(log.children):
            child.remove()
        self.notify_status("已清空对话视图（服务端 Trace 与审计记录不受影响）", "dim")

    async def load(self, *, force: bool = False) -> None:
        """Chat 页面没有必须预加载的数据；仅校验后端可达性。"""
        try:
            health = await self.client.health()
        except ApiError as exc:
            self.notify_status(f"后端不可用：{exc}", "error")
            return
        if self.last_answer is not None:
            self.set_message(
                f"上一次回答：{self.last_answer.execution_status} · "
                f"trace {self.last_answer.trace_id}",
                "ok",
            )
            return
        self.set_message(f"后端 {health.service or 'api'} v{health.version or '?'} 已连接", "ok")

    @property
    def request_in_flight(self) -> bool:
        return self._in_flight


__all__ = ["ChatPage"]
