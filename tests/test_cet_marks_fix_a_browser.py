"""卡点修复 · 包A（前端）真实浏览器端到端测试 —— 真实服务 + 真实点击。

运行前提（与 tests/test_cet_fix21_a_browser.py 同一纪律）：
    1. 服务已启动（隔离库）：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_CONFIG_DIR=/tmp/<x>/config ELT_PORT=5281 \
           .venv/bin/python backend/fastapi_server.py
    2. 固定样例材料已导入：
         ELT_USER_DB=... ELT_CONFIG_DIR=... .venv/bin/python scripts/cet/import_materials.py --all
    3. 一份**无逐句时间轴**的四六级材料（问题3 用），只导音频：
         ... scripts/cet/import_materials.py --audio <某 mp3> --title "无时间轴材料（仅音频）" --level cet4

环境变量：
    CET_BASE_URL   必填。不设则整组 skip。
    CET_NO_TIMELINE_LESSON  可选。无时间轴材料的 lesson 号；不设则自动探测
                            （扫 /api/cet/materials 找 sentenceCount==0 且 lessonUrl 可用的）。

为什么必须真实浏览器（任务书纪律）：
    问题1 的验收是「连按 3 次 M，currentTime 单调递增、不出现回退」；
    问题3 是「无时间轴材料打开工作区没有 #cet-toolbar」；
    问题6 是「点时间线刻度，播放位置落在该刻度对应处」。
    这三条都是**运行时行为 + 播放器真实状态**，断言静态 HTML 没有任何意义。
    也不允许 route.abort() 拦请求 —— 那是「假装模块不在」，不是产品行为。
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")
AUDIO = "document.getElementById('local-media-player')"


def _service_up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过浏览器端到端测试"
    "（示例：CET_BASE_URL=http://127.0.0.1:5281）",
)


def _get_json(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _lesson_of(item: dict) -> int | None:
    tail = str(item.get("lessonUrl") or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _materials() -> list[dict]:
    try:
        data = _get_json("/api/cet/materials")
    except Exception:  # noqa: BLE001
        return []
    return data.get("items") or data.get("materials") or []


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    args = ["--autoplay-policy=no-user-gesture-required"]
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome", args=args)
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True, args=args)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg._reqs = []  # type: ignore[attr-defined]
    pg.on("request", lambda r: pg._reqs.append(f"{r.method} {r.url.replace(BASE, '')}"))
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


@pytest.fixture(scope="module")
def timed_lesson() -> int:
    for item in _materials():
        lesson = _lesson_of(item)
        if lesson is not None and int(item.get("sentenceCount") or 0) > 0:
            return lesson
    pytest.skip("需要至少一份带逐句时间轴的四六级材料（scripts/cet/import_materials.py --all）")


def _open_listening(page, lesson: int):
    """打开工作区 → 等模块就绪 → 进入卡点练习模式。"""
    page.goto(f"{BASE}/workspace/{lesson}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function("() => window.__cet && window.__cet.S.materialId", timeout=20000)
    page.wait_for_timeout(1200)
    page.click("#cet-toggle-mode")
    page.wait_for_timeout(400)


def _cur(pg) -> float:
    return round(pg.evaluate(f"() => {AUDIO} ? {AUDIO}.currentTime : -1"), 3)


def _marks(pg) -> list[dict]:
    return pg.evaluate(
        "() => (window.__cet.S.marks || []).map(m => ({"
        " id: m.markId, pressed: m.pressedAtSec, sug: m.suggestedSentenceId,"
        " conf: m.confirmedSentenceId, inPractice: m.inPractice }))"
    )


def _ensure_autoplay_off(pg):
    """把「标记后自动回听」显式关掉（默认就是关的，这里防御上一个用例残留）。"""
    pg.evaluate("() => window.__cet.setAutoplayOnMark(false, { silent: true })")
    pg.wait_for_timeout(150)


# ══════════════════════════════════════════════════════════════
# A-1（P0）：连按 3 次 M，播放位置不被抢回
# ══════════════════════════════════════════════════════════════


def test_a1_three_marks_do_not_rewind_playhead(page, timed_lesson):
    """关闭自动回听后：连按 3 次 M，currentTime 单调递增、不回退。

    改前实测（主代理审计 + 本地复现）：3.74s → 0.058s，连按 3 次全落同一点。
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)

    page.evaluate(f"{AUDIO}.currentTime = 2.9; {AUDIO}.play()")
    page.wait_for_timeout(900)

    records = []
    for i in range(3):
        before = _cur(page)
        page.keyboard.press("KeyM")
        page.wait_for_timeout(130)          # 立刻看有没有被 seek 走
        right_after = _cur(page)
        # 注意：间隔必须 > 后端 1.5s 去重窗口，否则第 3 次会被合法去重成第 2 次
        # （那是 A-2 的正确行为，不是 A-1 的靶子）。1x 倍速下墙钟≈音频时间。
        page.wait_for_timeout(1700)
        records.append({
            "i": i + 1,
            "before": before,
            "after_130ms": right_after,
            "after_1.8s": _cur(page),
        })

    # ① 按完那一刻不能被拽回去（这是问题1 的核心症状）
    for r in records:
        assert r["after_130ms"] >= r["before"] - 0.05, (
            f"第 {r['i']} 次按 M 后播放位置被抢回：{r['before']} → {r['after_130ms']}"
            f"（问题1 回归：自动回听又无条件生效了）\n全部记录：{records}"
        )

    # ② 整段单调递增（播放中，不该出现回退）
    times = [r["before"] for r in records] + [records[-1]["after_1.8s"]]
    assert all(b >= a for a, b in zip(times, times[1:])), f"播放位置出现回退：{times}"

    # ③ 3 个标记的 pressedAtSec 各不相同（改前：1.459 / 1.459 / 1.459）
    pressed = [m["pressed"] for m in _marks(page)]
    assert len(pressed) >= 3, (
        f"应有至少 3 个标记，实际 {len(pressed)}：{pressed}\n"
        f"记录：{records}"
    )
    assert len(set(pressed)) == len(pressed), (
        f"多个标记落在同一时刻（问题1/2 回归）：{pressed}"
    )
    # 相邻标记间隔应接近我们的按键间隔（>1.5s），而不是全挤在一起
    gaps = [round(b - a, 3) for a, b in zip(pressed, pressed[1:])]
    assert all(g > 1.0 for g in gaps), (
        f"相邻标记间隔过小，说明按键时位置被拽回同一处：{pressed}（间隔 {gaps}）"
    )


