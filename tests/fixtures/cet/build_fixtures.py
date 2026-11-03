#!/usr/bin/env python3
"""生成四六级黑匣子的共享契约样例（C 负责）。

设计约束（任务书第 7 节）：
- 时间轴一律用原音频绝对秒数；预算与用时一律用毫秒（字段名以 Ms 结尾）。
- 材料身份用稳定 materialId + materialVersion，不使用浏览器本地文件路径。
- 交卷前返回的题目不含参考答案、证据和带练解释；答案只出现在交卷后的样例里。
- 未知答案保留 unknown，不推断、不填充。
- 所有开发样例带 developmentFixture 标识，避免与真实服务结果混淆。

用法：
    python tests/fixtures/cet/build_fixtures.py            # 重新生成全部样例
    python tests/fixtures/cet/build_fixtures.py --check    # 只校验不写盘（CI 用）

输入：
    tests/fixtures/cet/source/cet6-202512-conv2.sentences.json  （已入库的句子时间轴）
    tests/fixtures/cet/source/cet6-202512-conv2.questions.json  （已入库的题目与答案来源）
输出：
    tests/fixtures/cet/materials.json
    tests/fixtures/cet/material_cet6-202512-conv2.json
    tests/fixtures/cet/questions_before_attempt.json
    tests/fixtures/cet/attempt_result.json
    tests/fixtures/cet/marks.json
    tests/fixtures/cet/practice.json
    tests/fixtures/cet/practice_steps.json
    tests/fixtures/cet/review_session.json
    tests/fixtures/cet/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "source"

MATERIAL_ID = "cet6-202512-conv2"
MATERIAL_VERSION = "v1"

# 音频不进 Git：用本地配置映射。样例会记录内容哈希，供三人核对是同一段音频。
AUDIO_SHA256 = "de98e7de987d400cb05fe957f21b5a4fa57112df9676bdb4c3f6214ff8a34259"
AUDIO_FILE_HINT = "cet6-202512-conv2.mp3"
AUDIO_SOURCE_PATH = (
    "旧项目 desktop/.acceptance/cet6-202512/conversation-two/full.mp3"
)

# 两处卡点：模拟用户在播放时按下空格的位置（原始按键秒数，未经吸附）。
# 原始按键秒数与建议句子必须分别保留，不能只留吸附后的结果。
MARK_PRESSES = [
    {
        "markId": "mark-001",
        "pressedAtSec": 36.9,
        "materialVersion": MATERIAL_VERSION,
    },
    {
        "markId": "mark-002",
        "pressedAtSec": 86.0,
        "materialVersion": MATERIAL_VERSION,
    },
]


def load_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(name: str, payload, check_only: bool, written: list) -> None:
    path = HERE / name
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if check_only:
        if not path.exists():
            raise SystemExit(f"[check] 缺少样例文件：{name}")
        current = path.read_text(encoding="utf-8")
        if current != text:
            raise SystemExit(
                f"[check] 样例与生成结果不一致：{name}（请运行 build_fixtures.py 重新生成）"
            )
        return
    path.write_text(text, encoding="utf-8")
    written.append(name)


def build_sentences() -> tuple[dict, list[dict]]:
    src = load_json(SOURCE / "cet6-202512-conv2.sentences.json")
    duration = float(src["durationSec"])
    sentences = []
    for i, s in enumerate(src["sentences"]):
        sentences.append(
            {
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "sentenceId": s["sentenceId"],
                "index": i + 1,
                "startSec": round(float(s["startSec"]), 3),
                "endSec": round(float(s["endSec"]), 3),
                "text": s["text"],
            }
        )
    return src, sentences


def validate_sentences(sentences: list[dict], duration: float) -> None:
    """句子合法性的最低门槛。故意填错时应报出具体句号。"""
    problems = []
    prev_end = -1.0
    seen_ids = set()
    for s in sentences:
        sid = s["sentenceId"]
        if sid in seen_ids:
            problems.append(f"句子 ID 重复：{sid}")
        seen_ids.add(sid)
        start, end = s["startSec"], s["endSec"]
        if start < 0:
            problems.append(f"{sid} 开始时间为负：{start}")
        if end <= start:
            problems.append(f"{sid} 起止时间倒置或为零：{start} → {end}")
        if end > duration + 0.001:
            problems.append(f"{sid} 结束时间越界：{end} > 材料时长 {duration}")
        if start + 0.001 < prev_end:
            problems.append(f"{sid} 与上一句重叠：{prev_end} → {start}")
        if not str(s["text"]).strip():
            problems.append(f"{sid} 原文为空")
        prev_end = end
    if problems:
        raise SystemExit("[sentence] 句子校验失败：\n  - " + "\n  - ".join(problems))


def build_questions(sentences: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回（交卷前、交卷后）两份题目视图。

    交卷前：无 answer、无 evidence、无 explanation。
    交卷后：带 answer、evidenceRef（真实句子 ID）与 explanation。
    """
    src = load_json(SOURCE / "cet6-202512-conv2.questions.json")
    by_sentence = {s["sentenceId"]: s for s in sentences}
    before, after = [], []
    for q in src["questions"]:
        base = {
            "questionId": q["questionId"],
            "number": q["number"],
            "stem": q["stem"],
            "options": q["options"],
            "answerSource": q["answerSource"],
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
        }
        if q.get("developmentFixture"):
            base["developmentFixture"] = True
            base["fixtureNote"] = q.get("fixtureNote", "")
        before.append(dict(base))

        evidence_id = q.get("evidenceSentenceId")
        if evidence_id is not None and evidence_id not in by_sentence:
            raise SystemExit(f"[question] 第 {q['number']} 题引用了不存在的句子：{evidence_id}")
        resolved = {
            **base,
            "answer": q["answer"],
            "answerVisible": True,
            "evidenceRef": (
                {
                    "sentenceId": evidence_id,
                    "startSec": by_sentence[evidence_id]["startSec"],
                    "endSec": by_sentence[evidence_id]["endSec"],
                    "quote": by_sentence[evidence_id]["text"],
                }
                if evidence_id
                else None
            ),
            "explanation": q.get("explanation", ""),
        }
        after.append(resolved)
    return before, after


