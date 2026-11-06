"""四六级底座 · 集中功能配置（cet_mode）与前端模式运行时（cet-mode.js）契约。

覆盖点：
1. `cet_mode.config()` 的功能键清单、双模式（ELT_CET_MODE=0/1）语义
2. 拼错的功能键必须显式失败（不能静默当作「已收束」）
3. `/api/cet/mode` 与模板注入同源
4. `cet-mode.js` 的静态契约：配置读取、hash 路由、DOM 防 null、失败降级
5. 收束的模块**不发起**后台请求（不是 CSS 遮挡）

为什么用静态断言而不是只跑浏览器：
前端运行时是纯静态资源，浏览器验证（T01/T12/T14）另有 Playwright 证据；
这里守住「不写第二套开关」「未知 hash 有兜底」这类容易回归的契约。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODE_JS = REPO / "frontend" / "static" / "cet-mode.js"
MODE_CSS = REPO / "frontend" / "static" / "cet-base.css"

# 冻结约定 §2 的功能键清单（拼错会 KeyError，这是故意的）
KEPT_FEATURES = {
    "listening_library", "local_import", "player", "transcript", "dictionary",
    "word_favorites", "sentence_favorites", "progress_resume", "settings_core",
    "ai_word_sentence_explain",
}
RETIRED_FEATURES = {
    "link_import", "reading_course", "text_to_speech_course", "video_learning",
    "memory_story", "scenario_sentence", "ielts_reuse", "free_retell",
    "ai_chat_general", "outline_generation", "credits_account",
}


def _cet_mode():
    from webapp import cet_mode

    return cet_mode


# ── 1. 集中配置本身 ──────────────────────────────────────────
def test_feature_key_registry_matches_frozen_contract():
    cfg = _cet_mode().config()
    assert set(cfg["features"]) == KEPT_FEATURES | RETIRED_FEATURES


def test_cet_mode_keeps_core_and_retires_generic_entries(monkeypatch):
    monkeypatch.setenv("ELT_CET_MODE", "1")
    cfg = _cet_mode().config()
    assert cfg["enabled"] is True
    for key in KEPT_FEATURES:
        assert cfg["features"][key]["enabled"] is True, f"{key} 应在 CET 模式保留"
    for key in RETIRED_FEATURES:
        assert cfg["features"][key]["enabled"] is False, f"{key} 应在 CET 模式收束"
    assert set(cfg["retired_keys"]) == RETIRED_FEATURES


def test_generic_mode_keeps_everything_for_original_regression(monkeypatch):
    """ELT_CET_MODE=0 必须完全退回通用模式：所有功能都可用。"""
    monkeypatch.setenv("ELT_CET_MODE", "0")
    cfg = _cet_mode().config()
    assert cfg["enabled"] is False
    assert all(item["enabled"] for item in cfg["features"].values())
    assert cfg["retired_keys"] == []
    # 导航回到原版四项
    labels = [item["label"] for item in cfg["nav"]]
    assert "课程" in labels and "词汇" in labels
    assert cfg["copy"]["home_heading"] == "课程"


def test_cet_nav_is_exactly_the_three_cet_entries(monkeypatch):
    monkeypatch.setenv("ELT_CET_MODE", "1")
    nav = _cet_mode().config()["nav"]
    assert [item["label"] for item in nav] == ["听力材料", "我的收藏", "设置"]
    assert [item["key"] for item in nav] == ["lessons", "favorites", "resources"]
    assert nav[0]["hash"] == "" and nav[1]["hash"] == "favorites"


def test_cet_copy_uses_cet_wording_not_generic(monkeypatch):
    monkeypatch.setenv("ELT_CET_MODE", "1")
    copy = _cet_mode().config()["copy"]
    assert "四六级" in copy["site_title"]
    assert copy["home_heading"] == "四六级听力材料"
    assert copy["vocab_heading"] == "词汇收藏"
    assert copy["sentences_heading"] == "句子收藏"
    assert copy["unspecified_level"] == "待分类"
    # 通用模式的「课程 / 阅读 / 词汇记忆工坊」不得残留
    assert "课程" not in copy["library_heading"]
    assert copy["transcript_label"] == "原文"


def test_cet_wordlists_are_only_cet4_and_cet6(monkeypatch):
    monkeypatch.setenv("ELT_CET_MODE", "1")
    assert _cet_mode().config()["wordlists"] == ["builtin_cet4", "builtin_cet6"]
    monkeypatch.setenv("ELT_CET_MODE", "0")
    assert _cet_mode().config()["wordlists"] == []


def test_unknown_feature_key_raises_instead_of_silently_disabling(monkeypatch):
    """拼错的开关名如果静默返回 False，会变成难查的「入口莫名消失」。"""
    monkeypatch.setenv("ELT_CET_MODE", "1")
    with pytest.raises(KeyError):
        _cet_mode().feature_enabled("link_imprt")


def test_template_context_matches_http_config(monkeypatch):
    """模板注入与 /api/cet/mode 必须同源，不能各写一套开关。"""
    monkeypatch.setenv("ELT_CET_MODE", "1")
    cet_mode = _cet_mode()
    ctx = cet_mode.template_context()
    cfg = cet_mode.config()
    assert ctx["cet_mode"] == cfg
    assert ctx["cet_on"] is True
    assert ctx["cet_copy"] == cfg["copy"]
    assert ctx["cet_nav"] == cfg["nav"]
    assert ctx["cet_features"] == {k: v["enabled"] for k, v in cfg["features"].items()}


def test_api_cet_mode_endpoint_returns_same_config(monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setenv("ELT_CET_MODE", "1")
    resp = TestClient(create_app()).get("/api/cet/mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body == _cet_mode().config()
    assert body["features"]["link_import"]["enabled"] is False


# ── 2. cet-mode.js 静态契约 ──────────────────────────────────
def test_mode_js_exists_and_is_syntax_valid():
    assert MODE_JS.exists(), "frontend/static/cet-mode.js 缺失"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    result = subprocess.run([node, "--check", str(MODE_JS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mode_js_reads_the_single_config_source():
    text = MODE_JS.read_text(encoding="utf-8")
    assert "fetch('/api/cet/mode'" in text
    # 首屏注入（JSON 数据块）与接口两处都读，但不允许出现第二套写死的开关表
    assert "cet-boot" in text
    assert "window.CET" in text
    # 契约要求所有功能键都能被查询
    for key in KEPT_FEATURES | RETIRED_FEATURES:
        assert f"{key}:" in text, f"cet-mode.js 缺少功能键默认值 {key}"


def test_mode_js_degrades_without_blank_page():
    text = MODE_JS.read_text(encoding="utf-8")
    # 接口失败：沿用注入值 / CET 默认值，不抛到顶层
    assert ".catch(function () {" in text
    assert "return null; // 接口失败：沿用注入值，不白屏" in text
    # 请求有超时，避免一直挂着导致导航不渲染
    assert "AbortController" in text
    assert "4000" in text


def test_mode_js_guards_null_dom_operations():
    text = MODE_JS.read_text(encoding="utf-8")
    # 每个 getElementById 之后都必须有存在性判断，不能直接解引用
    for match in re.finditer(r"document\.getElementById\(([^)]*)\)", text):
        tail = text[match.end():match.end() + 120]
        assert re.search(r"(if \(|if\(|\?\.|\|\||&&|\)\s*;)", tail), (
            f"getElementById 后未防 null: {text[match.start():match.end() + 60]!r}")


def test_mode_js_handles_hash_routing_including_favorites():
    text = MODE_JS.read_text(encoding="utf-8")
    # D-HOME-1：导航项 #favorites 必须能切页，不能是死链接
    assert "'favorites': 'favorites'" in text
    assert "'vocab': 'vocab'" in text
    assert "'sentences': 'sentences'" in text
    assert "'resources': 'resources'" in text
    assert "'settings': 'resources'" in text
    # 浏览器前进/后退
    assert "addEventListener('hashchange'" in text
    # 未知/已收束 hash：回首页 + 明确说明，不留空白页
    assert "RETIRED_HASH_HINT" in text
    assert "已收束，已返回听力材料" in text
    assert "没有这个页面，已返回听力材料" in text


def test_mode_js_removes_retired_nodes_instead_of_css_hiding():
    text = MODE_JS.read_text(encoding="utf-8")
    assert "data-cet-feature" in text
    assert "el.remove();" in text, "收束节点必须真移除（避免隐藏按钮仍可 Tab 聚焦）"
    # 不允许用 CSS 遮挡冒充收束
    assert "display = 'none'" not in text
    assert "display:none" not in text


def test_mode_js_supports_favorites_sub_pane():
    text = MODE_JS.read_text(encoding="utf-8")
    assert "cet-favorites-pane" in text
    assert "'sentences'" in text and "'vocab'" in text


# ── 3. cet-base.css 契约 ─────────────────────────────────────
def test_base_css_reuses_existing_variables():
    assert MODE_CSS.exists(), "frontend/static/cet-base.css 缺失"
    css = MODE_CSS.read_text(encoding="utf-8")
    for var in ("--brand", "--ink", "--line", "--surface", "--muted"):
        assert f"var({var})" in css, f"cet-base.css 未复用既有变量 {var}"
    # 不重做视觉：不得定义新的根变量表
    assert ":root" not in css


def test_base_css_covers_narrow_viewports():
    css = MODE_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css
    assert "@media (max-width: 420px)" in css
    # 窄窗口不允许横向溢出
    assert "grid-template-columns: 1fr;" in css


# ── 4. 收束不是 CSS 遮挡（模板层） ───────────────────────────
def _render_home(monkeypatch, mode="1"):
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setenv("ELT_CET_MODE", mode)
    resp = TestClient(create_app()).get("/")
    assert resp.status_code == 200
    return resp.text


def test_retired_source_cards_are_absent_from_cet_dom(monkeypatch):
    html = _render_home(monkeypatch, "1")
    # 收束的入口不是被 CSS 藏起来，而是根本不在 DOM 里
    for marker in (
        'id="link-source-card"',
        'id="baidu-pan-source-card"',
        'id="reading-text-input"',
        'id="reading-file-input"',
        'id="text-tts"',
        'id="vocab-story-view"',
        'id="story-history-panel"',
        'id="credit-badge"',
        'id="login-btn"',
        'id="user-menu"',
        'id="planning-tab-link"',
    ):
        assert marker not in html, f"CET 模式下不应渲染 {marker}"


def test_generic_mode_still_renders_every_legacy_entry(monkeypatch):
    """ELT_CET_MODE=0 必须保留原版全部入口（原版回归）。"""
    html = _render_home(monkeypatch, "0")
    for marker in (
        'id="link-source-card"',
        'id="baidu-pan-source-card"',
        'id="reading-text-input"',
        'id="text-tts"',
        'id="vocab-story-view"',
        'id="credit-badge"',
        'id="planning-tab-link"',
        'id="course-search"',
    ):
        assert marker in html, f"通用模式应保留 {marker}"


def _nav_bar_html(html: str) -> str:
    """取出导航条本身的 HTML（只看用户可见入口，不看脚本注释里的字眼）。"""
    start = html.index('<div class="tab-bar"')
    return html[start:html.index("</div>", html.index("</button>", start)) + 6]


def test_cet_home_renders_nav_from_config(monkeypatch):
    html = _render_home(monkeypatch, "1")
    nav = _nav_bar_html(html)
    assert 'id="tab-btn-lessons"' in nav
    assert 'id="tab-btn-favorites"' in nav
    assert 'id="tab-btn-resources"' in nav
    assert "听力材料" in nav and "我的收藏" in nav
    # 通用模式的导航项不再出现在导航条里（任务书 §3.2）
    assert "📺 课程" not in nav
    assert "个人档案" not in nav
    assert "planning-tab-link" not in nav
    # 导航项只有三项，不多不少
    assert nav.count('class="tab-btn') == 3


def test_cet_home_keeps_shared_asset_inclusions(monkeypatch):
    html = _render_home(monkeypatch, "1")
    # 既有共享资产契约（tests/test_natural_tts.py / test_elt_credits_public_contract.py）
    assert "/static/natural-tts.js" in html
    assert "/static/elt_credits.js" in html
    assert "NaturalTTS.speak" in html
    # 新增的 CET 资产
    assert "/static/cet-base.css" in html
    assert "/static/cet-mode.js" in html


def test_cet_home_boot_payload_is_json_not_executable_script(monkeypatch):
    html = _render_home(monkeypatch, "1")
    match = re.search(r'<script type="application/json" id="cet-boot">(.*?)</script>', html, re.S)
    assert match, "缺少 cet-boot 首屏配置注入"
    payload = json.loads(match.group(1))
    assert payload["enabled"] is True
    assert payload["features"]["link_import"]["enabled"] is False
    assert payload["copy"]["home_heading"] == "四六级听力材料"
