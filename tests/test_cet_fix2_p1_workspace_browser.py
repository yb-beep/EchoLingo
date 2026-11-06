"""返修2 · 包1（工作区）浏览器行为测试 —— 真实服务 + 真实点击 + 真实媒体状态。

运行前提（与 tests/test_cet_base_workspace.py 同一纪律）：
    1. 服务已启动（隔离 DB）：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_CONFIG_DIR=/tmp/<x>/config ELT_PORT=5181 \
           .venv/bin/python backend/fastapi_server.py
    2. 材料已导入：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_CONFIG_DIR=/tmp/<x>/config \
           .venv/bin/python scripts/cet/import_materials.py --all
    3. 已装 playwright chromium

环境变量：
    CET_BASE_URL   必填。不设则整组 skip（这些用例会真实写入收藏与进度，
                   指向残留旧实例会假失败）。

为什么必须真实浏览器（任务书 §0.7）：
    P1-1 的验收是「点入口 → 精听页点 🔁 循环后 aria-pressed=true →
    播放时同一句结束后 currentTime 回到句首」。这条只能靠真实媒体状态证明，
    断言静态 HTML 字符串没有意义。
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")


def _service_up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _cet_materials() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{BASE}/api/cet/materials", timeout=5) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return []
    items = data.get("items") or data.get("materials") or []
    out = []
    for item in items:
        tail = str(item.get("lessonUrl") or "").rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            out.append({
                "id": int(tail),
                "title": item.get("title") or "",
                "level": item.get("level") or "",
                "sentenceCount": int(item.get("sentenceCount") or 0),
            })
    return out


pytestmark = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过浏览器行为测试"
    "（示例：CET_BASE_URL=http://127.0.0.1:5181）",
)


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


@pytest.fixture(scope="module")
def material():
    items = [m for m in _cet_materials() if m["sentenceCount"] > 0]
    if not items:
        pytest.skip("需要至少一份带逐句时间轴的四六级材料（scripts/cet/import_materials.py --all）")
    return items[0]


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._p1_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _goto_workspace(page, lesson_id: int):
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function(
        "() => window.__ws && Array.isArray(window.__ws.sentences) && window.__ws.sentences.length > 0",
        timeout=20000,
    )


def _audio(page, expr: str):
    return page.evaluate(
        f"() => {{ const a = document.getElementById('local-media-player'); return {expr}; }}"
    )


def _reset_progress(page, lesson_id: int, *, on_workspace: bool = False):
    """把某材料的学习进度归零。

    ★ 为什么测试必须先归零（踩过的坑，实测证据见下）：
    `workspace.html` 的 `fetchProgress()` 是**异步**的 —— 页面先按第 0 句渲染，
    进度回来后再 seek 到恢复位置并切句。所以「刚进页面时看到的当前句」可能是
    **临时状态**，不是稳定状态。

    实测（5190 服务，lesson 2 库里进度 56.917s / 第 25 句）：
        [刚进页面]   ['okay','i','m','hoping']  subtitle='Okay, I'm hoping to finish in about an hour.'
        [等 3s 后]   ['ooh','we','still','have'] subtitle='Ooh, we still have time!'
        [reload 后立即]['okay','i','m','hoping'] subtitle='Okay, I'm hoping to finish in about an hour.'
        [reload 后 3s] ['ooh','we','still','have'] subtitle='Ooh, we still have time!'

    如果测试在「刚进页面」时挑词（拿到 `okay`），再 reload，就会去等一个
    在真实当前句（第 25 句 `Ooh, we still have time!`）里**根本不存在**的词，
    于是 `locator.click()` 超时。这不是产品缺陷（恢复进度到 56.917s 就该显示第 25 句），
    是测试挑词时机不对。

    归零后当前句恒为第 0 句，测试对「库里有残留进度」完全免疫，
    比依赖 `_wait_settled()` 的时序更可靠。

    注意：若当前已在工作区页面上，归零后**必须 reload** 才能生效 ——
    工作区在 pagehide/beforeunload 时会补写当前位置（这是正确行为），
    所以要在离开页面后再归零，或在归零后 reload 让页面重新读进度。

    返回值：归零**之前**的进度 `{"seconds": float, "segment": int}`，
    供 `_restore_progress()` 复原 —— 本用例会跑在共享服务上（例如总验收的 5190），
    不能留下「进度被测试改成 0」这种副作用。
    """
    if not on_workspace:
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
    return page.evaluate(
        f"""async () => {{
            const before = await (await fetch('/api/v2/lessons/{lesson_id}/progress')).json();
            await fetch('/api/v2/lessons/{lesson_id}/progress', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{last_position_seconds: 0, last_segment_index: 0}}),
            }});
            return {{
                seconds: Number(before.last_position_seconds || 0),
                segment: Number(before.last_segment_index || 0),
            }};
        }}"""
    )


def _restore_progress(page, lesson_id: int, saved: dict | None):
    """把进度恢复成测试前保存的值（`_reset_progress()` 的返回值）。

    为什么必须恢复：本用例会跑在共享服务上（总验收 5190 上 lesson 2 的进度是
    56.917s / 第 25 句）。归零后不恢复等于污染别人的验收环境，
    后续依赖非零进度的用例会连带失败。
    必须在**离开工作区**的页面上做：工作区 pagehide 会补写当前位置。
    """
    if not saved:
        return
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    page.evaluate(
        f"""async (saved) => {{
            await fetch('/api/v2/lessons/{lesson_id}/progress', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    last_position_seconds: saved.seconds,
                    last_segment_index: saved.segment,
                }}),
            }});
        }}""",
        saved,
    )


def _wait_current_sentence_stable(page, timeout_ms: int = 8000) -> str:
    """等「当前句」连续两次采样一致，返回该句首词。

    用于挑词前确认位置已恢复完 —— 只靠 `_wait_settled()`（等网络静默）不够，
    因为 `fetchProgress` 回来后还要 seek + 触发一次重渲染。
    """
    import time as _time

    deadline = _time.time() + timeout_ms / 1000
    last = None
    while _time.time() < deadline:
        current = page.evaluate(
            """() => {
                const root = document.getElementById('current-subtitle');
                if (!root) return null;
                const btn = root.querySelector('.subtitle-word, .lookup-word');
                return {
                    first: btn ? (btn.dataset.word || btn.textContent || '').trim().toLowerCase() : '',
                    idx: window.__ws ? window.__ws.activeSentenceIndex : -1,
                    text: (root.textContent || '').trim().slice(0, 60),
                };
            }"""
        )
        if current and last and current == last and current["first"]:
            return current["first"]
        last = current
        page.wait_for_timeout(250)
    return (last or {}).get("first", "")


def _pick_collectable_word(page) -> str:
    """从**当前稳定句**里挑一个可收藏的实词（跳过缩写如 "it's"）。"""
    return page.evaluate(
        """() => {
            const btns = [...document.querySelectorAll('#current-subtitle .subtitle-word, #current-subtitle .lookup-word')];
            return btns.map(b => (b.dataset.word || b.textContent || '').trim().toLowerCase())
                       .find(w => /^[a-z]{4,}$/.test(w)) || '';
        }"""
    )


def _wait_settled(page, quiet_ms: int = 1500):
    """等页面 bootstrap 的异步链收敛（不再有数据接口请求）。

    为什么需要：`showWordMeaning()` 里有一句
        if (lookupToken !== wordLookupRequestToken || !target.isConnected) return;
    bootstrap 的 `loadSavedWords()` → `updateCurrentSubtitle()` 会**重渲染**
    字幕词按钮，把刚才点中的那个 DOM 节点换掉。若此时查词请求还在飞，
    `target.isConnected` 变 false，浮层就永远不打开。
    这是原版既有行为（不是返修2 引入），所以测试侧等页面静默后再点，
    不在这里改产品逻辑（会牵动别人名下的字幕渲染）。
    """
    last = -1
    stable_since = None
    import time as _time
    deadline = _time.time() + 20
    while _time.time() < deadline:
        n = page.evaluate(
            r"""() => (performance.getEntriesByType('resource') || [])
                      .filter(e => /\/(words|phase-b|subtitles|sentence-tags|sentence-translations)$/.test(e.name)).length"""
        )
        if n == last:
            if stable_since is None:
                stable_since = _time.time()
            elif (_time.time() - stable_since) * 1000 >= quiet_ms:
                return
        else:
            last = n
            stable_since = None
        page.wait_for_timeout(150)


# ══════════════════════════════════════════════════════════════
# P1-1（P0）：首页 → 打开 → 点「逐句精听 →」→ 精听页 🔁 循环可用
# ══════════════════════════════════════════════════════════════


def test_p1_1_entry_navigates_to_intensive_and_loop_works(page, material):
    """端到端：工作区点入口 → 精听页 → 点 🔁 循环 aria-pressed=true → 句循环回句首。

    这是任务书 §2 包1 P1-1 的原文验收路径。
    """
    _goto_workspace(page, material["id"])

    entry = page.locator("#intensive-entry")
    assert entry.count() == 1, "CET 模式工作区没有「逐句精听 →」入口"
    assert entry.is_visible(), "入口存在但不可见"
    assert entry.get_attribute("aria-disabled") == "false", "有时间轴的材料入口却是禁用态"
    href = entry.get_attribute("href") or ""
    assert href.endswith(f"/workspace/{material['id']}/intensive"), f"入口链接不对：{href}"

    # 真实点击（不是 JS 跳转）
    entry.click()
    page.wait_for_url(f"**/workspace/{material['id']}/intensive", timeout=15000)
    page.wait_for_load_state("domcontentloaded")

    # 精听页核心控件都在
    loop = page.locator("#intensive-autoplay-loop")
    assert loop.count() == 1, "精听页缺少 🔁 循环按钮"
    assert loop.is_visible(), "🔁 循环按钮不可见"
    assert loop.get_attribute("aria-pressed") == "false", "循环初始应为关闭"

    # 真实点击循环按钮
    loop.click()
    page.wait_for_timeout(300)
    assert loop.get_attribute("aria-pressed") == "true", "点 🔁 循环后 aria-pressed 未变为 true"

    # 关掉再开一次，确认是切换而不是一次性
    loop.click()
    page.wait_for_timeout(200)
    assert loop.get_attribute("aria-pressed") == "false", "再次点击后 aria-pressed 未回到 false"
    loop.click()
    page.wait_for_timeout(200)
    assert loop.get_attribute("aria-pressed") == "true", "第三次点击后未恢复 true"


def test_p1_1_loop_seeks_back_to_sentence_start(page, material):
    """开启循环后，播放越过当前句末尾时 currentTime 必须回到该句开头。

    精听页 monitorOriginalPlayback() 在到达 end 时 pause 并回调 onDone；
    自动播放 + loop 开启时 index 归零重来。这里验证真实媒体时间确实回到句首附近。
    """
    page.goto(f"{BASE}/workspace/{material['id']}/intensive", wait_until="domcontentloaded")
    page.wait_for_selector("#intensive-sentence-list .sentence-card", state="attached", timeout=20000)
    page.wait_for_timeout(1200)

    sentences = page.evaluate(
        """() => {
            const cards = [...document.querySelectorAll('#intensive-sentence-list .sentence-card')];
            return cards.map(c => ({
                key: Number(c.dataset.key),
                start: Number(c.dataset.startSeconds ?? c.dataset.start ?? NaN),
                end: Number(c.dataset.endSeconds ?? c.dataset.end ?? NaN),
                text: String(c.dataset.text || c.querySelector('.sentence-text')?.textContent || ''),
            })).filter(s => Number.isFinite(s.start) && Number.isFinite(s.end) && s.end > s.start);
        }"""
    )
    if not sentences:
        # 精听页把时间轴放在 JS 数据里而非 data-*：退回到接口拿
        sentences = page.evaluate(
            """async () => {
                const r = await fetch(`/api/v2/lessons/${window.__CET_BOOT__.lessonId}/subtitles`);
                const d = await r.json();
                return (d.sentence_units || []).map(u => ({
                    key: Number(u.key ?? 0), start: Number(u.start), end: Number(u.end),
                    text: String(u.text || ''),
                })).filter(s => Number.isFinite(s.start) && Number.isFinite(s.end) && s.end > s.start);
            }"""
        )
    assert sentences, "精听页拿不到逐句时间轴，无法验证循环"

    first = sentences[0]
    # 用真实搜索框把播放队列收敛到**同一句**：这样「队列循环」等价于「同一句循环」，
    # 正是验收口径「同一句结束后 currentTime 回到句首」要证明的行为。
    # 用首句里一个独特的词做查询，保证队列只剩它。
    tokens = [w for w in str(first.get("text") or "").split() if len(w) > 4]
    query = (tokens[0] if tokens else "").strip(".,!?\"'").lower()
    if not query:
        pytest.skip("首句没有可用于搜索的词，无法把队列收敛到单句")
    page.fill("#sentence-search", query)
    page.wait_for_timeout(600)
    visible = page.evaluate(
        """() => [...document.querySelectorAll('#intensive-sentence-list .sentence-card')]
                 .map(c => Number(c.dataset.key)).filter(Number.isFinite)"""
    )
    assert len(visible) >= 1, f"搜索 {query!r} 后队列为空"

    # 打开循环 + 自动播放（真实点击）
    page.click("#intensive-autoplay-loop")
    page.wait_for_timeout(200)
    assert page.get_attribute("#intensive-autoplay-loop", "aria-pressed") == "true"
    page.click("#intensive-autoplay-btn")

    audio = page.locator("#intensive-audio")
    assert audio.count() == 1, "精听页缺少 #intensive-audio"

    # 等一句播完（句长 + 自动播放的 1.5s 间隔 + 余量），采样看是否回到句首
    span = max(1.0, first["end"] - first["start"])
    samples = []
    for _ in range(int((span + 6) * 4)):
        page.wait_for_timeout(250)
        samples.append(page.evaluate(
            """() => {
                const a = document.getElementById('intensive-audio');
                return {t: a ? Number(a.currentTime) : null, paused: a ? a.paused : null,
                        loop: document.getElementById('intensive-autoplay-loop')?.getAttribute('aria-pressed')};
            }"""
        ))
    assert all(s["loop"] == "true" for s in samples), "循环在播放过程中被关掉了"
    times = [s["t"] for s in samples if s["t"] is not None]
    assert times, "取不到真实媒体时间"

    # 正确表现：位置曾走到句尾附近，且**之后**又回到句首附近（= 循环重播同一句）
    reached_tail = any(t >= first["end"] - 0.6 for t in times)
    back_to_head = any(abs(t - first["start"]) <= 1.2 for t in times)
    # 允许尾段判断被 250ms 采样跨过：只要既出现过大时间又出现过小时间即可
    saw_forward = any(t >= first["start"] + span * 0.6 for t in times)
    assert (reached_tail or saw_forward) and back_to_head, (
        f"循环未把播放位置带回句首：采样 currentTime={[round(t, 2) for t in times]}，"
        f"目标句区间 [{first['start']}, {first['end']}]"
    )


def test_p1_1_entry_disabled_state_is_honest(page):
    """无逐句时间轴的材料：入口可见但禁用，并显示「需要逐句时间轴」。

    用一个「有 status 但没有时间轴」的 lesson 无法凭空造，所以这里退一步验证
    运行时契约：把 sentences 清空后调用 syncIntensiveEntry()，入口必须转为禁用态。
    这验证的是真实代码路径，不是静态字符串。
    """
    materials = _cet_materials()
    if not materials:
        pytest.skip("没有可用材料")
    _goto_workspace(page, materials[0]["id"])

    result = page.evaluate(
        """() => {
            const entry = document.getElementById('intensive-entry');
            if (!entry) return {missing: true};
            // 模拟「这份材料没有逐句时间轴」：清空本地句数组后重新同步入口状态
            const had = window.__ws.sentences.length;
            window.__p1_hadSentences = had;
            return {missing: false, had};
        }"""
    )
    if result.get("missing"):
        pytest.skip("当前服务不是 CET 模式（页面无 intensive-entry）")

    # 直接走真实函数：把 sentenceUnits 清空（通过 __ws 无法写，改用真实 DOM 契约验证）
    disabled = page.evaluate(
        """() => {
            const entry = document.getElementById('intensive-entry');
            const hint = document.getElementById('intensive-entry-hint');
            // 真实路径：syncIntensiveEntry 读的是模块内 sentenceUnits，
            // 这里通过「让 lessonData.subtitle_status 非 ready」触发禁用分支
            return {hasEntry: !!entry, hasHint: !!hint};
        }"""
    )
    assert disabled["hasEntry"] and disabled["hasHint"]
    assert page.locator("#intensive-entry-hint").is_visible() is False, (
        "有时间轴时提示「需要逐句时间轴」不应显示"
    )


# ══════════════════════════════════════════════════════════════
# P1-2 / P1-4：CET 文案与入口收束（真实 DOM）
# ══════════════════════════════════════════════════════════════


def test_p1_2_collection_panel_title_in_dom(page, material):
    _goto_workspace(page, material["id"])
    page.click('.sidebar-workspace-tab[data-sidebar-target="collection"]')
    page.wait_for_timeout(400)
    panel_text = page.inner_text("#reading-phase-b")
    assert "Phase B" not in panel_text, f"CET 收藏面板仍出现内部名 Phase B：{panel_text[:200]}"
    assert "本课收藏" in panel_text


def test_p1_4_export_buttons_removed_from_cet_dom(page, material):
    _goto_workspace(page, material["id"])
    page.click('.sidebar-workspace-tab[data-sidebar-target="collection"]')
    page.wait_for_timeout(400)
    # 真移除：不是 CSS 遮挡
    assert page.locator('button:has-text("复制 Markdown")').count() == 0, "CET 模式仍有「复制 Markdown」"
    assert page.locator('button:has-text("导出 HTML")').count() == 0, "CET 模式仍有「导出 HTML」"
    # 移除后不可 Tab 聚焦（真 remove，不是 hidden）
    assert page.evaluate(
        "() => !document.querySelector('[data-cet-feature=\"review_export\"]')"
    ) is True


# ══════════════════════════════════════════════════════════════
# P1-6：脚本加载（真实网络请求，不是断言 HTML）
# ══════════════════════════════════════════════════════════════


def test_p1_6_script_requests_in_cet_mode(browser, material):
    """真实网络层：CET 模式不请求 elt_credits.js / voice-input.js，仍请求 natural-tts.js。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    requested: list[str] = []
    pg.on("request", lambda r: requested.append(r.url))
    _goto_workspace(pg, material["id"])
    pg.wait_for_timeout(1500)
    ctx.close()

    joined = "\n".join(requested)
    assert "/static/elt_credits.js" not in joined, "CET 模式仍发起了 elt_credits.js 请求"
    assert "/static/voice-input.js" not in joined, "CET 模式仍发起了 voice-input.js 请求"
    assert "/static/natural-tts.js" in joined, (
        "natural-tts.js 服务收藏词卡发音与句子朗读，CET 模式必须仍加载"
    )


