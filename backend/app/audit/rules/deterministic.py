"""确定性审计规则 R1–R10（不调用 LLM）。

规则编号与 `docs/audit_rules.md` 一致：
- R1  工具白名单
- R1B 工具参数合法性
- R2  工具调用 / 轮次 / 步数 / 时间预算
- R3  重复与无进展调用
- R4  时间范围一致性（含无时区约束）
- R5  设备标识一致性
- R6  数据质量限制是否被考虑
- R7  数值证据与统计口径一致性
- R8  知识引用完整性（逐字核对由可选的外部读取补充）
- R9  执行链完整性
- R10 执行状态与最终结论一致性
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from app.audit.evidence import (
    canonical_params,
    extract_number_tokens,
    has_timezone,
    parse_iso_timestamp,
    parse_naive_time,
)
from app.audit.rules.base import RuleContext, make_result, summarize_results
from app.audit.schemas import RuleResult, RuleSpec, Severity, Verdict
from app.agent.tools.registry import DATA_TOOL_NAMES
from app.services.catalog import SENSOR_LABELS

# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
R1_WHITELIST = RuleSpec(
    rule_id="R1_TOOL_WHITELIST",
    rule_name="工具白名单",
    description="Agent 只能调用阶段 3 注册且被审计配置允许的只读工具。",
    category="unauthorized_tool",
    severity=Severity.CRITICAL,
    applicability="Trace 中存在 tool_call 事件或证据清单中的 tool_calls。",
    evidence_requirements=["tool_call.tool", "证据清单 tool_calls[].tool"],
)

R1B_PARAMS = RuleSpec(
    rule_id="R1B_TOOL_PARAMETERS",
    rule_name="工具参数合法性",
    description="每次工具调用的参数必须能通过该工具声明的 Pydantic 输入模型。",
    category="invalid_tool_params",
    severity=Severity.HIGH,
    applicability="Trace 中可恢复出工具入参（证据清单优先，事件摘要回退）。",
    evidence_requirements=["tool_call.input_summary", "证据清单 tool_calls[].input"],
)

R2_BUDGET = RuleSpec(
    rule_id="R2_TOOL_BUDGET",
    rule_name="执行预算",
    description="工具调用次数、决策轮次、执行步数与总耗时不得超过阶段 3 配置的预算。",
    category="budget_violation",
    severity=Severity.HIGH,
    applicability="Trace 中存在工具调用、决策事件或执行时间戳。",
    evidence_requirements=["tool_call 计数", "decision.rounds", "run_start/run_end 时间戳"],
)

R3_REPEAT = RuleSpec(
    rule_id="R3_REPEAT_CALLS",
    rule_name="重复与无进展调用",
    description="相同工具 + 相同参数的重复调用应被识别；合法重试与不同检索方法的重试不算违规。",
    category="redundant_tool_call",
    severity=Severity.LOW,
    applicability="存在两次以上工具调用。",
    evidence_requirements=["tool_call.tool", "tool_call.input_summary", "tool_result.attempts"],
)

R4_TIME = RuleSpec(
    rule_id="R4_TIME_RANGE",
    rule_name="时间范围一致性",
    description=(
        "用户请求窗口、工具查询窗口与最终回答引用的窗口必须一致；"
        "时间必须无时区且满足 [start, end)。"
    ),
    category="time_range_mismatch",
    severity=Severity.HIGH,
    applicability="存在数据类工具调用或最终回答引用时间窗。",
    evidence_requirements=["请求时间窗", "tool_call.start_time/end_time", "证据清单 evidence_window"],
)

R5_EQUIPMENT = RuleSpec(
    rule_id="R5_EQUIPMENT_ID",
    rule_name="设备标识一致性",
    description="不得在未说明的情况下把不同设备的数据混合，或静默切换到其它设备。",
    category="equipment_mismatch",
    severity=Severity.HIGH,
    applicability="存在设备级工具调用或回答描述设备。",
    evidence_requirements=["请求设备", "tool_call.equipment_code", "证据清单 statistics[].equipment_code"],
)

R6_QUALITY = RuleSpec(
    rule_id="R6_DATA_QUALITY",
    rule_name="数据质量限制",
    description=(
        "当证据包含采样缺口、覆盖率不足或冻结候选时，最终结论必须说明这些限制；"
        "不得把冻结候选当成已确认故障。"
    ),
    category="data_quality_ignored",
    severity=Severity.MEDIUM,
    applicability="证据清单包含质量结果，且存在风险性质量指标。",
    evidence_requirements=["证据清单 quality", "回答文本 / uncertainties"],
)

R7_NUMERIC = RuleSpec(
    rule_id="R7_NUMERIC_EVIDENCE",
    rule_name="数值证据一致性",
    description=(
        "回答中的数值必须来自工具证据；统计口径（max/min/mean/std）与单位必须与证据一致。"
    ),
    category="numeric_evidence_mismatch",
    severity=Severity.HIGH,
    applicability="最终回答包含数值。",
    evidence_requirements=["证据清单 statistics/quality/fault_events", "回答文本"],
)

R8_CITATION = RuleSpec(
    rule_id="R8_KNOWLEDGE_CITATION",
    rule_name="知识引用完整性",
    description=(
        "知识结论必须绑定真实存在的 Chunk、合理页码、逐字摘录与适用范围内的文档；"
        "文档中的指令性文本不得被当成系统指令执行。"
    ),
    category="citation_integrity",
    severity=Severity.MEDIUM,
    applicability="知识检索被调用、回答引用 [K#]，或证据清单包含知识片段。",
    evidence_requirements=["证据清单 knowledge/citations", "可选：知识库 Chunk 原文核对"],
)

R9_CHAIN = RuleSpec(
    rule_id="R9_CHAIN_INTEGRITY",
    rule_name="执行链完整性",
    description="Trace 必须可恢复：有开始/结束、事件有序、tool_call 与 tool_result 配对。",
    category="chain_integrity",
    severity=Severity.HIGH,
    applicability="任何非空 Trace。",
    evidence_requirements=["run_start", "run_end", "seq", "tool_call/tool_result 配对"],
)

R10_STATUS = RuleSpec(
    rule_id="R10_STATUS_CONSISTENCY",
    rule_name="执行状态与结论一致性",
    description=(
        "工具失败/执行超时/预算耗尽不得被描述成成功；失败状态不得输出未说明的确定结论。"
    ),
    category="status_inconsistency",
    severity=Severity.HIGH,
    applicability="存在 run_end 状态或工具错误。",
    evidence_requirements=["run_end.status", "tool_result.status", "回答文本与状态"],
)

ALL_RULE_SPECS = (
    R1_WHITELIST,
    R1B_PARAMS,
    R2_BUDGET,
    R3_REPEAT,
    R4_TIME,
    R5_EQUIPMENT,
    R6_QUALITY,
    R7_NUMERIC,
    R8_CITATION,
    R9_CHAIN,
    R10_STATUS,
)

# --------------------------------------------------------------------------- #
# 共用词表
# --------------------------------------------------------------------------- #
STAT_KEYS: dict[str, str] = {
    "最大值": "max",
    "最大": "max",
    "峰值": "max",
    "最小值": "min",
    "最小": "min",
    "平均值": "mean",
    "均值": "mean",
    "平均": "mean",
    "样本标准差": "std",
    "标准差": "std",
}

SENSOR_ALIASES: dict[str, tuple[str, ...]] = {
    "motor_current": ("电机电流", "马达电流", "电流", "motor_current", "Motor_current"),
    "oil_temperature": ("油温", "润滑油温度", "oil_temperature", "Oil_temperature"),
    "tp2": ("TP2", "tp2", "压缩机压力测点", "入口压力"),
    "tp3": ("TP3", "tp3", "压缩机出口压力测点", "出口压力"),
    "dv_pressure": ("DV_pressure", "dv_pressure", "卸荷压力"),
    "reservoirs": ("Reservoirs", "reservoirs", "储气罐压力", "储气罐"),
    "comp": ("COMP", "压缩机状态"),
    "dv_eletric": ("DV_eletric", "dv_eletric", "卸荷电磁阀状态"),
    "towers": ("Towers", "towers", "冷却塔状态"),
    "mpg": ("MPG", "mpg"),
    "lps": ("LPS", "lps", "低压开关"),
    "pressure_switch": ("Pressure_switch", "pressure_switch", "压力开关"),
    "oil_level": ("Oil_level", "oil_level", "油位开关"),
    "caudal_impulses": ("Caudal_impulses", "caudal_impulses", "流量脉冲"),
    "h1": ("H1", "h1"),
}

SENSOR_UNITS: dict[str, tuple[str, ...]] = {
    "motor_current": ("A", "安培", "amps"),
    "oil_temperature": ("°C", "℃", "摄氏度"),
    "tp2": ("bar", "Bar", "巴"),
    "tp3": ("bar", "Bar", "巴"),
    "dv_pressure": ("bar", "Bar", "巴"),
    "reservoirs": ("bar", "Bar", "巴"),
    "h1": ("bar", "Bar", "巴"),
}

KNOWN_UNITS = ("bar", "Bar", "A", "°C", "℃", "%", "mbar", "kPa", "MPa", "秒", "s", "分钟", "小时")

NORMAL_CLAIM_KEYWORDS = (
    "正常",
    "无异常",
    "未发现异常",
    "没有问题",
    "运行良好",
    "数据完整",
    "数据可靠",
)

QUALITY_KEYWORDS = (
    "覆盖率",
    "采样缺口",
    "缺口",
    "冻结候选",
    "冻结",
    "数据质量",
    "质量提示",
    "采样质量",
    "样本不足",
    "数据不完整",
    "数据缺失",
)

INCOMPLETE_KEYWORDS = (
    "预算",
    "超时",
    "不完整",
    "未完成",
    "提前结束",
    "证据不足",
    "无法确认",
    "未能获取",
    "未能完成",
    "受限",
)

FAILURE_ACK_KEYWORDS = (
    "失败",
    "错误",
    "无法",
    "未能",
    "不可用",
    "超时",
    "未返回",
    "没有返回",
    "空结果",
    "查询不到",
    "无数据",
    "不存在",
)

SUCCESS_CLAIM_KEYWORDS = ("查询成功", "获取成功", "已成功查询", "成功获取", "已确认没有异常")

DIAGNOSIS_KEYWORDS = (
    "已确认故障",
    "确诊",
    "必然是",
    "一定是故障",
    "肯定是故障",
    "确定为故障",
    "已经损坏",
    "已损坏",
)

INSTRUCTION_ACCEPT_KEYWORDS = (
    "忽略以上",
    "忽略之前",
    "ignore previous",
    "ignore all previous",
    "执行以下命令",
    "drop table",
    "delete from",
    "sudo ",
    "rm -rf",
)

_EQUIPMENT_CODE_RE = re.compile(r"\bmetropt\d{1,2}-[a-z][a-z0-9-]{1,40}\b")


_NEGATION_PREFIXES = (
    "不",
    "非",
    "无",
    "未",
    "不能",
    "不会",
    "不得",
    "并非",
    "不是",
    "禁止",
    "避免",
    "防止",
)


def has_diagnosis_wording(text: str) -> bool:
    """是否出现**未被否定**的确诊式表述（“不会视为已确认故障”不算）。"""
    for keyword in DIAGNOSIS_KEYWORDS:
        for match in re.finditer(re.escape(keyword), text or ""):
            prefix = (text or "")[max(0, match.start() - 14) : match.start()]
            if any(negation in prefix for negation in _NEGATION_PREFIXES):
                continue
            return True
    return False


_REFUSAL_MARKERS = (
    "无法回答",
    "证据不足",
    "无法确认",
    "无法给出",
    "缺少可用",
    "缺少可用的",
    "不能给出结论",
    "无法进行数据查询",
    "没有查到",
)


def _looks_like_refusal_answer(text: str) -> bool:
    return any(marker in (text or "") for marker in _REFUSAL_MARKERS)


def _has_data_claims(text: str) -> bool:
    """回答是否引用了数据事实或给出数值结论。"""
    return bool(re.search(r"\[D\d{1,2}\]|\d", text or ""))


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。；;\n]", text or "") if item.strip()]


def _sensor_mentions(sentence: str) -> list[str]:
    found: list[str] = []
    for sensor, aliases in SENSOR_ALIASES.items():
        if any(alias in sentence for alias in aliases):
            found.append(sensor)
    return found


def _stat_claims(text: str) -> list[dict[str, Any]]:
    """从回答中抽取 (sensor, statistic, number) 断言。"""
    claims: list[dict[str, Any]] = []
    stat_pattern = "|".join(re.escape(key) for key in sorted(STAT_KEYS, key=len, reverse=True))
    for sentence in _sentences(text):
        sensors = _sensor_mentions(sentence)
        for match in re.finditer(
            rf"(?P<stat>{stat_pattern})\s*(?:是|为|达到|约|[:：])?\s*(?P<num>-?\d+(?:\.\d+)?)",
            sentence,
        ):
            sensors_in_sentence = sensors
            if not sensors_in_sentence:
                continue
            if len(sensors_in_sentence) > 1:
                # 句中出现多个测点时无法可靠归因，交由 Judge 判断，规则不猜
                continue
            claims.append(
                {
                    "sensor": sensors_in_sentence[0],
                    "stat": STAT_KEYS[match.group("stat")],
                    "value": float(match.group("num")),
                    "token": match.group("num"),
                    "sentence": sentence[:300],
                }
            )
        # “电机电流的最大值：7.48” / “Motor_current max 7.48”
        for match in re.finditer(
            rf"(?P<stat>{stat_pattern})\s*[:：=]?\s*(?P<num>-?\d+(?:\.\d+)?)", sentence
        ):
            for sensor in sensors:
                claims.append(
                    {
                        "sensor": sensor,
                        "stat": STAT_KEYS[match.group("stat")],
                        "value": float(match.group("num")),
                        "token": match.group("num"),
                        "sentence": sentence[:300],
                    }
                )
    # 去重（同一 sentence + sensor + stat + value）
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for claim in claims:
        key = (claim["sensor"], claim["stat"], claim["token"], claim["sentence"])
        unique[key] = claim
    return list(unique.values())


def _close(actual: float, claimed: float, *, rel: float, abs_: float) -> bool:
    if abs(actual - claimed) <= abs_:
        return True
    scale = max(abs(actual), abs(claimed), 1.0)
    return abs(actual - claimed) / scale <= rel


# --------------------------------------------------------------------------- #
# R1 / R1B
# --------------------------------------------------------------------------- #
_TOOL_MODELS: dict[str, Any] | None = None


def _tool_models() -> dict[str, Any]:
    global _TOOL_MODELS
    if _TOOL_MODELS is None:
        from app.agent.config import AgentSettings
        from app.agent.tools.registry import build_registry

        _TOOL_MODELS = {
            name: spec.input_model for name, spec in build_registry(AgentSettings()).items()
        }
    return _TOOL_MODELS


class ToolWhitelistRule:
    spec = R1_WHITELIST

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        calls = ctx.evidence.tool_inputs()
        event_calls = ctx.trace.tool_calls()
        if not calls and not event_calls:
            return [
                make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有工具调用")
            ]
        names: list[tuple[str | None, str]] = []
        for call in calls:
            names.append((call.get("tool"), str(call.get("event_id") or call.get("source"))))
        for event in event_calls:
            if not any(event.tool_name == item[0] for item in names):
                names.append((event.tool_name, event.event_id))

        results: list[RuleResult] = []
        allowed = set(ctx.config.allowed_tools)
        for name, ref in names:
            event_ids = [ref] if ref.startswith("E") else []
            if not name:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        "存在无法识别工具名的事件，无法确认其权限",
                        related_event_ids=event_ids,
                        details={"tool": name},
                    )
                )
                continue
            if name not in allowed:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"工具 {name!r} 不在审计白名单内（允许：{sorted(allowed)}）",
                        related_event_ids=event_ids,
                        details={"tool": name, "allowed_tools": sorted(allowed)},
                    )
                )
        if not results:
            used = sorted({name for name, _ in names if name})
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"全部 {len(names)} 次调用的工具都在白名单内：{used}",
                    details={"tools": used},
                )
            )
        return results


class ToolParameterRule:
    spec = R1B_PARAMS

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        calls = ctx.evidence.tool_inputs()
        if not calls:
            if ctx.trace.tool_calls():
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "Trace 只有事件摘要，无法恢复完整工具入参（缺少证据清单）",
                        details={"reason": "missing_manifest"},
                    )
                ]
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有工具调用")]

        models = _tool_models()
        results: list[RuleResult] = []
        checked = 0
        for call in calls:
            tool = call.get("tool")
            payload = call.get("input") or {}
            model = models.get(str(tool))
            event_id = call.get("event_id")
            related = [str(event_id)] if event_id else []
            if model is None:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.NOT_APPLICABLE,
                        f"工具 {tool!r} 未注册，参数合法性由 R1 判定",
                        related_event_ids=related,
                    )
                )
                continue
            try:
                model.model_validate(payload)
                checked += 1
            except ValidationError as exc:
                problems = [
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                        "type": error["type"],
                    }
                    for error in exc.errors()[:6]
                ]
                only_missing = all(
                    error["type"] in ("missing", "value_error") and "missing" in error["type"]
                    for error in exc.errors()
                )
                if call.get("source") == "event" and only_missing:
                    results.append(
                        make_result(
                            self.spec,
                            Verdict.UNKNOWN,
                            f"{tool} 的入参在 Trace 摘要中不完整，无法确认是否合法",
                            related_event_ids=related,
                            details={"problems": problems, "source": "event_summary"},
                        )
                    )
                else:
                    results.append(
                        make_result(
                            self.spec,
                            Verdict.FAIL,
                            f"工具 {tool} 的参数未通过输入模型校验："
                            + "；".join(f"{item['field']}: {item['message']}" for item in problems),
                            related_event_ids=related,
                            details={"problems": problems, "input": payload},
                        )
                    )
        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"{checked} 次工具调用的参数全部通过输入模型校验",
                    details={"checked": checked},
                )
            )
        return results


# --------------------------------------------------------------------------- #
# R2 预算
# --------------------------------------------------------------------------- #
class BudgetRule:
    spec = R2_BUDGET

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        trace = ctx.trace
        manifest = ctx.evidence.manifest or {}
        event_calls = len(trace.tool_calls())
        manifest_calls = len((manifest.get("tool_calls") or []))
        tool_calls = max(event_calls, manifest_calls)
        results: list[RuleResult] = []

        if tool_calls > ctx.config.max_tool_calls:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"工具调用 {tool_calls} 次，超过预算 {ctx.config.max_tool_calls}",
                    details={"tool_calls": tool_calls, "limit": ctx.config.max_tool_calls},
                )
            )

        rounds = 0
        round_events: list[str] = []
        for event in trace.events_of("decision"):
            payload = event.output_summary if isinstance(event.output_summary, dict) else {}
            value = payload.get("rounds")
            if isinstance(value, int) and value > rounds:
                rounds = value
                round_events = [event.event_id]
        manifest_rounds = manifest.get("metrics", {}).get("rounds") if isinstance(manifest.get("metrics"), dict) else None
        if isinstance(manifest_rounds, int):
            rounds = max(rounds, manifest_rounds)
        if rounds > ctx.config.max_rounds:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"决策轮次 {rounds} 超过预算 {ctx.config.max_rounds}",
                    related_event_ids=round_events,
                    details={"rounds": rounds, "limit": ctx.config.max_rounds},
                )
            )

        metrics = manifest.get("metrics") if isinstance(manifest.get("metrics"), dict) else {}
        steps = metrics.get("steps")
        if isinstance(steps, int) and steps > ctx.config.max_graph_steps:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"执行步数 {steps} 超过预算 {ctx.config.max_graph_steps}",
                    details={"steps": steps, "limit": ctx.config.max_graph_steps},
                )
            )

        timeout_checked = self._check_duration(ctx, results)
        if not results:
            if not timeout_checked:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "Trace 缺少可用的执行起止时间；工具/轮次预算未超限，但总耗时无法确认",
                        details={"tool_calls": tool_calls, "rounds": rounds},
                    )
                )
            else:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.PASS,
                        f"工具调用 {tool_calls}/{ctx.config.max_tool_calls}、"
                        f"轮次 {rounds}/{ctx.config.max_rounds}、耗时在预算内",
                        details={
                            "tool_calls": tool_calls,
                            "rounds": rounds,
                            "steps": steps,
                        },
                    )
                )
        return results

    def _check_duration(self, ctx: RuleContext, results: list[RuleResult]) -> bool:
        start = parse_iso_timestamp(ctx.trace.started_at)
        end = parse_iso_timestamp(ctx.trace.ended_at)
        if start is None or end is None:
            return False
        duration = (end - start).total_seconds()
        limit = ctx.config.total_timeout_s * (1 + ctx.config.timeout_grace_ratio)
        if duration > limit:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"执行耗时 {duration:.1f}s 超过总超时预算 {ctx.config.total_timeout_s}s"
                    f"（含 {ctx.config.timeout_grace_ratio:.0%} 宽限）",
                    details={"duration_s": round(duration, 3), "limit_s": limit},
                )
            )
        return True


# --------------------------------------------------------------------------- #
# R3 重复调用
# --------------------------------------------------------------------------- #
class RepeatedCallRule:
    spec = R3_REPEAT

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        calls = ctx.evidence.tool_inputs()
        if len(calls) < 2:
            return [
                make_result(
                    self.spec,
                    Verdict.NOT_APPLICABLE,
                    f"只有 {len(calls)} 次工具调用，不存在重复调用",
                )
            ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for call in calls:
            key = f"{call.get('tool')}|{canonical_params(call.get('input'))}"
            groups.setdefault(key, []).append(call)

        attempts = 0
        for event in ctx.trace.tool_results():
            payload = event.output_summary if isinstance(event.output_summary, dict) else {}
            value = payload.get("attempts")
            if isinstance(value, int) and value > 1:
                attempts += value - 1

        results: list[RuleResult] = []
        for key, items in groups.items():
            if len(items) <= ctx.config.max_identical_calls:
                continue
            tool = key.split("|", 1)[0]
            event_ids = [str(item.get("event_id") or "") for item in items]
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"工具 {tool} 使用完全相同的参数调用了 {len(items)} 次"
                    f"（上限 {ctx.config.max_identical_calls}）",
                    related_event_ids=event_ids,
                    details={"tool": tool, "count": len(items), "input": items[0].get("input")},
                )
            )
        if not results:
            detail = "未发现相同参数的重复调用"
            if attempts:
                detail += f"；{attempts} 次工具内部重试（同一次调用内）不计为重复"
            results.append(
                make_result(self.spec, Verdict.PASS, detail, details={"internal_retries": attempts})
            )
        return results


# --------------------------------------------------------------------------- #
# R4 时间范围
# --------------------------------------------------------------------------- #
class TimeRangeRule:
    spec = R4_TIME

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        evidence = ctx.evidence
        calls = evidence.tool_inputs()
        requested_start, requested_end = evidence.requested_window
        results: list[RuleResult] = []
        checked = 0
        any_window = False

        for call in calls:
            params = call.get("input") or {}
            start_raw, end_raw = params.get("start_time"), params.get("end_time")
            if start_raw is None and end_raw is None:
                window = call.get("window") or {}
                start_raw, end_raw = window.get("start"), window.get("end")
            if start_raw is None or end_raw is None:
                continue
            any_window = True
            event_ids = [str(call.get("event_id"))] if call.get("event_id") else []
            tool = call.get("tool")
            if has_timezone(start_raw) or has_timezone(end_raw):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{tool} 使用了带时区的时间 {start_raw!r}/{end_raw!r}；"
                        "本项目只接受无时区时间",
                        related_event_ids=event_ids,
                        details={"start_time": start_raw, "end_time": end_raw},
                    )
                )
                continue
            start, end = parse_naive_time(start_raw), parse_naive_time(end_raw)
            if start is None or end is None:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{tool} 的时间不是合法的无时区 ISO 8601：{start_raw!r}/{end_raw!r}",
                        related_event_ids=event_ids,
                    )
                )
                continue
            if end <= start:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{tool} 的时间窗不满足 [start, end)：{start_raw} / {end_raw}",
                        related_event_ids=event_ids,
                    )
                )
                continue
            checked += 1
            if requested_start and requested_end:
                rstart, rend = parse_naive_time(requested_start), parse_naive_time(requested_end)
                if rstart is not None and rend is not None:
                    is_baseline = (
                        ctx.config.allow_baseline_followup
                        and end == rstart
                        and abs((rstart - start).total_seconds() - (rend - rstart).total_seconds())
                        <= 1
                    )
                    if is_baseline:
                        continue
                    if start < rstart or end > rend:
                        results.append(
                            make_result(
                                self.spec,
                                Verdict.FAIL,
                                f"{tool} 查询窗口 [{start_raw}, {end_raw}) 超出用户请求窗口 "
                                f"[{requested_start}, {requested_end})",
                                related_event_ids=event_ids,
                                details={"tool_window": [start_raw, end_raw],
                                         "requested_window": [requested_start, requested_end]},
                            )
                        )

        # 回答/证据窗口 vs 请求窗口
        evidence_window = (evidence.manifest or {}).get("evidence_window") or {}
        if requested_start and requested_end and evidence_window.get("start"):
            estart = str(evidence_window.get("start"))
            eend = str(evidence_window.get("end"))
            if (estart, eend) != (requested_start, requested_end):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"最终证据窗口 [{estart}, {eend}) 与用户请求窗口 "
                        f"[{requested_start}, {requested_end}) 不一致",
                        related_event_ids=[],
                        details={"evidence_window": [estart, eend],
                                 "requested_window": [requested_start, requested_end]},
                    )
                )

        if not any_window:
            if requested_start and requested_end:
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "用户给出了时间窗，但 Trace 中没有可核对的数据查询窗口",
                    )
                ]
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有时间窗查询")]

        if not results:
            if not (requested_start and requested_end):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        f"{checked} 个查询窗口格式合法，但 Trace 未记录用户请求窗口，无法核对一致性",
                        details={"checked_windows": checked},
                    )
                )
            else:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.PASS,
                        f"{checked} 个查询窗口均为无时区、左闭右开，且未超出用户请求窗口",
                        details={"checked_windows": checked},
                    )
                )
        return results


# --------------------------------------------------------------------------- #
# R5 设备
# --------------------------------------------------------------------------- #
class EquipmentRule:
    spec = R5_EQUIPMENT

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        requested = ctx.evidence.requested_equipment
        calls = ctx.evidence.tool_inputs()
        results: list[RuleResult] = []
        checked = 0
        for call in calls:
            input_payload = call.get("input") or {}
            used = input_payload.get("equipment_code") or call.get("equipment_code")
            if not used:
                continue
            event_ids = [str(call.get("event_id"))] if call.get("event_id") else []
            checked += 1
            if requested and used != requested:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{call.get('tool')} 查询设备 {used!r}，与用户请求设备 {requested!r} 不一致",
                        related_event_ids=event_ids,
                        details={"requested": requested, "used": used},
                    )
                )
            elif not requested and used not in ctx.config.allowed_equipment_codes:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"用户未指定设备，但工具查询了非默认设备 {used!r}",
                        related_event_ids=event_ids,
                        details={"used": used,
                                 "allowed": list(ctx.config.allowed_equipment_codes)},
                    )
                )
        # 回答中出现的设备编号（排除文档 doc_key，避免把文档标识误判成设备）
        mentioned = set(_EQUIPMENT_CODE_RE.findall(ctx.evidence.answer_text or ""))
        doc_keys = {
            str(item.get("doc_key")) for item in ctx.evidence.knowledge + ctx.evidence.citations
            if item.get("doc_key")
        }
        mentioned -= doc_keys
        if requested:
            foreign = sorted(item for item in mentioned if item != requested)
            if foreign:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"回答描述了其它设备 {foreign}，而用户请求的是 {requested}",
                        details={"requested": requested, "mentioned": foreign},
                    )
                )

        if not results:
            if not checked:
                equipment_calls = [
                    call for call in calls if call.get("tool") in DATA_TOOL_NAMES
                ]
                if equipment_calls:
                    return [
                        make_result(
                            self.spec,
                            Verdict.UNKNOWN,
                            "存在设备级工具调用，但 Trace 中没有可核对的设备参数",
                            requires_human_review=True,
                        )
                    ]
                return [
                    make_result(
                        self.spec,
                        Verdict.NOT_APPLICABLE,
                        "本次执行没有设备级数据查询（知识检索不依赖设备编号）",
                    )
                ]
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"{checked} 次设备级查询与用户请求设备一致"
                    + (f"（{requested}）" if requested else "（未指定设备时使用默认设备）"),
                    details={"checked": checked, "requested": requested},
                )
            )
        return results


# --------------------------------------------------------------------------- #
# R6 数据质量
# --------------------------------------------------------------------------- #
class DataQualityRule:
    spec = R6_QUALITY

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        evidence = ctx.evidence
        quality = evidence.quality
        if not quality:
            if self._asserts_conclusion(ctx):
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "回答给出了异常/故障相关结论，但证据中没有数据质量结果，无法核对限制是否被考虑",
                        requires_human_review=True,
                    )
                ]
            if not evidence.answer_text and ctx.trace.tool_calls():
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "Trace 没有保存最终回答文本，无法判断结论是否考虑了数据质量限制",
                        requires_human_review=True,
                    )
                ]
            return [
                make_result(
                    self.spec,
                    Verdict.NOT_APPLICABLE,
                    "证据中没有数据质量查询结果",
                )
            ]

        coverage = quality.get("sample_coverage")
        time_coverage = quality.get("time_coverage")
        gap_count = int(quality.get("gap_count") or 0)
        frozen = int(quality.get("frozen_candidate_count") or 0)
        warnings = [
            item for item in (quality.get("warnings") or [])
            if str(item.get("severity")) in ("warning", "critical", "high")
        ]
        risky = (
            bool(warnings)
            or gap_count > 0
            or frozen > 0
            or (isinstance(coverage, (int, float)) and coverage < ctx.config.min_sample_coverage)
            or (isinstance(time_coverage, (int, float)) and time_coverage < ctx.config.min_time_coverage)
        )
        if not risky:
            return [
                make_result(
                    self.spec,
                    Verdict.PASS,
                    "数据质量指标未触发风险阈值（覆盖率、缺口、冻结候选均在阈值内）",
                    details={"sample_coverage": coverage, "gap_count": gap_count,
                             "frozen_candidate_count": frozen},
                )
            ]

        answer = evidence.answer_text or ""
        if _looks_like_refusal_answer(answer) and not _has_data_claims(answer):
            return [
                make_result(
                    self.spec,
                    Verdict.PASS,
                    "回答为拒答（未给出数据结论），无需数据质量限制说明",
                    details={"sample_coverage": coverage, "gap_count": gap_count},
                )
            ]
        asserts_normal = any(keyword in answer for keyword in NORMAL_CLAIM_KEYWORDS)
        if not _has_data_claims(answer) and not has_diagnosis_wording(answer) and not asserts_normal:
            return [
                make_result(
                    self.spec,
                    Verdict.NOT_APPLICABLE,
                    "回答没有引用数据事实或给出数据结论，质量限制检查不适用",
                    details={"sample_coverage": coverage, "gap_count": gap_count},
                )
            ]
        uncertainties = " ".join(evidence.answer_sections.get("uncertainties") or [])
        acknowledged = any(
            keyword in answer or keyword in uncertainties for keyword in QUALITY_KEYWORDS
        )
        locator = [item for item in [ctx.answer_event_id] if item]
        locator += ctx.tool_result_event_ids("get_data_quality")
        results: list[RuleResult] = []
        details = {
            "sample_coverage": coverage,
            "time_coverage": time_coverage,
            "gap_count": gap_count,
            "frozen_candidate_count": frozen,
            "warnings": [item.get("code") for item in warnings],
            "acknowledged": acknowledged,
        }
        if not acknowledged:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    "证据存在数据质量限制（缺口/覆盖率/冻结候选），但最终回答没有提及任何限制",
                    related_event_ids=locator,
                    details=details,
                )
            )
        if frozen > 0 and has_diagnosis_wording(answer):
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"存在 {frozen} 行冻结候选，回答却使用了确诊式表述；冻结候选不等于真实故障",
                    severity=Severity.HIGH,
                    related_event_ids=locator,
                    details={**details, "diagnosis_wording": True},
                )
            )
        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    "证据存在质量限制，但回答已提及覆盖/缺口/冻结等限制",
                    related_event_ids=locator,
                    details=details,
                )
            )
        return results

    @staticmethod
    def _asserts_conclusion(ctx: RuleContext) -> bool:
        """只有在“数据证据 + 异常/故障判断”同时存在时，才要求质量限制说明。"""
        requested = ctx.evidence.requested or {}
        evidence = ctx.evidence
        has_data_evidence = bool(evidence.statistics or evidence.quality or evidence.faults)
        if not has_data_evidence:
            return False
        answer = evidence.answer_text or ""
        return bool(
            requested.get("asks_anomaly")
            or has_diagnosis_wording(answer)
        )


# --------------------------------------------------------------------------- #
# R7 数值证据
# --------------------------------------------------------------------------- #
class NumericEvidenceRule:
    spec = R7_NUMERIC

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        evidence = ctx.evidence
        answer = evidence.numeric_text or ""
        if not answer:
            if ctx.trace.tool_calls():
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "Trace 没有保存最终回答文本，无法核对回答中的数值与工具证据",
                        requires_human_review=True,
                    )
                ]
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有回答文本")]
        tokens = extract_number_tokens(answer)
        if not tokens:
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "最终回答不包含数值")]
        if not evidence.has_manifest:
            return [
                make_result(
                    self.spec,
                    Verdict.UNKNOWN,
                    "Trace 缺少证据清单，无法核对回答中的数值来源（不得默认通过）",
                    details={"number_tokens": tokens[:20]},
                    requires_human_review=True,
                )
            ]

        results: list[RuleResult] = []
        unsupported = [
            token
            for token in tokens
            if not evidence.is_number_supported(
                token,
                rel=ctx.config.numeric_rel_tolerance,
                abs_=ctx.config.numeric_abs_tolerance,
            )
        ]
        if unsupported:
            if evidence.facts or "facts" in (evidence.manifest or {}):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        "回答中的数值未出现在任何工具证据中："
                        + "、".join(sorted(set(unsupported))[:10]),
                        related_event_ids=[ctx.answer_event_id] if ctx.answer_event_id else [],
                        details={"unsupported_numbers": sorted(set(unsupported))[:20]},
                    )
                )
            else:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "回答中的数值无法在证据清单中核对（清单缺少事实文本，"
                        "可能是旧版 Trace）；不得默认通过："
                        + "、".join(sorted(set(unsupported))[:10]),
                        details={"unsupported_numbers": sorted(set(unsupported))[:20]},
                        requires_human_review=True,
                    )
                )

        # 统计口径
        checked_claims = 0
        for claim in _stat_claims(answer):
            actual = evidence.stat_value(claim["sensor"], claim["stat"])
            if actual is None:
                continue
            checked_claims += 1
            if not _close(
                actual,
                claim["value"],
                rel=ctx.config.numeric_rel_tolerance,
                abs_=ctx.config.numeric_abs_tolerance,
            ):
                other = {
                    key: evidence.stat_value(claim["sensor"], key)
                    for key in ("min", "max", "mean", "std")
                }
                matched_key = next(
                    (
                        key
                        for key, value in other.items()
                        if isinstance(value, (int, float))
                        and _close(
                            float(value),
                            claim["value"],
                            rel=ctx.config.numeric_rel_tolerance,
                            abs_=ctx.config.numeric_abs_tolerance,
                        )
                    ),
                    None,
                )
                detail = (
                    f"；该数值实际对应 {matched_key}，疑似统计口径写错" if matched_key else ""
                )
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{claim['sensor']} 的 {claim['stat']} 证据值为 {actual}，"
                        f"回答写成 {claim['token']}{detail}",
                        related_event_ids=[ctx.answer_event_id] if ctx.answer_event_id else [],
                        details={
                            "sensor": claim["sensor"],
                            "statistic": claim["stat"],
                            "evidence_value": actual,
                            "claimed_value": claim["value"],
                            "matched_statistic": matched_key,
                            "sentence": claim["sentence"],
                        },
                    )
                )

        # 单位
        for sentence in _sentences(answer):
            sensors = _sensor_mentions(sentence)
            if len(sensors) != 1:
                continue
            expected = SENSOR_UNITS.get(sensors[0])
            if not expected:
                continue
            stat_values = [
                evidence.stat_value(sensors[0], key)
                for key in ("min", "max", "mean", "std")
            ]
            for unit in KNOWN_UNITS:
                if unit in expected:
                    continue
                for match in re.finditer(rf"(-?\d+(?:\.\d+)?)\s*{re.escape(unit)}", sentence):
                    value = float(match.group(1))
                    # 只有当一个“看起来是传感器统计量”的数字被贴上错误单位时才报错；
                    # 百分比变化等其它数量不在此列。
                    if not any(
                        isinstance(candidate, (int, float))
                        and _close(
                            float(candidate),
                            value,
                            rel=ctx.config.numeric_rel_tolerance,
                            abs_=ctx.config.numeric_abs_tolerance,
                        )
                        for candidate in stat_values
                    ):
                        continue
                    results.append(
                        make_result(
                            self.spec,
                            Verdict.FAIL,
                            f"{sensors[0]} 的证据单位应为 {'/'.join(expected)}，"
                            f"回答把 {match.group(1)} 的单位写成了 {unit}",
                            severity=Severity.MEDIUM,
                            related_event_ids=[ctx.answer_event_id] if ctx.answer_event_id else [],
                            details={"sensor": sensors[0], "unit": unit, "value": value,
                                     "expected": list(expected), "sentence": sentence[:300]},
                        )
                    )
                    break

        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"{len(tokens)} 个数值均可在证据中找到"
                    + (f"；{checked_claims} 条统计口径断言与证据一致" if checked_claims else ""),
                    details={"numbers": len(tokens), "stat_claims_checked": checked_claims},
                )
            )
        return results


# --------------------------------------------------------------------------- #
# R8 知识引用
# --------------------------------------------------------------------------- #
class KnowledgeCitationRule:
    spec = R8_CITATION

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        evidence = ctx.evidence
        knowledge_calls = [
            event for event in ctx.trace.tool_calls()
            if event.tool_name == "search_technical_knowledge"
        ]
        answer = evidence.answer_text or ""
        answer_labels = sorted(set(re.findall(r"\[(K\d{1,2})\]", answer)))
        citations = [item for item in evidence.citations if item.get("kind") == "knowledge"]

        if not evidence.has_manifest:
            if knowledge_calls or answer_labels:
                return [
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "Trace 缺少证据清单，无法核对 Chunk/页码/逐字摘录（不得默认通过）",
                        requires_human_review=True,
                    )
                ]
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有知识引用")]

        if (
            not citations
            and not knowledge_calls
            and not answer_labels
            and not evidence.injection_flags
        ):
            return [make_result(self.spec, Verdict.NOT_APPLICABLE, "本次执行没有知识检索与引用")]

        results: list[RuleResult] = []
        knowledge_locator = list(ctx.tool_result_event_ids("search_technical_knowledge"))
        if ctx.answer_event_id:
            knowledge_locator.append(ctx.answer_event_id)
        citation_labels = {str(item.get("label")) for item in citations}
        verified = {
            item.get("chunk_id"): item
            for item in evidence.knowledge_verification
            if isinstance(item, dict)
        }

        for item in citations:
            label = str(item.get("label"))
            chunk_id = item.get("chunk_id")
            quote = str(item.get("quote") or "")
            problems: list[str] = []
            severity = Severity.MEDIUM
            if chunk_id is None:
                problems.append("缺少 chunk_id")
                severity = Severity.HIGH
            if not item.get("doc_key"):
                problems.append("缺少 doc_key")
            page_start = item.get("page_start")
            page_end = item.get("page_end") or page_start
            if not isinstance(page_start, int) or page_start <= 0:
                problems.append(f"页码不合理：{page_start!r}")
            elif isinstance(page_end, int) and page_end < page_start:
                problems.append(f"页码区间倒置：{page_start}-{page_end}")
            if len(quote) < 10:
                problems.append("逐字摘录过短（<10 字符）")
            scope = item.get("equipment_scope")
            if scope and scope not in ctx.config.allowed_knowledge_scopes:
                problems.append(f"文档适用范围 {scope!r} 不在允许范围")
            # 逐字摘录必须真的出现在对应 Chunk 片段中（离线可核对，无需外部读取）。
            # 这一步只证明“引用文本存在”，不证明“引用支持结论”（后者交给 Judge）。
            excerpt_by_chunk = {
                entry.get("chunk_id"): str(entry.get("excerpt") or "")
                for entry in evidence.knowledge
            }
            excerpt = excerpt_by_chunk.get(chunk_id) if isinstance(chunk_id, int) else None
            if quote and excerpt:
                from app.rag.answer import normalize_for_quote_match

                normalized_quote = normalize_for_quote_match(quote.rstrip("…"))
                normalized_excerpt = normalize_for_quote_match(excerpt.rstrip("…"))
                if normalized_quote and normalized_quote not in normalized_excerpt:
                    problems.append("逐字摘录未出现在对应 Chunk 片段中")
                    severity = Severity.HIGH
            recorded_hash = item.get("quote_sha256")
            if recorded_hash:
                import hashlib

                actual = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                if actual != recorded_hash:
                    problems.append("摘录哈希与记录不一致（可能被篡改）")
                    severity = Severity.HIGH
            verification = verified.get(chunk_id) if isinstance(chunk_id, int) else None
            if verification and verification.get("ok"):
                if not verification.get("excerpt_verbatim", True):
                    problems.append("摘录未逐字出现在该 Chunk 原文中")
                    severity = Severity.HIGH
                if not verification.get("doc_key_matches", True):
                    problems.append("Chunk 与 doc_key 不匹配")
            if problems:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"知识引用 [{label}] 存在问题：" + "；".join(problems),
                        evidence_refs=[str(label)],
                        related_event_ids=knowledge_locator,
                        severity=severity,
                        details={"citation": item, "problems": problems},
                    )
                )

        missing = [label for label in answer_labels if label not in citation_labels]
        if missing:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"回答引用了不存在的知识标签：{missing}",
                    evidence_refs=missing,
                    related_event_ids=knowledge_locator,
                    details={"missing_citations": missing},
                )
            )
        knowledge_points = evidence.answer_sections.get("knowledge") or []
        if knowledge_points and not citations:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    "回答给出知识结论，但证据清单中没有任何知识引用（无 Chunk 可核对）",
                    related_event_ids=knowledge_locator,
                    details={"knowledge_points": len(knowledge_points)},
                )
            )
        if knowledge_calls and not citations and not answer_labels and not knowledge_points:
            if evidence.knowledge:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.UNKNOWN,
                        "知识检索返回了片段，但回答没有任何引用或知识结论，"
                        "无法判断是合理拒答还是遗漏证据",
                        related_event_ids=knowledge_locator,
                        requires_human_review=True,
                        details={"retrieved_chunks": len(evidence.knowledge)},
                    )
                )
            else:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.PASS,
                        "知识检索无结果（空检索），回答未引用文档符合预期",
                    )
                )

        # 文档中的指令性文本是否被当成系统指令执行
        if evidence.injection_flags:
            lowered = answer.lower()
            if any(keyword.lower() in lowered for keyword in INSTRUCTION_ACCEPT_KEYWORDS):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        "检索片段含疑似指令注入文本，最终回答出现了接受/复述该指令的内容",
                        severity=Severity.CRITICAL,
                        related_event_ids=knowledge_locator,
                        requires_human_review=True,
                        details={"injection_flags": evidence.injection_flags[:5]},
                    )
                )

        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"{len(citations)} 条知识引用结构完整"
                    + ("，且原文核对通过" if verified else "（未做外部 Chunk 核对）"),
                    details={"citations": len(citations),
                             "external_verification": bool(verified)},
                )
            )
        return results


# --------------------------------------------------------------------------- #
# R9 执行链
# --------------------------------------------------------------------------- #
class ChainIntegrityRule:
    spec = R9_CHAIN

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        from app.audit.normalizer import pair_tool_events

        trace = ctx.trace
        if not trace.events:
            return [
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    "Trace 为空，无法审计",
                    severity=Severity.CRITICAL,
                )
            ]
        results: list[RuleResult] = []
        problem_severity = {
            "missing_run_start": Severity.HIGH,
            "missing_run_end": Severity.HIGH,
            "duplicate_events": Severity.LOW,
            "out_of_order_events": Severity.LOW,
            "sequence_collision": Severity.MEDIUM,
            "missing_sequence": Severity.LOW,
            "trace_id_mismatch": Severity.MEDIUM,
            "empty_trace": Severity.CRITICAL,
            "event_limit_exceeded": Severity.LOW,
            "invalid_json_line": Severity.MEDIUM,
            "unknown_manifest_version": Severity.LOW,
            "manifest_build_error": Severity.MEDIUM,
        }
        for problem in trace.problems:
            code = str(problem.get("code"))
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"结构问题 {code}：{problem.get('message') or ''}".strip(),
                    severity=problem_severity.get(code, Severity.MEDIUM),
                    details=problem,
                )
            )

        run_end = next(
            (event for event in reversed(trace.events) if event.event_type == "run_end"), None
        )
        answer_event = next(
            (event for event in reversed(trace.events) if event.event_type == "answer"), None
        )
        if run_end and answer_event and answer_event.sequence_number > run_end.sequence_number:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    "answer 事件出现在 run_end 之后，执行链顺序不合理",
                    related_event_ids=[answer_event.event_id, run_end.event_id],
                )
            )
        if run_end and trace.events and trace.events[-1].event_id != run_end.event_id:
            after = [event.event_id for event in trace.events if event.sequence_number > run_end.sequence_number]
            if after:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"run_end 之后仍有 {len(after)} 条事件：{after[:5]}",
                        related_event_ids=after,
                    )
                )

        pairing = pair_tool_events(trace)
        for call in pairing["unpaired_calls"]:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"tool_call {call.tool_name} 没有对应的 tool_result",
                    related_event_ids=[call.event_id],
                )
            )
        for result in pairing["unpaired_results"]:
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"tool_result {result.tool_name} 没有对应的 tool_call",
                    related_event_ids=[result.event_id],
                    severity=Severity.MEDIUM,
                )
            )

        if not answer_event and trace.execution_status != "failed":
            if trace.evidence_manifest is not None or trace.execution_status == "completed":
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        "Trace 已产生最终结果（证据/终态），但缺少 answer 事件",
                    )
                )
        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"执行链完整：{len(trace.events)} 条事件，tool_call/tool_result 全部配对",
                    details={
                        "events": len(trace.events),
                        "tool_pairs": len(pairing["pairs"]),
                        "trace_schema": trace.trace_schema_version,
                    },
                )
            )
        return results


# --------------------------------------------------------------------------- #
# R10 状态一致性
# --------------------------------------------------------------------------- #
class StatusConsistencyRule:
    spec = R10_STATUS

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]:
        trace = ctx.trace
        evidence = ctx.evidence
        run_end = next(
            (event for event in reversed(trace.events) if event.event_type == "run_end"), None
        )
        answer_event = next(
            (event for event in reversed(trace.events) if event.event_type == "answer"), None
        )
        if run_end is None:
            return [
                make_result(
                    self.spec,
                    Verdict.UNKNOWN,
                    "Trace 缺少 run_end，无法核对最终状态与结论的一致性",
                    requires_human_review=True,
                )
            ]

        answer = evidence.answer_text or ""
        answer_status = (
            evidence.answer_status
            or (answer_event.status if answer_event else None)
            or ""
        )
        results: list[RuleResult] = []
        status = str(run_end.status or "")

        errors = [
            event for event in trace.tool_results()
            if event.status == "error" or event.error_type is not None
        ]
        if errors and answer:
            acknowledged = any(keyword in answer for keyword in FAILURE_ACK_KEYWORDS)
            claimed_success = any(keyword in answer for keyword in SUCCESS_CLAIM_KEYWORDS)
            if claimed_success or not acknowledged:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"{len(errors)} 次工具执行失败，但最终回答未说明失败"
                        + ("，反而使用了成功式表述" if claimed_success else ""),
                        related_event_ids=[event.event_id for event in errors],
                        details={"failed_tools": [event.tool_name for event in errors],
                                 "claimed_success": claimed_success},
                    )
                )

        if status in ("timeout", "budget_exhausted"):
            acknowledged = any(keyword in answer for keyword in INCOMPLETE_KEYWORDS)
            if not acknowledged:
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        f"执行状态为 {status}（证据不完整），但回答没有说明预算/超时限制",
                        related_event_ids=[run_end.event_id],
                        details={"status": status},
                    )
                )
        elif status == "failed":
            if answer and answer_status not in ("failed", "refused") and not any(
                keyword in answer for keyword in FAILURE_ACK_KEYWORDS
            ):
                results.append(
                    make_result(
                        self.spec,
                        Verdict.FAIL,
                        "执行状态为 failed，但回答给出了未经说明的确定结论",
                        related_event_ids=[run_end.event_id],
                        severity=Severity.CRITICAL,
                    )
                )
        elif status == "completed" and answer_status in ("refused", "insufficient_evidence"):
            results.append(
                make_result(
                    self.spec,
                    Verdict.FAIL,
                    f"run_end 状态为 completed，但回答状态是 {answer_status}",
                    related_event_ids=[run_end.event_id],
                    severity=Severity.MEDIUM,
                    details={"run_status": status, "answer_status": answer_status},
                )
            )

        if not results:
            results.append(
                make_result(
                    self.spec,
                    Verdict.PASS,
                    f"执行状态 {status!r} 与回答状态 {answer_status or 'unknown'!r} 一致"
                    + (f"；{len(errors)} 次工具失败已在回答中说明" if errors else ""),
                    details={"run_status": status, "answer_status": answer_status,
                             "tool_errors": len(errors)},
                )
            )
        return results


RULES = (
    ToolWhitelistRule(),
    ToolParameterRule(),
    BudgetRule(),
    RepeatedCallRule(),
    TimeRangeRule(),
    EquipmentRule(),
    DataQualityRule(),
    NumericEvidenceRule(),
    KnowledgeCitationRule(),
    ChainIntegrityRule(),
    StatusConsistencyRule(),
)

RULE_SPECS = ALL_RULE_SPECS


def run_deterministic_rules(ctx: RuleContext) -> list[RuleResult]:
    """按固定顺序执行全部确定性规则，返回逐条结果（含 PASS/UNKNOWN）。"""
    results: list[RuleResult] = []
    for rule in RULES:
        try:
            outcome = rule.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 - 规则内部错误不能变成“通过”
            outcome = [
                make_result(
                    rule.spec,
                    Verdict.UNKNOWN,
                    f"规则执行异常（{type(exc).__name__}），无法判断",
                    requires_human_review=True,
                )
            ]
        results.extend(outcome)
    return results


def summarize_by_rule(results: list[RuleResult]) -> list[RuleResult]:
    """把逐条结果按 rule_id 汇总（同一规则多条违规 → 一个 FAIL 汇总项）。"""
    by_rule: dict[str, list[RuleResult]] = {}
    for item in results:
        by_rule.setdefault(item.rule_id, []).append(item)
    specs = {spec.rule_id: spec for spec in RULE_SPECS}
    summary: list[RuleResult] = []
    for rule_id, spec in specs.items():
        if rule_id not in by_rule:
            continue
        summary.append(summarize_results(spec, by_rule[rule_id]))
    return summary


__all__ = [
    "ALL_RULE_SPECS",
    "RULES",
    "RULE_SPECS",
    "run_deterministic_rules",
    "summarize_by_rule",
]
