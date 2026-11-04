"""CET listening black-box HTTP API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from starlette.concurrency import run_in_threadpool
from webapp.services import cet_service

router = APIRouter(prefix="/api/cet", tags=["cet"])


@router.get("/materials")
def list_cet_materials(level: str | None = Query(default=None)):
    items = cet_service.list_materials(level)
    # 顶层包装对齐契约：{items, count}（原为 {materials}）
    return {"items": items, "count": len(items), "materials": items}


@router.get("/materials/{material_id}")
def get_cet_material(material_id: str):
    return cet_service.get_material(material_id)


@router.get("/materials/{material_id}/questions")
def get_cet_questions(material_id: str):
    return cet_service.list_questions(material_id)


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
