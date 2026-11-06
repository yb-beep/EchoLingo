"""CET 底座 · 材料库与本地导入路由（P1 + P2）。

**为什么单独一个文件**：冻结约定 §5 规定 `fastapi_routes/cet.py` 由 Lead 维护，
施工者不得直接改。底座新增的路由全部放这里，由 Lead 在 `fastapi_server.py`
注册一行即可（`from webapp.fastapi_routes.cet_base import router as cet_base_router`）。

路由清单：
- `GET  /api/cet/base/materials`            材料库（级别/搜索/类别/最近学习/含归档）
- `GET  /api/cet/base/materials/summary`    各级别数量 + 待分类统计
- `GET  /api/cet/base/materials/{id}`       单份材料详情（含句子）
- `PATCH /api/cet/base/materials/{id}`      改标题 / 手动归类 / 归档恢复
- `GET  /api/cet/base/import/formats`       真实支持的导入格式清单
- `POST /api/cet/base/import/validate`      只校验不导入（前端预检）
- `POST /api/cet/base/import`               导入音频 + 可选字幕/文本

全部走 `cet_materials` / `cet_import` 两个服务层，路由只做参数与错误映射。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from webapp.services import cet_import, cet_materials

router = APIRouter(prefix="/api/cet/base", tags=["cet-base"])


def _error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message, **extra},
    )


def _validation_payload(exc: cet_import.ImportValidationError) -> dict[str, Any]:
    return {
        "code": "IMPORT_VALIDATION_FAILED",
        "message": exc.message,
        "errors": exc.errors,
        "errorCount": len(exc.errors),
    }


def _validation_status(exc: cet_import.ImportValidationError) -> int:
    """校验错误的状态码：默认 422；文件过大用 413 区分（BASE-03）。"""
    return int(getattr(exc, "status_code", 422) or 422)


def _media_upload_error_types() -> tuple[type[BaseException], ...]:
    """上传设施的错误类型（延迟导入，避免路由模块启动时硬依赖服务实现）。"""
    from webapp.services.v2_lessons import (
        MediaUploadConsumedError,
        MediaUploadError,
        MediaUploadTooLargeError,
    )

    return (MediaUploadTooLargeError, MediaUploadConsumedError, MediaUploadError)


def _media_upload_error_status(exc: BaseException) -> int:
    from webapp.services.v2_lessons import MediaUploadTooLargeError

    return 413 if isinstance(exc, MediaUploadTooLargeError) else 422


def _media_upload_error_payload(exc: BaseException) -> dict[str, Any]:
    """上传错误 → 与 ImportValidationError 同构的结构化错误体。"""
    from webapp.services.v2_lessons import MediaUploadTooLargeError

    reason = "audio_too_large" if isinstance(exc, MediaUploadTooLargeError) else "invalid_audio"
    return {
        "code": "IMPORT_VALIDATION_FAILED",
        "message": "音频文件过大" if reason == "audio_too_large" else "音频文件无效",
        "errors": [{
            "index": 0, "line": 0, "reason": reason,
            "message": str(exc) or type(exc).__name__,
        }],
        "errorCount": 1,
    }


@router.get("/materials")
def list_base_materials(
    level: str | None = Query(default=None),
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    only_started: bool = Query(default=False),
    sort: str = Query(default="recent"),
):
    """材料库列表（P1）。`level` 认 cet4/cet6/unspecified，也认「四级」「六级」。"""
    try:
        return cet_materials.list_materials(
            level=level,
            query=q,
            category=category,
            include_archived=include_archived,
            only_started=only_started,
            sort=sort,
        )
    except ValueError as exc:
        raise _error(422, "INVALID_FILTER", str(exc)) from exc


@router.get("/materials/summary")
def base_materials_summary():
    """材料库概览：各级别数量、归档数、待分类数。空库返回全 0。"""
    return cet_materials.library_summary()


@router.get("/materials/{lesson_id}")
def get_base_material(lesson_id: int):
    try:
        return cet_materials.get_material(lesson_id)
    except KeyError as exc:
        raise _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", lessonId=lesson_id) from exc


@router.patch("/materials/{lesson_id}")
def patch_base_material(lesson_id: int, body: dict[str, Any]):
    """改材料信息 / 手动归类 / 归档恢复（任务书 P1）。

    请求体字段（全部可选）：
    ```json
    {"title": "...", "level": "cet4|cet6|unspecified", "category": "真题|练习|样例|开发夹具", "archived": false}
    ```
    `level="unspecified"` = 退回待分类；`category=""` = 清除类别标注。
    """
    allowed = {"title", "level", "category", "archived"}
    unknown = set(body or {}) - allowed
    if unknown:
        raise _error(
            422, "INVALID_FIELD",
            f"不支持的字段：{', '.join(sorted(unknown))}",
            allowed=sorted(allowed),
        )
    try:
        return cet_materials.update_material(
            lesson_id,
            level=body.get("level"),
            category=body.get("category"),
            title=body.get("title"),
            archived=body.get("archived"),
        )
    except KeyError as exc:
        raise _error(404, "MATERIAL_NOT_FOUND", "材料不存在。", lessonId=lesson_id) from exc
    except ValueError as exc:
        raise _error(422, "INVALID_VALUE", str(exc)) from exc


@router.get("/import/formats")
def import_formats():
    """真实支持的导入格式（界面照这份清单显示，不承诺未测格式）。"""
    return cet_import.supported_formats()


@router.post("/import/validate")
async def validate_import(
    subtitle: UploadFile | None = File(default=None),
    audio: UploadFile | None = File(default=None),
):
    """只校验，不导入（前端「校验」按钮用）。

    音频只用来取真实时长做时间轴上界；给了音频就走 ffprobe 真校验，
    没给音频就用字幕自身最大结束时间当上界（只做格式与顺序校验）。
    """
    audio_duration: float | None = None
    warnings: list[str] = []
    if audio is not None:
        payload = await audio.read()
        if not cet_import.audio_suffix_allowed(audio.filename or ""):
            raise _error(
                422, "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的音频格式：{audio.filename}",
                supported=list(cet_import.SUPPORTED_AUDIO_EXTS),
            )
        audio_duration = await run_in_threadpool(
            cet_import.probe_audio_duration, payload, audio.filename or ""
        )
        if audio_duration is None:
            warnings.append("无法读取音频时长，已跳过「不超音频时长」这一项校验。")

    if subtitle is None:
        return {
            "ok": True, "timed": False, "sentenceCount": 0, "errors": [],
            "audioDurationSec": audio_duration, "warnings": warnings,
        }

    raw = (await subtitle.read()).decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return {
            "ok": False, "timed": False, "sentenceCount": 0,
            "errors": [{
                "index": 0, "line": 0, "reason": "empty_file",
                "message": "字幕/文本文件是空的",
            }],
            "audioDurationSec": audio_duration, "warnings": warnings,
        }

    prepared = await run_in_threadpool(
        cet_import.prepare_subtitle,
        subtitle.filename or "subtitle.srt", raw,
        audio_duration=audio_duration,
    )
    return {
        "ok": not prepared["errors"],
        "format": prepared["format"],
        "timed": prepared["timed"],
        "sentenceCount": prepared["sentenceCount"],
        "errors": prepared["errors"],
        "errorCount": len(prepared["errors"]),
        "audioDurationSec": audio_duration,
        "declaredDurationSec": prepared.get("durationSec"),
        "warnings": warnings,
    }


@router.post("/import")
async def import_material(
    audio: UploadFile = File(...),
    title: str = Form(default=""),
    level: str = Form(default="unspecified"),
    category: str = Form(default=""),
    subtitle: UploadFile | None = File(default=None),
    allow_duplicate: bool = Form(default=False),
):
    """导入本地材料（P2）：音频必填，字幕/文本可选。

    返回 `{duplicate, lesson, lessonUrl, material, subtitle, warnings}`。
    校验失败返回 422 且带逐条 errors（含条目序号与行号），**不留下任何材料**。
    """
    audio_payload = await audio.read()
    if not audio_payload:
        raise _error(422, "EMPTY_AUDIO", "音频文件是空的")

    subtitle_payload: bytes | None = None
    subtitle_name = ""
    if subtitle is not None:
        subtitle_payload = await subtitle.read()
        subtitle_name = subtitle.filename or ""

    try:
        result = await run_in_threadpool(
            cet_import.import_local_material,
            audio_bytes=audio_payload,
            audio_filename=audio.filename or "",
            title=title,
            level=level,
            category=category,
            subtitle_bytes=subtitle_payload,
            subtitle_filename=subtitle_name,
            allow_duplicate=allow_duplicate,
        )
    except cet_import.ImportValidationError as exc:
        raise HTTPException(
            status_code=_validation_status(exc), detail=_validation_payload(exc)
        ) from exc
    except _media_upload_error_types() as exc:
        # BASE-03 缺陷 B：上传设施的错误（扩展名伪装、ffprobe 不认、空文件、
        # 超大小）必须映射成结构化 4xx。服务层已映射过一次，这里是第二道
        # 保险：任何绕过服务层映射的上传错误也不能漏成纯文本 500。
        raise HTTPException(
            status_code=_media_upload_error_status(exc),
            detail=_media_upload_error_payload(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # 真正的程序异常**不吞**：状态码仍是 500（不谎报成用户的输入错误），
        # 但响应体是结构化 JSON 而不是纯文本 "Internal Server Error"，
        # 前端能拿到可读原因，服务端日志仍保留完整栈。
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": "IMPORT_INTERNAL_ERROR",
                    "message": f"导入过程出现未预期错误：{type(exc).__name__}",
                    "errors": [{
                        "index": 0, "line": 0, "reason": "internal_error",
                        "message": str(exc) or type(exc).__name__,
                    }],
                    "errorCount": 1,
                },
            },
        )
    return result
