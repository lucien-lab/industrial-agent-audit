"""RAG 领域异常。

原则：
- 模型/解析/检索失败必须抛出**可区分**的异常，由 API 层翻译成状态码；
- 异常消息只包含可公开信息（模型名、HTTP 状态、重试次数），不含 API Key；
- 不允许任何调用点在失败后返回伪造向量或伪造答案。
"""

from __future__ import annotations

from typing import Any


class RAGError(Exception):
    """RAG 异常基类。"""

    code = "rag_error"
    default_message = "RAG 处理失败"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class DocumentParseError(RAGError):
    """PDF 无法打开或整体无法解析（例如加密、损坏）。"""

    code = "document_parse_error"
    default_message = "文档解析失败"


class EmbeddingError(RAGError):
    """Embedding 调用失败（网络、超时、限流重试耗尽、维度不一致）。"""

    code = "embedding_error"
    default_message = "Embedding 服务调用失败"


class EmbeddingDimensionMismatch(EmbeddingError):
    """模型实际输出维度与配置/数据库 vector 维度不一致。"""

    code = "embedding_dimension_mismatch"
    default_message = "Embedding 维度与配置不一致"


class LLMError(RAGError):
    """LLM 调用失败（非超时）。"""

    code = "llm_error"
    default_message = "语言模型调用失败"


class LLMTimeout(LLMError):
    """LLM 调用超时。"""

    code = "llm_timeout"
    default_message = "语言模型调用超时"


class LLMRateLimited(LLMError):
    """LLM 限流且重试耗尽。"""

    code = "llm_rate_limited"
    default_message = "语言模型触发限流，重试后仍失败"


class LLMUnavailable(LLMError):
    """LLM 服务不可达。"""

    code = "llm_unavailable"
    default_message = "语言模型服务不可用"


class RetrievalError(RAGError):
    """检索层错误（索引缺失、模型版本不匹配等）。"""

    code = "retrieval_error"
    default_message = "知识检索失败"


class IndexModelMismatch(RetrievalError):
    """索引中的向量模型/版本与当前配置不一致。"""

    code = "index_model_mismatch"
    default_message = "索引向量模型与当前配置不一致，需要重新索引"
