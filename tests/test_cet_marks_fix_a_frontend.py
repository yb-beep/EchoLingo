"""卡点修复 · 包A（前端）静态契约 + 反向锁定测试。

覆盖编号：A-1 ~ A-6（对应 docs/cet-base/卡点修复/包A-交付说明.md）。

分工：
    - 本文件：不起服务，直接读 frontend/static/cet-listening.js / .css，跑得很快
    - tests/test_cet_marks_fix_a_browser.py：真实服务 + 真实点击（CET_BASE_URL）

为什么有大量「反向断言」（断言某段代码**不存在**）：
    本轮修复的核心是**拆掉会打断用户的操作**。
    「createMark() 不再无条件自动回听」本身就是要锁定的行为，
    只断言「新开关存在」会漏掉「旧的无条件调用被加回来」这种回归。
    所以每条修复都同时锁「旧写法不在」+「新写法在」。

注意：所有「不得出现」的断言都在**去注释后的代码**上做。
    本轮改动每处都带解释性注释，注释里会自然出现被删掉的旧代码原文
    （"原来是无条件 previewMark(..., {autoplay:true})"）。
    直接在原文上做子串匹配会把「注释里说不要这么写」误判成「代码里还这么写」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LISTENING_JS = ROOT / "frontend" / "static" / "cet-listening.js"
LISTENING_CSS = ROOT / "frontend" / "static" / "cet-listening.css"
WORKSPACE = ROOT / "frontend" / "templates" / "workspace.html"


def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留**代码**（见模块 docstring）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


@pytest.fixture(scope="module")
def js_raw() -> str:
    return _text(LISTENING_JS)


@pytest.fixture(scope="module")
def js(js_raw: str) -> str:
    return _strip_js_comments(js_raw)


@pytest.fixture(scope="module")
def css() -> str:
    return _text(LISTENING_CSS)


def _block(src: str, start_marker: str, end_marker: str) -> str:
    """截取 start_marker 起、到下一个 end_marker 之前的代码块。"""
    i = src.find(start_marker)
    assert i >= 0, f"找不到代码块起点：{start_marker}"
    j = src.find(end_marker, i + len(start_marker))
    assert j > i, f"找不到代码块终点：{end_marker}（起点 {start_marker} 之后）"
    return src[i:j]


def _call_sites(body: str, call_re: str) -> list[int]:
    """返回 body 里所有匹配 call_re 的起点下标。"""
    return [m.start() for m in re.finditer(call_re, body)]


def _preceding_guard(body: str, idx: int) -> str:
    """取某处调用**前面紧邻的非空白代码**（用来判断它被哪个 if 包着）。"""
    return body[:idx].rstrip()


def _assert_guarded(body: str, call_re: str, guard: str, msg: str) -> None:
    """断言 call_re 的每一处调用都被 guard 这个 if 条件包住。

    为什么不能只用「代码里不得出现 X」这种子串断言：
        修复后的写法是 `if (S.autoplayOnMark) { previewMark(...) }`，
        调用本身还在，只是被守卫了。子串反向断言会把「被正确守卫的调用」
        误判成「无条件调用」，测试变成假红。
        所以这里检查的是**调用前紧邻的代码是不是那个 guard**。
    """
    sites = _call_sites(body, call_re)
    assert sites, f"找不到预期调用：{call_re}"
    for idx in sites:
        before = _preceding_guard(body, idx)
        assert before.endswith(guard), (
            f"{msg}\n  该调用前面紧邻的代码是：...{before[-120:]!r}\n"
            f"  期望以 {guard!r} 结尾"
        )


# ══════════════════════════════════════════════════════════════
# A-1（P0）：打完标记不再自动回听抢走播放位置
# ══════════════════════════════════════════════════════════════


def test_a1_create_mark_does_not_unconditionally_autoplay(js: str):
    """createMark() 里**不得**再有无条件的 previewMark(..., {autoplay:true})。

    这是问题1的元凶：按 M 后播放位置被 seek 回候选句起点，
    实测 3.74s → 0.058s，连按 3 次全落在同一句。

    注意断言方式：修复后的代码里调用还在（能力保留），只是被 if 守卫。
    所以不能断言「调用不存在」，要断言「每一处调用前面紧邻的都是那个守卫」。
    """
    body = _block(js, "async function createMark(", "function flashHint(")
    _assert_guarded(
        body,
        r"previewMark\([^;]*autoplay:\s*true",
        "if (S.autoplayOnMark) {",
        "createMark() 里出现无守卫的 previewMark(..., {autoplay:true}) —— "
        "这会把播放位置抢回候选句起点（问题1 回归）",
    )


def test_a1_autoplay_is_guarded_by_switch(js: str):
    """自动回听必须被 S.autoplayOnMark 守卫（能力保留，默认关闭）。"""
    body = _block(js, "async function createMark(", "function flashHint(")
    assert "S.autoplayOnMark" in body, "createMark() 里没有读自动回听开关"
    assert re.search(
        r"if\s*\(\s*S\.autoplayOnMark\s*\)\s*\{\s*previewMark\(", body
    ), "自动回听没有被 `if (S.autoplayOnMark)` 包住"


def test_a1_switch_defaults_to_off(js: str):
    """开关默认值必须是关闭（S 初始态 + 读不到偏好时都关闭）。"""
    assert re.search(r"autoplayOnMark:\s*false", js), "S.autoplayOnMark 初始值不是 false"
    body = _block(js, "function loadAutoplayPref(", "function saveAutoplayPref(")
    assert "=== '1'" in body, "loadAutoplayPref() 应只在值为 '1' 时返回 true"
    assert re.search(r"catch\s*\(_\)\s*\{\s*return false;", body), (
        "loadAutoplayPref() 读不到 localStorage 时必须回落到 false（隐私模式）"
    )


def test_a1_switch_is_persisted(js: str):
    """开关状态存 localStorage，且用独立的键（不塞进按材料分键的状态）。"""
    assert re.search(r"autoplayKey:\s*'cet\.marks\.autoplay\.v1'", js), (
        "缺少自动回听开关的 localStorage 键"
    )
    body = _block(js, "function saveAutoplayPref(", "function setAutoplayOnMark(")
    assert "localStorage.setItem" in body, "saveAutoplayPref() 没有落盘"


def test_a1_switch_is_mounted_in_toolbar(js: str):
    """开关必须挂在工具栏里（和「开始卡点练习 / 今日复习」并列），用户能看见。"""
    body = _block(js, "function mount()", "function setListening(")
    assert "cet-autoplay-mark" in body, "工具栏里没有自动回听开关"
    assert "标记后自动回听" in body, "开关缺少可读文案"
    # 必须真的追加进工具栏，而不是创建了没挂
    assert re.search(r"bar\.appendChild\(\s*autoWrap\s*\)", body), "开关没有追加进工具栏"


def test_a1_switch_blurs_after_toggle(js_raw: str):
    """勾完开关必须 blur。

    否则焦点留在 <input> 上，而 isTypingTarget() 把 input 一律当输入目标，
    之后按 M 全被吞掉（实测：勾完开关按 M 不产生标记）。

    注意：用**去注释前**的原文找代码块 —— 这段代码块后面紧邻的注释标题
    `// ── UI 挂载` 在去注释后会被一起删掉，块边界就找不到了。
    """
    body = _block(js_raw, "function setAutoplayOnMark(", "// ── UI 挂载")
    assert "blur()" in body, "setAutoplayOnMark() 没有把焦点从复选框移走"
    mount_body = _block(_strip_js_comments(js_raw), "function mount()", "function setListening(")
    assert re.search(r"setAutoplayOnMark\(autoBox\.checked,\s*\{\s*blur:\s*true\s*\}\)", mount_body), (
        "复选框 onchange 没有传 { blur: true }"
    )


def test_a1_css_has_switch_style(css: str):
    """开关要有样式（不引新设计系统，复用原变量）。"""
    assert ".cet-autoplay-switch" in css, "缺少自动回听开关的样式"


# ══════════════════════════════════════════════════════════════
# A-2：后端 1.5s 去重命中时不得重复入列
# ══════════════════════════════════════════════════════════════


def test_a2_create_mark_dedups_by_mark_id(js: str):
    """收到响应后必须按 markId 查重：已存在就更新，不能无条件 push。"""
    body = _block(js, "async function createMark(", "function flashHint(")
    assert "findIndex" in body, "createMark() 没有按 markId 查重"
    assert re.search(r"x\.markId\s*===\s*mark\.markId", body), "查重没有比对 markId"
    assert "Object.assign" in body, "重复标记应就地更新（Object.assign）而不是新增"
    # push 本身要保留（新标记还是要入列），但必须被 isNew 守卫 ——
    # 无条件 push 才是问题2 的回归形态。
    _assert_guarded(
        body,
        r"S\.marks\.push\(mark\);",
        "if (isNew) {",
        "createMark() 又变回无条件 S.marks.push(mark) —— "
        "后端 1.5s 去重命中时 UI 会出现重复行",
    )


def test_a2_duplicate_gets_clear_hint(js: str):
    """去重命中要明确告知用户「未新增」，不能看起来像按了没反应。"""
    body = _block(js, "async function createMark(", "function flashHint(")
    assert "未新增" in body, "去重命中时没有明确提示"


# ══════════════════════════════════════════════════════════════
# A-3（P0）：无时间轴材料不挂载工具栏（假入口）
# ══════════════════════════════════════════════════════════════


def test_a3_boot_skips_mount_without_sentences(js: str):
    """S.sentences.length === 0 时必须 return，不 mount。"""
    body = _block(js, "async function boot()", "function waitForPlayerThenTimeline(")
    assert re.search(r"if\s*\(\s*!\s*S\.sentences\.length\s*\)\s*\{", body), (
        "boot() 里没有「无句子时间轴就不挂载」的判断"
    )
    # return 必须在 mount() 之前
    i_guard = body.find("!S.sentences.length")
    i_mount = body.find("mount();")
    assert 0 <= i_guard < i_mount, "无时间轴判断必须在 mount() 之前 return"
    assert re.search(r"return;", body[i_guard:i_mount]), "无时间轴分支没有 return"
    assert "console.info" in body[i_guard:i_mount], "无时间轴跳过时应 console.info 说明原因"


def test_a3_template_feature_flag_is_untouched():
    """不能把「材料是否适用」写进模板的功能开关条件（两层不同）。"""
    html = _text(WORKSPACE)
    assert "{% if cet_features.cet_listening_agent %}" in html, (
        "模板里的「模块是否加载」开关被改动了 —— A-3 只应改 JS 的「材料是否适用」层"
    )


def test_a3_module_still_mounts_for_material_with_sentences(js: str):
    """有题材料必须照常 mount（不能为了修假入口把正常路径也断掉）。"""
    body = _block(js, "async function boot()", "function waitForPlayerThenTimeline(")
    i_guard = body.find("!S.sentences.length")
    assert body.find("mount();", i_guard) > i_guard, "有句子时 mount() 调用不见了"


# ══════════════════════════════════════════════════════════════
# A-4：标记时刻与候选句区间口径分离
# ══════════════════════════════════════════════════════════════


def test_a4_row_labels_pressed_time_explicitly(js: str):
    """列表里必须显式写「标记于 X」，不能只放一个裸时间。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    assert "标记于" in body, "标记行没有显式的「标记于」文案"
    assert re.search(r"`标记于 \$\{fmt\(m\.pressedAtSec\)\}`", body), (
        "「标记于」后面应带 pressedAtSec"
    )