def validate_questions(before: list[dict], after: list[dict], sentences: list[dict]) -> None:
    problems = []
    numbers = set()
    for q in before:
        if q["number"] in numbers:
            problems.append(f"题号重复：{q['number']}")
        numbers.add(q["number"])
        if not q["options"] or len(q["options"]) < 2:
            problems.append(f"第 {q['number']} 题选项不足")
        if "answer" in q:
            problems.append(f"第 {q['number']} 题在交卷前暴露了 answer 字段")
        if "evidenceRef" in q:
            problems.append(f"第 {q['number']} 题在交卷前暴露了 evidenceRef 字段")
        if len(set(q["options"])) != len(q["options"]):
            problems.append(f"第 {q['number']} 题存在重复选项")
    valid_ids = {s["sentenceId"] for s in sentences}
    for q in after:
        ans = q.get("answer")
        if ans is not None:
            if not isinstance(ans, int) or ans < 0 or ans >= len(q["options"]):
                problems.append(f"第 {q['number']} 题答案索引越界：{ans}")
            if q.get("answerSource") == "unknown":
                problems.append(f"第 {q['number']} 题来源标为 unknown 却带答案")
        else:
            if q.get("answerSource") != "unknown":
                problems.append(
                    f"第 {q['number']} 题答案为 null，来源必须标 unknown，实际 {q.get('answerSource')}"
                )
        ev = q.get("evidenceRef")
        if ev and ev["sentenceId"] not in valid_ids:
            problems.append(f"第 {q['number']} 题证据句子不存在：{ev['sentenceId']}")
    if problems:
        raise SystemExit("[question] 题目校验失败：\n  - " + "\n  - ".join(problems))


def nearest_sentence(sentences: list[dict], pressed: float) -> dict:
    """把原始按键秒数吸附到包含它、或距离最近的句子。"""
    for s in sentences:
        if s["startSec"] <= pressed <= s["endSec"]:
            return s
    return min(sentences, key=lambda s: min(abs(s["startSec"] - pressed), abs(s["endSec"] - pressed)))


def build_marks(sentences: list[dict]) -> list[dict]:
    marks = []
    for i, m in enumerate(MARK_PRESSES):
        sug = nearest_sentence(sentences, m["pressedAtSec"])
        marks.append(
            {
                "markId": m["markId"],
                "listeningSessionId": "listen-20261103-c1",
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "pressedAtSec": m["pressedAtSec"],
                "suggestedSentenceId": sug["sentenceId"],
                # 第一处用户确认了建议句；第二处用户改成后一句，用于验证前后句调整
                "confirmedSentenceId": (
                    sug["sentenceId"]
                    if i == 0
                    else sentences[
                        min(sug["index"], len(sentences) - 1)
                    ]["sentenceId"]
                ),
                "confirmedAtSec": None,
                "createdAt": "2026-11-03T12:00:00Z",
            }
        )
    return marks


