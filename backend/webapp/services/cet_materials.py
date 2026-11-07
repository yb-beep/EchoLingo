"""CET 底座 · 材料库元数据层（P1）。

职责边界（冻结约定 §3）：
- **不新增表、不新增列**。所有元数据只来自现有 `v2_lessons` 的
  `tags` / `title` / `source_url` / `duration` + `v2_subtitle_segments`
  计数 + `v2_lesson_progress`。
- **不猜**。级别、素材类别、题目数取不到时返回 `None`（前端显示「未标注」），
  绝不按标题关键词推断级别或真题身份。
- 业务 materialId 由 `source_url` 派生（`cet_service._business_id` 同源规则），
  派生不出时回退 `v2-<lesson_id>`；业务身份不依赖硬编码 lesson 数字。

为什么单独一层而不直接改 `cet_service.py`：
`cet_service.py` 是队友模块（冻结约定 §5 禁止修改）。底座需要「归档可见 /
最近学习 / 待分类统计 / 手动归类」这些材料库视角的能力，放在这里做，
只读它们的表，不动它们的代码。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import db

# ── 级别 ────────────────────────────────────────────────────
# 冻结约定 §3：认 cet4/cet6/四级/六级，否则 unspecified（= 待分类）。
LEVELS = ("cet4", "cet6", "unspecified")
_LEVEL_TAGS = {
    "cet4": "cet4",
    "四级": "cet4",
    "cet6": "cet6",
    "六级": "cet6",
}
# 归类时允许用户传入的别名（只做大小写/全角空格归一，不做语义猜测）
_LEVEL_ALIASES = {
    "cet4": "cet4",
    "cet-4": "cet4",
    "cet 4": "cet4",
    "4": "cet4",
    "四级": "cet4",
    "cet6": "cet6",
    "cet-6": "cet6",
    "cet 6": "cet6",
    "6": "cet6",
    "六级": "cet6",
    "unspecified": "unspecified",
    "unclassified": "unspecified",
    "unknown": "unspecified",
    "待分类": "unspecified",
    "未分类": "unspecified",
    "未标注": "unspecified",
    "": "unspecified",
}

# ── 素材类别 ────────────────────────────────────────────────
# 任务书 P1 要求显示「真题/练习/样例/开发夹具」。
# 这些是**已登记事实**，只能来自 tags 或 source_type/source_url 的显式夹具标记，
# 不允许从标题猜（「2025年12月六级」这样的标题不能自动升级成「真题」）。
CATEGORY_REAL_EXAM = "真题"
CATEGORY_PRACTICE = "练习"
CATEGORY_SAMPLE = "样例"
CATEGORY_FIXTURE = "开发夹具"
CATEGORIES = (CATEGORY_REAL_EXAM, CATEGORY_PRACTICE, CATEGORY_SAMPLE, CATEGORY_FIXTURE)

_CATEGORY_ALIASES = {
    "真题": CATEGORY_REAL_EXAM,
    "real_exam": CATEGORY_REAL_EXAM,
    "real-exam": CATEGORY_REAL_EXAM,
    "official": CATEGORY_REAL_EXAM,
    "练习": CATEGORY_PRACTICE,
    "practice": CATEGORY_PRACTICE,
    "样例": CATEGORY_SAMPLE,
    "sample": CATEGORY_SAMPLE,
    "example": CATEGORY_SAMPLE,
    "开发夹具": CATEGORY_FIXTURE,
    "夹具": CATEGORY_FIXTURE,
    "fixture": CATEGORY_FIXTURE,
    "development_fixture": CATEGORY_FIXTURE,
    "developmentfixture": CATEGORY_FIXTURE,
}

# 显式夹具身份：这些 source_type/source_url 前缀是既有代码写死的「夹具」标记，
# 属于已知事实，不是从标题推断。
_FIXTURE_SOURCE_TYPES = {"developmentfixture", "fixture", "testfixture"}
_FIXTURE_SOURCE_PREFIXES = ("fixture://", "testfixture://")

_SENTENCE_ID_RE = re.compile(r"^v2-(\d+)$")

_AUDIO_HASH_CACHE: dict[str, str] = {}


def _json_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def normalize_level(value: Any) -> str:
    """把用户/调用方传入的级别归一成 cet4 / cet6 / unspecified。"""
    key = str(value or "").strip().lower()
    if key in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[key]
    raise ValueError(
        f"level 仅支持 cet4、cet6 或 unspecified，收到：{value!r}"
    )


def lesson_level(lesson: dict[str, Any]) -> str:
    """从 tags 读级别；读不到 → unspecified（待分类）。**不看标题**。"""
    normalized = {tag.strip().lower() for tag in _json_tags(lesson.get("tags"))}
    if "cet6" in normalized or "六级" in normalized:
        return "cet6"
    if "cet4" in normalized or "四级" in normalized:
        return "cet4"
    return "unspecified"


def normalize_category(value: Any) -> str | None:
    """素材类别归一；空值返回 None（= 未标注）。不猜。"""
    key = str(value or "").strip().lower()
    if not key:
        return None
    return _CATEGORY_ALIASES.get(key)


def lesson_category(lesson: dict[str, Any]) -> str | None:
    """素材类别：先看 tags 里的显式类别，再看显式夹具来源标记；都没有 → None。"""
    for tag in _json_tags(lesson.get("tags")):
        mapped = _CATEGORY_ALIASES.get(tag.strip().lower())
        if mapped:
            return mapped
    source_type = str(lesson.get("source_type") or "").strip().lower()
    source_url = str(lesson.get("source_url") or "").strip().lower()
    if source_type in _FIXTURE_SOURCE_TYPES or source_url.startswith(_FIXTURE_SOURCE_PREFIXES):
        return CATEGORY_FIXTURE
    return None


def business_id(lesson: dict[str, Any]) -> str:
    """由 source_url 派生稳定业务 ID；派生不出返回空串（调用方回退 v2-<id>）。

    与 `cet_service._business_id` 同规则：fixture:// 与 upload: 前缀没有稳定
    业务含义（换台机器就变），不派生。
    """
    src = str(lesson.get("source_url") or "")
    if not src or src.startswith("fixture://") or src.startswith("upload:"):
        return ""
    name = src.rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0]
    return name


def material_id(lesson: dict[str, Any]) -> str:
    return business_id(lesson) or f"v2-{int(lesson['id'])}"


def _output_roots() -> list[Path]:
    """媒体文件可解析的根目录候选（只读探测，不创建目录）。"""
    roots: list[Path] = []
    try:
        from webapp.storage.lessons import OUTPUT_DIR  # noqa: WPS433

        roots.append(Path(OUTPUT_DIR))
    except Exception:  # noqa: BLE001
        pass
    try:
        from webapp.storage import user_assets  # noqa: WPS433

        roots.append(Path(user_assets.GLOBAL_OUTPUT_DIR))
    except Exception:  # noqa: BLE001
        pass
    roots.append(Path(db.DB_PATH).resolve().parents[1] / "output")
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def resolve_media_path(media_url: str) -> Path | None:
    """把 `/output/...` 形式的 media_url 解析成本机文件；解析不到返回 None。"""
    raw = str(media_url or "").strip()
    if not raw or "\x00" in raw:
        return None
    rel = raw.lstrip("/")
    if not rel.startswith("output/"):
        return None
    inner = rel[len("output/"):]
    for root in _output_roots():
        candidate = (root / inner).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def audio_sha256(lesson: dict[str, Any]) -> str:
    """材料音频的 SHA-256；文件不可读返回空串（不编造）。"""
    media_url = str(lesson.get("media_url") or "")
    if not media_url:
        return ""
    cached = _AUDIO_HASH_CACHE.get(media_url)
    if cached is not None:
        return cached
    digest = ""
    path = resolve_media_path(media_url)
    if path is not None:
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        except OSError:
            digest = ""
    _AUDIO_HASH_CACHE[media_url] = digest
    return digest


def _question_count(lesson: dict[str, Any]) -> int | None:
    """题目数。表不存在/查询失败 → None（未知），不返回 0 冒充「没有题」。"""
    ids = [f"v2-{int(lesson['id'])}"]
    biz = business_id(lesson)
    if biz:
        ids.append(biz)
    placeholders = ",".join("?" for _ in ids)
    try:
        with db._db() as conn:  # noqa: SLF001  (只读查询，不写表)
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM cet_questions WHERE material_id IN ({placeholders})",
                ids,
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    return int(row["c"] if row else 0)


def _progress(lesson: dict[str, Any]) -> dict[str, Any]:
    duration = float(lesson.get("duration") or 0)
    position = float(lesson.get("last_position_seconds") or 0)
    updated = str(lesson.get("progress_updated_at") or "")
    percent = 0
    if duration > 0:
        percent = int(min(100, max(0, round(position / duration * 100))))
    return {
        "lastPositionSeconds": position,
        "lastSegmentIndex": int(lesson.get("last_segment_index") or 0),
        "progressPercent": percent,
        "lastActiveAt": updated or "",
        "started": bool(updated) or position > 0,
    }


def _sentence_count(lesson: dict[str, Any]) -> int:
    """句子数。

    `list_v2_lessons()` 会带 `subtitle_count` 子查询；`get_v2_lesson()` 不带，
    所以单份材料查询要回落到实际计数，否则「有 4 句」会显示成 0 句。
    """
    if "subtitle_count" in lesson and lesson.get("subtitle_count") is not None:
        return int(lesson["subtitle_count"])
    try:
        return len(db.get_v2_subtitle_segments(int(lesson["id"])))
    except Exception:  # noqa: BLE001
        return 0


def _reading_block_count(lesson: dict[str, Any]) -> int:
    if "reading_block_count" in lesson and lesson.get("reading_block_count") is not None:
        return int(lesson["reading_block_count"])
    try:
        return len(db.get_v2_reading_blocks(int(lesson["id"])))
    except Exception:  # noqa: BLE001
        return 0


def _availability(lesson: dict[str, Any]) -> dict[str, Any]:
    sentence_count = _sentence_count(lesson)
    media_url = str(lesson.get("media_url") or "")
    media_path = resolve_media_path(media_url) if media_url else None
    status = str(lesson.get("subtitle_status") or "")
    return {
        "hasAudio": bool(media_url),
        "audioReachable": media_path is not None,
        "hasTimeline": sentence_count > 0,
        "sentenceCount": sentence_count,
        "hasTranscript": sentence_count > 0 or _reading_block_count(lesson) > 0,
        "readingBlockCount": _reading_block_count(lesson),
        "questionCount": _question_count(lesson),
        # 状态原样回显：ready / unavailable（无逐句时间轴，非生成中）/ pending / failed
        "subtitleStatus": status,
        "timelineStatus": status,
        "isGenerating": status == "pending",
    }


def material_item(lesson: dict[str, Any], *, include_sentences: bool = False) -> dict[str, Any]:
    """单份材料的材料库视图（P1 卡片所需全部字段）。"""
    lesson_id = int(lesson["id"])
    availability = _availability(lesson)
    item: dict[str, Any] = {
        "lessonId": lesson_id,
        "materialId": material_id(lesson),
        "title": str(lesson.get("title") or ""),
        "level": lesson_level(lesson),
        "category": lesson_category(lesson),
        "durationSec": float(lesson.get("duration") or 0),
        "mediaUrl": str(lesson.get("media_url") or ""),
        "mediaKind": str(lesson.get("media_kind") or ""),
        "lessonUrl": f"/workspace/{lesson_id}",
        "sourceType": str(lesson.get("source_type") or ""),
        "sourceUrl": str(lesson.get("source_url") or ""),
        "tags": _json_tags(lesson.get("tags")),
        "archived": bool(lesson.get("archived")),
        "createdAt": str(lesson.get("created_at") or ""),
        "updatedAt": str(lesson.get("updated_at") or ""),
        "audioSha256": audio_sha256(lesson),
        "availability": availability,
        "progress": _progress(lesson),
    }
    # 常用字段平铺一层，前端不必嵌套取（缺失保持 None，前端显示「未标注」）
    item.update({
        "hasAudio": availability["hasAudio"],
        "hasTimeline": availability["hasTimeline"],
        "sentenceCount": availability["sentenceCount"],
        "questionCount": availability["questionCount"],
        "subtitleStatus": availability["subtitleStatus"],
    })
    if include_sentences:
        item["sentences"] = db.get_v2_subtitle_segments(lesson_id)
    return item


def _matches_query(item: dict[str, Any], query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = " ".join([
        str(item.get("title") or ""),
        str(item.get("materialId") or ""),
        " ".join(item.get("tags") or []),
    ]).lower()
    return needle in haystack


def list_materials(
    *,
    level: str | None = None,
    query: str | None = None,
    include_archived: bool = False,
    category: str | None = None,
    only_started: bool = False,
    sort: str = "recent",
) -> dict[str, Any]:
    """材料库列表。

    排序 `recent`：按最近学习/更新时间倒序（与 `v2_lessons` 既有排序一致）。
    排序 `title`：按标题升序。
    `only_started=True` 只返回有学习记录的材料（继续学习视图）。
    """
    normalized_level: str | None = None
    if level not in (None, ""):
        normalized_level = normalize_level(level)
    normalized_category: str | None = None
    if category not in (None, ""):
        normalized_category = normalize_category(category)
        if normalized_category is None:
            raise ValueError(
                f"category 仅支持 {'、'.join(CATEGORIES)}，收到：{category!r}"
            )

    items: list[dict[str, Any]] = []
    for lesson in db.list_v2_lessons(include_archived=include_archived):
        item = material_item(lesson)
        if normalized_level and item["level"] != normalized_level:
            continue
        if normalized_category and item["category"] != normalized_category:
            continue
        if only_started and not item["progress"]["started"]:
            continue
        if query and not _matches_query(item, query):
            continue
        items.append(item)

    if sort == "title":
        items.sort(key=lambda entry: (entry["title"] or "").lower())
    elif sort != "recent":
        raise ValueError(f"sort 仅支持 recent 或 title，收到：{sort!r}")

    return {
        "items": items,
        "count": len(items),
        "filter": {
            "level": normalized_level or "all",
            "query": query or "",
            "category": normalized_category or "",
            "includeArchived": bool(include_archived),
            "onlyStarted": bool(only_started),
            "sort": sort,
        },
    }


def library_summary() -> dict[str, Any]:
    """材料库概览：各级别数量 + 待分类数量。空库返回全 0，不编造统计。"""
    counts = {level: 0 for level in LEVELS}
    archived = 0
    total = 0
    pending_classification: list[int] = []
    for lesson in db.list_v2_lessons(include_archived=True):
        level = lesson_level(lesson)
        if bool(lesson.get("archived")):
            archived += 1
            continue
        total += 1
        counts[level] += 1
        if level == "unspecified":
            pending_classification.append(int(lesson["id"]))
    return {
        "total": total,
        "byLevel": counts,
        "archived": archived,
        "pendingClassification": len(pending_classification),
        "pendingClassificationLessonIds": pending_classification,
        "supportedLevels": list(LEVELS),
        "supportedCategories": list(CATEGORIES),
    }


def _level_tag(level: str) -> str | None:
    return None if level == "unspecified" else level


def _is_level_tag(tag: str) -> bool:
    return tag.strip().lower() in _LEVEL_TAGS


def _is_category_tag(tag: str) -> bool:
    return tag.strip().lower() in _CATEGORY_ALIASES


def _strip_level_tags(tags: list[str]) -> list[str]:
    """只去掉级别标签，保留类别标签与用户自己的其他标签。"""
    return [tag for tag in tags if not _is_level_tag(tag)]


def _strip_category_tags(tags: list[str]) -> list[str]:
    """只去掉类别标签，保留级别标签与用户自己的其他标签。"""
    return [tag for tag in tags if not _is_category_tag(tag)]


def update_material(
    lesson_id: int,
    *,
    level: str | None = None,
    category: str | None = None,
    title: str | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """改材料信息 / 手动归类 / 归档恢复（任务书 P1）。

    **PATCH 语义（BASE-01 返修）**：未传的字段一律保留原值，
    只有显式传入的字段才会被改写。

    - `level=None`（未传）→ 级别标签原样保留；`level="unspecified"` → 退回待分类
    - `category=None`（未传）→ 类别标签原样保留；`category=""` → 清除类别
    - `title=None`（未传）→ 标题不变；`archived=None`（未传）→ 归档状态不变

    为什么不能「先删两类标签再重建」：首页「手动归类」只提交 `level`，
    旧实现会顺手把类别标签删掉且不重建，用户看到的是「六级/未标注」，
    样例类别凭空丢失；反方向只改类别又会把级别变成 `unspecified`。

    级别与类别都写进 `v2_lessons.tags`（冻结约定 §3：只用现有列）。
    级别标签去重后置首，类别标签追加在后；用户自定义标签一律保留。
    """
    lesson = db.get_v2_lesson(int(lesson_id))
    if not lesson:
        raise KeyError(f"lesson {lesson_id} 不存在")

    # ── 1. 先把所有校验做完，任何一项不合法都不写库（避免半改）──
    normalized_level: str | None = None
    if level is not None:
        normalized_level = normalize_level(level)

    normalized_category: str | None = None
    clear_category = False
    if category is not None:
        if str(category).strip() == "":
            clear_category = True
        else:
            normalized_category = normalize_category(category)
            if normalized_category is None:
                raise ValueError(
                    f"category 仅支持 {'、'.join(CATEGORIES)}，收到：{category!r}"
                )

    new_title: str | None = None
    if title is not None:
        new_title = str(title).strip()
        if not new_title:
            raise ValueError("标题不能为空")
        new_title = new_title[:200]

    # ── 2. 只重写本次显式传入的那一类受管标签 ──
    tags = _json_tags(lesson.get("tags"))
    tags_changed = False

    if normalized_level is not None:
        tags = _strip_level_tags(tags)
        tag = _level_tag(normalized_level)
        if tag:
            tags.insert(0, tag)
        tags_changed = True

    if clear_category:
        tags = _strip_category_tags(tags)
        tags_changed = True
    elif normalized_category is not None:
        tags = _strip_category_tags(tags)
        tags.append(normalized_category)
        tags_changed = True

    if new_title is not None:
        db.update_v2_lesson_metadata(int(lesson_id), title=new_title)

    if tags_changed:
        db.set_v2_lesson_tags(int(lesson_id), tags)

    if archived is not None:
        db.set_v2_lesson_archived(int(lesson_id), bool(archived))

    updated = db.get_v2_lesson(int(lesson_id))
    return material_item(updated or lesson)


def get_material(lesson_id: int) -> dict[str, Any]:
    lesson = db.get_v2_lesson(int(lesson_id))
    if not lesson:
        raise KeyError(f"lesson {lesson_id} 不存在")
    return material_item(lesson, include_sentences=True)


def find_by_material_id(material_id_value: str) -> dict[str, Any] | None:
    """按 `v2-<id>` 或业务 ID 找材料（只读，供导入去重/前端跳转用）。"""
    raw = str(material_id_value or "").strip()
    if not raw:
        return None
    match = _SENTENCE_ID_RE.fullmatch(raw)
    if match:
        lesson = db.get_v2_lesson(int(match.group(1)))
        return material_item(lesson) if lesson else None
    for lesson in db.list_v2_lessons(include_archived=True):
        if business_id(lesson) == raw:
            return material_item(lesson)
    return None


def sentence_translation(text: str) -> str:
    """只读：取 `v2_sentences` 里该句**已有**的中文译文；没有则返回空串。

    供无模型 key 的离线释义兜底使用（D2）：有现成翻译就给，没有就省略，
    绝不现编。直接开只读连接查询，避免 `db.get_v2_sentence()` 在空库上
    顺手初始化 schema 的写副作用。
    """
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return ""
    try:
        conn = sqlite3.connect(
            f"file:{db.current_db_path()}?mode=ro", uri=True, timeout=2
        )
    except Exception:  # noqa: BLE001
        return ""
    try:
        row = conn.execute(
            "SELECT translation FROM v2_sentences WHERE normalized_text=?",
            (normalized,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        conn.close()
    return str(row[0]).strip() if row and row[0] else ""