def test_a1_switch_default_is_off(page, timed_lesson):
    """开关默认关闭，且工具栏里可见。"""
    page.goto(f"{BASE}/workspace/{timed_lesson}", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__cet && window.__cet.S.materialId", timeout=20000)
    page.wait_for_timeout(1000)
    state = page.evaluate(
        "() => ({ s: window.__cet.S.autoplayOnMark,"
        " box: document.getElementById('cet-autoplay-mark')"
        "   ? document.getElementById('cet-autoplay-mark').checked : null,"
        " inToolbar: !!document.querySelector('#cet-toolbar #cet-autoplay-mark'),"
        " text: document.querySelector('.cet-autoplay-switch')"
        "   ? document.querySelector('.cet-autoplay-switch').innerText.trim() : null })"
    )
    assert state["s"] is False, f"自动回听默认应为关闭，实际 {state['s']}"
    assert state["box"] is False, "复选框初始态应为未勾选"
    assert state["inToolbar"] is True, "开关不在工具栏里"
    assert "自动回听" in (state["text"] or ""), f"开关文案不对：{state['text']}"


def test_a1_switch_on_restores_autoplay(page, timed_lesson):
    """打开开关后，行为回到原来的「标记即回听候选句」（能力必须保留）。"""
    _open_listening(page, timed_lesson)
    page.evaluate("() => window.__cet.setAutoplayOnMark(true, { silent: true })")
    page.wait_for_timeout(200)
    assert page.evaluate("() => window.__cet.S.autoplayOnMark") is True

    page.evaluate(f"{AUDIO}.currentTime = 40.0; {AUDIO}.play()")
    page.wait_for_timeout(900)
    before = _cur(page)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(400)
    after = _cur(page)
    assert after < before - 1.0, (
        f"打开开关后应自动回听候选句（位置前移），实际 {before} → {after}。"
        "能力被误删了 —— 要求是「默认关闭 + 给开关」，不是删掉功能。"
    )
    page.evaluate("() => window.__cet.setAutoplayOnMark(false, { silent: true })")


def test_a1_switch_persists_across_reload(page, timed_lesson):
    """开关状态写进 localStorage，刷新后仍生效。"""
    page.goto(f"{BASE}/workspace/{timed_lesson}", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__cet && window.__cet.S.materialId", timeout=20000)
    page.wait_for_timeout(800)
    page.evaluate("() => window.__cet.setAutoplayOnMark(true, { silent: true })")
    page.wait_for_timeout(200)
    assert page.evaluate("() => localStorage.getItem('cet.marks.autoplay.v1')") == "1"

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("() => window.__cet && window.__cet.S.materialId", timeout=20000)
    page.wait_for_timeout(1000)
    assert page.evaluate("() => window.__cet.S.autoplayOnMark") is True, "刷新后开关状态丢了"
    assert page.evaluate(
        "() => document.getElementById('cet-autoplay-mark').checked"
    ) is True, "刷新后复选框没同步"

    # 复原成默认关闭，避免影响其它用例/后续手工验证
    page.evaluate("() => window.__cet.setAutoplayOnMark(false, { silent: true })")


def test_a1_switch_toggle_does_not_swallow_m_key(page, timed_lesson):
    """真实点击开关后，按 M 仍然有效。

    这是一个**实测发现**的坑：勾完复选框焦点留在 <input> 上，
    而 isTypingTarget() 把 input 一律当输入目标 → 之后按 M 全被吞掉。
    所以 setAutoplayOnMark 必须 blur。
    """
    _open_listening(page, timed_lesson)
    page.click("#cet-autoplay-mark")        # 真实点击，不用 JS
    page.wait_for_timeout(400)
    page.click("#cet-autoplay-mark")        # 再点回来（关）
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__cet.S.autoplayOnMark") is False

    n_before = len(_marks(page))
    page.evaluate(f"{AUDIO}.currentTime = 30.0")
    page.wait_for_timeout(200)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1500)
    n_after = len(_marks(page))
    assert n_after == n_before + 1, (
        f"点过开关后按 M 没有新增标记（{n_before} → {n_after}）："
        "焦点留在复选框上，M 键被 isTypingTarget 吞掉了"
    )


# ══════════════════════════════════════════════════════════════
# A-2：后端 1.5s 去重命中时 UI 不出现重复行
# ══════════════════════════════════════════════════════════════


def test_a2_dedup_window_does_not_duplicate_rows(page, timed_lesson):
    """1.5s 内连按 2 次 M：S.marks 长度 == 库内条数 == DOM 行数。

    改前实测：S.marks 3 → 5（delta 2），DOM 5 行，库里只有 2 条。
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)

    page.evaluate(f"{AUDIO}.currentTime = 50.0; {AUDIO}.play()")
    page.wait_for_timeout(700)

    n_before = len(_marks(page))
    page.keyboard.press("KeyM")
    page.wait_for_timeout(420)          # < 1.5s 去重窗口
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1500)

    marks = _marks(page)
    rows = page.evaluate("() => document.querySelectorAll('.cet-mark').length")

    # 后端 1.5s 去重会把第二次也映射到同一个 markId → 前端只应 +1
    delta = len(marks) - n_before
    assert delta == 1, (
        f"1.5s 内连按 2 次应只新增 1 个标记（后端去重），实际 +{delta}（问题2 回归）"
    )
    assert rows == len(marks), f"DOM 行数({rows}) 与 S.marks({len(marks)}) 不一致"

    # 库里真实条数（通过 GET /marks 对账，不直接读 DB）
    mid = page.evaluate("() => window.__cet.S.materialId")
    sid = page.evaluate("() => localStorage.getItem('cet.session.v1')")
    db = _get_json(f"/api/cet/marks?materialId={mid}&listeningSessionId={sid}")
    db_items = db.get("items") or db.get("marks") or []
    assert len(db_items) == len(marks), (
        f"S.marks({len(marks)}) 与库内条数({len(db_items)}) 不一致 —— UI 在展示不存在的标记"
    )
    # 同一 markId 不应在列表里出现两次
    ids = [m["id"] for m in marks]
    assert len(set(ids)) == len(ids), f"列表里出现重复 markId：{ids}"


# ══════════════════════════════════════════════════════════════
# A-3（P0）：无时间轴材料不出现假入口
# ══════════════════════════════════════════════════════════════


def _find_no_timeline_lesson(page) -> int | None:
    """找一份 sentenceCount==0 的四六级材料（问题3 的靶子）。"""
    env = os.environ.get("CET_NO_TIMELINE_LESSON")
    if env and env.isdigit():
        return int(env)
    for item in _materials():
        lesson = _lesson_of(item)
        if lesson is not None and int(item.get("sentenceCount") or 0) == 0:
            return lesson
    return None


def test_a3_no_timeline_material_has_no_fake_entry(page):
    """无逐句时间轴材料：打开工作区**没有** #cet-toolbar。

    改前实测：toolbar=true、文案「开始卡点练习 今日复习」、
    点进去能进 listening 模式（body.cet-listening=true）、按 M 才报
    「标记失败：该材料没有可用句子时间轴。」—— 就是假入口。
    """
    lesson = _find_no_timeline_lesson(page)
    if lesson is None:
        pytest.skip(
            "需要一份无逐句时间轴的四六级材料："
            "scripts/cet/import_materials.py --audio <mp3> --title '无时间轴材料（仅音频）' --level cet4"
        )

    page.goto(f"{BASE}/workspace/{lesson}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_timeout(3500)          # 给 boot() 足够的 async 时间

    state = page.evaluate(
        "() => ({"
        " toolbar: !!document.querySelector('#cet-toolbar'),"
        " toggle: !!document.querySelector('#cet-toggle-mode'),"
        " reviewBtn: !!document.querySelector('#cet-review-btn'),"
        " autoplaySwitch: !!document.querySelector('#cet-autoplay-mark'),"
        " cetMid: window.__cet ? window.__cet.S.materialId : null,"
        " cetSent: window.__cet ? (window.__cet.S.sentences || []).length : null,"
        " listening: window.__cet ? window.__cet.S.listening : null,"
        " bodyClass: document.body.className })"
    )
    # 确认确实是「材料被识别到了、但没时间轴」—— 不是 404 静默跳过
    assert state["cetMid"], "材料未被识别（这条用例要求材料是四六级、只是没有时间轴）"
    assert state["cetSent"] == 0, f"该材料应无句子，实际 {state['cetSent']}"

    assert state["toolbar"] is False, (
        f"无时间轴材料仍挂出了卡点工具栏（假入口，问题3 回归）：{state}"
    )
    assert state["toggle"] is False, "无时间轴材料仍出现「开始卡点练习」"
    assert state["reviewBtn"] is False, "无时间轴材料仍出现「今日复习」"
    assert state["autoplaySwitch"] is False, "无时间轴材料仍出现自动回听开关"
    assert state["listening"] is not True, "无时间轴材料仍进入了 listening 模式"

    # 页面本身要正常（不是白屏/报错）
    assert page.evaluate("() => document.body.innerText.length") > 50, "页面应有内容"
    errors = [e for e in page._errors if "favicon" not in e]  # type: ignore[attr-defined]
    assert not errors, f"无时间轴材料页面有 JS 报错：{errors}"


def test_a3_timed_material_still_mounts(page, timed_lesson):
    """有逐句时间轴的材料：工具栏照常出现（不能为了修假入口把正常路径断掉）。"""
    page.goto(f"{BASE}/workspace/{timed_lesson}", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__cet && window.__cet.S.materialId", timeout=20000)
    page.wait_for_timeout(1200)
    state = page.evaluate(
        "() => ({"
        " toolbar: !!document.querySelector('#cet-toolbar'),"
        " toggle: !!document.querySelector('#cet-toggle-mode'),"
        " reviewBtn: !!document.querySelector('#cet-review-btn'),"
        " sent: (window.__cet.S.sentences || []).length })"
    )
    assert state["sent"] > 0
    assert state["toolbar"] is True, "有题材料的工具栏不见了（修过头）"
    assert state["toggle"] is True, "有题材料缺「开始卡点练习」"
    assert state["reviewBtn"] is True, "有题材料缺「今日复习」"


def test_a3_skip_reason_is_logged(page):
    """跳过挂载时要有 console.info 说明原因（不是静默消失，便于排障）。"""
    lesson = _find_no_timeline_lesson(page)
    if lesson is None:
        pytest.skip("需要一份无逐句时间轴的四六级材料")
    logs: list[str] = []
    page.on("console", lambda msg: logs.append(msg.text))
    page.goto(f"{BASE}/workspace/{lesson}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_timeout(3500)
    hit = [t for t in logs if "没有逐句时间轴" in t]
    assert hit, f"跳过挂载没有 console.info 说明原因，实际 console：{logs[:20]}"


# ══════════════════════════════════════════════════════════════
# A-4：列表里「标记时刻」与「候选句区间」明确分开
# ══════════════════════════════════════════════════════════════


def test_a4_row_shows_two_distinct_time_scopes(page, timed_lesson):
    """每行必须同时出现「标记于 X」与「候选句 A–B」，且有箭头分隔。

    改前实测（同一行）：
        0:05.60 | 待确认 | It's getting late... | 预览 0:00.01–0:03.49
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)
    page.evaluate(f"{AUDIO}.currentTime = 20.0; {AUDIO}.play()")
    page.wait_for_timeout(700)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1500)

    row = page.evaluate(
        """() => {
        const r = document.querySelector('.cet-mark');
        if (!r) return null;
        const t = r.querySelector('.cet-mark-time');
        const tag = r.querySelector('.cet-mark-preview-tag');
        const arrow = r.querySelector('.cet-mark-arrow');
        return {
          timeText: t ? t.textContent.trim() : null,
          timeTitle: t ? t.title : null,
          tagText: tag ? tag.textContent.trim() : null,
          tagTitle: tag ? tag.title : null,
          hasArrow: !!arrow,
          innerText: r.innerText.replace(/\\s+/g, ' ').trim(),
        };
    }"""
    )
    assert row, "没有渲染出标记行"
    assert row["timeText"].startswith("标记于 "), (
        f"「按键时刻」没有显式标注：{row['timeText']!r}（应形如「标记于 0:20.00」）"
    )
    assert "候选句" in row["tagText"], f"候选句没有标出：{row['tagText']!r}"
    assert "–" in row["tagText"], f"候选句应显示区间 start–end：{row['tagText']!r}"
    assert row["hasArrow"], "缺少「标记时刻 → 候选句」的箭头分隔"
    # 两个时间值必须是**不同**的数（否则用户仍会以为标错）
    pressed = row["timeText"].replace("标记于 ", "").strip()
    span = row["tagText"].replace("候选句 ", "").strip()
    assert pressed not in span, (
        f"标记时刻({pressed}) 出现在候选句区间({span}) 里 —— 两段口径没有区分开"
    )


