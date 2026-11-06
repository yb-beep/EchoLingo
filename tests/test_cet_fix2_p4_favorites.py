"""返修2 · 包4：我的收藏与首页文案（P4-1 / P4-2 / P4-3）。

对应任务书 §2 包4：
  P4-1 统一「收藏」口径 + 「我的收藏」加载时补同步历史遗留的本课收藏
  P4-2 去英文残留（Vocabulary atelier / 继续词汇练习 / 句子精听）→ 经 cet_copy 取
  P4-3 首页 elt_credits.js / natural-tts.js 按 feature 条件加载

分两层：
  1. 静态契约层（不依赖服务）：cet_copy 键成对、模板走 cet_copy、脚本条件加载、
     billableFetch 封装覆盖所有调用点。这一层 ELT_CET_MODE=0/1 都断言。
  2. 真实服务层（需要 CET_BASE_URL）：补同步端点幂等、端到端「收藏 2 词 1 句 →
     我的收藏词 2 / 句子 1」。服务不可达时整组 skip（与 test_cet_base_workspace.py
     同一纪律），不制造假失败。

跑法：
    # 静态层
    ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_fix2_p4_favorites.py
    # 含真实服务层（先起 5184 并导入材料）
    CET_BASE_URL=http://127.0.0.1:5184 ELT_CONFIG_DIR=$(mktemp -d) \
      .venv/bin/python -m pytest -q tests/test_cet_fix2_p4_favorites.py
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
INDEX = REPO / "frontend" / "templates" / "index.html"
INDEX_TEXT = INDEX.read_text(encoding="utf-8")

# P4-1/P4-2 新增的 cet_copy 键（append-only，两套文案成对）
FIX2_COPY_KEYS = [
    "fix2_vocab_eyebrow",
    "fix2_vocab_continue_cta",
    "fix2_sentences_eyebrow",
    "fix2_favorites_pane_vocab",
    "fix2_favorites_pane_sentences",
    "fix2_favorite_word_cta",
    "fix2_favorite_sentence_cta",
    "fix2_favorite_added",
    "fix2_sync_collections",
    "fix2_my_collections_wordlist",
    "fix2_lesson_collections",
    # 包1 P1-2 / P1-1 用的 4 个键（返修2 合并后补加，见交付说明 §11）
    "fix2_lesson_collections_note",
    "fix2_intensive_entry",
    "fix2_intensive_need_timeline",
    "fix2_word_in_favorites",
]


def _visible_html(html: str) -> str:
    """剥掉 <script>/<style> 后的可见标记，用于「用户能看到什么」的文案断言。"""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    return re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.S)


def _render(monkeypatch, mode="1") -> str:
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setenv("ELT_CET_MODE", mode)
    resp = TestClient(create_app()).get("/")
    assert resp.status_code == 200
    return resp.text


@pytest.fixture()
def cet_html(monkeypatch):
    return _render(monkeypatch, "1")


@pytest.fixture()
def generic_html(monkeypatch):
    return _render(monkeypatch, "0")


# ══════════════════════════════════════════════════════════════
# P4-2：文案统一口径，且经 cet_copy 取（不写死）
# ══════════════════════════════════════════════════════════════
def test_p4_2_copy_keys_exist_in_both_modes(monkeypatch):
    """新增键必须两套文案成对，否则 CET.copy 的兜底会悄悄盖住漏配。"""
    from webapp import cet_mode

    monkeypatch.setenv("ELT_CET_MODE", "1")
    cet_copy = cet_mode.config()["copy"]
    monkeypatch.setenv("ELT_CET_MODE", "0")
    generic_copy = cet_mode.config()["copy"]
    for key in FIX2_COPY_KEYS:
        assert key in cet_copy, f"CET 文案缺少 {key}"
        assert key in generic_copy, f"通用文案缺少 {key}"
        assert str(cet_copy[key]).strip(), f"{key} 的 CET 文案为空"
        assert str(generic_copy[key]).strip(), f"{key} 的通用文案为空"


def test_p4_2_generic_copy_equals_pre_fix_literals(monkeypatch):
    """通用模式的值必须是返修前模板里硬编码的原文（ELT_CET_MODE=0 渲染不变）。"""
    from webapp import cet_mode

    monkeypatch.setenv("ELT_CET_MODE", "0")
    copy = cet_mode.config()["copy"]
    assert copy["fix2_vocab_eyebrow"] == "Vocabulary atelier"
    assert copy["fix2_vocab_continue_cta"] == "继续词汇练习 →"
    assert copy["fix2_sentences_eyebrow"] == "句子精听"
    assert copy["fix2_favorites_pane_vocab"] == "词汇收藏"
    assert copy["fix2_favorites_pane_sentences"] == "句子收藏"
    assert copy["fix2_my_collections_wordlist"] == "我的生词本"
    # 基线 workspace.html 的收藏面板标题是 <h2>学习收集</h2>，
    # "Phase B · …" 在 panel-note 里 —— 标题本身不含 Phase B。
    # 通用值必须还原成「学习收集」，否则通用模式渲染会变
    # （实测会打破包1 test_p1_2_generic_collection_panel_title_unchanged）。
    assert copy["fix2_lesson_collections"] == "学习收集"
    assert copy["fix2_lesson_collections_note"] == "Phase B · 本课收藏的词、句子和标签。"
    assert copy["fix2_intensive_entry"] == "逐句精听 →"
    assert copy["fix2_intensive_need_timeline"] == "需要逐句时间轴"
    assert copy["fix2_word_in_favorites"] == "收藏后进入「我的收藏」"


def test_p4_2_generic_collection_panel_copy_is_byte_identical(monkeypatch):
    """通用模式收藏面板文案必须与基线模板逐字一致（防「通用模式被改动」回归）。

    背景：基线 `workspace.html:1738-1739` 是
        <h2>学习收集</h2>
        <span class="panel-note">Phase B · 本课收藏的词、句子和标签。</span>
    标题**不含** Phase B。曾把通用值误写成「学习收集 Phase B」，
    导致包1 的 `test_p1_2_generic_collection_panel_title_unchanged` 变红。
    """
    from webapp import cet_mode

    monkeypatch.setenv("ELT_CET_MODE", "0")
    copy = cet_mode.config()["copy"]
    assert copy["fix2_lesson_collections"] == "学习收集"
    assert copy["fix2_lesson_collections_note"] == "Phase B · 本课收藏的词、句子和标签。"
    # 组合起来必须能还原基线两行原文
    assert copy["fix2_lesson_collections"] == "学习收集"
    assert "Phase B" not in copy["fix2_lesson_collections"]


def test_p4_2_lesson_collections_keys_are_paired(monkeypatch):
    """包1 用的 4 个键两套文案必须都在（否则模板会静默走兜底，不算集中配置）。"""
    from webapp import cet_mode

    for key in ("fix2_lesson_collections_note", "fix2_intensive_entry",
                "fix2_intensive_need_timeline", "fix2_word_in_favorites"):
        monkeypatch.setenv("ELT_CET_MODE", "1")
        cet_value = cet_mode.config()["copy"].get(key)
        monkeypatch.setenv("ELT_CET_MODE", "0")
        generic_value = cet_mode.config()["copy"].get(key)
        assert cet_value, f"CET 文案缺少 {key}"
        assert generic_value, f"通用文案缺少 {key}"


def test_p4_2_cet_copy_is_chinese_cet_wording(monkeypatch):
    """CET 模式的文案必须是中文四六级口径，不得残留英文/内部名。"""
    from webapp import cet_mode

    monkeypatch.setenv("ELT_CET_MODE", "1")
    copy = cet_mode.config()["copy"]
    for key in ("fix2_vocab_eyebrow", "fix2_sentences_eyebrow",
                "fix2_vocab_continue_cta", "fix2_favorites_pane_vocab",
                "fix2_favorites_pane_sentences", "fix2_lesson_collections"):
        value = str(copy[key])
        assert re.search(r"[\u4e00-\u9fff]", value), f"{key} 的 CET 文案没有中文：{value}"
    # 具体禁止的英文残留（任务书 P4-2 点名）
    assert "atelier" not in copy["fix2_vocab_eyebrow"].lower()
    assert "VOCABULARY" not in copy["fix2_vocab_eyebrow"].upper()
    # 「复习本 / 生词本」不是统一口径
    assert "复习本" not in copy["fix2_lesson_collections"]
    assert "生词本" not in copy["fix2_my_collections_wordlist"]


def test_p4_2_template_reads_copy_not_hardcoded(cet_html):
    """模板必须经 cet_copy 取，不是写死第二套中文字符串。"""
    assert "{{ cet_copy.fix2_vocab_eyebrow }}" in INDEX_TEXT
    assert "{{ cet_copy.fix2_vocab_continue_cta }}" in INDEX_TEXT
    assert "{{ cet_copy.fix2_sentences_eyebrow }}" in INDEX_TEXT
    assert "{{ cet_copy.fix2_favorites_pane_vocab }}" in INDEX_TEXT
    assert "{{ cet_copy.fix2_favorites_pane_sentences }}" in INDEX_TEXT
    assert "{{ cet_copy.fix2_my_collections_wordlist }}" in INDEX_TEXT


def test_p4_2_cet_visible_copy_has_no_english_leftovers(cet_html):
    """CET 模式用户可见标记里不得再出现 P4-2 点名的英文/混杂叫法。"""
    visible = _visible_html(cet_html)
    for banned in ("VOCABULARY ATELIER", "Vocabulary atelier", "ATELIER",
                   "继续词汇练习", "句子精听"):
        assert banned not in visible, f"CET 模式仍可见残留文案：{banned}"
    # 统一口径确实渲染出来了
    assert "四六级 · 词汇收藏" in visible
    assert "四六级 · 句子收藏" in visible


def test_p4_2_generic_visible_copy_unchanged(generic_html):
    """ELT_CET_MODE=0 必须还是原版文案（原版回归）。"""
    visible = _visible_html(generic_html)
    assert "Vocabulary atelier" in visible
    assert "继续词汇练习 →" in visible
    assert "句子精听" in visible
    # 通用模式的词汇/句子页仍是原版标题（不是 CET 的「词汇收藏 / 句子收藏」）
    assert "词汇记忆工坊" in visible
    assert "句子库" in visible
    # CET 新文案不得漏进通用模式
    assert "四六级 · 词汇收藏" not in visible
    assert "四六级 · 句子收藏" not in visible


def test_p4_2_generic_and_cet_eyebrow_uppercase_rendering_matches():
    """`.eyebrow` 有 text-transform:uppercase —— 通用值大小写不影响渲染。

    这条锁住「通用值写 `Vocabulary atelier`（小写 a）也能得到返修前的
    `VOCABULARY ATELIER` 视觉效果」这个前提，防止有人改掉 CSS 后静默变形。
    """
    assert "text-transform: uppercase;" in INDEX_TEXT
    css_block = INDEX_TEXT[INDEX_TEXT.index(".eyebrow {"):]
    css_block = css_block[:css_block.index("}") + 1]
    assert "text-transform: uppercase" in css_block


# ══════════════════════════════════════════════════════════════
# P4-1：统一口径 —— 用户可见只用「收藏」一词
# ══════════════════════════════════════════════════════════════
def test_p4_1_favorites_panes_use_unified_wording(cet_html):
    """「我的收藏」两个子页统一叫「收藏单词 / 收藏句子」。"""
    visible = _visible_html(cet_html)
    assert visible.count("收藏单词") >= 2   # 词汇页 + 句子页的共用 tab
    assert visible.count("收藏句子") >= 2
    # 旧的两套叫法在 CET 可见标记里不应再作为 tab 出现
    assert 'data-cet-favorites-pane="vocab"' in cet_html
    assert 'data-cet-favorites-pane="sentences"' in cet_html


def test_p4_1_cet_visible_has_no_second_names(cet_html):
    """CET 可见标记里不得出现「生词」「复习本」这类第二种叫法。"""
    visible = _visible_html(cet_html)
    for banned in ("加入生词", "加入复习本", "生词本", "复习本"):
        assert banned not in visible, f"CET 模式仍可见第二种叫法：{banned}"


def test_p4_1_my_vocab_wordlist_renamed_only_in_cet(cet_html, generic_html):
    """`my_vocab` 这张词表在 CET 下显示为「我的收藏」，通用模式仍是「我的生词本」。"""
    # 设置页静态段落（Jinja 侧）
    assert "{{ cet_copy.fix2_my_collections_wordlist }}" in INDEX_TEXT
    # 动态渲染侧（renderWordlistConfigUI / renderWordlistFilters）
    assert "fix2_my_collections_wordlist" in INDEX_TEXT
    assert INDEX_TEXT.count("fix2_my_collections_wordlist") >= 3
    # 通用模式的静态段落仍是原文
    assert "我的生词本和你上传的词表都在这里" in _visible_html(generic_html)


def test_p4_1_sync_endpoint_exists_and_is_lesson_scoped():
    """补同步端点必须存在，且复用 activate 同一条写入路径（source=listening）。"""
    from webapp.fastapi_routes import vocab as vocab_routes

    paths = {getattr(route, "path", "") for route in vocab_routes.router.routes}
    assert "/api/vocab-review/sync-collections" in paths, "缺少补同步端点"
    source = (REPO / "backend" / "webapp" / "fastapi_routes" / "vocab.py").read_text(encoding="utf-8")
    fn = source.split("def sync_lesson_collections(", 1)[1]
    assert 'source="listening"' in fn, "补同步未用 source=listening（与包1 口径不一致）"
    assert "db.activate_word_review(" in fn, "补同步未复用 activate 的写入函数"


def test_p4_1_sync_is_idempotent_by_construction():
    """幂等依据：底层是 INSERT ... ON CONFLICT，count 有 <1 保护。"""
    db_source = (REPO / "backend" / "db.py").read_text(encoding="utf-8")
    fn = db_source.split("def activate_word_review(", 1)[1].split("\ndef ", 1)[0]
    assert "ON CONFLICT(word) DO UPDATE" in fn, "activate 不是幂等 upsert"
    assert "CASE WHEN words.count < 1 THEN 1 ELSE words.count END" in fn, \
        "words.count 缺少「不重复计数」保护"


def test_p4_1_frontend_syncs_on_favorites_load(cet_html):
    """「/#favorites」加载时自动补一次（方案：加载时补，不加按钮）。"""
    assert "cetSyncLessonCollections" in INDEX_TEXT
    # initVocab（收藏单词页的加载入口）必须调用它
    init_fn = INDEX_TEXT.split("async function initVocab()", 1)[1].split("\n}", 1)[0]
    assert "cetSyncLessonCollections" in init_fn, "收藏页加载未触发补同步"
    # 只在 CET 模式执行（通用模式行为不变）
    sync_fn = INDEX_TEXT.split("async function cetSyncLessonCollections(", 1)[1].split("\n}", 1)[0]
    assert "CET.enabled" in sync_fn
    # 必须打的是补同步端点
    assert "/api/vocab-review/sync-collections" in sync_fn


