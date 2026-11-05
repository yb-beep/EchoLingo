"""四六级卡点训练 · 前端行为测试（真实浏览器 + 真实服务）

为什么需要这组测试：
    空格标记的边界行为（长按连发、输入框、暂停、非练习模式）靠肉眼看代码
    很容易漏；计划 §4 对每一条都有明确要求，必须能被自动化验证。

运行前提：
    1. 服务已启动：scripts/qa/start_cet.sh --mock
    2. 已装 playwright chromium：.venv/bin/playwright install chromium
    3. 六级材料已导入：.venv/bin/python scripts/cet/import_materials.py --all

运行：
    .venv/bin/python -m pytest tests/test_cet_listening_ui.py -q
    加 -s 可看到每项实测输出

跳过条件：
    服务不可达时整组跳过，不制造假失败。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import os
# F07：允许指定隔离地址，避免「只测交付者本机那一个库」
BASE = os.environ.get("CET_UI_BASE", "http://127.0.0.1:5173").rstrip("/")
# F09：不硬编码 lesson id（导入脚本用自增 ID，换库会变）。
# 默认值仅用于本机；换环境时用 CET_UI_LESSON_ID 指定。
LESSON_ID = int(os.environ.get("CET_UI_LESSON_ID", "2181"))
CET4_LESSON_ID = int(os.environ.get("CET_UI_CET4_LESSON_ID", "2182"))
AUDIO = "document.getElementById('local-media-player')"


def _service_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _service_up(), reason="服务未启动，跳过前端行为测试")


@pytest.fixture(scope="module")
def browser():
    """共享一个浏览器实例。

    Playwright Sync API 在同一进程里反复 with sync_playwright() 会报
    "using Sync API inside the asyncio loop"，所以统一在这里开一次。
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome")
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(scope="module")
def page(browser):
    pg = browser.new_page(viewport={"width": 1500, "height": 1200})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    pg.wait_for_timeout(4000)
    pg._cet_errors = errors          # 供用例断言无 JS 报错
    yield pg
    pg.close()


