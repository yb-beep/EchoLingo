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


def test_base_player_space_key_ownership_is_three_state():
    """返修2 P2：空格归属改为三态，不再是「一律不碰」。

    权威契约（主代理定，包2/包5/审核方共用）：

        CET 模式 + 队友关闭 → 底座注册 Space/←/→/R/L，ownsSpaceKey = true
        CET 模式 + 队友开启 → 底座一个都不注册，          ownsSpaceKey = false
        通用模式(ELT_CET_MODE=0) → 不注册（原版无快捷键）， ownsSpaceKey = false

    旧用例（test_base_player_does_not_own_space_key）锁的是「一律让出」的旧契约，
    与 P2-1「队友不在时工作区没有任何播放快捷键」直接冲突，已随契约演进改写。
    行为验证见 tests/test_cet_fix2_p2_hotkeys.py（真实浏览器 + HTMLMediaElement）。
    """
    js = _text(CET_PLAYER)

    # 1. 归属判定必须同时看 CET 模式与队友模块状态，不能是常量
    assert "function hotkeyActive()" in js, "缺少快捷键归属判定函数"
    assert "return CET_ON && !cetListeningEnabled();" in js, "归属判定未同时考虑 CET_ON 与队友模块"

    # 2. ownsSpaceKey 必须是 getter（反映当前实际状态），不是对外暴露的字面量
    assert "get ownsSpaceKey()" in js, "ownsSpaceKey 仍是常量，无法反映三态"
    # 对外接口区（window.__cetPlayer 对象字面量）里不得再出现裸的 ownsSpaceKey: false
    export_block = js[js.index("window.__cetPlayer = {"):]
    assert "ownsSpaceKey: false," not in export_block, "对外接口仍把 ownsSpaceKey 写死为 false"
    # 内部状态初值允许是 false（挂载前未接管），但必须由 syncHotkeyOwnership 改写
    assert "S.ownsSpaceKey = active;" in js, "ownsSpaceKey 未与实际注册状态同步"

    # 3. 注册/注销成对出现，且由 syncHotkeyOwnership 统一驱动
    assert "function syncHotkeyOwnership()" in js
    assert "document.addEventListener('keydown', onHotkeyKeyDown)" in js, "关闭态未注册 keydown"
    assert "document.removeEventListener('keydown', onHotkeyKeyDown)" in js, "让出时未注销 keydown"

    # 4. 幂等标记（P2-4）：重复 mount 不能重复绑定
    assert "S.keydownBound" in js, "keydown 绑定缺少幂等标记"

    # 5. 让出态/通用模式在 handler 入口就返回，不做任何播放操作
    assert "if (!hotkeyActive()) return;" in js, "handler 未在入口处守卫归属"

    # 6. 底座只提供查询接口，队友模块仍可据此判断
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


# ── 返修2 包2：底座快捷键静态契约（P2-1…P2-4）───────────────────
# 行为验证在 tests/test_cet_fix2_p2_hotkeys.py（真实浏览器 + HTMLMediaElement）。
# 这里只锁「代码里必须有的结构与必须没有的反模式」，跑得快、不依赖服务。


def test_p2_hotkeys_cover_all_five_keys():
    """P2-1：关闭态要接管 Space / ← / → / R / L 五个键。"""
    js = _text(CET_PLAYER)
    assert "function onHotkeyKeyDown(event)" in js, "缺少 keydown 处理函数"
    # Space（用 e.code 判定，不受输入法/键盘布局影响）
    assert "event.code === 'Space'" in js, "未处理 Space"
    # 左右方向键
    assert "event.key === 'ArrowLeft'" in js and "event.key === 'ArrowRight'" in js, "未处理 ←/→"
    # R 重听当前句
    assert "event.key === 'r' || event.key === 'R'" in js, "未处理 R"
    # L 句循环开关
    assert "event.key === 'l' || event.key === 'L'" in js, "未处理 L"