# ══════════════════════════════════════════════════════════════
# A-5：删除按钮在「本轮已练」态提前禁用（真实全链路）
# ══════════════════════════════════════════════════════════════


def _require_in_practice_field(page, material_id: str, session_id: str) -> None:
    """确认后端已透出 inPractice 字段（需要 backend >= b1c51cd）。

    本包只改前端；如果被验证的服务端还是旧后端（没有 inPractice），
    前端按约定「null 视为可点」，A5 的行为无法被验证 —— 这种情况应 skip 并说明，
    而不是报红（那不是前端缺陷，是后端版本不匹配）。
    """
    data = _get_json(f"/api/cet/marks?materialId={material_id}&listeningSessionId={session_id}")
    items = data.get("items") or data.get("marks") or []
    if not items:
        return
    if not any("inPractice" in i for i in items):
        pytest.skip(
            "服务端 GET /api/cet/marks 未透出 inPractice 字段（需要 backend >= b1c51cd）。"
            "A5 的判定口径依赖该字段，旧后端下无法验证。"
        )


def _walk_to_practice_active(page) -> bool:
    """交卷 → 确认句 → 开始带练，停在 **active**（已开始带练、还没练完）。

    这是缺陷5 的真正靶子：B 实测这个状态下 markState() 返回 'ready'，
    按 markState() 判定的旧修法仍会让删除按钮可点，用户点下去才收到 409。
    返回 True 表示已进入 active；False 表示前置条件不满足（调用方 skip）。
    """
    page.wait_for_function("() => window.__cet.S.questions.length > 0", timeout=15000)
    page.evaluate(
        """() => {
        const S = window.__cet.S;
        S.questions.forEach((q) => {
          if (!S.answers[q.questionId]) {
            const opt = (q.options && q.options[0]) || {};
            S.answers[q.questionId] = opt.key || opt.id || 'A';
          }
        });
    }"""
    )
    page.evaluate("() => window.__cet.submitAttempt()")
    page.wait_for_timeout(1500)
    if not page.evaluate("() => !!window.__cet.S.attempt"):
        return False

    page.evaluate(
        """() => {
        const S = window.__cet.S;
        const m = S.marks[0];
        if (!m) return null;
        return window.__cet.confirmMark(m, window.__cet.previewSentenceId(m));
    }"""
    )
    page.wait_for_timeout(1500)

    # 真实点击「开始带练」
    clicked = page.evaluate(
        """() => {
        for (const r of document.querySelectorAll('.cet-mark')) {
          const b = Array.from(r.querySelectorAll('button'))
            .find(x => x.textContent.trim() === '开始带练');
          if (b) { b.click(); return true; }
        }
        return false;
    }"""
    )
    if not clicked:
        return False
    page.wait_for_timeout(2500)
    return page.evaluate("() => window.__cet.S.practice ? window.__cet.S.practice.status : null") == "active"


