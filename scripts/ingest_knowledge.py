#!/usr/bin/env python
"""知识库入库 CLI（阶段 2）。

典型用法：

    # 只解析与切分，不写库（检查解析质量/Chunk 数量）
    python scripts/ingest_knowledge.py --dry-run --no-embed

    # 真实入库（本地 Ollama bge-m3，1024 维）
    python scripts/ingest_knowledge.py --strategy structural --reindex
    python scripts/ingest_knowledge.py --strategy fixed --reindex

安全边界：
- 默认拒绝写入库名不以 `_test` / `_dev` 结尾的数据库（生产库需显式 `--allow-main-db`）；
- 不修改任何工业传感器表；只写 documents / document_chunks / knowledge_ingest_runs；
- Embedding 失败立即中止该文档并记录，不会写入零向量。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.rag.config import get_rag_settings  # noqa: E402
from app.rag.embedding import get_embedding_provider  # noqa: E402
from app.rag.ingest import index_stats, load_sources, run_ingest  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent


def _check_database(url: str, allow_main: bool) -> str:
    db_name = url.rsplit("/", 1)[-1].split("?")[0]
    if not allow_main and not (db_name.endswith("_test") or db_name.endswith("_dev")):
        raise SystemExit(
            f"拒绝写入库 {db_name!r}：知识库入库只允许 *_test / *_dev 隔离库；"
            "确需写入其它库请显式加 --allow-main-db"
        )
    return db_name


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库：解析 / 切分 / 向量化 / 入库")
    parser.add_argument("--sources", type=Path, default=PROJECT_ROOT / "knowledge" / "sources.json")
    parser.add_argument("--doc-key", action="append", default=None, help="只处理指定 doc_key（可重复）")
    parser.add_argument("--strategy", choices=("fixed", "structural"), default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--chunk-overlap", type=int, default=None)
    parser.add_argument("--chunking-version", default=None)
    parser.add_argument("--db-url", default=None, help="覆盖 RAG_DATABASE_URL（仅用于隔离库）")
    parser.add_argument("--allow-main-db", action="store_true", help="允许写入非 _test/_dev 库")
    parser.add_argument("--dry-run", action="store_true", help="不写库，只解析+切分（可含 Embedding）")
    parser.add_argument("--no-embed", action="store_true", help="不调用 Embedding（Chunk 的 vector 为空）")
    parser.add_argument("--reindex", action="store_true", help="忽略“内容未变”判断，强制重建该切分版本")
    parser.add_argument("--json-report", type=Path, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_rag_settings()
    db_url = args.db_url or settings.database_url
    db_name = _check_database(db_url, args.allow_main_db)

    strategy = args.strategy or settings.chunking_strategy
    chunk_size = args.chunk_size or settings.chunk_size
    chunk_overlap = args.chunk_overlap if args.chunk_overlap is not None else settings.chunk_overlap
    chunking_version = args.chunking_version or (
        settings.chunking_version if strategy == settings.chunking_strategy else f"{strategy}-v1"
    )

    specs = load_sources(args.sources)
    if args.doc_key:
        wanted = set(args.doc_key)
        specs = [spec for spec in specs if spec.doc_key in wanted]
        missing = wanted - {spec.doc_key for spec in specs}
        if missing:
            raise SystemExit(f"未找到 doc_key：{sorted(missing)}")
    if not specs:
        raise SystemExit("没有可处理的文档")

    embedder = None if args.no_embed else get_embedding_provider(settings)

    engine = create_engine(db_url, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    print(
        f"[ingest] db={db_name} strategy={strategy} version={chunking_version} "
        f"size={chunk_size} overlap={chunk_overlap} "
        f"embed={embedder.model if embedder else 'none'} documents={len(specs)}"
    )
    try:
        report = run_ingest(
            session,
            specs,
            project_root=PROJECT_ROOT,
            settings=settings,
            strategy=strategy,
            chunking_version=chunking_version,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            embedder=embedder,
            dry_run=args.dry_run,
            reindex=args.reindex,
        )
        stats = index_stats(session)
    finally:
        session.close()
        engine.dispose()

    print("\n[ingest] 每份文档：")
    for result in report.results:
        print(
            f"  {result.status:18s} {result.doc_key:42s} pages={result.page_count} "
            f"chunks={result.chunks:5d} emb={result.embeddings:5d} "
            f"parse={result.parse_elapsed_ms}ms chunk={result.chunk_elapsed_ms}ms "
            f"embed={result.embedding_elapsed_ms}ms write={result.write_elapsed_ms}ms"
            + (f" error={result.error}" if result.error else "")
        )
    print(
        f"\n[ingest] 汇总：documents={report.documents_total} imported={report.documents_imported} "
        f"skipped={report.documents_skipped} failed={report.documents_failed} "
        f"chunks={report.chunks_created} embeddings={report.embeddings_created} "
        f"total={report.total_elapsed_ms}ms"
    )
    print(f"[ingest] 索引现状：{json.dumps(stats, ensure_ascii=False)}")

    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        payload = {"report": report.as_dict(), "index_stats": stats, "database": db_name}
        args.json_report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[ingest] 报告写入 {args.json_report}")

    return 0 if report.documents_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
