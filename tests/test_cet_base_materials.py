"""CET 底座 P1：材料库元数据、级别筛选、搜索、待分类、归档恢复。

对应任务书 §6 自验清单 **T02 / T11**：
- T02 四级/六级分类、搜索、最近学习
- T11 旧库启动、旧未知材料归类、归档可恢复

全部用 `tmp_path` 隔离数据库；**不设 ELT_USER_DB**（那会覆盖
`monkeypatch.setattr(db, "DB_PATH", ...)`，导致跨用例污染）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402
from webapp.fastapi_routes.cet_base import router as cet_base_router  # noqa: E402
from webapp.services import cet_materials, cet_service  # noqa: E402


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """隔离库：monkeypatch DB_PATH（不设 ELT_USER_DB，避免 env 抢优先级）。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    db.init_db()
    # 真实运行时 cet_questions 表由 cet_service.init_cet_schema() 建好；
    # 这里同样建上，题目数才是「真实 0」而不是「表不存在 → 未知」。
    cet_service.init_cet_schema()
    return tmp_path

@pytest.fixture()
def client(isolated_db):
    app = FastAPI()
    app.include_router(cet_base_router)
    return TestClient(app)


def _make_lesson(
    *,
    title: str,
    source_url: str,
    tags: list[str] | None = None,
    duration: float = 60.0,
    media_url: str = "",
    segments: list[dict] | None = None,
    source_type: str = "cetMaterial",
) -> int:
    lesson = db.create_v2_lesson(
        source_type=source_type,
        source_url=source_url,
        title=title,
        duration=duration,
        media_url=media_url,
        media_kind="audio",
    )
    lesson_id = int(lesson["id"])
    if tags is not None:
        db.set_v2_lesson_tags(lesson_id, tags)
    if segments:
        db.replace_v2_subtitle_segments(lesson_id, segments)
        db.set_v2_lesson_status(lesson_id, subtitle_status="ready")
    return lesson_id


def _segments(count: int = 3, step: float = 2.0) -> list[dict]:
    return [
        {
            "index": index,
            "start": index * step,
            "end": (index + 1) * step,
            "text": f"Sentence number {index + 1}.",
        }
        for index in range(count)
    ]


# ── T02：级别分类 ────────────────────────────────────────────


def test_level_filter_cet4_cet6_unspecified(isolated_db):
    four = _make_lesson(title="四级材料", source_url="file://cet4-a.mp3", tags=["cet4"])
    six = _make_lesson(title="六级材料", source_url="file://cet6-a.mp3", tags=["cet6"])
    unknown = _make_lesson(title="没有标签的旧材料", source_url="file://legacy-a.mp3", tags=[])

    items = cet_materials.list_materials()["items"]
    by_id = {item["lessonId"]: item for item in items}

    assert by_id[four]["level"] == "cet4"
    assert by_id[six]["level"] == "cet6"
    # 旧材料无法判断级别 → unspecified（待分类），不能猜、不能丢
    assert by_id[unknown]["level"] == "unspecified"
    assert len(items) == 3


def test_level_filter_accepts_chinese_tags_and_aliases(isolated_db):
    _make_lesson(title="中文标签四级", source_url="file://a.mp3", tags=["四级"])
    _make_lesson(title="中文标签六级", source_url="file://b.mp3", tags=["六级"])

    assert [i["title"] for i in cet_materials.list_materials(level="cet4")["items"]] == ["中文标签四级"]
    assert [i["title"] for i in cet_materials.list_materials(level="cet6")["items"]] == ["中文标签六级"]
    # 「四级」别名与大小写变体都归一
    assert len(cet_materials.list_materials(level="四级")["items"]) == 1
    assert len(cet_materials.list_materials(level="CET4")["items"]) == 1
    assert len(cet_materials.list_materials(level="unspecified")["items"]) == 0


