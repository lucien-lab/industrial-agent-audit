"""Textual 应用入口：布局、导航、系统状态栏与页面调度。

结构：

```
┌ 状态栏（真实探测结果：API / 工业库 / RAG / Audit / LLM）────────────────┐
│ 侧边导航 │ 当前页面（Agent Chat / Equipment / Knowledge / Trace / Audit / Status）│
└ 状态行（最近一次操作结果） + 快捷键 Footer ─────────────────────────────┘
```

边界：
- TUI 只通过 `IaaApiClient` 访问后端，不直连数据库、不重实现 Agent/RAG/审计；
- 任何后端不可用只影响对应页面，应用本身继续可用；
- 退出时取消后台任务并关闭 HTTP 连接。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Footer, Label, ListItem, ListView, Static

from app.tui.client import DEFAULT_BASE_URL, ApiError, IaaApiClient
from app.tui.formatting import state_style
from app.tui.pages.audit import AuditPage
from app.tui.pages.base import BasePage
from app.tui.pages.chat import ChatPage
from app.tui.pages.equipment import EquipmentPage
from app.tui.pages.knowledge import KnowledgePage
from app.tui.pages.modals import HelpScreen, QuickSwitchScreen
from app.tui.pages.status import StatusPage
from app.tui.pages.traces import TracePage
from app.tui.status import ComponentState, SystemStatus, probe_system_status

logger = logging.getLogger("industrial_agent_audit.tui.app")

BRAND = "Industrial Agent Audit · 工业智能运维 Agent"


class IaaTuiApp(App[None]):
    """工业智能运维 Agent 的终端界面。"""

    CSS_PATH = "app.tcss"
    TITLE = BRAND
    SUB_TITLE = "Textual TUI · Phase 5"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("1", "show_page('chat')", "对话"),
        Binding("2", "show_page('equipment')", "设备"),
        Binding("3", "show_page('knowledge')", "知识"),
        Binding("4", "show_page('traces')", "Trace"),
        Binding("5", "show_page('audit')", "审计"),
        Binding("6", "show_page('status')", "状态"),
        Binding("ctrl+k", "quick_switch", "切换"),
        Binding("ctrl+r", "refresh_page", "刷新"),
        Binding("ctrl+l", "clear_page", "清空"),
        Binding("f1", "help", "帮助"),
    ]

    PAGE_ORDER: ClassVar[list[tuple[str, str, str, type[BasePage]]]] = [
        ("1", "chat", "Agent Chat", ChatPage),
        ("2", "equipment", "Equipment", EquipmentPage),
        ("3", "knowledge", "Knowledge", KnowledgePage),
        ("4", "traces", "Trace Explorer", TracePage),
        ("5", "audit", "Audit Reports", AuditPage),
        ("6", "status", "System Status", StatusPage),
    ]

    def __init__(
        self,
        client: IaaApiClient | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        audit_token: str | None = None,
        transport=None,
    ) -> None:
        super().__init__()
        self._owns_client = client is None
        self.client = client or IaaApiClient(
            base_url, audit_token=audit_token, transport=transport
        )
        self.system_status: SystemStatus | None = None
        self.last_trace_id: str | None = None
        self.current_page: str = "chat"
        self._status_line_text: str = "就绪 · Ctrl+K 切换页面 · F1 帮助 · Ctrl+Q 退出"
        self._llm_success_at: datetime | None = None
        self._status_busy = False

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical(id="status-bar"):
            yield Static(BRAND, id="status-bar-brand")
            yield Static("正在探测后端状态…", id="status-bar-states")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("功能导航", id="nav-title")
                yield ListView(
                    *[
                        ListItem(Label(f"{key} · {title}"), id=f"nav-{page_id}")
                        for key, page_id, title, _ in self.PAGE_ORDER
                    ],
                    id="nav",
                )
                yield Static(
                    "Ctrl+K 快速切换\n"
                    "Ctrl+R 刷新  Ctrl+L 清空\n"
                    "F1 帮助  Ctrl+Q 退出\n"
                    "Enter 换行 · Ctrl+S 发送",
                    id="sidebar-hint",
                )
            with ContentSwitcher(initial="page-chat", id="page-container"):
                yield ChatPage()
                yield EquipmentPage()
                yield KnowledgePage()
                yield TracePage()
                yield AuditPage()
                yield StatusPage()
        yield Static(self._status_line_text, id="status-line")
        yield Footer()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_mount(self) -> None:
        self.query_one("#nav", ListView).index = 0
        self._set_status_line("就绪 · 正在探测后端状态…", level="dim")
        self._start_status_probe()
        # 首屏页面（Chat）不需要预加载数据，但要在状态可用后提示
        self.query_one("#page-container", ContentSwitcher).current = "page-chat"

    async def on_unmount(self) -> None:
        for worker in list(self.workers):
            if not worker.is_finished:
                worker.cancel()
        if self._owns_client:
            await self.client.close()

    # ------------------------------------------------------------------ #
    # 状态栏
    # ------------------------------------------------------------------ #
    def _start_status_probe(self, *, deep: bool = False) -> None:
        self.run_worker(
            self.refresh_system_status(deep=deep),
            group="system-status",
            name="probe",
            exclusive=True,
            exit_on_error=False,
        )

    async def refresh_system_status(self, *, deep: bool = False) -> SystemStatus:
        """真实探测所有后端能力并刷新状态栏（页面也可以调用）。"""
        self._status_busy = True
        try:
            status = await probe_system_status(self.client, deep=deep)
        finally:
            self._status_busy = False
        self.system_status = status
        self._render_status_bar()
        status_page = self.query_one(StatusPage)
        if status_page.is_mounted:
            try:
                status_page.render_status(status)
            except Exception:  # pragma: no cover - 页面尚未 compose 完成
                pass
        if status.overall.value != "ready":
            self._set_status_line(
                f"系统状态 {status.overall.label_zh}："
                + "；".join(item.summary for item in status.components
                            if item.state.value != "ready"),
                level="warn" if status.overall.value != "unavailable" else "error",
            )
        return status

    def _render_status_bar(self) -> None:
        states = self.query_one("#status-bar-states", Static)
        if self.system_status is None:
            states.update(Text("正在探测后端状态…", style="dim"))
            return
        order = ["api", "industrial_db", "rag", "audit", "llm"]
        text = Text()
        for index, key in enumerate(order):
            component = self.system_status.get(key)
            if component is None:
                continue
            if index:
                text.append("  │  ", style="dim")
            text.append(f"{component.label} ", style="dim")
            glyph = {
                ComponentState.READY: "●",
                ComponentState.DEGRADED: "◐",
                ComponentState.UNAVAILABLE: "✕",
                ComponentState.UNKNOWN: "○",
            }[component.state]
            text.append(f"{glyph} {component.state.label_zh}", style=state_style(component.state.value))
        if self._llm_success_at is not None:
            text.append(
                f"  │  最近问答 {self._llm_success_at.strftime('%H:%M:%S')} 成功",
                style="dim",
            )
        text.append(
            f"  │  探测 {self.system_status.probed_at.strftime('%H:%M:%S')}"
            f"{'（深度）' if self.system_status.deep_probe else ''}",
            style="dim",
        )
        states.update(text)

    def note_llm_success(self) -> None:
        """Agent 真实回答成功后，把 LLM 状态从“未探测”提升为可用（有真实证据）。"""
        self._llm_success_at = datetime.now()
        status = self.system_status
        if status is not None:
            for index, item in enumerate(status.components):
                if item.key == "llm" and item.state is ComponentState.UNKNOWN:
                    from app.tui.status import ComponentStatus

                    status.components[index] = ComponentStatus(
                        key="llm",
                        label="LLM",
                        state=ComponentState.READY,
                        detail=f"最近一次 Agent 调用成功（{self._llm_success_at.strftime('%H:%M:%S')}）",
                    )
        self._render_status_bar()

    def set_status_line(self, text: str, *, level: str = "dim") -> None:
        self._status_line_text = text
        self._set_status_line(text, level=level)

    def _set_status_line(self, text: str, *, level: str = "dim") -> None:
        style = {
            "error": "bold red",
            "warn": "yellow",
            "ok": "green",
            "dim": "dim",
        }.get(level, "dim")
        try:
            self.query_one("#status-line", Static).update(Text(text, style=style))
        except Exception:  # pragma: no cover - 未挂载
            pass

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #
    def page(self, page_id: str) -> BasePage:
        return self.query_one(f"#page-{page_id}", BasePage)

    def action_show_page(self, page_id: str) -> None:
        known = {item[1] for item in self.PAGE_ORDER}
        if page_id not in known:
            self._set_status_line(f"未知页面 {page_id!r}", level="error")
            return
        self.current_page = page_id
        switcher = self.query_one("#page-container", ContentSwitcher)
        switcher.current = f"page-{page_id}"
        nav = self.query_one("#nav", ListView)
        index = [item[1] for item in self.PAGE_ORDER].index(page_id)
        nav.index = index
        title = dict((item[1], item[2]) for item in self.PAGE_ORDER)[page_id]
        self._set_status_line(f"当前页面：{title}", level="dim")
        page = self.page(page_id)
        self.run_worker(
            page.activate(),
            group="page-activate",
            name=f"activate-{page_id}",
            exclusive=True,
            exit_on_error=False,
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("nav-"):
            self.action_show_page(item_id.removeprefix("nav-"))

    def action_quick_switch(self) -> None:
        self.run_worker(
            self._quick_switch(),
            group="modal",
            name="quick-switch",
            exclusive=True,
            exit_on_error=False,
        )

    async def _quick_switch(self) -> None:
        pages = [
            (key, page_id, title, page_cls.PAGE_SUMMARY)
            for key, page_id, title, page_cls in self.PAGE_ORDER
        ]
        result = await self.push_screen_wait(QuickSwitchScreen(pages))
        if result:
            self.action_show_page(result)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh_page(self) -> None:
        page_id = self.current_page
        page = self.page(page_id)
        self._set_status_line(f"刷新 {page_id} …", level="dim")
        self.run_worker(
            page.reload(), group="page-reload", name=f"reload-{page_id}", exclusive=True,
            exit_on_error=False,
        )

    def action_clear_page(self) -> None:
        page = self.page(self.current_page)
        result = page.reset_view()
        if hasattr(result, "__await__"):
            self.run_worker(result, group="page-clear", name="clear", exclusive=True,
                            exit_on_error=False)
        self._set_status_line(f"已清空 {self.current_page} 视图", level="dim")

    # ------------------------------------------------------------------ #
    # 页面间跳转
    # ------------------------------------------------------------------ #
    def open_trace(self, trace_id: str) -> None:
        self.last_trace_id = trace_id
        page = self.query_one(TracePage)
        page.prefill(trace_id)
        self.action_show_page("traces")
        self.query_one("#trace-id")  # 确保已挂载
        self._set_status_line(f"已在 Trace Explorer 打开 {trace_id}", level="dim")

    def open_audit(self, trace_id: str) -> None:
        self.last_trace_id = trace_id
        page = self.query_one(AuditPage)
        page.prefill(trace_id)
        self.action_show_page("audit")
        self._set_status_line(f"已在 Audit 页面准备好 trace {trace_id}（点击“发起审计”）", level="dim")


def create_app(
    base_url: str = DEFAULT_BASE_URL,
    *,
    audit_token: str | None = None,
    transport=None,
) -> IaaTuiApp:
    """工厂函数：便于 CLI 与测试复用。"""
    return IaaTuiApp(base_url=base_url, audit_token=audit_token, transport=transport)


__all__ = ["IaaTuiApp", "create_app", "BRAND", "ApiError"]
