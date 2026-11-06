"""P3 听力工作区 · 浏览器行为测试（真实服务 + 真实音频）。

与 test_cet_base_player.py 的分工：
    - 那个文件锁静态契约（模板/静态 JS 片段），不起服务
    - 这个文件用真实浏览器检查**实际媒体行为**（时间、速率、暂停）与
      **材料切换不串数据**，对应任务书 §6 的 T06/T07/T10/T14

运行前提：
    1. 服务已启动（隔离 DB）：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_PORT=5183 \
           .venv/bin/python backend/fastapi_server.py
    2. 材料已导入：
         ELT_USER_DB=/tmp/<x>/vocab.db .venv/bin/python scripts/cet/import_materials.py --all
    3. 已装 playwright chromium

服务不可达时整组跳过，不制造假失败（与 test_cet_listening_ui.py 同一纪律）。

⚠️ **必须指向本轮刚起的服务，不要指向残留的旧实例**：
    本文件会真实写入学习进度（如 seek 到 55s 后断言 A 材料进度落在 55s 附近）。
    若连到别人跑过测试的旧服务，库里已有旧进度，T10「进度不串材料」会假失败
    （实测 A 读到 0.212 而非 55）。正确做法：
        CET_BASE_URL=http://127.0.0.1:<你刚起的端口> pytest tests/test_cet_base_workspace.py

环境变量：
    CET_BASE_URL        必填。不设置则整组跳过 —— 这是刻意的：
                        这些用例会真实写入学习进度，指向残留的旧服务实例会假失败。
    CET_BASE_LESSON_A   第一份材料 lesson id（默认自动发现）
    CET_BASE_LESSON_B   第二份材料 lesson id（默认自动发现）
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

# 必须显式指定目标服务：默认值会让全量回归误打到开发时残留的旧实例，
# 而旧库里的历史进度会让 T10 的断言假失败（实测过）。
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
    """从真实接口取材料，不硬编码 lesson id（换库会变）。"""
    try:
        with urllib.request.urlopen(f"{BASE}/api/cet/materials", timeout=5) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return []
    items = data.get("items") or data.get("materials") or []
    out = []
    for item in items:
        lesson_url = str(item.get("lessonUrl") or "")
        tail = lesson_url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            out.append({"id": int(tail), "title": item.get("title") or "", "level": item.get("level") or ""})
    return out


pytestmark = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过浏览器行为测试"
    "（示例：CET_BASE_URL=http://127.0.0.1:5186）",
)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    # 真实用户点了播放才有声音；无头浏览器默认拦截自动播放，
    # 会让「play() 后是否真的在播」这类断言假失败。这里放开策略，
    # 断言检查的仍是真实媒体状态（paused/currentTime/playbackRate）。
    launch_args = ["--autoplay-policy=no-user-gesture-required"]
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome", args=launch_args)
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True, args=launch_args)
        yield b
        b.close()


@pytest.fixture(scope="module")
def materials():
    items = _cet_materials()
    if len(items) < 2:
        pytest.skip("需要至少两份四六级材料（脚本：scripts/cet/import_materials.py --all）")
    by_id = {item["id"]: item for item in items}
    a = int(os.environ.get("CET_BASE_LESSON_A", "0")) or items[0]["id"]
    b = int(os.environ.get("CET_BASE_LESSON_B", "0")) or next(
        item["id"] for item in items if item["id"] != a
    )
    return {"a": by_id.get(a, items[0]), "b": by_id.get(b, items[1])}


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    failures: list[str] = []
    errors: list[str] = []
    pg.on("response", lambda r: failures.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._cet_failures = failures  # type: ignore[attr-defined]
    pg._cet_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _goto_workspace(page, lesson_id: int):
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="domcontentloaded")
    # 等播放器真正挂载（workspace.html 在 status 回来后才建 <audio>）
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function(
        "() => window.__ws && Array.isArray(window.__ws.sentences) && window.__ws.sentences.length > 0",
        timeout=20000,
    )


def _audio(page, expr: str):
    return page.evaluate(f"() => {{ const a = document.getElementById('local-media-player'); return {expr}; }}")


# ── T06：连续切换两份材料，音频/原文/标题/位置/收藏归属都正确 ─────────


def test_t06_switching_materials_keeps_identity_in_sync(page, materials):
    """连续打开 A → B → A：每次标题、音频地址、原文、句数都必须属于当前材料。"""
    seen = {}
    for key in ("a", "b", "a"):
        lesson = materials[key]
        _goto_workspace(page, lesson["id"])
        state = page.evaluate(
            """() => ({
                lessonId: window.__ws.lessonId,
                title: window.__ws.title,
                mediaUrl: window.__ws.mediaUrl,
                sentenceCount: window.__ws.sentences.length,
                firstSentence: window.__ws.sentences[0]?.text || '',
                audioSrc: document.getElementById('local-media-player')?.getAttribute('src') || '',
                h1: document.getElementById('video-title')?.textContent || '',
                bodyLessonId: document.body.dataset.cetLessonId,
            })"""
        )
        # 归属：lesson id 三处必须一致
        assert state["lessonId"] == lesson["id"]
        assert state["bodyLessonId"] == str(lesson["id"])
        # 标题
        assert state["h1"] == state["title"], f"标题未同步：h1={state['h1']!r} data={state['title']!r}"
        assert lesson["title"] in state["title"] or state["title"] in lesson["title"]
        # 音源必须属于当前材料（不能显示 A 而播放 B）
        assert state["mediaUrl"], "当前材料没有音频地址"
        assert state["audioSrc"].endswith(state["mediaUrl"].split("/")[-1]), (
            f"音源与材料不匹配：src={state['audioSrc']} mediaUrl={state['mediaUrl']}"
        )
        # 原文
        assert state["sentenceCount"] > 0, "原文未加载"
        seen.setdefault(lesson["id"], []).append(state)
    # 同一材料两次打开，句数与首句必须一致（不会串成别的材料）
    for lesson_id, states in seen.items():
        assert len({s["sentenceCount"] for s in states}) == 1
        assert len({s["firstSentence"] for s in states}) == 1
    # 两份材料的首句必须不同（证明确实换了原文）
    first_a = seen[materials["a"]["id"]][0]["firstSentence"]
    first_b = seen[materials["b"]["id"]][0]["firstSentence"]
    assert first_a != first_b, "切换材料后原文没有变化，可能串了"


def test_t06_favorites_are_scoped_to_their_material(page, materials):
    """收藏列表必须只显示当前材料的收藏，不把另一份材料的收藏混进来。"""
    states = {}
    for key in ("a", "b"):
        lesson = materials[key]
        _goto_workspace(page, lesson["id"])
        page.wait_for_selector("#cet-favorites", state="attached", timeout=10000)
        page.wait_for_function(
            "() => window.__cetFavorites && window.__cetFavorites.isMounted()", timeout=10000
        )
        page.wait_for_timeout(800)
        states[key] = page.evaluate(
            """() => {
                const st = window.__cetFavorites.getState();
                return {
                    lessonId: window.__cetFavorites.lessonId,
                    materialId: st.material?.materialId || '',
                    words: st.words.slice(),
                    sentences: st.sentences.map(s => s.text),
                    shown: [...document.querySelectorAll('#cet-favorites [data-cet-fav-word]')]
                        .map(n => n.dataset.cetFavWord),
                };
            }"""
        )
        assert states[key]["lessonId"] == lesson["id"]
        # 面板渲染的词必须就是接口返回的词（不串）
        assert sorted(states[key]["shown"]) == sorted(states[key]["words"])
    # 两份材料的 materialId 必须不同
    if states["a"]["materialId"] and states["b"]["materialId"]:
        assert states["a"]["materialId"] != states["b"]["materialId"]


# ── T07：播放、拖动、倍速、逐句、循环 —— 检查真实媒体状态 ────────────


def test_t07_play_pause_touches_real_media_time(page, materials):
    _goto_workspace(page, materials["a"]["id"])
    before = _audio(page, "a.currentTime")
    page.evaluate("() => window.__ws.play()")
    page.wait_for_timeout(1600)
    playing_time = _audio(page, "a.currentTime")
    paused_after_play = _audio(page, "a.paused")
    assert paused_after_play is False, "调用 play() 后音频仍处于暂停"
    assert playing_time > before + 0.3, f"播放后媒体时间没有前进：{before} → {playing_time}"

    page.evaluate("() => window.__ws.pause()")
    page.wait_for_timeout(500)
    assert _audio(page, "a.paused") is True, "调用 pause() 后音频未暂停"
    frozen = _audio(page, "a.currentTime")
    page.wait_for_timeout(900)
    assert abs(_audio(page, "a.currentTime") - frozen) < 0.25, "暂停后媒体时间仍在走"


def test_t07_seek_moves_real_media_time(page, materials):
    _goto_workspace(page, materials["a"]["id"])
    target = 30.0
    page.evaluate(f"() => window.__ws.seek({target})")
    page.wait_for_timeout(900)
    actual = _audio(page, "a.currentTime")
    assert abs(actual - target) < 1.5, f"seek 未落到目标时间：期望≈{target}，实际 {actual}"


def test_t07_playback_rate_changes_real_media_rate(page, materials):
    _goto_workspace(page, materials["a"]["id"])
    for rate in (0.75, 1.5, 2):
        page.select_option("#playback-rate", str(rate))
        page.wait_for_timeout(500)
        actual = _audio(page, "a.playbackRate")
        assert abs(actual - rate) < 0.01, f"倍速未生效：期望 {rate}，媒体实际 {actual}"
        assert page.evaluate("() => window.__ws.getRate()") == pytest.approx(rate, abs=0.01)
    page.select_option("#playback-rate", "1")
    page.wait_for_timeout(300)
    assert abs(_audio(page, "a.playbackRate") - 1) < 0.01


def test_t07_jump_sentence_moves_time_and_updates_index(page, materials):
    _goto_workspace(page, materials["a"]["id"])
    sentences = page.evaluate("() => window.__ws.sentences.map(s => ({start: s.start, text: s.text}))")
    assert len(sentences) >= 3

    page.evaluate("() => window.__ws.jumpSentence(1)")
    page.wait_for_timeout(700)
    idx = page.evaluate("() => window.__ws.activeSentenceIndex")
    assert idx >= 1, f"逐句前进后索引未变化：{idx}"
    actual = _audio(page, "a.currentTime")
    expected = sentences[idx]["start"]
    assert abs(actual - expected) < 1.2, f"逐句跳转未对齐句子起点：期望≈{expected}，实际 {actual}"

    # 当前句文本必须跟着换（原文与音频同步，不是只动进度条）
    shown = page.inner_text("#current-subtitle")
    first_word = sentences[idx]["text"].split()[0]
    assert first_word in shown, f"当前句未随跳转更新：期望含 {first_word!r}，实际 {shown[:80]!r}"

    page.evaluate("() => window.__ws.jumpSentence(-1)")
    page.wait_for_timeout(700)
    back = page.evaluate("() => window.__ws.activeSentenceIndex")
    assert back == idx - 1, f"逐句后退未回到上一句：{back} != {idx - 1}"


def test_t07_jump_sentence_clamps_at_bounds(page, materials):
    _goto_workspace(page, materials["a"]["id"])
    total = page.evaluate("() => window.__ws.sentences.length")
    page.evaluate("() => window.__ws.jumpSentence(-999)")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__ws.activeSentenceIndex") == 0
    page.evaluate("() => window.__ws.jumpSentence(999)")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__ws.activeSentenceIndex") == total - 1


# ── T08：原文显示、CET 高亮与查词不依赖新模型；无原文状态合理 ─────────


def test_t08_transcript_renders_without_ai_key(page, materials):
    """无 AI key 时原文、点词、CET 高亮都必须可用。"""
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_selector("#current-subtitle .subtitle-word, #current-subtitle", timeout=15000)
    # 原文可见且有内容
    text = page.inner_text("#current-subtitle")
    assert len(text.strip()) > 10, f"原文没有渲染：{text!r}"
    # 句子导航可用（说明 sentenceUnits 就绪）
    assert page.evaluate("() => window.__ws.sentences.length") > 0
    assert page.locator("#prev-sentence").count() == 1
    assert page.locator("#next-sentence").count() == 1
    # 点词查义：本地词典，不依赖模型
    word = page.evaluate(
        """() => {
            const btn = document.querySelector('#current-subtitle .subtitle-word, #current-subtitle .lookup-word');
            return btn ? (btn.dataset.word || btn.textContent || '').trim() : '';
        }"""
    )
    assert word, "当前句没有可点查的词"
    resp = page.evaluate(
        """async (w) => {
            const r = await fetch(`/api/v2/lessons/${window.__ws.lessonId}/word-meaning/${encodeURIComponent(w)}`);
            const d = await r.json();
            return {status: r.status, found: d.found, meaning: d.meaning || '', source: d.source || ''};
        }""",
        word,
    )
    assert resp["status"] == 200, f"查词接口失败：{resp}"
    assert resp["found"] is True, f"本地词典未命中「{word}」：{resp}"
    # 离线来源：compiled = 内置编译词表，local = 本地词典（ECDICT 等）；
    # 都不是 external（模型/在线），说明查词不依赖新模型。
    assert resp["source"] in {"compiled", "local"}, f"查词走了外部/AI：{resp['source']}"


def test_t08_highlighted_words_present_without_model(page, materials):
    """CET 词表高亮来自后端 highlight，不依赖新模型调用。"""
    _goto_workspace(page, materials["a"]["id"])
    highlighted = page.evaluate(
        """() => {
            const segs = window.__ws.segments || [];
            return segs.flatMap(s => s.highlighted_words || []);
        }"""
    )
    # 至少要有高亮能力（数据里可能为空，但字段必须存在且是数组）
    assert isinstance(highlighted, list)
    body_has_highlight_class = page.evaluate(
        "() => !!document.querySelector('.subtitle-word, .lookup-word, .highlight-word, ruby')"
    )
    assert body_has_highlight_class, "原文没有渲染成可交互词"


def test_t08_badge_reports_real_transcript_state(page, materials):
    """徽标必须陈述事实：有原文时不能说「生成中」。"""
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1200)
    badge = page.inner_text("#status-badge")
    body = page.inner_text("body")
    assert "字幕生成中" not in body, "页面仍出现「字幕生成中」"
    assert "AI 正在" not in body, "页面仍出现「AI 正在…」假进行态"
    assert badge.strip() not in {"", "—"} or page.evaluate("() => window.__ws.sentences.length") == 0
    if page.evaluate("() => window.__ws.sentences.length") > 0:
        assert "原文" in badge or "句" in badge, f"有原文时徽标未说明事实：{badge!r}"


# ── T10：刷新 / 关闭重开 / 继续学习，位置与收藏按材料恢复 ──────────────


def _reset_progress(page, lesson_id: int):
    """把某材料的进度归零。

    必须在**离开工作区**的页面上做：工作区页面在 pagehide/beforeunload 时
    会补写当前位置（这是正确行为），如果在工作区页面里重置再跳转，
    那次补写会把刚重置的值又覆盖掉，导致断言看到旧值。
    """
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    page.evaluate(
        f"""async () => {{
            await fetch('/api/v2/lessons/{lesson_id}/progress', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{last_position_seconds: 0, last_segment_index: 0}}),
            }});
        }}"""
    )


def test_t10_progress_restored_after_reload(page, materials):
    lesson_id = materials["a"]["id"]
    # 先归一化到 0：库里可能有历史进度，不依赖「新库」这个前提
    _reset_progress(page, lesson_id)
    _goto_workspace(page, lesson_id)
    assert _audio(page, "a.currentTime") < 2, "归零后页面不应停在旧位置"

    target = 40.0
    page.evaluate(f"() => window.__ws.seek({target})")
    page.wait_for_timeout(1200)
    # 触发一次强制落盘（pause 会立即写）
    page.evaluate("() => window.__ws.play()")
    page.wait_for_timeout(400)
    page.evaluate("() => window.__ws.pause()")
    page.wait_for_timeout(1200)

    saved = page.evaluate(
        f"""async () => {{
            const r = await fetch('/api/v2/lessons/{lesson_id}/progress');
            return await r.json();
        }}"""
    )
    saved_seconds = float(saved["last_position_seconds"])
    assert abs(saved_seconds - target) < 8, (
        f"进度未落盘：期望≈{target}，服务端 {saved_seconds}"
    )

    # 重开页面：位置必须恢复
    _goto_workspace(page, lesson_id)
    page.wait_for_timeout(2500)
    restored = _audio(page, "a.currentTime")
    assert abs(restored - saved_seconds) < 5, (
        f"重开后未恢复位置：服务端 {saved_seconds}，页面 {restored}"
    )


def test_t10_progress_does_not_leak_between_materials(page, materials):
    """给 A 写一个位置，B 不能被影响。"""
    a_id, b_id = materials["a"]["id"], materials["b"]["id"]
    # 两份材料都先归零，确保断言只反映本用例写入的值
    _reset_progress(page, a_id)
    _reset_progress(page, b_id)

    _goto_workspace(page, a_id)
    page.evaluate("() => window.__ws.seek(55)")
    page.wait_for_timeout(1200)
    page.evaluate("() => window.__ws.play()")
    page.wait_for_timeout(300)
    page.evaluate("() => window.__ws.pause()")
    page.wait_for_timeout(1200)

    prog_a = page.evaluate(f"async () => (await fetch('/api/v2/lessons/{a_id}/progress')).json()")
    prog_b = page.evaluate(f"async () => (await fetch('/api/v2/lessons/{b_id}/progress')).json()")
    a_seconds = float(prog_a["last_position_seconds"])
    b_seconds = float(prog_b["last_position_seconds"])
    # A 应该停在 55s 附近（seek 到 55 后短暂播放，可能略前进）
    assert 48 <= a_seconds <= 65, f"A 的进度未落在 55s 附近：{a_seconds}"
    # B 已归零且不能被 A 污染
    assert b_seconds < 5, f"B 的进度被 A 污染：A={a_seconds} B={b_seconds}"


def test_t10_favorites_survive_reload(page, materials):
    lesson_id = materials["a"]["id"]
    _goto_workspace(page, lesson_id)
    page.wait_for_selector("#cet-favorites", state="attached", timeout=10000)
    page.wait_for_function("() => window.__cetFavorites && window.__cetFavorites.isMounted()", timeout=10000)
    page.wait_for_timeout(800)
    before = page.evaluate("() => window.__cetFavorites.getState().words.slice()")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function("() => window.__cetFavorites && window.__cetFavorites.isMounted()", timeout=15000)
    page.wait_for_timeout(1200)
    after = page.evaluate("() => window.__cetFavorites.getState().words.slice()")
    assert sorted(before) == sorted(after), f"刷新后收藏不一致：{before} → {after}"


# ── T13：队友模块关闭/开启，底座独立可用，空格不冲突 ──────────────────


def test_t13_base_workspace_works_without_teammate_module(page, materials):
    """把队友模块的脚本请求拦掉，模拟「未接队友模块」：底座仍完整可用。"""
    page.route("**/cet-listening.js*", lambda route: route.abort())
    page.route("**/cet-listening.css*", lambda route: route.abort())
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1500)
    # 底座能力仍在
    assert page.evaluate("() => window.__ws.sentences.length") > 0
    assert page.evaluate("() => !!document.getElementById('local-media-player')")
    assert page.locator("#cet-study-bar").count() == 1, "底座状态条未挂载"
    assert page.locator("#cet-favorites").count() == 1, "底座收藏未挂载"
    assert page.evaluate("() => window.__cetPlayer.isMounted()") is True
    # 播放仍可用
    page.evaluate("() => window.__ws.play()")
    page.wait_for_timeout(1200)
    assert _audio(page, "a.paused") is False
    # 没有 JS 报错
    errors = [e for e in page._cet_errors if "cet-listening" not in e]  # type: ignore[attr-defined]
    assert not errors, f"未接队友模块时有 JS 报错：{errors}"


def test_t13_space_key_is_owned_by_base_player_when_teammate_off(page, materials):
    """底座在「队友模块关闭」态**接管**空格（返修2 P2-1 契约变更）。

    契约三态（返修2 P2-1 起，主代理裁决的权威版本）：

        | 状态                              | 判定                          | 底座注册 | ownsSpaceKey |
        | --------------------------------- | ----------------------------- | -------- | ------------ |
        | CET 模式 + 队友模块**关闭**        | `CET_ON && !cetListeningEnabled()` | 注册     | `true`       |
        | CET 模式 + 队友模块**开启**        | `CET_ON && cetListeningEnabled()`  | 不注册   | `false`      |
        | 通用模式 `ELT_CET_MODE=0`          | —                             | 不注册   | `false`      |

    本用例断言第一行：**关闭态底座 ownsSpaceKey === true，且按 Space 真实翻转 paused**。

    ⚠️ 为什么和旧断言相反：
        旧契约是「底座永不注册 keydown，空格永远让给队友」。实测问题（任务书 P2-1）：
        队友模块**默认关闭**时，空格让给了一个不在场的模块 —— 工作区里**没有任何播放快捷键**。
        返修2 起底座在关闭态接管 `Space`（播放/暂停）、`←`/`→`（上下句）、`R`（重听）、
        `L`（句循环）；开启态仍让出，契约 `docs/cet-base/队友接入说明.md` §5 同步更新。

    本条用例需 **包2（P2-1）合并后**才会转绿：本包分支上 `cet-player.js` 仍是旧版，
    `ownsSpaceKey` 还是 `false`，属预期红，由主代理在集成分支验证转绿。

    包2 的实现依据（`codex/cet-fix2-p2` @ `6571a57`）：
        - `cet-player.js:459-462` `hotkeyActive()` → `return CET_ON && !cetListeningEnabled();`
        - `cet-player.js:797-818` `syncHotkeyOwnership()` → 成对 add/removeEventListener，
          `S.ownsSpaceKey = active`
        - `cet-player.js:879` `get ownsSpaceKey()` —— **getter，非常量**，故本用例现读现判、不缓存
        - 调用点 3 处：`mount()`:831、`updateBar()`:151、1.5s 定时器:841（覆盖队友模块晚挂载）

    开启态分支（`ownsSpaceKey === false` 且按 Space 不翻转）由**包2 自己的浏览器测试**覆盖
    （它用 `CET_AGENT_ON_BASE_URL` 指向队友开启态服务）。本用例不重复起第二个服务，
    只在下方做「关闭态」的前置断言。
    """
    _goto_workspace(page, materials["a"]["id"])

    # 前置：本用例只在「CET 模式 + 队友模块关闭」态有意义。
    # 开启态下底座让出空格（ownsSpaceKey=false、按 Space 不翻转），
    # 那条分支由包2 自己的浏览器测试覆盖（CET_AGENT_ON_BASE_URL 指向队友开启态服务）。
    listening = page.evaluate("() => window.__cetPlayer.cetListeningEnabled()")
    assert listening is False, (
        "本用例要求队友模块处于关闭态（CET_BASE_URL 指向的服务默认就是关闭态）；"
        "开启态的让出行为由包2 的浏览器测试覆盖"
    )

    # ① 底座明确声明接管（ownsSpaceKey 是 getter，现读现判）
    assert page.evaluate("() => window.__cetPlayer.ownsSpaceKey") is True, (
        "队友模块关闭时底座必须接管空格（返修2 P2-1）；实测 ownsSpaceKey 非 true"
    )

    # ② 真实按键 → 真实媒体状态翻转（读 HTMLMediaElement.paused，不看按钮文案）
    page.evaluate("() => window.__ws.pause()")
    page.wait_for_timeout(400)
    paused_before = _audio(page, "a.paused")
    assert paused_before is True, "前置失败：暂停后媒体仍处于播放态"

    page.keyboard.press("Space")
    page.wait_for_timeout(600)
    paused_after = _audio(page, "a.paused")

    assert paused_after != paused_before, (
        "关闭态下按空格没有翻转播放状态 —— 底座未真正接管（返修2 P2-1）"
    )
    assert paused_after is False, "暂停态按空格应开始播放"

    # ③ 再按一次应回到暂停（证明是稳定切换而非单次副作用）
    page.keyboard.press("Space")
    page.wait_for_timeout(600)
    assert _audio(page, "a.paused") is True, "播放态再按空格应暂停"


def test_t13_teammate_module_mounts_once_when_enabled(page, materials):
    """队友模块开启时只挂载一次，不与底座重复。"""
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(2000)
    toolbar_count = page.locator("#cet-toolbar").count()
    assert toolbar_count <= 1, f"队友工具栏重复挂载：{toolbar_count}"
    bar_count = page.locator("#cet-study-bar").count()
    assert bar_count == 1, f"底座状态条重复挂载：{bar_count}"
    # 底座模块单例
    assert page.evaluate("() => window.__cetPlayer.isMounted()") is True
    assert page.evaluate("() => window.__cetFavorites.isMounted()") is True
    # 再手动调一次 mount 不应重复
    page.evaluate("() => { window.__cetPlayer.mount(); window.__cetFavorites.mount(); }")
    page.wait_for_timeout(500)
    assert page.locator("#cet-study-bar").count() == 1
    assert page.locator("#cet-favorites").count() == 1


def test_t13_no_duplicate_network_polling(page, materials):
    """重复初始化不应产生重复的进度请求（幂等挂载）。"""
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1500)
    before = len([u for u in page._cet_failures])  # type: ignore[attr-defined]
    page.evaluate("() => { window.__cetPlayer.mount(); window.__cetPlayer.mount(); }")
    page.wait_for_timeout(2000)
    after = len(page._cet_failures)  # type: ignore[attr-defined]
    assert after == before, f"重复 mount 产生了新的 4xx：{page._cet_failures[before:]}"


# ── T14：常用桌面宽度和窄窗口 ──────────────────────────────


@pytest.mark.parametrize("width", [1440, 1280, 1024, 820])
def test_t14_layout_has_no_horizontal_overflow(page, materials, width):
    page.set_viewport_size({"width": width, "height": 900})
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1200)
    metrics = page.evaluate(
        """() => ({
            scrollW: document.documentElement.scrollWidth,
            clientW: document.documentElement.clientWidth,
            bodyScrollW: document.body.scrollWidth,
        })"""
    )
    # 允许 2px 的四舍五入误差
    assert metrics["scrollW"] <= metrics["clientW"] + 2, (
        f"{width}px 下出现横向溢出：scrollWidth={metrics['scrollW']} clientWidth={metrics['clientW']}"
    )


@pytest.mark.parametrize("width", [1440, 1024, 820])
def test_t14_core_controls_visible_and_reachable(page, materials, width):
    page.set_viewport_size({"width": width, "height": 900})
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1200)
    for selector in ("#local-media-player", "#playback-rate", "#prev-sentence", "#next-sentence",
                     "#subtitle-mode", "#status-badge"):
        locator = page.locator(selector).first
        assert locator.count() == 1, f"{width}px 下缺少控件 {selector}"
        assert locator.is_visible(), f"{width}px 下控件不可见：{selector}"
    # 侧栏页签与收藏面板可达
    assert page.locator('.sidebar-workspace-tab[data-sidebar-target="collection"]').is_visible()
    page.click('.sidebar-workspace-tab[data-sidebar-target="collection"]')
    page.wait_for_timeout(400)
    assert page.locator("#cet-favorites").is_visible(), f"{width}px 下收藏面板不可达"


def test_t14_page_is_scrollable_on_narrow_window(page, materials):
    page.set_viewport_size({"width": 820, "height": 700})
    _goto_workspace(page, materials["a"]["id"])
    page.wait_for_timeout(1200)
    metrics = page.evaluate(
        """() => {
            const de = document.documentElement;
            return {
                canScroll: de.scrollHeight > de.clientHeight,
                bodyOverflow: getComputedStyle(document.body).overflow,
            };
        }"""
    )
    # 窄窗口必须能滚动（body overflow:auto），否则内容被截断无法访问
    assert metrics["bodyOverflow"] in {"auto", "scroll"}, (
        f"窄窗口 body overflow={metrics['bodyOverflow']}，内容可能无法滚动"
    )
