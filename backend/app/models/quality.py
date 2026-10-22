"""数据质量标记的语义定义（唯一事实来源）。

规则必须可复现、可追溯，并写死在代码里而不是散落在导入脚本中：

- **行级标记**（`sensor_readings.quality_flags`，位掩码）只描述“这一条记录本身”。
- **窗口级缺陷**（采样缺口）不挂在单行上，而是写入 `data_gaps` 表，
  由区间查询/覆盖度函数统一表达。两类信息不混用。

本阶段只实现一条行级规则：传感器冻结候选。
它**不删除**、不修改任何原始测量值，只是把“疑似记录端卡死”标出来供查询排除。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# 规则版本：任何判据变化都必须升版本，历史行保留旧版本号，保证可追溯。
FROZEN_RULE_VERSION = "frozen-v1"

# 位掩码定义（smallint，按位或）
QUALITY_FLAG_FROZEN_CANDIDATE = 1

QUALITY_FLAG_DOCS: dict[int, str] = {
    QUALITY_FLAG_FROZEN_CANDIDATE: (
        "frozen_candidate：本条记录的 7 个模拟量（tp2/tp3/h1/dv_pressure/reservoirs/"
        "oil_temperature/motor_current）与源文件中紧邻的上一条记录逐位相同，"
        "且时间戳仍向前推进。判据见 frozen-v1，属于“疑似记录端卡死”，"
        "不删除记录、不修改数值，也不代表设备真实稳态。"
    ),
}

# 缺口判定阈值（秒）：标称步长 10 s，实测时钟量化抖动上限 13 s，
# 取 30 s 才计为缺口（与 Phase 1A 分析脚本一致）。
GAP_THRESHOLD_SECONDS = 30.0
GAP_RULE_VERSION = "gap-v1"

# 覆盖度计算使用的标称步长（秒），来自时间间隔众数实测值。
NOMINAL_STEP_SECONDS = 10.0

# 缺失样本估算公式（P2-1 统一口径）：
#
#   missing_samples = round_half_up(gap_seconds / nominal_step) - 1
#
# 说明：
# - **估计值**，基于名义采样周期 10 s，不是对真实缺失次数的精确观测；
# - 相邻两条记录间隔 Δ 内，“本应存在”的采样点个数在 Δ 为步长整数倍时为 Δ/步长-1；
# - 采用**四舍五入（half-up，半值向上进位）**，与 PostgreSQL `round(numeric)` 一致；
#   Python 内置 `round()` 使用银行家舍入（half-to-even），在 x.5 处会少算 1，
#   这正是此前 332 个缺口中有 23 个与文档公式不一致的原因；
# - 时钟抖动（±1–3 s）不是缺失，只有 Δ ≥ GAP_THRESHOLD_SECONDS 才记为缺口。
MISSING_SAMPLE_ROUNDING = "half_up"


def estimate_missing_samples(
    gap_seconds: float | Decimal | int,
    step_seconds: float | Decimal = NOMINAL_STEP_SECONDS,
) -> int:
    """按统一口径估算缺口内缺失的采样样本数（>= 0）。"""
    try:
        gap = Decimal(str(gap_seconds))
        step = Decimal(str(step_seconds))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - 防御性
        raise ValueError(f"无法解析间隔/步长：{gap_seconds!r}, {step_seconds!r}") from exc
    if step <= 0:
        raise ValueError("step_seconds 必须为正数")
    if gap <= 0:
        return 0
    rounded = (gap / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(int(rounded) - 1, 0)


def describe_flags(value: int) -> list[str]:
    """把位掩码还原成规则名列表，供脚本与测试断言使用。"""
    return [name for bit, name in _flag_names().items() if value & bit]


def _flag_names() -> dict[int, str]:
    return {
        QUALITY_FLAG_FROZEN_CANDIDATE: "frozen_candidate",
    }
