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
    # 只统计真实进度（不变）
    assert "item.progress?.started" in block
    # L1：没有学习记录时整块隐藏，不再显示「还没有学习记录」占位大空白
    # （上方「继续学习」已承担入口作用）
    assert "recent-section" in block, "cetRenderRecent 没有控制「最近学习」区块显隐"
    assert "section.hidden = started.length === 0" in block, "无记录时没有隐藏整块"


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
    # L2：手机上级别按钮改成一行横排 + 横向滑动（原来每个占一整行，吃掉半屏）
    assert ".cet-level-switch" in css and "flex-wrap: nowrap" in css, "级别按钮没有改成一行横排"
    assert "overflow-x: auto" in css, "级别按钮没有横向滑动"
    assert ".cet-level-btn { flex: 1 1 100%; }" not in css, "仍在强制级别按钮占满整行"


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


# ══════════════════════════════════════════════════════════════
# BASE-06：功能收束必须覆盖**动态渲染**、筛选、旧链接和初始化请求
# 验收方实测（用户可见，不是隐藏 JS 字符串误报）：进入「我的收藏」加载后
# 仍有 GRE / 雅思 / 考研 / 托福 标签按钮、「全部造句情况」筛选、
# 「情景造句练习」按钮；句子收藏保留句式练习筛选；学习页仍叫「阅读」「加入精读」；
# 首页仍真实请求 /api/v2/lessons/baidu-pan/capability。
# ══════════════════════════════════════════════════════════════


def test_base06_retired_exam_tags_are_not_rendered_in_cet(cet_html):
    """收藏页「考试标签」筛选：CET 模式只留四级/六级。"""
    block = cet_html.split('id="vocab-exam-filter"', 1)[1].split("</select>", 1)[0]
    for other in ("雅思", "托福", "GRE", "考研"):
        assert other not in block, f"收藏页仍渲染退役考试标签：{other}"


def test_base06_dynamic_wordlist_filters_are_scoped(cet_html):
    """关键回归：GRE/雅思/考研/托福 是 initVocab() 后**动态追加**的按钮。

    静态 Jinja 条件管不到，必须由 renderWordlistFilters() 按同一份集中配置过滤。
    验收方实测的正是这一类「动态渲染漏收」。
    """
    assert "CET_VOCAB_FILTER_KEYS" in cet_html, "缺少动态词表筛选白名单"
    assert "cetVocabFilterAllowed" in cet_html, "缺少动态词表筛选判定函数"
    # renderWordlistFilters 必须真的用它过滤，而不是照单全收
    fn = cet_html.split("function renderWordlistFilters(", 1)[1].split("\n}", 1)[0]
    assert "cetVocabFilterAllowed" in fn, "renderWordlistFilters 未按集中配置过滤"
    assert ".filter(" in fn
    # 白名单只含 CET 相关词表
    keys_block = cet_html.split("CET_VOCAB_FILTER_KEYS = [", 1)[1].split("]", 1)[0]
    assert "builtin_cet4" in keys_block and "builtin_cet6" in keys_block
    for retired in ("builtin_gre", "builtin_ielts", "builtin_ky", "builtin_toefl"):
        assert retired not in keys_block, f"退役词表仍在 CET 白名单：{retired}"


def test_base06_exam_tags_on_word_cards_are_scoped(cet_html):
    """词卡上的 exam_tags 来自 ECDICT，同一单词会挂多个考试标签。"""
    assert "CET_EXAM_TAGS" in cet_html
    assert "cetExamTagVisible" in cet_html
    tags_block = cet_html.split("CET_EXAM_TAGS = [", 1)[1].split("]", 1)[0]
    assert "四级" in tags_block and "六级" in tags_block
    for retired in ("雅思", "托福", "GRE", "考研"):
        assert retired not in tags_block, f"词卡仍显示退役考试标签：{retired}"
    # 词卡渲染处必须真的调用过滤，而不是保留原数组
    assert "entry.exam_tags || []" in cet_html
    assert ".filter(cetExamTagVisible)" in cet_html, "词卡 exam_tags 未过滤"


def test_base06_scenario_filters_are_not_rendered_in_cet(cet_html):
    """「按造句情况筛选」属已收束的情境造句（scenario_sentence）。"""
    assert 'id="vocab-practice-filter"' not in cet_html, "CET 模式仍渲染「全部造句情况」筛选"
    assert "全部造句情况" not in cet_html


def test_base06_sentence_pattern_filter_is_not_rendered_in_cet(cet_html):
    """句子收藏的「按句式练习筛选」属已收束的句式训练。"""
    assert 'id="sentence-pattern-filter"' not in cet_html, "CET 模式句子收藏仍保留句式练习筛选"
    assert "全部句式状态" not in cet_html