@pytest.fixture()
def listening_page(page):
    """进入卡点练习模式并开始播放的页面。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(500)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    yield page
    page.evaluate(f"{AUDIO}.pause()")


def _marks(pg) -> int:
    return pg.evaluate("window.__cet.S.marks.length")


# ── 挂载与材料识别 ───────────────────────────────────────

def test_module_mounts_and_identifies_cet_material(page):
    """卡点控件应挂载，且识别出正确的四六级材料（不是标题相近的其他课程）。"""
    state = json.loads(page.evaluate(
        "JSON.stringify({mid: window.__cet.S.materialId, "
        "qs: window.__cet.S.questions.length, "
        "sent: window.__cet.S.sentences.length})"
    ))
    assert state["mid"] == "cet6-202512-conv2", f"材料识别错误：{state['mid']}"
    assert state["sent"] == 32, f"句子数应为 32，实际 {state['sent']}"
    assert state["qs"] == 5, f"题目数应为 5，实际 {state['qs']}"
    assert page.evaluate("!!document.querySelector('#cet-toolbar')")
    assert page.evaluate("!!document.querySelector('#cet-questions')")


def test_original_ui_is_preserved(page):
    """原页面结构必须保留：播放器、字幕、侧栏、原有控件都还在。"""
    for sel in ("#local-media-player", "#current-subtitle", ".side-column",
                "#subtitle-mode", "#playback-rate", "#cet-toggle-mode"):
        assert page.evaluate(f"!!document.querySelector('{sel}')"), f"缺少 {sel}"
    # 原有精听/阅读切换未被移除。
    # 注意：这个元素带 hidden 属性（未进入精听模式时隐藏），所以只检查存在性，
    # 不检查可见性。不能写成 `... or True` —— 那是永真断言，等于没测。
    assert page.evaluate("!!document.querySelector('#study-mode-switch')"), \
        "原精听/阅读切换控件应存在"


def test_non_cet_lesson_does_not_mount(page):
    """非四六级课程不应挂载卡点控件（静默跳过，不影响原页面）。

    用 v2-2182（四级样例）验证「四级也是四六级材料」的对照，
    再确认非四六级课程不挂载。
    """
    # v2-15 是底座里的纯文本课程，不是四六级材料
    page.goto(f"{BASE}/workspace/15", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3000)
    assert page.evaluate("!document.querySelector('#cet-toolbar')"), \
        "非四六级课程不应出现卡点工具栏"
    # 页面本身应正常渲染（不是白屏/报错）
    assert page.evaluate("document.body.innerText.length") > 50, "页面应有内容"


# ── 空格边界行为（计划 §4）──────────────────────────────

def test_space_marks_and_keeps_playing(listening_page):
    """播放中按空格：应新增标记，且音频继续播放（不暂停）。"""
    before = _marks(listening_page)
    listening_page.keyboard.press("Space")
    listening_page.wait_for_timeout(1800)
    after = _marks(listening_page)
    assert after == before + 1, f"应新增 1 个标记，实际 {after - before}"
    assert not listening_page.evaluate(f"{AUDIO}.paused"), "空格不应暂停音频"

    mark = json.loads(listening_page.evaluate("JSON.stringify(window.__cet.S.marks.slice(-1)[0])"))
    assert isinstance(mark.get("pressedAtSec"), (int, float)), "必须保存按下时的媒体时间"
    assert mark.get("suggestedSentenceId"), "应给出候选句"


def test_space_long_press_marks_once(listening_page):
    """长按连发：只算一次标记。"""
    before = _marks(listening_page)
    listening_page.keyboard.down("Space")
    listening_page.wait_for_timeout(1200)
    listening_page.keyboard.up("Space")
    listening_page.wait_for_timeout(1200)
    after = _marks(listening_page)
    assert after == before + 1, f"长按 1.2 秒应只算 1 次，实际 {after - before} 次"


def test_space_in_input_does_not_mark(listening_page):
    """输入框里的空格不能被全局监听抢走。"""
    listening_page.evaluate("""() => {
      const i = document.createElement('input');
      i.id = 'cet-test-input';
      i.style.cssText = 'position:fixed;left:10px;top:10px;z-index:9999;width:200px';
      document.body.appendChild(i); i.focus();
    }""")
    before = _marks(listening_page)
    listening_page.keyboard.type("hello world")
    listening_page.wait_for_timeout(900)
    after = _marks(listening_page)
    value = listening_page.evaluate("document.getElementById('cet-test-input').value")
    listening_page.evaluate("document.getElementById('cet-test-input').remove()")
    assert value == "hello world", f"输入框内容被干扰：{value!r}"
    assert after == before, "输入框里按空格不应产生标记"


def test_space_when_paused_resumes_without_marking(listening_page):
    """暂停状态下空格：恢复播放，不新增标记。"""
    listening_page.evaluate(f"{AUDIO}.pause()")
    listening_page.wait_for_timeout(400)
    before = _marks(listening_page)
    listening_page.keyboard.press("Space")
    listening_page.wait_for_timeout(1200)
    after = _marks(listening_page)
    assert after == before, "暂停时按空格不应新增标记"
    assert not listening_page.evaluate(f"{AUDIO}.paused"), "空格应恢复播放"


def test_space_outside_listening_mode_does_nothing(page):
    """非练习模式下按空格不应产生标记。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    # 不点「开始卡点练习」
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(800)
    before = _marks(page)
    page.keyboard.press("Space")
    page.wait_for_timeout(900)
    after = _marks(page)
    page.evaluate(f"{AUDIO}.pause()")
    assert after == before, "非练习模式不应产生标记"


# ── 完整链路 ─────────────────────────────────────────────

