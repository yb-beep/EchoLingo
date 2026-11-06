"""CET 底座 · 真实 app 路由注册契约（防「测试绿但产品 404」）。

背景（2026-11-05 集成期实际发生的事故）：
`cet_base.py` 实现了完整的 P1/P2 路由，但 `fastapi_server.py` 漏了
`app.include_router(cet_base_router)`。而单元测试是这么写的：

    app = FastAPI()                       # 自己造一个空 app
    app.include_router(cet_base_router)   # 手动挂上被测 router
    TestClient(app)

**测试自己挂了 router，所以永远绿；真实 create_app() 没挂，所以线上永远 404。**
88 个用例全部有效，却证明不了「用户能用」。

本文件只做一件事：对着 **真实 create_app()** 断言这些路由存在。
不自己拼 app，不 mock 路由表。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 底座路由：路径 + 方法。新增端点时同步加进这里，否则本测试不会覆盖它。
REQUIRED_CET_BASE_ROUTES = [
    ("GET", "/api/cet/base/materials"),
    ("GET", "/api/cet/base/materials/summary"),
    ("GET", "/api/cet/base/materials/{lesson_id}"),
    ("PATCH", "/api/cet/base/materials/{lesson_id}"),
    ("GET", "/api/cet/base/import/formats"),
    ("POST", "/api/cet/base/import/validate"),
    ("POST", "/api/cet/base/import"),
]

# 既有 CET 路由（队友模块）也必须仍在，不能被底座改造挤掉
REQUIRED_CET_ROUTES = [
    ("GET", "/api/cet/mode"),
    ("GET", "/api/cet/materials"),
    ("GET", "/api/cet/materials/by-lesson/{lesson_id}"),
    ("POST", "/api/cet/attempts"),
    ("POST", "/api/cet/marks"),
    ("POST", "/api/cet/practices"),
]


@pytest.fixture(scope="module")
def real_app_paths():
    """真实 create_app() 的路径→方法集合。用 openapi() 取，跨 FastAPI 版本稳定。"""
    import db
    import tempfile

    # 隔离库：不在正式库上建表
    tmp = Path(tempfile.mkdtemp(prefix="cet-route-contract-")) / "vocab.db"
    old_env = os.environ.pop("ELT_USER_DB", None)
    old_path = db.DB_PATH
    db.DB_PATH = tmp
    try:
        from fastapi_server import create_app

        spec = create_app().openapi()
    finally:
        db.DB_PATH = old_path
        if old_env is not None:
            os.environ["ELT_USER_DB"] = old_env

    return {
        path: {method.upper() for method in ops}
        for path, ops in (spec.get("paths") or {}).items()
    }


@pytest.mark.parametrize("method,path", REQUIRED_CET_BASE_ROUTES)
def test_cet_base_route_registered_in_real_app(real_app_paths, method, path):
    assert path in real_app_paths, (
        f"底座路由 {path} 未注册到真实 app。"
        f"检查 fastapi_server.py 是否漏了 app.include_router(cet_base_router)。"
        f"（单元测试自己挂 router 会掩盖这个错误）"
    )
    assert method in real_app_paths[path], (
        f"{path} 缺少 {method} 方法，实际: {sorted(real_app_paths[path])}")


@pytest.mark.parametrize("method,path", REQUIRED_CET_ROUTES)
def test_existing_cet_route_still_registered(real_app_paths, method, path):
    """底座改造不得挤掉队友模块的既有路由。"""
    assert path in real_app_paths, f"既有 CET 路由 {path} 丢失"
    assert method in real_app_paths[path], (
        f"{path} 缺少 {method}，实际: {sorted(real_app_paths[path])}")


def test_cet_mode_endpoint_returns_feature_flags(real_app_paths):
    """集中功能配置接口必须存在（前端 cet-mode.js 依赖它）。"""
    assert "/api/cet/mode" in real_app_paths


def test_page_routes_exist(real_app_paths):
    """四个页面路由仍在（改造不得删页面）。"""
    for path in ("/", "/vocab", "/workspace/{lesson_id}", "/workspace/{lesson_id}/intensive"):
        assert path in real_app_paths, f"页面路由 {path} 丢失"
