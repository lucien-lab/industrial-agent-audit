"""页面包：每个功能一个页面，页面之间只通过 `IaaTuiApp` 的跳转方法通信。"""

from __future__ import annotations

from app.tui.pages.audit import AuditPage
from app.tui.pages.base import BasePage
from app.tui.pages.chat import ChatPage
from app.tui.pages.equipment import EquipmentPage
from app.tui.pages.knowledge import KnowledgePage
from app.tui.pages.status import StatusPage
from app.tui.pages.traces import TracePage

__all__ = [
    "AuditPage",
    "BasePage",
    "ChatPage",
    "EquipmentPage",
    "KnowledgePage",
    "StatusPage",
    "TracePage",
]
