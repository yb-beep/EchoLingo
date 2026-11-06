"""公开库/单用户兼容测试：私有模块（webapp.auth、auth/admin_private/credits 路由）
缺失时 fastapi_server 仍能以单用户模式启动。

模拟方式：在子进程中安装 meta_path blocker，对私有模块名抛 ImportError，
等价于公开库不含这些文件；然后 import fastapi_server 并 create_app。
该测试本身可同步到公开库（不 import 任何私有模块）。
"""
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CHILD_SCRIPT = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED_PREFIXES = (
        "webapp.auth",
        "webapp.fastapi_routes.auth",
        "webapp.fastapi_routes.admin_private",
        "webapp.fastapi_routes.credits",
    )

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == p or fullname.startswith(p + ".") for p in BLOCKED_PREFIXES):
                raise ImportError(f"blocked private module: {fullname}")
            return None

    sys.meta_path.insert(0, _Blocker())
    sys.path.insert(0, "__BACKEND_DIR__")

    # P0.4 数据隔离：子进程不继承父进程 conftest 对 db.DB_PATH 的重定向，
    # create_app() 会回落到默认 resources/vocab.db 并建库 —— 等于测试写用户正式库。
    # 注意：仓库根 `.env` 里有 ELT_USER_DB，analyzer/ai_config 在 import 期
    # load_dotenv() 会把它注入 os.environ，而 db.current_db_path() 的优先级是
    # contextvar → ELT_USER_DB → DB_PATH，所以必须在 import fastapi_server
    # **之后**再清一次环境变量并重定向，否则会被 .env 覆盖。
    import os, tempfile
    from pathlib import Path as _Path
    _tmp_db = _Path(tempfile.mkdtemp(prefix="elt-single-user-")) / "vocab.db"

    import db as _db

    import fastapi_server

    os.environ.pop("ELT_USER_DB", None)
    _db.DB_PATH = _tmp_db
    # 注意：不能 set _current_db_path() —— db.current_user_root() 把 contextvar
    # 当作「多用户请求上下文」标志，设了会让 current_output_root() 返回
    # <tmp>/output 而不是全局 output，破坏本用例要验证的单用户语义。
    # 只改 DB_PATH + 清 env 就足够让 current_db_path() 回落到临时库。

    assert fastapi_server.auth_router is None, "auth_router 应降级为 None"
    assert fastapi_server.admin_private_router is None, "admin_private_router 应降级为 None"
    assert fastapi_server.credits_router is None, "credits_router 应降级为 None"

    app = fastapi_server.create_app()
    # FastAPI 0.162+/Starlette 1.10+ 的 app.routes 里含 _IncludedRouter 包装对象
    # （没有 .path 属性，真实路由挂在 .routes 里）。用 openapi() 取权威路径集合，
    # 既跨版本稳定，也不依赖内部结构。
    paths = set(app.openapi().get("paths", {}))
    assert "/health" in paths, f"/health 缺失: {sorted(paths)}"
    assert not any(p.startswith("/api/auth") for p in paths), "公开库不应注册 auth 路由"
    assert not any(p.startswith("/api/credits") for p in paths), "公开库不应注册 credits 路由"

    from webapp.runtime import access, credit_meter
    from webapp.storage import user_assets

    assert access.multiuser_enabled() is False, "无 auth 模块时应为单用户模式"
    assert credit_meter.mode() == "off", f"无 auth 模块时计费应 off，实际 {credit_meter.mode()}"
    assert credit_meter.billing_active() is False

    from pathlib import Path
    root = user_assets.current_output_root(Path("output"))
    assert root == Path("output"), f"单用户 output 应回落全局目录，实际 {root}"

    assert str(_db.current_db_path()) == str(_tmp_db), (
        f"子进程应使用自己的临时库，实际 {_db.current_db_path()}")
    print("PUBLIC_SINGLE_USER_COMPAT_OK")
    """
)


def test_fastapi_server_starts_single_user_without_private_modules():
    backend = str(REPO / "backend")
    script = CHILD_SCRIPT.replace("__BACKEND_DIR__", backend.replace("\\", "\\\\"))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"单用户兼容子进程失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "PUBLIC_SINGLE_USER_COMPAT_OK" in proc.stdout
