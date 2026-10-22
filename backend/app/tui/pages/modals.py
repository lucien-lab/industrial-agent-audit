"""模态窗口：确认对话框、快捷页面切换、帮助。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

HELP_TEXT = """\
# 键盘与操作说明

| 按键 | 作用 |
| --- | --- |
| `1`–`6` | 直接切换功能页面 |
| `Ctrl+K` | 快速切换页面（弹出列表） |
| `Ctrl+R` | 刷新当前页面（重新读取后端数据） |
| `Ctrl+L` | 清空当前页面视图（不修改服务端数据） |
| `Ctrl+S` | 在 Chat / Equipment / Knowledge / Trace / Audit 页面执行主操作 |
| `Ctrl+X` | Chat 页面取消界面等待（服务端任务不会被取消） |
| `Esc` | 关闭弹窗 |
| `Tab` / `Shift+Tab` | 在控件间切换焦点 |
| `Ctrl+Q` | 退出 TUI |
| `Ctrl+C` | Textual 默认的中断处理（会提示如何退出）；在输入框中为先复制 |

## 各页面主操作

- **Agent Chat**：多行输入，`Ctrl+S` 发送；回答下方可展开工具调用与证据；
  “打开 Trace / 发起审计”会把当前 trace_id 带到对应页面。
- **Equipment**：选择设备与时间窗（左闭右开、无时区），查看统计、质量、故障与采样片段。
- **Knowledge**：三种检索方式，选中结果查看原文片段、页码与适用性。
- **Trace Explorer**：输入 trace_id 或从“最近审计”中选择，查看事件时间线与单事件入参/输出。
- **Audit**：选择 rule_only（默认，毫秒级）/ llm_only / hybrid（约 20–40 秒），
  查看风险发现、证据引用，并可提交人工复核结论。
- **System Status**：真实探测各服务；“深度探测”会真实调用 Embedding 与 LLM。

## 重要限制

- 审计是**离线可观测性审计**，不是运行时拦截：它不会阻止已经完成的 Agent 行为。
- `UNKNOWN` 表示缺少证据或 Judge 未成功，**不等于** PASS，也不会被显示为 PASS。
- 取消界面等待不会取消服务端任务（后端未提供取消接口）。
- 主库迁移版本与代码期望不一致时，系统状态会显示 `degraded`，TUI 不会自动迁移数据库。
"""


class ConfirmScreen(ModalScreen[bool]):
    """是/否确认。返回 True 表示确认。"""

    BINDINGS = [
        Binding("escape", "dismiss_false", "取消"),
        Binding("n", "dismiss_false", "取消", show=False),
        Binding("y", "dismiss_true", "确认", show=False),
    ]

    def __init__(self, title: str, message: str, *, confirm_label: str = "确认") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(self._title, classes="dialog-title")
            yield Static(self._message)
            with Horizontal(classes="dialog-buttons"):
                yield Button(self._confirm_label, id="confirm-ok", variant="warning")
                yield Button("取消 (Esc)", id="confirm-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-ok")

    def action_dismiss_true(self) -> None:
        self.dismiss(True)

    def action_dismiss_false(self) -> None:
        self.dismiss(False)


class QuickSwitchScreen(ModalScreen[str | None]):
    """快速切换页面（Ctrl+K）。返回页面 id 或 None。"""

    BINDINGS = [Binding("escape", "dismiss_none", "关闭")]

    def __init__(self, pages: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._pages = pages

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static("快速切换（Enter 确认，Esc 取消）", classes="dialog-title")
            yield OptionList(*self._build_options(), id="quick-switch-list")

    def _build_options(self) -> list[Option]:
        return [
            Option(f"{key}. {title} · {summary}", id=page_id)
            for key, page_id, title, summary in self._pages
        ]

    def on_mount(self) -> None:
        self.query_one("#quick-switch-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """帮助页面（F1 / Ctrl+H）。"""

    BINDINGS = [
        Binding("escape", "close", "关闭"),
        Binding("f1", "close", "关闭", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static("帮助 · 快捷键与限制", classes="dialog-title")
            with VerticalScroll(id="help-body"):
                from textual.widgets import Markdown

                yield Markdown(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["ConfirmScreen", "HelpScreen", "QuickSwitchScreen", "HELP_TEXT"]
