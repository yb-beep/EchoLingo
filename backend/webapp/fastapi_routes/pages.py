"""Phase 7B native FastAPI page routes migrated from Flask templates."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import db
from webapp import cet_mode
from webapp.runtime import ai_config

router = APIRouter()
templates = Jinja2Templates(directory=str(ai_config.BASE_DIR / "frontend" / "templates"))


def _lesson_missing(request: Request, lesson_id: int):
    """不存在的 lesson → 404 页面（仅 CET 模式）。

    返修2 P5-2：原版对不存在的 lesson 一律渲染 200 空工作区/空精听页，
    用户看到一个空壳页却不知道是材料没了。CET 模式改为 404 + 明确文案 + 返回入口。

    通用模式（`ELT_CET_MODE=0`）保持原行为不变（仍是 200 空页）—— 任务书 §0.6
    要求通用模式行为不变，本函数只在 CET 模式下被调用。
    """
    return templates.TemplateResponse(
        request,
        "cet_404.html",
        {"lesson_id": lesson_id, **cet_mode.template_context()},
        status_code=404,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _lesson_exists(lesson_id: int) -> bool:
    """复用 db.get_v2_lesson（既有查询函数），不新写一套连接逻辑。"""
    try:
        return db.get_v2_lesson(lesson_id) is not None
    except Exception:  # noqa: BLE001
        # 查库异常不把页面打成 500：退回原行为（渲染工作区），避免误伤正常材料。
        return True


@router.get("/")
def index(request: Request):
    # 四六级专用底座：模板需要的开关/文案全部来自 cet_mode 这一份集中配置，
    # 不在模板里散落判断（任务书 §3.3「优先通过集中功能配置控制」）。
    return templates.TemplateResponse(request, "index.html", cet_mode.template_context())


@router.get("/vocab")
def vocab_page(request: Request):
    return templates.TemplateResponse(request, "vocab.html", cet_mode.template_context())


@router.get("/workspace/{lesson_id}")
def workspace(request: Request, lesson_id: int):
    # 返修2 P5-2：CET 模式下不存在的材料返回 404 页（通用模式保持原 200 空页）
    if cet_mode.cet_mode_enabled() and not _lesson_exists(lesson_id):
        return _lesson_missing(request, lesson_id)
    return templates.TemplateResponse(
        request, "workspace.html", {"lesson_id": lesson_id, **cet_mode.template_context()}
    )


@router.get("/workspace/{lesson_id}/intensive")
def intensive_workspace(request: Request, lesson_id: int):
    # 返修2 P5-2：同上，精听页也返回 404（原为 200 空精听页）
    if cet_mode.cet_mode_enabled() and not _lesson_exists(lesson_id):
        return _lesson_missing(request, lesson_id)
    return templates.TemplateResponse(
        request,
        "intensive.html",
        {"lesson_id": lesson_id, **cet_mode.template_context()},
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


# 账号相关模板仅存在于私有库/云端：公开库不含这些模板时对应路由不注册（404 而非 500）
if (ai_config.BASE_DIR / "frontend" / "templates" / "login.html").exists():

    @router.get("/login")
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html")

    @router.get("/register")
    def register_page(request: Request):
        return templates.TemplateResponse(request, "register.html")

    @router.get("/survey")
    def survey_page(request: Request):
        return templates.TemplateResponse(request, "survey.html")

    @router.get("/account")
    def account_page(request: Request):
        return templates.TemplateResponse(request, "account.html")


if (ai_config.BASE_DIR / "frontend" / "templates" / "planning.html").exists():

    @router.get("/planning")
    def planning_page(request: Request):
        return templates.TemplateResponse(
            request,
            "planning.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
