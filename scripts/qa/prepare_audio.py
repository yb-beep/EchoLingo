#!/usr/bin/env python3
"""四六级听力黑匣子 · 共同样例音频准备与核对（C 负责）。

为什么单独一个脚本：
    样例音频按仓库 .gitignore **不入库**（版权与仓库体积考虑），
    所以「新克隆的仓库第一次启动」时一定缺音频。
    D3 要在另一台电脑验收，这一步必须能独立完成且能验证「是不是同一段」。

它做什么：
    1. 在候选位置里找音频，逐个核对 SHA-256
    2. 找到哈希正确的那份就用它（不复制、不移动，保持原文件不动）
    3. 找不到就给出**具体**的获取方式与环境变量写法
    4. 支持 --copy-from 显式从某个路径复制过来（需哈希匹配才接受）

为什么强调哈希：
    三人各自有音频。若用了不同版本（不同音质、不同切分），
    时间轴就对不上，回听会偏。哈希一致才证明是同一段。

用法：
    .venv/bin/python scripts/qa/prepare_audio.py                    # 查找并核对
    .venv/bin/python scripts/qa/prepare_audio.py --copy-from /path/to/full.mp3
    .venv/bin/python scripts/qa/prepare_audio.py --json

退出码：0 已就绪｜1 未就绪（缺音频或哈希不符）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "fixtures" / "cet" / "manifest.json"
TARGET_DIR = ROOT / ".cet-qa" / "media"

# 已知可能存放样例音频的位置。新机器上多半都不存在 —— 那就如实报缺。
CANDIDATES = [
    # C 的孤立数据目录
    TARGET_DIR / "cet6-202512-conv2.mp3",
    # 旧项目的验收产物（本机来源）
    Path("/Users/a1111/Documents/ChatGPT/四六级Skill升级/desktop/.acceptance/"
         "cet6-202512/conversation-two/full.mp3"),
    # 用户 Downloads 里的整卷音频（需重新切分才能得到 Conversation Two，不算直接可用）
]


def load_manifest() -> dict:
    with MANIFEST.open(encoding="utf-8") as fh:
        return json.load(fh)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path, expected: str) -> dict:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    try:
        digest = sha256_of(path)
    except OSError as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    return {
        "path": str(path),
        "exists": True,
        "sha256": digest,
        "match": digest == expected,
        "size": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="共同样例音频准备与核对")
    parser.add_argument("--copy-from", metavar="PATH", help="从指定路径复制（须哈希匹配）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    manifest = load_manifest()
    expected = manifest["audio"]["sha256"]
    hint = manifest["audio"]["fileNameHint"]
    target = TARGET_DIR / hint

    # 允许环境变量覆盖目标位置
    env_media = os.environ.get("CET_QA_MEDIA")
    if env_media:
        target = Path(env_media)

    report = {
        "expectedSha256": expected,
        "fileName": hint,
        "target": str(target),
        "ready": False,
        "candidates": [],
        "action": "",
    }

    # 显式复制
    if args.copy_from:
        src = Path(args.copy_from).expanduser()
        info = probe(src, expected)
        if not info.get("exists"):
            report["action"] = f"源文件不存在：{src}"
            _emit(report, args.json)
            return 1
        if not info.get("match"):
            report["action"] = (
                f"源文件哈希不符：{info.get('sha256', '')[:16]}… ≠ {expected[:16]}…。"
                "三人必须用同一段音频，不要将就。"
            )
            report["candidates"].append(info)
            _emit(report, args.json)
            return 1
        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        report["ready"] = True
        report["action"] = f"已从 {src} 复制到 {target}（哈希已核对）"
        report["candidates"].append(probe(target, expected))
        _emit(report, args.json)
        return 0

    # 逐个探测候选
    seen = set()
    for cand in CANDIDATES:
        info = probe(cand, expected)
        info["isTarget"] = str(cand) == str(target)
        if info.get("exists") and info.get("sha256"):
            if info["sha256"] in seen:
                continue
            seen.add(info["sha256"])
        report["candidates"].append(info)

    # 找到哈希正确的
    good = [c for c in report["candidates"] if c.get("match")]
    at_target = [c for c in good if c["path"] == str(target)]
    if at_target:
        # 目标位置本身就有正确文件 —— 真正就绪
        report["ready"] = True
        report["action"] = "目标位置已有正确音频，无需处理"
        _emit(report, args.json)
        return 0
    if good:
        # 只是别处有正确文件，目标位置还没有 —— 不算就绪，要复制
        report["ready"] = False
        report["action"] = (
            f"别处找到哈希正确的音频：{good[0]['path']}\n"
            f"   但目标位置还没有。执行下面这行即可就绪：\n"
            f"   scripts/qa/prepare_audio.py --copy-from \"{good[0]['path']}\""
        )
        _emit(report, args.json)
        return 1

    # 有文件但哈希不符
    wrong = [c for c in report["candidates"] if c.get("exists") and not c.get("match")]
    if wrong:
        report["action"] = (
            "找到文件但哈希不符 —— 不是同一段音频，不能用于联调。"
            f"期望 {expected}"
        )
    else:
        report["action"] = _missing_help(expected, hint, target)

    _emit(report, args.json)
    return 1


def _missing_help(expected: str, hint: str, target: Path) -> str:
    return (
        f"未找到样例音频 {hint}。\n"
        f"  期望 SHA-256：{expected}\n"
        f"  目标位置：{target}\n"
        "\n"
        "获取方式（任选其一）：\n"
        "  A. 向 C 索取该文件（三人必须用同一段，靠哈希确认）\n"
        "  B. 从本机旧项目取（若存在）：\n"
        "     scripts/qa/prepare_audio.py --copy-from \\\n"
        '       "/Users/a1111/Documents/ChatGPT/四六级Skill升级/desktop/.acceptance/'
        'cet6-202512/conversation-two/full.mp3"\n'
        "  C. 已有同源文件放在别处：\n"
        "     scripts/qa/prepare_audio.py --copy-from /你的/路径/full.mp3\n"
        "     或用环境变量指向：export CET_QA_MEDIA=/你的/路径/full.mp3\n"
        "\n"
        "音频不入库是有意的（版权与体积）。哈希一致才证明是同一段，\n"
        "否则时间轴对不上，回听会偏。"
    )


def _emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print("四六级听力黑匣子 · 共同样例音频")
    print("-" * 66)
    print(f"期望哈希 {report['expectedSha256']}")
    print(f"目标位置 {report['target']}")
    print()
    for c in report["candidates"]:
        p = c["path"]
        if not c.get("exists"):
            print(f"  [--] {p}")
            continue
        if c.get("error"):
            print(f"  [??] {p}  读取失败：{c['error']}")
            continue
        flag = "OK" if c.get("match") else "X "
        size = f"{c.get('size', 0):,}"
        print(f"  [{flag}] {p}  ({size} 字节)")
    print("-" * 66)
    print(("就绪" if report["ready"] else "未就绪") + "：" + report["action"])


if __name__ == "__main__":
    sys.exit(main())