def test_a4_row_labels_sentence_as_candidate(js: str):
    """候选句必须标成「候选句 A–B」，与「标记于 X」明确分开。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    assert "候选句" in body, "标记行没有「候选句」标签"
    assert "确认句" in body, "已确认状态没有「确认句」标签"
    # 必须同时展示两个区间值（start/end），而不是只有起点
    assert re.search(r"fmt\(sent\.startSeconds\)", body) and re.search(r"fmt\(sent\.endSeconds\)", body), (
        "候选句区间应显示 start–end"
    )


def test_a4_row_has_arrow_separator(js: str):
    """两段之间要有可见的关系符号，让人一眼看出是「标记位置 → 吸附出的候选句」。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    assert "cet-mark-arrow" in body, "缺少标记时刻与候选句之间的箭头分隔"


def test_a4_css_has_arrow_style(css: str):
    assert ".cet-mark-arrow" in css, "缺少箭头样式"


# ══════════════════════════════════════════════════════════════
# A-5：删除按钮在「本轮已练」态提前禁用
# ══════════════════════════════════════════════════════════════


def test_a5_delete_disabled_by_in_practice(js: str):
    """删除按钮必须按后端透出的 `inPractice` 禁用，不能用 markState()。

    为什么不能用 markState()：
        markState() 只在 status==='complete' 时返回 'done'，
        而 cet_practices 默认 status 是 'active'；后端 delete_mark() 的拒绝条件是
        「cet_practices 里有没有对应行」，与 status 无关。
        所以「已开始带练但没练完」这个最常见状态下 markState() 返回 'ready'，
        按钮仍可点，用户点下去才收到 409 MARK_IN_PRACTICE。
    """
    body = _block(js, "function renderMarks()", "function previewMark(")
    assert re.search(r"if\s*\(\s*m\.inPractice\s*===\s*true\s*\)", body), (
        "删除按钮没有按 `m.inPractice === true` 判断（必须严格等于 true，"
        "因为 inPractice 可能是 null）"
    )
    assert re.search(r"del\.disabled\s*=\s*true", body), "已带练时没有禁用删除按钮"
    assert "该标记已开始带练，不能删除" in body, "缺少 title 说明"
    # 反向：不得再退回 markState()==='done' 的判定（漏掉 active 态）
    i = body.find("const del = el('button'")
    assert i > 0, "找不到删除按钮构造处"
    del_block = body[i:i + 900]
    assert "st === 'done'" not in del_block, (
        "删除按钮又变回按 markState()==='done' 判定 —— "
        "会漏掉「已开始带练但未完成（active）」这个最常见状态"
    )