def test_a5_delete_disabled_when_practice_active(page, timed_lesson):
    """**已开始带练但未完成（active）**：删除按钮必须已禁用。

    这是缺陷5 的核心场景（B 的实测发现）：
        practice 201 status='active'  ← 后端此后必定 409 MARK_IN_PRACTICE
        但 markState() 只把 status='complete' 认作 done → 返回 'ready' → 按钮可点
    改前实测：inPractice=True 但 delDisabled=False、title="删除这个标记"。
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)
    page.evaluate(f"{AUDIO}.currentTime = 22.0; {AUDIO}.play()")
    page.wait_for_timeout(700)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1500)
    if not _marks(page):
        pytest.skip("没有建立标记，无法验证删除按钮状态")

    _require_in_practice_field(
        page,
        page.evaluate("() => window.__cet.S.materialId"),
        page.evaluate("() => localStorage.getItem('cet.session.v1')"),
    )

    if not _walk_to_practice_active(page):
        pytest.skip("未能走到「已开始带练（active）」状态")

    state = page.evaluate(
        """() => {
        const S = window.__cet.S;
        const m = S.marks[0];
        const r = document.querySelector('.cet-mark');
        const b = r ? Array.from(r.querySelectorAll('button'))
          .find(x => x.textContent.trim() === '删除') : null;
        const badge = r ? r.querySelector('.cet-mark-badge') : null;
        return {
          practiceStatus: S.practice ? S.practice.status : null,
          inPractice: m ? m.inPractice : 'NO_FIELD',
          badge: badge ? badge.textContent.trim() : null,
          disabled: b ? b.disabled : null,
          title: b ? b.title : null,
          ariaDisabled: b ? b.getAttribute('aria-disabled') : null,
          blocked: b ? b.getAttribute('data-cet-mark-delete-blocked') : null,
        };
    }"""
    )
    # 先确认我们真的在 active 态（不是 complete —— 那才是旧修法唯一覆盖的场景）
    assert state["practiceStatus"] == "active", f"应停在 active，实际 {state['practiceStatus']}"
    assert state["badge"] == "待练", (
        f"active 态下列表徽章应是「待练」（markState 返回 ready），实际 {state['badge']!r}"
        " —— 这正是旧修法漏掉的状态"
    )
    # 后端必须已透出 inPractice
    assert state["inPractice"] is True, (
        f"标记的 inPractice 应为 True（已被带练占用），实际 {state['inPractice']!r}"
    )
    # 核心断言：按钮已禁用
    assert state["disabled"] is True, (
        f"「已开始带练（active）」时删除按钮仍可点（disabled={state['disabled']}）—— "
        "缺陷5 未修好：用户点下去才会收到 409 MARK_IN_PRACTICE"
    )
    assert state["title"] == "该标记已开始带练，不能删除", (
        f"删除按钮 title 不对：{state['title']!r}"
    )
    assert state["ariaDisabled"] == "true", "缺少 aria-disabled"
    assert state["blocked"] == "1", "缺少 data-cet-mark-delete-blocked 标记"

    # 真实点击 disabled 按钮：不应发出 DELETE 请求
    page._reqs.clear()  # type: ignore[attr-defined]
    page.evaluate(
        """() => {
        const r = document.querySelector('.cet-mark');
        const b = Array.from(r.querySelectorAll('button'))
          .find(x => x.textContent.trim() === '删除');
        if (b) b.click();
    }"""
    )
    page.wait_for_timeout(800)
    deletes = [r for r in page._reqs if r.startswith("DELETE")]  # type: ignore[attr-defined]
    assert not deletes, f"点击被禁用的删除按钮仍发出了请求：{deletes}"


def test_a5_in_practice_matches_backend_truth(page, timed_lesson):
    """前端 inPractice 必须与后端 GET /marks 的真值一致（口径对账）。"""
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)
    page.evaluate(f"{AUDIO}.currentTime = 24.0; {AUDIO}.play()")
    page.wait_for_timeout(700)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1500)
    if not _marks(page):
        pytest.skip("没有建立标记")

    _require_in_practice_field(
        page,
        page.evaluate("() => window.__cet.S.materialId"),
        page.evaluate("() => localStorage.getItem('cet.session.v1')"),
    )

    if not _walk_to_practice_active(page):
        pytest.skip("未能走到 active 状态")

    mid = page.evaluate("() => window.__cet.S.materialId")
    sid = page.evaluate("() => localStorage.getItem('cet.session.v1')")
    db = _get_json(f"/api/cet/marks?materialId={mid}&listeningSessionId={sid}")
    items = db.get("items") or db.get("marks") or []
    assert items, "后端列表为空"
    for item in items:
        assert "inPractice" in item, (
            f"后端响应缺少 inPractice 字段：{sorted(item.keys())} —— "
            "前端无法提前禁用删除按钮（需要 backend >= b1c51cd）"
        )
    backend_truth = {i["markId"]: i["inPractice"] for i in items}
    front = {m["id"]: m.get("inPractice") for m in _marks(page)}
    for mark_id, flag in backend_truth.items():
        assert front.get(mark_id) == flag, (
            f"前端 inPractice({front.get(mark_id)}) 与后端({flag}) 不一致：{mark_id}"
        )
    # 至少有一条是被占用的，否则这条用例没测到东西
    assert any(backend_truth.values()), f"应至少有一条 inPractice=True，实际 {backend_truth}"


# ══════════════════════════════════════════════════════════════
# A-6：时间线刻度与列表同口径，点了落在刻度处
# ══════════════════════════════════════════════════════════════


def test_a6_timeline_tick_matches_list_sentence(page, timed_lesson):
    """时间线刻度秒数 == 该标记候选句起点（与列表口径一致）。

    改前实测：刻度 sec=5.084（pressedAtSec），列表说的是「第 0 句（0:00.01–0:03.49）」
    —— 同一个标记两个位置。
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)
    page.evaluate(f"{AUDIO}.currentTime = 26.0; {AUDIO}.play()")
    page.wait_for_timeout(700)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1800)

    ticks = page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-cet-mark-sec]')).map(t => ({
        sec: Number(t.getAttribute('data-cet-mark-sec')),
        sent: t.getAttribute('data-cet-mark-sentence'),
        pressed: Number(t.getAttribute('data-cet-mark-pressed')),
        title: t.getAttribute('title'),
        left: t.style.left,
    }))"""
    )
    assert ticks, "时间线上没有刻度"

    sentences = page.evaluate(
        "() => { const o = {}; (window.__cet.S.sentences || []).forEach(s => {"
        " o[s.sentenceId] = [s.startSeconds, s.endSeconds]; }); return o; }"
    )
    marks = _marks(page)
    for tick in ticks:
        sid = tick["sent"]
        assert sid in sentences, f"刻度指向不存在的句子：{sid}"
        assert abs(tick["sec"] - sentences[sid][0]) < 0.02, (
            f"刻度位置({tick['sec']}) 与候选句起点({sentences[sid][0]}) 不一致"
            f"（问题6 回归：刻度又用了按键时刻 {tick['pressed']}）"
        )
        # 刻度位置应等于候选句起点，而不是按键时刻（除非两者恰好相同）
        assert "候选句" in (tick["title"] or ""), f"刻度 title 没标出候选句：{tick['title']!r}"

    # 每个标记都能在刻度上找到对应（口径一一对应）
    assert len(ticks) == len(marks), (
        f"刻度数({len(ticks)}) 与标记数({len(marks)}) 不一致"
    )


def test_a6_clicking_tick_lands_on_candidate_start(page, timed_lesson):
    """真实点击刻度：播放位置落在该刻度对应处（不是按键时刻）。

    改前：刻度画在按键时刻、点下去却跳到候选句 —— 画的和跳的不是一处。
    """
    _open_listening(page, timed_lesson)
    _ensure_autoplay_off(page)
    page.evaluate(f"{AUDIO}.currentTime = 26.0; {AUDIO}.play()")
    page.wait_for_timeout(700)
    page.keyboard.press("KeyM")
    page.wait_for_timeout(1800)

    tick_info = page.evaluate(
        """() => {
        const t = document.querySelector('[data-cet-mark-sec]');
        if (!t) return null;
        return { sec: Number(t.getAttribute('data-cet-mark-sec')),
                 sent: t.getAttribute('data-cet-mark-sentence') };
    }"""
    )
    assert tick_info, "没有刻度可点"

    page.evaluate(f"{AUDIO}.pause()")
    page.click("[data-cet-mark-sec]")       # 真实点击
    # 点刻度会 replayRange(起点, 终点) —— 先 seek 再 play，所以位置会从起点往前跑。
    # 只等很短时间就采样，并把「合理播放推进」算进去（1x 倍速下 ≈ 实际等待时长）。
    page.wait_for_timeout(250)
    landed = _cur(page)

    # 判据：落点必须在候选句区间内（起点 ~ 起点+1s），而不是按键时刻附近。
    # 改前的症状是「画在按键时刻、点下去跳到候选句」—— 那样 landed 会远离 tick.sec。
    assert tick_info["sec"] <= landed <= tick_info["sec"] + 1.2, (
        f"点刻度后播放位置({landed}) 不在刻度位置({tick_info['sec']}) 起 1.2s 内 —— "
        "画的位置和跳的位置不是一处（问题6 回归）"
    )
    assert not page.evaluate(f"() => {AUDIO}.paused"), (
        "点刻度应开始回放候选句（replayRange 会 play）"
    )
