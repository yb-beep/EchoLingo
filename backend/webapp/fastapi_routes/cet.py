"""CET listening black-box HTTP API."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from starlette.concurrency import run_in_threadpool

import db
from webapp import cet_mode
from webapp.services import cet_service

router = APIRouter(prefix="/api/cet", tags=["cet"])


# ══════════════════════════════════════════════════════════════
# 首页汇总（卡点融合 · 包 H / H1）：**只读**统计
#
# 契约：GET /api/cet/summary
#   {
#     "materials": {
#       "<lessonId>": {submitted, score, total, unscored, marks, pendingReview}
#     },
#     "reviewDue": {marks, sentences, words}
#   }
#
# 口径说明（全部来自现有表，不写库、不改 cet_service 的业务规则）：
#   - materials 键是底座课程 lessonId 的字符串形式（材料卡片直接用）。
#   - submitted 是布尔「是否交过卷」；score/total 是最近一次交卷的得分/题目数，
#     前端拼出「已交卷 4/4」；unscored 是「题库没有可靠答案」的题数（unknownCount）。
#   - marks 是该材料的卡点（cet_marks）总数；pendingReview 是该材料待复习的
#     卡点条目（cet_review_items.status='pending'）。
#   - reviewDue.marks/sentences/words 是「今天值得复习」的数量：
#     待复习卡点条目 / 到期收藏句 / 到期收藏词。全部为 0 时前端显示空态。
#   - 表不存在或查询失败一律按 0 返回，不向调用方报错（空库可安全打开首页）。
# ══════════════════════════════════════════════════════════════

# 收藏词复习间隔与前端 reviewStatus() 的 INTERVALS 保持一致（1/3/7/14/30/60 天）。
_WORD_REVIEW_INTERVALS = (1, 3, 7, 14, 30, 60)


def _summary_lesson_material_ids(lesson: dict[str, Any]) -> tuple[str, ...]:
    """同一份材料在存量表里有两种可能的 ID：`v2-<lessonId>` 与业务稳定 ID。"""
    lesson_id = int(lesson["id"])
    ids = [f"v2-{lesson_id}"]
    source_url = str(lesson.get("source_url") or "")
    # 与 cet_service._business_id 同一规则：fixture:// / upload: 不派生业务 ID。
    if source_url and not source_url.startswith(("fixture://", "upload:")):
        name = source_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if name and name not in ids:
            ids.append(name)
    return tuple(ids)


def _summary_placeholders(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def _summary_count(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    """只读 COUNT；表缺失/查询失败按 0 处理（空库不报错）。"""
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001
        return 0
    return int(row[0] if row else 0)


def _summary_row(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _summary_rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _empty_material_summary(total: int = 0) -> dict[str, Any]:
    return {
        "submitted": False,
        "score": 0,
        "total": total,
        "unscored": 0,
        "marks": 0,
        "pendingReview": 0,
    }


def _word_is_due(row: Any, today: date) -> bool:
    """与前端 `reviewStatus()` 同一套「到期」判断（next_review 优先，否则按间隔）。"""
    next_review = str(row["next_review"] or "")[:10]
    if next_review:
        return next_review <= today.isoformat()
    last_studied = str(row["last_studied"] or "")[:10]
    if not last_studied:
        return True
    try:
        days_since = (today - date.fromisoformat(last_studied)).days
    except ValueError:
        return True
    try:
        count = max(1, int(row["count"] or 1))
    except (TypeError, ValueError):
        count = 1
    interval_days = _WORD_REVIEW_INTERVALS[min(count - 1, len(_WORD_REVIEW_INTERVALS) - 1)]
    return interval_days - days_since <= 0


def _cet_home_summary() -> dict[str, Any]:
    try:
        lessons = db.list_v2_lessons(include_archived=True)
    except Exception:  # noqa: BLE001
        lessons = []

    materials: dict[str, dict[str, Any]] = {}
    review_due = {"marks": 0, "sentences": 0, "words": 0}
    today = date.today()

    try:
        with db._db() as conn:  # noqa: SLF001 — 只读 SELECT，不写业务表
            for lesson in lessons:
                lesson_id = int(lesson["id"])
                ids = _summary_lesson_material_ids(lesson)
                marks = _summary_placeholders(ids)
                total = _summary_count(
                    conn,
                    f"SELECT COUNT(*) FROM cet_questions WHERE material_id IN ({marks})",
                    ids,
                )
                entry = _empty_material_summary(total)
                attempt = _summary_row(
                    conn,
                    f"SELECT * FROM cet_attempts WHERE material_id IN ({marks}) "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    ids,
                )
                if attempt is not None:
                    entry["submitted"] = True
                    entry["score"] = int(attempt["score"] or 0)
                    entry["unscored"] = int(attempt["unknown_count"] or 0)
                entry["marks"] = _summary_count(
                    conn,
                    f"SELECT COUNT(*) FROM cet_marks WHERE material_id IN ({marks})",
                    ids,
                )
                entry["pendingReview"] = _summary_count(
                    conn,
                    f"SELECT COUNT(*) FROM cet_review_items "
                    f"WHERE status='pending' AND material_id IN ({marks})",
                    ids,
                )
                materials[str(lesson_id)] = entry

            review_due["marks"] = _summary_count(
                conn,
                "SELECT COUNT(*) FROM cet_review_items WHERE status='pending'",
            )
            review_due["sentences"] = _summary_count(
                conn,
                """
                SELECT COUNT(*) FROM v2_sentences AS sentence
                WHERE COALESCE(sentence.archived, 0) = 0
                  AND (
                    sentence.next_review IS NULL
                    OR sentence.next_review = ''
                    OR substr(sentence.next_review, 1, 10) <= ?
                  )
                  AND (
                    COALESCE(sentence.saved_manually, 0) = 1
                    OR EXISTS (
                      SELECT 1 FROM v2_phase_b_sentences AS saved
                      WHERE saved.sentence_id = sentence.id
                    )
                    OR EXISTS (
                      SELECT 1 FROM v2_sentence_review_items AS review
                      WHERE review.sentence_id = sentence.id
                    )
                  )
                """,
                (today.isoformat(),),
            )
            word_rows = _summary_rows(
                conn,
                """
                SELECT words.count, words.next_review, words.last_studied
                FROM word_review_items AS review
                JOIN words ON words.word = review.word
                WHERE COALESCE(review.archived, 0) = 0
                  AND COALESCE(review.mastered, 0) = 0
                """,
            )
            review_due["words"] = sum(1 for row in word_rows if _word_is_due(row, today))
    except Exception:  # noqa: BLE001
        # 数据库不可用时不阻断首页：返回全 0（契约要求「查不到数据返回 0」）。
        materials = {}
        review_due = {"marks": 0, "sentences": 0, "words": 0}

    return {"materials": materials, "reviewDue": review_due}


@router.get("/summary")
def get_cet_summary():
    """首页汇总（只读）：材料卡片摘要 + 今日复习数量。"""
    return _cet_home_summary()


@router.get("/mode")
def get_cet_mode():
    """四六级专用底座的集中功能配置（任务书 §3.3）。

    前端启动时读这一份，决定渲染哪些导航、跳过哪些模块的初始化。
    与后端 `webapp.cet_mode` 同源，不各写一套开关。
    """
    return cet_mode.config()


@router.get("/materials")
def list_cet_materials(level: str | None = Query(default=None)):
    items = cet_service.list_materials(level)
    # 顶层包装对齐契约：{items, count}（原为 {materials}）
    return {"items": items, "count": len(items), "materials": items}


@router.get("/materials/by-lesson/{lesson_id}")
def cet_material_by_lesson(lesson_id: int):
    """按底座 lesson id 查四六级材料。

    前端在 /workspace/<lesson_id> 页面里需要知道「当前课程是不是四六级材料」，
    用 lesson id 精确查最可靠（标题匹配会命中相近课程）。
    不是四六级材料时返回 404，前端据此不挂载卡点控件。
    """
    material = cet_service.find_material_by_lesson(lesson_id)
    if not material:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_CET_MATERIAL", "message": "该课程不是四六级材料。",
                    "lessonId": lesson_id},
        )
    return material


@router.get("/materials/{material_id}")
def get_cet_material(material_id: str, version: str | None = Query(default=None)):
    """材料详情。

    契约要求顶层含 material 包装；同时校验 version（不符必须拒绝）。
    """
    material = cet_service.get_material(material_id)
    # S5：版本不符必须拒绝（原来忽略 version 参数，bogus-v0 也返回 200）
    if version and version != material["materialVersion"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MATERIAL_VERSION_CHANGED",
                "message": "材料版本已变化，请刷新后重试。",
                "currentVersion": material["materialVersion"],
                "requestedVersion": version,
            },
        )
    # 契约：{material: {...}, sentences: [...]}
    return {"material": material, "sentences": material.get("sentences", [])}


@router.get("/materials/{material_id}/questions")
def get_cet_questions(material_id: str):
    """题目列表。

    契约要求 items；底座旧字段 questions 保留兼容。
    """
    data = cet_service.list_questions(material_id)
    questions = data.get("questions") or []
    return {**data, "items": questions, "count": len(questions)}


@router.get("/attempts/{attempt_id}")
def cet_get_attempt(attempt_id: str):
    """取回交卷结果（刷新后恢复交卷状态用）。"""
    return cet_service.get_attempt(attempt_id)


@router.post("/attempts", status_code=201)
def submit_cet_attempt(body: dict[str, Any] = Body(...)):
    return cet_service.submit_attempt(body)


@router.post("/marks", status_code=201)
def create_cet_mark(body: dict[str, Any] = Body(...)):
    return cet_service.create_mark(body)


@router.get("/marks")
def list_cet_marks(materialId: str = Query(...), listeningSessionId: str | None = Query(default=None)):
    items = cet_service.list_marks(materialId, listeningSessionId)
    return {"items": items, "count": len(items), "marks": items}


@router.patch("/marks/{mark_id}")
def confirm_cet_mark(mark_id: str, body: dict[str, Any] = Body(...)):
    return cet_service.confirm_mark(mark_id, body)


@router.delete("/marks/{mark_id}")
def delete_cet_mark(mark_id: str):
    return cet_service.delete_mark(mark_id)


@router.post("/practices", status_code=201)
def create_cet_practice(body: dict[str, Any] = Body(...)):
    return cet_service.create_practice(body)


@router.get("/practices/{practice_id}/ai-status")
def cet_ai_status(practice_id: str):
    """B03：查询当前 AI 请求状态。"""
    return cet_service.ai_request_status(practice_id)


@router.post("/practices/{practice_id}/ai-cancel")
def cet_ai_cancel(practice_id: str, body: dict[str, Any] = Body(default={})):
    """B03：取消当前 AI 请求（幂等）。"""
    return cet_service.cancel_ai_request(practice_id, body or {})


@router.get("/practices/{practice_id}")
def get_cet_practice(practice_id: str):
    return cet_service._practice_payload(practice_id)


@router.post("/practices/{practice_id}/events")
def record_cet_practice_event(practice_id: str, body: dict[str, Any] = Body(...)):
    return cet_service.record_practice_event(practice_id, body)


@router.post("/practices/{practice_id}/next")
async def next_cet_practice_step(practice_id: str, body: dict[str, Any] = Body(...)):
    return await run_in_threadpool(cet_service.next_practice_step, practice_id, body)


@router.post("/practices/{practice_id}/local")
def local_cet_practice_action(practice_id: str, body: dict[str, Any] = Body(...)):
    return cet_service.local_practice_action(practice_id, body)


@router.post("/practices/{practice_id}/finish")
def finish_cet_practice(practice_id: str, body: dict[str, Any] = Body(...)):
    return cet_service.finish_practice(practice_id, body)


@router.get("/review-items")
def list_cet_review_items():
    items = cet_service.list_review_items()
    return {"items": items, "count": len(items), "reviewItems": items}


@router.post("/reviews", status_code=201)
def create_cet_review(body: dict[str, Any] = Body(...)):
    return cet_service.create_review(body)


@router.get("/reviews/{review_id}")
def get_cet_review(review_id: str):
    return cet_service._review_payload(review_id)


@router.post("/reviews/{review_id}/actions")
def cet_review_action(review_id: str, body: dict[str, Any] = Body(...)):
    return cet_service.review_action(review_id, body)
