"""证据整合：把工具返回的原始结果变成**带标签的事实清单**与数值接地校验。

为什么需要这一层：
1. 回答里的每个数值都必须能追溯到某次真实工具调用（不允许模型自己算或猜）；
2. 知识结论必须绑定到具体 Chunk（文档 ID + 页码 + 逐字摘录），仅“文档存在”不算引用正确
   （Phase 2 的 69.2% 引用完全正确率就是这类问题的直接教训）；
3. 数值接地校验用**精确数值匹配**判断回答中的数字是否来自事实清单，
   而不是用另一个模型再判断一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.intent import SENSOR_LABELS_ZH

SCOPE_LABELS = {
    "metropt3_direct": "MetroPT-3 直接关联",
    "generic_compressor": "通用压缩机资料（非本设备专属）",
    "model_specific_other": "其它型号专属资料",
    "unsupported_scope": "适用范围未知",
    "unknown": "适用范围未知",
}

# 数字切分：不把设备编号里的数字当成“回答中的数值”
_NUMBER_RE = re.compile(r"(?<![\w.\-])(-?\d+(?:\.\d+)?)(?![\w.])")
_LABEL_RE = re.compile(r"\[([DKG]\d{1,2})\]")
_URL_RE = re.compile(r"https?://\S+")


def fmt_num(value: float | int | None, digits: int = 4) -> str:
    """稳定格式化（去尾零，整数保留整数形式）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    number = float(value)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    text = f"{round(number, digits):.{digits}f}"
    return text.rstrip("0").rstrip(".")


def fmt_ratio(value: float | None) -> str:
    return "null" if value is None else fmt_num(value, 4)


@dataclass
class FactItem:
    """一条带标签的数据事实（文本中的数值即工具返回值）。"""

    label: str
    kind: str
    text: str
    numbers: tuple[float, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
        }


@dataclass
class KnowledgeItem:
    """一条知识库证据（绑定 Chunk 与逐字摘录）。"""

    label: str
    chunk: dict[str, Any]
    quote: str

    @property
    def content(self) -> str:
        return str(self.chunk.get("excerpt") or self.quote)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": "knowledge",
            "chunk_id": self.chunk.get("chunk_id"),
            "document_id": self.chunk.get("document_id"),
            "doc_key": self.chunk.get("doc_key"),
            "document_title": self.chunk.get("document_title"),
            "page_start": self.chunk.get("page_start"),
            "page_end": self.chunk.get("page_end"),
            "section_title": self.chunk.get("section_title"),
            "section_path": self.chunk.get("section_path"),
            "equipment_scope": self.chunk.get("equipment_scope"),
            "scope_label": SCOPE_LABELS.get(
                str(self.chunk.get("equipment_scope")), str(self.chunk.get("equipment_scope"))
            ),
            "doc_type": self.chunk.get("doc_type"),
            "trust_level": self.chunk.get("trust_level"),
            "source_url": self.chunk.get("source_url"),
            "license": self.chunk.get("license"),
            "chunking_version": self.chunk.get("chunking_version"),
            "citation_label": self.chunk.get("citation_label"),
            "score": self.chunk.get("score"),
            "vector_similarity": self.chunk.get("vector_similarity"),
            "keyword_score": self.chunk.get("keyword_score"),
            "rrf_score": self.chunk.get("rrf_score"),
            "matched_terms": self.chunk.get("matched_terms") or [],
            "quote": self.quote,
        }