def test_attempt_reveals_answers_only_after_submit(page):
    """交卷前不泄露答案；交卷后给出判题与原声证据。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)

    # 交卷前面板只有选项，没有任何答案/证据
    assert page.evaluate("document.querySelectorAll('.cet-q-evidence').length") == 0
    assert page.evaluate("document.querySelectorAll('.cet-q-result').length") == 0

    opts = page.query_selector_all(".cet-opt")
    assert len(opts) >= 4, "题目选项应已渲染"
    for i in (1, 6):
        if i < len(opts):
            opts[i].click()
            page.wait_for_timeout(100)

    page.click("#cet-submit")
    page.wait_for_timeout(3000)

    results = page.evaluate("document.querySelectorAll('.cet-q-result').length")
    evidences = page.evaluate("document.querySelectorAll('.cet-q-evidence').length")
    assert results == 5, f"应有 5 条判题结果，实际 {results}"
    assert evidences >= 3, f"应有原声证据，实际 {evidences}"


def test_mark_confirm_and_restore_after_reload(page):
    """核心链路：标记 → 交卷 → 确认 → 刷新后仍能恢复。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)

    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    page.keyboard.press("Space")
    page.wait_for_timeout(1800)
    page.evaluate(f"{AUDIO}.pause()")
    assert _marks(page) >= 1, "应先产生一个标记"

    # 交卷（B 的后端要求先交卷才能确认句子）
    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(2800)

    # 确认卡点句子
    confirm = page.query_selector('button:has-text("确认这句")')
    assert confirm, "应出现「确认这句」按钮"
    confirm.click()
    page.wait_for_timeout(2500)

    badges = page.evaluate(
        "Array.from(document.querySelectorAll('.cet-mark-badge')).map(e=>e.textContent)"
    )
    assert any(b in ("待练", "本轮已练") for b in badges), f"确认后状态应前进，实际 {badges}"

    # 刷新后恢复
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4500)
    restored = _marks(page)
    assert restored >= 1, f"刷新后应恢复标记，实际 {restored}"
    sentences = page.evaluate(
        "Array.from(document.querySelectorAll('.cet-mark-sentence')).map(e=>e.textContent)"
    )
    assert any(len(s) > 10 for s in sentences), f"恢复的标记应带句子文本，实际 {sentences}"


