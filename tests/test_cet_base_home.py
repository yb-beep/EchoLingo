"""四六级底座 · 首页与入口（P1 页面 + P4 功能收束和导航 + 文案一致性）。

对应任务书 §4 P1 / §4 P4 与 §6 的 T01 / T02 / T12 / T14。
这里守住的是**模板与前端逻辑契约**；真实浏览器证据见
`docs/cet-base/功能收束清单.md` 的 T01/T02/T12/T14 记录。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "frontend" / "templates" / "index.html"
INDEX_TEXT = INDEX.read_text(encoding="utf-8")


def _visible_html(html: str) -> str:
    """剥掉 <script>/<style> 后的可见标记，用于「用户能看到什么」的文案断言。"""
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    return re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.S)


def _lessons_pane(html: str) -> str:
    """只取「听力材料」这个 pane，避免把收藏/设置 pane 的占位算成首屏占位。"""
    start = html.index('<div class="tab-pane active" id="tab-lessons">')
    end = html.index('<div class="tab-pane" id="tab-vocab">')
    return html[start:end]


def _render(monkeypatch, mode="1"):
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


# ── T01：空库启动可用、可导入、无虚假加载、无强制模型配置 ────
def test_t01_home_renders_without_any_material(cet_html):
    """空库（隔离库）首页必须能渲染，并给出明确导入入口。"""
    assert "四六级听力材料" in cet_html
    assert 'id="cet-import-panel"' in cet_html
    assert 'id="cet-audio-input"' in cet_html
    assert "导入听力材料" in cet_html
    # 空库的显式入口是首屏那一处
    assert 'id="cet-empty-library"' in cet_html


def test_t01_no_fake_loading_placeholders(cet_html):
    """首屏不得写死「加载中…」这类会永久悬挂的占位（任务书 P3 要求）。"""
    pane = _lessons_pane(cet_html)
    assert "加载中…" not in pane
    # 初始文案是真实的空态说明，而不是伪造的进度
    assert "正在读取材料…" in pane
    # 最近学习区块的初始文案同样是真实空态（由 cetRenderRecent 写入，模板里先占位）
    assert 'id="lesson-grid"' in pane


def test_t01_home_never_requires_model_configuration(cet_html):
    """基础链路不强制模型配置：CET 首页不出现 key 缺失告警。"""
    assert "未配置 Groq Key" not in cet_html
    assert "未配置千问 Key" not in cet_html
    assert "请先在设置 → API 设置填写" not in cet_html
    # 模型设置入口保留在设置页（任务书 P4 要求保留现有模型设置入口），
    # 但首页不出现、也不强推配置
    assert 'id="ai-settings-section"' not in _lessons_pane(cet_html)
    assert 'id="ai-settings-section"' in cet_html  # 仍在设置 pane 里


def test_t01_import_works_without_ai_key(cet_html):
    """导入不需要 key：界面文案明确「只有音频也能导入」。"""
    assert "只有音频也能导入并整段播放" in cet_html
    assert "不选也能导入" in cet_html
    assert "不伪造时间戳" in cet_html


# ── T02：级别分类、搜索、最近学习、待分类归类 ────────────────
def test_t02_level_switch_offers_cet4_cet6_and_unspecified(cet_html):
    assert 'data-cet-level="cet4"' in cet_html
    assert 'data-cet-level="cet6"' in cet_html
    assert 'data-cet-level="unspecified"' in cet_html
    assert "待分类" in cet_html


def test_t02_level_and_category_filters_are_present(cet_html):
    assert 'id="cet-level-filter"' in cet_html
    assert 'id="cet-category-filter"' in cet_html
    # 类别选项与后端 CATEGORIES 对齐
    for category in ("真题", "练习", "样例", "开发夹具"):
        assert category in cet_html


def test_t02_title_search_uses_material_fields_only():
    """搜索基于标题/标签/业务 ID，不看标题关键词猜级别。"""
    assert "function cetMatches(item)" in INDEX_TEXT
    block = INDEX_TEXT.split("function cetMatches(item)", 1)[1].split("function cetRenderTagFilter", 1)[0]
    assert "item.title" in block
    assert "item.tags" in block
    # 搜索不能改级别判定
    assert "item.level =" not in block


def test_t02_recent_learning_only_counts_real_progress():
    block = INDEX_TEXT.split("function cetRenderRecent()", 1)[1].split("function cetUpdateContinue", 1)[0]
    assert "item.progress?.started" in block
    assert "还没有学习记录" in block


def test_t02_unspecified_material_offers_manual_classification():
    """旧材料级别未知 → 「待分类」+ 手动归类入口，不能静默丢掉。"""
    assert "function cetManageMaterial" in INDEX_TEXT
    block = INDEX_TEXT.split("async function cetManageMaterial", 1)[1]
    assert "手动归类" in INDEX_TEXT
    assert "不按标题猜级别" in block
    # 归类只接受显式值，其他输入直接拒绝
    assert "只支持 cet4 / cet6 / 四级 / 六级，或留空表示待分类" in block


def test_t02_missing_metadata_shows_unspecified_not_guessed():
    """年份/月份/套次/题型本轮不落库 → 一律显示「未标注」。"""
    assert "function cetUndatedNote()" in INDEX_TEXT
    assert "年份 / 月份 / 套次 / 题型：未标注" in INDEX_TEXT
    # 不允许用标题正则猜年份/级别
    assert not re.search(r"title\.match\(/.*(cet|四级|六级)", INDEX_TEXT, re.I)


def test_t02_archive_is_recoverable():
    assert "取消归档" in INDEX_TEXT
    assert 'value="archived"' in INDEX_TEXT
    assert "之后可在「已归档」筛选里找回" in INDEX_TEXT


# ── T12：入口收束、保留按钮可用、无空白死路 ─────────────────
def test_t12_retired_entries_are_gone_from_cet_dom(cet_html):
    """任务书 §3.3 列出的入口在 CET 模式下不渲染。"""
    retired_markers = {
        "链接导入（B站/YouTube）": 'id="url-input"',
        "百度网盘": 'id="baidu-pan-link"',
        "本地文件选择（服务器路径）": 'id="browse-file-btn"',
        "文章/PDF 阅读课程": 'id="reading-text-input"',
        "文本生成朗读课程": 'id="text-tts"',
        "记忆故事": 'id="vocab-story-view"',
        "积分入口": 'id="credit-badge"',
        "登录入口": 'id="login-btn"',
        "个人档案": 'id="planning-tab-link"',
    }
    for label, marker in retired_markers.items():
        assert marker not in cet_html, f"CET 模式下不应出现「{label}」（{marker}）"


def test_t12_no_retired_copy_in_primary_flow(cet_html):
    """清理残余主流程文案（任务书 §4 P4）。"""
    visible = _visible_html(cet_html)
    for phrase in ("粘贴 B站", "粘贴 YouTube", "生成记忆故事", "雅思复用", "雅思注释", "记忆故事"):
        assert phrase not in visible, f"残余文案未清理：{phrase}"
    # 通用模式的「课程」口径已改为「听力材料」
    assert "＋ 新建课程" not in visible
    assert "全部课程" not in visible


def test_t12_kept_entries_are_present_and_usable(cet_html):
    """保留的能力必须在 DOM 里（不是被藏起来）。"""
    kept_markers = {
        "材料库列表": 'id="course-library-list"',
        "最近学习": 'id="lesson-grid"',
        "标题搜索": 'id="course-search"',
        "归档筛选": 'id="course-status-filter"',
        "分页": 'id="course-page-next"',
        "导入面板": 'id="cet-import-panel"',
        "继续学习": 'id="continue-lesson-link"',
        "设置页": 'id="tab-resources"',
    }
    for label, marker in kept_markers.items():
        assert marker in cet_html, f"保留能力缺失：{label}（{marker}）"


def test_t12_favorites_route_is_not_a_dead_link():
    """D-HOME-1：#favorites 是导航项，必须能切页。"""
    mode_js = (REPO / "frontend" / "static" / "cet-mode.js").read_text(encoding="utf-8")
    assert "'favorites': 'favorites'" in mode_js
    # switchTab 支持 favorites 这个导航级名字
    assert "if (name === 'favorites')" in INDEX_TEXT
    assert "cetFavoritesPane" in INDEX_TEXT


