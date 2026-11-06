"""返修2 · 包3 · P3-2：导入时清洗行首说话人标记。

缺陷（任务书 P3-2）：六级真题原文第 23 句是 `/ W: What?`，说话人标记没清洗；
文档写 32 句，精听页显示 33 句。

根因（本轮实测确认，不是文档说法）：
`.cet-qa/media/cet6-202512-conv2.sentences.source.json` 的第 22 条 `text`
字段末尾粘着**下一位说话人的整句** ——
`When we are done with it and send it upstairs, what do you think will happen? / W: What?`
而第 23 条自己就是 `What?`。落库是 32 条（与文档一致），但
`build_translation_units()` 会把第 22 条按标点再切一次，于是
`/api/v2/lessons/{id}/subtitles` 的 `sentence_units` 变成 33 条，
第 23 条就是以 `/ W:` 开头的那条 —— 这就是「精听页比文档多一句」。

修复：
1. `split_speaker_markers()` 按标记切句并剥掉标记本身；
2. `_apply_speaker_cleaning()` 做**标记回声去重**（拆出的尾巴若与下一条逐字相同，
   说明它是回声而非新内容，丢弃尾巴但保留标记报告）；
3. 标记写 `output/v2_assets/<lesson_id>/speaker_markers.json` sidecar
   （冻结约定 §3 禁止新增列，所以不落库列）；
4. 无标记时**逐字不变**（`test_import_srt_stored_verbatim_not_resegmented` 守着）。

全部用 `tmp_path` 隔离；**不设 ELT_USER_DB**。
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
from webapp.services import cet_import, cet_service  # noqa: E402
from webapp.services.v2_translation import build_translation_units  # noqa: E402

MEDIA = ROOT / ".cet-qa" / "media"
CET6_JSON = MEDIA / "cet6-202512-conv2.sentences.source.json"
CET4_JSON = MEDIA / "cet4-sample-hope-wish.sentences.source.json"

# 带标记的最小样例：第 1 条末尾粘着下一位说话人的整句，第 2 条自己就是那句
MARKER_JSON = {
    "duration": 8.25,
    "sentences": [
        {"start": 0.0, "end": 3.0,
         "text": "What do you think will happen? / W: What?"},
        {"start": 3.2, "end": 4.0, "text": "What?"},
        {"start": 4.2, "end": 6.0, "text": "Normal sentence without marker."},
    ],
}

PLAIN_TEXT_WITH_MARKERS = """W: First paragraph after the speaker marker.
M: Second paragraph after a different marker.
"""


# ── 环境 ────────────────────────────────────────────────────


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import webapp.services.v2_lessons as lesson_service
    from webapp.storage import user_assets

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(user_assets, "GLOBAL_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(lesson_service, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        lesson_service, "_probe_uploaded_media", lambda path: (60.0, "local_audio"),
    )

    def _no_transcribe(*args, **kwargs):
        raise AssertionError("不勾选自动切句时不得排队转写")

    monkeypatch.setattr(lesson_service, "enqueue_local_import", _no_transcribe)
    db.init_db()
    cet_service.init_cet_schema()
    return tmp_path


@pytest.fixture()
def client(env):
    from webapp.fastapi_routes.v2_lessons import router as v2_lessons_router

    app = FastAPI()
    app.include_router(cet_base_router)
    app.include_router(v2_lessons_router)
    return TestClient(app)


def _import_json(client, payload, *, title="标记清洗材料", filename="material.json"):
    return client.post(
        "/api/cet/base/import",
        files={
            "audio": ("listening.mp3", b"fake-mp3-bytes", "audio/mpeg"),
            "subtitle": (filename, json.dumps(payload), "application/json"),
        },
        data={"title": title, "level": "cet6"},
    )


# ── 纯函数：split_speaker_markers ───────────────────────────


def test_split_slash_marker_splits_and_strips():
    """斜杠形态标记既是句界又要剥掉（P3-2 的核心）。"""
    parts = cet_import.split_speaker_markers(
        "What do you think will happen? / W: What?"
    )
    assert [part["text"] for part in parts] == [
        "What do you think will happen?",
        "What?",
    ]
    assert parts[0]["marker"] == ""
    assert parts[1]["marker"] == "/ W:"
    assert parts[1]["speaker"] == "W"


def test_split_leading_bare_marker():
    """行首裸标记 `W:` 也剥掉。"""
    parts = cet_import.split_speaker_markers("W: Hello there.")
    assert len(parts) == 1
    assert parts[0]["text"] == "Hello there."
    assert parts[0]["marker"] == "W:"
    assert parts[0]["speaker"] == "W"


def test_split_multiple_markers_in_one_entry():
    parts = cet_import.split_speaker_markers("Hello. / M: Hi. / W: Bye.")
    assert [part["text"] for part in parts] == ["Hello.", "Hi.", "Bye."]
    assert [part["marker"] for part in parts] == ["", "/ M:", "/ W:"]


@pytest.mark.parametrize("text", [
    "Ratio A: 3 and note: fine.",
    "and/or test",
    "OK: nothing to see",
    "It is 5 p.m. on Friday.",
    "No markers at all in this sentence.",
])
def test_plain_text_is_not_touched(text):
    """多字母冒号（Note:/OK:/Ratio A:）与普通斜杠不能被当成说话人标记。

    这是「无标记时行为与现在完全一致」的底线：不能为了清洗标记
    误伤正常英文文本。
    """
    parts = cet_import.split_speaker_markers(text)
    assert len(parts) == 1
    assert parts[0]["text"] == text
    assert parts[0]["marker"] == ""


def test_empty_text_yields_no_parts():
    assert cet_import.split_speaker_markers("") == []
    assert cet_import.split_speaker_markers("   ") == []


# ── 纯函数：clean_sentences（回声去重）──────────────────────


def test_echo_tail_is_dropped_not_duplicated():
    """拆出的尾巴若与下一条逐字相同 → 是回声，丢弃尾巴（句子数不增加）。"""
    cleaned, markers = cet_import.clean_sentences([
        {"start": 0.0, "end": 3.0, "text": "What do you think will happen? / W: What?"},
        {"start": 3.2, "end": 4.0, "text": "What?"},
        {"start": 4.2, "end": 6.0, "text": "Normal sentence."},
    ])
    assert [item["text"] for item in cleaned] == [
        "What do you think will happen?",
        "What?",
        "Normal sentence.",
    ]
    # 标记本身仍在报告里（信息不丢）
    assert len(markers) == 1
    assert markers[0]["marker"] == "/ W:"
    assert markers[0]["cleanedText"] == "What?"


def test_non_echo_tail_is_kept():
    """尾巴与下一条不同 → 是真实内容，必须保留（不能一律删）。"""
    cleaned, markers = cet_import.clean_sentences([
        {"start": 0.0, "end": 3.0, "text": "First part. / W: A different reply."},
        {"start": 3.2, "end": 4.0, "text": "Something else entirely."},
    ])
    assert [item["text"] for item in cleaned] == [
        "First part.", "A different reply.", "Something else entirely.",
    ]
    assert len(markers) == 1


def test_cleaning_is_idempotent():
    """二次清洗不再改变结果（防止重导时反复切分）。"""
    once, _ = cet_import.clean_sentences([
        {"start": 0.0, "end": 3.0, "text": "A sentence. / W: Another."},
    ])
    twice, markers = cet_import.clean_sentences(once)
    assert [item["text"] for item in twice] == [item["text"] for item in once]
    assert markers == []


def test_timeline_stays_monotonic_after_split():
    """拆出来的多条时间轴必须递增且不越界（否则会被 validate_entries 拒）。"""
    cleaned, _ = cet_import.clean_sentences([
        {"start": 10.0, "end": 14.0, "text": "Long first part here. / W: Short."},
    ])
    assert len(cleaned) == 2
    assert cleaned[0]["start"] == pytest.approx(10.0)
    assert cleaned[0]["end"] > cleaned[0]["start"]
    assert cleaned[1]["start"] == pytest.approx(cleaned[0]["end"])
    assert cleaned[1]["end"] == pytest.approx(14.0)


def test_entries_without_markers_are_returned_verbatim():
    """无标记条目：文本逐字不变（不能被空白归一以外的任何处理碰到）。"""
    source = [
        {"start": 0.0, "end": 2.0, "text": "First sentence here."},
        {"start": 2.0, "end": 4.0, "text": "Second sentence here."},
    ]
    cleaned, markers = cet_import.clean_sentences(source)
    assert markers == []
    assert [item["text"] for item in cleaned] == [item["text"] for item in source]


# ── prepare_subtitle：清洗接在解析后、校验前 ────────────────


def test_prepare_subtitle_cleans_markers_and_reports_count():
    prepared = cet_import.prepare_subtitle(
        "material.json", json.dumps(MARKER_JSON), audio_duration=8.25,
    )
    assert prepared["errors"] == []
    assert prepared["timed"] is True
    assert prepared["sentenceCount"] == 3
    assert prepared["speakerMarkerCount"] == 1
    assert prepared["speakerMarkers"][0]["marker"] == "/ W:"
    assert [seg["text"] for seg in prepared["segments"]] == [
        "What do you think will happen?",
        "What?",
        "Normal sentence without marker.",
    ]


def test_prepare_subtitle_plain_text_strips_leading_markers():
    """纯文本不拆句（一段一行），只剥行首标记；空行仍保留分段语义。"""
    prepared = cet_import.prepare_subtitle(
        "transcript.txt",
        "W: First paragraph after the speaker marker.\n"
        "\n"
        "M: Second paragraph after a different marker.\n",
        audio_duration=8.25,
    )
    assert prepared["timed"] is False
    assert prepared["paragraphs"] == [
        "First paragraph after the speaker marker.",
        "Second paragraph after a different marker.",
    ]
    assert prepared["speakerMarkerCount"] == 2


def test_plain_text_markers_stripped_even_when_lines_merge():
    """没有空行分隔时 `split_plain_text` 会合并成一段（既有语义），
    但每行的行首标记仍必须被剥掉 —— 这正是「先按行清洗再分段」的理由。"""
    prepared = cet_import.prepare_subtitle(
        "transcript.txt",
        PLAIN_TEXT_WITH_MARKERS,
        audio_duration=8.25,
    )
    assert prepared["timed"] is False
    # 既有分段语义不变：无空行 → 合并为一段
    assert prepared["paragraphs"] == [
        "First paragraph after the speaker marker. "
        "Second paragraph after a different marker."
    ]
    # 但两处标记都被剥掉（合并后的段中间不能残留 `M:`）
    assert "M:" not in prepared["paragraphs"][0]
    assert prepared["speakerMarkerCount"] == 2


def test_plain_text_without_markers_is_byte_identical():
    """无标记纯文本：分段与文本完全不变（行为与现在一致）。"""
    source = (
        "First paragraph of the transcript.\n"
        "It continues on a second line.\n"
        "\n"
        "Second paragraph after the blank line.\n"
    )
    prepared = cet_import.prepare_subtitle(
        "transcript.txt", source, audio_duration=8.25,
    )
    assert prepared["paragraphs"] == [
        "First paragraph of the transcript. It continues on a second line.",
        "Second paragraph after the blank line.",
    ]
    assert prepared["speakerMarkerCount"] == 0


def test_prepare_subtitle_srt_without_markers_unchanged():
    """规范 SRT 不受影响：条数与文本逐字不变（P3-2 不能误伤 BASE-02 的语义）。"""
    srt = (
        "1\n00:00:01,000 --> 00:00:04,000\n"
        "First sentence. Second sentence in same cue.\n\n"
        "2\n00:00:04,000 --> 00:00:07,000\nThird sentence here.\n"
    )
    prepared = cet_import.prepare_subtitle("two-cues.srt", srt, audio_duration=60.0)
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 2
    assert prepared["segments"][0]["text"] == (
        "First sentence. Second sentence in same cue."
    )
    assert prepared["speakerMarkerCount"] == 0


# ── 端到端：HTTP 导入后落库与精听页口径一致 ─────────────────


def test_http_import_has_no_marker_prefixed_sentence(client, env):
    """验收 ①：重导后没有任何句子以 `/` 或 `W:` 开头。"""
    resp = _import_json(client, MARKER_JSON)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subtitle"]["sentenceCount"] == 3
    assert data["subtitle"]["speakerMarkerCount"] == 1

    lesson_id = data["material"]["lessonId"]
    segments = db.get_v2_subtitle_segments(lesson_id)
    assert [seg["text"] for seg in segments] == [
        "What do you think will happen?",
        "What?",
        "Normal sentence without marker.",
    ]
    for seg in segments:
        stripped = seg["text"].lstrip()
        assert not stripped.startswith("/"), seg
        assert stripped[:2] not in ("W:", "M:", "Q:"), seg


def test_markers_persisted_to_sidecar_not_lost(client, env):
    """标记保留在元数据里不丢：写 sidecar，且不改 schema。"""
    resp = _import_json(client, MARKER_JSON)
    lesson_id = resp.json()["material"]["lessonId"]

    markers = cet_import.read_speaker_markers(lesson_id)
    assert len(markers) == 1
    assert markers[0]["marker"] == "/ W:"
    assert markers[0]["speaker"] == "W"
    # 原始（未清洗）文本也在，便于追溯
    assert "/ W: What?" in markers[0]["text"]

    path = cet_import.speaker_markers_path(lesson_id)
    assert path is not None and path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lessonId"] == lesson_id
    assert payload["count"] == 1


def test_import_warns_about_cleaned_markers(client, env):
    resp = _import_json(client, MARKER_JSON)
    warnings = resp.json()["warnings"]
    assert any("说话人标记" in warning for warning in warnings)


def test_no_marker_import_writes_no_sidecar(client, env):
    """无标记时不产生 sidecar，也不额外加 warning（行为与现在一致）。"""
    payload = {"duration": 6.0, "sentences": [
        {"start": 0.0, "end": 3.0, "text": "First clean sentence."},
        {"start": 3.0, "end": 6.0, "text": "Second clean sentence."},
    ]}
    resp = _import_json(client, payload, title="无标记材料")
    assert resp.status_code == 200, resp.text
    lesson_id = resp.json()["material"]["lessonId"]
    assert cet_import.read_speaker_markers(lesson_id) == []
    assert not any("说话人标记" in w for w in resp.json()["warnings"])


def test_subtitles_endpoint_unit_count_matches_segment_count(client, env):
    """验收 ②：`/api/v2/lessons/{id}/subtitles` 的句数与落库条数一致。

    这是「文档 32 句 / 精听页 33 句」缺陷的正面回归：
    修复前 segments=32 而 sentence_units=33（多出 `/ W: What?`）。
    """
    resp = _import_json(client, MARKER_JSON)
    lesson_id = resp.json()["material"]["lessonId"]

    payload = client.get(f"/api/v2/lessons/{lesson_id}/subtitles").json()
    segments = db.get_v2_subtitle_segments(lesson_id)
    units = payload["sentence_units"]

    assert len(segments) == 3
    assert len(units) == len(segments), (
        f"精听页句数 {len(units)} 与落库条数 {len(segments)} 不一致"
    )
    for unit in units:
        stripped = unit["text"].lstrip()
        assert not stripped.startswith("/"), unit
        assert stripped[:2] not in ("W:", "M:", "Q:"), unit


# ── 真实六级真题（有素材时才跑，缺素材自动 skip）──────────────


@pytest.mark.skipif(not CET6_JSON.is_file(), reason="缺少 cet6-202512-conv2 句子时间轴")
def test_real_cet6_fixture_has_no_marker_sentence():
    """真实真题：清洗后 32 条、无标记句子、精听页 unit 数 = 落库条数。"""
    raw = CET6_JSON.read_text(encoding="utf-8")
    prepared = cet_import.prepare_subtitle(
        CET6_JSON.name, raw, audio_duration=105.49,
    )
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 32
    assert prepared["speakerMarkerCount"] == 1
    assert prepared["speakerMarkers"][0]["marker"] == "/ W:"

    segments = [
        {"index": i, "start": s["start"], "end": s["end"], "text": s["text"]}
        for i, s in enumerate(prepared["segments"])
    ]
    for seg in segments:
        stripped = seg["text"].lstrip()
        assert not stripped.startswith("/"), seg
        assert stripped[:2] not in ("W:", "M:", "Q:"), seg

    units = build_translation_units(segments)
    assert len(units) == len(segments) == 32, (
        f"精听页 {len(units)} 句 vs 落库 {len(segments)} 句"
    )


@pytest.mark.skipif(not CET6_JSON.is_file(), reason="缺少 cet6-202512-conv2 句子时间轴")
def test_real_cet6_echo_sentence_not_duplicated():
    """真实真题第 22/23 条：`What?` 只出现一次（回声被去掉，不是重复内容）。"""
    raw = CET6_JSON.read_text(encoding="utf-8")
    prepared = cet_import.prepare_subtitle(
        CET6_JSON.name, raw, audio_duration=105.49,
    )
    texts = [seg["text"] for seg in prepared["segments"]]
    # 第 22 条（1 基）是 "…will happen?"，第 23 条是 "What?"
    assert texts[21].endswith("what do you think will happen?"), texts[21]
    assert texts[22] == "What?", texts[22]
    # 整份里 "What?" 只出现一次
    assert texts.count("What?") == 1


@pytest.mark.skipif(not CET4_JSON.is_file(), reason="缺少 cet4 样例句子时间轴")
def test_real_cet4_sample_has_no_markers():
    """四级样例本身没有标记：清洗必须是零改动（行为不变）。"""
    raw = CET4_JSON.read_text(encoding="utf-8")
    prepared = cet_import.prepare_subtitle(
        CET4_JSON.name, raw, audio_duration=80.53,
    )
    assert prepared["errors"] == []
    assert prepared["sentenceCount"] == 32
    assert prepared["speakerMarkerCount"] == 0