def test_a5_in_practice_null_means_deletable(js: str):
    """inPractice 为 null（旧接口/未查场景）时按「可点」处理，只有 true 才禁用。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    i = body.find("const del = el('button'")
    del_block = body[i:i + 900]
    assert "else {" in del_block, "缺少 else 分支（null/false 应保持可点）"
    assert re.search(r"del\.title = '删除这个标记'", del_block), "可点态缺少 title"
    # 不允许把 null 也当禁用（`if (m.inPractice)` 会把 null 判成 false 反而没事，
    # 但 `!m.inPractice` / `m.inPractice === false` 这类写法会把 null 误判成可点/不可点，
    # 所以明确锁定「=== true」这一种写法）
    assert not re.search(r"if\s*\(\s*!\s*m\.inPractice\s*\)", body), (
        "不要用 !m.inPractice 取反判断（语义容易反）"
    )


def test_a5_start_practice_syncs_local_flag(js: str):
    """带练建立后必须同步本地 m.inPractice，否则要等刷新才禁用删除。"""
    body = _block(js, "async function startPractice(", "function renderCurrentStepInto(")
    assert re.search(r"m\.inPractice\s*=\s*true", body), (
        "startPractice() 成功后没有同步本地 m.inPractice —— "
        "删除按钮要等下一次刷新列表才会禁用"
    )


def test_a5_delete_still_enabled_for_other_states(js: str):
    """未带练的标记必须保持可点（后端仍会最终校验，前端只是提前反馈）。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    i = body.find("if (m.inPractice === true)")
    assert i > 0
    tail = body[i:i + 900]
    assert re.search(r"else\s*\{", tail), "inPractice 之外没有 else 分支（可能把全部行都禁用了）"
    assert "del.onclick" in tail, "非禁用态没有绑定删除动作"


