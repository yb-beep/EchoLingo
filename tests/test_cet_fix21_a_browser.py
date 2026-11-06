"""返修2.1 · 包A（前端）真实浏览器端到端测试 —— 真实服务 + 真实点击。

运行前提（与 tests/test_cet_fix2_p1_workspace_browser.py 同一纪律）：
    1. 服务已启动（**全新隔离库**）：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_CONFIG_DIR=/tmp/<x>/config ELT_PORT=5281 \
           .venv/bin/python backend/fastapi_server.py
    2. 材料已导入：
         ELT_USER_DB=/tmp/<x>/vocab.db ELT_CONFIG_DIR=/tmp/<x>/config \
           .venv/bin/python scripts/cet/import_materials.py --all

环境变量：
    CET_BASE_URL   必填。不设则整组 skip。
    CET_MODE       "cet"（默认）或 "generic"。
                   generic 请指向 ELT_CET_MODE=0 的服务（如 5283），
                   用来跑 F6「通用模式行为不变」那几条。

为什么必须真实浏览器（任务书 §1）：
    A1 的验收是「全新库打开工作区：状态条本课收藏 0、字幕高亮照常、精听页数量一致」；
    A3 是「网络里只有 1 次 POST /word，没有额外 activate」；
    A5 是「设 hidden 后 getComputedStyle().display === 'none'」。
    这三条都只能靠真实渲染 + 真实网络记录证明，断言静态 HTML 没有意义。

**本文件的用例会写收藏数据**，请对全新库运行；复用隔离库时靠
`_pick_unfavorited_word` 挑未收藏词，挑不到就 skip（不是产品缺陷，是数据耗尽）。
"""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")
MODE = (os.environ.get("CET_MODE") or "cet").strip().lower()
IS_CET = MODE != "generic"


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


def _cet_materials() -> list[dict]:
    try:
        data = _get_json("/api/cet/materials")
    except Exception:  # noqa: BLE001
        return []
    items = data.get("items") or data.get("materials") or []
    out = []
    for item in items:
        tail = str(item.get("lessonUrl") or "").rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit() and int(item.get("sentenceCount") or 0) > 0:
            out.append({"id": int(tail), "title": item.get("title") or ""})
    return out


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
def lesson():
    items = _cet_materials()
    if not items:
        pytest.skip("需要至少一份带逐句时间轴的四六级材料（scripts/cet/import_materials.py --all）")
    return items[0]


def _open_workspace(page, lesson_id: int):
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="domcontentloaded")
    page.wait_for_selector("#local-media-player", state="attached", timeout=20000)
    page.wait_for_function(
        "() => window.__ws && Array.isArray(window.__ws.sentences) && window.__ws.sentences.length > 0",
        timeout=20000,
    )
    page.wait_for_timeout(2000)


def _pick_unfavorited_word(page) -> str | None:
    return page.evaluate(
        """() => {
        const btns = Array.from(document.querySelectorAll('.subtitle-word'));
        for (const b of btns) {
          const w = (b.dataset.word || '').trim();
          if (w && !b.classList.contains('saved-word')) return w;
        }
        return null;
    }"""
    )


# ══════════════════════════════════════════════════════════════
# A1（阻断）：CET 模式不再自动同步高亮词
# ══════════════════════════════════════════════════════════════


@pytest.mark.skipif(not IS_CET, reason="CET 模式专用（CET_MODE=generic 时不适用）")
def test_a1_workspace_does_not_sync_highlighted_words(page, lesson):
    """全新库打开工作区：不发高亮词同步、本课收藏 0、/words 空。"""
    before = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    if before:
        pytest.skip(f"该课已有 {len(before)} 个收藏词（隔离库复用），A1 需全新库验证")

    _open_workspace(page, lesson["id"])

    sync_calls = [r for r in page._reqs if "highlighted-words/sync" in r]
    assert not sync_calls, f"CET 模式仍发起高亮词同步：{sync_calls}"

    dom = page.evaluate(
        """() => ({
        saved: (document.querySelector('#cet-saved-count') || {}).textContent || '',
        review: (document.querySelector('#review-word-count') || {}).textContent || '',
        subtitle_words: document.querySelectorAll('.subtitle-word').length,
    })"""
    )
    assert dom["saved"] == "0", f"状态条「本课收藏」应为 0，实际 {dom['saved']}"
    assert dom["review"] == "0 词", f"收藏面板应为 0 词，实际 {dom['review']}"
    after = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    assert after == [], f"打开工作区后 v2_lesson_words 被自动写入：{after}"