def test_level_is_not_guessed_from_title(isolated_db):
    """标题里写着「六级」但没有级别标签 → 仍是待分类。禁止按标题猜级别。"""
    lesson_id = _make_lesson(
        title="2025年12月六级第一套 Conversation",
        source_url="file://guess.mp3",
        tags=[],
    )
    item = cet_materials.get_material(lesson_id)
    assert item["level"] == "unspecified"


def test_invalid_level_rejected(isolated_db):
    with pytest.raises(ValueError):
        cet_materials.list_materials(level="cet8")


# ── T02：素材类别 ────────────────────────────────────────────


def test_category_from_tags_and_fixture_source(isolated_db):
    exam = _make_lesson(title="真题材料", source_url="file://e.mp3", tags=["cet6", "真题"])
    practice = _make_lesson(title="练习材料", source_url="file://p.mp3", tags=["cet4", "练习"])
    sample = _make_lesson(title="样例材料", source_url="file://s.mp3", tags=["cet4", "样例"])
    fixture = _make_lesson(
        title="夹具材料", source_url="fixture://cet-001",
        tags=["cet4"], source_type="developmentFixture",
    )
    unlabeled = _make_lesson(title="没标类别", source_url="file://u.mp3", tags=["cet4"])

    assert cet_materials.get_material(exam)["category"] == "真题"
    assert cet_materials.get_material(practice)["category"] == "练习"
    assert cet_materials.get_material(sample)["category"] == "样例"
    assert cet_materials.get_material(fixture)["category"] == "开发夹具"
    # 未标注类别 → None（前端显示「未标注」），不猜
    assert cet_materials.get_material(unlabeled)["category"] is None


def test_category_is_not_guessed_from_title(isolated_db):
    """标题写着「2025年12月六级第一套」也不能自动升级成「真题」。"""
    lesson_id = _make_lesson(
        title="2025年12月六级第一套 · Conversation Two",
        source_url="file://no-category.mp3",
        tags=["cet6"],
    )
    assert cet_materials.get_material(lesson_id)["category"] is None


# ── T02：可用状态 ────────────────────────────────────────────


def test_availability_reports_real_state(isolated_db):
    with_timeline = _make_lesson(
        title="有字幕", source_url="file://t.mp3",
        tags=["cet4"], segments=_segments(4),
    )
    audio_only = _make_lesson(title="仅音频", source_url="file://o.mp3", tags=["cet4"])

    item = cet_materials.get_material(with_timeline)
    assert item["availability"]["hasTimeline"] is True
    assert item["availability"]["sentenceCount"] == 4
    assert item["sentenceCount"] == 4
    assert item["subtitleStatus"] == "ready"
    # 题目表存在但没题 → 0（真实 0，不是未知）
    assert item["questionCount"] == 0

    bare = cet_materials.get_material(audio_only)
    assert bare["availability"]["hasTimeline"] is False
    assert bare["availability"]["sentenceCount"] == 0
    assert bare["hasTimeline"] is False


def test_audio_reachability_is_not_faked(isolated_db, tmp_path):
    """media_url 有值但文件不存在 → audioReachable False，hasAudio 仍按声明为 True。"""
    lesson_id = _make_lesson(
        title="声明有音频但文件缺失", source_url="file://missing.mp3",
        tags=["cet4"], media_url="/output/v2_assets/9999/missing.mp3",
    )
    item = cet_materials.get_material(lesson_id)
    assert item["hasAudio"] is True          # 声明有
    assert item["availability"]["audioReachable"] is False  # 实际不可达
    assert item["audioSha256"] == ""         # 不编造哈希


# ── T02：搜索 ────────────────────────────────────────────────


def test_search_by_title(isolated_db):
    _make_lesson(title="Campus library workshop", source_url="file://a.mp3", tags=["cet4"])
    _make_lesson(title="Airport announcement", source_url="file://b.mp3", tags=["cet6"])

    assert [i["title"] for i in cet_materials.list_materials(query="library")["items"]] == [
        "Campus library workshop"
    ]
    # 大小写不敏感
    assert len(cet_materials.list_materials(query="AIRPORT")["items"]) == 1
    # 无匹配 → 空列表，不是全部
    assert cet_materials.list_materials(query="zzz-not-found")["items"] == []


