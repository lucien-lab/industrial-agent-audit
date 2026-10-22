"""LLM 规划器（可选的工具选择方式，默认不用）。

用途：
- 对比实验：规则规划 vs LLM 规划的工具选择准确率（评估脚本会跑两种）；
- 复杂问题：规则抽不到槽位时，让模型在**注册表内**选择工具。

安全边界（LLM 只是“建议者”，不是执行者）：
- 只能从注册表里已有的工具名中选择，未知工具直接丢弃并记录；
- 参数仍然走 Pydantic 校验与 Service 层白名单，LLM 无法注入 SQL 或新参数；
- 计划仍然受 `max_tool_calls` 裁剪；
- 任何解析/调用失败都回退到规则计划（不会因为模型抽风而无法回答）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.config import AgentSettings
from app.agent.intent import IntentResult, PlannedCall, Slots, build_plan
from app.agent.tools.registry import build_registry, tool_catalog
from app.rag.llm import ChatMessage

PLANNER_PROMPT_VERSION = "agent-planner-v1"

PLANNER_SYSTEM_PROMPT = """你是工业设备运维 Agent 的工具规划器。你只能从给定工具清单里选择工具，\
不能发明工具、不能写 SQL、不能编造参数。

规则：
1. 只选回答该问题**必需**的工具，不要把所有工具都选一遍；
2. 纯技术知识问题（含义/原理/维护建议）只用 search_technical_knowledge；
3. 数值或统计问题（最大/最小/平均/是否异常/故障事件/数据质量）用数据工具；
4. 数据工具必须给出 equipment_code、start_time、end_time（无时区 ISO 8601，左闭右开）；
5. 输出单个 JSON 对象，不要输出其它文字：
{"calls": [{"tool": "工具名", "params": {...}, "reason": "为什么调用"}]}
"""


def _strip_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def plan_with_llm(
    *,
    question: str,
    intent: IntentResult,
    slots: Slots,
    settings: AgentSettings,
    chat_model: Any,
) -> tuple[list[PlannedCall], dict[str, Any]]:
    """让 LLM 在注册表内选工具；失败时回退规则计划。"""
    registry = build_registry(settings)
    catalog = tool_catalog(settings)
    catalog_text = "\n".join(
        f"- {item['name']}（{item['category']}）：{item['description']}" for item in catalog
    )
    slot_text = json.dumps(slots.as_dict(), ensure_ascii=False)
    user_content = (
        f"问题：{question}\n意图：{intent.kind}\n已抽取槽位：{slot_text}\n\n"
        f"可用工具：\n{catalog_text}\n\n请输出 JSON 计划。"
    )
    meta: dict[str, Any] = {"status": "ok", "planner": "llm", "prompt_chars": len(user_content)}
    try:
        response = chat_model.chat(
            [
                ChatMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
                ChatMessage(role="user", content=user_content),
            ],
            json_mode=True,
            max_tokens=600,
        )
        payload = _strip_json(response.text)
    except Exception as exc:  # noqa: BLE001 - 规划失败必须回退，不阻塞回答
        meta.update({"status": "error", "error": str(exc)[:200]})
        return build_plan(intent, slots, settings), meta

    if payload is None:
        meta.update({"status": "invalid_json"})
        return build_plan(intent, slots, settings), meta

    raw_calls = payload.get("calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        meta.update({"status": "empty_plan"})
        return build_plan(intent, slots, settings), meta

    calls: list[PlannedCall] = []
    rejected: list[str] = []
    for entry in raw_calls[: settings.max_tool_calls]:
        if not isinstance(entry, dict):
            rejected.append("非对象条目")
            continue
        name = str(entry.get("tool") or "").strip()
        if name not in registry:
            rejected.append(f"未注册工具 {name}")
            continue
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            rejected.append(f"{name} 参数不是对象")
            continue
        try:
            registry[name].input_model.model_validate(params)
        except Exception as exc:  # noqa: BLE001
            rejected.append(f"{name} 参数校验失败：{str(exc)[:120]}")
            continue
        calls.append(
            PlannedCall(
                tool=name,
                params=params,
                reason=str(entry.get("reason") or "LLM 规划")[:200],
                depends_on_equipment=registry[name].requires_equipment,
            )
        )
    meta["rejected"] = rejected
    if not calls:
        meta.update({"status": "no_valid_calls"})
        return build_plan(intent, slots, settings), meta
    return calls, meta


__all__ = ["PLANNER_PROMPT_VERSION", "PLANNER_SYSTEM_PROMPT", "plan_with_llm"]
