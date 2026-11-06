"""返修2 包5 · P5-2：不存在的材料必须 404 + 返回链接（仅 CET 模式）。

背景（任务书 P5-2 实测）：
    `/workspace/999`（999 不存在）原本返回 **200 + 空工作区**，
    `/workspace/999/intensive` 同样 200 + 空精听页。用户点开一个坏链接，
    看到的是空壳页面而不是「材料没了」，无法判断该返回还是该等。

本文件的断言口径：
    - CET 模式（默认）：两个 URL 都是 404，页面含「材料不存在或已删除」+「返回听力材料」链接；
    - 存在的材料仍然 200（不误伤正常材料）；
    - 通用模式（`ELT_CET_MODE=0`）：**保持原行为 200 空页**，不出现 CET 404 文案
      —— 任务书 §0.6 要求通用模式行为不变。

对着**真实 create_app()** 测，不自己拼 app（理由见 tests/test_cet_base_routes_registered.py）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

MISSING_LESSON_ID = 999999  # 刻意取一个建不出来的 id

MISSING_COPY = "材料不存在或已删除"
BACK_LINK_COPY = "返回听力材料"


def _client(tmp_path, monkeypatch):
    import db
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    return _ClientFactory(create_app(), db)


class _ClientFactory:
    """薄封装：只为了在同一次调用里拿到 db 模块，便于建课。"""

    def __init__(self, app, db_module):
        from fastapi.testclient import TestClient

        self._client = TestClient(app)
        self.db = db_module

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)

    def make_lesson(self, title: str = "P5-2 样例材料") -> int:
        lesson = self.db.create_v2_lesson(
            source_type="local",
            source_url=f"p5-2-{title}-{os.getpid()}",
            title=title,
        )
        return lesson["id"]


# ── CET 模式（默认）────────────────────────────────────────────────


def test_missing_lesson_workspace_returns_404_in_cet_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    c = _client(tmp_path, monkeypatch)

    resp = c.get(f"/workspace/{MISSING_LESSON_ID}")

    assert resp.status_code == 404, (
        f"不存在的 lesson 必须 404，实际 {resp.status_code}（P5-2 回归）"
    )
    assert MISSING_COPY in resp.text
    assert BACK_LINK_COPY in resp.text


def test_missing_lesson_intensive_returns_404_in_cet_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    c = _client(tmp_path, monkeypatch)

    resp = c.get(f"/workspace/{MISSING_LESSON_ID}/intensive")

    assert resp.status_code == 404, (
        f"不存在的 lesson 精听页必须 404，实际 {resp.status_code}（P5-2 回归）"
    )
    assert MISSING_COPY in resp.text
    assert BACK_LINK_COPY in resp.text


def test_404_page_has_back_link_to_library(tmp_path, monkeypatch):
    """返回链接必须指向材料库首页 `/`，否则用户还是出不去。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    c = _client(tmp_path, monkeypatch)

    resp = c.get(f"/workspace/{MISSING_LESSON_ID}")

    assert 'href="/"' in resp.text, "404 页必须含指向 / 的返回链接"
    # 链接文案与 href 必须在同一个 <a> 里
    assert f'href="/"' in resp.text and BACK_LINK_COPY in resp.text


def test_existing_lesson_still_renders_workspace(tmp_path, monkeypatch):
    """反向保护：存在的材料不能被 404 误伤。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    c = _client(tmp_path, monkeypatch)
    lesson_id = c.make_lesson()

    resp = c.get(f"/workspace/{lesson_id}")

    assert resp.status_code == 200, f"存在的材料必须 200，实际 {resp.status_code}"
    assert MISSING_COPY not in resp.text


def test_existing_lesson_still_renders_intensive(tmp_path, monkeypatch):
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    c = _client(tmp_path, monkeypatch)
    lesson_id = c.make_lesson("P5-2 精听样例")

    resp = c.get(f"/workspace/{lesson_id}/intensive")

    assert resp.status_code == 200, f"存在的材料精听页必须 200，实际 {resp.status_code}"
    assert MISSING_COPY not in resp.text


# ── 通用模式回归（§0.6：行为不变）──────────────────────────────────


def test_missing_lesson_workspace_stays_200_in_general_mode(tmp_path, monkeypatch):
    """`ELT_CET_MODE=0`：原版对不存在 lesson 渲染 200 空页，必须保持不变。"""
    monkeypatch.setenv("ELT_CET_MODE", "0")
    c = _client(tmp_path, monkeypatch)

    resp = c.get(f"/workspace/{MISSING_LESSON_ID}")

    assert resp.status_code == 200, (
        f"通用模式行为必须不变（原为 200），实际 {resp.status_code}"
    )
    assert MISSING_COPY not in resp.text, "通用模式不应出现 CET 模式的 404 文案"


def test_missing_lesson_intensive_stays_200_in_general_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ELT_CET_MODE", "0")
    c = _client(tmp_path, monkeypatch)

    resp = c.get(f"/workspace/{MISSING_LESSON_ID}/intensive")

    assert resp.status_code == 200, (
        f"通用模式行为必须不变（原为 200），实际 {resp.status_code}"
    )
    assert MISSING_COPY not in resp.text, "通用模式不应出现 CET 模式的 404 文案"