def test_t12_unknown_hash_falls_back_with_explanation():
    mode_js = (REPO / "frontend" / "static" / "cet-mode.js").read_text(encoding="utf-8")
    assert "RETIRED_HASH_HINT" in mode_js
    assert "已收束，已返回听力材料" in mode_js
    assert "没有这个页面，已返回听力材料" in mode_js
    # 兜底提示节点在首页存在
    assert 'id="cet-route-notice"' in INDEX_TEXT


def test_t12_switch_tab_never_lands_on_blank_pane():
    block = INDEX_TEXT.split("function switchTab(name, opts)", 1)[1].split(
        "const USER_WORDLIST_SELECTION_KEY", 1)[0]
    assert "if (!paneEl)" in block
    assert "switchTab('lessons', {keepHash: true})" in block


def test_t12_retired_modules_do_not_initialize(cet_html):
    """收束的模块不初始化 → 不发后台请求（不是 CSS 遮挡）。"""
    init_block = INDEX_TEXT.split("// ── Init ──", 1)[1].split("// ── 自动恢复运行中的 job", 1)[0]
    assert "if (CET.feature('settings_core')) loadAiSettings();" in init_block
    assert "if (CET_LINK_IMPORT) autoResumeJob();" in init_block
    assert "if (CET.feature('player') && CET_LINK_IMPORT) initBaiduPanCard();" in init_block
    # credits 请求在 CET 模式下不发（D-HOME-2）
    assert "CET.feature('credits_account')" in INDEX_TEXT