@pytest.mark.skipif(not IS_CET, reason="CET 模式专用")
def test_a1_subtitle_highlight_still_works(page, lesson):
    """关掉同步后字幕高亮**照常显示**（A1 依赖确认的浏览器证据）。

    高亮来自 GET /subtitles 的 segment.highlighted_words（highlight_segments 当场算），
    不读 v2_lesson_words，所以不受 A1 影响。

    ★ 为什么必须 seek 到「有高亮的句子」再断言（实测踩到的坑）：
    工作区的 `#current-subtitle` **只渲染当前一句**，不是整篇时间轴。
    刚进页面时当前句是第 0 句，而它未必有高亮词 —— 实测 lesson2：
      [刚进页面] subtitle_word=10, highlight_word=0（第 0 句 "Okay, I'm hoping..." 无高亮）
      [seek 到第 3 句] subtitle_word=9, highlight_word=1（"Did you notice any **sentences**..."）
    所以在第 0 句断言「DOM 有 .highlight-word」是**测试写错**，不是产品缺陷。
    """
    api = _get_json(f"/api/v2/lessons/{lesson['id']}/subtitles")
    segs = api.get("segments") or []
    target_idx = next(
        (i for i, s in enumerate(segs) if s.get("highlighted_words")), None
    )
    if target_idx is None:
        pytest.skip("这份材料没有词表命中的高亮词，无法验证高亮显示")
    expected_words = [str(w).lower() for w in segs[target_idx]["highlighted_words"]]

    _open_workspace(page, lesson["id"])

    # seek 到第一个有高亮的句子（真实播放定位，不是改 DOM）
    page.evaluate(
        """(i) => { const s = window.__ws.sentences[i]; window.__ws.seek(s.start); }""",
        target_idx,
    )
    page.wait_for_timeout(2500)

    dom = page.evaluate(
        """() => ({
        active: window.__ws ? window.__ws.activeSentenceIndex : null,
        subtitle_words: document.querySelectorAll('.subtitle-word').length,
        highlight_words: document.querySelectorAll('.subtitle-word.highlight-word').length,
        highlighted_in_dom: Array.from(document.querySelectorAll('.subtitle-word.highlight-word'))
            .map(el => (el.dataset.word || '').toLowerCase()),
    })"""
    )
    assert dom["subtitle_words"] > 0, "字幕词按钮没了（原文渲染异常）"
    assert dom["highlight_words"] > 0, (
        f"字幕高亮消失了：API 第 {target_idx} 句有高亮 {expected_words}，DOM 里 0 个 .highlight-word"
    )
    # 高亮的正是 API 给的那些词（证明高亮数据确实来自 /subtitles，不是 v2_lesson_words）
    assert set(dom["highlighted_in_dom"]) & set(expected_words), (
        f"DOM 高亮词 {dom['highlighted_in_dom']} 与 API 高亮词 {expected_words} 无交集"
    )


@pytest.mark.skipif(not IS_CET, reason="CET 模式专用")
def test_a1_intensive_page_does_not_sync_and_keeps_focus_count(page, lesson):
    """A1 附加修复：精听页也不写库，且「重点词汇 N」数量不变。

    这是本轮发现的**第二条**自动写入通道（intensive.html:2300）。
    门控后必须：不发 sync、v2_lesson_words 仍空、重点词汇计数 = API 算出来的值。
    """
    before = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    if before:
        pytest.skip(f"该课已有 {len(before)} 个收藏词（隔离库复用），A1 需全新库验证")

    doc = _get_json(f"/api/v2/lessons/{lesson['id']}/intensive")
    expected = sum(1 for s in doc.get("sentences") or [] if s.get("highlighted_words"))

    page._reqs.clear()
    page.goto(f"{BASE}/workspace/{lesson['id']}/intensive", wait_until="networkidle")
    page.wait_for_timeout(2500)

    sync_calls = [r for r in page._reqs if "highlighted-words/sync" in r]
    assert not sync_calls, f"精听页仍在同步高亮词：{sync_calls}"

    after = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    assert after == [], f"打开精听页后 v2_lesson_words 被自动写入：{after}"

    dom = page.evaluate(
        """() => ({
        vocab: (document.querySelector('#count-vocabulary') || {}).textContent || '',
        all: (document.querySelector('#count-all') || {}).textContent || '',
    })"""
    )
    assert dom["vocab"] == str(expected), (
        f"精听页「重点词汇」数量变了：期望 {expected}，实际 {dom['vocab']}"
    )