@dataclass
class FactSheet:
    """一次运行的全部可用证据。"""

    facts: list[FactItem] = field(default_factory=list)
    knowledge: list[KnowledgeItem] = field(default_factory=list)
    equipment: dict[str, Any] | None = None
    window: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    quality: dict[str, Any] | None = None
    faults: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    allowed_numbers: set[float] = field(default_factory=set)
    injection_flags: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def has_data(self) -> bool:
        return bool(self.facts)

    @property
    def has_knowledge(self) -> bool:
        return bool(self.knowledge)

    def labels(self) -> list[str]:
        return [item.label for item in self.facts] + [item.label for item in self.knowledge]

    def facts_text(self) -> str:
        return "\n".join(f"[{item.label}] {item.text}" for item in self.facts)

    def knowledge_text(self) -> str:
        blocks: list[str] = []
        for item in self.knowledge:
            chunk = item.chunk
            pages = (
                f"p{chunk.get('page_start')}"
                if chunk.get("page_start") == chunk.get("page_end")
                else f"p{chunk.get('page_start')}-{chunk.get('page_end')}"
            )
            blocks.append(
                f"[{item.label}] 文档={chunk.get('document_title')} | 文档键={chunk.get('doc_key')} "
                f"| 文档ID={chunk.get('document_id')} | ChunkID={chunk.get('chunk_id')} | 页码={pages} "
                f"| 章节={chunk.get('section_title') or '未识别'} "
                f"| 适用范围={SCOPE_LABELS.get(str(chunk.get('equipment_scope')), '未知')} "
                f"| 可信度={chunk.get('trust_level')}\n<evidence>\n{item.content}\n</evidence>"
            )
        return "\n\n".join(blocks)

    def evidence_payload(self) -> list[dict[str, Any]]:
        """API 响应中的 `evidence` 列表（数据事实 + 知识片段）。"""
        payload: list[dict[str, Any]] = []
        for item in self.facts:
            payload.append(
                {
                    "label": item.label,
                    "kind": "data",
                    "fact_type": item.kind,
                    "text": item.text,
                    "source": item.source,
                }
            )
        for item in self.knowledge:
            payload.append(item.as_dict())
        return payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "equipment": self.equipment,
            "window": self.window,
            "baseline_window": self.baseline,
            "quality": self.quality,
            "fault_events": self.faults,
            "history": self.history,
            "facts": [item.as_dict() for item in self.facts],
            "knowledge": [item.as_dict() for item in self.knowledge],
            "warnings": self.warnings,
            "injection_flags": self.injection_flags,
        }


# --------------------------------------------------------------------------- #
# 构建事实清单
# --------------------------------------------------------------------------- #
_DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in_evidence_text(text: str) -> tuple[float, ...]:
    """证据文本中出现的数字片段（引用标签与 URL 不计）。

    这里比“违规检测”宽松：证据文本由本系统按真实数值生成，其中的时间戳
    （如 `2020-07-15T14:30:00`）每个分量都是真实证据的一部分，因此把全部数字
    片段（含被 `T`/`:`/`-` 分隔的分量）都登记为“可引用数值”，
    避免模型如实引用 14:30 却被判成编造。违规检测仍用更严格的 `_NUMBER_RE`。
    """
    cleaned = _URL_RE.sub(" ", _LABEL_RE.sub(" ", str(text)))
    values: list[float] = []
    for match in _DIGIT_RUN_RE.finditer(cleaned):
        try:
            values.append(float(match.group(0)))
        except ValueError:  # pragma: no cover
            continue
    return tuple(values)


def _add(fact_sheet: FactSheet, kind: str, text: str, numbers: tuple[float, ...], source: dict) -> FactItem:
    label = f"D{len(fact_sheet.facts) + 1}"
    item = FactItem(label=label, kind=kind, text=text, numbers=numbers, source=source)
    fact_sheet.facts.append(item)
    for number in numbers:
        if number is not None:
            fact_sheet.allowed_numbers.add(float(number))
    # 事实文本里出现的数值（例如覆盖率阈值 0.95、标称步长 10）同样是证据支持的数字
    for number in _numbers_in_evidence_text(text):
        fact_sheet.allowed_numbers.add(number)
    return item