def test_t12_no_css_hiding_for_retired_modules():
    """禁止用 display:none 冒充收束。"""
    assert "el.remove();" in (REPO / "frontend" / "static" / "cet-mode.js").read_text(encoding="utf-8")
    # 模板里收束的卡用 Jinja 条件，不是 style="display:none"
    assert "{% if cet_features.link_import %}" in INDEX_TEXT
    assert "{% if cet_features.reading_course %}" in INDEX_TEXT
    assert "{% if cet_features.memory_story %}" in INDEX_TEXT


def test_t12_navigation_has_no_hidden_but_focusable_entries(cet_html):
    """隐藏按钮不得仍可 Tab 聚焦：收束项必须不在 DOM。"""
    nav_start = cet_html.index('<div class="tab-bar"')
    nav = cet_html[nav_start:cet_html.index("</div>", nav_start) + 6]
    assert 'style="display:none"' not in nav
    assert "tabindex" not in nav


# ── T14：布局不溢出、窄窗口可达 ─────────────────────────────
def test_t14_base_css_handles_narrow_viewports():
    css = (REPO / "frontend" / "static" / "cet-base.css").read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css
    assert "@media (max-width: 420px)" in css
    # 主要操作在窄窗口变成单列/整行，不被遮挡
    assert ".cet-import-grid { grid-template-columns: 1fr; }" in css
    assert ".cet-level-btn { flex: 1 1 100%; }" in css


def test_t14_no_fixed_width_overflow_risk():
    """新增节点不使用固定大宽度，避免 375px 横向溢出。"""
    css = (REPO / "frontend" / "static" / "cet-base.css").read_text(encoding="utf-8")
    assert "min-width: 0;" in css
    # 不允许出现硬编码大于 360px 的固定宽度
    for match in re.finditer(r"(?<!max-)(?<!min-)width:\s*(\d{3,})px", css):
        assert int(match.group(1)) <= 360, f"固定宽度过大，窄窗口会溢出：{match.group(0)}"


# ── 文案一致性（任务书 §3.2：不能只改首页主标题） ────────────
def test_copy_is_consistent_across_cet_surfaces(cet_html):
    """首页/材料库/设置/收藏的标题都走 cet_copy，不各写一套。"""
    assert "四六级听力" in cet_html                      # site_title
    assert "四六级听力材料" in cet_html                  # home_heading
    assert "听力材料" in cet_html                        # library_heading
    assert "词汇收藏" in cet_html                        # vocab_heading
    assert "句子收藏" in cet_html                        # sentences_heading
    # 模板里不允许硬编码第二套标题
    assert "<title>{{ cet_copy.site_title }}</title>" in INDEX_TEXT


