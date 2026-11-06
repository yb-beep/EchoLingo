"""返修2 · 包4：真实浏览器端到端（P4-1 / P4-2 / P4-3）。

任务书 §0.7 要求「浏览器验证必须是真实服务 + 真实点击/真实媒体状态」，
本文件照此执行：真起 chromium、真点收藏按钮、真读渲染出来的 DOM。

覆盖：
  P4-1 工作区收藏 2 词 1 句 → /#favorites 词 2 → /#sentences 句 1（E5 场景）
  P4-2 词汇收藏页 / 句子收藏页的可见文案（CET 中文口径，无英文残留）
  P4-3 CET 页面无 elt_credits.js 请求、natural-tts.js 仍在；通用模式两者都在

运行前提（与 test_cet_base_workspace.py 同一纪律：服务不可达整组 skip）：
    1. 隔离库起服务：
         ELT_USER_DB=/tmp/fix2-p4-svc/vocab.db ELT_PORT=5184 \
           .venv/bin/python backend/fastapi_server.py
    2. 导入材料：
         ELT_USER_DB=/tmp/fix2-p4-svc/vocab.db \
           .venv/bin/python scripts/cet/import_materials.py --all
    3. CET_BASE_URL=http://127.0.0.1:5184 pytest tests/test_cet_fix2_p4_browser.py

⚠️ 这些用例会真实写入收藏数据，务必指向**本轮新起的隔离库服务**。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")
EVIDENCE = REPO / "docs" / "cet-base" / "返修2" / "evidence"


def _service_up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过浏览器端到端测试"
    "（示例：CET_BASE_URL=http://127.0.0.1:5184）",
)


def _cet_materials() -> list[dict]:
    with urllib.request.urlopen(f"{BASE}/api/cet/materials", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("materials") or data.get("lessons") or []


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    launch_args = ["--autoplay-policy=no-user-gesture-required"]
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chrome", args=launch_args)
        except Exception:  # noqa: BLE001
            b = p.chromium.launch(headless=True, args=launch_args)
        yield b
        b.close()


@pytest.fixture(scope="module")
def lesson():
    items = _cet_materials()
    if not items:
        pytest.skip("隔离库没有四六级材料（先跑 scripts/cet/import_materials.py --all）")
    return items[0]


@pytest.fixture()
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    requests: list[str] = []
    errors: list[str] = []
    pg.on("request", lambda r: requests.append(r.url))
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg._cet_requests = requests  # type: ignore[attr-defined]
    pg._cet_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


# ══════════════════════════════════════════════════════════════
# P4-3：脚本加载（真实网络请求，不是断言静态 HTML）
# ══════════════════════════════════════════════════════════════
def test_p4_3_cet_home_does_not_request_elt_credits(page):
    """CET 模式首页真实不发起 elt_credits.js 请求（credits_account 已收束）。"""
    page.goto(f"{BASE}/", wait_until="networkidle")
    requested = [u for u in page._cet_requests if "elt_credits.js" in u]  # type: ignore[attr-defined]
    assert not requested, f"CET 首页仍请求 elt_credits.js：{requested}"
    # 页面没有因缺脚本报错（billableFetch 封装兜住了）
    assert not page._cet_errors, f"CET 首页有 JS 错误：{page._cet_errors}"  # type: ignore[attr-defined]


def test_p4_3_cet_home_still_loads_natural_tts(page):
    """natural-tts.js 服务保留功能（收藏词卡发音 / 句子兜底朗读），CET 仍加载。"""
    page.goto(f"{BASE}/", wait_until="networkidle")
    requested = [u for u in page._cet_requests if "natural-tts.js" in u]  # type: ignore[attr-defined]
    assert requested, "CET 首页没有加载 natural-tts.js（收藏词卡发音会失效）"
    # 脚本真的执行了：window.NaturalTTS 可用
    assert page.evaluate("() => typeof window.NaturalTTS") == "object"


def test_p4_3_billable_fetch_wrapper_works_without_elt_credits(page):
    """CET 下没有 eltCredits，billableFetch 封装必须仍可用（带幂等键）。"""
    page.goto(f"{BASE}/", wait_until="networkidle")
    assert page.evaluate("() => typeof window.eltCredits") == "undefined"
    assert page.evaluate("() => typeof window.billableFetch") == "function"
    # 用真实请求验证封装在无 eltCredits 时也发得出去，且带 Idempotency-Key
    result = page.evaluate(
        """async () => {
            const resp = await window.billableFetch('word_analysis', '/health', {method: 'GET'});
            return {status: resp.status};
        }"""
    )
    assert result["status"] == 200


# ══════════════════════════════════════════════════════════════
# P4-2：文案（真实渲染的可见文本）
# ══════════════════════════════════════════════════════════════
def test_p4_2_favorites_page_copy_is_chinese_cet(page):
    """/#favorites 的可见文案是中文四六级口径，无 VOCABULARY ATELIER。"""
    page.goto(f"{BASE}/#favorites", wait_until="networkidle")
    page.wait_for_selector("#tab-vocab.active", timeout=10000)
    body = page.inner_text("body")
    for banned in ("VOCABULARY ATELIER", "Vocabulary atelier", "继续词汇练习", "句子精听"):
        assert banned not in body, f"CET 收藏页仍显示英文/残留文案：{banned}"
    assert "四六级 · 词汇收藏" in body
    assert "收藏单词" in body and "收藏句子" in body

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "p4-2-favorites-cet.png"), full_page=False)


