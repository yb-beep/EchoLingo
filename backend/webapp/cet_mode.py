"""四六级专用底座 · 集中功能配置（CET mode）。

任务书 §3.3 要求：退出泛化入口是「CET 产品模式内的路由、渲染和初始化收束」，
**优先通过集中功能配置控制，避免散落 CSS 遮挡**。

这个模块就是那份唯一的集中配置。所有页面/脚本都从这里取开关，不要各自写死：

- 后端：`pages.py` 把 `cet_mode` 注入 Jinja 上下文，模板用 `{% if cet_mode.x %}`。
- 前端：`/api/cet/mode` 返回同一份配置，`cet-mode.js` 在启动时应用，
  被收起的模块**不会发起后台请求/平台探测**。

开关来源优先级：环境变量 `ELT_CET_MODE` > 默认值。
`ELT_CET_MODE=0` 可退回通用模式（便于原版回归测试与队友对照）。
"""
from __future__ import annotations

import os
from typing import Any

# ── 功能项注册表 ─────────────────────────────────────────
# key: 稳定的功能标识（模板/JS 都用它）
# kept=True 表示 CET 模式下保留；False 表示收束（入口隐藏、初始化跳过）
_FEATURES: dict[str, dict[str, Any]] = {
    # ── 保留并优化的核心能力 ──
    "listening_library": {
        "kept": True,
        "label": "听力材料库",
        "note": "CET 主入口：四级/六级筛选、搜索、最近学习、继续学习",
    },
    "local_import": {
        "kept": True,
        "label": "本地导入",
        "note": "音频 + 可选 SRT/句子 JSON + 可选纯文本原文",
    },
    "player": {
        "kept": True,
        "label": "听力工作区",
        "note": "播放/拖动/倍速/逐句回听/循环/原文联动",
    },
    "transcript": {
        "kept": True,
        "label": "原文",
        "note": "听力稿显示、隐藏原文、译文切换（无译文明确说明）",
    },
    "dictionary": {
        "kept": True,
        "label": "查词",
        "note": "离线词典优先，无 key 可用",
    },
    "word_favorites": {
        "kept": True,
        "label": "词汇收藏",
        "note": "原词汇工坊的收藏部分，CET4/CET6 词表高亮",
    },
    "sentence_favorites": {
        "kept": True,
        "label": "句子收藏",
        "note": "原句子库，保留回到原材料/原音频",
    },
    "progress_resume": {
        "kept": True,
        "label": "学习进度",
        "note": "按材料恢复位置；完成态来自真实行为",
    },
    "settings_core": {
        "kept": True,
        "label": "设置",
        "note": "默认级别、学习显示、词表、现有模型设置入口（不改用户配置）",
    },
    "ai_word_sentence_explain": {
        "kept": True,
        "label": "词句解释（次级操作）",
        "note": "以当前句和材料为上下文，按需触发；无 key 不阻塞",
    },
    # ── 收束的泛化入口（CET 模式内退出）──
    "link_import": {
        "kept": False,
        "label": "链接导入（B站/YouTube/网盘/网页）",
        "note": "任务书 §3.3 明确退出；底层解析器保留不删",
    },
    "reading_course": {
        "kept": False,
        "label": "独立文章/PDF 阅读课程",
        "note": "退出；听力稿改名为「原文」保留文本联动",
    },
    "text_to_speech_course": {
        "kept": False,
        "label": "文本生成朗读课程",
        "note": "退出",
    },
    "video_learning": {
        "kept": False,
        "label": "泛英语视频学习入口",
        "note": "退出；播放器保留纯音频用法",
    },
    "memory_story": {
        "kept": False,
        "label": "记忆故事",
        "note": "退出",
    },
    "scenario_sentence": {
        "kept": False,
        "label": "情境造句",
        "note": "退出",
    },
    "ielts_reuse": {
        "kept": False,
        "label": "雅思复用",
        "note": "退出；逐句精读页同步清理文案",
    },
    "free_retell": {
        "kept": False,
        "label": "自由复述评分",
        "note": "退出",
    },
    "ai_chat_general": {
        "kept": False,
        "label": "泛用 AI 聊天",
        "note": "退出核心区域；词句解释保留在上下文",
    },
    "outline_generation": {
        "kept": False,
        "label": "内容大纲生成",
        "note": "退出；不自动发起后台生成",
    },
    "credits_account": {
        "kept": False,
        "label": "积分/登录/个人档案",
        "note": "本机单用户运行非基础依赖，隐藏入口；**不绕过鉴权、不改服务安全配置**",
    },
    # ── 队友模块挂载开关（BASE-05）──
    # 验收方指出：底座此前**无条件**加载 cet-listening.js/.css，导致新导入的无题
    # 材料仍出现「开始卡点练习/今日复习/交卷」等点了没反应的假入口。
    # 这里给一个正式开关：底座独立运行默认不挂载；队友真正接入后置 kept=True
    # 或设 ELT_CET_FEATURE_CET_LISTENING_AGENT=1 即可开启，无需改模板。
    "cet_listening_agent": {
        "kept": False,
        "label": "卡点陪练模块（队友交付）",
        "note": "队友卡点/智能复习模块；底座独立运行默认不挂载，真正接入后开启",
    },
}