def test_p2_hotkeys_respect_focus_guard():
    """P2-1：焦点在 input/textarea/contenteditable 时不响应，也不 preventDefault。"""
    js = _text(CET_PLAYER)
    assert "function isTypingTarget(target)" in js, "缺少输入焦点守卫"
    for needle in ("'input'", "'textarea'", "'select'", "isContentEditable"):
        assert needle in js, f"焦点守卫未覆盖 {needle}"
    # 守卫必须在 preventDefault 之前（否则输入框里按空格会被吞掉）
    guard_at = js.index("if (isTypingTarget(event.target)) return;")
    first_prevent = js.index("event.preventDefault();", guard_at - 3000)
    assert guard_at < first_prevent or first_prevent > guard_at, "焦点守卫晚于 preventDefault"


def test_p2_hotkeys_skip_modified_and_repeated_keys():
    """带 Ctrl/Cmd/Alt 的组合键属于浏览器/系统快捷键，不能抢；长按不连发。"""
    js = _text(CET_PLAYER)
    assert "event.ctrlKey || event.metaKey || event.altKey" in js, "未排除修饰键组合"
    assert js.count("if (event.repeat) return;") >= 3, "长按未做连发抑制（Space/R/L 各一处）"


def test_p2_sentence_loop_uses_ws_timeline_not_dom():
    """P2-1：句子边界从 window.__ws 取（唯一数据来源），不解析 DOM。"""
    js = _text(CET_PLAYER)
    assert "function sentences()" in js
    assert "Array.isArray(api.sentences) ? api.sentences : []" in js, "未从 __ws.sentences 取句子"
    # 句循环：跨过句尾 seek 回句首
    assert "function enforceLoop()" in js, "缺少句循环执行体"
    assert "now >= end - LOOP_EPSILON" in js, "句尾判定不是 currentTime >= 句尾"
    assert "seekToSeconds(start)" in js, "句循环未回到句首"
    # 防抖：回跳后短暂静音，避免 timeupdate 反复 seek
    assert "LOOP_GUARD_MS" in js and "loopGuardUntil" in js, "句循环缺少防抖窗"
    # 精确驱动：rAF 逐帧比对（timeupdate 粒度 ~250ms 会多播下一句开头）
    assert "function startLoopWatcher()" in js and "requestAnimationFrame" in js


def test_p2_loop_anchor_survives_crossing_sentence_end():
    """句循环必须锚定「开启时那一句」，不能跟着 activeSentenceIndex 漂走。"""
    js = _text(CET_PLAYER)
    assert "function setLoopAnchor(index)" in js, "缺少句循环锚点"
    assert "S.loopAnchor" in js
    # 锚点存的是 start/end 数值，不是索引查询结果
    assert "start: Number(unit.start || 0)" in js and "end: Number(unit.end" in js
    # 越过句尾那一刻 activeSentenceIndex 已指向下一句，所以只能用锚点
    assert "const {start, end} = S.loopAnchor;" in js


def test_p2_hotkey_hint_is_rendered_by_player_not_template():
    """P2-3：「⌨ 快捷键」提示由 cet-player.js 自建，不依赖 workspace.html 插片段。"""
    js = _text(CET_PLAYER)
    assert "⌨ 快捷键" in js, "缺少快捷键提示文案"
    assert "cet-hotkey-hint" in js and "cet-hotkey-panel" in js
    # 只在关闭态（底座接管态）显示
    assert "const show = Boolean(S.ownsSpaceKey);" in js, "提示未跟随接管状态显隐"
    # hover + 点击两种展开方式都要有
    assert "mouseenter" in js and "'click'" in js
    # 提示面板要能被读屏与键盘识别
    assert "aria-expanded" in js and "aria-controls" in js
    # 明确：不需要往 workspace.html 插任何片段
    assert "cet-hotkey-hint" not in _text(WORKSPACE), (
        "workspace.html 里出现了 cet-hotkey-hint —— 包2 的实现约定是自建，"
        "若包1 也插了同 id 元素会重复"
    )


def test_p2_ownership_syncs_when_teammate_mounts_late():
    """P2-2：队友模块可能晚于底座挂载，归属必须在运行期重判。"""
    js = _text(CET_PLAYER)
    # mount() 里定一次
    assert "syncHotkeyOwnership();" in js
    # 状态条刷新与周期补绑里也要重判（队友 boot() 要等两个接口才 mount）
    assert js.count("syncHotkeyOwnership();") >= 3, "归属只在 mount 判了一次，队友晚挂载会漏判"
    # 让出时停掉句循环，避免和队友抢播放位置
    assert "stopLoopWatcher();" in js
    assert "底座让出播放快捷键" in js


