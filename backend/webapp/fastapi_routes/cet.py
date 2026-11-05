"""CET listening black-box HTTP API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from webapp.services import cet_service

router = APIRouter(prefix="/api/cet", tags=["cet"])


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