def test_search_combines_with_level_filter(isolated_db):
    _make_lesson(title="Library CET4", source_url="file://a.mp3", tags=["cet4"])
    _make_lesson(title="Library CET6", source_url="file://b.mp3", tags=["cet6"])

    result = cet_materials.list_materials(level="cet6", query="library")
    assert [i["title"] for i in result["items"]] == ["Library CET6"]
    assert result["filter"]["level"] == "cet6"


# ── T02：最近学习 / 继续学习 ─────────────────────────────────


def test_recent_and_continue_use_existing_progress_table(isolated_db):
    """最近学习与继续学习都基于现有 `v2_lesson_progress`，不新建表。"""
    first = _make_lesson(title="先学的", source_url="file://a.mp3", tags=["cet4"], duration=100)
    second = _make_lesson(title="后学的", source_url="file://b.mp3", tags=["cet4"], duration=100)
    _make_lesson(title="没学过", source_url="file://c.mp3", tags=["cet4"], duration=100)

    db.upsert_v2_lesson_progress(first, 10.0, 0)
    db.upsert_v2_lesson_progress(second, 50.0, 2)

    started = cet_materials.list_materials(only_started=True)["items"]
    assert {item["title"] for item in started} == {"先学的", "后学的"}

    by_title = {item["title"]: item for item in started}
    assert by_title["先学的"]["progress"]["progressPercent"] == 10
    assert by_title["后学的"]["progress"]["progressPercent"] == 50
    assert by_title["后学的"]["progress"]["started"] is True
    # 没学过的材料不冒充「继续学习」
    assert "没学过" not in by_title


def test_unstarted_material_has_zero_progress_not_fake_stats(isolated_db):
    lesson_id = _make_lesson(title="全新材料", source_url="file://a.mp3", tags=["cet4"], duration=100)
    item = cet_materials.get_material(lesson_id)
    assert item["progress"]["started"] is False
    assert item["progress"]["progressPercent"] == 0
    assert item["progress"]["lastPositionSeconds"] == 0


def test_recent_sort_is_progress_then_updated(isolated_db):
    older = _make_lesson(title="旧的", source_url="file://a.mp3", tags=["cet4"])
    newer = _make_lesson(title="新的", source_url="file://b.mp3", tags=["cet4"])
    db.upsert_v2_lesson_progress(older, 5.0, 0)

    items = cet_materials.list_materials()["items"]
    # 有学习记录的排前面
    assert items[0]["lessonId"] == older
    assert items[1]["lessonId"] == newer


# ── T11：待分类归类 ─────────────────────────────────────────


def test_summary_counts_pending_classification(isolated_db):
    _make_lesson(title="四级", source_url="file://a.mp3", tags=["cet4"])
    _make_lesson(title="六级", source_url="file://b.mp3", tags=["cet6"])
    unknown = _make_lesson(title="旧材料", source_url="file://c.mp3", tags=[])

    summary = cet_materials.library_summary()
    assert summary["total"] == 3
    assert summary["byLevel"] == {"cet4": 1, "cet6": 1, "unspecified": 1}
    assert summary["pendingClassification"] == 1
    assert summary["pendingClassificationLessonIds"] == [unknown]


def test_manual_classification_writes_level_tag(isolated_db):
    lesson_id = _make_lesson(title="旧材料", source_url="file://a.mp3", tags=["我的标签"])

    updated = cet_materials.update_material(lesson_id, level="cet6")
    assert updated["level"] == "cet6"
    # 用户自己的其他标签保留，不能被归类操作清掉
    assert "我的标签" in updated["tags"]
    assert "cet6" in updated["tags"]

    # 归类后从待分类列表消失
    assert cet_materials.library_summary()["pendingClassification"] == 0


def test_reclassify_level_replaces_previous_level_tag(isolated_db):
    lesson_id = _make_lesson(title="材料", source_url="file://a.mp3", tags=["cet4"])
    cet_materials.update_material(lesson_id, level="cet6")

    tags = cet_materials.get_material(lesson_id)["tags"]
    assert "cet6" in tags
    assert "cet4" not in tags  # 不能同时属于两级


