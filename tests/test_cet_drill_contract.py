"""卡点融合（cet-drill）· 跨包静态契约测试（主代理新增）。

这些用例只校验**接口/模板/模块的静态契约**，不依赖浏览器与真实服务；
浏览器端到端验收由 `tests/test_cet_drill_acceptance.py` 在设置
`CET_DRILL_BASE_URL` 时执行。

覆盖范围：
- K：三步状态机、blind 不泄露原文、旧二态按钮已删除、`?review=1` 深链、离线释义 DOM。
- W：`window.__ws` 六个新增接口、步骤条按开关渲染、四处缓存串。
- H：`GET /api/cet/summary` 路由注册与空库响应结构。
- I：`intensive.html` 的「我的卡点」筛选与卡点徽标。
- D：占位题窄过滤规则、无 key 的 explain/transfer 降级结构。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _strip_js_comments(text: str) -> str:
    """去掉 // 行注释与 /* */ 块注释（只为「某符号必须不存在」类断言服务）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", "", text)
    return text


# ── K 卡点前端 ─────────────────────────────────────────────


def test_k_step_machine_contract():
    js = _read("frontend/static/cet-listening.js")
    assert "STEP_KEYS = ['blind', 'review', 'drill']" in js
    assert "cet:step-change" in js
    assert "get step() { return S.step; }" in js
    assert "hasQuestions: S.questions.length > 0" in js
    assert "submitted: !!S.attempt" in js


def test_k_blind_step_hides_transcript():
    js = _read("frontend/static/cet-listening.js")
    # blind 强制隐藏；review（非复习盲听）/drill 显示
    assert "S.step === 'blind'" in js
    assert "setTranscriptHidden" in js
    assert "__ws.setTranscriptHidden" not in js or "typeof api.setTranscriptHidden" in js


def test_k_legacy_toggle_button_removed():
    js = _strip_js_comments(_read("frontend/static/cet-listening.js"))
    assert "cet-toggle-mode" not in js, "旧「开始卡点练习」按钮仍被创建"
    assert "开始卡点练习" not in js, "旧二态按钮文案仍在可执行代码里"
    assert "cet-step-primary" in js


def test_k_review_query_deeplink():
    js = _read("frontend/static/cet-listening.js")
    assert "new URLSearchParams(location.search).get('review') === '1'" in js
    assert "startReview()" in js


def test_k_offline_explain_rendering():
    js = _read("frontend/static/cet-listening.js")
    assert "data.mode === 'offline'" in js
    assert "cet-offline-explain" in js
    # hint 由包 D 的响应提供，前端只负责渲染，不硬编码文案
    assert "data.hint" in js
    assert "cet-offline-word-meaning" in js


def test_k_evidence_button_play_sentence():
    js = _read("frontend/static/cet-listening.js")
    assert "cet-evidence-btn" in js
    assert "▶ 听原声证据" in js
    assert "playSentenceBySentence" in js or "playSentence" in js


# ── W 工作区布局 ───────────────────────────────────────────


def test_w_ws_interface_all_six_methods():
    html = _read("frontend/templates/workspace.html")
    for token in (
        "playSentence: (index, options) => wsPlaySentence(index, options)",
        "stopSentence: () => wsStopSentence()",
        "setTranscriptHidden: hidden => wsSetTranscriptHidden(hidden)",
        "mountStepPanel: (stepKey, element) => wsMountStepPanel(stepKey, element)",
        "setStepActions: element => wsSetStepActions(element)",
        "registerHotkeyHelp: entry => wsRegisterHotkeyHelp(entry)",
    ):
        assert token in html, f"window.__ws 缺少接口：{token}"
    assert "get transcriptHidden()" in html


def test_w_step_bar_gated_by_feature():
    html = _read("frontend/templates/workspace.html")
    assert 'id="ws-step-bar"' in html
    idx = html.index('id="ws-step-bar"')
    prelude = html[max(0, idx - 400):idx]
    assert "cet_features.cet_listening_agent" in prelude
    assert "cet_on" in prelude


