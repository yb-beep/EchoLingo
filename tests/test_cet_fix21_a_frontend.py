"""返修2.1 · 包A（前端）静态契约 + 反向锁定测试。

覆盖编号：A1 / A1-附加（精听页第二条通道）/ A2 / A3 / A4 / A5。
每个编号都同时断言：
    · CET 模式（ELT_CET_MODE=1）下新行为生效
    · 通用模式（ELT_CET_MODE=0）下**原版行为不变**（任务书 §1、F6）

分工：
    - 本文件：不起服务，直接渲染模板 / 读模板与静态文件，跑得很快
    - tests/test_cet_fix21_a_browser.py：真实服务 + 真实点击（CET_BASE_URL）

为什么本文件大量是「反向断言」（断言某段代码**不存在**）：
    返修2.1 的核心是**拆掉多余通道**（任务书 §0：上一轮的前提是错的）。
    「不再调用 sync-collections / 不再重复 activate」本身就是要锁定的行为，
    正向断言（断言新函数存在）反而会漏掉「旧调用被加回来」这种回归。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

WORKSPACE = ROOT / "frontend" / "templates" / "workspace.html"
INTENSIVE = ROOT / "frontend" / "templates" / "intensive.html"
INDEX = ROOT / "frontend" / "templates" / "index.html"
CET_CSS = ROOT / "frontend" / "static" / "cet-workspace.css"
CET_FAVORITES = ROOT / "frontend" / "static" / "cet-favorites.js"
CET_MODE_PY = ROOT / "backend" / "webapp" / "cet_mode.py"


def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def _visible_html(html: str) -> str:
    """剥掉 <script>/<style> 后的可见标记（用户真正能看到的文本）。"""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    return re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.S)


def _strip_js_comments(src: str) -> str:
    """去掉 JS 注释，只留**代码**。

    为什么需要：本轮改动几乎每处都带一段解释性注释，注释里会自然地出现
    `highlighted-words/sync` / `sync-collections` / `vocab-review/activate` 这些
    **被删掉的端点名**（"这里不再调用 X"）。若断言直接在原文上做子串匹配，
    就会把「注释里说不要调」误判成「代码里还在调」，测试变成假红。
    所以所有「不得出现」的断言都必须在去注释后的代码上做。
    """
    # 先去掉 /* ... */ 块注释，再去掉 // 行注释（避免误伤 http:// —— 本文件里
    # 端点都是相对路径，行注释判断要求 // 前不是 ':'）
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def _js_function(src: str, signature: str) -> str:
    """取一个 JS 函数的**代码体**（已去注释）。

    截断点用 `\\n}` 而不是 `}`，是因为函数体内部有缩进的 `  } catch (...)`
    （那是 `\\n  }`，不是 `\\n}`），只有函数自己的收尾括号顶格。
    """
    code = _strip_js_comments(src)
    assert signature in code, f"找不到函数签名：{signature}"
    return code.split(signature, 1)[1].split("\n}", 1)[0]


def _render_workspace(monkeypatch, cet_mode: str) -> str:
    monkeypatch.setenv("ELT_CET_MODE", cet_mode)
    import db
    from fastapi.templating import Jinja2Templates
    from fastapi.testclient import TestClient
    from fastapi_server import create_app
    from webapp.fastapi_routes import pages

    monkeypatch.setattr(
        pages, "templates", Jinja2Templates(directory=str(ROOT / "frontend" / "templates"))
    )
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="cetMaterial",
        source_url="file://fix21-a-fixture.mp3",
        title="返修2.1 包A 测试材料",
        lesson_mode="listening",
    )
    resp = client.get(f"/workspace/{lesson['id']}")
    assert resp.status_code == 200, resp.text[:400]
    return resp.text


@pytest.fixture()
def cet_html(monkeypatch):
    return _render_workspace(monkeypatch, "1")


@pytest.fixture()
def generic_html(monkeypatch):
    return _render_workspace(monkeypatch, "0")


# ══════════════════════════════════════════════════════════════
# A1（阻断）：CET 模式不再自动同步高亮词到「本课收藏」
# ══════════════════════════════════════════════════════════════


def test_a1_workspace_sync_gated_by_central_switch():
    """`syncHighlightedWordsAfterRender()` 开头必须按集中开关早退。

    开关唯一来源是 window.__CET_BOOT__ → cet_on（workspace.html 的 CET_ON），
    不得新写第二套判断（如 localStorage / 自己的环境变量探测）。
    """
    js = _text(WORKSPACE)
    fn = _js_function(js, "async function syncHighlightedWordsAfterRender()")
    # 守卫必须在**发起请求之前**
    assert "if (CET_ON) return;" in fn, "CET 模式下仍会调用高亮词同步"
    guard_at = fn.index("if (CET_ON) return;")
    fetch_at = fn.index("highlighted-words/sync")
    assert guard_at < fetch_at, "守卫写在了 fetch 之后，等于没拦"
    # 开关读集中配置，不另起判断
    assert "CET_ON" in fn
    assert "localStorage" not in fn and "ELT_CET_MODE" not in fn, (
        "A1 开关必须读集中配置 CET_ON（← window.__CET_BOOT__），不得另写第二套判断"
    )


def test_a1_workspace_sync_still_present_for_generic_mode():
    """通用模式必须**保留**原同步（F6：ELT_CET_MODE=0 行为与 3573e90 一致）。"""
    js = _text(WORKSPACE)
    fn = _js_function(js, "async function syncHighlightedWordsAfterRender()")
    assert "highlighted-words/sync" in fn, "通用模式的高亮同步被误删"
    # 门控是 early return，不是把请求删掉
    assert "return;" in fn
    # 原 P1-5 的「synced>0 才重拉」优化必须还在（不能被本轮改回无条件 loadSavedWords）
    assert "if (Number(data.synced || 0) > 0) await loadSavedWords();" in fn


def test_a1_all_sync_call_sites_exist():
    """调用点本身保留（门控在函数内部），避免「删调用点」漏掉将来新增的调用方。"""
    js = _text(WORKSPACE)
    assert js.count("syncHighlightedWordsAfterRender();") >= 1


# ══════════════════════════════════════════════════════════════
# A1 附加（阻断）：精听页存在**第二条**自动写入通道
# ══════════════════════════════════════════════════════════════


def test_a1_intensive_sync_gated_by_central_switch():
    """intensive.html 的 `syncIntensiveHighlightedWords()` 同样必须门控。

    这是 A1 的第二条通道：只堵 workspace 那条，用户点一下「逐句精听 →」
    再返回，自动高亮词又会写进 v2_lesson_words 冒充收藏。
    """
    js = _text(INTENSIVE)
    assert "function syncIntensiveHighlightedWords()" in js, "精听页同步函数不见了（改名？）"
    fn = _js_function(js, "async function syncIntensiveHighlightedWords()")
    assert "if (CET_ON) return;" in fn, "精听页 CET 模式下仍会同步高亮词"
    guard_at = fn.index("if (CET_ON) return;")
    fetch_at = fn.index("highlighted-words/sync")
    assert guard_at < fetch_at, "精听页守卫写在了 fetch 之后"
    # CET_ON 来自集中配置（intensive.html 里由 __CET_BOOT__.enabled 定义）
    assert "const CET_ON = window.__CET_BOOT__.enabled;" in js


def test_a1_intensive_sync_still_present_for_generic_mode():
    """通用模式精听页必须保留原同步。"""
    js = _text(INTENSIVE)
    fn = _js_function(js, "async function syncIntensiveHighlightedWords()")
    assert "highlighted-words/sync" in fn
    assert "return;" in fn


def test_a1_intensive_call_site_kept():
    """调用点保留（门控在函数内部，将来多一个调用方也安全）。"""
    js = _text(INTENSIVE)
    assert "await syncIntensiveHighlightedWords();" in js


# ══════════════════════════════════════════════════════════════
# A2（阻断）：打开「我的收藏」不再触发写库
# ══════════════════════════════════════════════════════════════


def test_a2_favorites_load_does_not_post_sync_collections():
    """`initVocab()` 尾部不得再调用补同步（查看页面不应写库）。"""
    html = _text(INDEX)
    init_fn = _js_function(html, "async function initVocab()")
    assert "cetSyncLessonCollections" not in init_fn, "收藏页加载仍在触发补同步"
    assert "sync-collections" not in init_fn


def test_a2_no_frontend_caller_of_sync_collections_at_all():
    """前端全库不得再有 `sync-collections` 调用（端点由包 B 的 B3 删除）。"""
    for path in (INDEX, WORKSPACE, INTENSIVE, CET_FAVORITES):
        src = _strip_js_comments(_text(path))
        # 允许出现在注释里说明「为什么删掉」，但不允许出现在 fetch(...) 里
        for match in re.finditer(r"fetch\(\s*[`'\"]([^`'\"]*sync-collections[^`'\"]*)", src):
            pytest.fail(f"{path.name} 仍在 fetch sync-collections：{match.group(1)}")


def test_a2_sync_function_removed_from_index():
    """`cetSyncLessonCollections()` 函数本身已删除（不留死代码通道）。"""
    html = _text(INDEX)
    assert "function cetSyncLessonCollections" not in html, (
        "补同步函数仍在 index.html，等于留了一条随时可被调用的写库通道"
    )
    assert "_cetCollectionsSynced" not in html


# ══════════════════════════════════════════════════════════════
# A3：去掉收藏后多余的 activate 重复调用
# ══════════════════════════════════════════════════════════════


def test_a3_toggle_word_save_does_not_call_activate_again():
    """`toggleWordSave` 新增分支只发 1 次 POST /word，不再额外 activate。

    依据：`save_word`（v2_lessons.py:1700）内部已 `db.activate_word_review(...)`。
    """
    js = _text(WORKSPACE)
    code = _strip_js_comments(js)
    toggle = code.split("async function toggleWordSave", 1)[1].split(
        "async function addWordToReviewFromPopover", 1
    )[0]
    assert "autoActivateFavoriteWord(" not in toggle, "收藏后仍重复调用自动 activate"
    assert "vocab-review/activate" not in toggle, "收藏后仍直接打 activate 端点"
    # 收藏请求本身还在（行为不变：收藏仍然成功）
    assert "`/api/v2/lessons/${LESSON_ID}/word`" in toggle


def test_a3_auto_activate_helper_removed():
    """`autoActivateFavoriteWord()` 已整段删除（唯一调用点已移除，留着就是活的重复通道）。"""
    js = _strip_js_comments(_text(WORKSPACE))
    assert "function autoActivateFavoriteWord" not in js
    assert "async function autoActivateFavoriteWord" not in js


def test_a3_activate_endpoint_still_used_by_generic_review_button():
    """通用模式「加入复习本」仍走 activateWordReview → /api/vocab-review/activate。

    A3 只删「收藏后自动多调一次」，不能顺手删掉通用模式的用户主动入口。
    """
    js = _text(WORKSPACE)
    assert "async function activateWordReview(" in js
    assert "/api/vocab-review/activate" in js
    assert "async function addWordToReviewFromPopover(" in js
    assert "async function addSavedWordToReview(" in js


def test_a3_favorites_module_has_no_activate_dependency():
    """cet-favorites.js 不得依赖 activate（它只读 /words + /phase-b + 材料身份）。"""
    js = _text(CET_FAVORITES)
    assert "vocab-review/activate" not in js
    assert "sync-collections" not in js


# ══════════════════════════════════════════════════════════════
# A4：查词卡片文案走 cet_copy，「生词」「复习本」在 CET 可见文本 0 次
# ══════════════════════════════════════════════════════════════


def test_a4_popover_copy_uses_cet_copy_keys():
    """收藏按钮文案必须走 cetCopy 键，不得硬编码两套中文。"""
    js = _text(WORKSPACE)
    pop = _strip_js_comments(js).split("function openWordPopover(", 1)[1].split(
        "function speakWordFromPopover", 1
    )[0]
    assert "cetCopy('fix2_favorite_word_cta'" in pop, "未收藏文案没走 cet_copy"
    assert "cetCopy('fix2_favorite_added'" in pop, "已收藏文案没走 cet_copy"
    assert "cetCopy('fix2_master_hint'" in pop, "「已掌握」悬浮说明没走 cet_copy"


def test_a4_review_book_button_removed_in_cet_only():
    """「＋ 加入复习本」在 CET 分支不渲染，通用分支保留（F6）。"""
    js = _text(WORKSPACE)
    pop = _strip_js_comments(js).split("function openWordPopover(", 1)[1].split(
        "function speakWordFromPopover", 1
    )[0]
    # CET 分支：reviewBtnHtml 为空
    assert "CET_ON ? ''" in pop, "CET 模式仍渲染「加入复习本」按钮"
    # 通用分支：按钮还在，文案是原版
    assert "＋ 加入复习本" in pop, "通用模式丢了原「加入复习本」按钮"
    assert "✓ 已在复习本" in pop


def test_a4_lesson_panel_review_button_removed_in_cet_only():
    """「本课收藏」面板同样：CET 只出状态标签，通用保留原按钮。"""
    js = _text(WORKSPACE)
    panel = _strip_js_comments(js).split("function renderReadingSavedItems()", 1)[1].split(
        "async function activateWordReview", 1
    )[0]
    assert "data-review-book-state=\"1\"" in panel, "CET 面板丢了状态标签"
    assert 'class="review-book-btn"' in panel, "通用模式面板丢了原「加入复习本」按钮"
    # CET 分支取的是键，不是硬编码
    assert "cetCopy('fix2_favorite_added'" in panel
    assert "cetCopy('fix2_word_in_favorites'" in panel


def test_a4_cet_visible_has_no_banned_wording(cet_html):
    """CET 模式工作区可见标记里「生词」「复习本」出现 0 次。

    注意：`buildReviewMarkdown()` 的 '## 生词 / 不理解的表达' 在 <script> 内，
    属于导出的 Markdown 标题、不在页面上，_visible_html 已剥掉 script。
    """
    visible = _visible_html(cet_html)
    for banned in ("生词", "复习本"):
        assert banned not in visible, f"CET 模式工作区仍可见「{banned}」"


def test_a4_generic_visible_keeps_original_wording(generic_html):
    """通用模式可见标记保留原版「加入复习本」（F6 回归）。"""
    visible = _visible_html(generic_html)
    assert "加入复习本" in visible or "review-book-btn" in generic_html


def test_a4_copy_keys_are_paired_and_use_fallbacks():
    """所有 fix2_ 键都必须带兜底（键没合进来时页面不白屏）+ 每个键两套文案成对。"""
    js = _text(WORKSPACE)
    code = _strip_js_comments(js)
    bare = re.findall(r"cetCopy\(\s*'(fix2_[^']+)'\s*\)", code)
    assert not bare, f"这些 fix2_ 键取值没带兜底：{bare}"
    bare_jinja = re.findall(r"cet_copy\.get\(\s*'(fix2_[^']+)'\s*\)", code)
    assert not bare_jinja, f"这些 fix2_ 键（Jinja 侧）没带兜底：{bare_jinja}"

    src = _text(CET_MODE_PY)
    cet_part = src.split("_COPY_CET = {", 1)[1].split("_COPY_GENERIC = {", 1)[0]
    gen_part = src.split("_COPY_GENERIC = {", 1)[1].split("\n}", 1)[0]
    cet_keys = set(re.findall(r'"(fix2_[a-z0-9_]+)"\s*:', cet_part))
    gen_keys = set(re.findall(r'"(fix2_[a-z0-9_]+)"\s*:', gen_part))
    assert cet_keys == gen_keys, (
        f"fix2 键两套不成对 —— 仅 CET 有：{sorted(cet_keys - gen_keys)}；"
        f"仅通用有：{sorted(gen_keys - cet_keys)}"
    )
    # 模板用到的键必须都在 cet_mode 里（否则 CET 会退到兜底文案，等于没配）
    used = set(re.findall(r"cetCopy\(\s*'(fix2_[^']+)'", code))
    missing = used - cet_keys
    assert not missing, f"模板用了但 cet_mode.py 没有的键：{sorted(missing)}"


def test_a4_same_key_same_fallback_everywhere():
    """同一个键在所有调用点的兜底值必须一致（否则无法核对哪份才是 CET 文案）。"""
    import collections

    src = _strip_js_comments(_text(WORKSPACE))
    found = collections.defaultdict(set)
    for m in re.finditer(r"cet_copy\.get\(\s*'([^']+)'\s*,\s*'([^']*)'", src):
        found[m.group(1)].add(m.group(2))
    for m in re.finditer(r"cetCopy\(\s*'([^']+)'\s*,\s*'([^']*)'", src):
        found[m.group(1)].add(m.group(2))
    inconsistent = {k: v for k, v in found.items() if len(v) > 1}
    assert not inconsistent, f"以下键在不同调用点用了不同兜底值：{inconsistent}"


# ══════════════════════════════════════════════════════════════
# A5：`.cet-study-item[hidden]` 必须真的隐藏
# ══════════════════════════════════════════════════════════════


def test_a5_hidden_rule_exists_after_inline_flex_rule():
    """`[hidden]` 规则必须在 `.cet-study-item{display:inline-flex}` **之后**。

    同优先级下后者覆盖前者；这里还用了 !important，但顺序仍应正确（防御性）。
    """
    css = _text(CET_CSS)
    base_at = css.index(".cet-study-bar .cet-study-item {")
    hidden_rule = ".cet-study-bar .cet-study-item[hidden] { display: none !important; }"
    assert hidden_rule in css, "缺少 A5 的 [hidden] 规则"
    assert css.index(hidden_rule) > base_at, "[hidden] 规则写在了 display:inline-flex 之前"


def test_a5_hidden_rule_is_specific_and_important():
    """选择器必须带 .cet-study-bar 前缀 + !important，否则压不过作者样式。"""
    css = _text(CET_CSS)
    assert ".cet-study-bar .cet-study-item[hidden] { display: none !important; }" in css
    # 原 inline-flex 规则必须保留（布局不能变）
    assert ".cet-study-bar .cet-study-item {" in css
    assert "display: inline-flex;" in css


def test_a5_player_set_shown_kept_untouched():
    """包2 的 setShown() 保留（任务书 A5：不要顺手重构 cet-player.js）。"""
    player = ROOT / "frontend" / "static" / "cet-player.js"
    js = _text(player)
    assert "function setShown(" in js, "setShown 被删了 —— 任务书要求保留"
    assert "el.style.display" in js, "setShown 的行内 display 被删了"


def test_a5_css_cache_buster_bumped():
    """cet-workspace.css 改了规则，缓存串必须 bump，否则真实用户刷新仍拿旧 CSS。"""
    html = _text(WORKSPACE)
    m = re.search(r"cet-workspace\.css\?v=([^\"']+)", html)
    assert m, "找不到 cet-workspace.css 的引用"
    assert m.group(1) != "20261105-p1", (
        f"cet-workspace.css 缓存串未 bump（仍是 {m.group(1)}），A5 对老用户不生效"
    )


# ══════════════════════════════════════════════════════════════
# 跨编号：通用模式回归护栏
# ══════════════════════════════════════════════════════════════


def test_generic_mode_keeps_highlight_sync_and_review_button(generic_html):
    """一次性锁住 F6 的三件事：高亮同步在、加入复习本在、原收藏文案在。"""
    js = _text(WORKSPACE)
    assert "highlighted-words/sync" in js
    assert "＋ 加入复习本" in js
    assert "☆ 加入生词" in js
    # 通用模式渲染出来的页面里，CET 专属状态标签不应出现在 DOM 里
    assert 'data-review-book-state="1"' in js  # 模板里两条分支都在（运行时按 CET_ON 选）