def build_practice(sentences: list[dict], marks: list[dict]) -> tuple[dict, list[dict]]:
    by_id = {s["sentenceId"]: s for s in sentences}
    mark = marks[0]
    target = by_id[mark["confirmedSentenceId"]]
    practice = {
        "practiceId": "practice-001",
        "markId": mark["markId"],
        "materialId": MATERIAL_ID,
        "materialVersion": MATERIAL_VERSION,
        "sentenceId": target["sentenceId"],
        "status": "active",
        "currentStepId": "step-003",
        "feedbackHistory": ["sound"],
        "effectiveMs": 42_000,
        "aiRequest": {"status": "idle", "requestId": None, "errorCode": None},
        "createdAt": "2026-11-03T12:01:00Z",
        "updatedAt": "2026-11-03T12:04:00Z",
    }
    steps = [
        {
            "stepId": "step-001",
            "practiceId": "practice-001",
            "action": "replay",
            "source": "confirmed_sentence",
            "playback": {
                "startSec": target["startSec"],
                "endSec": target["endSec"],
                "speed": 1.0,
            },
            "textVisible": False,
            "feedback": "sound",
            "candidateRef": None,
            "createdAt": "2026-11-03T12:01:10Z",
        },
        {
            "stepId": "step-002",
            "practiceId": "practice-001",
            "action": "slow",
            "source": "confirmed_sentence",
            "playback": {
                "startSec": target["startSec"],
                "endSec": target["endSec"],
                "speed": 0.75,
            },
            "textVisible": False,
            "feedback": "sound",
            "candidateRef": None,
            "createdAt": "2026-11-03T12:02:20Z",
        },
        {
            "stepId": "step-003",
            "practiceId": "practice-001",
            "action": "explain",
            "source": "model",
            "playback": None,
            "textVisible": True,
            "feedback": "meaning",
            "candidateRef": None,
            "payload": {
                "explanation": (
                    "说话人用 weariness（疲态）解释了为什么要收工："
                    "“we're both displaying signs of weariness” 直接给出了收工理由。"
                ),
                "developmentFixture": True,
            },
            "createdAt": "2026-11-03T12:03:00Z",
        },
    ]
    return practice, steps



def build_transfer_candidates(sentences: list[dict]) -> dict:
    """换句检查候选（T10）。

    任务书第 7 节把 `transfer` 列为带练动作、`candidateRef` 列为 PracticeStep 的字段，
    但没有定义候选对象的结构与「无候选」时的返回形态。这里给出 C 提议的可运行形状，
    并在 needsBackendConfirmation 里列出必须由 B 决定的点 —— C 不替 B 定业务字段。
    """
    by_id = {s["sentenceId"]: s for s in sentences}
    picks = ["s14", "s19"]
    items = []
    for i, sid in enumerate(picks, start=1):
        if sid not in by_id:
            raise SystemExit(f"[transfer] 候选取句不存在：{sid}")
        s = by_id[sid]
        items.append(
            {
                "candidateId": f"cand-{i:02d}",
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "sentenceId": sid,
                "startSec": s["startSec"],
                "endSec": s["endSec"],
                "text": s["text"],
                "source": "same_material_real_audio",
                "relevanceNote": "开发样例占位：与目标句的表达关联未经人工核对，正式验收需 C 逐条听检",
                "developmentFixture": True,
            }
        )
    return {
        "developmentFixture": True,
        "status": "C 提议的契约形状，待 B 确认",
        "proposedBy": "C",
        "needsBackendConfirmation": [
            "候选检索的真实数据来源（是否只在本材料内检索，还是跨材料）",
            "候选数量上限与排序依据",
            "无候选时返回空 items 还是错误码（本样例按空 items 设计，便于页面展示空态）",
            "换句题目与参考答案是否需要独立生成并要求来源",
        ],
        "cases": [
            {"caseId": "with_candidates", "description": "有候选：可播放真实原声并作答", "items": items},
            {"caseId": "no_candidate", "description": "无候选：items 为空数组，页面走空态且仍可保存卡点", "items": []},
        ],
    }