def test_base06_scenario_practice_entry_points_are_not_rendered_in_cet(cet_html):
    """「情景造句练习」/「AI 情景造句」/「主动造句」入口在 CET 模式不渲染。

    注意断言方式：这些文案在 CET 模式**仍存在于 JS 模板字面量源码里**
    （按 ``${CET.feature('scenario_sentence') ? ... : ''}`` 条件拼装），
    这属于「不物理删除通用源码」的约定，不是用户可见入口。
    验收方明确说过「不是隐藏 JS 字符串误报」，所以这里断言的是
    **渲染条件结构**，真实 DOM 证据由浏览器实测给出（见收束清单 §九）。
    """
    # 每处入口都必须被 scenario_sentence 条件包住
    for entry in ("情景造句练习", "AI 情景造句", "开始自由造句"):
        idx = cet_html.index(entry)
        # 往前找最近的 CET.feature 判定，必须在同一段模板表达式内
        window = cet_html[max(0, idx - 2000):idx]
        assert "CET.feature('scenario_sentence')" in window, \
            f"「{entry}」未被 scenario_sentence 条件收束"
    # 静态区块直接由 Jinja 条件包住（不是 JS 条件）—— 在**源模板**上断言，
    # 渲染后的 HTML 里 Jinja 标签已被求值掉，看不到。
    assert INDEX_TEXT.count("{% if cet_features.scenario_sentence %}") >= 2, \
        "静态造句筛选区块未用 Jinja 条件收束（收藏页 + 句子收藏页）"
    assert 'id="vocab-practice-filter"' not in cet_html
    # 但**词句收藏能力本身必须保留**（不能连收藏一起收掉）
    assert "收藏句子" in cet_html or "已收藏" in cet_html
    assert "编辑标签" in cet_html
    assert "标记已掌握" in cet_html
    assert "返回原句" in cet_html
    # 造句实现函数体保留（不物理删除通用源码），只是入口不渲染
    assert "function startVocabSentenceMaking" in cet_html


def test_base06_reading_and_intensive_entries_are_not_rendered_in_cet(cet_html):
    """材料卡片「阅读」「精读」按钮是动态渲染的，必须按集中配置收束。"""
    fn = cet_html.split("function buildModeButton(", 1)[1].split("\n}", 1)[0]
    assert "CET.feature('reading_course')" in fn, "buildModeButton 未按 reading_course 收束"
    # 精听必须保留
    assert "mode === 'listening'" in fn
    # 精读文案不再出现在用户可见标记里
    visible = _visible_html(cet_html)
    assert "加入精读" not in visible


def test_base06_baidu_pan_init_is_not_unconditional(cet_html):
    """`index.html:8855` 的无条件 initBaiduPanCard() 绕过模式判断 —— 必须修掉。

    验收方实测 CET 模式首页仍真实请求 /api/v2/lessons/baidu-pan/capability。
    """
    # 不允许出现「行首直接调用」的无条件形式
    assert not re.search(r"^\s*initBaiduPanCard\(\);\s*$", cet_html, re.M), \
        "仍存在无条件 initBaiduPanCard() 调用"
    # 所有调用点都必须受 link_import 收束
    # CET_LINK_IMPORT 就是 CET.feature('link_import') 的别名（Init 块里定义的 const）
    calls = re.findall(r"[^\n]*initBaiduPanCard\(\);", cet_html)
    assert calls, "找不到 initBaiduPanCard 调用"
    for line in calls:
        guarded = ("CET.feature('link_import')" in line) or ("CET_LINK_IMPORT" in line)
        assert guarded, f"调用未受模式控制：{line.strip()}"
        assert line.strip().startswith("if ") or line.strip().startswith("if("), \
            f"调用不是条件式：{line.strip()}"
    # 别名确实等于 link_import，不是另立的开关
    assert "const CET_LINK_IMPORT = CET.feature('link_import');" in cet_html


def test_base06_generic_mode_keeps_all_retired_entries(generic_html):
    """通用模式（ELT_CET_MODE=0）必须完整恢复原版入口（原版回归）。"""
    assert 'id="vocab-practice-filter"' in generic_html
    assert "全部造句情况" in generic_html
    assert 'id="sentence-pattern-filter"' in generic_html
    assert "情景造句练习" in generic_html
    assert "AI 情景造句" in generic_html
    # 动态过滤函数在通用模式下直接放行
    assert "if (!(window.CET && CET.enabled && CET.enabled())) return true;" in generic_html
