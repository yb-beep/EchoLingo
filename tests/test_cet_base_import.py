"""CET 底座 P2：本地导入、字幕校验、去重、仅音频、失败回滚。

对应任务书 §6 自验清单 **T03 / T04 / T05**：
- T03 音频 + 合法带时间轴字幕导入 → 真实保存、可播放、逐句定位
- T04 仅音频导入 → 可播放、无字幕明确说明、不强制转写
- T05 纯文本 / 错误时间轴 / 坏文件 / 重复提交 → 各自清楚结果

另含 **D-IMPORT-1 回归**：导入后 `subtitle_status` 必须是 `ready`
（有字幕）或 `unavailable`（无字幕），**不能是 `pending`**。

全部用 `tmp_path` 隔离；**不设 ELT_USER_DB**（会覆盖 monkeypatch 的 DB_PATH）。
"""
from __future__ import annotations

import io
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
from webapp.services import cet_import, cet_materials, cet_service  # noqa: E402

VALID_SRT = """1
00:00:01,000 --> 00:00:03,500
Hello and welcome to the listening practice.

2
00:00:03,500 --> 00:00:06,000
This is the second line of the transcript.

3
00:00:06,000 --> 00:00:08,250
And here is the third and final one.
"""

VALID_VTT = """WEBVTT

00:00:01.000 --> 00:00:03.500
Hello and welcome to the listening practice.

00:00:03.500 --> 00:00:06.000
This is the second line of the transcript.
"""

SENTENCE_JSON = {
    "duration": 8.25,
    "sentences": [
        {"start": 1.0, "end": 3.5, "text": "Hello and welcome to the listening practice."},
        {"start": 3.5, "end": 6.0, "text": "This is the second line of the transcript."},
        {"start": 6.0, "end": 8.25, "text": "And here is the third and final one."},
    ],
}

PLAIN_TEXT = """First paragraph of the transcript.
It continues on a second line.

Second paragraph after the blank line.
"""


