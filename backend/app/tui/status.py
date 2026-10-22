"""系统状态聚合：把多个接口的真实返回翻译成 ready / degraded / unavailable / unknown。

规则（必须与后端语义一致，不允许为了好看标成 Ready）：

- `unavailable`：请求失败（连接、超时、5xx）或后端明确不可用；
- `degraded`：接口可用，但结构版本与代码期望不一致、索引缺失、探测失败等；
- `ready`：接口可用且关键不变量成立（schema 一致、文档/索引非空等）；
- `unknown`：**尚未探测**（例如 LLM/Embedding 只在深度探测时才真实调用）。

主库 `schema_version` 与代码期望不一致时必须是 `degraded`，
即使 `/ready` 返回 HTTP 200——本项目当前就是这种情况（主库停在 `0004`）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.tui.client import ApiError, IaaApiClient


class ComponentState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"

    @property
    def label_zh(self) -> str:
        return {
            "ready": "正常",
            "degraded": "降级",
            "unavailable": "不可用",
            "unknown": "未探测",
        }[self.value]

    @property
    def priority(self) -> int:
        """聚合时的优先级（数字越大越严重）。"""
        return {
            "ready": 0,
            "unknown": 1,
            "degraded": 2,
            "unavailable": 3,
        }[self.value]


@dataclass(frozen=True)
class ComponentStatus:
    key: str
    label: str
    state: ComponentState
    detail: str = ""

    @property
    def summary(self) -> str:
        base = f"{self.label}: {self.state.label_zh}"
        return f"{base}（{self.detail}）" if self.detail else base


@dataclass
class SystemStatus:
    components: list[ComponentStatus] = field(default_factory=list)
    probed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    deep_probe: bool = False
    api_base_url: str = ""
    notes: list[str] = field(default_factory=list)

    def get(self, key: str) -> ComponentStatus | None:
        for item in self.components:
            if item.key == key:
                return item
        return None

    def state_of(self, key: str) -> ComponentState:
        item = self.get(key)
        return item.state if item else ComponentState.UNKNOWN

    @property
    def overall(self) -> ComponentState:
        if not self.components:
            return ComponentState.UNKNOWN
        worst = max(self.components, key=lambda item: item.state.priority)
        return worst.state

    @property
    def headline(self) -> str:
        return f"系统状态: {self.overall.label_zh}"

    def ordered_keys(self) -> list[str]:
        return [item.key for item in self.components]


def _component(key: str, label: str, state: ComponentState, detail: str = "") -> ComponentStatus:
    return ComponentStatus(key=key, label=label, state=state, detail=detail)


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, ApiError):
        if exc.kind == "timeout":
            return "探测超时"
        if exc.kind == "network":
            return "无法连接"
        if exc.kind == "invalid_response":
            return "响应契约不符"
        return f"HTTP {exc.status_code}: {exc.message}"
    return f"{type(exc).__name__}"


async def probe_system_status(client: IaaApiClient, *, deep: bool = False) -> SystemStatus:
    """并发探测所有后端能力，返回聚合状态。

    `deep=True` 会让知识库接口真实调用一次 Embedding 与 LLM（1–15 s），
    这是唯一能确认 Ollama 可用的方式；默认关闭，此时 LLM 状态为“未探测”。
    """
    health, readiness, knowledge, audit = await asyncio.gather(
        client.health(),
        client.readiness(),
        client.knowledge_health(check_services=deep),
        client.audit_summary(days=1),
        return_exceptions=True,
    )
    components: list[ComponentStatus] = []
    notes: list[str] = []

    # ---------------- API ----------------
    if isinstance(health, BaseException):
        components.append(
            _component("api", "API", ComponentState.UNAVAILABLE, _describe_error(health))
        )
        notes.append("后端 API 不可达：请先启动 `scripts/start_api.sh`，其余功能无法使用。")
    else:
        components.append(
            _component(
                "api",
                "API",
                ComponentState.READY,
                f"{health.service or 'api'} v{health.version or '?'}",
            )
        )

    # ---------------- 工业数据库 ----------------
    if isinstance(readiness, BaseException):
        components.append(
            _component(
                "industrial_db", "工业库", ComponentState.UNAVAILABLE, _describe_error(readiness)
            )
        )
    else:
        db_name = readiness.database.database or "?"
        if readiness.status == "ok" and readiness.schema_ok:
            components.append(
                _component(
                    "industrial_db",
                    "工业库",
                    ComponentState.READY,
                    f"{db_name} schema {readiness.schema_version}",
                )
            )
        else:
            detail = (
                f"{db_name} schema {readiness.schema_version or '?'} "
                f"≠ 期望 {readiness.schema_expected_version or '?'}"
            )
            components.append(
                _component("industrial_db", "工业库", ComponentState.DEGRADED, detail)
            )
            notes.append(
                "工业库迁移版本与代码期望不一致（未迁移，TUI 不会自动迁移）："
                f"{readiness.schema_version} ≠ {readiness.schema_expected_version}。"
            )

    # ---------------- RAG 知识库 ----------------
    if isinstance(knowledge, BaseException):
        components.append(
            _component("rag", "RAG", ComponentState.UNAVAILABLE, _describe_error(knowledge))
        )
    else:
        state = (
            ComponentState.READY
            if knowledge.status == "ok"
            else ComponentState.DEGRADED
            if knowledge.status == "degraded"
            else ComponentState.UNAVAILABLE
        )
        detail = (
            f"{knowledge.database.database or '?'} 文档 {knowledge.documents} / "
            f"Chunk {knowledge.chunks} / schema {knowledge.schema_version or '?'}"
        )
        if not knowledge.schema_ok:
            detail += f" ≠ 期望 {knowledge.schema_expected_version or '?'}"
            notes.append("知识库迁移版本与代码期望不一致。")
        if knowledge.warnings:
            notes.extend(f"RAG: {w}" for w in knowledge.warnings)
        components.append(_component("rag", "RAG", state, detail))

    # ---------------- 审计服务 ----------------
    if isinstance(audit, BaseException):
        if isinstance(audit, ApiError) and audit.is_auth:
            components.append(
                _component(
                    "audit",
                    "Audit",
                    ComponentState.DEGRADED,
                    "需要有效的 X-Audit-Token",
                )
            )
            notes.append("审计接口要求 X-Audit-Token：请设置 IAA_AUDIT_TOKEN 后重启 TUI。")
        else:
            components.append(
                _component("audit", "Audit", ComponentState.UNAVAILABLE, _describe_error(audit))
            )
    else:
        total = audit.get("total_audits", 0) if isinstance(audit, dict) else 0
        components.append(
            _component("audit", "Audit", ComponentState.READY, f"审计记录 {total} 条（近 1 天）")
        )

    # ---------------- Embedding / LLM ----------------
    embedding_state: ComponentState = ComponentState.UNKNOWN
    embedding_detail = "未深度探测"
    llm_state: ComponentState = ComponentState.UNKNOWN
    llm_detail = "未深度探测"
    if not isinstance(knowledge, BaseException):
        embedding_meta = knowledge.embedding or {}
        llm_meta = knowledge.llm or {}
        if deep:
            probe = embedding_meta.get("probe") or {}
            if probe.get("ok"):
                embedding_state = ComponentState.READY
                embedding_detail = (
                    f"{embedding_meta.get('model')} dim={probe.get('dim')} "
                    f"{float(probe.get('elapsed_ms') or 0):.0f}ms"
                )
            else:
                embedding_state = ComponentState.UNAVAILABLE
                embedding_detail = str(probe.get("error") or "探测失败")
            llm_probe = llm_meta.get("probe") or {}
            if llm_probe.get("ok"):
                llm_state = ComponentState.READY
                llm_detail = (
                    f"{llm_probe.get('model') or llm_meta.get('model')} "
                    f"{float(llm_probe.get('elapsed_ms') or 0):.0f}ms"
                )
            else:
                llm_state = ComponentState.UNAVAILABLE
                llm_detail = str(llm_probe.get("error") or "探测失败")
        else:
            embedding_detail = f"配置 {embedding_meta.get('model') or '?'}（未探测）"
            llm_detail = f"配置 {llm_meta.get('model') or '?'}（未探测）"
    components.append(_component("embedding", "Embedding", embedding_state, embedding_detail))
    components.append(_component("llm", "LLM", llm_state, llm_detail))

    return SystemStatus(
        components=components,
        probed_at=datetime.now(timezone.utc),
        deep_probe=deep,
        api_base_url=client.base_url,
        notes=notes,
    )


COMPONENT_LABELS: dict[str, str] = {
    "api": "API",
    "industrial_db": "工业库",
    "rag": "RAG",
    "audit": "Audit",
    "embedding": "Embedding",
    "llm": "LLM",
}


def status_from_checks(checks: dict[str, Any]) -> SystemStatus:
    """（测试/工具用）从已构造的字典快速生成状态，便于纯逻辑断言。"""
    components = [
        _component(key, COMPONENT_LABELS.get(key, key.upper()), ComponentState(value), detail)
        for key, (value, detail) in checks.items()
    ]
    return SystemStatus(components=components)


__all__ = [
    "ComponentState",
    "ComponentStatus",
    "SystemStatus",
    "probe_system_status",
    "status_from_checks",
]