def test_level_back_to_unspecified(isolated_db):
    lesson_id = _make_lesson(title="材料", source_url="file://a.mp3", tags=["cet4"])
    updated = cet_materials.update_material(lesson_id, level="unspecified")
    assert updated["level"] == "unspecified"
    assert "cet4" not in updated["tags"]
    assert cet_materials.library_summary()["pendingClassification"] == 1


def test_category_update_and_clear(isolated_db):
    lesson_id = _make_lesson(title="材料", source_url="file://a.mp3", tags=["cet4"])

    updated = cet_materials.update_material(lesson_id, category="练习")
    assert updated["category"] == "练习"

    cleared = cet_materials.update_material(lesson_id, category="")
    assert cleared["category"] is None
    assert "练习" not in cleared["tags"]


def test_title_update(isolated_db):
    lesson_id = _make_lesson(title="旧标题", source_url="file://a.mp3", tags=["cet4"])
    updated = cet_materials.update_material(lesson_id, title="  新标题  ")
    assert updated["title"] == "新标题"

    with pytest.raises(ValueError):
        cet_materials.update_material(lesson_id, title="   ")


def test_update_missing_lesson_raises_keyerror(isolated_db):
    with pytest.raises(KeyError):
        cet_materials.update_material(99999, level="cet4")


# ── T11：归档可恢复 ─────────────────────────────────────────


def test_archive_hides_then_restores(isolated_db):
    lesson_id = _make_lesson(title="要归档的材料", source_url="file://a.mp3", tags=["cet4"])

    archived = cet_materials.update_material(lesson_id, archived=True)
    assert archived["archived"] is True

    # 默认列表不含归档
    assert cet_materials.list_materials()["items"] == []
    # 显式要求时可见
    visible = cet_materials.list_materials(include_archived=True)["items"]
    assert [item["lessonId"] for item in visible] == [lesson_id]

    # 恢复
    restored = cet_materials.update_material(lesson_id, archived=False)
    assert restored["archived"] is False
    assert [item["lessonId"] for item in cet_materials.list_materials()["items"]] == [lesson_id]


def test_archived_material_not_counted_as_pending_classification(isolated_db):
    lesson_id = _make_lesson(title="归档旧材料", source_url="file://a.mp3", tags=[])
    assert cet_materials.library_summary()["pendingClassification"] == 1

    cet_materials.update_material(lesson_id, archived=True)
    summary = cet_materials.library_summary()
    assert summary["pendingClassification"] == 0
    assert summary["archived"] == 1
    assert summary["total"] == 0


def test_archive_does_not_delete_segments_or_progress(isolated_db):
    lesson_id = _make_lesson(
        title="有数据的材料", source_url="file://a.mp3",
        tags=["cet4"], segments=_segments(3),
    )
    db.upsert_v2_lesson_progress(lesson_id, 4.0, 1)

    cet_materials.update_material(lesson_id, archived=True)

    assert len(db.get_v2_subtitle_segments(lesson_id)) == 3
    assert db.get_v2_lesson_progress(lesson_id)["last_position_seconds"] == 4.0


# ── 业务身份 ────────────────────────────────────────────────


def test_material_id_derived_from_source_url(isolated_db):
    lesson_id = _make_lesson(
        title="六级材料", source_url="file://cet6-202512-conv2.mp3", tags=["cet6"],
    )
    item = cet_materials.get_material(lesson_id)
    assert item["materialId"] == "cet6-202512-conv2"
    assert item["lessonId"] == lesson_id


def test_material_id_falls_back_to_lesson_id_for_upload(isolated_db):
    """upload: 前缀没有稳定业务含义（换机器就变），回退 v2-<id>。"""
    lesson_id = _make_lesson(
        title="上传材料", source_url="upload:abc123", tags=["cet4"],
        source_type="uploaded_media",
    )
    assert cet_materials.get_material(lesson_id)["materialId"] == f"v2-{lesson_id}"