# ══════════════════════════════════════════════════════════════
# A-6：时间线刻度改用候选句起点（与列表同口径）
# ══════════════════════════════════════════════════════════════


def test_a6_timeline_uses_candidate_start(js: str):
    """刻度位置必须用候选句起点，不是 pressedAtSec。"""
    body = _block(js, "function renderTimelineMarks(", "function mountQuestionPanel(")
    assert re.search(
        r"const\s+sentId\s*=\s*st\s*===\s*'pending'\s*\?\s*previewSentenceId\(m\)\s*:\s*m\.confirmedSentenceId",
        body,
    ), "时间线没有取「待确认用预览句 / 已确认用确认句」"
    assert re.search(r"const\s+sent\s*=\s*findSentence\(sentId\)", body), "时间线没有查句子"
    assert re.search(
        r"const\s+sec\s*=\s*sent\s*\?\s*Number\(sent\.startSeconds\)\s*:\s*\(Number\(m\.pressedAtSec\)\s*\|\|\s*0\)",
        body,
    ), "刻度秒数没有优先用候选句起点（缺兜底也不行）"
    # 反向：pct 不能再直接吃 pressedAtSec
    assert not re.search(r"const\s+sec\s*=\s*Number\(m\.pressedAtSec\)\s*\|\|\s*0", body), (
        "时间线又变回直接用 pressedAtSec 算刻度（问题6 回归）"
    )


