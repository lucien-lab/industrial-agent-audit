"""阶段 3：工业设备智能运维 Agent（LangGraph 单 Agent + 多工具）。

模块边界：
- `app.agent.tools`：只读工具（复用 Phase 1C Service 与 Phase 2 检索模块）；
- `app.agent.intent`：确定性意图分析与工具规划（不依赖 LLM 猜测数值）；
- `app.agent.graph`：LangGraph 工作流（状态机 + 条件分支 + 预算 + 超时）；
- `app.agent.answer`：证据整合与回答生成（LLM 叙述 + 确定性事实 + 引用校验）；
- `app.agent.trace`：结构化执行事件（供后续 Agent-Probe 消费）。

本阶段只做 Agent，不做 Agent-Probe 风险评分、不做多 Agent 协作、不做设备控制。
"""

from __future__ import annotations

__all__ = ["AGENT_VERSION"]

AGENT_VERSION = "phase3-agent-v1"