def test_nav_and_copy_come_from_injected_config_not_hardcoded(cet_html):
    assert "{% for item in cet_nav %}" in INDEX_TEXT
    assert "{{ cet_copy.home_heading }}" in INDEX_TEXT
    assert "{{ cet_copy.library_heading }}" in INDEX_TEXT


def test_generic_mode_keeps_original_wording(generic_html):
    """ELT_CET_MODE=0 必须还是原版文案（原版回归）。"""
    assert "英语精学工作台" in generic_html
    assert "课程" in generic_html
    assert "词汇记忆工坊" in generic_html
    assert "句子库" in generic_html


# ── 导入界面契约（P2 的界面侧） ─────────────────────────────
def test_import_form_matches_backend_field_names(cet_html):
    """字段名照抄后端：audio / subtitle / title / level / category。"""
    assert "form.append('audio'" in INDEX_TEXT
    assert "form.append('subtitle'" in INDEX_TEXT
    assert "form.append('title'" in INDEX_TEXT
    assert "form.append('level'" in INDEX_TEXT
    assert "form.append('category'" in INDEX_TEXT


def test_import_uses_base_endpoints_with_fallback():
    assert "fetch('/api/cet/base/import'" in INDEX_TEXT
    assert "fetch('/api/cet/base/import/validate'" in INDEX_TEXT
    assert "fetch('/api/cet/base/import/formats'" in INDEX_TEXT
    # 端点缺失时降级到现有上传设施（不赌后端注册顺序）
    assert "async function cetImportFallback" in INDEX_TEXT
    assert "if (resp.status === 404)" in INDEX_TEXT


def test_import_shows_per_item_validation_errors():
    assert "function cetShowImportErrors" in INDEX_TEXT
    block = INDEX_TEXT.split("function cetShowImportErrors", 1)[1].split("function cetClearImportErrors", 1)[0]
    assert "err.line" in block
    assert "err.index" in block
    assert "第 ${err.line} 行" in block


def test_import_does_not_promise_untested_formats(cet_html):
    """格式清单来自后端 /api/cet/base/import/formats，界面不硬编码承诺。"""
    assert "cetImportFormatNote" in INDEX_TEXT
    block = INDEX_TEXT.split("function cetImportFormatNote", 1)[1].split("function cetOnAudioPicked", 1)[0]
    assert "data.audio" in block
    assert "data.subtitle" in block


# ── 我的收藏：词汇/句子共用一个导航项 ───────────────────────
def test_favorites_share_one_nav_entry(cet_html):
    assert "data-cet-favorites-pane" in cet_html
    assert cet_html.count('id="tab-btn-favorites"') == 1
    # 两个子页都在 DOM 里，由收藏导航统一进入
    assert 'id="tab-vocab"' in cet_html
    assert 'id="tab-sentences"' in cet_html


def test_cet_wordlist_options_are_cet4_cet6_first(cet_html):
    """默认展示 CET4/CET6 相关词表，其他词表不作主选项（任务书 §3.2）。"""
    assert 'id="vocab-exam-filter"' in cet_html
    block = cet_html.split('id="vocab-exam-filter"', 1)[1].split("</select>", 1)[0]
    assert "四级" in block and "六级" in block
    # 雅思/托福/GRE 等不再是 CET 主选项
    for other in ("雅思", "托福", "GRE", "考研"):
        assert other not in block, f"{other} 不应是 CET 模式主选项"


def test_settings_page_keeps_wordlist_and_model_entries(cet_html):
    """设置聚焦：词表可用性 + 现有模型设置入口（不改用户配置）。"""
    assert 'id="wordlist-config-list"' in cet_html
    assert 'id="wordlist-priority-row"' in cet_html
    assert 'id="ai-settings-section"' in cet_html
    # 不允许在 CET 收束里删掉用户已有的模型配置界面
    assert "loadAiSettings" in cet_html