def test_no_js_errors_during_flow(page):
    """整个流程不应产生 JS 错误。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1000)
    page.keyboard.press("Space")
    page.wait_for_timeout(1500)
    page.evaluate(f"{AUDIO}.pause()")
    assert not page._cet_errors, f"出现 JS 错误：{page._cet_errors}"


def test_cet4_material_also_supported(page):
    """四级样例也应被识别并挂载控件（计划 §1 要求四六级各一份）。"""
    page.goto(f"{BASE}/workspace/2182", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(4000)
    state = json.loads(page.evaluate(
        "JSON.stringify({mid: window.__cet.S.materialId, "
        "qs: window.__cet.S.questions.length, "
        "sent: window.__cet.S.sentences.length})"
    ))
    assert state["mid"] == "cet4-sample-hope-wish", f"四级材料识别错误：{state['mid']}"
    assert state["sent"] == 32
    assert state["qs"] == 5
    assert page.evaluate("!!document.querySelector('#cet-toolbar')")


# ── M2：反馈驱动带练 ─────────────────────────────────────

def _to_practice(page):
    """走到「带练已开始」的状态，返回 practiceId。

    每个用例用独立会话：sessionStorage 里的 sessionId 决定 listeningSessionId，
    不复用会拿到上一个用例已确认过的标记（表现为没有「确认这句」按钮）。
    """
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    # 两个 storage 都要清：localStorage 存了 attemptId/practiceId，
    # 不清会把上一个用例的带练状态带进来（表现为「没有确认这句按钮」）。
    page.evaluate("localStorage.clear(); sessionStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4000)
    # 等模块完成材料加载
    page.wait_for_function("window.__cet && window.__cet.S.materialId", timeout=20000)
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    page.keyboard.press("Space")
    page.wait_for_timeout(1800)
    page.evaluate(f"{AUDIO}.pause()")

    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(120)
    page.click("#cet-submit")
    page.wait_for_timeout(2800)

    confirm = page.query_selector('button:has-text("确认这句")')
    assert confirm, "应出现「确认这句」按钮"
    confirm.click()
    page.wait_for_timeout(2500)

    start = page.query_selector('button:has-text("开始带练")')
    assert start, "确认后应出现「开始带练」"
    start.click()
    page.wait_for_timeout(3000)
    return page.evaluate("window.__cet.S.practice && window.__cet.S.practice.practiceId")


def test_feedback_sound_triggers_slow_playback(page):
    """「没听清」应触发慢放，且播放速度真的变化（计划 §5：本地动作不等 AI）。"""
    assert _to_practice(page)
    btn = page.query_selector('button:has-text("没听清")')
    assert btn, "应有「没听清」按钮"
    btn.click()
    page.wait_for_timeout(3000)

    out = page.evaluate("(document.querySelector('#cet-practice-out')||{}).textContent || ''")
    assert "慢放" in out, f"「没听清」应触发慢放，实际提示：{out}"

    rate = page.evaluate(f"{AUDIO}.playbackRate")
    assert abs(rate - 0.75) < 0.01, f"播放速度应变 0.75，实际 {rate}"


def test_feedback_meaning_degrades_gracefully_without_ai(page):
    """「不懂意思」需要模型；未配置时应给可操作提示，不能白屏或静默失败。"""
    assert _to_practice(page)
    btn = page.query_selector('button:has-text("不懂意思")')
    assert btn, "应有「不懂意思」按钮"
    btn.click()
    page.wait_for_timeout(4000)

    # 用 innerText 读，包含子元素里的提示（等待区、降级说明都是子节点）
    out = page.evaluate("(document.querySelector('#cet-practice-out')||{}).innerText || ''")
    # 配置了模型会给解释；没配置要给「仍可回听/慢放/保存」这类可操作提示
    assert any(k in out for k in ("解释", "模型", "暂不", "回听", "慢放")), \
        f"「不懂意思」应给明确反馈，实际：{out!r}"
    # 无论成败，回听与保存必须仍可用
    assert page.evaluate("!!document.querySelector('#local-media-player')")


def test_two_feedbacks_lead_to_different_actions(page):
    """两类反馈必须产生不同动作（计划 §5 硬要求）。"""
    assert _to_practice(page)

    sound = page.query_selector('button:has-text("没听清")')
    sound.click()
    page.wait_for_timeout(2500)
    out_sound = page.evaluate("(document.querySelector('#cet-practice-out')||{}).textContent || ''")

    meaning = page.query_selector('button:has-text("不懂意思")')
    meaning.click()
    page.wait_for_timeout(3500)
    out_meaning = page.evaluate("(document.querySelector('#cet-practice-out')||{}).textContent || ''")

    assert out_sound != out_meaning, \
        f"两类反馈应给不同帮助，实际相同：{out_sound!r}"


# ── M3：复习 ─────────────────────────────────────────────

def test_review_starts_and_lists_at_most_three(page):
    """今日复习应能开始，最多三处（计划 §7 M3）。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.evaluate("sessionStorage.clear()")
    btn = page.query_selector("#cet-review-btn")
    assert btn, "应有「今日复习」按钮"
    btn.click()
    page.wait_for_timeout(3000)

    visible = page.evaluate("!document.getElementById('cet-review').hidden")
    assert visible, "复习面板应出现"
    rows = page.evaluate("document.querySelectorAll('.cet-review-row').length")
    assert rows <= 3, f"首版最多三处，实际 {rows}"


def test_review_skip_moves_to_next_without_counting_done(page):
    """跳过应推进到下一处，且**不能**记为已完成（计划 §12/T12）。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-review-btn")
    page.wait_for_timeout(3000)

    labels_before = page.evaluate(
        "Array.from(document.querySelectorAll('.cet-review-label')).map(e=>e.textContent)"
    )
    if "进行中" not in labels_before:
        pytest.skip("当前复习没有进行中的条目")

    skip = page.query_selector('button:has-text("跳过")')
    assert skip, "应有「跳过」按钮"
    skip.click()
    page.wait_for_timeout(2500)

    labels_after = page.evaluate(
        "Array.from(document.querySelectorAll('.cet-review-label')).map(e=>e.textContent)"
    )
    assert "已跳过" in labels_after, f"应出现「已跳过」，实际 {labels_after}"
    assert labels_after.count("已完成") == labels_before.count("已完成"), \
        "跳过不应增加「已完成」数量（跳过 ≠ 听懂）"
    assert "进行中" in labels_after, "跳过后应自动推进到下一处"


def test_review_pause_and_restore_after_reload(page):
    """暂停后刷新应恢复同一复习会话（计划 T11）。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-review-btn")
    page.wait_for_timeout(3000)

    pause = page.query_selector('button:has-text("暂停")')
    assert pause, "应有「暂停」按钮"
    pause.click()
    page.wait_for_timeout(2500)

    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4500)
    restored = page.evaluate("!document.getElementById('cet-review').hidden")
    assert restored, "刷新后应恢复复习面板"
    rows = page.evaluate("document.querySelectorAll('.cet-review-row').length")
    assert rows >= 1, f"恢复后应有条目，实际 {rows}"


