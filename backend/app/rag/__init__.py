"""阶段 2：工业知识 RAG（解析 → 切分 → 向量化 → 混合检索 → 有证据的回答）。

设计边界：
- 本包只做知识库与检索问答，不包含 LangGraph Agent、Agent-Probe 审计与前端；
- 所有模型调用都通过可替换的 Provider（Ollama / 兼容 HTTP API），失败必须显式抛出，
  不允许静默返回伪造向量或伪造答案；
- 数据库结构由 Alembic 迁移 `0007_knowledge_rag` 管理，应用启动不建表。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