# ── 环境 ────────────────────────────────────────────────────


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DB / output / uploads；ffprobe 桩成固定时长（真 ffprobe 另测）。"""
    import webapp.services.v2_lessons as lesson_service
    from webapp.storage import user_assets

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(user_assets, "GLOBAL_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(lesson_service, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        lesson_service, "_probe_uploaded_media",
        lambda path: (60.0, "local_audio"),
    )
    # 关键：任何后台 Whisper 转写都不该被触发。桩成抛错，一旦被调用就暴露。
    def _no_transcribe(*args, **kwargs):
        raise AssertionError("导入不得排队 Whisper 转写（任务书 P2 明确禁止）")

    monkeypatch.setattr(lesson_service, "enqueue_local_import", _no_transcribe)
    db.init_db()
    cet_service.init_cet_schema()
    return tmp_path


@pytest.fixture()
def client(env):
    """测试用 app：注册底座路由 + 既有 v2_lessons 路由。

    要注册 v2_lessons 是因为本文件的断言要验证**真实工作区接口**的行为
    （`/status` 回显 ready、`/reading` 不再 409），不能只测自己的服务层。
    """
    from webapp.fastapi_routes.v2_lessons import router as v2_lessons_router

    app = FastAPI()
    app.include_router(cet_base_router)
    app.include_router(v2_lessons_router)
    return TestClient(app)


def _import(client, **overrides):
    """调 HTTP 导入端点。默认一份合法音频 + 合法 SRT。"""
    payload = {
        "audio": ("listening.mp3", b"fake-mp3-bytes", "audio/mpeg"),
    }
    files = {"audio": payload["audio"]}
    data = {
        "title": "测试听力材料",
        "level": "cet4",
        "category": "练习",
    }
    subtitle = overrides.pop("subtitle", ("listening.srt", VALID_SRT, "text/plain"))
    if subtitle is not None:
        files["subtitle"] = subtitle
    data.update(overrides)
    return client.post("/api/cet/base/import", files=files, data=data)


# ── T03：音频 + 合法 SRT ────────────────────────────────────


def test_import_audio_with_valid_srt(client, env):
    resp = _import(client)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["duplicate"] is False
    assert data["lessonUrl"].startswith("/workspace/")
    material = data["material"]
    assert material["title"] == "测试听力材料"
    assert material["level"] == "cet4"
    assert material["category"] == "练习"
    assert material["hasTimeline"] is True
    assert material["sentenceCount"] == 3
    assert data["subtitle"]["format"] == "srt"
    assert data["subtitle"]["timed"] is True
    assert data["subtitle"]["sentenceCount"] == 3
    assert data["subtitle"]["errors"] == []

    # 真实落库：句子与时间轴都在
    lesson_id = material["lessonId"]
    segments = db.get_v2_subtitle_segments(lesson_id)
    assert len(segments) == 3
    assert segments[0]["start"] == pytest.approx(1.0)
    assert segments[0]["end"] == pytest.approx(3.5)
    assert "Hello and welcome" in segments[0]["text"]
    assert segments[2]["end"] == pytest.approx(8.25)

    # 音频真实保存到服务可见位置
    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["media_url"].startswith("/output/v2_assets/")
    stored = env / "output" / lesson["media_url"].split("/output/", 1)[1]
    assert stored.is_file()
    assert stored.read_bytes() == b"fake-mp3-bytes"
    assert lesson["media_kind"] == "local_audio"


def test_import_marks_subtitle_status_ready_not_pending(client, env):
    """D-IMPORT-1 回归：导入后状态必须是 ready，不能停在 pending。"""
    resp = _import(client)
    assert resp.status_code == 200
    lesson_id = resp.json()["material"]["lessonId"]

    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["subtitle_status"] == "ready"
    assert lesson["subtitle_status"] != "pending"
    # summary_status 也不能留 pending，否则前端以为在生成大纲
    assert lesson["summary_status"] != "pending"

    # 状态接口同样回显 ready（前端徽标据此显示「字幕就绪」）
    status = client.get(f"/api/v2/lessons/{lesson_id}/status")
    assert status.status_code == 200
    assert status.json()["lesson"]["subtitle_status"] == "ready"


def test_import_builds_reading_blocks_for_reader_mode(client, env):
    """有字幕时应同时生成阅读分段，工作区 reading 接口不再 409。"""
    resp = _import(client)
    lesson_id = resp.json()["material"]["lessonId"]
    blocks = db.get_v2_reading_blocks(lesson_id)
    assert blocks, "导入带时间轴的字幕后应生成阅读分段"

    reading = client.get(f"/api/v2/lessons/{lesson_id}/reading")
    assert reading.status_code == 200, reading.text
    assert reading.json()["blocks"]


def test_import_audio_with_valid_vtt(client, env):
    resp = _import(client, subtitle=("captions.vtt", VALID_VTT, "text/vtt"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subtitle"]["format"] == "vtt"
    assert data["subtitle"]["sentenceCount"] == 2
    assert data["material"]["sentenceCount"] == 2


def test_import_audio_with_repo_sentence_json(client, env):
    """兼容仓库句子 JSON 结构（.cet-qa/media/*.sentences.source.json 同构）。"""
    resp = _import(
        client,
        subtitle=("material.sentences.source.json",
                  json.dumps(SENTENCE_JSON), "application/json"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subtitle"]["format"] == "json"
    assert data["subtitle"]["timed"] is True
    assert data["subtitle"]["sentenceCount"] == 3

    segments = db.get_v2_subtitle_segments(data["material"]["lessonId"])
    assert [s["text"] for s in segments] == [
        item["text"] for item in SENTENCE_JSON["sentences"]
    ]


def test_import_srt_stored_verbatim_not_resegmented(client, env):
    """用户给的规范 SRT 必须原样落库：条数与文本不被自动切句改写。"""
    srt = """1
00:00:01,000 --> 00:00:04,000
First sentence. Second sentence in same cue.

2
00:00:04,000 --> 00:00:07,000
Third sentence here.
"""
    resp = _import(client, subtitle=("two-cues.srt", srt, "text/plain"))
    assert resp.status_code == 200
    segments = db.get_v2_subtitle_segments(resp.json()["material"]["lessonId"])
    # 2 条 cue → 2 条句子（不因标点被拆成 3 条）
    assert len(segments) == 2
    assert segments[0]["text"] == "First sentence. Second sentence in same cue."


# ── T04：仅音频 ─────────────────────────────────────────────


def test_import_audio_only_has_no_timeline_and_is_not_pending(client, env):
    """T04 + D-IMPORT-1：仅音频不能落入 pending（会被前端当成「字幕生成中」）。"""
    resp = _import(client, subtitle=None)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["duplicate"] is False
    assert data["subtitle"]["timed"] is False
    assert data["subtitle"]["sentenceCount"] == 0
    material = data["material"]
    assert material["hasAudio"] is True
    assert material["hasTimeline"] is False
    assert material["sentenceCount"] == 0
    # 明确「暂无逐句定位」，不是「生成中」也不是「失败」
    assert material["subtitleStatus"] == "unavailable"
    assert any("暂无逐句定位" in warning for warning in data["warnings"])

    lesson = db.get_v2_lesson(material["lessonId"])
    assert lesson["subtitle_status"] == "unavailable"
    assert lesson["subtitle_status"] != "pending"
    assert lesson["subtitle_status"] != "failed"
    assert lesson["summary_status"] != "pending"
    assert db.get_v2_subtitle_segments(material["lessonId"]) == []


def test_audio_only_import_does_not_queue_transcription(client, env):
    """仅音频导入不得排队 Whisper（fixture 里桩成抛错，被调用就 fail）。"""
    resp = _import(client, subtitle=None)
    assert resp.status_code == 200, resp.text
    # fixture 的 _no_transcribe 一旦被调用就会让导入失败；能 200 即证明没调用
    assert resp.json()["material"]["hasTimeline"] is False


def test_audio_only_material_is_playable_and_has_no_reading_409(client, env):
    """仅音频仍可播放；reading 接口明确 409（前端据此不轮询），不是静默挂住。"""
    resp = _import(client, subtitle=None)
    lesson_id = resp.json()["material"]["lessonId"]

    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["media_url"]
    stored = env / "output" / lesson["media_url"].split("/output/", 1)[1]
    assert stored.is_file()

    reading = client.get(f"/api/v2/lessons/{lesson_id}/reading")
    # 无阅读内容 → 409「Reading content is not ready」，这是既有明确语义
    assert reading.status_code == 409


# ── T05：纯文本 ─────────────────────────────────────────────


def test_import_plain_text_never_fabricates_timestamps(client, env):
    resp = _import(client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["subtitle"]["format"] == "text"
    assert data["subtitle"]["timed"] is False
    assert data["subtitle"]["sentenceCount"] == 2

    lesson_id = data["material"]["lessonId"]
    # 关键：绝不能伪造时间戳
    assert db.get_v2_subtitle_segments(lesson_id) == []
    assert data["material"]["hasTimeline"] is False
    # 但原文确实保存了（作为阅读块）
    blocks = db.get_v2_reading_blocks(lesson_id)
    assert len(blocks) == 2
    assert "First paragraph" in blocks[0]["text"]
    assert blocks[0]["start_seconds"] is None  # 不编造开始时间

    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["subtitle_status"] == "unavailable"
    assert any("暂无逐句定位" in w for w in data["warnings"])


def test_plain_text_with_arrow_but_bad_timeline_is_rejected(client, env):
    """内容像字幕但时间轴非法 → 拒绝，不降级当纯文本静默接受。"""
    broken = """1
00:00:09,000 --> 00:00:04,000
End before start.
"""
    resp = _import(client, subtitle=("broken.srt", broken, "text/plain"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    assert detail["errors"][0]["reason"] == "end_not_after_start"
    assert "第 1 条" in detail["errors"][0]["message"]


# ── T05：错误时间轴逐条返回 ─────────────────────────────────


def test_out_of_order_timeline_reports_specific_entry(client, env):
    srt = """1
00:00:05,000 --> 00:00:07,000
Second in time but first in file.

2
00:00:01,000 --> 00:00:02,000
Comes earlier.
"""
    resp = _import(client, subtitle=("order.srt", srt, "text/plain"))
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert len(errors) == 1
    assert errors[0]["reason"] == "out_of_order"
    assert errors[0]["index"] == 2
    assert "第 2 条" in errors[0]["message"]


def test_beyond_audio_duration_rejected(client, env):
    """fixture 桩音频 60s；字幕写到 90s → 超出音频时长。"""
    srt = """1
00:00:80,000 --> 00:00:90,000
Beyond the audio.
"""
    resp = _import(client, subtitle=("long.srt", srt, "text/plain"))
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["reason"] == "beyond_audio"
    assert "超出音频时长" in errors[0]["message"]


def test_negative_timestamp_rejected(client, env):
    srt = """1
-00:00:01,000 --> 00:00:02,000
Negative start.
"""
    resp = _import(client, subtitle=("negative.srt", srt, "text/plain"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["reason"] == "negative_time"


def test_empty_text_cue_rejected(client, env):
    srt = """1
00:00:01,000 --> 00:00:03,000

2
00:00:03,000 --> 00:00:05,000
Only the second has text.
"""
    resp = _import(client, subtitle=("empty.srt", srt, "text/plain"))
    assert resp.status_code == 422
    errors = resp.json()["detail"]["errors"]
    assert errors[0]["reason"] == "empty_text"
    assert errors[0]["index"] == 1


def test_multiple_errors_all_reported_not_just_first(client, env):
    """不能只报第一条就停：所有损坏条目都要列出来。"""
    srt = """1
00:00:09,000 --> 00:00:04,000
Backwards.

2
00:00:02,000 --> 00:00:03,000

3
00:00:80,000 --> 00:00:90,000
Beyond audio.
"""
    resp = _import(client, subtitle=("multi.srt", srt, "text/plain"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["errorCount"] >= 2
    reasons = {item["reason"] for item in detail["errors"]}
    assert "end_not_after_start" in reasons
    assert "empty_text" in reasons


def test_corrupt_subtitle_never_reports_success(client, env):
    """坏文件不能「默默跳过损坏行报全部成功」。"""
    resp = _import(client, subtitle=("garbage.srt", "\x00\x01 not a subtitle at all", "text/plain"))
    # 无 "-->" → 被当纯文本；但显式 .srt 且内容无时间轴时按纯文本处理是合理降级，
    # 这里断言的是：要么明确报错，要么明确标 timed=False —— 不能假装有逐句时间轴。
    if resp.status_code == 200:
        data = resp.json()
        assert data["subtitle"]["timed"] is False
        assert data["material"]["hasTimeline"] is False
    else:
        assert resp.status_code == 422


def test_failed_validation_leaves_no_material(client, env):
    """校验失败不留正常可用假卡片，也不留上传暂存。"""
    before = len(db.list_v2_lessons(include_archived=True))
    resp = _import(client, subtitle=("bad.srt", "1\n00:00:09,000 --> 00:00:04,000\nBad.\n", "text/plain"))
    assert resp.status_code == 422
    assert len(db.list_v2_lessons(include_archived=True)) == before

    # 上传暂存也被清掉，不占空间
    with db._db() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT COUNT(*) AS c FROM v2_media_uploads").fetchone()
    assert int(rows["c"]) == 0


def test_unsupported_audio_format_rejected(client, env):
    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("song.exe", b"nope", "application/octet-stream")},
        data={"title": "坏格式", "level": "cet4"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    assert detail["errors"][0]["reason"] == "unsupported_audio_format"


def test_invalid_level_rejected(client, env):
    resp = _import(client, level="cet8")
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["reason"] == "invalid_level"


def test_invalid_category_rejected(client, env):
    resp = _import(client, category="随便写的")
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"][0]["reason"] == "invalid_category"


# ── T05：重复提交 ───────────────────────────────────────────


def test_duplicate_import_returns_existing_material(client, env):
    """同音频 SHA-256 + 同标题 → 返回已有材料，不建第二份。"""
    first = _import(client)
    assert first.status_code == 200
    first_data = first.json()
    assert first_data["duplicate"] is False
    first_lesson_id = first_data["material"]["lessonId"]

    second = _import(client)
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["duplicate"] is True
    assert second_data["material"]["lessonId"] == first_lesson_id
    assert second_data["lessonUrl"] == first_data["lessonUrl"]
    assert any("未重复创建" in w for w in second_data["warnings"])

    # 只有一份材料
    assert len(db.list_v2_lessons()) == 1


def test_duplicate_detection_requires_same_title(client, env):
    """同音频不同标题 → 不是重复（用户可能想建两份不同用途的材料）。"""
    _import(client, title="第一份")
    second = _import(client, title="第二份")
    assert second.status_code == 200
    assert second.json()["duplicate"] is False
    assert len(db.list_v2_lessons()) == 2


def test_duplicate_detection_requires_same_audio(client, env):
    """同标题不同音频 → 不是重复。"""
    _import(client)
    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("other.mp3", b"different-audio-bytes", "audio/mpeg")},
        data={"title": "测试听力材料", "level": "cet4"},
    )
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False
    assert len(db.list_v2_lessons()) == 2


def test_duplicate_allowed_explicitly(client, env):
    _import(client)
    resp = _import(client, allow_duplicate="true")
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False
    assert len(db.list_v2_lessons()) == 2


def test_duplicate_does_not_overwrite_existing_timeline(client, env):
    """重复提交不能覆盖已有材料的时间轴。"""
    first = _import(client)
    lesson_id = first.json()["material"]["lessonId"]
    original = db.get_v2_subtitle_segments(lesson_id)

    other_srt = """1
00:00:01,000 --> 00:00:02,000
Completely different text.
"""
    second = _import(client, subtitle=("other.srt", other_srt, "text/plain"))
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    # 原有时间轴一字未动
    assert db.get_v2_subtitle_segments(lesson_id) == original


# ── 回滚：失败不留假卡片 ────────────────────────────────────


def test_lesson_creation_failure_rolls_back_and_keeps_upload_retryable(env, monkeypatch):
    """建课中途失败 → 不留半成品课程，上传恢复 ready 可重试。"""
    import webapp.services.v2_lessons as lesson_service

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(lesson_service, "_copy_media_for_lesson", boom)

    with pytest.raises(cet_import.ImportValidationError) as excinfo:
        cet_import.import_local_material(
            audio_bytes=b"fake-mp3-bytes",
            audio_filename="listening.mp3",
            title="会失败的材料",
            level="cet4",
            subtitle_bytes=VALID_SRT.encode(),
            subtitle_filename="listening.srt",
        )
    assert excinfo.value.errors[0]["reason"] == "import_failed"
    # 不留假卡片
    assert db.list_v2_lessons() == []
    # 上传恢复可重试
    with db._db() as conn:  # noqa: SLF001
        row = conn.execute("SELECT status FROM v2_media_uploads").fetchone()
    assert row["status"] == "ready"


# ── 校验端点（只校验不导入）────────────────────────────────


def test_validate_endpoint_accepts_good_srt(client, env):
    resp = client.post(
        "/api/cet/base/import/validate",
        files={"subtitle": ("good.srt", VALID_SRT, "text/plain")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["timed"] is True
    assert data["sentenceCount"] == 3
    assert data["errors"] == []


def test_validate_endpoint_reports_errors_without_creating_material(client, env):
    resp = client.post(
        "/api/cet/base/import/validate",
        files={"subtitle": ("bad.srt", "1\n00:00:09,000 --> 00:00:04,000\nBad.\n", "text/plain")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["errors"][0]["reason"] == "end_not_after_start"
    # 只校验，绝不建材料
    assert db.list_v2_lessons() == []


def test_validate_endpoint_uses_audio_duration_when_given(client, env):
    resp = client.post(
        "/api/cet/base/import/validate",
        files={
            "audio": ("a.mp3", b"fake", "audio/mpeg"),
            "subtitle": ("long.srt", "1\n00:00:80,000 --> 00:00:90,000\nLong.\n", "text/plain"),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["errors"][0]["reason"] == "beyond_audio"
    assert data["audioDurationSec"] == pytest.approx(60.0)


def test_formats_endpoint_lists_tested_formats(client, env):
    resp = client.get("/api/cet/base/import/formats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["audio"] == [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"]
    assert ".srt" in data["subtitle"]
    assert ".json" in data["subtitle"]
    assert ".txt" in data["plainText"]


# ── 格式清单真实性：每个音频扩展名都要能走通 ────────────────


@pytest.mark.parametrize("suffix", [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"])
def test_every_declared_audio_format_is_accepted(client, env, suffix):
    """界面会照这份清单显示，所以每一项都必须实测可用。"""
    resp = client.post(
        "/api/cet/base/import",
        files={"audio": (f"audio{suffix}", b"fake-bytes", "application/octet-stream")},
        data={"title": f"格式测试 {suffix}", "level": "cet4"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["material"]["hasAudio"] is True


# ── 服务层单元测试 ─────────────────────────────────────────


def test_parse_timestamp_variants():
    assert cet_import.parse_timestamp("00:00:01,500") == pytest.approx(1.5)
    assert cet_import.parse_timestamp("01:02:03,250") == pytest.approx(3723.25)
    assert cet_import.parse_timestamp("1:02.5") == pytest.approx(62.5)
    assert cet_import.parse_timestamp("12.5") == pytest.approx(12.5)
    assert cet_import.parse_timestamp("-00:00:01,000") == pytest.approx(-1.0)
    assert cet_import.parse_timestamp("not a time") is None
    assert cet_import.parse_timestamp("") is None


def test_split_plain_text_by_blank_lines():
    paragraphs = cet_import.split_plain_text(PLAIN_TEXT)
    assert len(paragraphs) == 2
    assert paragraphs[0] == "First paragraph of the transcript. It continues on a second line."
    assert paragraphs[1] == "Second paragraph after the blank line."


def test_validate_entries_accepts_contiguous_boundaries():
    """相邻句 end == 下一句 start 是合法的（不是 out_of_order）。"""
    entries = [
        {"index": 1, "line": 1, "start": 0.0, "end": 2.0, "text": "a"},
        {"index": 2, "line": 4, "start": 2.0, "end": 4.0, "text": "b"},
    ]
    assert cet_import.validate_entries(entries, audio_duration=10.0) == []


def test_validate_entries_reports_line_numbers():
    entries = [
        {"index": 1, "line": 12, "start": 5.0, "end": 3.0, "text": "a"},
    ]
    errors = cet_import.validate_entries(entries, audio_duration=10.0)
    assert errors[0]["line"] == 12
    assert "第 12 行" in errors[0]["message"]


def test_detect_subtitle_format_sniffing():
    assert cet_import.detect_subtitle_format("a.srt", VALID_SRT) == "srt"
    assert cet_import.detect_subtitle_format("a.vtt", VALID_VTT) == "vtt"
    assert cet_import.detect_subtitle_format("a.json", "{}") == "json"
    assert cet_import.detect_subtitle_format("a.txt", PLAIN_TEXT) == "text"
    # 内容像 SRT 但扩展名是 .txt → 仍按 SRT 处理（用户常这么存）
    assert cet_import.detect_subtitle_format("a.txt", VALID_SRT) == "srt"


def test_supported_formats_constant_matches_upload_service():
    """导入声明支持的音频格式必须与上传设施白名单一致，不能多承诺。"""
    import webapp.services.v2_lessons as lesson_service

    assert set(cet_import.SUPPORTED_AUDIO_EXTS) == set(lesson_service._AUDIO_EXTS)  # noqa: SLF001


# ── 材料库联动 ─────────────────────────────────────────────


def test_imported_material_appears_in_library_with_level(client, env):
    _import(client, level="cet6", category="真题", title="六级导入材料")

    listed = client.get("/api/cet/base/materials", params={"level": "cet6"})
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["title"] for item in items] == ["六级导入材料"]
    assert items[0]["category"] == "真题"
    assert items[0]["hasTimeline"] is True


def test_import_with_unspecified_level_goes_to_pending_classification(client, env):
    _import(client, level="unspecified", category="", title="没标级别的材料")
    summary = client.get("/api/cet/base/materials/summary").json()
    assert summary["pendingClassification"] == 1

    lesson_id = client.get("/api/cet/base/materials").json()["items"][0]["lessonId"]
    patched = client.patch(f"/api/cet/base/materials/{lesson_id}", json={"level": "cet4"})
    assert patched.status_code == 200
    assert client.get("/api/cet/base/materials/summary").json()["pendingClassification"] == 0


def test_imported_material_business_id_not_lesson_number(client, env):
    """业务身份不依赖硬编码 lesson 数字：upload: 来源回退 v2-<id>，但不写死。"""
    resp = _import(client)
    material = resp.json()["material"]
    assert material["materialId"] == f"v2-{material['lessonId']}"
    assert material["lessonUrl"] == f"/workspace/{material['lessonId']}"


def test_import_does_not_hardcode_absolute_paths(client, env):
    """落库的 media_url 是服务相对 URL，不是本机绝对路径。"""
    resp = _import(client)
    lesson = db.get_v2_lesson(resp.json()["material"]["lessonId"])
    assert lesson["media_url"].startswith("/output/")
    assert str(env) not in lesson["media_url"]


# ── 真实世界边界：BOM / 多行 cue / VTT 位置标记 ─────────────


def test_bom_prefixed_srt_parses(isolated_bom_env=None):
    """带 UTF-8 BOM 的 SRT（Windows 记事本常见）第一行不能被误报成非法行。"""
    raw = "\ufeff1\n00:00:01,000 --> 00:00:02,000\nBOM text.\n"
    prepared = cet_import.prepare_subtitle("bom.srt", raw, audio_duration=60.0)
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 1
    assert prepared["segments"][0]["text"] == "BOM text."


def test_bom_prefixed_vtt_parses():
    raw = "\ufeffWEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi there.\n"
    prepared = cet_import.prepare_subtitle("bom.vtt", raw, audio_duration=60.0)
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 1


def test_bom_prefixed_plain_text_has_no_invisible_prefix():
    paragraphs = cet_import.split_plain_text("\ufeff第一段\n\n第二段")
    assert paragraphs == ["第一段", "第二段"]


def test_multi_line_cue_joined_into_one_sentence():
    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nLine one\nLine two\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nNext.\n"
    )
    prepared = cet_import.prepare_subtitle("multi.srt", srt, audio_duration=60.0)
    assert prepared["sentenceCount"] == 2
    assert prepared["segments"][0]["text"] == "Line one Line two"


def test_vtt_position_settings_ignored():
    """VTT cue 的 `line:90% align:center` 之类设置不能混进文本。"""
    vtt = (
        "WEBVTT\nKind: captions\n\n"
        "1\n00:00:01.000 --> 00:00:02.000 line:90% align:center\n"
        "Positioned cue text.\n"
    )
    prepared = cet_import.prepare_subtitle("pos.vtt", vtt, audio_duration=60.0)
    assert prepared["errors"] == []
    assert prepared["segments"][0]["text"] == "Positioned cue text."


def test_srt_without_trailing_newline_parses():
    prepared = cet_import.prepare_subtitle(
        "n.srt", "1\n00:00:01,000 --> 00:00:02,000\nNo trailing newline.",
        audio_duration=60.0,
    )
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 1


def test_exported_audio_ends_within_tolerance():
    """字幕结束时间正好等于音频时长是合法的（不留容差误报）。"""
    entries = [{"index": 1, "line": 1, "start": 58.0, "end": 60.0, "text": "edge"}]
    assert cet_import.validate_entries(entries, audio_duration=60.0) == []


# ── BASE-02 回归：JSON 坏条目不得静默丢弃 ────────────────────
# 缺陷：`_json_sentences()` 用 `[item for item in raw if isinstance(item, dict)]`
# 预先滤掉非对象条目，于是 `[合法句1,"BROKEN",null,合法句2]` 返回 200、
# errors=[]、只落 2 句。用户以为全部导入成功，且原始序号无从追溯。
#
# 修复要求：非对象条目逐条报错 + 拒绝整份导入 + 保留原始条目位置。


MIXED_JSON = [
    {"start": 0.0, "end": 2.0, "text": "Valid one."},
    "BROKEN",
    None,
    {"start": 2.0, "end": 3.0, "text": "Valid two."},
]


def test_json_non_object_entries_rejected_with_original_positions(client, env):
    """验收方原始场景：混合坏条目必须整体拒绝，并指出第几个元素坏了。"""
    before = len(db.list_v2_lessons(include_archived=True))

    resp = _import(
        client,
        subtitle=("mixed.json", json.dumps(MIXED_JSON), "application/json"),
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    # 两条坏条目都要报，不能只报第一条
    assert detail["errorCount"] == 2
    reasons = [item["reason"] for item in detail["errors"]]
    assert reasons == ["invalid_entry", "invalid_entry"]
    # 原始数组位置（1 基）：第 2 个是 "BROKEN"，第 3 个是 null
    assert [item["index"] for item in detail["errors"]] == [2, 3]
    # 错误信息说清实际类型，用户能定位
    assert "BROKEN" in detail["errors"][0]["message"]
    assert "null" in detail["errors"][1]["message"]

    # 整份拒绝：一句都没落库，也没有假卡片
    assert len(db.list_v2_lessons(include_archived=True)) == before
    with db._db() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT COUNT(*) AS c FROM v2_media_uploads").fetchone()
    assert int(rows["c"]) == 0


def test_json_non_object_entries_in_wrapped_object_rejected(client, env):
    """`{"sentences": [...]}` 包装形态同样逐条报错，不静默过滤。"""
    payload = {"duration": 3.0, "sentences": [
        {"start": 0.0, "end": 1.0, "text": "Ok."},
        123,
    ]}
    resp = _import(
        client,
        subtitle=("wrapped.json", json.dumps(payload), "application/json"),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["errorCount"] == 1
    assert detail["errors"][0]["index"] == 2
    assert detail["errors"][0]["reason"] == "invalid_entry"
    # 包装层名字进消息，用户知道是哪个数组
    assert "sentences" in detail["errors"][0]["message"]
    assert db.list_v2_lessons() == []


def test_json_sentence_count_is_not_filtered_length(client, env):
    """不能把「过滤后的长度」当全部成功：合法条目数 != 数组长度时拒绝。"""
    entries, errors, declared = cet_import.parse_sentence_json(json.dumps(MIXED_JSON))
    # 只解析出 2 条合法句，但必须同时报 2 条错误 —— 调用方据此整体拒绝
    assert len(entries) == 2
    assert len(errors) == 2
    assert declared is None
    # 合法条目的 index 也保留原始位置（1 和 4），不是压缩后的 1、2
    assert [entry["index"] for entry in entries] == [1, 4]


def test_json_validate_endpoint_reports_bad_entries(client, env):
    """预检端点同样要报坏条目，前端「校验」按钮不能显示通过。"""
    resp = client.post(
        "/api/cet/base/import/validate",
        files={"subtitle": ("mixed.json", json.dumps(MIXED_JSON), "application/json")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["errorCount"] == 2
    assert [item["index"] for item in data["errors"]] == [2, 3]


def test_json_all_valid_entries_still_import(client, env):
    """修复不能误伤合法 JSON：全合法时照旧成功。"""
    resp = _import(
        client,
        subtitle=("ok.json", json.dumps(SENTENCE_JSON), "application/json"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subtitle"]["sentenceCount"] == 3


def test_json_nan_timeline_rejected_at_validation_not_database(client, env):
    """`start:"NaN"` 这类值必须在预检被拒，不能冒成 SQL 约束错误。"""
    resp = _import(
        client,
        subtitle=("nan.json",
                  json.dumps([{"start": "NaN", "end": 2, "text": "Broken timeline."}]),
                  "application/json"),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["errors"][0]["reason"] == "unparsable_timestamp"
    assert detail["errors"][0]["index"] == 1
    # 不是 import_failed（那说明已经落库才炸）
    assert detail["errors"][0]["reason"] != "import_failed"
    assert db.list_v2_lessons() == []


# ── BASE-03 缺陷 A 回归：并发重复导入只留一份 ───────────────


def test_concurrent_duplicate_import_creates_one_material(client, env, monkeypatch):
    """验收方原始场景：同标题同音频两并发请求。

    旧行为：两者都 duplicate=false，建出两份材料（lessonId 4、5，count=2）。
    新行为：一个 duplicate=false 真正建课，另一个 duplicate=true 指向同一 lesson，
    列表 count=1。

    这里复刻验收探针的手法：在**快路径查重**返回后插屏障，制造确定性交错
    （两个请求都先看到「无重复」），再由临界区内的权威二次查重兜住。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2, timeout=10)
    original = cet_import.find_duplicate

    def synced(**kwargs):
        value = original(**kwargs)
        barrier.wait()
        return value

    monkeypatch.setattr(cet_import, "find_duplicate", synced)

    def do_import(_):
        return _import(client, title="并发同素材", subtitle=None)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(do_import, range(2)))

    statuses = [resp.status_code for resp in responses]
    assert statuses == [200, 200], [resp.text for resp in responses]
    payloads = [resp.json() for resp in responses]
    duplicates = sorted(payload["duplicate"] for payload in payloads)
    # 恰好一个建课、一个判重
    assert duplicates == [False, True]
    lesson_ids = {payload["material"]["lessonId"] for payload in payloads}
    # 判重的那份指向同一 lesson，不是另建一份
    assert len(lesson_ids) == 1

    # 库里真的只有一份
    assert len(db.list_v2_lessons()) == 1
    listed = client.get("/api/cet/base/materials", params={"q": "并发同素材"}).json()
    assert listed["count"] == 1


def test_stale_duplicate_lookup_still_cannot_double_create(client, env, monkeypatch):
    """即使快路径查重被绕过（模拟多进程/缓存陈旧），也不能建出两份。

    这是临界区内 `_matching_lesson_ids()` 权威二次查重的作用：
    不依赖可被替换的公开查重函数，只看数据库真实状态。
    """
    monkeypatch.setattr(cet_import, "find_duplicate", lambda **kwargs: None)

    first = _import(client, title="绕过查重", subtitle=None)
    second = _import(client, title="绕过查重", subtitle=None)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert (
        second.json()["material"]["lessonId"]
        == first.json()["material"]["lessonId"]
    )
    assert len(db.list_v2_lessons()) == 1


def test_allow_duplicate_still_creates_second_material(client, env):
    """显式 allow_duplicate=true 不受去重锁影响，仍然可以再建一份。"""
    _import(client, subtitle=None)
    second = _import(client, subtitle=None, allow_duplicate="true")
    assert second.status_code == 200
    assert second.json()["duplicate"] is False
    assert len(db.list_v2_lessons()) == 2


# ── BASE-03 缺陷 B 回归：坏字节文件要结构化 4xx，不留残留 ────


def _make_probe_reject_audio(monkeypatch):
    """让音频探测像真实 ffprobe 那样对坏字节抛 MediaUploadError。"""
    import webapp.services.v2_lessons as lesson_service

    def reject(path):
        raise lesson_service.MediaUploadError(
            "无法识别的媒体文件：ffprobe 返回码 1"
        )

    monkeypatch.setattr(lesson_service, "_probe_uploaded_media", reject)


def test_invalid_audio_returns_structured_422_not_plain_500(client, env, monkeypatch):
    """验收方原始场景：`bad.mp3`（内容 b'not audio at all'）。

    旧行为：500 + 纯文本 `Internal Server Error`（上传调用在保护范围外，
    路由只捕获 ImportValidationError）。
    新行为：结构化 422，明确说明「文件不是有效音频」，并给出支持的格式。
    """
    _make_probe_reject_audio(monkeypatch)

    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("bad.mp3", b"not audio at all", "audio/mpeg")},
        data={"title": "坏音频", "level": "cet4", "category": "练习"},
    )

    assert resp.status_code == 422, resp.text
    # 必须是 JSON 结构化错误，不是纯文本
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    assert detail["message"] == "音频文件无效"
    assert detail["errors"][0]["reason"] == "invalid_audio"
    assert "不是有效音频" in detail["errors"][0]["message"]
    # 告诉用户支持什么格式，便于自行处置
    assert ".mp3" in detail["errors"][0]["message"]


def test_invalid_audio_leaves_no_material_and_no_upload_residue(client, env, monkeypatch):
    """坏音频被拒后：无假卡片、无上传记录、uploads 目录干净。"""
    from webapp.storage import user_assets

    _make_probe_reject_audio(monkeypatch)
    uploads_root = user_assets.current_uploads_root()

    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("bad.mp3", b"not audio at all", "audio/mpeg")},
        data={"title": "坏音频", "level": "cet4"},
    )
    assert resp.status_code == 422

    # 无假卡片
    assert db.list_v2_lessons(include_archived=True) == []
    # 无上传记录
    with db._db() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT COUNT(*) AS c FROM v2_media_uploads").fetchone()
    assert int(rows["c"]) == 0
    # 无残留暂存（uploads 根目录为空）
    assert list(uploads_root.iterdir()) == []


def test_audio_probe_distinguishes_invalid_audio_from_missing_ffprobe(env, monkeypatch):
    """探测失败必须区分两种原因，不能一律当「不是音频」拒绝。

    - `MediaUploadError`（ffprobe 能跑，判定不是音视频）→ `invalid_audio`：明确拒绝
    - 其它异常（ffprobe 缺失、临时文件故障）→ `probe_unavailable`：
      不据此拒绝，只跳过「不超音频时长」这项校验
    """
    import webapp.services.v2_lessons as lesson_service

    def ok(path):
        return 60.0, "local_audio"

    monkeypatch.setattr(lesson_service, "_probe_uploaded_media", ok)
    assert cet_import._audio_probe(b"x", "a.mp3") == (60.0, "")

    def not_media(path):
        raise lesson_service.MediaUploadError("文件不是有效的音视频媒体")

    monkeypatch.setattr(lesson_service, "_probe_uploaded_media", not_media)
    assert cet_import._audio_probe(b"x", "a.mp3") == (None, "invalid_audio")

    def no_probe(path):
        raise FileNotFoundError("ffprobe 不可用")

    monkeypatch.setattr(lesson_service, "_probe_uploaded_media", no_probe)
    assert cet_import._audio_probe(b"x", "a.mp3") == (None, "probe_unavailable")


def test_probe_unavailable_skips_duration_check_only(env, monkeypatch):
    """探测设施不可用时：预检阶段跳过时长上界，不把它当成格式错误。

    这里让预检那一次探测抛非 MediaUploadError、上传那次正常，
    模拟「临时探测抖动」——导入应继续，而不是报 invalid_audio。
    """
    import webapp.services.v2_lessons as lesson_service

    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError("ffprobe 临时不可用")
        return 60.0, "local_audio"

    monkeypatch.setattr(lesson_service, "_probe_uploaded_media", flaky)

    result = cet_import.import_local_material(
        audio_bytes=b"fake-mp3-bytes",
        audio_filename="ok.mp3",
        title="探测抖动",
        level="cet4",
        subtitle_bytes=VALID_SRT.encode(),
        subtitle_filename="listening.srt",
    )
    assert result["duplicate"] is False
    # 跳过时长上界后仍完成导入（时长以上传设施回填的值为准）
    assert result["material"]["hasTimeline"] is True


def test_route_maps_unexpected_error_to_structured_json(client, env, monkeypatch):
    """路由兜底：未映射异常不能漏成**纯文本** 500。

    程序 bug 不谎报成 422（那是用户的输入错误），状态码仍是 500，
    但响应体必须是结构化 JSON，前端能拿到可读原因。
    """
    def boom(**kwargs):
        raise RuntimeError("意外故障")

    monkeypatch.setattr(cet_import, "import_local_material", boom)

    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("ok.mp3", b"fake-mp3-bytes", "audio/mpeg")},
        data={"title": "意外故障", "level": "cet4"},
    )
    assert resp.status_code == 500
    # 关键：不是纯文本 "Internal Server Error"
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_INTERNAL_ERROR"
    assert detail["errors"][0]["reason"] == "internal_error"
    assert "意外故障" in detail["errors"][0]["message"]


def test_route_maps_media_upload_error_to_structured_422(client, env, monkeypatch):
    """上传设施的错误绕过服务层映射时，路由也要给出结构化 4xx。"""
    import webapp.services.v2_lessons as lesson_service

    def boom(**kwargs):
        raise lesson_service.MediaUploadError("ffprobe 不认这个文件")

    monkeypatch.setattr(cet_import, "import_local_material", boom)

    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("ok.mp3", b"fake-mp3-bytes", "audio/mpeg")},
        data={"title": "上传错误", "level": "cet4"},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/json")
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    assert detail["errors"][0]["reason"] == "invalid_audio"


def test_route_maps_too_large_to_413(client, env, monkeypatch):
    """超大小用 413 与格式错误（422）区分。"""
    import webapp.services.v2_lessons as lesson_service

    def boom(**kwargs):
        raise lesson_service.MediaUploadTooLargeError("文件超过大小限制 500 MB")

    monkeypatch.setattr(cet_import, "import_local_material", boom)

    resp = client.post(
        "/api/cet/base/import",
        files={"audio": ("big.mp3", b"x", "audio/mpeg")},
        data={"title": "太大", "level": "cet4"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["errors"][0]["reason"] == "audio_too_large"
