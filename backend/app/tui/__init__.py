"""阶段 5：工业智能运维 Agent 的终端用户界面（Textual）。

边界（与 `docs/tui_design.md` 一致）：

- TUI **只**通过 HTTP 调用已存在的 FastAPI 接口，不直连数据库、
  不重新实现 RAG / Agent / 审计引擎；
- 所有展示内容来自真实后端响应，不在客户端伪造工具执行或审计结论；
- 后端不可用时只降级对应面板，不整体退出（LLM 不可用仍可查设备数据与历史审计）。

入口：

- `iaa-tui`（安装后）或 `python -m app.tui`（项目 `backend/` 目录下）。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "5.0.0"
