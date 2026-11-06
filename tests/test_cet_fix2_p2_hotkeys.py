"""返修2 包2 · 底座快捷键（P2-1…P2-4）· 真实浏览器行为测试。

与 tests/test_cet_base_player.py 的分工：
    - 那个文件锁静态契约（源码里必须有什么结构、不许有什么反模式）
    - 这个文件在**真实服务 + 真实 HTMLMediaElement** 上验证行为：
      Space 翻转 paused、→ 跳到下一句起点、L 后跨句尾回句首、重复 mount 只翻转一次、
      队友模块开启时底座一次都不处理

三条运行前提（缺一不可，否则整组 skip 或该用例 skip）：

1. **底座关闭态服务**（队友模块不加载）—— P2-1/P2-3/P2-4 的主场
       ELT_USER_DB=<D>/vocab.db ELT_CONFIG_DIR=<D>/config ELT_PORT=5182 \\
         .venv/bin/python backend/fastapi_server.py
       CET_BASE_URL=http://127.0.0.1:5182

2. **队友开启态服务** —— P2-2
       ELT_CET_FEATURE_CET_LISTENING_AGENT=1 ELT_PORT=5186
       CET_AGENT_ON_BASE_URL=http://127.0.0.1:5186

3. **通用模式服务**（ELT_CET_MODE=0，且队友模块关闭）—— §0.6 回归
       ELT_CET_MODE=0 ELT_CET_FEATURE_CET_LISTENING_AGENT=0 ELT_PORT=5188
       CET_GENERIC_BASE_URL=http://127.0.0.1:5188

为什么三个服务都要显式给 URL：这些用例会真实播放音频、写学习进度。
指向残留的旧实例会读到别人的历史进度，断言会假失败（与 test_cet_base_workspace.py
同一纪律）。三个变量都没设时整组 skip，不制造假失败。

为什么不用 route.abort() 伪造「队友不在」：任务书 §0.7 明确禁止。队友模块是否加载
由后端 cet_mode 开关决定，三个服务就是从配置层真实区分的三种状态。
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

# 三种状态各自的真实服务地址（都必填，缺哪项就 skip 对应用例）
BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")
AGENT_ON_BASE = os.environ.get("CET_AGENT_ON_BASE_URL", "").rstrip("/")
GENERIC_BASE = os.environ.get("CET_GENERIC_BASE_URL", "").rstrip("/")

# 句循环断言的时间窗：比句长短太多会假失败，太长会拖慢测试
LOOP_TRACK_MS = 9000


def _up(base: str) -> bool:
    if not base:
        return False
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _first_lesson(base: str) -> int:
    with urllib.request.urlopen(f"{base}/api/cet/materials", timeout=5) as r:
        data = json.load(r)
    items = data.get("items") or data.get("materials") or []
    for item in items:
        tail = str(item.get("lessonUrl") or "").rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            return int(tail)
    raise AssertionError(f"{base} 没有可用的四六级材料（先跑 scripts/cet/import_materials.py --all）")


pytestmark = pytest.mark.skipif(
    not _up(BASE),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过底座快捷键浏览器测试"
    "（示例：CET_BASE_URL=http://127.0.0.1:5182）",
)


# ── fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    # 无头浏览器默认拦截自动播放；这里放开策略，断言检查的仍是真实媒体状态
    launch_args = ["--autoplay-policy=no-user-gesture-required"]
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome", args=launch_args)
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True, args=launch_args)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._p2_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _open(page, base: str, *, expect_agent: bool) -> int:
    """打开工作区并等到播放器 + 逐句时间轴就绪。"""
    lesson_id = _first_lesson(base)
    page.goto(f"{base}/workspace/{lesson_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function(
        "() => window.__ws && Array.isArray(window.__ws.sentences) && window.__ws.sentences.length > 0",
        timeout=20000,
    )
    page.wait_for_function("() => !!window.__cetPlayer && window.__cetPlayer.isMounted()", timeout=15000)
    if expect_agent:
        # 队友模块的 boot() 要先等两个接口才 mount；等它的工具栏出现
        page.wait_for_selector("#cet-toolbar", state="attached", timeout=15000)
    # 归属同步在 updateBar()/1.5s 定时器里，给一轮时间让它稳定
    page.wait_for_timeout(1800)
    return lesson_id


def _audio(page, expr: str):
    return page.evaluate(
        "() => { const a = document.getElementById('local-media-player'); return a ? (" + expr + ") : null; }"
    )


def _sentences(page) -> list[dict]:
    return page.evaluate("() => window.__ws.sentences.map(s => ({start: s.start, end: s.end}))")


def _pause(page):
    page.evaluate("() => window.__ws.pause()")
    page.wait_for_timeout(350)


# ── P2-1：关闭态接管 Space / ← / → / R / L ──────────────────


def test_p2_1_space_toggles_paused(page):
    """P2-1 验收①：Space 后 HTMLMediaElement.paused 翻转。"""
    _open(page, BASE, expect_agent=False)

    # 前置：底座真的接管了（否则后面按空格没反应是别的原因，不该混在一起判）
    assert page.evaluate("() => window.__cetPlayer.ownsSpaceKey") is True
    assert page.evaluate("() => window.__cetPlayer._state.keydownBound") is True

    _pause(page)
    assert _audio(page, "a.paused") is True

    page.keyboard.press("Space")
    page.wait_for_timeout(700)
    assert _audio(page, "a.paused") is False, "Space 未开始播放"

    page.keyboard.press("Space")
    page.wait_for_timeout(700)
    assert _audio(page, "a.paused") is True, "Space 未暂停播放"

    assert page.evaluate("() => window.__cetPlayer._state.keyHandled") >= 2


def test_p2_1_arrow_jumps_to_next_sentence_start(page):
    """P2-1 验收②：→ 后 currentTime 跳到下一句起点。"""
    _open(page, BASE, expect_agent=False)
    sents = _sentences(page)
    assert len(sents) >= 3

    # 先落到第 2 句起点，再按 → 应该到第 3 句起点
    page.evaluate(f"() => window.__ws.seek({sents[1]['start']})")
    page.wait_for_timeout(500)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(600)

    now = _audio(page, "a.currentTime")
    assert abs(now - sents[2]["start"]) < 0.45, (
        f"→ 未跳到第 3 句起点：currentTime={now}, 期望 {sents[2]['start']}（±0.45）"
    )

    # ← 回到第 2 句起点
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(600)
    back = _audio(page, "a.currentTime")
    assert abs(back - sents[1]["start"]) < 0.45, f"← 未回到第 2 句起点：{back} vs {sents[1]['start']}"


def test_p2_1_r_replays_current_sentence(page):
    """P2-1：R 重听当前句（回到句首）。"""
    _open(page, BASE, expect_agent=False)
    sents = _sentences(page)
    unit = sents[2]

    # 停在句中间
    mid = unit["start"] + max(0.15, (unit["end"] - unit["start"]) * 0.5)
    page.evaluate(f"() => window.__ws.seek({mid})")
    page.wait_for_timeout(500)
    assert _audio(page, "a.currentTime") > unit["start"] + 0.1

    page.keyboard.press("R")
    page.wait_for_timeout(600)
    # R 在暂停态会顺带开始播放，currentTime 会立刻往前走，所以断言「落点」而不是
    # 事后采样值：lastReplay 记录的是 seek 的目标句首，不受播放推进影响。
    replay = page.evaluate("() => window.__cetPlayer._state.lastReplay")
    assert replay is not None, "R 未触发重听"
    assert abs(replay["start"] - unit["start"]) < 0.3, f"R 未回到本句句首：{replay} vs {unit['start']}"
    now = _audio(page, "a.currentTime")
    assert now < unit["end"] + 0.3, f"R 后播放位置跑出了本句：{now}（句尾 {unit['end']}）"


def test_p2_1_l_loops_back_to_sentence_start(page):
    """P2-1 验收③：L 开启后跨过句尾回到句首。

    断言方式是**轨迹**而不是单点：采样 currentTime，必须出现「上涨 → 回落」，
    且全程不超过句尾太多。单点断言分不清「正常播到下一句」和「循环回跳」。
    """
    _open(page, BASE, expect_agent=False)
    sents = _sentences(page)
    unit = sents[0]
    span = unit["end"] - unit["start"]
    assert span > 0.8, f"首句太短（{span}s），无法稳定验证循环，换一份材料"

    page.evaluate(f"() => window.__ws.seek({unit['start']})")
    page.wait_for_timeout(400)
    page.keyboard.press("L")
    page.wait_for_timeout(300)

    assert page.evaluate("() => window.__cetPlayer._state.loopEnabled") is True, "L 未开启句循环"
    anchor = page.evaluate("() => window.__cetPlayer._state.loopAnchor")
    assert anchor and abs(anchor["start"] - unit["start"]) < 0.3, f"循环锚点不对：{anchor}"
    assert abs(anchor["end"] - unit["end"]) < 0.3, f"循环锚点句尾不对：{anchor}"

    page.evaluate("() => window.__ws.play()")
    traj: list[float] = []
    for _ in range(int(LOOP_TRACK_MS / 120)):
        page.wait_for_timeout(120)
        traj.append(_audio(page, "a.currentTime"))
    _pause(page)

    # 必须发生过回跳：后一个采样比前一个采样小了一大截
    drops = [
        (traj[i], traj[i + 1])
        for i in range(len(traj) - 1)
        if traj[i + 1] < traj[i] - 0.3
    ]
    assert drops, f"L 开启后未出现回跳（轨迹单调上升）：{traj[:12]}…"
    before, after = drops[0]
    assert before <= unit["end"] + 0.5, f"回跳发生在句尾之后太久：{before} > {unit['end']}"
    assert abs(after - unit["start"]) < 0.6, f"回跳未落到句首：{after} vs {unit['start']}"

    # 全程不应真的播到下一句之后（证明不是「播过去就不管了」）
    next_start = sents[1]["start"]
    assert max(traj) < next_start + 0.5, f"循环期间播进了下一句：max={max(traj)}, 下一句起点={next_start}"

    # 再按一次 L 关闭，回跳消失
    page.keyboard.press("L")
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__cetPlayer._state.loopEnabled") is False


def test_p2_1_typing_target_is_ignored(page):
    """P2-1：焦点在 input/textarea 时不响应快捷键（也不 preventDefault）。"""
    _open(page, BASE, expect_agent=False)
    _pause(page)
    handled_before = page.evaluate("() => window.__cetPlayer._state.keyHandled")

    page.evaluate(
        """() => {
            const t = document.createElement('textarea');
            t.id = 'p2-probe-textarea';
            document.body.appendChild(t);
            t.focus();
        }"""
    )
    page.wait_for_timeout(200)
    assert page.evaluate("() => document.activeElement.id") == "p2-probe-textarea"

    page.keyboard.press("Space")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(500)

    handled_after = page.evaluate("() => window.__cetPlayer._state.keyHandled")
    assert handled_after == handled_before, "输入框里底座仍处理了快捷键"
    assert _audio(page, "a.paused") is True, "输入框里按空格把播放打开了"


# ── P2-2：队友模块开启后必须让出 ─────────────────────────────


@pytest.mark.skipif(not _up(AGENT_ON_BASE), reason="未设置 CET_AGENT_ON_BASE_URL（队友模块开启态服务）")
def test_p2_2_base_yields_all_hotkeys_when_agent_on(page):
    """P2-2：ELT_CET_FEATURE_CET_LISTENING_AGENT=1 时底座 handler 一次都不触发。"""
    _open(page, AGENT_ON_BASE, expect_agent=True)

    # 队友模块真的在（不是「因为没加载所以让出」）
    assert page.evaluate("() => window.__cetPlayer.cetListeningEnabled()") is True
    assert page.locator("#cet-toolbar").count() == 1

    # 底座已让出：声明与实现一致
    assert page.evaluate("() => window.__cetPlayer.ownsSpaceKey") is False
    assert page.evaluate("() => window.__cetPlayer._state.keydownBound") is False

    _pause(page)
    assert _audio(page, "a.paused") is True
    before_time = _audio(page, "a.currentTime")
    before_handled = page.evaluate("() => window.__cetPlayer._state.keyHandled")

    page.keyboard.press("Space")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("R")
    page.keyboard.press("L")
    page.wait_for_timeout(900)

    assert page.evaluate("() => window.__cetPlayer._state.keyHandled") == before_handled == 0, (
        "队友开启态底座仍处理了按键（计数器不为 0）"
    )
    # 底座没碰：暂停态保持暂停，时间没被 seek
    assert _audio(page, "a.paused") is True, "队友开启态底座仍抢了 Space"
    assert abs(_audio(page, "a.currentTime") - before_time) < 0.4, "队友开启态底座仍抢了方向键"
    assert page.evaluate("() => window.__cetPlayer._state.loopEnabled") is False, "队友开启态底座仍开了句循环"


# ── P2-3：「⌨ 快捷键」提示 ───────────────────────────────────


def test_p2_3_hotkey_hint_only_in_base_owned_state(page):
    """P2-3：提示只在关闭态（底座接管态）显示，hover/点击可展开。"""
    _open(page, BASE, expect_agent=False)

    hint = page.locator("#cet-hotkey-hint")
    assert hint.count() == 1, "状态条里没有「⌨ 快捷键」提示"
    assert hint.is_visible(), "底座接管态下提示应可见"
    assert page.locator("#cet-hotkey-btn").inner_text().strip() == "⌨ 快捷键"

    panel = page.locator("#cet-hotkey-panel")
    assert panel.is_hidden(), "提示面板默认应收起"

    # 点击展开
    page.locator("#cet-hotkey-btn").click()
    page.wait_for_timeout(250)
    assert panel.is_visible(), "点击后提示面板未展开"
    assert page.locator("#cet-hotkey-btn").get_attribute("aria-expanded") == "true"
    text = panel.inner_text()
    for needle in ("Space", "←", "→", "R", "L"):
        assert needle in text, f"提示面板缺少 {needle} 说明"

    # 再点收起
    page.locator("#cet-hotkey-btn").click()
    page.wait_for_timeout(250)
    assert panel.is_hidden(), "再次点击后面板未收起"


@pytest.mark.skipif(not _up(AGENT_ON_BASE), reason="未设置 CET_AGENT_ON_BASE_URL（队友模块开启态服务）")
def test_p2_3_hotkey_hint_hidden_when_agent_owns(page):
    """P2-3：队友开启态下不显示底座快捷键提示（避免两套快捷键说明打架）。"""
    _open(page, AGENT_ON_BASE, expect_agent=True)
    hint = page.locator("#cet-hotkey-hint")
    assert hint.count() == 1, "提示元素应存在（只是隐藏），便于状态切换"
    assert hint.is_hidden(), "队友接管态下底座快捷键提示仍可见"


# ── P2-4：重复 mount 幂等 ───────────────────────────────────


def test_p2_4_repeated_mount_does_not_double_bind(page):
    """P2-4：调两次（这里三次）mount 后按 Space 只翻转一次。"""
    _open(page, BASE, expect_agent=False)
    assert page.evaluate("() => window.__cetPlayer._state.keydownBound") is True

    page.evaluate("() => { window.__cetPlayer.mount(); window.__cetPlayer.mount(); window.__cetPlayer.mount(); }")
    page.wait_for_timeout(500)

    # 仍然只有一份 DOM、一个绑定
    assert page.locator("#cet-study-bar").count() == 1, "状态条重复挂载"
    assert page.locator("#cet-hotkey-hint").count() == 1, "快捷键提示重复挂载"
    assert page.evaluate("() => window.__cetPlayer._state.keydownBound") is True

    _pause(page)
    assert _audio(page, "a.paused") is True
    before = page.evaluate("() => window.__cetPlayer._state.keyHandled")

    page.keyboard.press("Space")
    page.wait_for_timeout(800)

    delta = page.evaluate("() => window.__cetPlayer._state.keyHandled") - before
    assert delta == 1, f"重复 mount 后一次 Space 被处理了 {delta} 次（应恰好 1 次）"
    assert _audio(page, "a.paused") is False, "一次 Space 没把播放打开"


# ── §0.6 通用模式回归 ───────────────────────────────────────


@pytest.mark.skipif(not _up(GENERIC_BASE), reason="未设置 CET_GENERIC_BASE_URL（ELT_CET_MODE=0 服务）")
def test_p2_generic_mode_has_no_base_hotkeys(page):
    """§0.6：ELT_CET_MODE=0 时行为不变 —— 原版没有这些快捷键，底座也不注册。"""
    _open(page, GENERIC_BASE, expect_agent=False)

    assert page.evaluate("() => window.__cetPlayer.cetOn") is False
    assert page.evaluate("() => window.__cetPlayer.ownsSpaceKey") is False, "通用模式不应接管快捷键"
    assert page.evaluate("() => window.__cetPlayer._state.keydownBound") is False, "通用模式仍注册了 keydown"

    _pause(page)
    before_paused = _audio(page, "a.paused")
    before_time = _audio(page, "a.currentTime")
    before_handled = page.evaluate("() => window.__cetPlayer._state.keyHandled")

    page.keyboard.press("Space")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("R")
    page.keyboard.press("L")
    page.wait_for_timeout(800)

    assert page.evaluate("() => window.__cetPlayer._state.keyHandled") == before_handled == 0
    assert _audio(page, "a.paused") == before_paused, "通用模式下底座改变了播放状态（行为不再「不变」）"
    assert abs(_audio(page, "a.currentTime") - before_time) < 0.4, "通用模式下底座 seek 了播放位置"
    assert page.evaluate("() => window.__cetPlayer._state.loopEnabled") is False

    # 状态条与快捷键提示照旧不出现（通用模式没有这套 UI 的语义）
    assert page.locator("#cet-hotkey-hint").is_hidden(), "通用模式下仍显示底座快捷键提示"
