"""获取 MetroPT-3 官方原始数据并登记完整性哈希。

行为：
1. 若 data/raw/metropt3/ 已有 CSV 与 PDF，则只校验哈希，不重新下载。
2. 缺失时才从 UCI 官方地址下载压缩包（流式写入临时文件）。
3. 解压后计算 SHA-256，写入 manifest.json（含来源、时间、字节数、哈希）。
4. 不修改、不改写原始文件内容。

用法：
    python scripts/fetch_metropt.py --check   # 只校验
    python scripts/fetch_metropt.py           # 缺失则下载，然后校验
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone

from metropt_common import (
    CSV_NAME,
    CSV_PATH,
    DATASET_PAGE,
    DATASET_URL,
    DOI,
    LICENSE,
    PDF_NAME,
    PDF_PATH,
    PROCESSED_DIR,
    PROJECT_ROOT,
    RAW_DIR,
    ZIP_NAME,
)

MANIFEST_PATH = RAW_DIR / "manifest.json"
CHUNK = 1 << 20


def sha256_of(path, chunk: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def download_zip(target) -> None:
    print(f"下载 {DATASET_URL}")
    req = urllib.request.Request(DATASET_URL, headers={"User-Agent": "industrial-agent-audit/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as out:
        total = 0
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            out.write(block)
            total += len(block)
            if total % (20 * CHUNK) < CHUNK:
                print(f"  已接收 {total / 1e6:.1f} MB", flush=True)
    print(f"  下载完成：{total / 1e6:.1f} MB")


def extract_members(zip_path) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for member in (CSV_NAME, PDF_NAME):
            if member not in names:
                raise RuntimeError(f"压缩包缺少预期文件 {member}，实际内容：{names}")
            # 直接解压到目标目录，不改写内容
            with zf.open(member) as src, open(RAW_DIR / member, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            print(f"  解压 {member}")


def build_manifest(zip_sha: str | None, downloaded: bool) -> dict:
    files = {}
    for path in (CSV_PATH, PDF_PATH):
        files[path.name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_of(path),
        }
    return {
        "dataset": "MetroPT-3 (Air Compressor / APU)",
        "license": LICENSE,
        "doi": DOI,
        "citation_page": DATASET_PAGE,
        "download_url": DATASET_URL,
        "archive": {"name": ZIP_NAME, "sha256": zip_sha, "downloaded_this_run": downloaded},
        "files": files,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": [
            "原始文件只读保存，任何预处理产物写入 data/processed/。",
            "SHA-256 用于复现与篡改检测，不用于推断数据质量。",
        ],
    }


def load_manifest() -> dict | None:
    if not MANIFEST_PATH.exists():
        return None
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def verify(manifest: dict) -> bool:
    ok = True
    for path in (CSV_PATH, PDF_PATH):
        recorded = manifest.get("files", {}).get(path.name)
        if not path.exists():
            print(f"FAIL 缺失：{path}")
            ok = False
            continue
        actual = sha256_of(path)
        size = path.stat().st_size
        if recorded and recorded.get("sha256") != actual:
            print(f"FAIL 哈希不一致：{path.name}")
            print(f"     记录 {recorded.get('sha256')}")
            print(f"     实际 {actual}")
            ok = False
        elif recorded and recorded.get("bytes") != size:
            print(f"FAIL 字节数不一致：{path.name}（记录 {recorded.get('bytes')}，实际 {size}）")
            ok = False
        else:
            print(f"OK   {path.name}  {size:,} 字节  sha256={actual}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="获取并校验 MetroPT-3 原始数据")
    parser.add_argument("--check", action="store_true", help="只校验本地文件，不下载")
    args = parser.parse_args(argv)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    have_both = CSV_PATH.exists() and PDF_PATH.exists()
    downloaded = False
    zip_sha = None

    if have_both:
        print("原始文件已存在，跳过下载。")
    elif args.check:
        print("原始文件缺失，且指定了 --check，未执行下载。需要：")
        for path in (CSV_PATH, PDF_PATH):
            if not path.exists():
                print(f"  - {path}")
        return 2
    else:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = f"{tmp}/{ZIP_NAME}"
            download_zip(zip_path)
            zip_sha = sha256_of(zip_path)
            print(f"  压缩包 sha256={zip_sha}")
            extract_members(zip_path)
        downloaded = True

    manifest = load_manifest()
    if manifest is None or downloaded:
        manifest = build_manifest(zip_sha, downloaded)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写入 {MANIFEST_PATH.relative_to(PROJECT_ROOT)}")

    ok = verify(manifest)
    print("完整性校验：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
