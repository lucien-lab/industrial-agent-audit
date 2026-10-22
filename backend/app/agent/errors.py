"""Agent 领域异常与错误分类。

原则（与 Phase 1C/2 一致）：
- 异常只携带可公开信息（工具名、参数名、错误码、重试次数），不含 SQL / 连接串 / API Key；
- 错误必须**可分类**：能否重试、是否需要中止后续依赖工具，都由 `error_category` 决定，
  而不是靠字符串匹配。
"""

from __future__ import annotations

from typing import Any

# 工具/节点错误类别。retryable 与 blocks_dependents 是显式策略表，不靠猜。
ERROR_CATEGORIES: dict[str, dict[str, bool]] = {
    # 参数非法（时间格式、越界、未知传感器）：重试无意义，但可以继续调用其它工具
    "invalid_input": {"retryable": False, "blocks_dependents": False},
    # 设备不存在：重试无意义，且所有依赖该设备的查询都不该继续
    "not_found": {"retryable": False, "blocks_dependents": True},
    # 合法查询但没有数据：不是错误，不重试
    "empty": {"retryable": False, "blocks_dependents": False},
    # 超时：允许有限重试
    "timeout": {"retryable": True, "blocks_dependents": False},
    # 瞬时故障（数据库不可达、Embedding/LLM 服务不可用、限流）：允许有限重试
    "transient": {"retryable": True, "blocks_dependents": False},
    # 服务内部明确失败（非瞬时）：不重试
    "service_error": {"retryable": False, "blocks_dependents": False},
    # 配置/索引不匹配：重试无意义，需要人工处理
    "config_error": {"retryable": False, "blocks_dependents": True},
    # 预算耗尽（工具调用次数、轮次、总超时）
    "budget": {"retryable": False, "blocks_dependents": True},
    # 安全护栏命中（越界提问、提示注入）
    "security": {"retryable": False, "blocks_dependents": True},
    # 未分类内部错误
    "internal": {"retryable": False, "blocks_dependents": False},
}


def is_retryable(category: str) -> bool:
    return ERROR_CATEGORIES.get(category, ERROR_CATEGORIES["internal"])["retryable"]


def blocks_dependents(category: str) -> bool:
    return ERROR_CATEGORIES.get(category, ERROR_CATEGORIES["internal"])["blocks_dependents"]


class AgentError(Exception):
    """Agent 领域异常基类。"""

    code = "agent_error"
    default_message = "Agent 执行失败"
    category = "internal"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "details": self.details,
        }


class AgentInputError(AgentError):
    """请求本身不合法（空问题、超长、非法设备编号格式）。"""

    code = "agent_invalid_request"
    default_message = "请求参数不合法"
    category = "invalid_input"


class AgentBudgetExceeded(AgentError):
    """预算耗尽（工具调用次数 / 决策轮次 / 总超时）。"""

    code = "agent_budget_exceeded"
    default_message = "执行预算耗尽"
    category = "budget"


class AgentTimeout(AgentError):
    """整轮执行超时。"""

    code = "agent_timeout"
    default_message = "Agent 执行超时"
    category = "timeout"


class ToolInputError(AgentError):
    """工具输入不合法（Pydantic 校验失败）。"""

    code = "tool_invalid_input"
    default_message = "工具参数不合法"
    category = "invalid_input"


class TraceNotFound(AgentError):
    """trace_id 不存在（进程内只保留最近的执行记录）。"""

    code = "trace_not_found"
    default_message = "Trace 不存在"
    category = "not_found"


class UnknownTool(AgentError):
    """请求了未注册的工具（不允许客户端提交任意工具代码）。"""

    code = "tool_not_registered"
    default_message = "工具未注册"
    category = "invalid_input"


__all__ = [
    "ERROR_CATEGORIES",
    "AgentBudgetExceeded",
    "AgentError",
    "AgentInputError",
    "AgentTimeout",
    "ToolInputError",
    "TraceNotFound",
    "UnknownTool",
    "blocks_dependents",
    "is_retryable",
]
