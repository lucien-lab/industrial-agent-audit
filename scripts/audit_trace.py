#!/usr/bin/env python
"""独立运行 Agent-Probe 审计（不启动 API、不重新运行业务 Agent）。

用法：

    # 列出可用 Trace
    python scripts/audit_trace.py --list

    # 只跑确定性规则（秒级）
    python scripts/audit_trace.py --trace-id <trace_id> --mode rule_only

    # 混合审计并写入独立审计库
    python scripts/audit_trace.py --trace-id <trace_id> --mode hybrid --persist \
        --out data/processed/audit/<trace_id>.json

    # 不读取知识库原文（纯冻结 Trace 回放）
    python scripts/audit_trace.py --trace-id <trace_id> --mode hybrid --offline-only
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.audit.collector import discover_trace_ids  # noqa: E402
from app.audit.config import get_audit_settings  # noqa: E402
from app.audit.engine import AuditEngine  # noqa: E402
from app.core.config import PROJECT_ROOT, load_env_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent-Probe 独立审计 CLI")
    parser.add_argument("--trace-id", default="", help="要审计的 trace_id")
    parser.add_argument("--mode", default="rule_only", choices=["rule_only", "llm_only", "hybrid"])
    parser.add_argument("--audit-version", default="", help="审计版本（默认 phase4-audit-v1）")
    parser.add_argument("--persist", action="store_true", help="写入 AUDIT_DATABASE_URL 指向的审计库")
    parser.add_argument("--offline-only", action="store_true", help="不读取知识库原文")
    parser.add_argument("--list", action="store_true", help="列出 Trace 目录中的 trace_id")
    parser.add_argument("--out", default="", help="把完整报告写到该 JSON 文件")
    args = parser.parse_args()

    load_env_file()
    settings = get_audit_settings()
    if args.offline_only:
        settings = replace(settings, verify_knowledge_offline=False)

    if args.list:
        traces = discover_trace_ids(settings)
        print(f"trace_dir={settings.trace_dir} count={len(traces)}")
        for trace_id in traces:
            print(trace_id)
        return
    if not args.trace_id:
        raise SystemExit("需要 --trace-id（或用 --list 查看可用 Trace）")

    engine = AuditEngine(settings, use_repository=args.persist)
    report = engine.audit_trace_id(
        args.trace_id,
        mode=args.mode,
        audit_version=args.audit_version or "phase4-audit-v1",
        persist=args.persist,
    )
    payload = report.model_dump(mode="json")
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = PROJECT_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告写入 {out_path}")

    print(
        f"audit_id={report.audit_id} trace_id={report.trace_id} mode={report.audit_mode} "
        f"status={report.status} risk={report.overall_risk} findings={report.total_findings} "
        f"duration_ms={report.duration_ms} judge={report.judge.status}"
    )
    for finding in report.findings:
        events = ",".join(finding.related_event_ids[:4]) or "-"
        print(
            f"  [{finding.severity}] {finding.rule_id} ({finding.risk_category}) "
            f"events={events} :: {finding.explanation[:180]}"
        )
    if report.error:
        print(f"  error={report.error}")


if __name__ == "__main__":
    main()