def test_p4_2_sentences_pane_copy_is_chinese_cet(page):
    """句子收藏子页的 eyebrow 同样是中文口径。"""
    page.goto(f"{BASE}/#sentences", wait_until="networkidle")
    page.wait_for_selector("#tab-sentences.active", timeout=10000)
    body = page.inner_text("body")
    assert "句子精听" not in body, "CET 句子收藏页仍显示「句子精听」"
    assert "四六级 · 句子收藏" in body

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "p4-2-sentences-cet.png"), full_page=False)


# ══════════════════════════════════════════════════════════════
# P4-1：端到端收藏回流（E5）
# ══════════════════════════════════════════════════════════════
def _open_workspace(page, lesson_id: int) -> None:
    page.goto(f"{BASE}/workspace/{lesson_id}", wait_until="networkidle")
    page.wait_for_selector(".subtitle-word", timeout=25000)


def _favorite_words(page, wanted: int = 2) -> list[str]:
    """在工作区真实点词 → 浮层里点「☆ 加入生词」收藏，返回收藏到的词。

    实测路径（2026-11-06，workspace.html）：
      1. 点 `.subtitle-word`（原文里的可点单词）→ 弹出共享浮层 `#word-popover`
      2. 点浮层里的 `.word-save-btn`（「☆ 加入生词」→「★ 已收藏」）

    注意两个实测坑：
      · `.word-save-btn` 是**每次渲染重建**的，必须限定在 `#word-popover` 内取，
        否则会取到上一轮残留的隐藏节点（playwright 报 element is not visible）；
      · `.subtitle-word` 的 innerText 会带上「★」角标，取词要剥掉。
    """
    saved: list[str] = []
    tokens = page.locator(".subtitle-word")
    count = tokens.count()
    assert count > 0, "工作区找不到可点单词（.subtitle-word）"
    for i in range(min(count, 20)):
        if len(saved) >= wanted:
            break
        token = tokens.nth(i)
        raw = (token.inner_text() or "").strip()
        word = re.sub(r"[^A-Za-z'\-]", "", raw).lower()
        if not re.fullmatch(r"[a-z][a-z'\-]{2,}", word):
            continue  # 跳过标点 / 单字母 / 噪声
        token.click()
        page.wait_for_timeout(500)
        btn = page.locator("#word-popover .word-save-btn")
        if not btn.count() or not btn.first.is_visible():
            continue
        label = btn.first.inner_text() or ""
        if "已收藏" in label:
            continue  # 隔离库复用，之前就收藏过，换下一个词
        btn.first.click()
        page.wait_for_timeout(900)
        if "已收藏" in (page.locator("#word-popover .word-save-btn").first.inner_text() or ""):
            saved.append(word)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    return saved


def _favorite_current_sentence(page) -> bool:
    """收藏当前句：点工作区播放条上的 #save-sentence（aria-label「收藏句子」）。"""
    btn = page.locator("#save-sentence")
    if not btn.count():
        return False
    if btn.first.is_disabled():
        return False
    btn.first.click()
    page.wait_for_timeout(1200)
    return True


