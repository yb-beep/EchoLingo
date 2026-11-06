import os

# 测试套件默认禁用 Docling 子进程（torch 加载慢且本机才装）；
# 需要真实 Docling 的测试自行 monkeypatch 打开并跳过不可用环境。
os.environ.setdefault("ELT_DOCLING", "off")

import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# ── P0.4 数据隔离护栏（2026-11-05 实测事故后新增）────────────────
# 事故：不加隔离直接跑 pytest，会把测试数据写进**正式库** `resources/vocab.db`
# （实测 md5 由 d2c9f47… 变为 8131637…）。原因是多数测试用
# `monkeypatch.setattr(db, "DB_PATH", tmp_path/"vocab.db")` 做隔离，
# 但没写这个 monkeypatch 的测试（以及 TestClient 里新建的 app）会回落到
# 模块级默认 `DB_PATH = resources/vocab.db`。
#
# 处理方式：在导入测试模块**之前**把默认库指向临时目录。
# 仍然支持两种既有写法：
#   1. monkeypatch.setattr(db, "DB_PATH", ...)  → 覆盖本默认值，照常生效
#   2. ELT_USER_DB 环境变量                     → 优先级更高，但**不要**在跑 pytest 时设置
#      （它会覆盖 monkeypatch，导致所有测试写进同一个库、互相污染）
_PRODUCTION_DB = Path(__file__).resolve().parents[1] / "resources" / "vocab.db"
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="echolingo-pytest-"))
_TEST_DB = _TEST_DB_DIR / "vocab.db"


def _install_db_guard() -> None:
    try:
        import db  # noqa: PLC0415  (必须在 sys.path 就绪后导入)
    except ImportError:
        return
    if Path(db.DB_PATH).resolve() == _PRODUCTION_DB.resolve():
        db.DB_PATH = _TEST_DB


_install_db_guard()


@pytest.fixture(scope="session", autouse=True)
def _forbid_production_db_writes():
    """兜底断言：整套测试跑完，正式库不得被创建/改动。

    与上面的默认重定向是双保险 —— 前者防「没写 monkeypatch」，
    后者防「某个测试显式把路径改回正式库」。
    """
    before = _PRODUCTION_DB.stat().st_mtime_ns if _PRODUCTION_DB.exists() else None
    yield
    after = _PRODUCTION_DB.stat().st_mtime_ns if _PRODUCTION_DB.exists() else None
    assert before == after, (
        "正式库 resources/vocab.db 在测试期间被修改了！"
        "请检查是否遗漏 monkeypatch.setattr(db, 'DB_PATH', tmp_path/'vocab.db')，"
        "或是否设置了 ELT_USER_DB 环境变量。"
    )


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """每个测试前清零内存限流器：register/login 的 IP 限额是全局状态，
    不重置会让整套件里大量注册/登录用例在 10 次后集体 429。"""
    try:
        from webapp.runtime import rate_limit
    except ImportError:
        yield
        return
    rate_limit._reset_all()
    yield
    rate_limit._reset_all()