def test_a6_timeline_keeps_test_hooks(js: str):
    """保留 data-cet-mark-sec 供测试用，并补上可追溯的属性。"""
    body = _block(js, "function renderTimelineMarks(", "function mountQuestionPanel(")
    assert "data-cet-mark-sec" in body, "data-cet-mark-sec 被删了（测试依赖）"
    assert "data-cet-mark-sentence" in body, "缺少「该刻度对应哪句」的可追溯属性"
    assert "data-cet-mark-pressed" in body, "缺少原始按键时刻属性（便于对账）"


def test_a6_timeline_title_shows_relation(js: str):
    """title 要能看出「刻度 = 候选句」且带原始标记时刻。"""
    body = _block(js, "function renderTimelineMarks(", "function mountQuestionPanel(")
    assert "候选句 · " in body, "title 没有标出「候选句」"
    assert "标记于" in body, "title 没有带上原始按键时刻（用户无法对账）"


def test_a6_timeline_click_lands_on_tick(js: str):
    """点刻度跳到的位置必须与刻度画的位置一致（都是候选句起点）。"""
    body = _block(js, "function renderTimelineMarks(", "function mountQuestionPanel(")
    i = body.find("tick.onclick")
    assert i > 0, "刻度没有点击行为"
    tail = body[i:]
    assert re.search(r"replayRange\(sent\.startSeconds,\s*sent\.endSeconds\)", tail), (
        "点刻度没有回放候选句区间"
    )


# ══════════════════════════════════════════════════════════════
# 不回归：原有能力不能被这轮修复碰坏
# ══════════════════════════════════════════════════════════════


def test_no_regression_mark_key_is_still_m(js: str):
    """M 键仍是标记键（返修 2.1 契约）。"""
    assert "if (e.code !== 'KeyM') return;" in js, "标记键被改动"


def test_no_regression_space_still_not_taken(js: str):
    """队友模块仍不抢 Space（底座独占播放/暂停）。"""
    assert "if (e.code !== 'Space') return;" not in js, "队友模块又开始抢 Space"


def test_no_regression_manual_replay_kept(js: str):
    """「回听 / 前一句 / 后一句 / 开始带练」等手动入口必须保留。"""
    body = _block(js, "function renderMarks()", "function previewMark(")
    for label in ("回听", "前一句", "后一句", "确认这句", "开始带练", "改一下", "删除"):
        assert label in body, f"手动入口「{label}」不见了"


def test_no_regression_preview_autoplay_api_still_supported(js: str):
    """previewMark 的 autoplay 选项必须保留（开关打开时靠它）。"""
    body = _block(js, "function previewMark(", "function adjustMark(")
    assert re.search(r"opts\s*&&\s*opts\.autoplay", body), "previewMark 不再支持 autoplay 选项"
    assert "replayRange(" in body, "previewMark 不再回放"
