"""工业设备查询页面：设备列表、统计、数据质量、故障区间、采样片段。

约束（与 `docs/tui_design.md` 一致）：

- 不加载全量数据：统计在数据库内聚合；采样只取窗口内的**前 N 条**（默认 240，硬上限 1000）；
- 图形数据来自真实 `/readings` 响应，缺失值不插值（Sparkline 用空格占位）；
- 覆盖率为 1 与缺口会明确提示；`frozen_candidate`（记录端卡死候选）不等于设备故障；
- 故障区间内的传感器记录不会被当作“已确认故障样本”。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent, TabPane

from app.tui.client import ApiError
from app.tui.formatting import (
    coverage_note,
    fmt_number,
    fmt_ratio,
    fmt_seconds,
    fmt_ts,
    sparkline,
)
from app.tui.models import Equipment, FaultPage, QualityInfo, ReadingPage, StatsPage
from app.tui.pages.base import BasePage

DEFAULT_SENSORS = "motor_current,dv_pressure,tp2"
READING_SAMPLE_LIMIT = 240


class EquipmentPage(BasePage):
    PAGE_ID = "equipment"
    PAGE_TITLE = "Equipment"
    PAGE_SUMMARY = "设备数据查询（统计 / 质量 / 故障 / 采样）"

    BINDINGS = [Binding("ctrl+s", "query", "查询", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._equipment: list[Equipment] = []
        self._busy = False

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Equipment · 工业设备数据查询", classes="page-header")
            yield Static(
                "时间窗为左闭右开 [start, end)，时间无时区；统计与质量在数据库内聚合，"
                "采样只取前 240 条真实观测（不插值）。",
                classes="page-hint",
            )
            with Horizontal(classes="controls"):
                yield Select([], id="equipment-id", prompt="设备", allow_blank=True)
                yield Input(placeholder="YYYY-MM-DDTHH:MM:SS", id="equipment-start")
                yield Input(placeholder="YYYY-MM-DDTHH:MM:SS", id="equipment-end")
                yield Input(value=DEFAULT_SENSORS, placeholder="sensors", id="equipment-sensors")
                yield Button("查询", id="equipment-load", variant="primary")
            yield Static("", id="page-equipment-message", classes="message")
            with TabbedContent(id="equipment-tabs"):
                with TabPane("统计", id="tab-stats"):
                    yield DataTable(id="equipment-stats-table", cursor_type="row")
                with TabPane("质量", id="tab-quality"):
                    with VerticalScroll(classes="scroll-detail"):
                        yield Static("", id="equipment-quality")
                with TabPane("故障", id="tab-faults"):
                    yield DataTable(id="equipment-faults-table", cursor_type="row")
                with TabPane("采样", id="tab-readings"):
                    with Vertical():
                        yield Static("", id="equipment-chart")
                        yield DataTable(id="equipment-readings-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#equipment-stats-table", DataTable).add_columns(
            "传感器", "类型", "样本数", "min", "max", "mean", "std / 说明"
        )
        self.query_one("#equipment-faults-table", DataTable).add_columns(
            "事件", "类型", "开始", "结束", "时长", "重叠秒", "核验状态"
        )
        self.query_one("#equipment-readings-table", DataTable).add_columns(
            "观测时间", "质量位", "冻结候选", "取值"
        )

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    async def load(self, *, force: bool = False) -> None:
        if not self._equipment or force:
            try:
                page = await self.client.list_equipment(limit=50)
            except ApiError as exc:
                self.notify_status(f"设备列表加载失败：{exc}", "error")
                return
            self._equipment = page.items
            select = self.query_one("#equipment-id", Select)
            select.set_options(
                [(f"{item.equipment_name or item.equipment_id} ({item.equipment_id})", item.equipment_id)
                 for item in page.items]
            )
            if page.items and select.value is Select.BLANK:
                select.value = page.items[0].equipment_id
            self._apply_default_window(page.items[0] if page.items else None)
        await self._query()

    def _apply_default_window(self, equipment: Equipment | None) -> None:
        """用设备真实观测范围推导默认窗口（不使用硬编码日期）。

        只在两个输入框都为空时填充；用户已经填过就不覆盖。
        """
        if equipment is None:
            return
        start_input = self.query_one("#equipment-start", Input)
        end_input = self.query_one("#equipment-end", Input)
        if start_input.value.strip() or end_input.value.strip():
            return
        last = equipment.last_observed_at
        if last is None:
            self.set_message(
                "该设备没有观测记录：请手动填写时间窗后再查询。", "warn"
            )
            return
        end_day = datetime(last.year, last.month, last.day)
        start_day = end_day - timedelta(days=1)
        start_input.value = start_day.strftime("%Y-%m-%dT%H:%M:%S")
        end_input.value = end_day.strftime("%Y-%m-%dT%H:%M:%S")

    async def action_query(self) -> None:
        await self._query()

    async def _query(self) -> None:
        if self._busy:
            self.notify_status("上一次查询尚未结束。", "warn")
            return
        equipment_id = self.query_one("#equipment-id", Select).value
        if equipment_id is Select.BLANK or not equipment_id:
            self.notify_status("请先选择设备。", "warn")
            return
        start = self.query_one("#equipment-start", Input).value.strip()
        end = self.query_one("#equipment-end", Input).value.strip()
        sensors = self._parse_sensors(self.query_one("#equipment-sensors", Input).value)
        if not start or not end:
            self.notify_status("请填写 start / end（无时区 ISO 8601）。", "warn")
            return
        self._set_busy(True)
        self.notify_status(f"查询 {equipment_id} [{start}, {end}) …", "dim")
        try:
            stats, quality, faults, readings = await asyncio.gather(
                self.client.stats(equipment_id, start_time=start, end_time=end, sensors=sensors),
                self.client.quality(equipment_id, start_time=start, end_time=end),
                self.client.faults(equipment_id, start_time=start, end_time=end, limit=50),
                self.client.readings(
                    equipment_id,
                    start_time=start,
                    end_time=end,
                    sensors=sensors,
                    limit=READING_SAMPLE_LIMIT,
                ),
                return_exceptions=True,
            )
        finally:
            self._set_busy(False)

        errors: list[str] = []
        if isinstance(stats, BaseException):
            errors.append(f"统计：{_describe(stats)}")
        else:
            self._render_stats(stats)
        if isinstance(quality, BaseException):
            errors.append(f"质量：{_describe(quality)}")
        else:
            self._render_quality(quality)
        if isinstance(faults, BaseException):
            errors.append(f"故障：{_describe(faults)}")
        else:
            self._render_faults(faults)
        if isinstance(readings, BaseException):
            errors.append(f"采样：{_describe(readings)}")
        else:
            self._render_readings(readings, sensors)

        if errors:
            self.notify_status("部分查询失败 → " + "；".join(errors), "error")
        else:
            total = stats.row_count if not isinstance(stats, BaseException) else 0
            self.notify_status(
                f"完成：窗口内 {total} 行观测，采样显示前 {len(readings.items)} 条（真实数据）",
                "ok",
            )

    @staticmethod
    def _parse_sensors(raw: str) -> list[str] | None:
        items = [part.strip() for part in raw.replace(" ", ",").split(",") if part.strip()]
        return items or None

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self.query_one("#equipment-load", Button).disabled = busy
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render_stats(self, stats: StatsPage) -> None:
        table = self.query_one("#equipment-stats-table", DataTable)
        table.clear()
        for item in stats.analog:
            table.add_row(
                item.sensor,
                "模拟量",
                str(item.count),
                fmt_number(item.minimum),
                fmt_number(item.maximum),
                fmt_number(item.mean),
                fmt_number(item.std),
            )
        for item in stats.digital:
            table.add_row(
                item.sensor,
                "数字量",
                str(item.count),
                f"0:{item.count_0}",
                f"1:{item.count_1}",
                f"ratio1={fmt_ratio(item.ratio_1)}",
                f"切换 {item.transitions if item.transitions is not None else '—'} 次",
            )
        if not stats.analog and not stats.digital:
            self.set_message("窗口内没有可统计的传感器数据。", "warn")

    def _render_quality(self, quality: QualityInfo) -> None:
        note = coverage_note(quality.sample_coverage, quality.time_coverage)
        lines = [
            "### 数据质量与采样覆盖率",
            "",
            "| 指标 | 值 | 说明 |",
            "| --- | --- | --- |",
            f"| 实际样本 | {quality.actual_samples} | 窗口内真实存在的观测行（未修补） |",
            f"| 应有样本 | {quality.expected_samples} | 按标称 "
            f"{fmt_number(quality.nominal_step_seconds, 0)} s 计算 |",
            f"| 样本覆盖率 | {fmt_ratio(quality.sample_coverage)} | actual/expected，不封顶 |",
            f"| 时间覆盖率 | {fmt_ratio(quality.time_coverage)} | 1 − 估计缺失秒数/窗口秒数 |",
            f"| 缺口个数 | {quality.gap_count} | 与窗口相交的缺口 |",
            f"| 缺口总时长 | {fmt_seconds(quality.gap_duration_seconds)} | 落在窗口内的实际秒数 |",
            f"| 最大缺口 | {fmt_seconds(quality.max_gap_seconds)} | 单个缺口最长时长 |",
            f"| 估计缺失样本 | {quality.missing_samples_estimate} | **估计值**，不是实测 |",
            f"| 冻结候选行 | {quality.frozen_candidate_count} "
            f"({fmt_ratio(quality.frozen_candidate_ratio)}) | 疑似记录端卡死，**不等于设备故障** |",
            f"| 带质量位行数 | {quality.records_with_quality_flags} | 不静默丢弃，单独统计 |",
            f"| 窗口在数据范围内 | {'是' if quality.window_inside_data_range else '否'} | "
            "false 表示窗口超出数据范围 |",
        ]
        if note:
            lines += ["", f"> ⚠ {note}"]
        if quality.warnings:
            lines += ["", "**接口提示**", ""]
            lines += [f"- `{w.code}`（{w.severity or '—'}）：{w.message}" for w in quality.warnings]
        self.query_one("#equipment-quality", Static).update("\n".join(lines))

    def _render_faults(self, faults: FaultPage) -> None:
        table = self.query_one("#equipment-faults-table", DataTable)
        table.clear()
        for item in faults.items:
            table.add_row(
                item.event_code,
                item.failure_type or "—",
                fmt_ts(item.start_time),
                fmt_ts(item.end_time),
                fmt_seconds(item.duration_seconds),
                fmt_seconds(item.overlap_seconds),
                item.verification_status or "—",
            )
        if not faults.items:
            table.add_row("—", "窗口内无官方故障区间", "—", "—", "—", "—", "—")

    def _render_readings(self, readings: ReadingPage, requested: list[str] | None) -> None:
        table = self.query_one("#equipment-readings-table", DataTable)
        table.clear()
        sensors = readings.sensors or requested or []
        for item in readings.items[:80]:
            values = ", ".join(
                f"{name}={fmt_number(item.values.get(name))}" for name in sensors if name in item.values
            )
            table.add_row(
                fmt_ts(item.observed_at),
                str(item.quality_flags),
                "是" if item.quality_labels else "否",
                values,
            )
        chart = self.query_one("#equipment-chart", Static)
        if not readings.items:
            chart.update("窗口内没有采样记录（不插值，不补造数据）。")
            return
        sensor = next(
            (name for name in sensors if name in readings.items[0].values), sensors[0] if sensors else None
        )
        if sensor is None:
            chart.update("无法确定可绘图传感器。")
            return
        series = [item.values.get(sensor) for item in readings.items]
        numeric = [value for value in series if isinstance(value, (int, float))]
        if not numeric:
            chart.update(f"{sensor}：窗口内前 {len(series)} 条记录没有数值。")
            return
        line = sparkline(series, width=100)
        chart.update(
            f"{sensor} 采样片段（前 {len(series)} 条真实观测，未插值）\n"
            f"min={fmt_number(min(numeric))}  max={fmt_number(max(numeric))}  "
            f"返回 {len(readings.items)}/{readings.pagination.total} 条"
            f"（limit={readings.pagination.limit}）\n{line}"
        )

    async def reset_view(self) -> None:  # type: ignore[override]
        self.query_one("#equipment-stats-table", DataTable).clear()
        self.query_one("#equipment-faults-table", DataTable).clear()
        self.query_one("#equipment-readings-table", DataTable).clear()
        self.query_one("#equipment-quality", Static).update("")
        self.query_one("#equipment-chart", Static).update("")
        self.notify_status("已清空设备页面视图（服务端数据未修改）", "dim")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ApiError):
        if exc.status_code == 422:
            return f"参数不合法（{exc.message}）"
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


__all__ = ["EquipmentPage", "DEFAULT_SENSORS", "READING_SAMPLE_LIMIT"]