# ── T16：窄窗口适配 ──────────────────────────────────────

@pytest.mark.parametrize("width,height,label", [
    (420, 900, "窄屏手机"),
    (768, 1024, "平板"),
    (1280, 800, "小笔记本"),
])
def test_narrow_window_no_overflow(browser, width, height, label):
    """窄窗口下按钮可操作、无横向溢出（计划 T16）。

    用共享的 browser fixture：Playwright Sync API 不能在同一个进程里
    反复 with sync_playwright()（会撞 asyncio loop）。
    """
    pg = browser.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        pg.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
        pg.wait_for_timeout(3500)

        assert pg.evaluate("!!document.querySelector('#cet-toggle-mode')"), \
            f"{label}: 卡点按钮应存在"

        clickable = pg.evaluate("""() => {
          const b = document.querySelector('#cet-toggle-mode');
          const r = b.getBoundingClientRect();
          return r.width > 20 && r.height > 15
                 && r.left >= 0 && r.right <= window.innerWidth + 2;
        }""")
        assert clickable, f"{label}: 卡点按钮应可点击且不超出视口"

        overflow = pg.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
        )
        assert not overflow, f"{label}: 不应出现横向溢出"

        assert not errors, f"{label}: 不应有 JS 错误 {errors}"
    finally:
        pg.close()


# ── 返修回归（F01–F06）──────────────────────────────────

def test_f06_unanswered_shows_dash_not_a(page):
    """F06：未作答题必须显示「-」，不能显示成「你选 A」。

    根因是 '' in 'ABCDE' 为 True 且 .index('')=0，空作答被当成选项 A。
    """
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4000)

    # 只答第 1 题，其余留空
    opts = page.query_selector_all(".cet-opt")
    assert opts, "选项应已渲染"
    opts[0].click()
    page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(3000)

    text = page.evaluate("document.querySelector('#cet-question-list').innerText")
    import re as _re
    picks = _re.findall(r"你选 ([A-E-])", text)
    assert picks, f"应能读到判题结果，实际文本：{text[:200]}"
    assert picks[0] == "A", f"第 1 题选了 A，应显示 A，实际 {picks[0]}"
    assert picks[1:] and all(p == "-" for p in picks[1:]), \
        f"未作答题应显示「-」，实际 {picks[1:]}"


def test_f03_reload_restores_attempt_and_practice(page):
    """F03：刷新后必须恢复交卷状态与带练面板，不能只恢复标记。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4000)

    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    page.keyboard.press("Space")
    page.wait_for_timeout(1800)
    page.evaluate(f"{AUDIO}.pause()")

    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(2800)

    confirm = page.query_selector('button:has-text("确认这句")')
    assert confirm, "应出现确认按钮"
    confirm.click()
    page.wait_for_timeout(2500)

    start = page.query_selector('button:has-text("开始带练")')
    assert start, "应出现开始带练"
    start.click()
    page.wait_for_timeout(3000)

    page.reload(wait_until="networkidle")
    page.wait_for_timeout(5000)

    state = page.evaluate("(document.querySelector('#cet-attempt-state')||{}).textContent || ''")
    assert "已交卷" in state, f"刷新后应恢复交卷状态，实际 {state!r}"

    practice_visible = page.evaluate(
        "!!document.querySelector('#cet-practice') && !document.querySelector('#cet-practice').hidden"
    )
    assert practice_visible, "刷新后带练面板应恢复"


def test_f02_paused_review_shows_resume_button(page):
    """F02：暂停后必须显示「已暂停」并给出「继续复习」，不能仍显示「进行中/暂停」。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.evaluate("localStorage.clear()")
    page.click("#cet-review-btn")
    page.wait_for_timeout(3000)

    pause = page.query_selector('button:has-text("暂停")')
    assert pause, "进行中应显示「暂停」"
    pause.click()
    page.wait_for_timeout(2500)

    state = page.evaluate("(document.querySelector('#cet-review-state')||{}).textContent || ''")
    assert "已暂停" in state, f"暂停后状态应显示「已暂停」，实际 {state!r}"

    has_resume = page.evaluate(
        "!!Array.from(document.querySelectorAll('button')).find(b=>b.textContent.includes('继续复习'))"
    )
    assert has_resume, "暂停后应出现「继续复习」按钮"