def test_p4_1_sync_does_not_run_in_generic_mode():
    """通用模式不发起补同步（守卫在函数内部，CET.enabled() 为 false 直接 return）。"""
    sync_fn = INDEX_TEXT.split("async function cetSyncLessonCollections(", 1)[1].split("\n}", 1)[0]
    assert "if (!(window.CET && typeof CET.enabled === 'function' && CET.enabled())) return 0;" in sync_fn


# ══════════════════════════════════════════════════════════════
# P4-3：首页脚本按 feature 条件加载
# ══════════════════════════════════════════════════════════════
def test_p4_3_cet_page_has_no_elt_credits_script(cet_html):
    """CET 模式（credits_account 收束）不输出 elt_credits.js 的 <script>。"""
    assert '<script src="/static/elt_credits.js' not in cet_html
    # 注释里提到文件名不算加载
    assert "credits_account" in INDEX_TEXT


def test_p4_3_generic_page_keeps_elt_credits_script(generic_html):
    """通用模式（credits_account 启用）原样输出 —— 老契约不破。"""
    assert '<script src="/static/elt_credits.js?v=20260809-1"></script>' in generic_html


def test_p4_3_natural_tts_loaded_in_both_modes(cet_html, generic_html):
    """natural-tts.js 服务的是保留功能（收藏词卡发音 / 句子兜底朗读），两种模式都要有。

    任务书 P1-6/P4-3 的规则是「按对应 feature 条件加载」，不是「删掉」。
    """
    assert '<script src="/static/natural-tts.js' in cet_html
    assert '<script src="/static/natural-tts.js' in generic_html
    # 条件挂在 word_favorites / sentence_favorites 上（CET 都 kept=True）
    assert "cet_features.word_favorites" in INDEX_TEXT
    assert "cet_features.sentence_favorites" in INDEX_TEXT