def test_material_id_is_not_hardcoded_lesson_number(isolated_db):
    """业务身份不依赖硬编码 lesson 数字：同样的 source_url 换个 lesson id，ID 不变。"""
    first = _make_lesson(title="材料", source_url="file://same-name.mp3", tags=["cet4"])
    item = cet_materials.get_material(first)
    assert item["materialId"] == "same-name"
    assert item["materialId"] != f"v2-{first}"


def test_find_by_material_id_both_forms(isolated_db):
    lesson_id = _make_lesson(
        title="材料", source_url="file://cet6-202512-conv2.mp3", tags=["cet6"],
    )
    assert cet_materials.find_by_material_id("cet6-202512-conv2")["lessonId"] == lesson_id
    assert cet_materials.find_by_material_id(f"v2-{lesson_id}")["lessonId"] == lesson_id
    assert cet_materials.find_by_material_id("does-not-exist") is None
    assert cet_materials.find_by_material_id("") is None


# ── HTTP 路由 ───────────────────────────────────────────────


def test_route_list_and_filter(client, isolated_db):
    _make_lesson(title="四级材料", source_url="file://a.mp3", tags=["cet4"])
    _make_lesson(title="六级材料", source_url="file://b.mp3", tags=["cet6"])

    resp = client.get("/api/cet/base/materials")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2

    resp = client.get("/api/cet/base/materials", params={"level": "cet6"})
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["items"]] == ["六级材料"]

    resp = client.get("/api/cet/base/materials", params={"level": "cet8"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_FILTER"


def test_route_summary(client, isolated_db):
    _make_lesson(title="四级", source_url="file://a.mp3", tags=["cet4"])
    _make_lesson(title="未知", source_url="file://b.mp3", tags=[])

    resp = client.get("/api/cet/base/materials/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["pendingClassification"] == 1


def test_route_get_material_and_404(client, isolated_db):
    lesson_id = _make_lesson(
        title="材料", source_url="file://a.mp3", tags=["cet4"], segments=_segments(2),
    )
    resp = client.get(f"/api/cet/base/materials/{lesson_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["lessonId"] == lesson_id
    assert len(data["sentences"]) == 2

    resp = client.get("/api/cet/base/materials/99999")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MATERIAL_NOT_FOUND"


def test_route_patch_classify_and_archive(client, isolated_db):
    lesson_id = _make_lesson(title="旧材料", source_url="file://a.mp3", tags=[])

    resp = client.patch(
        f"/api/cet/base/materials/{lesson_id}",
        json={"level": "cet4", "category": "练习"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "cet4"
    assert data["category"] == "练习"

    resp = client.patch(f"/api/cet/base/materials/{lesson_id}", json={"archived": True})
    assert resp.status_code == 200
    assert resp.json()["archived"] is True

    resp = client.patch(f"/api/cet/base/materials/{lesson_id}", json={"archived": False})
    assert resp.status_code == 200
    assert resp.json()["archived"] is False


def test_route_patch_rejects_unknown_field(client, isolated_db):
    lesson_id = _make_lesson(title="材料", source_url="file://a.mp3", tags=["cet4"])
    resp = client.patch(
        f"/api/cet/base/materials/{lesson_id}", json={"bogusField": 1}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_FIELD"


def test_route_patch_invalid_level(client, isolated_db):
    lesson_id = _make_lesson(title="材料", source_url="file://a.mp3", tags=["cet4"])
    resp = client.patch(f"/api/cet/base/materials/{lesson_id}", json={"level": "cet8"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_VALUE"


def test_route_patch_missing_lesson(client, isolated_db):
    resp = client.patch("/api/cet/base/materials/99999", json={"level": "cet4"})
    assert resp.status_code == 404


# ── T11：旧库启动 ───────────────────────────────────────────


def test_legacy_db_without_tags_still_lists(isolated_db):
    """旧库里的课程 tags 为空/不是合法 JSON 时，不能崩、不能丢，归为待分类。"""
    legacy_a = db.create_v2_lesson(
        source_type="local_audio", source_url="/legacy/path/a.mp3",
        title="旧课程 A", duration=30, media_url="", media_kind="audio",
    )
    legacy_b = db.create_v2_lesson(
        source_type="youtube", source_url="https://example.com/watch?v=x",
        title="旧课程 B", duration=45, media_url="", media_kind="audio",
    )
    # 直接写坏 tags（旧库可能存的是空串或非 JSON）
    with db._db() as conn:  # noqa: SLF001
        conn.execute("UPDATE v2_lessons SET tags='' WHERE id=?", (int(legacy_a["id"]),))
        conn.execute("UPDATE v2_lessons SET tags='not-json' WHERE id=?", (int(legacy_b["id"]),))

    items = cet_materials.list_materials()["items"]
    assert len(items) == 2
    assert all(item["level"] == "unspecified" for item in items)
    assert cet_materials.library_summary()["pendingClassification"] == 2


def test_empty_library_summary_is_all_zero(isolated_db):
    summary = cet_materials.library_summary()
    assert summary["total"] == 0
    assert summary["byLevel"] == {"cet4": 0, "cet6": 0, "unspecified": 0}
    assert summary["pendingClassification"] == 0
    assert cet_materials.list_materials()["items"] == []


def test_supported_formats_declared(isolated_db):
    """级别与类别枚举是稳定契约（前端/文档照它显示）。"""
    summary = cet_materials.library_summary()
    assert summary["supportedLevels"] == ["cet4", "cet6", "unspecified"]
    assert summary["supportedCategories"] == ["真题", "练习", "样例", "开发夹具"]


# ── BASE-01 回归：PATCH 未传字段必须保留（互不串改）────────────
# 缺陷：`update_material()` 一进来就 `_strip_managed_tags()` 把级别与类别
# 标签**全部删掉**，然后只重建本次请求传入的字段。
# 真实路径：首页「手动归类」只提交 `level`，于是类别被删且不重建，
# 六级/样例 材料点一下「仍填六级」就变成「六级/未标注」。
# 反方向只改 `category` 会把级别变成 unspecified。
#
# 下面四条覆盖验收要求的四个互不串改场景，第五条是组合场景。


def test_patch_level_only_keeps_category(isolated_db):
    """只改级别 → 类别原样保留（验收方原始场景）。"""
    lesson_id = _make_lesson(
        title="六级样例材料", source_url="file://cet6-sample.mp3",
        tags=["cet6", "样例", "我的标签"],
    )

    updated = cet_materials.update_material(lesson_id, level="cet6")

    assert updated["level"] == "cet6"
    # 关键断言：类别没有被顺手删掉
    assert updated["category"] == "样例"
    assert "样例" in updated["tags"]
    # 用户自定义标签也不能丢
    assert "我的标签" in updated["tags"]
    # 旧级别标签被替换而不是并存
    assert updated["tags"].count("cet6") == 1

    # 落库后重新读一遍（不是只看返回值）
    reloaded = cet_materials.get_material(lesson_id)
    assert (reloaded["level"], reloaded["category"]) == ("cet6", "样例")


def test_patch_category_only_keeps_level(isolated_db):
    """只改类别 → 级别原样保留（验收方 API 反方向场景）。"""
    lesson_id = _make_lesson(
        title="六级样例材料", source_url="file://cet6-sample.mp3",
        tags=["cet6", "样例"],
    )

    updated = cet_materials.update_material(lesson_id, category="真题")

    assert updated["category"] == "真题"
    # 关键断言：级别没有变成 unspecified
    assert updated["level"] == "cet6"
    assert "cet6" in updated["tags"]
    assert "样例" not in updated["tags"]  # 旧类别被替换
    assert cet_materials.library_summary()["pendingClassification"] == 0

    reloaded = cet_materials.get_material(lesson_id)
    assert (reloaded["level"], reloaded["category"]) == ("cet6", "真题")


def test_patch_title_only_keeps_level_and_category(isolated_db):
    """只改标题 → 级别与类别都不动。"""
    lesson_id = _make_lesson(
        title="旧标题", source_url="file://cet6-sample.mp3",
        tags=["cet6", "样例"],
    )

    updated = cet_materials.update_material(lesson_id, title="新标题")

    assert updated["title"] == "新标题"
    assert updated["level"] == "cet6"
    assert updated["category"] == "样例"
    # 只改标题不该重写 tags（内容与顺序都不变）
    assert updated["tags"] == ["cet6", "样例"]


def test_patch_archive_only_keeps_level_and_category(isolated_db):
    """只归档 / 恢复 → 级别与类别都不动。"""
    lesson_id = _make_lesson(
        title="要归档的材料", source_url="file://cet6-sample.mp3",
        tags=["cet6", "样例"],
    )

    archived = cet_materials.update_material(lesson_id, archived=True)
    assert archived["archived"] is True
    assert archived["level"] == "cet6"
    assert archived["category"] == "样例"

    restored = cet_materials.update_material(lesson_id, archived=False)
    assert restored["archived"] is False
    assert restored["level"] == "cet6"
    assert restored["category"] == "样例"
    assert restored["tags"] == ["cet6", "样例"]


def test_patch_level_and_category_together(isolated_db):
    """同时改级别和类别 → 两者都生效，互不残留旧值。"""
    lesson_id = _make_lesson(
        title="材料", source_url="file://a.mp3", tags=["cet4", "练习", "我的标签"],
    )

    updated = cet_materials.update_material(lesson_id, level="cet6", category="真题")

    assert updated["level"] == "cet6"
    assert updated["category"] == "真题"
    assert "cet4" not in updated["tags"]
    assert "练习" not in updated["tags"]
    assert "我的标签" in updated["tags"]
    assert updated["tags"] == ["cet6", "我的标签", "真题"]


def test_patch_explicit_clear_only_clears_that_field(isolated_db):
    """明确清空才删除，且只删被清的那一类（BASE-01 另一半语义）。"""
    lesson_id = _make_lesson(
        title="材料", source_url="file://a.mp3", tags=["cet6", "样例"],
    )

    # 只清级别：类别保留
    back_to_pending = cet_materials.update_material(lesson_id, level="unspecified")
    assert back_to_pending["level"] == "unspecified"
    assert back_to_pending["category"] == "样例"
    assert cet_materials.library_summary()["pendingClassification"] == 1

    # 恢复级别后只清类别：级别保留
    cet_materials.update_material(lesson_id, level="cet6")
    cleared = cet_materials.update_material(lesson_id, category="")
    assert cleared["category"] is None
    assert cleared["level"] == "cet6"
    assert "样例" not in cleared["tags"]


def test_patch_invalid_value_does_not_partially_write(isolated_db):
    """一项不合法就整体不写：不能出现「级别改了、类别报错」的半改状态。"""
    lesson_id = _make_lesson(
        title="材料", source_url="file://a.mp3", tags=["cet6", "样例"],
    )

    with pytest.raises(ValueError):
        cet_materials.update_material(lesson_id, level="cet4", category="随便写的")

    untouched = cet_materials.get_material(lesson_id)
    assert untouched["level"] == "cet6"
    assert untouched["category"] == "样例"
    assert untouched["tags"] == ["cet6", "样例"]


def test_route_patch_level_only_keeps_category(client, isolated_db):
    """验收方浏览器路径的 HTTP 等价：PATCH 只带 level，类别必须还在。"""
    lesson_id = _make_lesson(
        title="六级样例材料", source_url="file://cet6-sample.mp3",
        tags=["cet6", "样例"],
    )

    resp = client.patch(
        f"/api/cet/base/materials/{lesson_id}", json={"level": "cet6"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["level"] == "cet6"
    assert data["category"] == "样例"

    # 反方向：只带 category，级别必须还在
    resp = client.patch(
        f"/api/cet/base/materials/{lesson_id}", json={"category": "真题"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["level"] == "cet6"
    assert data["category"] == "真题"