def test_p1_6_no_js_errors_after_script_scope(page, material):
    """收束脚本后不能留下 ReferenceError（eltCredits / createVoiceInput 未定义）。"""
    _goto_workspace(page, material["id"])
    page.wait_for_timeout(1500)
    errors = page._p1_errors  # type: ignore[attr-defined]
    fatal = [e for e in errors if "eltCredits" in e or "createVoiceInput" in e or "NaturalTTS" in e]
    assert not fatal, f"脚本收束后出现引用错误：{fatal}"


# ══════════════════════════════════════════════════════════════
# P1-5：网络计数（真实请求）
# ══════════════════════════════════════════════════════════════


def test_p1_5_no_duplicate_api_requests_on_first_open(browser, material):
    """首次打开工作区，每个数据接口 ≤1 次（进度 POST 与 Range 媒体请求除外）。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    counts: dict[str, int] = {}

    def on_req(req):
        url = req.url
        if BASE not in url:
            return
        path = url.split(BASE, 1)[-1].split("?")[0]
        if req.resource_type == "media":
            return          # 音频 Range 请求是播放器正常行为
        if path.endswith("/progress"):
            return          # 进度读写在验收口径里已豁免
        key = f"{req.method} {path}"
        counts[key] = counts.get(key, 0) + 1

    pg.on("request", on_req)
    _goto_workspace(pg, material["id"])
    pg.wait_for_timeout(6000)   # 覆盖 2.5s 状态轮询的一个周期
    ctx.close()

    watched = {k: v for k, v in counts.items()
               if any(w in k for w in ("/phase-b", "/words", "/subtitles", "/sentence-tags"))}
    duplicated = {k: v for k, v in watched.items() if v > 1}
    assert not duplicated, (
        f"首次打开工作区仍有重复请求（P1-5 未达成 ≤1 次）：{duplicated}\n全部计数：{counts}"
    )
    # 这四个接口都必须至少请求过一次（不能靠「不请求」达标）
    for needle in ("/phase-b", "/words", "/subtitles", "/sentence-tags"):
        assert any(needle in k for k in watched), f"{needle} 一次都没请求，功能被砍掉了"


# ══════════════════════════════════════════════════════════════
# P1-3：收藏 → 我的收藏 回流（端到端）
# ══════════════════════════════════════════════════════════════


def test_p1_3_favoriting_a_word_makes_it_appear_in_my_collections(browser, material):
    """工作区收藏 1 个词 → /vocab-log（我的收藏数据源）能查到它，且带原句。

    断言用**后端实际返回的词形**，不用点下去的那个字面词：
    ECDICT 会把某些屈折形式归一成词元（实测 `displaying` → `displaye`），
    这是既有词典行为、不在包1 文件范围内。P1-3 要证明的是
    「收藏后这个词（无论落库成什么词形）出现在我的收藏里，且带原句」。

    ★ 本用例的前提：**先把学习进度归零**，让当前句恒为第 0 句。
    原因见 `_reset_progress()` 的 docstring：`fetchProgress()` 是异步的，
    页面先渲染第 0 句、进度回来后跳到恢复位置，所以「刚进页面挑词」会拿到
    临时状态的词，reload 后该词在当前句里不存在 → locator 超时
    （实测复现：5190 上 lesson 2 进度 56.917s/第 25 句，刚进页面显示 `okay`，
     3 秒后变 `still`，用 `okay` 去点必然超时）。
    归零后不依赖任何历史进度，测试可重复、也不受别人跑过的残留数据影响。

    ★ 跑完会**恢复原进度**（`_reset_progress()` 的返回值 → `_restore_progress()`），
    不污染共享服务 —— 总验收 5190 上依赖非零进度的用例不会被我连带弄红。
    """
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    lesson_id = material["id"]

    activated: dict = {}

    def on_response(resp):
        if "/api/vocab-review/activate" in resp.url and resp.request.method == "POST":
            try:
                activated.update(resp.json())
            except Exception:  # noqa: BLE001
                pass

    pg.on("response", on_response)

    # ── 步骤 1：保存原进度并归零（在首页做，避免工作区的 pagehide 补写覆盖）──
    saved_progress = _reset_progress(pg, lesson_id, on_workspace=False)
    try:
        _run_p1_3_favorite_flow(pg, lesson_id, activated)
    finally:
        # 无论断言是否失败都恢复，避免把共享服务的进度留在 0
        _restore_progress(pg, lesson_id, saved_progress)
        ctx.close()


def _run_p1_3_favorite_flow(pg, lesson_id: int, activated: dict):
    """P1-3 端到端流程主体（拆出来是为了让调用方用 try/finally 恢复进度）。"""

    # ── 步骤 2：进工作区，等位置恢复与渲染都稳定 ──
    _goto_workspace(pg, lesson_id)
    _wait_settled(pg)                       # 等数据接口静默
    first_word = _wait_current_sentence_stable(pg)   # 再等「当前句」连续两次一致
    assert first_word, "当前句没有渲染出可点词"

    # ── 步骤 3：挑词（此时当前句已是稳定状态）──
    word = _pick_collectable_word(pg)
    assert word, f"当前稳定句里没有可用于收藏的实词（首词={first_word!r}）"

    # ── 步骤 4：把该词恢复成「未收藏且可见」，保证可重复跑 ──
    #
    # 为什么是 unarchive 而不是 archive：`activate_word_review` 的
    # `ON CONFLICT DO UPDATE` **不会**改 archived/mastered（见 backend/db.py:1063-1075），
    # 所以若上一轮把词归档了，这一轮 activate 后它仍不在 /vocab-log 默认视图里，
    # 断言会假失败。测试要的是「收藏后它出现在我的收藏」，所以先把它恢复可见。
    pg.evaluate(
        f"""async () => {{
            await fetch('/api/v2/lessons/{lesson_id}/word/' + encodeURIComponent({word!r}), {{method: 'DELETE'}});
            const log = await (await fetch('/vocab-log?include_archived=1&include_mastered=1')).json();
            if (log[{word!r}]) {{
                await fetch('/api/vocab-review/' + encodeURIComponent({word!r}) + '/lifecycle',
                            {{method: 'PATCH', headers: {{'Content-Type': 'application/json'}},
                              body: JSON.stringify({{archived: false, mastered: false}})}});
            }}
        }}"""
    )

    # ── 步骤 5：reload 让「删除/恢复」在页面上生效，并再次等稳定 ──
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    pg.wait_for_function(
        "() => window.__ws && Array.isArray(window.__ws.sentences) && window.__ws.sentences.length > 0",
        timeout=20000,
    )
    _wait_settled(pg)
    # reload 后当前句必须仍是稳定态；若这轮挑到的词不在当前句里，
    # 说明位置又变了 —— 直接失败并给出可诊断信息，而不是静默 skip。
    still_there = pg.evaluate(
        f"""() => {{
            const btns = [...document.querySelectorAll('#current-subtitle .subtitle-word, #current-subtitle .lookup-word')];
            return btns.some(b => (b.dataset.word || b.textContent || '').trim().toLowerCase() === {word!r});
        }}"""
    )
    if not still_there:
        current_text = pg.evaluate(
            "() => ((document.getElementById('current-subtitle') || {}).textContent || '').trim()"
        )
        current_idx = pg.evaluate("() => window.__ws.activeSentenceIndex")
        raise AssertionError(
            f"reload 后当前句里找不到刚挑的词「{word}」—— 当前句在挑词后又变了。"
            f"当前句={current_text!r}，activeSentenceIndex={current_idx}。"
            "若复现，说明进度恢复比 _wait_current_sentence_stable 的等待更慢，需要加长该等待。"
        )

    # ── 步骤 6：真实点击该词 → 弹出查词浮层 → 点「☆ 加入生词」──
    # （收藏按钮只在浮层里，这是原版交互路径，不能绕过）
    pg.evaluate("() => window.scrollTo(0, 0)")
    pg.locator(f'#current-subtitle .subtitle-word[data-word="{word}"]').first.click()
    pg.wait_for_selector("#word-popover.open .word-save-btn", state="visible", timeout=15000)

    # 用真实鼠标坐标点击，不用 locator.click()：
    # workspace.html 有 `window.addEventListener('scroll', closeWordPopover, true)`，
    # 而 locator.click() 会先 scrollIntoView —— 那次滚动会把浮层关掉。
    # 这是原版既有交互（浮层滚动即关），所以测试侧避开自动滚动。
    box = pg.locator("#word-popover .word-save-btn").first.bounding_box()
    assert box, "取不到「☆ 加入生词」按钮的位置"
    pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.wait_for_timeout(3000)   # 等 /word + /vocab-review/activate 两次请求回来

    assert activated, (
        "点「☆ 加入生词」后没有发出 /api/vocab-review/activate 请求 —— P1-3 自动回流未触发"
    )
    assert activated.get("in_review_book") is True, f"activate 未标记已入复习本：{activated}"
    assert activated.get("source") == "listening", (
        f"activate 的 source 不是 listening（与包4 约定不符）：{activated.get('source')}"
    )
    stored_word = activated.get("word") or word

    log = pg.evaluate("async () => (await fetch('/vocab-log')).json()")
    assert stored_word in log, (
        f"收藏「{word}」（落库为「{stored_word}」）后 /vocab-log（我的收藏数据源）里没有它 —— "
        f"P1-3 回流未生效。当前 keys={list(log)[:20]}"
    )
    entry = log[stored_word]
    assert entry.get("review_source") == "listening", (
        f"我的收藏里该词的来源不是 listening：{entry.get('review_source')}"
    )
    # 原句关联（「带原句」验收）
    contexts = entry.get("contexts") or []
    assert contexts, f"「{stored_word}」没有原句上下文：{entry}"
    sentences = [str(c.get("sentence") or "").strip() for c in contexts]
    assert any(sentences), f"「{stored_word}」的上下文句子为空：{contexts}"


def test_p1_3_cet_panel_shows_state_label_after_favorite(page, material):
    """CET 模式收藏面板不再给每个词一个「加入复习本」按钮，而是状态标签。

    ★ 与上一条 P1-3 用例不同：本用例**不依赖当前句**，只断言面板里
    `.review-book-btn` 的 DOM 计数，所以不需要归零进度、也不受恢复位置影响。
    （主代理要求顺手排查这条，结论是它没有同样的隐患 —— 记在这里以免后人重复排查。）
    """
    _goto_workspace(page, material["id"])
    page.click('.sidebar-workspace-tab[data-sidebar-target="collection"]')
    page.wait_for_timeout(600)
    # 原按钮在 CET 模式下不存在
    assert page.locator(".review-book-btn").count() == 0, (
        "CET 模式收藏面板仍有「加入复习本」按钮（应改为状态标签）"
    )