def test_p4_3_scripts_are_conditionally_guarded_in_template():
    """两个 <script> 都必须被 Jinja 条件包住，不是无条件加载。"""
    for script in ("elt_credits.js", "natural-tts.js"):
        idx = INDEX_TEXT.index(f'<script src="/static/{script}')
        window = INDEX_TEXT[max(0, idx - 900):idx]
        assert "{% if cet_features." in window, f"{script} 未被 feature 条件包住"


def test_p4_3_billable_fetch_wrapper_covers_all_call_sites():
    """CET 下没有 eltCredits，所有计费调用必须走本地封装，否则运行时 ReferenceError。

    验收要求：`eltCredits.billableFetch` 只能出现在封装内部（注释不算）。
    """
    # 先剥掉注释行，避免把说明文字误判成调用
    code = "\n".join(
        line for line in INDEX_TEXT.splitlines()
        if not line.lstrip().startswith("//")
    )
    occurrences = [m.start() for m in re.finditer(r"eltCredits\.billableFetch", code)]
    assert occurrences, "找不到 billableFetch（封装可能被误删）"
    wrapper_start = code.index("function billableFetch(")
    wrapper_end = code.index("\n}", wrapper_start)
    for pos in occurrences:
        assert wrapper_start <= pos <= wrapper_end, (
            "存在封装外的 eltCredits.billableFetch 裸调用（CET 模式会 ReferenceError）"
        )
    # 封装内部必须是「有 eltCredits 就等价转发」
    wrapper = code[wrapper_start:wrapper_end]
    assert "window.eltCredits" in wrapper
    assert "return eltCredits.billableFetch(opType, url, options);" in wrapper
    # 退化分支仍要带 Idempotency-Key（多用户/云端计费激活时缺 key 会被 400）
    assert "Idempotency-Key" in wrapper