def _source_of(observation: dict[str, Any]) -> dict[str, Any]:
    data = observation.get("data") or {}
    source = data.get("source") or {}
    payload = {
        "tool": observation.get("tool"),
        "service": source.get("service"),
        "table": source.get("table") or source.get("engine"),
        "window": data.get("window"),
        "read_only": True,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _equipment_line(equipment: dict[str, Any]) -> str:
    return (
        f"设备 {equipment.get('equipment_code')}「{equipment.get('equipment_name')}」"
        f"（类型 {equipment.get('equipment_type')}）；数据集观测范围 "
        f"{equipment.get('first_observed_at')} 至 {equipment.get('last_observed_at')}。"
    )


def _stats_facts(fact_sheet: FactSheet, data: dict[str, Any], source: dict[str, Any]) -> None:
    window = data.get("window") or {}
    _add(
        fact_sheet,
        "statistics_summary",
        (
            f"窗口 [{window.get('start')}, {window.get('end')}) 内共 {data.get('row_count')} 行观测"
            f"（标称采样步长 {fmt_num(data.get('nominal_step_seconds'))} 秒；"
            f"exclude_frozen={str(bool(data.get('exclude_frozen'))).lower()}）。"
        ),
        (float(data.get("row_count") or 0), float(data.get("nominal_step_seconds") or 0)),
        source,
    )
    for item in data.get("analog") or []:
        sensor = item.get("sensor")
        label = SENSOR_LABELS_ZH.get(sensor, sensor)
        if not item.get("count"):
            _add(
                fact_sheet,
                "statistics_analog",
                f"{label} 在窗口内没有任何样本（count=0），无法给出该测点的统计量。",
                (0.0,),
                source,
            )
            continue
        numbers = tuple(
            float(value)
            for value in (
                item.get("count") or 0,
                item.get("min"),
                item.get("max"),
                item.get("mean"),
                item.get("std"),
            )
            if value is not None
        )
        parts = [
            f"{label} 在窗口内 {item.get('count')} 个样本",
            f"最小 {fmt_num(item.get('min'))}",
            f"最大 {fmt_num(item.get('max'))}",
            f"均值 {fmt_num(item.get('mean'))}",
            f"样本标准差 {fmt_num(item.get('std'))}（stddev_samp，ddof=1）",
            f"首末样本时间 {item.get('first_timestamp')} / {item.get('last_timestamp')}",
        ]
        _add(fact_sheet, "statistics_analog", "，".join(parts) + "。", numbers, source)
    for item in data.get("digital") or []:
        sensor = item.get("sensor")
        label = SENSOR_LABELS_ZH.get(sensor, sensor)
        numbers = tuple(
            float(value)
            for value in (
                item.get("count") or 0,
                item.get("count_0") or 0,
                item.get("count_1") or 0,
                item.get("ratio_1"),
                item.get("transitions") or 0,
            )
            if value is not None
        )
        _add(
            fact_sheet,
            "statistics_digital",
            (
                f"{label} 在窗口内 {item.get('count')} 个样本：0 值 {item.get('count_0')} 次，"
                f"1 值 {item.get('count_1')} 次（1 值占比 {fmt_ratio(item.get('ratio_1'))}），"
                f"相邻行状态切换 {item.get('transitions')} 次。"
            ),
            numbers,
            source,
        )


def _baseline_facts(
    fact_sheet: FactSheet,
    target: dict[str, Any],
    baseline: dict[str, Any],
    source: dict[str, Any],
) -> None:
    base_window = baseline.get("window") or {}
    target_window = target.get("window") or {}
    _add(
        fact_sheet,
        "baseline",
        (
            f"对照窗口 [{base_window.get('start')}, {base_window.get('end')}) 共 "
            f"{baseline.get('row_count')} 行观测；目标窗口共 {target.get('row_count')} 行。"
            "对照窗口取目标窗口之前等长的紧邻窗口，用于描述性比较，不是判定标准。"
        ),
        (float(baseline.get("row_count") or 0), float(target.get("row_count") or 0)),
        source,
    )
    baseline_by_sensor = {item.get("sensor"): item for item in baseline.get("analog") or []}
    for item in target.get("analog") or []:
        other = baseline_by_sensor.get(item.get("sensor"))
        if not other or item.get("mean") is None or other.get("mean") is None:
            continue
        sensor = item.get("sensor")
        label = SENSOR_LABELS_ZH.get(sensor, sensor)
        mean_now, mean_before = float(item["mean"]), float(other["mean"])
        max_now, max_before = item.get("max"), other.get("max")
        change = None
        if mean_before != 0:
            change = (mean_now - mean_before) / abs(mean_before) * 100
        text = (
            f"{label} 均值：对照窗口 {fmt_num(mean_before)} → 目标窗口 {fmt_num(mean_now)}"
            + (f"（变化 {fmt_num(change, 2)}%）" if change is not None else "（对照窗口均值为 0，不计算百分比）")
            + f"；最大值：{fmt_num(max_before)} → {fmt_num(max_now)}；"
            f"样本数：{other.get('count')} → {item.get('count')}。"
        )
        numbers = [mean_now, mean_before]
        if change is not None:
            numbers.append(round(change, 4))
        for value in (max_now, max_before):
            if value is not None:
                numbers.append(float(value))
        _add(fact_sheet, "baseline", text, tuple(numbers), source)


def _quality_facts(fact_sheet: FactSheet, data: dict[str, Any], source: dict[str, Any]) -> None:
    window = data.get("window") or {}
    numbers = tuple(
        float(value)
        for value in (
            data.get("actual_samples") or 0,
            data.get("expected_samples") or 0,
            data.get("sample_coverage"),
            data.get("time_coverage"),
            data.get("gap_count") or 0,
            data.get("gap_duration_seconds") or 0,
            data.get("max_gap_seconds") or 0,
            data.get("missing_samples_estimate") or 0,
            data.get("frozen_candidate_count") or 0,
            data.get("frozen_candidate_ratio"),
            data.get("records_with_quality_flags") or 0,
            data.get("nominal_step_seconds") or 0,
        )
        if value is not None
    )
    def _with_percent(value: float | None) -> str:
        """同时给出小数与百分比两种写法，避免模型换算百分比被判成编造数值。"""
        if value is None:
            return "null"
        return f"{fmt_ratio(value)}（{fmt_num(float(value) * 100, 3)}%）"

    _add(
        fact_sheet,
        "quality",
        (
            f"数据质量（窗口 [{window.get('start')}, {window.get('end')})）：实际样本 "
            f"{data.get('actual_samples')} / 应有 {data.get('expected_samples')}"
            f"（样本覆盖率 {_with_percent(data.get('sample_coverage'))}），"
            f"时间覆盖率 {_with_percent(data.get('time_coverage'))}；"
            f"采样缺口 {data.get('gap_count')} 处，合计 {fmt_num(data.get('gap_duration_seconds'))} 秒"
            f"（最长 {fmt_num(data.get('max_gap_seconds'))} 秒，估计缺失样本 "
            f"{data.get('missing_samples_estimate')}）；"
            f"冻结候选 {data.get('frozen_candidate_count')} 行"
            f"（占比 {_with_percent(data.get('frozen_candidate_ratio'))}）；"
            f"带质量位记录 {data.get('records_with_quality_flags')} 行；"
            f"窗口是否完全落在观测范围内：{data.get('window_inside_data_range')}。"
        ),
        numbers
        + tuple(
            round(float(value) * 100, 4)
            for value in (
                data.get("sample_coverage"),
                data.get("time_coverage"),
                data.get("frozen_candidate_ratio"),
            )
            if value is not None
        ),
        source,
    )
    for warning in data.get("warnings") or []:
        _add(
            fact_sheet,
            "quality_warning",
            f"数据质量提示（{warning.get('code')}，{warning.get('severity')}）：{warning.get('message')}",
            (),
            source,
        )


def _fault_facts(fact_sheet: FactSheet, data: dict[str, Any], source: dict[str, Any]) -> None:
    total = data.get("total") or 0
    if not total:
        _add(
            fact_sheet,
            "fault",
            "与该时间窗相交的官方故障事件为 0 条（这只能说明该数据集未登记故障事件，"
            "不能反证设备无故障）。",
            (0.0,),
            source,
        )
        return
    for event in data.get("events") or []:
        numbers = tuple(
            float(value)
            for value in (event.get("duration_seconds") or 0, event.get("overlap_seconds") or 0)
            if value is not None
        )
        _add(
            fact_sheet,
            "fault",
            (
                f"官方故障事件 {event.get('event_code')}（PDF 标签 {event.get('pdf_label')}）："
                f"{event.get('start_time')} 至 {event.get('end_time')}，时长 "
                f"{fmt_num(event.get('duration_seconds'))} 秒，与查询窗口重叠 "
                f"{fmt_num(event.get('overlap_seconds'))} 秒；失败类型 {event.get('failure_type')}，"
                f"严重程度 {event.get('severity')}，故障描述「{event.get('fault_description')}」，"
                f"维护说明「{event.get('maintenance_note')}」，核验状态 {event.get('verification_status')}，"
                f"来源 {event.get('source_reference')}。"
            ),
            numbers,
            source,
        )
    _add(
        fact_sheet,
        "fault_semantics",
        str(data.get("sample_label_semantics") or "故障事件为区间级事实，不等同于逐行故障标签。"),
        (),
        source,
    )


def _history_facts(fact_sheet: FactSheet, data: dict[str, Any], source: dict[str, Any]) -> None:
    rows = data.get("rows") or []
    if not rows:
        return
    sensors = [str(name) for name in (data.get("sensors") or [])]
    first, last = rows[0], rows[-1]
    first_values = first.get("values") or {}
    rendered = "，".join(
        f"{name}={fmt_num(first_values.get(name))}"
        for name in sensors
        if isinstance(first_values.get(name), (int, float))
        and not isinstance(first_values.get(name), bool)
    )
    numbers = tuple(
        float(first_values[name])
        for name in sensors
        if isinstance(first_values.get(name), (int, float))
        and not isinstance(first_values.get(name), bool)
    )
    _add(
        fact_sheet,
        "history",
        (
            f"原始观测：窗口内共 {data.get('total')} 行，本次返回 {data.get('returned')} 行"
            f"（时间 {first.get('observed_at')} 至 {last.get('observed_at')}）；"
            f"首行取值：{rendered or '无可用数值'}。"
        ),
        numbers,
        source,
    )


def build_fact_sheet(
    observations: list[dict[str, Any]],
    *,
    max_knowledge_chunks: int = 5,
    max_baseline_compare: int = 6,
) -> FactSheet:
    """从工具观测（`ToolOutcome.to_dict()`）构建事实清单。"""
    sheet = FactSheet(observations=observations)
    target_stats: tuple[dict[str, Any], dict[str, Any]] | None = None
    quality_observation: dict[str, Any] | None = None

    # 按**执行顺序**遍历观测：标签 D1/D2/... 与工具调用顺序一致，便于对照 Trace
    for observation in observations:
        if observation.get("status") == "error":
            continue
        data = observation.get("data") or {}
        source = _source_of(observation)
        tool = observation.get("tool")
        if tool == "get_equipment_info" and data.get("equipment"):
            sheet.equipment = data["equipment"]
            _add(sheet, "equipment", _equipment_line(data["equipment"]), (), source)
        elif tool == "get_sensor_statistics":
            if target_stats is None:
                target_stats = (data, source)
                sheet.window = data.get("window")
                _stats_facts(sheet, data, source)
            else:
                sheet.baseline = data.get("window")
                _baseline_facts(sheet, target_stats[0], data, source)
        elif tool == "get_data_quality":
            sheet.quality = data
            quality_observation = observation
            if sheet.window is None:
                sheet.window = data.get("window")
            _quality_facts(sheet, data, source)
        elif tool == "get_fault_events":
            sheet.faults.extend(data.get("events") or [])
            if sheet.window is None:
                sheet.window = data.get("window")
            _fault_facts(sheet, data, source)
        elif tool == "get_sensor_history":
            sheet.history = data
            _history_facts(sheet, data, source)
        elif tool == "search_technical_knowledge":
            from app.rag.ingest import detect_injection_flags

            for chunk in data.get("chunks") or []:
                if len(sheet.knowledge) >= max_knowledge_chunks:
                    break
                label = f"K{len(sheet.knowledge) + 1}"
                excerpt = str(chunk.get("excerpt") or "")
                flags = detect_injection_flags(excerpt)
                if flags:
                    sheet.injection_flags.extend(
                        f"{label}: {flag}" for flag in flags[:3]
                    )
                sheet.knowledge.append(
                    KnowledgeItem(label=label, chunk=chunk, quote=excerpt)
                )
        else:  # pragma: no cover - 注册表外工具不会出现
            sheet.warnings.append(f"未识别工具 {tool} 的观测，已忽略其事实抽取")

    # 时间窗口的年/月/日/时分也是“回答里可以出现的数字”
    date_values: list[Any] = []
    for window in (sheet.window, sheet.baseline):
        if not window:
            continue
        for key in ("start", "end"):
            value = window.get(key)
            if not value:
                continue
            date_values.append(value)
            for token in re.findall(r"\d+", str(value)):
                sheet.allowed_numbers.add(float(token))
        duration = window.get("duration_seconds")
        if duration:
            sheet.allowed_numbers.add(float(duration))
            sheet.allowed_numbers.add(round(float(duration) / 3600, 4))
    if sheet.equipment:
        # 设备档案中的观测起止时间也会被回答引用
        for value in (
            sheet.equipment.get("first_observed_at"),
            sheet.equipment.get("last_observed_at"),
        ):
            if value:
                for token in re.findall(r"\d+", str(value)):
                    sheet.allowed_numbers.add(float(token))
    if sheet.quality:
        first = sheet.quality.get("data_first_observed_at")
        last = sheet.quality.get("data_last_observed_at")
        for value in (first, last):
            if value:
                for token in re.findall(r"\d+", str(value)):
                    sheet.allowed_numbers.add(float(token))

    if quality_observation is not None:
        warnings = (quality_observation.get("data") or {}).get("warnings") or []
        for warning in warnings:
            if warning.get("severity") == "warning":
                sheet.warnings.append(f"{warning.get('code')}: {warning.get('message')}")

    for observation in observations:
        if observation.get("status") == "empty":
            sheet.warnings.append(
                f"工具 {observation.get('tool')} 在给定条件下没有返回任何数据（空结果，不是执行失败）"
            )
        if observation.get("status") == "error":
            error = observation.get("error") or {}
            sheet.warnings.append(
                f"工具 {observation.get('tool')} 执行失败（{error.get('category')}/{error.get('code')}）："
                f"{error.get('message')}"
            )
    return sheet


# --------------------------------------------------------------------------- #
# 数值接地校验
# --------------------------------------------------------------------------- #
def remove_labels(text: str) -> str:
    return _LABEL_RE.sub(" ", text)


def extract_numbers(text: str) -> list[float]:
    """抽取回答中的数值（先剔除引用标签与 URL，避免把 K1/p12 当数值）。"""
    cleaned = _URL_RE.sub(" ", remove_labels(text))
    values: list[float] = []
    for match in _NUMBER_RE.finditer(cleaned):
        token = match.group(1)
        try:
            values.append(float(token))
        except ValueError:  # pragma: no cover
            continue
    return values


def _decimals(token: str) -> int:
    return len(token.split(".", 1)[1]) if "." in token else 0


def _matches_allowed(token: str, allowed: set[float]) -> bool:
    value = float(token)
    digits = _decimals(token)
    for candidate in allowed:
        if abs(candidate - value) < 1e-12:
            return True
        # 允许模型做四舍五入/截断展示（精度不高于事实清单原值）
        if digits <= 6 and round(candidate, digits) == round(value, digits):
            return True
    return False


def question_numbers(question: str) -> set[float]:
    """用户问题里出现过的数字：回答引用问题中的日期/范围不算编造。"""
    return {float(token) for token in extract_numbers(question or "")}


def numeric_violations(text: str, allowed: set[float]) -> list[str]:
    """返回回答中**不在事实清单里**的数值 token（用于阻止编造数值）。"""
    if not text:
        return []
    cleaned = _URL_RE.sub(" ", remove_labels(str(text)))
    violations: list[str] = []
    for match in _NUMBER_RE.finditer(cleaned):
        token = match.group(1)
        if not _matches_allowed(token, allowed):
            violations.append(token)
    return violations


def knowledge_allowed_numbers(fact_sheet: FactSheet) -> set[float]:
    """知识证据中可以合法出现的数值：片段原文中的数字 + 页码 + Chunk ID。"""
    values: set[float] = set()
    for item in fact_sheet.knowledge:
        values.update(_numbers_in_evidence_text(item.content))
        for key in ("page_start", "page_end", "chunk_id"):
            value = item.chunk.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.add(float(value))
    return values


__all__ = [
    "SCOPE_LABELS",
    "FactItem",
    "FactSheet",
    "KnowledgeItem",
    "build_fact_sheet",
    "extract_numbers",
    "fmt_num",
    "fmt_ratio",
    "knowledge_allowed_numbers",
    "numeric_violations",
    "question_numbers",
    "remove_labels",
]
