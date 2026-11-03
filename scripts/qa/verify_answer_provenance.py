#!/usr/bin/env python3
"""四六级听力黑匣子 · 答案与原文来源核对（C 负责，任务书 11 节）。

任务书第 11 节原文：
    「参考答案来源与原文来源由 C 核对，B 保存，A 展示。未知答案保留未知状态。」

这句话把核对责任明确给了 C，但 C 之前只给题目打了 `answerSource: "user_provided_pdf"`
这个标签，**从未真的把答案与来源文档逐题对过**。这个脚本补上。

它做三层核对：
    第 1 层 · 答案对不对得上来源文档
        解析原始答案解析文档里的「【答案】 B」，与契约的 answer 索引比对。
    第 2 层 · 证据句是否真的支撑答案
        每题引用的 evidenceRef 必须存在、文字能读、且落在材料时间轴内。
    第 3 层 · 未知答案是否被正确保留
        answerSource=unknown 的题必须 answer=null、correct=null、无证据句，
        **不允许被推断填充**。

设计取舍：
    第 2 层不做「语义是否真的支撑」的自动判定 —— 那需要人去读。
    脚本只保证「引用的句子真实存在且文字可读」，把语义判断留给人工，
    并在输出里把证据句原文打出来，方便一眼核对。

用法：
    .venv/bin/python scripts/qa/verify_answer_provenance.py
    .venv/bin/python scripts/qa/verify_answer_provenance.py --source <文档路径>
    .venv/bin/python scripts/qa/verify_answer_provenance.py --json

退出码：0 通过｜1 有不一致｜2 来源文档不可用
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "cet"
MATERIAL = FIXTURES / "material_cet6-202512-conv2.json"
QUESTIONS_SRC = FIXTURES / "source" / "cet6-202512-conv2.questions.json"
ATTEMPT = FIXTURES / "attempt_result.json"

# 答案来源：优先用入库的自包含记录，回退到 C 本机的原始解析文档。
#
# 为什么改：原先只依赖 C 本机的一个绝对路径，而该文件**不在 git 里** ——
# 外部机器上这个工具必然失败，而它检查的正是任务书 11 节要求的「答案来源核对」。
# 交付包必须能独立验证，不能依赖某台机器的私有文件。
BUNDLED_KEY = FIXTURES / "source" / "cet6-202512-conv2.answer-key.json"
LOCAL_SOURCE = Path(
    "/Users/a1111/Documents/ChatGPT/四六级Skill升级/desktop/.acceptance/"
    "cet6-202512/conversation-two-answers.txt"
)
DEFAULT_SOURCE = BUNDLED_KEY if BUNDLED_KEY.is_file() else LOCAL_SOURCE

LETTER_TO_INDEX = {"A": 0, "B": 1, "C": 2, "D": 3}


def parse_source(path: Path) -> dict[int, str]:
    """从答案来源里抽 {题号: 答案字母}。

    支持两种输入：
    - 入库的自包含 JSON（`source/*.answer-key.json`）—— 交付包用这个
    - C 本机的原始解析文本（回退）—— 格式形如
          5. A) They have got their work done.
          ...
          【答案】 B （细节题） They both look tired
    """
    if path.suffix == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
        return {int(a["number"]): a["answerLetter"] for a in doc.get("answers", [])}
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n(?=\d+\.\s*[A-D]\))", text)
    out: dict[int, str] = {}
    for block in blocks:
        m_num = re.search(r"^(\d+)\.\s*[A-D]\)", block, re.M)
        m_ans = re.search(r"【答案】\s*([A-D])", block)
        if m_num and m_ans:
            out[int(m_num.group(1))] = m_ans.group(1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="答案与原文来源核对")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="答案解析文档路径")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    problems: list[str] = []
    report: dict = {"layers": {}}

    # 载入契约
    try:
        material = json.loads(MATERIAL.read_text(encoding="utf-8"))
        questions = json.loads(QUESTIONS_SRC.read_text(encoding="utf-8"))["questions"]
        attempts = json.loads(ATTEMPT.read_text(encoding="utf-8"))["items"]
    except Exception as exc:  # noqa: BLE001
        print(f"X  契约样例读取失败：{exc}", file=sys.stderr)
        return 2

    sentences = {s["sentenceId"]: s for s in material["sentences"]}
    duration = material["material"]["durationSec"]

    # ── 第 1 层：答案 vs 来源文档 ──
    src_path = Path(args.source).expanduser()
    layer1: list[dict] = []
    if src_path.is_file():
        source_answers = parse_source(src_path)
        for q in questions:
            n = q["number"]
            if q.get("answerSource") == "unknown":
                layer1.append({"number": n, "skipped": True, "reason": "unknown 来源"})
                continue
            letter = source_answers.get(n)
            expected = LETTER_TO_INDEX.get(letter) if letter else None
            match = (q["answer"] == expected)
            layer1.append({
                "number": n,
                "contractAnswer": q["answer"],
                "sourceLetter": letter,
                "sourceIndex": expected,
                "match": match,
            })
            if not match:
                problems.append(
                    f"第 {n} 题答案与来源文档不一致：契约 {q['answer']}，"
                    f"来源 {letter}（索引 {expected}）"
                )
        report["layers"]["answerVsSource"] = {
            "sourceFile": str(src_path),
            "parsed": len(source_answers),
            "rows": layer1,
        }
    else:
        report["layers"]["answerVsSource"] = {"skipped": True, "reason": "来源文档不可用"}
        problems.append(f"来源文档不可用：{src_path}")

    # ── 第 2 层：证据句是否真实可读 ──
    layer2: list[dict] = []
    for item in attempts:
        ev = item.get("evidenceRef")
        row = {"number": item["number"], "hasEvidence": bool(ev)}
        if ev:
            sid = ev["sentenceId"]
            real = sentences.get(sid)
            row["sentenceId"] = sid
            if not real:
                problems.append(f"第 {item['number']} 题引用了不存在的句子 {sid}")
                row["ok"] = False
            else:
                in_range = (
                    0 <= float(ev["startSec"]) < float(ev["endSec"]) <= duration + 0.001
                )
                # 引用的时间必须与该句真实时间一致
                time_match = (
                    abs(float(ev["startSec"]) - real["startSec"]) < 0.01
                    and abs(float(ev["endSec"]) - real["endSec"]) < 0.01
                )
                text_match = str(ev.get("quote", "")).strip() == real["text"].strip()
                row.update({
                    "inRange": in_range,
                    "timeMatchesSentence": time_match,
                    "quoteMatchesSentence": text_match,
                    "ok": in_range and time_match and text_match,
                    "quote": real["text"],
                })
                if not in_range:
                    problems.append(f"第 {item['number']} 题证据时间越界")
                if not time_match:
                    problems.append(f"第 {item['number']} 题证据时间与该句不一致")
                if not text_match:
                    problems.append(f"第 {item['number']} 题证据引文与该句原文不一致")
        layer2.append(row)
    report["layers"]["evidenceValidity"] = layer2

    # ── 第 3 层：未知答案必须保留 unknown ──
    layer3: list[dict] = []
    for item in attempts:
        if item.get("answerSource") != "unknown":
            continue
        row = {
            "number": item["number"],
            "answerIsNull": item.get("answer") is None,
            "correctIsNull": item.get("correct") is None,
            "noEvidence": not item.get("evidenceRef"),
        }
        row["ok"] = row["answerIsNull"] and row["correctIsNull"] and row["noEvidence"]
        layer3.append(row)
        if not row["ok"]:
            problems.append(
                f"第 {item['number']} 题来源为 unknown 但被填充了答案或判题结果"
            )
    report["layers"]["unknownPreserved"] = layer3

    report["problems"] = problems
    report["ok"] = not problems

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not problems else 1

    # ── 人读输出 ──
    print("四六级听力黑匣子 · 答案与原文来源核对")
    print(f"材料 {material['material']['materialId']} / {material['material']['materialVersion']}")
    print("-" * 74)

    print("\n【第 1 层】答案 vs 来源文档")
    l1 = report["layers"]["answerVsSource"]
    if l1.get("skipped"):
        print(f"  跳过：{l1['reason']}")
    else:
        print(f"  来源：{Path(l1['sourceFile']).name}（解析到 {l1['parsed']} 题答案）")
        for r in l1["rows"]:
            if r.get("skipped"):
                print(f"    第 {r['number']} 题：{r['reason']}，不比对")
                continue
            mark = "OK" if r["match"] else "X "
            print(f"    [{mark}] 第 {r['number']} 题  契约索引 {r['contractAnswer']}"
                  f"  ← 来源 {r['sourceLetter']}（索引 {r['sourceIndex']}）")

    print("\n【第 2 层】证据句是否真实可读")
    for r in layer2:
        if not r["hasEvidence"]:
            print(f"    [--] 第 {r['number']} 题：无证据句（unknown 来源或无引用）")
            continue
        mark = "OK" if r["ok"] else "X "
        print(f"    [{mark}] 第 {r['number']} 题 → {r['sentenceId']}")
        if r.get("quote"):
            print(f"         「{r['quote'][:66]}」")

    print("\n【第 3 层】未知答案是否保留")
    if not layer3:
        print("    无 unknown 来源题目（本材料应有 1 道）")
    for r in layer3:
        mark = "OK" if r["ok"] else "X "
        print(f"    [{mark}] 第 {r['number']} 题：answer=null {r['answerIsNull']}，"
              f"correct=null {r['correctIsNull']}，无证据 {r['noEvidence']}")

    print("-" * 74)
    if problems:
        print(f"结论：{len(problems)} 处不一致")
        for p in problems:
            print(f"  · {p}")
        return 1
    print("结论：三层核对全部通过")
    print("\n说明：第 2 层只保证「引用的句子真实存在且文字一致」，")
    print("      「证据是否真的能推出答案」属语义判断，需人工读上面打出的原文。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