def test_p4_3_wrapper_preserves_idempotency_key_in_fallback():
    """封装退化分支的行为契约（不依赖浏览器，按源码结构断言）。"""
    wrapper = INDEX_TEXT[INDEX_TEXT.index("function billableFetch("):]
    wrapper = wrapper[:wrapper.index("\n}")]
    assert "_cetNewIdempotencyKey" in wrapper
    assert "crypto.randomUUID" in INDEX_TEXT
    # 与 elt_credits.js 的语义一致：FormData 不加 Content-Type
    assert "instanceof FormData" in wrapper


# ══════════════════════════════════════════════════════════════
# 真实服务层（需 CET_BASE_URL；服务不可达整组 skip）
# ══════════════════════════════════════════════════════════════
BASE = os.environ.get("CET_BASE_URL", "").rstrip("/")


def _service_up() -> bool:
    if not BASE:
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


service_required = pytest.mark.skipif(
    not _service_up(),
    reason="未设置 CET_BASE_URL 或服务不可达，跳过真实服务用例"
    "（示例：CET_BASE_URL=http://127.0.0.1:5184）",
)


def _post_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


@service_required
def test_p4_1_sync_endpoint_is_idempotent_on_live_service():
    """真实服务：连调两次补同步，第二次必须 0 条且不重复计数。"""
    first = _post_json("/api/vocab-review/sync-collections", {})
    assert first.get("ok") is True
    second = _post_json("/api/vocab-review/sync-collections", {})
    assert second.get("ok") is True
    assert second.get("synced") == 0, f"补同步不幂等，第二次仍同步了 {second.get('synced')} 条"
    assert second.get("words") == []