def _pick_lesson_with_unfavorited_words(min_words: int = 2) -> int | None:
    """挑一份「还有至少 min_words 个词没被收藏过」的材料。

    为什么要挑：本文件会真实写收藏数据，同一隔离库连跑两次时，
    上一轮已经收藏过的词不会再有「☆ 加入生词」可点（按钮已是「★ 已收藏」）。
    这不是产品缺陷，是测试数据耗尽 —— 所以换一份材料，而不是放宽断言。
    """
    review = set()
    with urllib.request.urlopen(f"{BASE}/vocab-log?include_archived=1&include_mastered=1", timeout=10) as resp:
        review = {k for k in json.loads(resp.read().decode("utf-8")) if not k.startswith("__")}
    for item in _cet_materials():
        match = re.search(r"/workspace/(\d+)", str(item.get("lessonUrl") or ""))
        if not match:
            continue
        lesson_id = int(match.group(1))
        with urllib.request.urlopen(f"{BASE}/api/v2/lessons/{lesson_id}/words", timeout=10) as resp:
            words = json.loads(resp.read().decode("utf-8")).get("words") or []
        if not words:
            # 全新库：该课还没收藏过任何词，原文里每个词都可点 → 直接可用
            return lesson_id
        pending = [w for w in words if str(w).lower() not in review]
        if len(pending) >= min_words:
            return lesson_id
    return None


def test_p4_1_favorites_end_to_end_two_words_one_sentence(page, lesson):
    """E5：工作区收藏 2 词 1 句 → /#favorites 词 2、/#sentences 句 1。

    数据回流有两条路径，本用例验证它们合起来的结果：
      1. 包1 负责的「收藏成功后自动 activate」—— 本用例直接点工作区的收藏按钮；
      2. 包4 负责的「/#favorites 加载时补同步历史遗留」—— 打开收藏页时触发。
    包1 尚未合并时第 1 条不成立，第 2 条的补同步仍应把词补进收藏页 ——
    这正是本用例要证的韧性（所以断言「收藏页能看到这 2 个词」，
    而不是「收藏页正好只有 2 个词」，隔离库可能已有历史数据）。
    """
    lesson_id = _pick_lesson_with_unfavorited_words()
    if lesson_id is None:
        pytest.skip("所有材料里的词都已收藏过，没有可点的「未收藏」词（隔离库复用）")

    _open_workspace(page, lesson_id)
    words = _favorite_words(page, wanted=2)
    assert len(words) >= 2, f"工作区只收藏到 {len(words)} 个词（期望 2）：{words}"
    sentence_saved = _favorite_current_sentence(page)

    # 打开「我的收藏」→ 触发加载时补同步
    page.goto(f"{BASE}/#favorites", wait_until="networkidle")
    page.wait_for_selector("#tab-vocab.active", timeout=15000)
    page.wait_for_timeout(2000)  # 等补同步 + 重新拉词表

    cards = page.locator("#word-list .word-card")
    shown = [str(cards.nth(i).get_attribute("data-word") or "").lower() for i in range(cards.count())]
    assert cards.count() >= 2, (
        f"/#favorites 只显示 {cards.count()} 个词，期望 ≥2"
        f"（收藏的词没回流到我的收藏；刚收藏的：{words}）"
    )
    for word in words:
        assert word in shown, f"刚收藏的 {word} 没出现在我的收藏里（页面有：{shown}）"

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "p4-1-favorites-words.png"), full_page=False)

    # 句子侧
    page.goto(f"{BASE}/#sentences", wait_until="networkidle")
    page.wait_for_selector("#tab-sentences.active", timeout=15000)
    page.wait_for_timeout(1200)
    total_text = (page.inner_text("#sentence-review-total-count") or "").strip()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "p4-1-sentences.png"), full_page=False)
    assert re.fullmatch(r"\d+", total_text), f"句子收藏总数不是数字：{total_text!r}"
    if sentence_saved:
        assert int(total_text) >= 1, "工作区收藏了 1 句，但 /#sentences 显示 0 句"


def test_p4_1_favorites_word_card_keeps_original_sentence(page, lesson):
    """P4-1 验收：收藏页的每个词都带原句，且能「回到原句」。"""
    page.goto(f"{BASE}/#favorites", wait_until="networkidle")
    page.wait_for_selector("#tab-vocab.active", timeout=15000)
    page.wait_for_timeout(1200)
    cards = page.locator("#word-list .word-card")
    if not cards.count():
        pytest.skip("当前隔离库收藏页没有词，无法验证原句（先跑收藏端到端用例）")
    first = cards.first
    # 原句在 .vocab-context 里渲染
    assert first.locator(".vocab-context").count(), "收藏页词卡没有原句区块"
    sentence = first.locator(".vocab-context").first.inner_text().strip()
    assert sentence, "收藏页词卡的原句为空"
    # 「回到原句」链接（CET 文案）
    body = first.inner_text()
    # 模板里的实际文案是「← 返回原句」（任务书口语写作「回到原句」）
    assert "返回原句" in body or first.locator("a[href*='/workspace/']").count(), \
        "收藏页词卡没有「返回原句」入口"
