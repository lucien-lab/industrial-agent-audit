"""pytest 公共装置。

安全边界（重要）：
- 所有测试只使用 `TEST_DATABASE_URL` 指向的**独立测试库**；
- 库名必须以 `_test` 结尾，否则直接终止测试会话；
- 每个用例前 TRUNCATE 测试库表，绝不清空生产库。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
for entry in (BACKEND_DIR, SCRIPTS_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.core.config import load_env_file  # noqa: E402
from app.services import dataset as ds  # noqa: E402

TABLES = (
    # 阶段 2：知识库表也在每个用例前清空，保证 RAG 测试互不影响
    "knowledge_ingest_runs",
    "document_chunks",
    "documents",
    "sensor_readings",
    "data_gaps",
    "ingest_runs",
    "fault_events",
    "data_sources",
    "equipment",
)


def _test_database_url() -> str:
    load_env_file()
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.exit("缺少 TEST_DATABASE_URL，拒绝在未隔离的数据库上运行测试", returncode=2)
    db_name = url.rsplit("/", 1)[-1]
    if not db_name.endswith("_test"):
        pytest.exit(f"测试库名必须以 _test 结尾，当前为 {db_name!r}", returncode=2)
    # 让被测代码默认也指向测试库（RAG 也必须在隔离库上运行）
    os.environ["DATABASE_URL"] = url
    os.environ["RAG_DATABASE_URL"] = url
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    return _test_database_url()


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """在测试库上执行 Alembic 迁移（不使用 create_all）。"""
    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.exit(
            f"测试库迁移失败：\n{completed.stdout}\n{completed.stderr}", returncode=2
        )
    return database_url


@pytest.fixture(scope="session")
def engine(database_url: str, migrated_database: str) -> Iterator[Engine]:
    eng = create_engine(database_url, pool_pre_ping=True, future=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()


@pytest.fixture(scope="module")
def module_session(engine: Engine) -> Iterator[Session]:
    """模块级会话：供“导入一次、多断言”的一致性测试使用。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    sess = factory()
    try:
        yield sess
    finally:
        sess.close()
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
def real_csv_path() -> Path:
    if not ds.CSV_PATH.is_file():
        pytest.skip("原始 CSV 不存在，跳过依赖真实数据样本的测试")
    return ds.CSV_PATH


