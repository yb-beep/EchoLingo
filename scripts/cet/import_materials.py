#!/usr/bin/env python
"""把四六级真实材料导入隔离数据库（可重复执行，幂等）。

为什么需要这个脚本：
- 材料音频与时间轴不随仓库交付（体积与版权），但验收方需要能复现
- 手工建课程 + 挂题目容易漏步骤（比如忘了把证据句 ID 转成 v2-<id>-<idx> 形式）
- 换台电脑要能一键重建同样的材料

用法：
    .venv/bin/python scripts/cet/import_materials.py --list
    .venv/bin/python scripts/cet/import_materials.py --all
    .venv/bin/python scripts/cet/import_materials.py --only cet4-sample-hope-wish

数据源约定（不改仓库内的正式数据）：
    音频原件   .cet-qa/media/<materialId>.mp3
    句子时间轴 .cet-qa/media/<materialId>.sentences.source.json
    题目与答案 tests/fixtures/cet/source/<materialId>.questions.json
    服务可见   output/v2_assets/cet/<materialId>.mp3
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ELT_USER_DB", str(ROOT / ".cet-qa" / "data" / "vocab.db"))

import db  # noqa: E402
db.DB_PATH = pathlib.Path(os.environ["ELT_USER_DB"])

from webapp.services import cet_service  # noqa: E402

MEDIA = ROOT / ".cet-qa" / "media"
FIXTURES = ROOT / "tests" / "fixtures" / "cet" / "source"
SERVED = ROOT / "output" / "v2_assets" / "cet"

# 材料登记表：新增材料在这里加一行
MATERIALS = {
    "cet6-202512-conv2": {
        "title": "2025年12月六级第一套 · Conversation Two",
        "level": "cet6",
        "duration": 105.486349,
        "sourceType": "cetMaterial",
        "note": "2025年12月六级第一套听力 Conversation Two，真实真题音频",
    },
    "cet4-sample-hope-wish": {
        "title": "四级样例 · Hope & Wish（Let's Learn English 节选）",
        "level": "cet4",
        "duration": 80.53,
        "sourceType": "cetMaterial",
        "note": "四级难度样例：hope/wish 语法讲解 + 秘鲁艺术节对话",
    },
}


def _sentences(material_id: str) -> list[dict]:
    """读取句子时间轴。支持多种来源格式。"""
    path = MEDIA / f"{material_id}.sentences.source.json"
    if not path.is_file():
        raise SystemExit(f"缺少句子时间轴：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else (
        raw.get("sentences") or raw.get("items") or raw.get("segments") or []
    )
    out = []
    for s in items:
        start = s.get("start", s.get("startSec", s.get("start_seconds")))
        end = s.get("end", s.get("endSec", s.get("end_seconds")))
        text = s.get("text") or s.get("content") or ""
        if start is None or end is None:
            continue
        out.append({"start": float(start), "end": float(end), "text": str(text)})
    return out


def _questions(material_id: str) -> list[dict]:
    path = FIXTURES / f"{material_id}.questions.json"
    if not path.is_file():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("questions") or []


def _has_attempts(storage_id: str) -> bool:
    """该材料是否已有交卷记录（决定能否重建题目）。"""
    import sqlite3
    try:
        conn = sqlite3.connect(os.environ["ELT_USER_DB"])
        row = conn.execute(
            "SELECT COUNT(*) FROM cet_attempt_answers WHERE question_id IN "
            "(SELECT id FROM cet_questions WHERE material_id=?)",
            (storage_id,),
        ).fetchone()
        conn.close()
        return bool(row and row[0])
    except Exception:  # noqa: BLE001
        return False


def import_one(material_id: str, force: bool = False) -> str:
    meta = MATERIALS.get(material_id)
    if not meta:
        raise SystemExit(f"未登记的材料：{material_id}（先在 MATERIALS 里加一行）")

    audio_src = MEDIA / f"{material_id}.mp3"
    if not audio_src.is_file():
        raise SystemExit(f"缺少音频：{audio_src}")

    sentences = _sentences(material_id)
    if not sentences:
        raise SystemExit(f"{material_id}: 时间轴为空")

    questions = _questions(material_id)

    # 1) 复制音频到服务可见位置
    SERVED.mkdir(parents=True, exist_ok=True)
    audio_dst = SERVED / f"{material_id}.mp3"
    if force or not audio_dst.is_file():
        shutil.copy2(audio_src, audio_dst)

    # 2) 幂等：已有同 source_url 的课程就复用
    existing = None
    for lesson in db.list_v2_lessons():
        if str(lesson.get("source_url") or "").endswith(f"/{material_id}.mp3"):
            existing = lesson
            break

    if existing and not force:
        lesson_id = int(existing["id"])
        db.replace_v2_subtitle_segments(lesson_id, [
            {"index": i, "start": s["start"], "end": s["end"], "text": s["text"]}
            for i, s in enumerate(sentences)
        ])
        action = "复用"
    else:
        if existing and force:
            lesson_id = int(existing["id"])
            db.replace_v2_subtitle_segments(lesson_id, [
                {"index": i, "start": s["start"], "end": s["end"], "text": s["text"]}
                for i, s in enumerate(sentences)
            ])
            action = "重建"
        else:
            lesson = db.create_v2_lesson(
                source_type=meta["sourceType"],
                source_url=f"file://{material_id}.mp3",
                title=meta["title"],
                duration=meta["duration"],
                media_url=f"/output/v2_assets/cet/{material_id}.mp3",
                media_kind="audio",
            )
            lesson_id = int(lesson["id"])
            db.replace_v2_subtitle_segments(lesson_id, [
                {"index": i, "start": s["start"], "end": s["end"], "text": s["text"]}
                for i, s in enumerate(sentences)
            ])
            action = "新建"

    db.set_v2_lesson_tags(lesson_id, [meta["level"]])
    storage_id = f"v2-{lesson_id}"

    # 3) 题目：证据句从 sNN 形式转成 v2-<id>-<0based>
    #
    # 注意：cet_attempt_answers.question_id 有外键指向 cet_questions，
    # 一旦有人交过卷，题目就不能删了（重建会 FK 失败）。
    # 这不是 bug，是「历史作答必须保留」的正确约束。
    # 所以这里先探测：有引用就跳过重建并明确告知，而不是抛异常中断。
    if questions and _has_attempts(storage_id):
        questions = []          # 跳过重建，保留既有题目
        skipped_questions = True
    else:
        skipped_questions = False

    if questions:
        payload = []
        for q in questions:
            ev = q.get("evidenceSentenceId")
            ev_seg = None
            if ev and str(ev).startswith("s") and str(ev)[1:].isdigit():
                ev_seg = f"{storage_id}-{int(ev[1:]) - 1}"
            elif ev:
                ev_seg = str(ev)
            ans = q.get("answer")
            payload.append({
                "id": q["questionId"],
                "questionNumber": q["number"],
                "prompt": q["stem"],
                "options": [
                    {"id": "ABCDE"[i], "text": opt}
                    for i, opt in enumerate(q["options"])
                ],
                "correctAnswer": None if ans is None else "ABCDE"[int(ans)],
                "answerSource": q.get("answerSource") or "unknown",
                "evidenceSentenceId": ev_seg,
            })
        cet_service.replace_questions_for_fixture(storage_id, payload)

    qnote = ""
    if skipped_questions:
        qnote = "｜题目已保留（已有交卷记录，不重建以免破坏历史作答）"
    elif not questions:
        qnote = "｜无题目"
    return (
        f"{action} {material_id} → {storage_id}"
        f"｜{len(sentences)} 句｜{meta['duration']:.1f}s{qnote}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="导入四六级材料到隔离数据库")
    ap.add_argument("--list", action="store_true", help="列出已登记材料")
    ap.add_argument("--all", action="store_true", help="导入全部")
    ap.add_argument("--only", help="只导入指定材料 ID")
    ap.add_argument("--force", action="store_true", help="强制重建（覆盖已有时间轴）")
    args = ap.parse_args()

    if args.list:
        print("已登记材料：")
        for mid, meta in MATERIALS.items():
            audio = "有" if (MEDIA / f"{mid}.mp3").is_file() else "缺"
            sent = "有" if (MEDIA / f"{mid}.sentences.source.json").is_file() else "缺"
            qs = len(_questions(mid))
            print(f"  {mid:26} {meta['level']:5} 音频:{audio} 时间轴:{sent} 题目:{qs}")
        return 0

    cet_service.init_cet_schema()

    if args.only:
        targets = [args.only]
    elif args.all:
        targets = list(MATERIALS)
    else:
        ap.print_help()
        return 1

    for mid in targets:
        print(import_one(mid, force=args.force))

    print("\n完成。启动后用 GET /api/cet/materials 确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
