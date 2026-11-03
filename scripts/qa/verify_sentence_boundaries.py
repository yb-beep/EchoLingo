#!/usr/bin/env python3
"""四六级听力黑匣子 · 句边界机器交叉复核（C 负责）。

解决什么问题：
    契约里的 32 条句边界来自旧项目对齐产物，来源标注是「程序覆盖检查」，
    **从未与音频内容做过独立比对**。任务书要求把「页面可打开」与
    「人耳听到正确原声」分开记录，而「边界是否落在真实语音上」在两者之间。

    这个脚本用 Whisper 对同一段音频做一次**独立转写**（带词级时间戳），
    再与契约时间轴交叉比对，回答两个机器能答的问题：
      1. 契约那句话范围内的语音，是不是真的在说这句话？（文字吻合）
      2. 契约的起止点是否落在该句首词/末词上？（边界吻合）

它不能替代什么：
    **不能替代人耳（H1）**。Whisper 也会听错、也会切错。
    它能做的是把「明显切歪」和「内容对不上」筛出来，
    把需要人耳复核的范围从 32 条缩到 0-2 条。

用法：
    .venv/bin/python scripts/qa/verify_sentence_boundaries.py
    .venv/bin/python scripts/qa/verify_sentence_boundaries.py --model small   # 更快
    .venv/bin/python scripts/qa/verify_sentence_boundaries.py --json

前置：faster-whisper 已装、模型已缓存（large-v3 已在本机就绪）。
耗时：large-v3 在 CPU 上约 80 秒 / 105 秒音频。

退出码：0 全部吻合｜1 有需人工复核的句子｜2 环境不满足
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATERIAL = ROOT / "tests" / "fixtures" / "cet" / "material_cet6-202512-conv2.json"
MANIFEST = ROOT / "tests" / "fixtures" / "cet" / "manifest.json"
AUDIO = ROOT / ".cet-qa" / "media" / "cet6-202512-conv2.mp3"
CACHE = ROOT / ".cet-qa" / "whisper_words.json"

# 判定阈值（保守：宁可多报待复核，不可漏掉切歪）
TEXT_HIT_MIN = 0.70        # 契约句的词有多少出现在该时间范围内的识别文字里
START_TOL_SEC = 0.45       # 起点偏差容忍
END_TOL_SEC = 0.60         # 终点偏差容忍（句尾常有拖音）

# 数字与口语变体归一化：whisper 常写 4 而原文写 four，OK 与 Okay 互见
NUM_WORDS = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "20": "twenty", "40": "forty",
}
ALT_FORMS = {"ok": "okay", "okay": "okay", "yes": "yeah"}


def normalize_words(text: str) -> list[str]:
    t = text.lower().replace("—", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    out = []
    for w in t.split():
        w = NUM_WORDS.get(w, w)
        w = ALT_FORMS.get(w, w)
        out.append(w)
    return out


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def transcribe(model_name: str, force: bool) -> list[dict]:
    """转写并缓存词级时间戳。缓存避免每次重跑都等 80 秒。"""
    if CACHE.exists() and not force:
        print(f"（复用已缓存的转写结果 {CACHE.name}，加 --refresh 可重算）")
        return json.loads(CACHE.read_text(encoding="utf-8"))

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("X  未安装 faster-whisper。先：.venv/bin/pip install -r requirements.txt",
              file=sys.stderr)
        raise SystemExit(2)

    print(f"用 {model_name} 转写中（CPU 上约 1-2 分钟）…")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(AUDIO), language="en", word_timestamps=True, vad_filter=False
    )
    segs = list(segments)
    print(f"完成：{len(segs)} 段，语言={info.language}（概率 {info.language_probability:.2f}）")

    payload = []
    for s in segs:
        payload.append({
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text.strip(),
            "words": [
                {"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3)}
                for w in (s.words or [])
            ],
        })
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="句边界机器交叉复核")
    parser.add_argument("--model", default="large-v3", help="whisper 模型名")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存重新转写")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if not AUDIO.is_file():
        print(f"X  缺少音频 {AUDIO}\n   先跑 scripts/qa/prepare_audio.py", file=sys.stderr)
        return 2
    if not MATERIAL.is_file():
        print(f"X  缺少契约样例 {MATERIAL}", file=sys.stderr)
        return 2

    contract = json.loads(MATERIAL.read_text(encoding="utf-8"))["sentences"]
    words = sorted(
        [w for s in transcribe(args.model, args.refresh) for w in s["words"]],
        key=lambda w: w["s"],
    )
    if not words:
        print("X  转写没有返回词级时间戳，无法比对", file=sys.stderr)
        return 2

    rows = []
    for c in contract:
        cs, ce = c["startSec"], c["endSec"]
        target = normalize_words(c["text"])
        inside = [w for w in words if overlap(cs, ce, w["s"], w["e"]) > 0.02]
        seen = set(normalize_words(" ".join(w["w"] for w in inside)))
        hit_ratio = (sum(1 for w in target if w in seen) / len(target)) if target else 0.0
        d_start = round(inside[0]["s"] - cs, 3) if inside else None
        d_end = round(inside[-1]["e"] - ce, 3) if inside else None
        text_ok = hit_ratio >= TEXT_HIT_MIN
        boundary_ok = (
            bool(inside)
            and abs(d_start) <= START_TOL_SEC
            and abs(d_end) <= END_TOL_SEC
        )
        rows.append({
            "sentenceId": c["sentenceId"],
            "startSec": cs,
            "endSec": ce,
            "text": c["text"],
            "textHitRatio": round(hit_ratio, 3),
            "startDelta": d_start,
            "endDelta": d_end,
            "textOk": text_ok,
            "boundaryOk": boundary_ok,
        })

    text_pass = sum(1 for r in rows if r["textOk"])
    bound_pass = sum(1 for r in rows if r["boundaryOk"])
    suspects = [r for r in rows if not (r["textOk"] and r["boundaryOk"])]

    if args.json:
        print(json.dumps({
            "audio": str(AUDIO),
            "sentences": len(rows),
            "textOk": text_pass,
            "boundaryOk": bound_pass,
            "thresholds": {
                "textHitMin": TEXT_HIT_MIN,
                "startTolSec": START_TOL_SEC,
                "endTolSec": END_TOL_SEC,
            },
            "suspects": suspects,
            "rows": rows,
        }, ensure_ascii=False, indent=2))
        return 0 if not suspects else 1

    print("\n四六级听力黑匣子 · 句边界机器交叉复核")
    print(f"音频 {AUDIO.name}｜契约 {len(rows)} 句｜独立转写 {len(words)} 词")
    print("-" * 76)
    print(f"{'句':<5}{'契约范围':<19}{'文字命中':<10}{'起差':<9}{'止差':<9}判定")
    print("-" * 76)
    for r in rows:
        verdict = ("OK" if r["textOk"] and r["boundaryOk"]
                   else "文字存疑" if not r["textOk"] else "边界存疑")
        ds = f"{r['startDelta']:+.2f}" if r["startDelta"] is not None else "  n/a"
        de = f"{r['endDelta']:+.2f}" if r["endDelta"] is not None else "  n/a"
        print(f"{r['sentenceId']:<5}{r['startSec']:6.2f}-{r['endSec']:6.2f}   "
              f"{r['textHitRatio']*100:5.0f}%   {ds:>6}   {de:>6}   {verdict}")
    print("-" * 76)
    print(f"文字吻合 {text_pass}/{len(rows)}｜边界吻合 {bound_pass}/{len(rows)}")

    if suspects:
        print(f"\n需人工复核 {len(suspects)} 条：")
        for r in suspects:
            print(f"  {r['sentenceId']}  {r['startSec']:.2f}-{r['endSec']:.2f}  "
                  f"命中{r['textHitRatio']*100:.0f}%  「{r['text'][:56]}…」")
    else:
        print("\n全部吻合：契约时间轴与独立转写一致。")

    print("\n提醒：本脚本不能替代人耳（H1）。Whisper 也会听错、切错，")
    print("      它的价值是把「明显切歪」筛出来，缩小需要人耳复核的范围。")
    return 0 if not suspects else 1


if __name__ == "__main__":
    sys.exit(main())
