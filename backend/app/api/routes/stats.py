"""传感器统计接口（模拟量 / 数字量语义分开）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import EndTimeQuery, ExcludeFrozenQuery, SensorQuery, SessionDep, StartTimeQuery
from app.api.serializers import equipment_brief, window_info
from app.schemas.common import ErrorResponse
from app.schemas.responses import AnalogStatsOut, DigitalStatsOut, StatisticsResponse
from app.services import sensor_statistics as service

router = APIRouter(prefix="/api/v1/equipment/{equipment_id}", tags=["传感器统计"])


@router.get(
    "/stats",
    response_model=StatisticsResponse,
    summary="时间窗内传感器统计",
    description=(
        "在数据库内完成聚合，不把原始行加载到内存。\n\n"
        "**模拟量**（`tp2/tp3/h1/dv_pressure/reservoirs/oil_temperature/motor_current`）返回\n"
        "- `count`：非空样本数\n"
        "- `min` / `max` / `mean`：库内原始值，无插值、无异常剔除\n"
        "- `std`：**样本标准差** `stddev_samp`（ddof=1，与 pandas `Series.std()` 默认一致），"
        "样本数 < 2 时为 null\n"
        "- `first_timestamp` / `last_timestamp`\n\n"
        "**数字量**（`comp/dv_eletric/towers/mpg/lps/pressure_switch/oil_level/caudal_impulses`）返回\n"
        "- `count_0` / `count_1`：0/1 状态数量\n"
        "- `ratio_0` / `ratio_1`：状态占比\n"
        "- `transitions`：窗口内相邻行之间的 0/1 状态切换次数（窗口首行不计）\n\n"
        "**边界**：`[start_time, end_time)`，无时区时间。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "设备不存在"},
        422: {"model": ErrorResponse, "description": "参数不合法 / 时间带时区 / 传感器不支持"},
    },
)
def get_statistics(
    equipment_id: str,
    session: SessionDep,
    start_time: StartTimeQuery,
    end_time: EndTimeQuery,
    sensor: SensorQuery = None,
    exclude_frozen: ExcludeFrozenQuery = False,
) -> StatisticsResponse:
    stats = service.get_sensor_statistics(
        session,
        equipment_id,
        start=start_time,
        end=end_time,
        sensors=sensor,
        exclude_frozen=exclude_frozen,
    )
    return StatisticsResponse(
        equipment=equipment_brief(stats.equipment),
        window=window_info(stats.window),
        sensors=list(stats.sensors),
        row_count=stats.row_count,
        nominal_step_seconds=stats.nominal_step_seconds,
        analog=[
            AnalogStatsOut(
                sensor=item.sensor,
                count=item.count,
                min=item.minimum,
                max=item.maximum,
                mean=item.mean,
                std=item.std,
                std_method=item.std_method,
                first_timestamp=item.first_timestamp,
                last_timestamp=item.last_timestamp,
            )
            for item in stats.analog
        ],
        digital=[
            DigitalStatsOut(
                sensor=item.sensor,
                count=item.count,
                count_0=item.count_0,
                count_1=item.count_1,
                ratio_0=item.ratio_0,
                ratio_1=item.ratio_1,
                transitions=item.transitions,
                first_timestamp=item.first_timestamp,
                last_timestamp=item.last_timestamp,
            )
            for item in stats.digital
        ],
        exclude_frozen=stats.exclude_frozen,
        semantics=list(stats.semantics),
    )
