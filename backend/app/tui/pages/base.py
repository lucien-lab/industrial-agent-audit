"""页面基类：统一忙碌状态、错误提示、Worker 生命周期与资源释放。

设计约束：
- 所有网络请求都在 Textual Worker（async）中执行，页面渲染只读取已经返回的结果；
- 每个页面一个 Worker 组（`page-<id>`），新请求会取消同组旧请求；
- 取消 Worker 只取消**界面等待**，不会取消服务端已经在执行的任务（后端无取消接口）；
- 页面被隐藏时不发起请求；首次进入或用户显式刷新时才加载数据。
"""

from __future__ import annotations

from typing import Any, ClassVar, Coroutine

from textual.containers import Container
from textual.widgets import Static
from textual.worker import Worker, WorkerCancelled

from app.tui.client import IaaApiClient


class BasePage(Container):
    """所有功能页面的基类。"""

    PAGE_ID: ClassVar[str] = "base"
    PAGE_TITLE: ClassVar[str] = "页面"
    PAGE_KEY: ClassVar[str] = ""
    PAGE_SUMMARY: ClassVar[str] = ""

    def __init__(self) -> None:
        super().__init__(id=f"page-{self.PAGE_ID}")
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 便利属性
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> IaaApiClient:
        return self.app.client  # type: ignore[attr-defined]

    @property
    def message_widget(self) -> Static | None:
        try:
            return self.query_one(f"#page-{self.PAGE_ID}-message", Static)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def activate(self, *, force: bool = False) -> None:
        """页面被切换到前台时调用；默认首次加载一次。"""
        if force or not self._loaded:
            await self.load(force=force)
            self._loaded = True

    async def load(self, *, force: bool = False) -> None:  # pragma: no cover - 由子类实现
        """加载页面数据。子类按需覆盖。"""

    async def reload(self) -> None:
        """Ctrl+R / 刷新按钮入口。"""
        self._loaded = True
        await self.load(force=True)

    def reset_view(self) -> None:
        """Ctrl+L：清空当前页面的临时输出（不修改后端数据）。"""
        self.set_message("已清空当前视图（不影响服务端数据）", "dim")

    def on_unmount(self) -> None:
        # 页面销毁时取消自己组内的后台任务，避免退出后仍在写界面
        try:
            self.workers.cancel_group(self, f"page-{self.PAGE_ID}")
            self.workers.cancel_group(self, "reload")
        except Exception:  # pragma: no cover - 关闭阶段的防御
            pass

    # ------------------------------------------------------------------ #
    # 提示信息
    # ------------------------------------------------------------------ #
    def set_message(self, text: str, level: str = "dim") -> None:
        """在页面固定位置显示一条提示（dim / ok / warn / error）。"""
        widget = self.message_widget
        if widget is None:
            return
        widget.update(text)
        widget.remove_class("message-error", "message-warn", "message-ok")
        if level == "error":
            widget.add_class("message-error")
        elif level == "warn":
            widget.add_class("message-warn")
        elif level == "ok":
            widget.add_class("message-ok")

    def notify_status(self, text: str, level: str = "info") -> None:
        """同时写入底部状态行与页面提示。"""
        self.set_message(text, "error" if level == "error" else level)
        self.app.set_status_line(text, level=level)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # Worker 封装
    # ------------------------------------------------------------------ #
    def launch(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        group: str | None = None,
        name: str = "task",
    ) -> Worker[Any]:
        """在当前页面启动一个独占的异步任务。"""
        return self.run_worker(
            coro,
            group=group or f"page-{self.PAGE_ID}",
            name=name,
            exclusive=True,
            exit_on_error=False,
        )

    def cancel_tasks(self, group: str | None = None) -> int:
        """取消本页面的后台任务，返回取消数量（仅影响界面等待）。"""
        cancelled = 0
        prefix = group or f"page-{self.PAGE_ID}"
        for worker in list(self.workers):
            if worker.group == prefix and not worker.is_finished:
                worker.cancel()
                cancelled += 1
        return cancelled

    @staticmethod
    def is_cancelled(exc: BaseException) -> bool:
        return isinstance(exc, WorkerCancelled)


__all__ = ["BasePage"]