@pytest.mark.skipif(not IS_CET, reason="CET 模式专用")
def test_a1_f1_variant_workspace_then_intensive_then_back(page, lesson):
    """F1 变体（主代理加的场景）：工作区 → 精听页 → 返回工作区，本课收藏仍 0。

    这条是本轮最有价值的回归：只堵工作区那条通道时，用户点一下
    「逐句精听 →」再返回，自动高亮词就会冒充收藏。
    """
    before = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    if before:
        pytest.skip(f"该课已有 {len(before)} 个收藏词（隔离库复用）")

    _open_workspace(page, lesson["id"])
    assert page.evaluate(
        "() => (document.querySelector('#cet-saved-count') || {}).textContent"
    ) == "0"

    page.goto(f"{BASE}/workspace/{lesson['id']}/intensive", wait_until="networkidle")
    page.wait_for_timeout(2500)

    _open_workspace(page, lesson["id"])
    saved = page.evaluate(
        "() => (document.querySelector('#cet-saved-count') || {}).textContent"
    )
    assert saved == "0", f"逛完精听页返回后「本课收藏」变成 {saved}（自动高亮词冒充了收藏）"
    words = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    assert words == [], f"逛完精听页返回后 v2_lesson_words 被写入：{words}"


# ══════════════════════════════════════════════════════════════
# A2（阻断）：打开「我的收藏」不写库
# ══════════════════════════════════════════════════════════════


def test_a2_favorites_page_does_not_post_sync_collections(page, lesson):
    """全新库：打开工作区 → 打开「我的收藏」，没有 sync-collections 请求。"""
    _open_workspace(page, lesson["id"])
    page._reqs.clear()
    page.goto(f"{BASE}/#favorites", wait_until="networkidle")
    page.wait_for_timeout(3000)

    bad = [r for r in page._reqs if "sync-collections" in r]
    assert not bad, f"打开「我的收藏」仍触发写库请求：{bad}"
    # 页面不能白屏 / 报错
    assert not page._errors, f"收藏页 JS 报错：{page._errors}"
    body = page.evaluate("() => (document.body.innerText || '').slice(0, 400)")
    assert body.strip(), "收藏页白屏（body 无文本）"


# ══════════════════════════════════════════════════════════════
# A3：收藏只发 1 次 POST /word，没有额外 activate
# ══════════════════════════════════════════════════════════════


def test_a3_favorite_sends_one_post_and_no_activate(page, lesson):
    """收藏 1 个词：网络里只有 1 次 POST .../word，没有 activate；词进 /vocab-log。"""
    _open_workspace(page, lesson["id"])
    word = _pick_unfavorited_word(page)
    if not word:
        pytest.skip("本课所有词都已收藏（隔离库复用），没有可点的未收藏词")

    page.evaluate(
        """(w) => {
        const b = Array.from(document.querySelectorAll('.subtitle-word'))
          .find(el => (el.dataset.word || '') === w);
        b.click();
    }""",
        word,
    )
    page.wait_for_selector("#word-popover.open", timeout=8000)
    page.wait_for_timeout(800)

    page._reqs.clear()
    page.click("#word-popover .word-save-btn")
    page.wait_for_timeout(2500)

    post_word = [r for r in page._reqs if r.startswith("POST") and r.endswith("/word")]
    activate = [r for r in page._reqs if "vocab-review/activate" in r]
    assert len(post_word) == 1, f"收藏 1 个词应只发 1 次 POST /word，实际 {post_word}"
    assert not activate, f"收藏后仍多调了一次 activate：{activate}"

    # 行为不变：词照样进「我的收藏」的数据源
    vocab = _get_json("/vocab-log?include_archived=1&include_mastered=1")
    words = [k for k in vocab if not k.startswith("__")]
    assert any(word.lower() == w.lower() for w in words), (
        f"收藏的「{word}」没进 /vocab-log（我的收藏看不到）：{words}"
    )