# 导航结构：CET 模式下的默认导航（任务书 §3.2）
_NAV_CET = [
    {"key": "lessons", "label": "听力材料", "hash": "", "icon": "🎧"},
    {"key": "favorites", "label": "我的收藏", "hash": "favorites", "icon": "⭐"},
    {"key": "resources", "label": "设置", "hash": "resources", "icon": "⚙️"},
]

# 通用模式导航（对照用，便于 `ELT_CET_MODE=0` 回归）
_NAV_GENERIC = [
    {"key": "lessons", "label": "课程", "hash": "", "icon": "📺"},
    {"key": "vocab", "label": "词汇", "hash": "vocab", "icon": "📖"},
    {"key": "sentences", "label": "句子库", "hash": "sentences", "icon": "🗂️"},
    {"key": "resources", "label": "设置", "hash": "resources", "icon": "⚙️"},
]

# 页面标题/文案：全站一致（任务书 §3.2 要求不能只改首页主标题）
_COPY_CET = {
    "site_title": "EchoLingo · 四六级听力",
    "home_heading": "四六级听力材料",
    "home_subtitle": "选择四级或六级，导入本地听力材料，逐句精听。",
    "library_heading": "听力材料",
    "workspace_title": "听力工作区",
    "transcript_label": "原文",
    "favorites_heading": "我的收藏",
    "vocab_heading": "词汇收藏",
    "sentences_heading": "句子收藏",
    "settings_heading": "设置",
    "empty_library": "还没有听力材料。导入一份本地音频开始学习。",
    "unspecified_level": "待分类",
    # ── 返修2 · 包4：P4-1 统一「收藏」口径 / P4-2 去英文残留 ──────────
    # 纪律（主代理裁决）：只追加、不改老键；每个键两套文案成对，
    # 通用版一律给「与返修前渲染结果等价」的原文，保证 ELT_CET_MODE=0 字节级不变。
    "fix2_vocab_eyebrow": "四六级 · 词汇收藏",
    "fix2_vocab_continue_cta": "继续自评熟悉度 →",
    "fix2_sentences_eyebrow": "四六级 · 句子收藏",
    "fix2_favorites_pane_vocab": "收藏单词",
    "fix2_favorites_pane_sentences": "收藏句子",
    "fix2_favorite_word_cta": "☆ 收藏单词",
    "fix2_favorite_sentence_cta": "☆ 收藏句子",
    "fix2_favorite_added": "✓ 已收藏",
    "fix2_sync_collections": "同步本课收藏",
    "fix2_my_collections_wordlist": "我的收藏",
    "fix2_lesson_collections": "本课收藏",
}