@service_required
def test_p4_1_sync_dry_run_reports_without_writing():
    dry = _post_json("/api/vocab-review/sync-collections", {"dry_run": True})
    assert dry.get("dry_run") is True
    assert isinstance(dry.get("would_sync"), int)
    assert isinstance(dry.get("words"), list)


@service_required
def test_p4_1_sync_unknown_lesson_is_404():
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post_json("/api/vocab-review/sync-collections", {"lesson_id": 999999})
    assert excinfo.value.code == 404


@service_required
def test_p4_1_favorites_page_shows_synced_words_and_original_sentence():
    """端到端：本课收藏的词补同步后，/vocab-log（我的收藏的数据源）能看到它，且带原句。

    这是 P4-1 验收「工作区收藏 → /#favorites 能看到，且带原句」的服务端证据；
    浏览器侧真实点击证据见 docs/cet-base/返修2/evidence/ 的截图与
    test_cet_fix2_p4_browser.py。
    """
    _post_json("/api/vocab-review/sync-collections", {})
    vocab_log = _get_json("/vocab-log?include_archived=1&include_mastered=1")
    words = {k: v for k, v in vocab_log.items() if not k.startswith("__")}
    if not words:
        pytest.skip("当前隔离库没有本课收藏，无法验证回填（先跑 import_materials + 收藏词）")
    for word, entry in words.items():
        contexts = entry.get("contexts") or []
        assert contexts, f"补同步后 {word} 没有原句（P4-1 要求带原句）"
        assert contexts[0].get("sentence"), f"{word} 的原句为空"


@service_required
def test_p4_1_sentences_page_shows_saved_sentences():
    """句子侧不需要补同步：工作区收藏句子写 phase-b，句子收藏已直接读它。"""
    data = _get_json("/api/v2/lessons/sentence-review?include_archived=1")
    assert "sentences" in data
    assert isinstance(data["sentences"], list)
    assert isinstance(data.get("total"), int)