# ══════════════════════════════════════════════════════════════
# A4：查词卡片文案
# ══════════════════════════════════════════════════════════════


def test_a4_popover_copy_cet(page, lesson):
    """CET：卡片按钮 = ☆ 收藏单词 / ✓ 已收藏，且没有「＋ 加入复习本」。"""
    if not IS_CET:
        pytest.skip("CET 模式专用")
    _open_workspace(page, lesson["id"])
    word = _pick_unfavorited_word(page)
    if not word:
        pytest.skip("没有可点的未收藏词")

    page.evaluate(
        """(w) => {
        const b = Array.from(document.querySelectorAll('.subtitle-word'))
          .find(el => (el.dataset.word || '') === w);
        b.click();
    }""",
        word,
    )
    page.wait_for_selector("#word-popover.open", timeout=8000)
    page.wait_for_timeout(800)

    before = page.evaluate(
        """() => {
        const pop = document.querySelector('#word-popover');
        return {
          save: (pop.querySelector('.word-save-btn') || {}).textContent || '',
          has_review: Boolean(pop.querySelector('.word-review-btn')),
          master: (pop.querySelector('.word-master-btn') || {}).textContent || '',
          text: pop.innerText,
        };
    }"""
    )
    assert before["save"].strip() == "☆ 收藏单词", f"未收藏文案不对：{before['save']}"
    assert not before["has_review"], "CET 模式仍有「加入复习本」按钮"
    assert "加入复习本" not in before["text"]
    assert before["master"].strip() == "✓ 已掌握", "「已掌握」被误改（任务书要求保留）"

    page.click("#word-popover .word-save-btn")
    page.wait_for_timeout(2500)
    after = page.evaluate(
        """() => {
        const pop = document.querySelector('#word-popover');
        return { save: (pop.querySelector('.word-save-btn') || {}).textContent || '',
                 has_review: Boolean(pop.querySelector('.word-review-btn')),
                 text: pop.innerText };
    }"""
    )
    assert after["save"].strip() == "✓ 已收藏", f"已收藏文案不对：{after['save']}"
    assert not after["has_review"]


def test_a4_cet_visible_text_has_no_banned_wording(page, lesson):
    """CET 工作区可见文本中「生词」「复习本」出现 0 次（含「本课收藏」面板）。"""
    if not IS_CET:
        pytest.skip("CET 模式专用")
    _open_workspace(page, lesson["id"])
    counts = page.evaluate(
        """() => {
        const t = document.body.innerText || '';
        const c = s => t.split(s).length - 1;
        return { '生词': c('生词'), '复习本': c('复习本') };
    }"""
    )
    assert counts["生词"] == 0, f"CET 工作区仍出现「生词」{counts['生词']} 次"
    assert counts["复习本"] == 0, f"CET 工作区仍出现「复习本」{counts['复习本']} 次"


