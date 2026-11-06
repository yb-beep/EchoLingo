#!/usr/bin/env python
"""把四六级真实材料导入隔离数据库（可重复执行，幂等）。

为什么需要这个脚本：
- 材料音频与时间轴不随仓库交付（体积与版权），但验收方需要能复现
- 手工建课程 + 挂题目容易漏步骤（比如忘了把证据句 ID 转成 v2-<id>-<idx> 形式）
- 换台电脑要能一键重建同样的材料

用法（登记表里的两份样例，行为与旧版一致）：
    .venv/bin/python scripts/cet/import_materials.py --list
    .venv/bin/python scripts/cet/import_materials.py --all
    .venv/bin/python scripts/cet/import_materials.py --only cet4-sample-hope-wish

用法（通用本地导入，P2）：
    .venv/bin/python scripts/cet/import_materials.py \
        --audio ~/Downloads/listening.mp3 \
        --title "2024年6月四级第一套 · News 1" \
        --level cet4 \
        --category 练习 \
        --subtitle ~/Downloads/listening.srt

数据源约定（不改仓库内的正式数据）：
    音频原件   .cet-qa/media/<materialId>.mp3
    句子时间轴 .cet-qa/media/<materialId>.sentences.source.json
    题目与答案 tests/fixtures/cet/source/<materialId>.questions.json
    服务可见   output/v2_assets/cet/<materialId>.mp3

环境变量：
    ELT_USER_DB   目标数据库（缺省 <repo>/.cet-qa/data/vocab.db，保留旧行为）
    CET_QA_MEDIA  素材目录（缺省 <repo>/.cet-qa/media，供 verify_fresh_db.py 覆盖）
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

# 保留旧行为：未显式给 ELT_USER_DB 时用仓库内隔离库。
# 通用导入路径同样读这个变量，可被外部覆盖（run.sh / verify_fresh_db.py 依赖）。
os.environ.setdefault("ELT_USER_DB", str(ROOT / ".cet-qa" / "data" / "vocab.db"))

import db  # noqa: E402
db.DB_PATH = pathlib.Path(os.environ["ELT_USER_DB"])

from webapp.services import cet_import, cet_service  # noqa: E402

MEDIA = pathlib.Path(os.environ.get("CET_QA_MEDIA") or (ROOT / ".cet-qa" / "media"))
FIXTURES = ROOT / "tests" / "fixtures" / "cet" / "source"
SERVED = ROOT / "output" / "v2_assets" / "cet"

# 材料登记表：新增固定样例材料在这里加一行
MATERIALS = {
    "cet6-202512-conv2": {
        "title": "2025年12月六级第一套 · Conversation Two",
        "level": "cet6",
        "category": "真题",
        "duration": 105.486349,
        "sourceType": "cetMaterial",
        "note": "2025年12月六级第一套听力 Conversation Two，真实真题音频",
    },
    "cet4-sample-hope-wish": {
        "title": "四级样例 · Hope & Wish（Let's Learn English 节选）",
        "level": "cet4",
        "category": "样例",
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


def _segments_payload(sentences: list[dict]) -> list[dict]:
    return [
        {"index": i, "start": s["start"], "end": s["end"], "text": s["text"]}
        for i, s in enumerate(sentences)
    ]


def _write_timeline(lesson_id: int, sentences: list[dict]) -> None:
    """写字幕 + 阅读分段 + **把状态收敛为 ready**（D-IMPORT-1 修复点）。

    旧版只写 `v2_subtitle_segments` 而不更新 `v2_lessons.subtitle_status`，
    于是导入成功后状态仍是默认的 `pending`，前端会：
      1. 徽标永久显示「字幕生成中」（明明 35 句已渲染）
      2. 反复轮询 `GET /api/v2/lessons/{id}/reading`
      3. 学习页初始态假称「字幕生成中」——违反任务书 §4 P3
    这里统一走 `cet_import.finalize_lesson_status`，保证「写入字幕」
    与「状态收敛」是一个原子动作，两个入口（脚本 / HTTP 导入）共用同一份实现。
    """
    segments = _segments_payload(sentences)
    db.replace_v2_subtitle_segments(lesson_id, segments)
    try:
        from webapp.services.media_reading import build_media_reading_blocks

        db.replace_v2_reading_blocks(lesson_id, build_media_reading_blocks(segments))
    except Exception:  # noqa: BLE001
        # 阅读分段是派生视图，失败不阻塞导入
        pass
    cet_import.finalize_lesson_status(lesson_id, has_timeline=bool(segments))


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
        _write_timeline(lesson_id, sentences)
        action = "复用"
    else:
        if existing and force:
            lesson_id = int(existing["id"])
            _write_timeline(lesson_id, sentences)
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
            _write_timeline(lesson_id, sentences)
            action = "新建"

    # 级别 + 素材类别都写进 tags（冻结约定 §3：只用现有列）
    tags = [meta["level"]]
    if meta.get("category"):
        tags.append(meta["category"])
    db.set_v2_lesson_tags(lesson_id, tags)
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


def import_generic(args: argparse.Namespace) -> str:
    """通用本地导入（P2）：音频 + 可选字幕/文本。

    与 HTTP 导入共用 `cet_import` 服务层，因此校验规则、去重规则、
    状态收敛规则完全一致 —— 不会出现「脚本能导、界面导不了」的分叉。
    """
    audio_path = pathlib.Path(args.audio).expanduser()
    if not audio_path.is_file():
        raise SystemExit(f"音频文件不存在：{audio_path}")

    subtitle_bytes: bytes | None = None
    subtitle_name = ""
    if args.subtitle:
        subtitle_path = pathlib.Path(args.subtitle).expanduser()
        if not subtitle_path.is_file():
            raise SystemExit(f"字幕/文本文件不存在：{subtitle_path}")
        subtitle_bytes = subtitle_path.read_bytes()
        subtitle_name = subtitle_path.name

    try:
        result = cet_import.import_local_material(
            audio_bytes=audio_path.read_bytes(),
            audio_filename=audio_path.name,
            title=args.title or "",
            level=args.level,
            category=args.category or "",
            subtitle_bytes=subtitle_bytes,
            subtitle_filename=subtitle_name,
            allow_duplicate=bool(args.allow_duplicate),
        )
    except cet_import.ImportValidationError as exc:
        print(f"导入失败：{exc.message}", file=sys.stderr)
        for item in exc.errors:
            location = f"第 {item['index']} 条" if item.get("index") else "整体"
            if item.get("line"):
                location += f"（第 {item['line']} 行）"
            print(f"  - [{item['reason']}] {location}：{item['message']}", file=sys.stderr)
        raise SystemExit(1) from exc

    material = result["material"]
    subtitle = result["subtitle"]
    timed_note = (
        f"{subtitle['sentenceCount']} 句（{subtitle['format']}，可逐句定位）"
        if subtitle["timed"] else "无逐句时间轴"
    )
    duplicate_note = "｜已存在同音频同标题材料，未重复创建" if result["duplicate"] else ""
    lines = [
        f"{'复用' if result['duplicate'] else '新建'} {material['materialId']} → v2-{material['lessonId']}"
        f"｜{material['title']}"
        f"｜级别 {material['level']}"
        f"｜{material['durationSec']:.1f}s"
        f"｜{timed_note}{duplicate_note}"
    ]
    for warning in result.get("warnings") or []:
        lines.append(f"  · {warning}")
    lines.append(f"  打开：{material['lessonUrl']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="导入四六级材料到隔离数据库（固定样例 + 通用本地导入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--list", action="store_true", help="列出已登记材料")
    ap.add_argument("--all", action="store_true", help="导入全部已登记材料")
    ap.add_argument("--only", help="只导入指定已登记材料 ID")
    ap.add_argument("--force", action="store_true", help="强制重建（覆盖已有时间轴）")

    generic = ap.add_argument_group("通用本地导入（P2）")
    generic.add_argument("--audio", help="音频文件路径（.mp3/.m4a/.wav/.ogg/.flac/.aac）")
    generic.add_argument("--title", help="材料标题（缺省用音频文件名）")
    generic.add_argument("--level", default="unspecified",
                         help="级别：cet4 / cet6 / unspecified（缺省 unspecified = 待分类）")
    generic.add_argument("--category", default="",
                         help="素材类别：真题 / 练习 / 样例 / 开发夹具")
    generic.add_argument("--subtitle", help="字幕或原文文件（.srt/.vtt/.json/.txt）")
    generic.add_argument("--allow-duplicate", action="store_true",
                         help="即使同音频同标题已存在也再导入一份")
    args = ap.parse_args()

    if args.list:
        print("已登记材料：")
        for mid, meta in MATERIALS.items():
            audio = "有" if (MEDIA / f"{mid}.mp3").is_file() else "缺"
            sent = "有" if (MEDIA / f"{mid}.sentences.source.json").is_file() else "缺"
            qs = len(_questions(mid))
            print(
                f"  {mid:26} {meta['level']:5} {meta.get('category') or '未标注':6}"
                f" 音频:{audio} 时间轴:{sent} 题目:{qs}"
            )
        print(f"\n素材目录：{MEDIA}")
        print(f"数据库：  {os.environ['ELT_USER_DB']}")
        print("支持的通用导入格式：")
        print(f"  音频  {', '.join(cet_import.SUPPORTED_AUDIO_EXTS)}")
        print(f"  字幕  {', '.join(cet_import.SUPPORTED_SUBTITLE_EXTS)}")
        return 0

    cet_service.init_cet_schema()

    if args.audio:
        print(import_generic(args))
        return 0

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