_COPY_GENERIC = {
    "site_title": "EchoLingo · 英语精学工作台",
    "home_heading": "课程",
    "home_subtitle": "输入素材，生成课程。",
    "library_heading": "全部课程",
    "workspace_title": "Video Workspace",
    "transcript_label": "阅读",
    "favorites_heading": "复习",
    "vocab_heading": "词汇记忆工坊",
    "sentences_heading": "句子库",
    "settings_heading": "设置",
    "empty_library": "暂无课程，从上方新建第一门吧。",
    "unspecified_level": "未指定",
    # ── 返修2 · 包4：与 _COPY_CET 同名成对，值 = 返修前模板里硬编码的原文 ──
    # 目的：`ELT_CET_MODE=0` 时模板经 CET.copy 取到的就是这些原字符串，
    # 渲染结果与返修前完全一致（通用模式回归）。
    "fix2_vocab_eyebrow": "Vocabulary atelier",
    "fix2_vocab_continue_cta": "继续词汇练习 →",
    "fix2_sentences_eyebrow": "句子精听",
    "fix2_favorites_pane_vocab": "词汇收藏",
    "fix2_favorites_pane_sentences": "句子收藏",
    "fix2_favorite_word_cta": "☆ 收藏",
    "fix2_favorite_sentence_cta": "☆ 收藏句子",
    "fix2_favorite_added": "✓ 已收藏",
    "fix2_sync_collections": "同步本课收藏",
    "fix2_my_collections_wordlist": "我的生词本",
    "fix2_lesson_collections": "学习收集 Phase B",
}

# 词表：CET 模式默认只把 CET4/CET6 作为主选项（任务书 §3.2）
_CET_WORDLISTS = ["builtin_cet4", "builtin_cet6"]


def cet_mode_enabled() -> bool:
    """CET 模式是否启用。默认启用；`ELT_CET_MODE=0` 退回通用模式。"""
    raw = (os.environ.get("ELT_CET_MODE") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def feature_enabled(key: str) -> bool:
    """单项功能在**当前模式**下是否启用。

    优先级：
      1. 环境变量 `ELT_CET_FEATURE_<KEY>`（显式覆盖，验收/接入时用正式配置开关）
      2. 通用模式（`ELT_CET_MODE=0`）→ 全部启用，保证原版回归可用
      3. `_FEATURES[key]["kept"]`

    未知 key 抛 KeyError（故意）：拼错的开关名如果静默返回 False，
    会变成难查的「入口莫名消失」。
    """
    item = _FEATURES.get(key)
    if item is None:
        raise KeyError(f"unknown CET feature: {key}")

    # 1. 环境变量显式覆盖：ELT_CET_FEATURE_CET_LISTENING_AGENT=1 / 0
    raw = (os.environ.get(f"ELT_CET_FEATURE_{key.upper()}") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False

    # 2. 通用模式：全部保留，保证原版可用
    if not cet_mode_enabled():
        return True

    # 3. 模式内默认值
    return bool(item["kept"])


def config() -> dict[str, Any]:
    """完整配置快照。后端模板上下文与 `/api/cet/mode` 共用这一份。

    每项 `enabled` 统一走 `feature_enabled()`，保证 `/api/cet/mode` 与模板、
    与 `ELT_CET_FEATURE_*` 覆盖三者完全一致。
    """
    enabled = cet_mode_enabled()
    features = {
        key: {
            "enabled": feature_enabled(key),
            "kept": bool(item["kept"]),
            "label": item["label"],
            "note": item["note"],
        }
        for key, item in _FEATURES.items()
    }
    return {
        "enabled": enabled,
        "features": features,
        "nav": _NAV_CET if enabled else _NAV_GENERIC,
        "copy": _COPY_CET if enabled else _COPY_GENERIC,
        "wordlists": _CET_WORDLISTS if enabled else [],
        "levels": ["cet4", "cet6", "unspecified"] if enabled else [],
        "retired_keys": [k for k, v in features.items() if not v["enabled"]],
    }


def template_context() -> dict[str, Any]:
    """给 Jinja 的上下文。

    模板里用 `{% if cet_mode.features.link_import.enabled %}` 这种形式判断；
    为了少写字，同时提供扁平的 `cet_on` 与 `cet_features`。
    """
    cfg = config()
    return {
        "cet_mode": cfg,
        "cet_on": cfg["enabled"],
        "cet_features": {k: v["enabled"] for k, v in cfg["features"].items()},
        "cet_copy": cfg["copy"],
        "cet_nav": cfg["nav"],
        "cet_wordlists": cfg["wordlists"],
    }