def test_f02_review_replay_seeks_to_sentence_start(page):
    """F02：复习回听必须跳到条目句子的起点（曾因字段拼写错误停在开头）。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-review-btn")
    page.wait_for_timeout(3000)

    # R6：必须真正拿到条目起点；取不到直接失败，不能跳过断言
    entry_start = page.evaluate("""() => {
      const st = window.__cet && window.__cet.getReviewState && window.__cet.getReviewState();
      const e = st && st.entries && st.entries[0];
      return e && e.item && e.item.sentence ? e.item.sentence.startSeconds : null;
    }""")
    assert entry_start is not None, \
        "取不到复习条目的起始时间（getReviewState 未导出或条目缺 sentence）"

    # R6：限定在复习面板内找「回听」，不能点到卡点按钮
    play = page.query_selector("#cet-review button:has-text('回听')")
    assert play, "复习面板应有「回听」按钮"
    play.click()
    page.wait_for_timeout(1200)

    now = page.evaluate(f"{AUDIO}.currentTime")
    # 断言真的跳到了该条目起点附近（±1.5 秒），不能只看「> 0.05」
    assert abs(now - entry_start) < 1.5, \
        f"回听应跳到条目起点 {entry_start}，实际 {now}"

    # 断言播放确实在推进
    t1 = page.evaluate(f"{AUDIO}.currentTime")
    page.wait_for_timeout(800)
    t2 = page.evaluate(f"{AUDIO}.currentTime")
    assert t2 > t1, f"回听后播放应推进，实际 {t1} → {t2}"

    # 暂停后音频必须停（R4）
    page.evaluate(f"{AUDIO}.pause()")
    page.wait_for_timeout(300)
    assert page.evaluate(f"{AUDIO}.paused"), "暂停后音频应停止"


def test_f04_feedback_persisted_to_record(page):
    """F04：用户反馈必须落库（latestFeedback / feedbackHistory），不能只切换动作。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4000)

    # 走到带练
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    page.keyboard.press("Space")
    page.wait_for_timeout(1800)
    page.evaluate(f"{AUDIO}.pause()")
    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(2800)
    confirm = page.query_selector('button:has-text("确认这句")')
    if confirm:
        confirm.click()
        page.wait_for_timeout(2500)
    start = page.query_selector('button:has-text("开始带练")')
    assert start, "应出现开始带练"
    start.click()
    page.wait_for_timeout(3000)

    sound = page.query_selector('button:has-text("没听清")')
    assert sound, "应有「没听清」按钮"
    sound.click()
    page.wait_for_timeout(2500)

    latest = page.evaluate("window.__cet.S.practice && window.__cet.S.practice.latestFeedback")
    assert latest == "sound", f"反馈应落库为 sound，实际 {latest!r}"


# ── 复验返修回归（R1–R6）────────────────────────────────

def test_r1_cross_material_does_not_leak_state(browser):
    """R1：在六级页面交卷带练后切到四级，不能串入六级的成绩与练习。"""
    pg = browser.new_page(viewport={"width": 1500, "height": 1400})
    try:
        pg.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
        pg.evaluate("localStorage.clear(); sessionStorage.clear()")
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(4000)

        pg.click("#cet-toggle-mode")
        pg.wait_for_timeout(400)
        pg.evaluate(f"{AUDIO}.play()")
        pg.wait_for_timeout(1200)
        pg.keyboard.press("Space")
        pg.wait_for_timeout(1800)
        pg.evaluate(f"{AUDIO}.pause()")
        opts = pg.query_selector_all(".cet-opt")
        if opts:
            opts[0].click()
            pg.wait_for_timeout(150)
        pg.click("#cet-submit")
        pg.wait_for_timeout(2800)

        # 切到四级页面
        pg.goto(f"{BASE}/workspace/{CET4_LESSON_ID}", wait_until="networkidle", timeout=45000)
        pg.wait_for_timeout(4500)

        assert pg.evaluate("window.__cet.S.materialId") == "cet4-sample-hope-wish", \
            "四级页面应识别为四级材料"
        state = pg.evaluate("(document.querySelector('#cet-attempt-state')||{}).textContent || ''")
        assert "已交卷" not in state, f"四级页面不应显示六级成绩，实际 {state!r}"

        practice_visible = pg.evaluate(
            "!!document.querySelector('#cet-practice') && !document.querySelector('#cet-practice').hidden"
        )
        assert not practice_visible, "四级页面不应显示六级的带练面板"
    finally:
        pg.close()


