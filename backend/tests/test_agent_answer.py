"""证据整合与回答生成测试：事实标签、数值接地、引用逐字校验、降级路径。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.agent.answer import (
    compose_answer,
    deterministic_conclusion,
    parse_llm_json,
    validate_knowledge_points,
)
from app.agent.config import AgentSettings, get_agent_settings
from app.agent.evidence import (
    build_fact_sheet,
    extract_numbers,
    fmt_num,
    numeric_violations,
)
from app.agent.intent import Slots
from app.rag.errors import LLMTimeout
from tests.helpers_rag import FakeChat

QUOTE = "TP2 (bar) – the measure of the pressure on the compressor."


def _stats_observation() -> dict:
    return {
        "tool": "get_sensor_statistics",
        "status": "ok",
        "error": None,
        "data": {
            "equipment": {
                "equipment_code": "metropt3-apu-01",
                "equipment_name": "MetroPT-3 APU",
                "equipment_type": "air_production_unit",
                "first_observed_at": "2020-02-01T00:00:00",
                "last_observed_at": "2020-09-01T03:59:50",
            },
            "window": {
                "start": "2020-04-17T00:00:00",
                "end": "2020-04-18T00:00:00",
                "duration_seconds": 86400,
            },
            "sensors": ["motor_current"],
            "row_count": 6301,
            "nominal_step_seconds": 10,
            "exclude_frozen": False,
            "analog": [
                {
                    "sensor": "motor_current",
                    "count": 6301,
                    "min": 0.035,
                    "max": 6.36,
                    "mean": 0.4511521980637994,
                    "std": 1.30197218672546,
                    "std_method": "stddev_samp",
                    "first_timestamp": "2020-04-17T00:00:05",
                    "last_timestamp": "2020-04-17T23:59:49",
                }
            ],
            "digital": [],
            "source": {"service": "services.sensor_statistics.get_sensor_statistics", "table": "sensor_readings"},
        },
    }


def _quality_observation() -> dict:
    return {
        "tool": "get_data_quality",
        "status": "ok",
        "data": {
            "equipment": {"equipment_code": "metropt3-apu-01"},
            "window": {"start": "2020-04-17T00:00:00", "end": "2020-04-18T00:00:00"},
            "actual_samples": 6301,
            "expected_samples": 8640,
            "sample_coverage": 0.7292824074074075,
            "time_coverage": 0.8314,
            "gap_count": 2,
            "gap_duration_seconds": 14594.0,
            "max_gap_seconds": 14092.0,
            "missing_samples_estimate": 1457,
            "frozen_candidate_count": 4378,
            "frozen_candidate_ratio": 0.6948,
            "records_with_quality_flags": 4378,
            "window_inside_data_range": True,
            "nominal_step_seconds": 10,
            "warnings": [
                {"code": "sample_coverage_below_threshold", "severity": "warning", "message": "覆盖率低"}
            ],
            "source": {"service": "services.data_quality.get_data_quality", "table": "sensor_readings"},
        },
    }


def _knowledge_observation() -> dict:
    return {
        "tool": "search_technical_knowledge",
        "status": "ok",
        "data": {
            "query": "TP2 的含义",
            "method": "hybrid",
            "returned": 1,
            "chunks": [
                {
                    "chunk_id": 42,
                    "document_id": "00000000-0000-0000-0000-000000000001",
                    "doc_key": "metropt3-data-description",
                    "document_title": "MetroPT-3 Data Description",
                    "page_start": 2,
                    "page_end": 2,
                    "section_title": "Sensors",
                    "equipment_scope": "metropt3_direct",
                    "doc_type": "dataset_description",
                    "trust_level": "official_primary",
                    "source_url": "https://example.invalid/doc.pdf",
                    "chunking_version": "structural-v1",
                    "citation_label": "metropt3-data-description:p2",
                    "score": 0.03,
                    "vector_similarity": 0.7,
                    "keyword_score": 0.4,
                    "rrf_score": 0.03,
                    "matched_terms": ["tp2"],
                    "excerpt": QUOTE,
                }
            ],
            "source": {"service": "services.knowledge.search_knowledge"},
        },
    }


@pytest.fixture()
def fact_sheet():
    return build_fact_sheet(
        [_stats_observation(), _quality_observation(), _knowledge_observation()]
    )


@pytest.fixture()
def settings() -> AgentSettings:
    return replace(get_agent_settings(), answer_mode="llm", trace_write_files=False)


def _slots() -> Slots:
    return Slots(
        equipment_code="metropt3-apu-01",
        start_time="2020-04-17T00:00:00",
        end_time="2020-04-18T00:00:00",
        sensors=("motor_current",),
        asks_statistics=True,
    )


# --------------------------------------------------------------------------- #
# 事实清单
# --------------------------------------------------------------------------- #
def test_fact_labels_follow_execution_order(fact_sheet) -> None:
    labels = [item.label for item in fact_sheet.facts]
    assert labels == [f"D{index}" for index in range(1, len(labels) + 1)]
    assert fact_sheet.facts[0].kind == "statistics_summary"
    assert fact_sheet.knowledge[0].label == "K1"
    assert fact_sheet.knowledge[0].chunk["chunk_id"] == 42


def test_allowed_numbers_include_window_and_metrics(fact_sheet) -> None:
    allowed = fact_sheet.allowed_numbers
    assert 6.36 in allowed  # 最大值
    assert 6301 in allowed
    assert 2020 in allowed  # 窗口年份
    assert 4 in allowed  # 月份
    assert 17 in allowed  # 日期


def test_numeric_violations_flags_invented_values(fact_sheet) -> None:
    assert numeric_violations("窗口内共 6301 个样本，最大 6.36。", fact_sheet.allowed_numbers) == []
    # 模型自行计算的数值不在事实清单里
    assert numeric_violations("平均电流约为 0.4512，峰值 6.36。", fact_sheet.allowed_numbers) == []
    assert numeric_violations("峰值 7.99，共 6301 个样本。", fact_sheet.allowed_numbers) == ["7.99"]
    # 引用标签里的数字不算数值
    assert numeric_violations("[D1] 见 [K2]", fact_sheet.allowed_numbers) == []
    # 允许四舍五入展示
    assert numeric_violations("均值 0.45", fact_sheet.allowed_numbers) == []


def test_extract_numbers_ignores_labels_and_urls() -> None:
    assert extract_numbers("[D12] 见 https://example.invalid/2020/x.pdf 共 12 个") == [12.0]


def test_fmt_num_trims_trailing_zeros() -> None:
    assert fmt_num(6.36) == "6.36"
    assert fmt_num(6301.0) == "6301"
    assert fmt_num(0.4511521980637994) == "0.4512"
    assert fmt_num(None) == "null"


def test_deterministic_conclusion_uses_fact_labels(fact_sheet) -> None:
    text = deterministic_conclusion(fact_sheet, "data_only", _slots())
    assert "[D" in text
    assert "2020-04-17" in text
    assert "6.36" in text


# --------------------------------------------------------------------------- #
# 引用校验
# --------------------------------------------------------------------------- #
def test_validate_knowledge_points_accepts_verbatim_quote(fact_sheet) -> None:
    accepted, rejected = validate_knowledge_points(
        [{"text": "TP2 是压缩机上的压力测点 [K1]", "labels": ["K1"], "support_quotes": {"K1": QUOTE}}],
        fact_sheet,
    )
    assert len(accepted) == 1
    assert not rejected


@pytest.mark.parametrize(
    ("labels", "quote"),
    [
        (["K9"], QUOTE),  # 不存在的标签
        (["K1"], "TP2 是压缩机上的压力测点。"),  # 中文改写/翻译，不是原文
        (["K1"], "TP2 measures pressure"),  # 近似但不逐字
        (["K1"], "short"),  # 太短
    ],
)
def test_validate_knowledge_points_rejects_bad_citations(fact_sheet, labels, quote) -> None:
    accepted, rejected = validate_knowledge_points(
        [{"text": "结论 [K1]", "labels": labels, "support_quotes": {labels[0]: quote}}],
        fact_sheet,
    )
    assert not accepted
    assert rejected


def test_parse_llm_json_handles_fenced_output() -> None:
    data, error = parse_llm_json('```json\n{"direct_answer": "x"}\n```')
    assert error is None and data["direct_answer"] == "x"
    data, error = parse_llm_json("not json at all")
    assert data is None and error


# --------------------------------------------------------------------------- #
# 回答生成
# --------------------------------------------------------------------------- #
def _llm_payload(**overrides) -> dict:
    payload = {
        "direct_answer": "窗口 [D1] 内电机电流最大 6.36，共 6301 个样本。",
        "knowledge_points": [
            {"text": "TP2 是压缩机上的压力测点 [K1]", "labels": ["K1"], "support_quotes": {"K1": QUOTE}}
        ],
        "inference_notes": ["采样覆盖率偏低（[D2]），异常判断需谨慎。"],
        "uncertainties": ["缺少厂商阈值，无法判定是否超出正常范围。"],
        "insufficient_evidence": False,
    }
    payload.update(overrides)
    return payload


def test_compose_answer_uses_valid_llm_output(fact_sheet, settings) -> None:
    chat = FakeChat(_llm_payload())
    result = compose_answer(
        question="2020-04-17 电机电流是否异常？",
        fact_sheet=fact_sheet,
        intent_kind="mixed_analysis",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.mode == "llm"
    assert result.status == "answered"
    assert result.grounding["ok"] is True
    assert "6.36" in result.answer
    assert "## 数据依据" in result.answer
    assert "## 技术知识依据" in result.answer
    assert "不构成故障诊断结论" in result.answer
    assert any(item["label"] == "K1" and item.get("chunk_id") == 42 for item in result.citations)


def test_compose_answer_falls_back_on_invented_number(fact_sheet, settings) -> None:
    chat = FakeChat(_llm_payload(direct_answer="电流峰值达到 9.87，明显高于正常水平。"))
    result = compose_answer(
        question="2020-04-17 电机电流是否异常？",
        fact_sheet=fact_sheet,
        intent_kind="mixed_analysis",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.mode == "deterministic"
    assert result.grounding["ok"] is False
    assert "9.87" in result.grounding["numeric_violations"]
    assert "9.87" not in result.answer
    assert any("数值" in warning for warning in result.warnings)


def test_compose_answer_drops_unsupported_knowledge_point(fact_sheet, settings) -> None:
    payload = _llm_payload()
    payload["knowledge_points"] = [
        {
            "text": "MetroPT-3 的额定排气压力为 8.2 bar [K1]",
            "labels": ["K1"],
            "support_quotes": {"K1": "额定排气压力为 8.2 bar"},  # 编造摘录
        }
    ]
    result = compose_answer(
        question="TP2 的含义是什么？",
        fact_sheet=fact_sheet,
        intent_kind="knowledge_only",
        slots=_slots(),
        settings=settings,
        chat_model=FakeChat(payload),
    )
    assert result.grounding["citation_issues"]
    assert any("引用" in warning for warning in result.warnings)


def test_compose_answer_rejects_diagnosis_overreach(fact_sheet, settings) -> None:
    chat = FakeChat(_llm_payload(direct_answer="已确认故障：压缩机轴承损坏。"))
    result = compose_answer(
        question="2020-04-17 是否异常？",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.mode == "deterministic"
    assert "确诊" in result.answer or "已确认故障" not in result.answer


def test_compose_answer_falls_back_on_invalid_json(fact_sheet, settings) -> None:
    chat = FakeChat("这不是 JSON")
    result = compose_answer(
        question="2020-04-17 电机电流最大值？",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.mode == "deterministic"
    assert "6.36" in result.answer


def test_compose_answer_falls_back_on_llm_timeout(fact_sheet, settings) -> None:
    class TimeoutChat:
        provider = "fake"
        model = "fake"

        def chat(self, *args, **kwargs):
            raise LLMTimeout("超时")

    result = compose_answer(
        question="2020-04-17 电机电流最大值？",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=TimeoutChat(),
    )
    assert result.mode == "deterministic"
    assert result.llm["status"] == "error"
    assert proceed_answer(result) is True


def proceed_answer(result) -> bool:
    return "## 数据依据" in result.answer


def test_compose_answer_refuses_without_evidence(settings) -> None:
    empty = build_fact_sheet([])
    result = compose_answer(
        question="2020-04-17 电机电流最大值？",
        fact_sheet=empty,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=FakeChat(_llm_payload()),
    )
    assert result.status == "insufficient_evidence"
    assert result.mode == "refusal"
    assert "不足以确认" in result.answer or "证据不足" in result.answer


def test_compose_answer_explicit_refusal_never_calls_llm(fact_sheet, settings) -> None:
    chat = FakeChat(_llm_payload())
    result = compose_answer(
        question="设备不存在",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
        refusal="设备 'x' 不存在，无法查询其运行数据。",
    )
    assert result.mode == "refusal"
    assert chat.calls == []
    assert "不存在" in result.answer


def test_compose_answer_marks_insufficient_evidence_from_model(fact_sheet, settings) -> None:
    chat = FakeChat(
        _llm_payload(
            direct_answer="证据不足：检索片段没有给出该测点的量程。",
            knowledge_points=[],
            insufficient_evidence=True,
        )
    )
    result = compose_answer(
        question="TP2 的量程是多少？",
        fact_sheet=fact_sheet,
        intent_kind="knowledge_only",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.status == "insufficient_evidence"


def test_compose_answer_deterministic_mode_skips_llm(fact_sheet) -> None:
    chat = FakeChat(_llm_payload())
    settings = replace(get_agent_settings(), answer_mode="deterministic")
    result = compose_answer(
        question="2020-04-17 电机电流最大值？",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=chat,
    )
    assert result.mode == "deterministic"
    assert chat.calls == []


def test_answer_sections_are_serializable(fact_sheet, settings) -> None:
    result = compose_answer(
        question="2020-04-17 电机电流最大值？",
        fact_sheet=fact_sheet,
        intent_kind="data_only",
        slots=_slots(),
        settings=settings,
        chat_model=FakeChat(_llm_payload()),
    )
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "numeric_violations" in payload
