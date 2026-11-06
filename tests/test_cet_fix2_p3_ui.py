"""返修2 · 包3 · P3-1 前端契约：导入面板「用本地模型自动切句」选项。

这些是**模板与前端逻辑契约**断言（真实浏览器证据见
`docs/cet-base/返修2/包3-交付说明.md` 里的截图与点击记录）。

守住的几条（对应任务书 P3-1 的硬要求）：
1. 选项存在、文案是「用本地模型自动切句（无需 API key）」；
2. **默认不勾**（HTML 里没有 checked，JS 重置也回到不勾）；
3. 有带时间轴的字幕（SRT/VTT/JSON）时 disabled + 说明原因；
4. `faster_whisper` 不可用时 disabled + 说明原因（能力由后端下发，不写死）；
5. 提交表单时只在「可勾且勾了」才传 `auto_split=true`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "frontend" / "templates" / "index.html"
INDEX_TEXT = INDEX.read_text(encoding="utf-8")


def _import_panel(html: str) -> str:
    """只取导入面板区块（包3 的改动范围），避免误命中别处的同名元素。

    边界用 `cet-import-panel` 的 div 到「链接导入卡」之前
    （渲染后 Jinja 注释已消失，不能用 `{% else %}` 定位）。
    """
    start = html.index('<div class="gen-panel cet-import-panel" id="cet-import-panel">')
    for end_marker in ('id="cet-import-errors"', 'course-source-title'):
        candidate = html.find(end_marker, start)
        if candidate != -1:
            # 向后延伸到该区块结束的 </div> 之后 2000 字符，覆盖整个导入面板
            return html[start:candidate + 2000]
    return html[start:start + 12000]


def _script_block(html: str, function_name: str) -> str:
    """取某个 JS 函数体（从 `function <name>(` 到下一个顶层 `function `）。"""
    marker = f"function {function_name}("
    start = html.index(marker)
    following = html.find("\nfunction ", start + len(marker))
    return html[start:following if following != -1 else len(html)]


def _render(monkeypatch, mode="1"):
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setenv("ELT_CET_MODE", mode)
    resp = TestClient(create_app()).get("/")
    assert resp.status_code == 200
    return resp.text


@pytest.fixture()
def cet_html(monkeypatch):
    return _render(monkeypatch, "1")


@pytest.fixture()
def generic_html(monkeypatch):
    return _render(monkeypatch, "0")


# ── ① 选项存在且默认不勾 ────────────────────────────────────


def test_auto_split_option_exists_in_cet_import_panel(cet_html):
    panel = _import_panel(cet_html)
    assert 'id="cet-auto-split"' in panel
    assert 'id="cet-auto-split-box"' in panel
    assert 'id="cet-auto-split-note"' in panel
    assert "用本地模型自动切句（无需 API key）" in panel


def test_auto_split_is_unchecked_by_default(cet_html):
    """默认不勾是硬要求：HTML 里不能出现 checked 属性。"""
    panel = _import_panel(cet_html)
    tag = re.search(r'<input[^>]*id="cet-auto-split"[^>]*>', panel)
    assert tag is not None, "找不到自动切句复选框"
    assert "checked" not in tag.group(0)


def test_auto_split_not_checked_in_generic_mode_either(generic_html):
    """通用模式不渲染 CET 导入面板，自然也没有这个选项（行为不变）。"""
    assert 'id="cet-import-panel"' not in generic_html
    assert 'id="cet-auto-split"' not in generic_html


def test_reset_returns_option_to_unchecked():
    """「再导入一份」必须把自动切句恢复成默认不勾。"""
    body = _script_block(INDEX_TEXT, "cetResetImport")
    assert "cet-auto-split" in body
    assert "checked = false" in body
    assert "cetSyncAutoSplitOption()" in body


# ── ② 有带时间轴字幕时 disabled + 说明原因 ──────────────────


def test_subtitle_timed_detection_covers_srt_vtt_json():
    body = _script_block(INDEX_TEXT, "cetSubtitleIsTimed")
    for suffix in (".srt", ".vtt", ".json"):
        assert suffix in body, f"cetSubtitleIsTimed 未覆盖 {suffix}"
    # 纯文本 .txt 不能被算成带时间轴（它要允许与自动切句共存 → 文本对齐）
    assert ".txt" not in body


def test_sync_option_disables_and_explains_on_timed_subtitle():
    body = _script_block(INDEX_TEXT, "cetSyncAutoSplitOption")
    # 禁用逻辑
    assert "input.disabled = disabled" in body
    # 禁用时清掉勾选（避免提交出「勾了却不生效」）
    assert "input.checked = false" in body
    # 说明原因里必须点出「会覆盖字幕时间轴」这个真实后果
    assert "覆盖" in body
    assert "带时间轴的字幕" in body


def test_subtitle_pick_triggers_option_resync():
    """选完字幕/文本后要重新同步（选 SRT → 立刻禁用）。"""
    body = _script_block(INDEX_TEXT, "cetOnSubtitlePicked")
    assert "cetSyncAutoSplitOption()" in body


# ── ③ faster_whisper 不可用 → 灰掉 + 说明 ───────────────────


def test_capability_comes_from_backend_not_hardcoded():
    """能力必须来自 `/import/formats` 的 autoSplit 字段，不能在前端写死。"""
    body = _script_block(INDEX_TEXT, "cetImportFormatNote")
    assert "data.autoSplit" in body
    assert "_cetAutoSplitCapability" in body
    assert "cetSyncAutoSplitOption()" in body


def test_unavailable_capability_disables_option_with_reason():
    body = _script_block(INDEX_TEXT, "cetSyncAutoSplitOption")
    assert "capability.available" in body
    assert "capability.reason" in body
    assert "capability.hint" in body
    assert "disabled = true" in body


def test_unknown_capability_is_conservatively_disabled():
    """探测还没回来时保守禁用（不能先放开再灰掉）。"""
    body = _script_block(INDEX_TEXT, "cetSyncAutoSplitOption")
    assert "正在检查本机自动切句能力" in body


# ── ④ 提交：只在可勾且勾了才传 true ─────────────────────────


def test_form_submits_auto_split_only_when_enabled_and_checked():
    body = _script_block(INDEX_TEXT, "cetImportFormData")
    assert "cet-auto-split" in body
    assert "autoSplitEl.checked && !autoSplitEl.disabled" in body
    assert "form.append('auto_split'" in body
    # 未勾时传 false（显式），与后端默认值一致
    assert "'true' : 'false'" in body


def test_import_progress_mentions_background_auto_split():
    """导入成功后要说明「已在后台自动切句」，不能让用户以为已经能逐句听。"""
    body = _script_block(INDEX_TEXT, "cetStartImport")
    assert "data.autoSplit" in body
    assert "后台自动切句" in body


# ── ⑤ 样式不落到 cet-base.css（那是包4 的文件）─────────────


def test_auto_split_styles_are_inline_not_in_shared_css():
    """包3 不得改 cet-base.css，所以新元素样式必须内联。"""
    css = (REPO / "frontend" / "static" / "cet-base.css").read_text(encoding="utf-8")
    assert "cet-auto-split" not in css, "不得把新样式写进 cet-base.css（包4 的文件）"
    panel = _import_panel(INDEX_TEXT)
    assert "cet-auto-split-box" in panel
    assert "style=" in panel
