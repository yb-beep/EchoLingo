"""Phase 7B native FastAPI page routes migrated from Flask templates."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from webapp import cet_mode
from webapp.runtime import ai_config

router = APIRouter()
templates = Jinja2Templates(directory=str(ai_config.BASE_DIR / "frontend" / "templates"))


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
    return templates.TemplateResponse(
        request, "workspace.html", {"lesson_id": lesson_id, **cet_mode.template_context()}
    )


@router.get("/workspace/{lesson_id}/intensive")
def intensive_workspace(request: Request, lesson_id: int):
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