def test_w_cache_busters():
    ws = _read("frontend/templates/workspace.html")
    assert "/static/cet-listening.css?v=20261107-drill-k" in ws
    assert "/static/cet-listening.js?v=20261107-drill-k" in ws
    assert "/static/cet-workspace.css?v=20261107-drill-w" in ws
    assert "/static/cet-player.js?v=20261107-drill-w" in ws
    index = _read("frontend/templates/index.html")
    assert "/static/cet-base.css?v=20261107-drill-h" in index


def test_w_play_sentence_restores_rate():
    js = _read("frontend/static/cet-player.js")
    assert "function playSentence(index, options)" in js
    assert "rateProvided" in js
    assert "applyPlaybackRate(session.previousRate)" in js
    assert "interruptSentencePlayback" in js


# ── H 首页与收藏 ───────────────────────────────────────────


def test_h_summary_route_registered():
    sys.path.insert(0, str(ROOT / "backend"))
    from webapp.fastapi_routes.cet import router  # noqa: E402

    paths = {route.path for route in router.routes}
    assert "/api/cet/summary" in paths


def test_h_summary_empty_db_shape(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "backend"))
    import db as db_module  # noqa: E402
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from webapp.fastapi_routes.cet import router  # noqa: E402
    from webapp.services import cet_service  # noqa: E402

    monkeypatch.setenv("ELT_USER_DB", str(tmp_path / "drill-summary.db"))
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "drill-summary.db")
    db_module.init_db()
    cet_service.init_cet_schema()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.get("/api/cet/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"materials", "reviewDue"}
    assert body["materials"] == {}
    assert body["reviewDue"] == {"marks": 0, "sentences": 0, "words": 0}


# ── I 逐句精听 ─────────────────────────────────────────────


def test_i_marks_filter_and_badge():
    html = _read("frontend/templates/intensive.html")
    assert 'data-filter="marks"' in html
    assert 'id="count-marks"' in html
    assert "CET_MARKS_ON" in html
    assert "sentence-cet-mark" in html
    assert "我的卡点" in html


# ── D 数据与离线兜底 ───────────────────────────────────────


def _load_import_materials():
    path = ROOT / "scripts" / "cet" / "import_materials.py"
    spec = importlib.util.spec_from_file_location("cet_drill_import_materials", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_d_placeholder_filter_is_narrow():
    module = _load_import_materials()
    placeholder = {
        "stem": "（样例占位）这道题故意不给参考答案",
        "answer": None,
    }
    assert module._is_sample_placeholder(placeholder) is True
    # answer=0 是合法选项 A，不能误删
    assert module._is_sample_placeholder({
        "stem": "（样例占位）这道题答案是 A", "answer": 0,
    }) is False
    # 题干不含占位词，即使 answer 缺失也不删
    assert module._is_sample_placeholder({
        "stem": "What does the speaker mean?", "answer": None,
    }) is False


def test_d_offline_explain_and_transfer(monkeypatch):
    sys.path.insert(0, str(ROOT / "backend"))
    from webapp.services import cet_ai  # noqa: E402

    monkeypatch.setattr(cet_ai.ai_config, "AI_API_KEY", "")
    sentence = {"text": "It's getting late.", "startSeconds": 0.0, "endSeconds": 2.0}
    raw, model = cet_ai.generate("explain", sentence, "meaning")
    assert model == "offline"
    assert raw["mode"] == "offline"
    assert raw["hint"] == "配置模型后可获得讲解"
    assert isinstance(raw["words"], list) and raw["words"]
    for item in raw["words"]:
        assert {"word", "ipa", "meaning"} <= set(item)

    transfer, model = cet_ai.generate("transfer", sentence, "heard", candidate={"text": "x"})
    assert model == "offline"
    assert transfer.get("noCandidate") is True
    assert transfer.get("reason") == "offline"