def test_r4_review_pause_stops_audio(page):
    """R4：复习暂停后音频必须真的停止，不能显示已暂停但还在响。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(3500)
    page.click("#cet-review-btn")
    page.wait_for_timeout(3000)

    play = page.query_selector("#cet-review button:has-text('回听')")
    assert play, "复习面板应有回听按钮"
    play.click()
    page.wait_for_timeout(1200)
    assert not page.evaluate(f"{AUDIO}.paused"), "回听后应在播放"

    pause = page.query_selector("#cet-review button:has-text('暂停')")
    assert pause, "应有暂停按钮"
    pause.click()
    page.wait_for_timeout(1500)

    assert page.evaluate(f"{AUDIO}.paused"), "暂停后音频必须停止"
    t1 = page.evaluate(f"{AUDIO}.currentTime")
    page.wait_for_timeout(800)
    t2 = page.evaluate(f"{AUDIO}.currentTime")
    assert abs(t2 - t1) < 0.1, f"暂停后时间不应推进：{t1} → {t2}"


def test_r5_waiting_note_mounted_during_request(page):
    """R5：请求期间等待提示必须真的挂到 DOM（原实现 parentNode 恒为 null）。"""
    page.goto(f"{BASE}/workspace/{LESSON_ID}", wait_until="networkidle", timeout=45000)
    page.evaluate("localStorage.clear(); sessionStorage.clear()")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(4000)

    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)
    page.evaluate(f"{AUDIO}.play()")
    page.wait_for_timeout(1200)
    page.keyboard.press("Space")
    page.wait_for_timeout(1800)
    page.evaluate(f"{AUDIO}.pause()")
    opts = page.query_selector_all(".cet-opt")
    if opts:
        opts[0].click()
        page.wait_for_timeout(150)
    page.click("#cet-submit")
    page.wait_for_timeout(2800)
    confirm = page.query_selector('button:has-text("确认这句")')
    if confirm:
        confirm.click()
        page.wait_for_timeout(2500)
    start = page.query_selector('button:has-text("开始带练")')
    assert start, "应出现开始带练"
    start.click()
    page.wait_for_timeout(3000)

    # R5：请求要延迟才观察得到等待区。无 key 时后端秒回 503，
    # 不延迟的话等待提示刚挂上就被移除，测不到「是否挂载」。
    page.evaluate("""() => {
      const orig = window.fetch;
      window.__cetDelayNext = true;
      window.fetch = async (url, opt) => {
        if (window.__cetDelayNext && String(url).includes('/next')) {
          window.__cetDelayNext = false;
          await new Promise(r => setTimeout(r, 2500));
        }
        return orig(url, opt);
      };
    }""")

    meaning = page.query_selector('button:has-text("不懂意思")')
    assert meaning, "应有不懂意思按钮"
    meaning.click()

    # 请求进行中检查等待区确实挂在 DOM
    page.wait_for_timeout(700)
    during = page.evaluate("""() => {
      const n = document.querySelector('#cet-practice-out .cet-ai-note');
      return n ? n.textContent : null;
    }""")
    assert during is not None, \
        "请求期间等待提示必须挂在 DOM 上（原实现创建后未挂载）"

    # 3 秒后兜底文案应变成可操作说明
    page.wait_for_timeout(3000)
    after = page.evaluate("""() => {
      const n = document.querySelector('#cet-practice-out .cet-ai-note');
      return n ? n.textContent : null;
    }""")
    if after is not None:
        assert ('回听' in after) or ('慢放' in after) or ('保存' in after) or ('模型' in after), \
            f"3 秒兜底应给可操作提示，实际 {after!r}"
