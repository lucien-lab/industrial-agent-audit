"""System Status 页面：真实探测后端与各依赖服务。

区分三态：`ready` / `degraded` / `unavailable`，另外 `unknown` 表示**尚未探测**。
`degraded` 的判定完全来自后端返回（例如 schema 版本不一致），不为了界面好看改成 ready。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Static

from app.tui.formatting import fmt_ts, state_style
from app.tui.pages.base import BasePage
from app.tui.status import SystemStatus


class StatusPage(BasePage):
    PAGE_ID = "status"
    PAGE_TITLE = "System Status"
    PAGE_SUMMARY = "服务健康与 Schema 版本"

    BINDINGS = [Binding("ctrl+s", "refresh", "重新探测", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("System Status · 运行环境检查", classes="page-header")
            yield Static(
                "状态来自真实接口探测：/health、/ready、/api/v1/knowledge/health、/api/v1/audit/summary。"
                "深度探测会真实调用一次 Embedding 与 LLM（1–15 s）。",
                classes="page-hint",
            )
            with Horizontal(classes="controls"):
                yield Button("重新探测", id="status-refresh", variant="primary")
                yield Button("深度探测 Embedding/LLM", id="status-deep")
            yield Static("", id="page-status-message", classes="message")
            yield Static("", id="status-headline")
            with VerticalScroll(classes="scroll-detail"):
                yield DataTable(id="status-table", cursor_type="row")
                yield Static("", id="status-notes")

    def on_mount(self) -> None:
        self.query_one("#status-table", DataTable).add_columns("组件", "状态", "说明")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "status-refresh":
            await self.action_refresh()
        elif event.button.id == "status-deep":
            await self.action_refresh(deep=True)

    async def action_refresh(self, deep: bool = False) -> None:
        if self._busy:
            self.notify_status("上一次探测尚未结束。", "warn")
            return
        self._busy = True
        self.notify_status(
            "深度探测中（真实调用 Embedding 与 LLM，可能需要 1–15 s）…" if deep else "探测中…",
            "dim",
        )
        try:
            status = await self.app.refresh_system_status(deep=deep)  # type: ignore[attr-defined]
        finally:
            self._busy = False
        self.render_status(status)
        self.notify_status(
            f"探测完成：{status.overall.label_zh} · {fmt_ts(status.probed_at)}",
            "ok" if status.overall.value == "ready" else "warn",
        )

    async def load(self, *, force: bool = False) -> None:
        status = getattr(self.app, "system_status", None)
        if status is None or force:
            status = await self.app.refresh_system_status()  # type: ignore[attr-defined]
        self.render_status(status)

    async def reload(self) -> None:
        await self.action_refresh()

    def render_status(self, status: SystemStatus) -> None:
        table = self.query_one("#status-table", DataTable)
        table.clear()
        for item in status.components:
            table.add_row(
                item.label,
                Text(item.state.label_zh, style=state_style(item.state.value)),
                item.detail or "—",
                key=item.key,
            )
        headline = (
            f"总体：{status.overall.label_zh} · 探测时间 {fmt_ts(status.probed_at)} · "
            f"{'深度探测' if status.deep_probe else '快速探测'} · API {status.api_base_url}"
        )
        self.query_one("#status-headline", Static).update(headline)
        notes = list(status.notes)
        notes.append(
            "说明：主库 schema 与代码期望不一致时显示“降级”，TUI 不会自动迁移数据库；"
            "LLM/Embedding 只有深度探测后才会有真实结论，未探测显示“未探测”。"
        )
        if status.deep_probe:
            notes.append("本次已真实调用 Embedding 与 LLM，结论来自实际响应。")
        self.query_one("#status-notes", Static).update("\n".join(f"• {note}" for note in notes))

    async def reset_view(self) -> None:  # type: ignore[override]
        await self.load(force=True)
        self.notify_status("已重新读取系统状态", "dim")


__all__ = ["StatusPage"]
