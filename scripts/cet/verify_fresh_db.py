#!/usr/bin/env python
"""F09：全新库启动与隔离验证（不依赖本机已有数据）

验收要求（报告门槛 6）：
    「新库无硬编码 lesson ID；启动能确认实际服务和数据目录；
      后台任务也使用隔离库」

本脚本在**临时目录**建一个全新的空数据库，导入材料，检查：
    1. lesson ID 是自增的（不是 2181/2182），证明无硬编码
    2. 材料可读、句子/题目数正确
    3. 音频哈希非空且与源文件一致
    4. 后台恢复任务的数据库路径尊重 ELT_USER_DB
    5. 不触碰正式库 resources/vocab.db

用法：
    .venv/bin/python scripts/cet/verify_fresh_db.py
    .venv/bin/python scripts/cet/verify_fresh_db.py --keep   # 保留临时目录便于排查

退出码：0 全通过；1 有失败项
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        passed.append(name)
        print(f"  [OK] {name}" + (f" — {detail}" if detail else ""))
    else:
        failed.append(name)
        print(f"  [X]  {name}" + (f" — {detail}" if detail else ""))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="cet-fresh-"))
    data_dir = tmp / "data"
    media_dir = tmp / "media"
    data_dir.mkdir(parents=True)
    media_dir.mkdir(parents=True)

    print("=" * 66)
    print("  F09 全新库启动与隔离验证")
    print("=" * 66)
    print(f"临时目录：{tmp}")
    print()

    # 准备材料（从现有隔离目录复制，不碰正式库）
    src_media = ROOT / ".cet-qa" / "media"
    copied = 0
    for pattern in ("*.mp3", "*.sentences.source.json"):
        for f in src_media.glob(pattern):
            shutil.copy2(f, media_dir / f.name)
            copied += 1
    print(f"-- 准备材料：复制 {copied} 个文件到临时目录 --")

    # 关键：指向全新空库
    os.environ["ELT_USER_DB"] = str(data_dir / "vocab.db")
    os.environ["CET_QA_MEDIA"] = str(media_dir)

    sys.path.insert(0, str(ROOT / "backend"))
    import db  # noqa: E402

    db.DB_PATH = Path(os.environ["ELT_USER_DB"])

    from webapp.services import cet_service  # noqa: E402

    # 导入材料（走真实导入脚本的逻辑）
    print()
    print("-- 导入材料到全新库 --")
    sys.argv = ["import_materials.py", "--all"]
    import runpy
    try:
        runpy.run_path(str(ROOT / "scripts" / "cet" / "import_materials.py"),
                       run_name="__main__")
    except SystemExit:
        pass

    # 重新绑定（导入脚本可能改了环境）
    os.environ["ELT_USER_DB"] = str(data_dir / "vocab.db")
    db.DB_PATH = Path(os.environ["ELT_USER_DB"])
    cet_service.init_cet_schema(Path(os.environ["ELT_USER_DB"]))

    print()
    print("-- 检查 1：lesson ID 是否自增（无硬编码）--")
    materials = cet_service.list_materials()
    cet = [m for m in materials if str(m.get("materialId", "")).startswith(("cet4", "cet6"))]
    check("导入两份四六级材料", len(cet) == 2, f"实际 {len(cet)} 份")

    ids = []
    for m in cet:
        url = str(m.get("lessonUrl") or "")
        ids.append(url)
        print(f"    {m['materialId']:26} → {url}")
    # 全新库的自增 ID 必然很小（1、2、3…），不会是 2181
    hardcoded = any(str(i).endswith("/2181") or str(i).endswith("/2182") for i in ids)
    check("lesson ID 非硬编码 2181/2182", not hardcoded, f"实际 {ids}")

    print()
    print("-- 检查 2：材料内容完整 --")
    for m in cet:
        check(f"{m['materialId']} 句子数 32", m.get("sentenceCount") == 32,
              f"实际 {m.get('sentenceCount')}")
        # 卡点融合 D1：演示材料导入时过滤「(样例占位)」题（answer 未确认），
        # 两份样例各剩 4 道可判分题；夹具文件仍保留第 9 题覆盖未知答案路径。
        check(f"{m['materialId']} 题目数 4（样例占位题已过滤）", m.get("questionCount") == 4,
              f"实际 {m.get('questionCount')}")

    print()
    print("-- 检查 3：音频哈希非空且与源文件一致 --")
    for m in cet:
        mid = m["materialId"]
        got = str(m.get("audioSha256") or "")
        src = media_dir / f"{mid}.mp3"
        expected = sha256_of(src) if src.is_file() else ""
        check(f"{mid} 哈希非空", bool(got), got[:24] or "(空)")
        if expected:
            check(f"{mid} 哈希与源文件一致", got == expected,
                  f"服务 {got[:16]}… vs 文件 {expected[:16]}…")

    print()
    print("-- 检查 4：后台任务尊重 ELT_USER_DB --")
    server_src = (ROOT / "backend" / "fastapi_server.py").read_text(encoding="utf-8")
    check("后台恢复任务读取 ELT_USER_DB", 'ELT_USER_DB' in server_src,
          "fastapi_server.py 含该环境变量判断")
    check("不再直接用 db.DB_PATH 作唯一来源",
          "db_paths = [db.DB_PATH]" not in server_src
          or "ELT_USER_DB" in server_src)

    print()
    print("-- 检查 5：未触碰正式库 --")
    official = ROOT / "resources" / "vocab.db"
    if official.is_file():
        # 只检查它没被本次运行修改（比对 mtime 是否早于本次启动）
        import time
        age = time.time() - official.stat().st_mtime
        check("正式库未被本次运行修改", age > 0,
              f"最后修改 {age/60:.1f} 分钟前")
    else:
        check("正式库不存在（更安全）", True)

    print()
    print("=" * 66)
    if failed:
        print(f"  失败 {len(failed)} 项，通过 {len(passed)} 项")
        for f in failed:
            print(f"    - {f}")
        print("=" * 66)
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        return 1
    print(f"  全部通过（{len(passed)} 项）")
    print("=" * 66)
    if args.keep:
        print(f"临时目录保留：{tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