def test_a4_generic_popover_copy_unchanged(page, lesson):
    """通用模式：☆ 加入生词 / ★ 已收藏 + ＋ 加入复习本（与 3573e90 一致）。"""
    if IS_CET:
        pytest.skip("通用模式专用（CET_MODE=generic + ELT_CET_MODE=0 的服务）")
    _open_workspace(page, lesson["id"])
    word = _pick_unfavorited_word(page)
    if not word:
        pytest.skip("没有可点的未收藏词")

    page.evaluate(
        """(w) => {
        const b = Array.from(document.querySelectorAll('.subtitle-word'))
          .find(el => (el.dataset.word || '') === w);
        b.click();
    }""",
        word,
    )
    page.wait_for_selector("#word-popover.open", timeout=8000)
    page.wait_for_timeout(800)

    before = page.evaluate(
        """() => {
        const pop = document.querySelector('#word-popover');
        return {
          save: (pop.querySelector('.word-save-btn') || {}).textContent || '',
          review: (pop.querySelector('.word-review-btn') || {}).textContent || '',
          has_review: Boolean(pop.querySelector('.word-review-btn')),
        };
    }"""
    )
    assert before["save"].strip() == "☆ 加入生词", f"通用模式文案被改了：{before['save']}"
    assert before["has_review"], "通用模式丢了「加入复习本」按钮"
    assert before["review"].strip() == "＋ 加入复习本"

    page.click("#word-popover .word-save-btn")
    page.wait_for_timeout(2500)
    after = page.evaluate(
        """() => (document.querySelector('#word-popover .word-save-btn') || {}).textContent || ''"""
    )
    assert after.strip() == "★ 已收藏", f"通用模式已收藏文案被改了：{after}"


def test_a4_generic_still_syncs_highlighted_words(page, lesson):
    """通用模式（F6）：高亮词同步**必须还在**（A1 只关 CET）。"""
    if IS_CET:
        pytest.skip("通用模式专用")
    before = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    if before:
        pytest.skip("该课已有收藏词（隔离库复用），通用模式同步验证需全新库")

    _open_workspace(page, lesson["id"])
    page.wait_for_timeout(2000)
    sync_calls = [r for r in page._reqs if "highlighted-words/sync" in r]
    assert sync_calls, "通用模式的高亮词同步被误删（F6 回归：必须与 3573e90 一致）"
    after = _get_json(f"/api/v2/lessons/{lesson['id']}/words").get("words") or []
    assert after, "通用模式打开工作区后 v2_lesson_words 仍应为空？不——原版会写入"


# ══════════════════════════════════════════════════════════════
# A5：hidden 必须真的隐藏
# ══════════════════════════════════════════════════════════════


def test_a5_study_item_hidden_is_really_hidden(page, lesson):
    """对任一 .cet-study-item 设 hidden=true → getComputedStyle().display === 'none'。

    同时验证「不是本来就 none」：先强制 display:inline-flex，再设 hidden。
    """
    _open_workspace(page, lesson["id"])
    has_bar = page.evaluate("() => Boolean(document.querySelector('.cet-study-bar .cet-study-item'))")
    if not has_bar:
        pytest.skip("当前页面没有 .cet-study-bar（CET 状态条未挂载）")

    result = page.evaluate(
        """() => {
        const el = document.querySelector('.cet-study-bar .cet-study-item');
        // 先证明它本来可见（作者样式 display:inline-flex）
        el.hidden = false;
        el.style.display = '';
        const visibleDisplay = getComputedStyle(el).display;
        // 再设 hidden
        el.hidden = true;
        const hiddenDisplay = getComputedStyle(el).display;
        // 还原
        el.hidden = false;
        return { visibleDisplay, hiddenDisplay };
    }"""
    )
    assert result["visibleDisplay"] == "flex" or result["visibleDisplay"] == "inline-flex", (
        f"对照组不成立：未设 hidden 时 display={result['visibleDisplay']}，测不出覆盖问题"
    )
    assert result["hiddenDisplay"] == "none", (
        f"A5 未生效：设了 hidden 后 display 仍是 {result['hiddenDisplay']}"
    )


def test_a5_loop_indicator_hidden_at_build_time(page, lesson):
    """初始态「本句循环」指示条（HTML 里带 hidden）必须不可见。

    这是包2 踩过的同一个坑，A5 补根因规则后应由 CSS 兜住（不再只靠 setShown）。
    """
    _open_workspace(page, lesson["id"])
    result = page.evaluate(
        """() => {
        const el = document.querySelector('.cet-study-bar .cet-study-item[data-cet-item="loop"]');
        if (!el) return null;
        return { hidden: el.hidden, display: getComputedStyle(el).display };
    }"""
    )
    if result is None:
        pytest.skip("当前页面没有「本句循环」指示条")
    if not result["hidden"]:
        pytest.skip("循环已开启，指示条本就该可见")
    assert result["display"] == "none", (
        f"「本句循环」设了 hidden 却仍显示（display={result['display']}）"
    )