def test_p2_generic_mode_does_not_register_hotkeys():
    """§0.6 通用模式回归：ELT_CET_MODE=0 时行为不变（原版没有这些快捷键）。"""
    js = _text(CET_PLAYER)
    # 归属判定的第一项就是 CET_ON，通用模式直接短路
    assert "return CET_ON && !cetListeningEnabled();" in js
    assert "if (!hotkeyActive()) return;" in js


# ── 返修2 包2 复审回归：§0.6 通用模式「本句循环」指示条曾泄漏 ────────
# 缺陷（Lead 用基线 6f07433 vs 集成的逐页可见文本 diff 抓到）：
#   buildBar() 建 DOM 时只给 #cet-loop-indicator 设了 hidden 属性，而
#   cet-workspace.css:60 的 `.cet-study-bar .cet-study-item{display:inline-flex}`
#   是作者样式，优先级高于 UA 的 `[hidden]{display:none}` → 元素照样显示。
#   通用模式下 ownsSpaceKey 初值 false、active 也是 false，
#   syncHotkeyOwnership() 的状态变更分支永不成立 → 一次都不纠正 → 一直可见。
#   实测：通用模式 /workspace/1 多出可见文本「本句循环 开」（基线没有该元素）。


def test_p2_loop_indicator_hidden_at_build_time():
    """回归守卫：buildBar() 建完 DOM 必须立刻按真实归属收起指示条。

    只靠 HTML 的 hidden 属性不够 —— 作者 CSS 会盖掉 UA 的 [hidden]。
    """
    js = _text(CET_PLAYER)
    build_body = js[js.index("function buildBar()"):js.index("function updateBar()")]
    assert "S.barEl = bar;" in build_body, "buildBar 未记录状态条引用"
    # 关键：S.barEl = bar 之后必须主动同步一次指示条可见性
    after_assign = build_body[build_body.index("S.barEl = bar;"):]
    assert "syncLoopIndicator();" in after_assign, (
        "buildBar() 建完 DOM 后未调用 syncLoopIndicator()，"
        "指示条会保持「只有 hidden 属性、被作者 CSS 覆盖」的状态 → 通用模式泄漏"
    )


def test_p2_ui_sync_not_inside_ownership_change_branch():
    """回归守卫：UI 刷新不能放在 `ownsSpaceKey !== active` 分支里。

    通用模式下 ownsSpaceKey 初值与 active 都是 false，分支永不进入，
    放在里面等于「永远不会被纠正」。
    """
    js = _text(CET_PLAYER)
    sync_body = js[js.index("function syncHotkeyOwnership()"):js.index("// ── 挂载")]
    branch_start = sync_body.index("if (S.ownsSpaceKey !== active) {")
    # 找到该 if 块的结束（按缩进配对大括号）
    depth = 0
    branch_end = None
    for idx in range(sync_body.index("{", branch_start), len(sync_body)):
        if sync_body[idx] == "{":
            depth += 1
        elif sync_body[idx] == "}":
            depth -= 1
            if depth == 0:
                branch_end = idx
                break
    assert branch_end is not None, "无法定位归属变更分支"
    branch = sync_body[branch_start:branch_end]
    outside = sync_body[:branch_start] + sync_body[branch_end:]

    assert "syncLoopIndicator();" not in branch, (
        "syncLoopIndicator() 仍在「状态变更」分支里 —— 通用模式下该分支永不进入，"
        "指示条不会被纠正（§0.6 回归成因）"
    )
    assert "syncHotkeyHint();" not in branch, "syncHotkeyHint() 仍在状态变更分支里"
    assert "syncLoopIndicator();" in outside, "syncHotkeyOwnership() 未无条件刷新指示条"
    assert "syncHotkeyHint();" in outside, "syncHotkeyOwnership() 未无条件刷新快捷键提示"


def test_p2_set_shown_handles_author_css_override():
    """setShown() 必须同时设 hidden 与行内 display（行内样式优先级最高）。"""
    js = _text(CET_PLAYER)
    body = js[js.index("function setShown(el, show)"):]
    body = body[:body.index("\n  }")]
    assert "el.hidden = !show" in body, "setShown 未设 hidden 属性"
    assert "el.style.display" in body, "setShown 未设行内 display（作者 CSS 会盖掉 hidden）"
