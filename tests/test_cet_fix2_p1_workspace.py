"""返修2 · 包1（工作区 workspace.html）静态与模板契约测试。

覆盖编号：P1-1 / P1-2 / P1-3 / P1-4 / P1-5 / P1-6，每个编号都同时断言：
    · CET 模式（ELT_CET_MODE=1）下新行为生效
    · 通用模式（ELT_CET_MODE=0）下**原版行为不变**

分工：
    - 本文件：不起服务，直接渲染模板 / 读模板与静态文件，跑得很快
    - tests/test_cet_fix2_p1_workspace_browser.py：真实服务 + 真实浏览器

为什么通用模式断言必须存在（任务书 §0.6）：
    CET 模式的每一条收束都可能顺手把原版功能砍掉。通用模式断言是那道闸门。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

WORKSPACE = ROOT / "frontend" / "templates" / "workspace.html"
CET_FAVORITES = ROOT / "frontend" / "static" / "cet-favorites.js"


def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


def _render_workspace(monkeypatch, cet_mode: str, *, lesson_mode: str = "listening") -> str:
    """用真实 FastAPI 应用渲染工作区页面，返回 HTML 文本。"""
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
    if lesson_mode == "listening":
        lesson = db.create_v2_lesson(
            source_type="cetMaterial",
            source_url="file://p1-fixture.mp3",
            title="P1 包1 测试材料",
            lesson_mode="listening",
        )
    else:
        lesson = db.create_v2_lesson(
            source_type="youtube",
            source_url="https://www.youtube.com/watch?v=p1fixture01",
            video_id="p1fixture01",
            title="P1 通用模式材料",
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
# P1-1（P0）：「逐句精听 →」入口
# ══════════════════════════════════════════════════════════════


def test_p1_1_intensive_entry_exists_in_cet_mode(cet_html):
    """CET 模式工作区必须有可见的「逐句精听 →」入口，且指向精听页。"""
    assert 'id="intensive-entry"' in cet_html, "CET 模式缺少逐句精听入口（P1-1 是 P0）"
    assert 'data-intensive-entry="1"' in cet_html
    assert re.search(r'href="/workspace/\d+/intensive"', cet_html), "入口未链接到精听页"
    assert "逐句精听" in cet_html
    # 入口必须落在播放控制条里（「精听」按钮旁），不是页面深处的孤岛
    controls = cet_html.split('class="learning-controls"', 1)
    assert len(controls) == 2, "未找到 learning-controls 区块"
    controls_block = controls[1].split("</div>\n\n      <section class=\"subtitle-card\"", 1)[0]
    assert 'id="intensive-entry"' in controls_block, "入口不在播放控制条内（应在「精听」按钮旁）"


def test_p1_1_intensive_entry_disabled_when_no_timeline(cet_html):
    """无逐句时间轴时入口禁用并提示「需要逐句时间轴」——可见但不可点。"""
    # 禁用文案与提示必须在页面上（运行时 syncIntensiveEntry() 依据真实时间轴切换）
    assert "需要逐句时间轴" in cet_html, "缺少「需要逐句时间轴」提示文案"
    assert 'id="intensive-entry-hint"' in cet_html
    js = _text(WORKSPACE)
    assert "function syncIntensiveEntry()" in js
    # 禁用态不是隐藏：aria-disabled + is-disabled，且去掉 href 防中键绕过
    assert "aria-disabled" in js
    assert "is-disabled" in js
    assert "entry.removeAttribute('href')" in js, "禁用态未去掉 href，可被中键/右键绕过"
    # 时间轴就绪后要能恢复可点
    assert "entry.setAttribute('href', `/workspace/${LESSON_ID}/intensive`)" in js


def test_p1_1_intensive_entry_not_duplicated_in_generic_mode(generic_html):
    """通用模式不新增这个入口（原版已有「跳转精学」，不能变成重复入口）。"""
    assert 'id="intensive-entry"' not in generic_html, (
        "通用模式出现了 CET 专属的「逐句精听 →」入口，违反「通用模式行为不变」"
    )
    # 原版入口必须在
    assert "跳转精学" in generic_html
    assert "openIntensiveExport" in generic_html


# ══════════════════════════════════════════════════════════════
# P1-2：「学习收集 Phase B」→「本课收藏」
# ══════════════════════════════════════════════════════════════


def test_p1_2_cet_collection_panel_title_is_user_facing(cet_html):
    """CET 模式收藏面板标题不得出现内部名「Phase B」。"""
    assert "本课收藏" in cet_html
    # 面板标题区不得再出现「学习收集」+「Phase B」
    panel_head = cet_html.split('data-sidebar-panel="collection"', 1)[1].split("</div>", 2)[0]
    assert "Phase B" not in panel_head, f"CET 收藏面板标题仍含内部名 Phase B：{panel_head[:200]}"
    assert "学习收集" not in panel_head


def test_p1_2_generic_collection_panel_title_unchanged(generic_html):
    """通用模式保留原版「学习收集 / Phase B」文案，字节级不变。"""
    assert "<h2>学习收集</h2>" in generic_html, "通用模式收藏面板标题被改动"
    assert "Phase B · 本课收藏的词、句子和标签。" in generic_html


# ══════════════════════════════════════════════════════════════
# P1-3：收藏自动回流「我的收藏」
# ══════════════════════════════════════════════════════════════


def _strip_js_comments(js: str) -> str:
    """剥掉 JS 的 // 行注释与 /* */ 块注释，只留可执行代码。

    断言「某段代码不存在」时不能被说明注释误伤 —— 返修2.1 删掉自动回流后，
    代码里仍有注释解释「这里不再调用 xxx」。
    """
    import re as _re
    js = _re.sub(r"/\*.*?\*/", "", js, flags=_re.S)
    return _re.sub(r"(?m)^\s*//.*$", "", js)


def test_p1_3_no_duplicate_activate_call_after_favorite(cet_html):
    """返修 2.1 A3：收藏成功后**不得**再额外调 `/api/vocab-review/activate`。

    背景（任务书 §0 前提更正）：返修 2 P1-3 在这里加了
    `if (CET_ON) await autoActivateFavoriteWord(...)`，依据是「工作区收藏进不了我的收藏」。
    该前提经复验是**错的** —— `POST /api/v2/lessons/{id}/word`（`save_word`）最后
    本来就调 `db.activate_word_review(...)`，收藏本身就是通的。

    再调一次 activate 等于「同一次收藏写两遍库」，多一次 POST，
    且让「收藏 1 个词 = 1 次 /word」的网络记录变成 2 次写请求。
    """
    js = _text(WORKSPACE)
    # 断言「可执行代码」里没有，而不是整个文件 —— A3 的说明注释会提到它
    code = _strip_js_comments(js)
    # 自动回流的函数与调用都已删除
    assert "function autoActivateFavoriteWord(" not in code, \
        "自动回流函数被加回来了 —— 返修2.1 A3 已删除（save_word 本来就激活）"
    assert "autoActivateFavoriteWord(" not in code, "自动回流的调用仍存在"
    # toggleWordSave 里不得再出现 activate 调用
    toggle = code.split("async function toggleWordSave", 1)[1].split(
        "async function addWordToReviewFromPopover", 1
    )[0]
    assert "/api/vocab-review/activate" not in toggle, \
        "toggleWordSave 收藏成功后仍在调 activate（重复写库）"
    # 但收藏请求本身必须保留（否则收藏功能就没了）
    assert "/api/v2/lessons/${LESSON_ID}/word" in toggle, "收藏请求本身被误删"
    # activate 端点本身必须保留（save_word 内部依赖 db.activate_word_review；
    # A3 删的是前端多余的第二次调用，不是这个端点）
    vocab_src = (ROOT / "backend" / "webapp" / "fastapi_routes" / "vocab.py").read_text(encoding="utf-8")
    assert '@router.post("/api/vocab-review/activate")' in vocab_src, "activate 端点被误删（应保留）"


def test_p1_3_cet_panel_shows_state_label_not_manual_button(cet_html):
    """CET 模式下收藏面板把「加入复习本」按钮换成状态标签。"""
    js = _text(WORKSPACE)
    # CET 分支渲染状态标签
    assert 'data-review-book-state="1"' in js
    assert "review-book-state" in js
    # 通用模式仍渲染原按钮（同一个模板里两条分支都在）
    assert 'class="review-book-btn"' in js
    # 原按钮文案保留在通用分支（老测试 test_v2_workspace_page.py:74 依赖）
    assert "加入复习本" in js


def test_p1_3_generic_mode_keeps_manual_review_button(generic_html):
    """通用模式保留「加入复习本」按钮与原行为（不自动回流）。"""
    js = _text(WORKSPACE)
    generic_branch = js.split("const action = CET_ON", 1)[1].split(";", 3)
    joined = ";".join(generic_branch)
    assert "review-book-btn" in joined, "通用模式分支丢了原「加入复习本」按钮"
    assert "addSavedWordToReview" in joined


# ══════════════════════════════════════════════════════════════
# P1-4：导出功能收束
# ══════════════════════════════════════════════════════════════


def test_p1_4_export_buttons_marked_for_cet_scope(cet_html):
    """「复制 Markdown」「导出 HTML」用未注册的 data-cet-feature 键收束。"""
    assert 'data-cet-feature="review_export"' in cet_html
    # 两个按钮都要标
    assert cet_html.count('data-cet-feature="review_export"') == 2, (
        "「复制 Markdown」「导出 HTML」应各带一个 data-cet-feature=review_export"
    )
    js = _text(WORKSPACE)
    # 收束机制是真移除（不是 CSS 遮挡）
    assert "el.remove();" in js
    # review_export 不能注册进 cet_mode（注册成 kept 会让按钮回到 CET 页面）
    cet_mode_src = (ROOT / "backend" / "webapp" / "cet_mode.py").read_text(encoding="utf-8")
    assert '"review_export"' not in cet_mode_src, (
        "review_export 不应注册进 cet_mode.py：未注册 key 才是「CET 收束、通用保留」"
    )


def test_p1_4_generic_mode_keeps_export_buttons(generic_html):
    """通用模式原版导出按钮与函数都在（收束只作用于 CET）。"""
    assert "复制 Markdown" in generic_html
    assert "导出 HTML" in generic_html
    assert "copyReviewMarkdown" in generic_html
    assert "exportReviewHtml" in generic_html
    assert "/review-export" in generic_html


# ══════════════════════════════════════════════════════════════
# P1-5：接口重复请求
# ══════════════════════════════════════════════════════════════


def test_p1_5_polling_only_starts_when_backend_still_working():
    """材料已 ready 时不得启动 2.5s 状态轮询（首跳会把整批接口再打一遍）。"""
    js = _text(WORKSPACE)
    assert "function needsSubtitlePolling()" in js
    # subtitlePoll 必须包在 needsSubtitlePolling() 判断里
    start_poll = js.split("function startPolling()", 1)[1].split("function updateStatusBadge", 1)[0]
    assert "if (needsSubtitlePolling())" in start_poll, (
        "startPolling 仍无条件启动状态轮询 → 首次打开会重复请求整批接口"
    )
    # 500ms 的本地时间轮询必须仍然无条件启动（当前句高亮跟随播放，不发网络请求）
    assert "timePoll = setInterval" in start_poll
    # 就绪分支要清掉遗留轮询
    assert "clearInterval(subtitlePoll);" in start_poll


def test_p1_5_highlight_sync_does_not_refetch_words_when_nothing_synced():
    """高亮同步 synced=0 时不得再拉一次 /words。"""
    js = _text(WORKSPACE)
    fn = js.split("async function syncHighlightedWordsAfterRender()", 1)[1].split("\n}", 1)[0]
    assert "data.synced" in fn, "未读取 synced 计数"
    assert "Number(data.synced || 0) > 0" in fn, "synced=0 时仍会重拉 /words"
    # 条件重拉，不是无条件
    assert "if (Number(data.synced || 0) > 0) await loadSavedWords();" in fn


def test_p1_5_workspace_broadcasts_snapshot_to_favorites():
    """workspace 把已加载的收藏数据派发给 cet-favorites.js（避免它重复拉）。"""
    js = _text(WORKSPACE)
    assert "function broadcastFavoritesSnapshot(" in js
    assert "function scheduleFavoritesBroadcast(" in js
    assert "'cet:favorites-changed'" in js
    # 派发里要带 words / sentences，cet-favorites 才能直接用
    fn = js.split("function broadcastFavoritesSnapshot(", 1)[1].split("\n}", 1)[0]
    assert "words:" in fn and "sentences:" in fn
    # meanings 必须是纯字符串（cet-favorites 直接当文案拼）
    assert "typeof cached === 'string'" in fn


def test_p1_5_favorites_reuses_snapshot_instead_of_refetching():
    """cet-favorites.js mount() 不再无条件 loadAll()。"""
    js = _text(CET_FAVORITES)
    assert "function applySnapshot(detail)" in js
    assert "function onFavoritesChanged(event)" in js
    mount = js.split("function mount()", 1)[1].split("function waitForSentencesThenRerender", 1)[0]
    # 首次加载不得无条件 loadAll()：只允许「3 秒兜底」里那一处条件调用
    assert mount.count("loadAll();") == 1, (
        f"mount() 里 loadAll() 出现 {mount.count('loadAll();')} 次，"
        "只应有 1 次（3 秒兜底）；无条件调用会重复拉 /words + /phase-b"
    )
    assert "if (!S.gotInitialData && !S.loading) loadAll();" in mount, (
        "mount() 里唯一的 loadAll() 必须是「快照没到才自己拉」的条件兜底"
    )
    assert "onFavoritesChanged" in mount
    # 写操作后仍要刷新（原事件监听保留）
    assert "addEventListener('cet:favorites-changed', onFavoritesChanged)" in js


def test_p1_5_favorites_still_refreshes_after_write_operations():
    """移除收藏后列表仍刷新（不能因为省请求把写操作后的刷新也砍了）。"""
    js = _text(CET_FAVORITES)
    # removeWord / removeSentence 走本地过滤 + 原页面 reload，不依赖 loadAll
    assert "S.words = S.words.filter(item => item !== word);" in js
    assert "S.sentences = S.sentences.filter(" in js
    # 兜底：workspace 派发缺失时自己拉一次
    assert "if (!S.gotInitialData && !S.loading) loadAll();" in js


# ══════════════════════════════════════════════════════════════
# P1-6：三个共享脚本按 feature 条件加载
# ══════════════════════════════════════════════════════════════


def test_p1_6_cet_drops_credits_and_voice_input_scripts(cet_html):
    """CET 模式工作区不输出 elt_credits.js 与 voice-input.js。"""
    assert "/static/elt_credits.js" not in cet_html, "CET 模式仍在加载 elt_credits.js"
    assert "/static/voice-input.js" not in cet_html, "CET 模式仍在加载 voice-input.js"


def test_p1_6_cet_keeps_natural_tts_conditionally(cet_html):
    """natural-tts.js 按 word_favorites / sentence_favorites 条件加载，CET 默认保留。"""
    assert "/static/natural-tts.js" in cet_html, (
        "natural-tts.js 服务收藏词卡发音（word_favorites）与句子朗读（sentence_favorites），"
        "两者 CET 都 kept=True，必须保留"
    )
    src = _text(WORKSPACE)
    assert "{% if cet_features.word_favorites or cet_features.sentence_favorites %}" in src, (
        "natural-tts.js 未按 feature 条件加载"
    )


def test_p1_6_generic_mode_keeps_all_three_scripts(generic_html):
    """通用模式三者都在，与改前一致。"""
    assert "/static/elt_credits.js" in generic_html
    assert "/static/natural-tts.js" in generic_html
    assert "/static/voice-input.js" in generic_html


def test_p1_6_cet_does_not_break_when_credits_script_absent(cet_html):
    """elt_credits.js 不输出时，页面不能因为 eltCredits 未定义而报错。"""
    js = _text(WORKSPACE)
    assert "function cetBillableFetch(" in js
    # 所有计费调用都走守卫：全模板只允许守卫内部那 1 处引用 eltCredits.billableFetch
    assert js.count("eltCredits.billableFetch(") == 1, (
        f"模板里有 {js.count('eltCredits.billableFetch(')} 处直接引用 eltCredits.billableFetch，"
        "只应保留 cetBillableFetch 守卫内那 1 处；CET 下脚本不加载会抛 ReferenceError"
    )
    assert js.count("await cetBillableFetch(") >= 3, "计费调用点未全部改走守卫"
    # 守卫要能降级为普通 fetch
    assert "return fetch(url, options);" in js


# ══════════════════════════════════════════════════════════════
# 缓存串 bump（包2 改 cet-player.js 后真实用户要能拿到新 JS）
# ══════════════════════════════════════════════════════════════


def test_cet_player_cache_buster_bumped():
    src = _text(WORKSPACE)
    # 卡点融合 W 包改了 cet-player.js 行为（§2 句播放接口），缓存串必须升到 drill-w。
    assert "/static/cet-player.js?v=20261107-drill-w" in src, (
        "cet-player.js 缓存串未 bump 到 20261107-drill-w，真实用户刷新仍拿到旧 JS"
    )
    player_line = src.split("cet-player.js", 1)[1].split(">", 1)[0]
    assert "20261106-fix2" not in player_line, "旧的 cet-player.js 缓存串仍在模板里"


def test_cet_favorites_cache_buster_bumped():
    """cet-favorites.js 也改了行为（P1-5 快照改造），缓存串必须一起 bump。

    只 bump cet-player.js 会让 P1-5 的修复对**老用户不生效**：
    浏览器继续用缓存里的旧 cet-favorites.js，它仍会自己拉 /words + /phase-b，
    重复请求照旧存在。
    """
    src = _text(WORKSPACE)
    assert "/static/cet-favorites.js?v=20261106-fix2" in src, (
        "cet-favorites.js 缓存串未 bump，P1-5 的修复对老用户不生效"
    )
    tag = src.split("cet-favorites.js", 1)[1].split(">", 1)[0]
    assert "?v=20261105-p1" not in tag, f"cet-favorites.js 仍是旧缓存串：{tag}"


def test_copy_fallbacks_are_consistent_per_key():
    """同一个文案键在所有调用点的兜底值必须一致。

    为什么要有这条：主代理合并时看到 `fix2_intensive_entry` 在 title 与可见文案
    用了两个不同兜底（'逐句精听' vs '逐句精听 →'），无法判断哪份才是 CET 文案，
    核对包4 写入 cet_mode.py 的键值时会出错。
    """
    import collections

    src = _text(WORKSPACE)
    found = collections.defaultdict(set)
    # Jinja: cet_copy.get('key', 'fallback')
    for m in re.finditer(r"cet_copy\.get\(\s*'([^']+)'\s*,\s*'([^']*)'", src):
        found[m.group(1)].add(m.group(2))
    # JS: cetCopy('key', 'fallback')
    for m in re.finditer(r"cetCopy\(\s*'([^']+)'\s*,\s*'([^']*)'", src):
        found[m.group(1)].add(m.group(2))
    assert found, "没有扫描到任何文案取值调用，正则可能失效"
    inconsistent = {k: v for k, v in found.items() if len(v) > 1}
    assert not inconsistent, (
        f"以下键在不同调用点用了不同兜底值，包4 无法核对：{inconsistent}"
    )


def test_copy_fallbacks_cover_all_fix2_keys_used():
    """所有 fix2_ 键都必须带兜底（键没合进来时页面不白屏）。"""
    src = _text(WORKSPACE)
    bare = re.findall(r"cet_copy\.get\(\s*'(fix2_[^']+)'\s*\)", src)
    assert not bare, f"这些 fix2_ 键取值没带兜底：{bare}"
    bare_js = re.findall(r"cetCopy\(\s*'(fix2_[^']+)'\s*\)", src)
    assert not bare_js, f"这些 fix2_ 键取值没带兜底（JS 侧）：{bare_js}"