def build_review(sentences: list[dict], marks: list[dict]) -> dict:
    by_id = {s["sentenceId"]: s for s in sentences}
    items = []
    for i, m in enumerate(marks):
        sid = m["confirmedSentenceId"]
        items.append(
            {
                "itemId": f"item-{i + 1:03d}",
                "order": i + 1,
                "practiceId": f"practice-{i + 1:03d}",
                "markId": m["markId"],
                "sentenceId": sid,
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "startSec": by_id[sid]["startSec"],
                "endSec": by_id[sid]["endSec"],
                "latestFeedback": "sound" if i == 0 else "meaning",
                "status": "current" if i == 0 else "pending",
            }
        )
    return {
        "reviewId": "review-001",
        "status": "active",
        "budgetMs": 600_000,
        "effectiveMs": 95_000,
        "currentItemId": "item-001",
        "maxItems": 3,
        "items": items,
        "startedAt": "2026-11-03T12:10:00Z",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验样例是否与生成结果一致")
    args = parser.parse_args()

    src, sentences = build_sentences()
    duration = float(src["durationSec"])
    validate_sentences(sentences, duration)

    before, after = build_questions(sentences)
    validate_questions(before, after, sentences)

    marks = build_marks(sentences)
    practice, steps = build_practice(sentences, marks)
    review = build_review(sentences, marks)
    transfer = build_transfer_candidates(sentences)

    written: list[str] = []
    dump_json(
        "materials.json",
        {
            "developmentFixture": True,
            "items": [
                {
                    "materialId": MATERIAL_ID,
                    "materialVersion": MATERIAL_VERSION,
                    "title": "2025年12月六级第一套 · Conversation Two",
                    "level": "cet6",
                    "section": "Conversation Two",
                    "audioUrl": f"/api/cet/materials/{MATERIAL_ID}/audio",
                    "audioSha256": AUDIO_SHA256,
                    "durationSec": duration,
                    "sentenceCount": len(sentences),
                    "questionCount": len(before),
                    "developmentFixture": True,
                }
            ],
            "count": 1,
        },
        args.check,
        written,
    )
    dump_json(
        f"material_{MATERIAL_ID}.json",
        {
            "developmentFixture": True,
            "material": {
                "materialId": MATERIAL_ID,
                "materialVersion": MATERIAL_VERSION,
                "title": "2025年12月六级第一套 · Conversation Two",
                "level": "cet6",
                "section": "Conversation Two",
                "audioUrl": f"/api/cet/materials/{MATERIAL_ID}/audio",
                "audioSha256": AUDIO_SHA256,
                "durationSec": duration,
                "sentenceCount": len(sentences),
                "questionCount": len(before),
            },
            "sentences": sentences,
            "provenance": src.get("provenance", {}),
            "developmentFixture": True,
        },
        args.check,
        written,
    )
    dump_json("questions_before_attempt.json", {"developmentFixture": True, "items": before}, args.check, written)
    dump_json("attempt_result.json", {"developmentFixture": True, "items": after}, args.check, written)
    dump_json("marks.json", {"developmentFixture": True, "items": marks}, args.check, written)
    dump_json("practice.json", {"developmentFixture": True, "practice": practice}, args.check, written)
    dump_json("practice_steps.json", {"developmentFixture": True, "practiceId": "practice-001", "items": steps}, args.check, written)
    dump_json("review_session.json", {"developmentFixture": True, "session": review}, args.check, written)
    dump_json("transfer_candidates.json", transfer, args.check, written)
    dump_json(
        "manifest.json",
        {
            "contractVersion": "cet-contract-v1",
            "generatedBy": "tests/fixtures/cet/build_fixtures.py",
            "materialId": MATERIAL_ID,
            "materialVersion": MATERIAL_VERSION,
            "audio": {
                "sha256": AUDIO_SHA256,
                "fileNameHint": AUDIO_FILE_HINT,
                "originPath": AUDIO_SOURCE_PATH,
                "inGit": False,
                "reason": "音频文件按仓库 .gitignore 不入库；本地配置映射到同一文件后用 sha256 核对",
            },
            "files": [
                "materials.json",
                f"material_{MATERIAL_ID}.json",
                "questions_before_attempt.json",
                "attempt_result.json",
                "marks.json",
                "practice.json",
                "practice_steps.json",
                "review_session.json",
                "transfer_candidates.json",
            ],
            "notes": [
                "时间轴为原音频绝对秒数；budgetMs / effectiveMs 为毫秒。",
                "交卷前样例不含 answer / evidenceRef / explanation。",
                "未知答案保留 answer=null 且 answerSource=unknown，不推断。",
                "所有样例带 developmentFixture=true，只用于契约与工具自测，不代表真实后端结果。",
            ],
        },
        args.check,
        written,
    )

    if args.check:
        print("[check] 全部样例与生成结果一致")
        return 0

    total = sum((HERE / n).stat().st_size for n in written)
    print(f"[ok] 生成 {len(written)} 个样例文件，共 {total} 字节")
    print(f"[ok] 句子 {len(sentences)} 条，题目 {len(before)} 道，卡点 {len(marks)} 处")
    print(f"[ok] 句时间轴自检通过；题目自检通过（含 1 道 unknown 答案题）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
