"""工具执行所需的运行时上下文（数据库会话、模型客户端、Trace 记录器）。

会话策略（比 Phase 1C 的请求级会话更严格）：
- **每个工具调用一个会话**（session-per-operation），由工具在自己的线程内打开与关闭；
- 连接串分别取 `DATABASE_URL`（工业数据）与 `RAG_DATABASE_URL`（知识库），
  **不假设两个库是同一个库**；
- 引擎在连接参数层面强制 `default_transaction_read_only=on`：
  即使工具实现出错，也不可能写入数据库（由测试 `test_agent_sql_injection` 验证）；
- 引擎带 `statement_timeout`，与线程级超时双层兜底，避免慢查询长期占用连接。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent.config import AgentSettings
from app.agent.trace import TraceRecorder
from app.core.config import get_settings
from app.rag.config import get_rag_settings

logger = logging.getLogger("industrial_agent_audit.agent")

STATEMENT_TIMEOUT_MS = 45_000


def _database_name(url: str) -> str:
    """从连接串中只取库名（日志/Trace 用，不含用户、密码、主机）。"""
    try:
        return url.rsplit("/", 1)[-1] or "unknown"
    except Exception:  # pragma: no cover - 不会发生
        return "unknown"


class AgentContext:
    """一次 Agent 执行的依赖容器。"""

    def __init__(
        self,
        *,
        settings: AgentSettings | None = None,
        recorder: TraceRecorder | None = None,
        data_url: str | None = None,
        knowledge_url: str | None = None,
        chat_model: Any | None = None,
        embedder: Any | None = None,
        readonly: bool = True,
    ) -> None:
        self.settings = settings or AgentSettings()
        self.recorder = recorder
        self._data_url = data_url
        self._knowledge_url = knowledge_url
        self._chat_model = chat_model
        self.embedder = embedder
        self.readonly = readonly
        self._engines: dict[str, Any] = {}
        self._factories: dict[str, sessionmaker[Session]] = {}

    # ------------------------------------------------------------------ #
    # 连接串（延迟读取，避免导入期强制要求环境变量）
    # ------------------------------------------------------------------ #
    @property
    def data_url(self) -> str:
        if self._data_url is None:
            self._data_url = get_settings().database_url
        return self._data_url

    @property
    def knowledge_url(self) -> str:
        if self._knowledge_url is None:
            self._knowledge_url = get_rag_settings().database_url
        return self._knowledge_url

    def describe_sources(self) -> dict[str, Any]:
        """可安全输出的数据源描述（连接串中只保留库名，不含用户/密码/主机）。"""
        return {
            "industrial_database": _database_name(self.data_url),
            "knowledge_database": _database_name(self.knowledge_url),
            "same_database": self.data_url == self.knowledge_url,
        }

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def _factory(self, url: str, *, label: str) -> sessionmaker[Session]:
        cached = self._factories.get(url)
        if cached is not None:
            return cached
        connect_args: dict[str, Any] = {"application_name": f"iaa-agent-{label}"}
        if self.readonly:
            # 连接级只读：任何写入都会失败，这是比“代码约定”更强的保证
            connect_args["options"] = (
                f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"
            )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=2,
            future=True,
            connect_args=connect_args,
        )
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._engines[url] = engine
        self._factories[url] = factory
        return factory

    @contextmanager
    def open_data_session(self) -> Iterator[Session]:
        """工业数据只读会话。"""
        session = self._factory(self.data_url, label="data")()
        try:
            yield session
        finally:
            # 只读：显式回滚结束事务并释放连接
            try:
                session.rollback()
            finally:
                session.close()

    @contextmanager
    def open_knowledge_session(self) -> Iterator[Session]:
        """知识库只读会话（独立连接串）。"""
        session = self._factory(self.knowledge_url, label="knowledge")()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    # ------------------------------------------------------------------ #
    # 模型
    # ------------------------------------------------------------------ #
    @property
    def chat_model(self) -> Any:
        """LLM 客户端（延迟创建；测试可注入替身）。"""
        if self._chat_model is None:
            from app.rag.llm import get_chat_model

            self._chat_model = get_chat_model(get_rag_settings())
        return self._chat_model

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
        self._factories.clear()

    def __enter__(self) -> "AgentContext":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def default_context(settings: AgentSettings | None = None, recorder: TraceRecorder | None = None) -> AgentContext:
    return AgentContext(settings=settings, recorder=recorder)


__all__ = ["AgentContext", "STATEMENT_TIMEOUT_MS", "default_context"]