@pytest.fixture(scope="session")
def real_head_csv(real_csv_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """从真实 CSV 头部截取一段作为导入测试样本（不是伪造数据）。"""
    target = tmp_path_factory.mktemp("metropt") / "metropt3_head_400.csv"
    frame = pd.read_csv(real_csv_path, nrows=400, dtype=str, keep_default_na=False)
    frame.to_csv(target, index=False)
    return target


@pytest.fixture()
def synthetic_csv(tmp_path: Path) -> Path:
    """人工构造的最小 CSV，只用于验证冻结/缺口规则实现是否正确。

    第 3–5 行的 7 路模拟量与第 2 行完全相同（触发 frozen-v1），
    第 6 行时间跳变 60 s（触发 gap-v1）。这些数值仅用于规则单元测试，
    不作为任何验收统计的依据。
    """
    header = (
        "Unnamed: 0,timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,"
        "Oil_temperature,Motor_current,COMP,DV_eletric,Towers,MPG,LPS,"
        "Pressure_switch,Oil_level,Caudal_impulses"
    )
    rows = [
        "0,2020-02-01 00:00:00,1.0,9.0,8.0,0.0,9.0,50.0,1.0,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "10,2020-02-01 00:00:10,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "20,2020-02-01 00:00:20,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "30,2020-02-01 00:00:30,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "40,2020-02-01 00:00:40,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "50,2020-02-01 00:01:40,0.9,8.9,7.9,0.0,8.9,49.9,0.9,0.0,1.0,0.0,0.0,1.0,0.0,0.0,0.0",
    ]
    path = tmp_path / "synthetic_rules.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 阶段 1C：FastAPI 测试装置
#
# 约定：
# - API 客户端只读（本服务不提供写接口）；
# - 测试数据一律通过 `engine` 导入到 `*_test` 库（提交后可见），
#   不使用 conftest 的 `session`（其事务要到用例结束才提交，API 侧看不到）；
# - 每个用例开始前 TRUNCATE 测试库，绝不触碰生产库。
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_engine(engine: Engine) -> Engine:
    """清空测试库表并返回 engine（API 侧可见的已提交数据都从这里写入）。"""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    return engine


@pytest.fixture()
def api_client(migrated_database: str):
    """FastAPI TestClient（不走真实网络端口，但会执行中间件与依赖）。"""
    from fastapi.testclient import TestClient

    from app.api.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def api_dataset(clean_engine: Engine, real_head_csv: Path):
    """真实数据样本（CSV 前 400 行）+ 官方故障事件，供 API 集成测试使用。"""
    from app.services.metropt_import import import_fault_events, import_metropt_csv

    import_metropt_csv(real_head_csv, engine=clean_engine)
    import_fault_events(engine=clean_engine)
    return {
        "equipment_id": "metropt3-apu-01",
        "window_start": "2020-02-01T00:00:00",
        "window_end": "2020-02-01T00:00:30",
    }


@pytest.fixture()
def api_quality_dataset(clean_engine: Engine, tmp_path: Path):
    """构造带“冻结候选 + 300 s 以上缺口”的小数据集，用于质量/统计断言。

    时间轴（秒）：0,10,…,100（11 行）→ 500, 510（2 行），共 13 行。
    - 缺口：100 s → 500 s，Δ=400 s（≥30 s 阈值），missing_samples=39
    - 冻结候选：20/30/40/50 s 四行与上一条模拟量逐位相同（共 4 行）
    - comp 数字序列：1×5, 0×6, 1×2 → 2 次状态切换
    """
    header = (
        "Unnamed: 0,timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,"
        "Oil_temperature,Motor_current,COMP,DV_eletric,Towers,MPG,LPS,"
        "Pressure_switch,Oil_level,Caudal_impulses"
    )
    # 只有 20/30/40/50 s 四行与上一行完全相同（模拟“记录端卡死”），其余行逐行不同
    analog = {
        0: (1.0, 9.0, 8.0, 0.0, 9.0, 50.0, 1.0),
        10: (1.1, 9.1, 8.1, 0.1, 9.1, 50.1, 1.1),
        20: (1.1, 9.1, 8.1, 0.1, 9.1, 50.1, 1.1),
        30: (1.1, 9.1, 8.1, 0.1, 9.1, 50.1, 1.1),
        40: (1.1, 9.1, 8.1, 0.1, 9.1, 50.1, 1.1),
        50: (1.1, 9.1, 8.1, 0.1, 9.1, 50.1, 1.1),
        60: (1.2, 9.2, 8.2, 0.2, 9.2, 50.2, 1.2),
        70: (1.25, 9.25, 8.25, 0.25, 9.25, 50.25, 1.25),
        80: (1.3, 9.3, 8.3, 0.3, 9.3, 50.3, 1.3),
        90: (1.35, 9.35, 8.35, 0.35, 9.35, 50.35, 1.35),
        100: (1.4, 9.4, 8.4, 0.4, 9.4, 50.4, 1.4),
    }
    comp = {0: 1, 10: 1, 20: 1, 30: 1, 40: 1, 50: 0, 60: 0, 70: 0, 80: 0, 90: 0, 100: 0}
    rows: list[str] = []
    for idx, offset in enumerate(sorted(analog)):
        values = analog[offset]
        ts = pd.Timestamp("2020-02-01") + pd.Timedelta(seconds=offset)
        rows.append(
            ",".join(
                [str(idx * 10), ts.strftime("%Y-%m-%d %H:%M:%S")]
                + [f"{value:.4f}".rstrip("0").rstrip(".") or "0" for value in values]
                + [str(comp[offset])]
                + ["1" if offset % 20 == 0 else "0"]
                + ["1", "1", "0", "1", "1", "0"]
            )
        )
    for offset in (500, 510):
        ts = pd.Timestamp("2020-02-01") + pd.Timedelta(seconds=offset)
        rows.append(
            ",".join(
                [str(offset), ts.strftime("%Y-%m-%d %H:%M:%S")]
                + (["1.5", "9.5", "8.5", "0.5", "9.5", "50.5", "1.5"] if offset == 500
                   else ["1.6", "9.6", "8.6", "0.6", "9.6", "50.6", "1.6"])
                + ["1"] * 8
            )
        )
    path = tmp_path / "api_quality_dataset.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    from app.services.metropt_import import import_metropt_csv

    import_metropt_csv(path, engine=clean_engine)
    return {
        "equipment_id": "metropt3-apu-01",
        "start": "2020-02-01T00:00:00",
        "end": "2020-02-01T00:10:00",
        "rows": 13,
        "gap_missing_samples": 39,
    }
