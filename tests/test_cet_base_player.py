"""P3 听力工作区 · 静态契约测试（CET 底座）。

为什么要这组测试：
    任务书 §4 P3 的几条硬要求（不假称生成中、切换材料不串、进度按材料恢复、
    无原文状态合理、收束模块不发起后台生成）都可以在模板/静态层先锁住，
    不必每次都靠浏览器。浏览器行为验证见 tests/test_cet_base_workspace.py
    与 docs/cet-base/自验报告.md 里的 T06–T14。

约定：
    - 这里只读模板与静态文件，不起服务，跑得很快
    - 断言用「关键片段」而不是整段 HTML，避免改文案就红
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "frontend" / "templates"
STATIC = ROOT / "frontend" / "static"

WORKSPACE = TEMPLATES / "workspace.html"
INTENSIVE = TEMPLATES / "intensive.html"
CET_PLAYER = STATIC / "cet-player.js"
CET_FAVORITES = STATIC / "cet-favorites.js"
CET_WORKSPACE_CSS = STATIC / "cet-workspace.css"


def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


# ── 文件存在性 ──────────────────────────────────────────────


def test_cet_base_player_assets_exist():
    for path in (CET_PLAYER, CET_FAVORITES, CET_WORKSPACE_CSS):
        assert path.is_file(), f"缺少底座资源：{path.name}"
        assert path.stat().st_size > 500, f"{path.name} 内容过少，可能是占位"


# ── 任务书 §4 P3：不能假称「生成中」/「AI 正在分析」──────────────


def test_workspace_initial_status_badge_is_neutral():
    """首屏徽标不得预设「字幕生成中」：真实状态由 updateStatusBadge 写入。"""
    html = _text(WORKSPACE)
    # 找到 #status-badge 的静态标签本体
    match = re.search(r'<span id="status-badge"[^>]*>(.*?)</span>', html, re.S)
    assert match, "未找到 #status-badge 标签"
    initial = match.group(1).strip()
    assert "生成中" not in initial, f"首屏徽标仍预设生成中：{initial!r}"
    assert "AI 正在" not in initial
    assert initial in {"—", "-", ""}, f"首屏徽标应为中性占位，实际 {initial!r}"


def test_workspace_cet_mode_has_no_fake_generation_copy():
    """CET 模式下不得出现「正在生成内容大纲」「AI 正在生成章节导航」这类假进行态。"""
    html = _text(WORKSPACE)
    # 这两句允许存在于「通用模式」分支里，但不允许作为无条件静态文案出现。
    for phrase in ("正在生成内容大纲", "AI 正在生成章节导航"):
        for match in re.finditer(re.escape(phrase), html):
            # 往前找最近的 {% if %} / else 上下文，确认被 cet_on 或 CET_ON 包住
            head = html[max(0, match.start() - 400):match.start()]
            guarded = (
                "{% if cet_on" in head
                or "{% else %}" in head
                or "CET_ON" in head
                or "cetFeature(" in head
            )
            assert guarded, f"「{phrase}」在 CET 模式下会无条件出现，缺少开关"


def test_workspace_cet_labels_for_empty_transcript():
    """无原文时不能暗示后台在生成；要有明确「没有原文」的说法。"""
    html = _text(WORKSPACE)
    assert "这份材料暂无原文" in html or "无原文" in html, "缺少无原文的明确状态文案"
    assert "没有独立阅读正文" in html, "缺少「听力材料没有阅读正文」的明确说明"


def test_intensive_cet_mode_retires_ielts_and_scenario_blocks():
    """逐句精学页的「雅思复用」「情景造句」必须由 cet_features 收束。"""
    html = _text(INTENSIVE)
    assert "{% if cet_features.ielts_reuse %}" in html, "雅思复用区块未受 cet_features 控制"
    assert "{% if cet_features.scenario_sentence %}" in html, "情景造句区块未受 cet_features 控制"
    # 函数级兜底：即使区块被脚本调用也不发起模型请求
    assert "if (!cetFeature('ielts_reuse')) return null;" in html
    assert "if (!cetFeature('scenario_sentence')) return '';" in html


def test_intensive_export_path_has_context_defaults():
    """导出 HTML 路径只传 lesson_id；模板必须有兜底默认值，否则 Undefined 报错。"""
    html = _text(INTENSIVE)
    assert "{%- set cet_on = cet_on | default(false) -%}" in html
    assert "{%- set cet_features = cet_features | default({}) -%}" in html


# ── 任务书 §4 P3：切换材料不串 / 进度按材料 ─────────────────────


def test_player_progress_is_bound_to_lesson_id():
    """进度读写必须带当前 lesson id，且校验返回的归属，防止跨材料串数据。"""
    js = _text(CET_PLAYER)
    assert "/api/v2/lessons/${LESSON_ID}/progress" in js, "进度接口未绑定当前 lesson"
    assert "data.lesson_id" in js, "未校验进度返回的 lesson_id 归属"
    assert "Number(data.lesson_id) !== LESSON_ID" in js, "缺少归属不符时的丢弃逻辑"


def test_player_restores_position_and_throttles_writes():
    js = _text(CET_PLAYER)
    assert "async function restorePosition()" in js
    assert "saveIntervalMs" in js, "缺少写入节流，会打爆进度接口"
    assert "pagehide" in js and "beforeunload" in js, "关页面时未补写最后位置"
    assert "seeked" in js and "pause" in js, "拖动/暂停后未立即落盘"


def test_favorites_keep_material_identity_and_timeline():
    js = _text(CET_FAVORITES)
    # 材料身份
    assert "materialId" in js and "materialVersion" in js, "收藏未保留材料身份"
    assert "/api/cet/materials/by-lesson/" in js, "未取材料业务身份"
    # 原句与音频定位
    assert "start_seconds" in js and "end_seconds" in js, "句子收藏未保留时间轴"
    assert "segment_ids" in js, "句子收藏未把 segment_index 映射回当前时间轴"
    # 移除不删材料
    assert "/word/${encodeURIComponent(word)}" in js
    assert "/phase-b/${encodeURIComponent(sentence.segment_index)}" in js
    assert "材料本身不受影响" in js, "移除收藏时未明确说明不删除材料"


def test_favorites_jump_has_explicit_failure_feedback():
    js = _text(CET_FAVORITES)
    assert "没有可用音频" in js, "音频不可用时缺少明确反馈"
    assert "暂无逐句时间轴" in js, "缺少时间轴时缺少明确反馈"
    assert "超出音频长度" in js, "时间点越界时缺少明确反馈"
    assert "已不在当前材料的时间轴里" in js, "材料已替换时缺少明确反馈"


# ── P5 队友接入边界 ────────────────────────────────────────


def test_teammate_module_references_are_preserved():
    """任务书 §4 P5：不得删除队友模块引用。"""
    html = _text(WORKSPACE)
    assert 'href="/static/cet-listening.css' in html
    assert 'src="/static/cet-listening.js' in html
    # 两行引用都必须存在于 <script> 区之前（页面主体已渲染，模块自行挂载）
    assert html.index("/static/cet-listening.js") < html.index("const LESSON_ID")


def test_base_player_does_not_own_space_key():
    """空格键归队友卡点模块；底座播放器不得注册全局空格快捷键。"""
    js = _text(CET_PLAYER)
    assert "ownsSpaceKey: false" in js, "未声明不拥有空格键"
    # 不得出现把 Space 映射成播放/暂停的监听
    space_handlers = re.findall(r"key(?:down|up)\s*===\s*['\"]\s?['\"]", js)
    assert not space_handlers, f"底座注册了空格键处理：{space_handlers}"
    assert "'Space'" not in js and '"Space"' not in js
    assert "code === 'Space'" not in js
    # 底座只提供查询接口，由队友模块决定是否启用
    assert "cetListeningEnabled" in js


def test_base_player_does_not_duplicate_mount_or_listeners():
    """幂等：重复执行不会重复建 DOM、重复绑监听。"""
    js = _text(CET_PLAYER)
    assert "if (S.mounted) return;" in js, "mount() 非幂等"
    assert "el.dataset.cetProgressBound === '1'" in js, "播放器监听未做幂等保护"
    assert "S.listenersBound" in js, "页面级监听未做幂等保护"


def test_workspace_exposes_integration_contract():
    """队友模块与底座适配层都通过 window.__ws 读取，不摸页面私有变量。"""
    html = _text(WORKSPACE)
    assert "window.__ws = {" in html
    for key in ("lessonId", "sentences", "activeSentenceIndex", "seek", "play", "pause", "setRate"):
        assert re.search(rf"\b{key}\b", html), f"__ws 契约缺少 {key}"
    assert "window.__CET_BOOT__" in html, "未注入集中开关快照"


def test_retired_modules_do_not_trigger_background_work():
    """收束的模块不得继续自动发起后台生成/平台探测。"""
    html = _text(WORKSPACE)
    # 大纲生成
    assert "if (cetFeature('outline_generation')) loadCachedDocumentOutline();" in html
    assert "if (!cetFeature('outline_generation')) return;" in html
    # 泛用 AI 聊天（会话/语音输入/引用）
    assert "if (cetFeature('ai_chat_general')) {" in html
    assert "if (cetFeature('ai_chat_general')) await loadChatSessions();" in html
    # 积分/计费
    assert "boot.features.credits_account === false" in html


def test_reading_endpoint_not_called_when_mode_unavailable():
    """D-WS-2：listening 课不得轮询 /reading 拿 409。"""
    html = _text(WORKSPACE)
    assert "function readingModeAvailable()" in html
    assert "if (!readingModeAvailable()) {" in html
    # 门控必须在真正 fetch 之前
    guard_at = html.index("if (!readingModeAvailable()) {")
    fetch_at = html.index("/reading${_wordlistsQuery()}")
    assert guard_at < fetch_at, "reading 门控在请求之后才判断"


# ── 兼容性：既有页面契约 ────────────────────────────────────


@pytest.mark.parametrize(
    "needle",
    [
        "youtube-player",
        "subtitle-timeline",
        "chat-panel",
        "当前句",
        "内容大纲",
        "save-sentence",
        "vocab-chips",
        "star-save",
        "prev-sentence",
        "next-sentence",
        "jumpSentence",
        "playback-rate",
        "onPlaybackRateChange",
        'class="home-link" href="/" aria-label="返回首页"',
    ],
)
def test_workspace_legacy_contract_strings_kept(needle):
    """test_v2_workspace_page.py 的硬断言依赖这些字符串；收束只能改可见性，不能删 DOM。"""
    assert needle in _text(WORKSPACE)


def test_workspace_scripts_order_keeps_credits_before_use():
    """elt_credits.js 必须早于首次 eltCredits.* 调用（共享模板契约）。"""
    html = _text(WORKSPACE)
    include_at = html.find("/static/elt_credits.js")
    first_use = min(m.start() for m in re.finditer(r"eltCredits\.", html))
    assert include_at != -1
    assert include_at < first_use
