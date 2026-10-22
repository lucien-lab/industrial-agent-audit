"""ORM 模型统一出口，Alembic 与测试通过这里拿到完整 metadata。"""

from app.db.base import Base
from app.models.audit import AuditFinding, AuditRun, AuditTrace, AuditTraceEvent
from app.models.data_source import TIMEZONE_STATUSES, DataSource
from app.models.equipment import Equipment
from app.models.fault_event import VERIFICATION_STATUSES, FaultEvent
from app.models.ingest import DataGap, IngestRun
from app.models.knowledge import (
    EMBEDDING_DIM,
    INGEST_STATUSES,
    Document,
    DocumentChunk,
    KnowledgeIngestRun,
)
from app.models.quality import (
    FROZEN_RULE_VERSION,
    GAP_RULE_VERSION,
    GAP_THRESHOLD_SECONDS,
    NOMINAL_STEP_SECONDS,
    QUALITY_FLAG_DOCS,
    QUALITY_FLAG_FROZEN_CANDIDATE,
    describe_flags,
    estimate_missing_samples,
)
from app.models.sensor_reading import ANALOG_COLUMNS, DIGITAL_COLUMNS, SensorReading

__all__ = [
    "ANALOG_COLUMNS",
    "AuditFinding",
    "AuditRun",
    "AuditTrace",
    "AuditTraceEvent",
    "Base",
    "DIGITAL_COLUMNS",
    "DataGap",
    "DataSource",
    "Document",
    "DocumentChunk",
    "EMBEDDING_DIM",
    "INGEST_STATUSES",
    "Equipment",
    "FROZEN_RULE_VERSION",
    "FaultEvent",
    "GAP_RULE_VERSION",
    "GAP_THRESHOLD_SECONDS",
    "IngestRun",
    "KnowledgeIngestRun",
    "NOMINAL_STEP_SECONDS",
    "QUALITY_FLAG_DOCS",
    "QUALITY_FLAG_FROZEN_CANDIDATE",
    "SensorReading",
    "TIMEZONE_STATUSES",
    "VERIFICATION_STATUSES",
    "describe_flags",
    "estimate_missing_samples",
]
