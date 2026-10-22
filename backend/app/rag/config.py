"""RAG 运行配置：全部来自环境变量 / 项目根 `.env`，源码内不含任何凭据。

与 `app/core/config.py` 的关系：
- 该模块负责工业数据服务（Phase 1C）所需的最小配置；
- 本模块只服务 RAG，不改变原有配置契约（原测试仍按原行为通过）。

关键约定：
- `RAG_DATABASE_URL` 未设置时回退到 `DATABASE_URL`（同一库时无需额外配置）；
- Embedding 模型、维度、索引版本必须成组出现：换模型必须同时换索引版本，
  检索时按 `embedding_model + embedding_version` 过滤，避免混用不同模型向量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import PROJECT_ROOT, load_env_file

EQUIPMENT_SCOPES = (
    # 与 MetroPT-3 数据集直接关联（数据集自带说明、围绕该数据集的论文）
    "metropt3_direct",
    # 同类型设备的通用技术资料（只能作为原理/排查思路参考）
    "generic_compressor",
    # 明确属于其它型号/厂商，禁止当作本设备专属参数
    "model_specific_other",
    # 适用性无法确定，默认不参与回答
    "unknown",
)

DOC_TYPES = (
    "dataset_description",
    "technical_manual",
    "maintenance_guide",
    "fault_case",
    "research_paper",
    "standard",
    "other",
)

PARSE_STATUSES = ("pending", "parsed", "partial", "failed")
TRUST_LEVELS = (
    "official_primary",       # 数据集/机构一手文档
    "authoritative_secondary",  # 政府/标准机构技术手册
    "general_reference",      # 通用科普/一般参考资料
    "unverified",
)

DEFAULT_ALLOWED_SCOPES = ("metropt3_direct", "generic_compressor")

# 切分策略标识（同时写入 documents/chunks 的 chunking_version，便于并存与对比）
CHUNKING_STRATEGIES = ("fixed", "structural")


@dataclass(frozen=True)
class RAGSettings:
    """RAG 全链路配置（不可变，便于测试注入）。"""

    database_url: str
    knowledge_root: Path

    # Embedding
    embedding_provider: str
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    embedding_version: str
    embedding_batch_size: int
    embedding_timeout_s: float
    embedding_max_retries: int

    # LLM
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_timeout_s: float
    llm_max_retries: int
    llm_temperature: float
    llm_num_ctx: int
    llm_max_tokens: int

    # 检索
    default_top_k: int
    max_top_k: int
    rrf_k: int
    vector_candidates: int
    keyword_candidates: int
    min_similarity: float
    max_context_chars: int
    max_chunk_chars_in_context: int

    # 切分
    chunking_strategy: str
    chunking_version: str
    chunk_size: int
    chunk_overlap: int

    @property
    def safe_database_url(self) -> str:
        from app.core.config import mask_password

        return mask_password(self.database_url)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # 配置错误必须明确失败，不静默回退
        raise ValueError(f"{name} 必须是整数，当前为 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字，当前为 {raw!r}") from exc


def get_rag_settings(env_file: Path | str | None = None) -> RAGSettings:
    """读取 RAG 配置。缺失知识库连接串时回退到 `DATABASE_URL`。"""
    load_env_file(env_file)

    database_url = _env("RAG_DATABASE_URL", "") or _env("DATABASE_URL", "")
    if not database_url:
        raise ValueError(
            "缺少 RAG_DATABASE_URL / DATABASE_URL，拒绝在未知数据库上执行 RAG 操作"
        )
    if not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("RAG 数据库连接串必须使用 psycopg 3 驱动（postgresql+psycopg://）")

    knowledge_root = Path(_env("KNOWLEDGE_ROOT", str(PROJECT_ROOT / "knowledge")))
    if not knowledge_root.is_absolute():  # 相对路径按项目根解析，避免受启动目录影响
        knowledge_root = (PROJECT_ROOT / knowledge_root).resolve()

    strategy = _env("CHUNKING_STRATEGY", "structural")
    if strategy not in CHUNKING_STRATEGIES:
        raise ValueError(
            f"CHUNKING_STRATEGY 必须是 {CHUNKING_STRATEGIES} 之一，当前为 {strategy!r}"
        )

    return RAGSettings(
        database_url=database_url,
        knowledge_root=knowledge_root,
        embedding_provider=_env("EMBEDDING_PROVIDER", "ollama").lower(),
        embedding_base_url=_env("EMBEDDING_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        embedding_model=_env("EMBEDDING_MODEL", "bge-m3"),
        embedding_dim=_env_int("EMBEDDING_DIM", 1024),
        embedding_version=_env("EMBEDDING_VERSION", "bge-m3-1024-v1"),
        embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 8),
        embedding_timeout_s=_env_float("EMBEDDING_TIMEOUT_S", 120.0),
        embedding_max_retries=_env_int("EMBEDDING_MAX_RETRIES", 3),
        llm_provider=_env("LLM_PROVIDER", "ollama").lower(),
        llm_base_url=_env("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        llm_model=_env("LLM_MODEL", "qwen2.5:7b-instruct"),
        llm_timeout_s=_env_float("LLM_TIMEOUT_S", 180.0),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 2),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
        llm_num_ctx=_env_int("LLM_NUM_CTX", 8192),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 900),
        default_top_k=_env_int("RAG_TOP_K", 5),
        max_top_k=_env_int("RAG_MAX_TOP_K", 20),
        rrf_k=_env_int("RRF_K", 60),
        vector_candidates=_env_int("VECTOR_CANDIDATES", 30),
        keyword_candidates=_env_int("KEYWORD_CANDIDATES", 30),
        min_similarity=_env_float("MIN_SIMILARITY", 0.0),
        max_context_chars=_env_int("RAG_MAX_CONTEXT_CHARS", 12000),
        max_chunk_chars_in_context=_env_int("RAG_MAX_CHUNK_CHARS", 2400),
        chunking_strategy=strategy,
        chunking_version=_env("CHUNKING_VERSION", f"{strategy}-v1"),
        chunk_size=_env_int("CHUNK_SIZE", 1000),
        chunk_overlap=_env_int("CHUNK_OVERLAP", 150),
    )
