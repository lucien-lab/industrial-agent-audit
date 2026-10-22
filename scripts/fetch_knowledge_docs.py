#!/usr/bin/env python
"""获取并核验知识库文档（阶段 2）。

行为：
1. 读取 `knowledge/sources.json`（唯一来源清单，含 URL、SHA-256、适用范围、许可）；
2. 文件已存在且 SHA-256 匹配 → 跳过下载（不重复下载）；
3. 缺失 → 按 `download_url` 下载到 `file_path`，然后**强制核验** SHA-256；
4. SHA-256 不匹配 → 退出码 2，不静默接受被替换的文件；
5. `--verify-only` 只核验不下载。

不修改 `data/raw/metropt3/` 中的既有文件；MetroPT-3 官方说明直接复用已核验副本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCES_FILE = PROJECT_ROOT / "knowledge" / "sources.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sources() -> list[dict]:
    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise SystemExit("sources.json 缺少 documents 列表")
    return documents


def download(url: str, destination: Path) -> None:
    """下载到目标路径。优先 urllib，失败时回退到 curl（本机 TLS 环境下 urllib 偶发 EOF）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _download_urllib(url, destination)
    except (urllib.error.URLError, OSError) as exc:
        print(f"[fetch] urllib 失败（{type(exc).__name__}: {exc}），回退 curl", file=sys.stderr)
        _download_curl(url, destination)


def _download_urllib(url: str, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "industrial-agent-audit/phase2 (+local research)"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        with temporary.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
    temporary.replace(destination)


def _download_curl(url: str, destination: Path) -> None:
    import subprocess

    temporary = destination.with_suffix(destination.suffix + ".part")
    completed = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--max-time",
            "300",
            "--output",
            str(temporary),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"curl 下载失败：{completed.stderr.strip()[:200]}")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并核验知识库文档")
    parser.add_argument("--verify-only", action="store_true", help="只核验，不下载")
    parser.add_argument("--json", type=Path, help="把核验结果写入 JSON")
    args = parser.parse_args()

    results = []
    failures = 0
    for doc in load_sources():
        path = PROJECT_ROOT / doc["file_path"]
        expected = doc["file_sha256"].lower()
        status = "present"
        try:
            if not path.is_file():
                if args.verify_only:
                    status = "missing"
                    failures += 1
                else:
                    if not doc.get("download_url"):
                        raise RuntimeError("缺少 download_url，无法自动下载")
                    print(f"[fetch] {doc['doc_key']} ← {doc['download_url']}", file=sys.stderr)
                    download(doc["download_url"], path)
                    status = "downloaded"
            if path.is_file():
                actual = sha256_file(path)
                if actual != expected:
                    status = "sha256_mismatch"
                    failures += 1
                elif status == "present":
                    status = "verified"
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            status = f"error: {type(exc).__name__}: {exc}"
            failures += 1
        entry = {
            "doc_key": doc["doc_key"],
            "path": str(path.relative_to(PROJECT_ROOT)),
            "bytes": path.stat().st_size if path.is_file() else None,
            "sha256": expected,
            "status": status,
        }
        results.append(entry)
        print(f"{status:16s} {entry['path']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"documents": results, "failures": failures}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if failures:
        print(f"核验失败 {failures} 份文档", file=sys.stderr)
        return 2
    print(f"全部 {len(results)} 份文档核验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
