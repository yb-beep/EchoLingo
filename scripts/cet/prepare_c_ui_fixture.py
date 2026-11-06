#!/usr/bin/env python3
"""C 板块验证夹具：在隔离库里造一份「未被接触过的换句候选材料」。

为什么需要它：
    `cet_service._transfer_candidate()` 只从**尚未接触过**的材料里选候选
    （排除已交卷材料、已建练习材料、已被用过的候选材料，以及它们的 source_url）。
    日常库里的候选池早就被前几轮测试消耗光了，所以 F05「有候选」路径
    在现有数据上必然走 no_candidate —— 那条分支验证不到。

    本脚本每次运行都插入一份**全新 lesson id + 全新 source_url** 的夹具，
    音频指向库里真实存在的 mp3，句子与原句有实词重叠，
    因此能被真实选候选逻辑选中，且播的是真实可听的音频。

边界：
    - 只写隔离库（ELT_USER_DB），绝不碰主工作区数据
    - 夹具材料会进入候选池，属于测试数据；跑完可用 --cleanup 删掉

用法：
    ELT_USER_DB=/tmp/wt-c-ui/.cet-qa/data/vocab.db \
      .venv/bin/python scripts/cet/prepare_c_ui_fixture.py
    # 清理本次/全部夹具：
    ELT_USER_DB=... .venv/bin/python scripts/cet/prepare_c_ui_fixture.py --cleanup
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# 夹具句必须与原句（v2-2181-0 "It's getting late and we're both displaying
# signs of weariness."）有实词重叠，否则 _transfer_candidate 的锚点匹配会落空。
FIXTURE_SEGMENTS = [
    (0.5, 4.0, "It was getting late, and both of them showed signs of weariness."),
    (5.0, 8.5, "We should meet again tomorrow morning to finish the rest."),
    (9.5, 12.0, "Nobody wanted to keep working so late at night."),
]

# 真实存在、可播放、且与原句材料不同的音频
FIXTURE_AUDIO = "/output/v2_assets/cet4/cet4-sample-hope-wish.mp3"

FIXTURE_TITLE_PREFIX = "C-UI 换句候选夹具"


def prepare(db_path: str, lesson_id: int | None = None) -> dict:
    """插入一份全新夹具材料，返回 {lessonId, materialId, audioUrl, sentences}。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if lesson_id is None:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nxt FROM v2_lessons").fetchone()
            lesson_id = max(int(row["nxt"]), 9001)
        source_url = f"upload:c-ui-fixture-{lesson_id}"
        now = "2026-11-05T00:00:00Z"
        conn.execute(
            "INSERT OR REPLACE INTO v2_lessons("
            "id,source_type,source_url,video_id,lesson_mode,media_url,media_kind,"
            "title,duration,subtitle_status,summary_status,created_at,updated_at,archived) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (lesson_id, "upload", source_url, "", "listening", FIXTURE_AUDIO, "local_audio",
             f"{FIXTURE_TITLE_PREFIX} #{lesson_id}", 80.53, "ready", "ready", now, now),
        )
        conn.execute("DELETE FROM v2_subtitle_segments WHERE lesson_id=?", (lesson_id,))
        for idx, (start, end, text) in enumerate(FIXTURE_SEGMENTS):
            conn.execute(
                "INSERT INTO v2_subtitle_segments(lesson_id,idx,start,end,text,normalized,words_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (lesson_id, idx, start, end, text, text.lower(), ""),
            )
        conn.commit()
        return {
            "lessonId": lesson_id,
            "materialId": f"v2-{lesson_id}",
            "audioUrl": FIXTURE_AUDIO,
            "sentences": [
                {"idx": i, "startSeconds": s, "endSeconds": e, "text": t}
                for i, (s, e, t) in enumerate(FIXTURE_SEGMENTS)
            ],
        }
    finally:
        conn.close()


def cleanup(db_path: str) -> int:
    """删掉所有本脚本造的夹具（含引用它们的候选与练习，避免外键悬空）。"""
    conn = sqlite3.connect(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM v2_lessons WHERE source_url LIKE 'upload:c-ui-fixture-%'"
        ).fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        material_ids = [f"v2-{i}" for i in ids]
        mplaceholders = ",".join("?" for _ in material_ids)
        conn.execute(
            f"DELETE FROM cet_transfer_candidates WHERE json_extract(candidate_json,'$.materialId') IN ({mplaceholders})",
            material_ids,
        )
        conn.execute(f"DELETE FROM v2_subtitle_segments WHERE lesson_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM v2_lessons WHERE id IN ({placeholders})", ids)
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true", help="删除夹具")
    ap.add_argument("--db", default=os.environ.get("ELT_USER_DB", ""),
                    help="隔离库路径（默认取 ELT_USER_DB）")
    args = ap.parse_args()
    if not args.db:
        print("X  需要 ELT_USER_DB 或 --db 指定隔离库路径（不要指向主工作区库）")
        return 2
    if not os.path.exists(args.db):
        print(f"X  库不存在：{args.db}")
        return 2
    if args.cleanup:
        n = cleanup(args.db)
        print(f"已清理 {n} 份夹具")
        return 0
    info = prepare(args.db)
    print("已插入夹具：")
    print(f"  lessonId   {info['lessonId']}")
    print(f"  materialId {info['materialId']}")
    print(f"  audioUrl   {info['audioUrl']}")
    for s in info["sentences"]:
        print(f"  [{s['startSeconds']:>5}-{s['endSeconds']:>5}] {s['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
