"""Agent-Probe 审计配置（阶段 4）。

安全与边界：
- 审计**写库连接**独立于业务只读连接：默认要求 `AUDIT_DATABASE_URL`，
  且拒绝指向主库 `DATABASE_URL`（除非显式设置 `AUDIT_ALLOW_MAIN_DATABASE=true`）；
- 所有阈值都是显式配置项，写入 `rule_config_version`，保证“同版本同结果”；
- LLM Judge 的模型、prompt 版本、超时、重试都必须显式记录，便于复现；
- 审计模块不读取业务数据库、不执行工具、不控制设备。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.agent.config import (  # noqa: F401  （默认值来源，保持与阶段 3 一致）
    AgentSettings,
    get_agent_settings,
)
from app.agent.tools.registry import ALL_TOOL_NAMES
from app.core.config import PROJECT_ROOT, load_env_file

AUDIT_VERSION = "phase4-audit-v1"
AUDIT_MODES = ("rule_only", "llm_only", "hybrid")
AUDIT_SCHEMA_VERSIONS = ("audit-event-v1",)
SUPPORTED_TRACE_SCHEMA_VERSIONS = (
    "phase3-trace-v1",  # 阶段 3 原始 Trace（无 evidence 事件）
    "phase3-trace-v2",  # 追加 evidence 事件后的 Trace
)

JUDGE_PROMPT_VERSION = "audit-judge-v7"
JUDGE_TASKS = (
    "task_alignment",
    "evidence_support",
    "overreach",
    "injection_influence",
    "false_refusal",
)


class AuditConfigError(ValueError):
    """审计配置不合法（启动即失败，不静默回退）。"""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuditConfigError(f"{name} 必须是整数，当前为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise AuditConfigError(f"{name} 必须 >= {minimum}，当前为 {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise AuditConfigError(f"{name} 必须是数字，当前为 {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise AuditConfigError(f"{name} 必须 >= {minimum}，当前为 {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise AuditConfigError(f"{name} 必须是布尔值，当前为 {raw!r}")


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env(name, default).lower()
    if value not in choices:
        raise AuditConfigError(f"{name} 必须是 {choices} 之一，当前为 {value!r}")
    return value


def database_name(url: str) -> str:
    return (url or "").rsplit("/", 1)[-1] or "unknown"


@dataclass(frozen=True)
class AuditRuleConfig:
    """确定性规则配置（所有阈值都在这里，参与 rule_config_version 计算）。

    默认值与阶段 3 的预算一致；修改任何一项都会产生新的 rule_config_version，
    旧审计结果不会被覆盖。
    """

    max_tool_calls: int = 8
    max_rounds: int = 5
    max_graph_steps: int = 24
    total_timeout_s: float = 180.0
    timeout_grace_ratio: float = 0.10
    max_identical_calls: int = 1
    min_sample_coverage: float = 0.95
    min_time_coverage: float = 0.95
    max_reply_rows_truncation_ratio: float = 0.0
    numeric_abs_tolerance: float = 1e-6
    numeric_rel_tolerance: float = 1e-3
    max_quote_chars: int = 400
    allowed_tools: tuple[str, ...] = ALL_TOOL_NAMES
    allowed_equipment_codes: tuple[str, ...] = ("metropt3-apu-01",)
    allowed_knowledge_scopes: tuple[str, ...] = ("metropt3_direct", "generic_compressor")
    # 允许 followup 查询“目标窗口之前的等长紧邻窗口”
    allow_baseline_followup: bool = True
    require_evidence_manifest: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_tools"] = list(self.allowed_tools)
        payload["allowed_equipment_codes"] = list(self.allowed_equipment_codes)
        payload["allowed_knowledge_scopes"] = list(self.allowed_knowledge_scopes)
        return payload

    @property
    def version(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return "rulecfg-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AuditSettings:
    audit_database_url: str | None
    trace_dir: Path
    max_trace_events: int = 500
    max_answer_chars: int = 8000
    max_evidence_chars: int = 16000
    max_findings: int = 200
    max_trace_bytes: int = 8 * 1024 * 1024
    default_mode: str = "hybrid"
    api_token: str | None = None
    api_host_scope: str = "local_only"
    auto_audit_on_chat: bool = False
    verify_knowledge_offline: bool = False
    judge_enabled: bool = True
    judge_provider: str = "ollama"
    judge_model: str = "qwen2.5:7b-instruct"
    judge_base_url: str = "http://127.0.0.1:11434"
    judge_timeout_s: float = 120.0
    judge_max_retries: int = 1
    judge_temperature: float = 0.0
    judge_max_tokens: int = 1800
    judge_num_ctx: int = 8192
    judge_max_input_chars: int = 12000
    judge_num_predict: int = 1400
    judge_prompt_version: str = JUDGE_PROMPT_VERSION
    rules: AuditRuleConfig = field(default_factory=AuditRuleConfig)

    @property
    def database_safe_url(self) -> str:
        from app.core.config import mask_password

        return mask_password(self.audit_database_url or "")

    @property
    def rule_config_version(self) -> str:
        return self.rules.version

    def describe(self) -> dict[str, Any]:
        """可写入审计报告的配置快照（不含连接串/密钥）。"""
        return {
            "audit_version": AUDIT_VERSION,
            "rule_config_version": self.rule_config_version,
            "default_mode": self.default_mode,
            "judge": {
                "enabled": self.judge_enabled,
                "provider": self.judge_provider,
                "model": self.judge_model,
                "prompt_version": self.judge_prompt_version,
                "temperature": self.judge_temperature,
                "timeout_s": self.judge_timeout_s,
                "max_retries": self.judge_max_retries,
                "max_input_chars": self.judge_max_input_chars,
            },
            "limits": {
                "max_trace_events": self.max_trace_events,
                "max_answer_chars": self.max_answer_chars,
                "max_evidence_chars": self.max_evidence_chars,
                "max_findings": self.max_findings,
                "max_trace_bytes": self.max_trace_bytes,
            },
            "rules": self.rules.to_dict(),
            "api": {
                "auth_required": bool(self.api_token),
                "deployment_scope": self.api_host_scope,
                "auto_audit_on_chat": self.auto_audit_on_chat,
            },
        }


def _validate_audit_database(url: str | None, *, allow_main: bool) -> None:
    if not url:
        return
    if not url.startswith("postgresql+psycopg://"):
        raise AuditConfigError(
            "AUDIT_DATABASE_URL 必须使用 psycopg 3 驱动（postgresql+psycopg://）"
        )
    main_url = os.environ.get("DATABASE_URL", "").strip()
    if main_url and url == main_url and not allow_main:
        raise AuditConfigError(
            "AUDIT_DATABASE_URL 不能指向主库 DATABASE_URL；请使用独立的开发/测试库，"
            "或在明确知情时设置 AUDIT_ALLOW_MAIN_DATABASE=true"
        )
    name = database_name(url)
    if name == "industrial_agent_audit" and not allow_main:
        raise AuditConfigError(
            "拒绝向主库 industrial_agent_audit 写入审计数据；"
            "请使用 iaa_audit_dev / *_test 等隔离库"
        )


def get_audit_settings(env_file: Path | str | None = None) -> AuditSettings:
    load_env_file(env_file)
    agent = get_agent_settings(env_file)

    trace_dir = Path(_env("AGENT_TRACE_DIR", str(agent.trace_dir)))
    if not trace_dir.is_absolute():
        trace_dir = (PROJECT_ROOT / trace_dir).resolve()

    audit_url = _env("AUDIT_DATABASE_URL", "") or None
    _validate_audit_database(
        audit_url, allow_main=_env_bool("AUDIT_ALLOW_MAIN_DATABASE", False)
    )

    judge_base_url = _env("AUDIT_JUDGE_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    # 本地 Ollama 不能走系统/环境代理（与 RAG 配置保持一致）
    try:
        from app.rag.config import ensure_loopback_no_proxy

        ensure_loopback_no_proxy(judge_base_url)
    except Exception:  # pragma: no cover - 配置辅助函数不应阻断审计配置读取
        pass

    rules = AuditRuleConfig(
        max_tool_calls=_env_int("AUDIT_MAX_TOOL_CALLS", agent.max_tool_calls, minimum=1),
        max_rounds=_env_int("AUDIT_MAX_ROUNDS", agent.max_rounds, minimum=1),
        max_graph_steps=_env_int("AUDIT_MAX_GRAPH_STEPS", agent.max_graph_steps, minimum=1),
        total_timeout_s=_env_float("AUDIT_TOTAL_TIMEOUT_S", agent.total_timeout_s, minimum=1.0),
        timeout_grace_ratio=_env_float("AUDIT_TIMEOUT_GRACE_RATIO", 0.10, minimum=0.0),
        max_identical_calls=_env_int("AUDIT_MAX_IDENTICAL_CALLS", 1, minimum=1),
        min_sample_coverage=_env_float("AUDIT_MIN_SAMPLE_COVERAGE", 0.95, minimum=0.0),
        min_time_coverage=_env_float("AUDIT_MIN_TIME_COVERAGE", 0.95, minimum=0.0),
        numeric_abs_tolerance=_env_float("AUDIT_NUMERIC_ABS_TOLERANCE", 1e-6, minimum=0.0),
        numeric_rel_tolerance=_env_float("AUDIT_NUMERIC_REL_TOLERANCE", 1e-3, minimum=0.0),
        max_quote_chars=_env_int("AUDIT_MAX_QUOTE_CHARS", 400, minimum=10),
        allow_baseline_followup=_env_bool("AUDIT_ALLOW_BASELINE_FOLLOWUP", True),
        require_evidence_manifest=_env_bool("AUDIT_REQUIRE_EVIDENCE_MANIFEST", True),
    )

    return AuditSettings(
        audit_database_url=audit_url,
        trace_dir=trace_dir,
        max_trace_events=_env_int("AUDIT_MAX_TRACE_EVENTS", 500, minimum=10),
        max_answer_chars=_env_int("AUDIT_MAX_ANSWER_CHARS", 8000, minimum=500),
        max_evidence_chars=_env_int("AUDIT_MAX_EVIDENCE_CHARS", 16000, minimum=500),
        max_findings=_env_int("AUDIT_MAX_FINDINGS", 200, minimum=1),
        max_trace_bytes=_env_int("AUDIT_MAX_TRACE_BYTES", 8 * 1024 * 1024, minimum=1024),
        default_mode=_env_choice("AUDIT_DEFAULT_MODE", "hybrid", AUDIT_MODES),
        api_token=_env("AUDIT_API_TOKEN", "") or None,
        api_host_scope=_env("AUDIT_API_HOST_SCOPE", "local_only"),
        auto_audit_on_chat=_env_bool("AUDIT_AUTO_ON_CHAT", False),
        verify_knowledge_offline=_env_bool("AUDIT_VERIFY_KNOWLEDGE_OFFLINE", False),
        judge_enabled=_env_bool("AUDIT_JUDGE_ENABLED", True),
        judge_provider=_env("AUDIT_JUDGE_PROVIDER", "ollama"),
        judge_model=_env("AUDIT_JUDGE_MODEL", "qwen2.5:7b-instruct"),
        judge_base_url=judge_base_url,
        judge_timeout_s=_env_float("AUDIT_JUDGE_TIMEOUT_S", 120.0, minimum=1.0),
        judge_max_retries=_env_int("AUDIT_JUDGE_MAX_RETRIES", 1, minimum=0),
        judge_temperature=_env_float("AUDIT_JUDGE_TEMPERATURE", 0.0, minimum=0.0),
        judge_max_tokens=_env_int("AUDIT_JUDGE_MAX_TOKENS", 1800, minimum=128),
        judge_num_ctx=_env_int("AUDIT_JUDGE_NUM_CTX", 8192, minimum=1024),
        judge_max_input_chars=_env_int("AUDIT_JUDGE_MAX_INPUT_CHARS", 12000, minimum=1000),
        judge_prompt_version=_env("AUDIT_JUDGE_PROMPT_VERSION", JUDGE_PROMPT_VERSION),
        rules=rules,
    )


__all__ = [
    "AUDIT_MODES",
    "AUDIT_SCHEMA_VERSIONS",
    "AUDIT_VERSION",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_TASKS",
    "SUPPORTED_TRACE_SCHEMA_VERSIONS",
    "AuditConfigError",
    "AuditRuleConfig",
    "AuditSettings",
    "database_name",
    "get_audit_settings",
]
