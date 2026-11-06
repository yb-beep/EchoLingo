"""返修2 · 包3 · P3-1：可选的本地模型自动切句（默认不勾、无 key）。

缺陷（任务书 P3-1）：真实真题最常见是「MP3 + 原文 TXT」，现在这种导入
`subtitle_status='unavailable'`，**不能逐句听**。原版会用本地 Whisper /
文本对齐自动切句。本机已装 `faster_whisper`。

设计约束（逐条都有测试）：
1. **默认不勾**：`auto_split=False` 时行为与返修前完全一致
   （现有 70 条 `test_cet_base_import` 守着，本文件补「不排队」的正面断言）；
2. **只有没有用户字幕时才允许**：有 SRT/VTT/句子 JSON 时前端 disabled，
   **后端也硬拒**（422 + `auto_split_conflicts_with_subtitle`）——
   直接调 API 不能绕过；
3. **有 TXT 时优先文本对齐**（句子文本来自用户原文，不被 ASR 改写）；
   没有原文才纯转写；
4. **进度复用现有状态枚举**：`pending`（=前端「正在准备原文」）→
   `ready` / `failed`（=「原文准备失败」），不新造状态；
5. **失败给明确原因**，不弹「字幕失败」诱导重试；
6. `faster_whisper` 不可用时能力探测报 `available=False`（界面据此灰掉）。

**性能纪律**：真实模型转写 105s 音频要 40s+，所以这里一律
monkeypatch `_transcribe_words_local` / `_transcribe_segments_local`，
只验调用路径、状态收敛与数据形状。真实模型端到端见
`docs/cet-base/返修2/包3-交付说明.md` 里贴的实测数字。

全部用 `tmp_path` 隔离；**不设 ELT_USER_DB**。
"""
from __future__ import annotations

import json
import sys
import threading
import time
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
"""

SENTENCE_JSON = {
    "duration": 8.25,
    "sentences": [
        {"start": 1.0, "end": 3.5, "text": "Hello and welcome to the listening practice."},
        {"start": 3.5, "end": 6.0, "text": "This is the second line of the transcript."},
    ],
}

PLAIN_TEXT = """Hello and welcome to the listening practice.
This is the second line of the transcript.
And here is a third sentence for alignment.
"""

# 桩：词级时间戳（对应 PLAIN_TEXT 的三句）
FAKE_WORDS = [
    {"label": "Hello", "start": 1.0, "end": 1.3},
    {"label": "and", "start": 1.3, "end": 1.4},
    {"label": "welcome", "start": 1.4, "end": 1.8},
    {"label": "to", "start": 1.8, "end": 1.9},
    {"label": "the", "start": 1.9, "end": 2.0},
    {"label": "listening", "start": 2.0, "end": 2.5},
    {"label": "practice.", "start": 2.5, "end": 3.0},
    {"label": "This", "start": 3.2, "end": 3.4},
    {"label": "is", "start": 3.4, "end": 3.5},
    {"label": "the", "start": 3.5, "end": 3.6},
    {"label": "second", "start": 3.6, "end": 4.0},
    {"label": "line", "start": 4.0, "end": 4.3},
    {"label": "of", "start": 4.3, "end": 4.4},
    {"label": "the", "start": 4.4, "end": 4.5},
    {"label": "transcript.", "start": 4.5, "end": 5.0},
    {"label": "And", "start": 5.2, "end": 5.4},
    {"label": "here", "start": 5.4, "end": 5.6},
    {"label": "is", "start": 5.6, "end": 5.7},
    {"label": "a", "start": 5.7, "end": 5.8},
    {"label": "third", "start": 5.8, "end": 6.1},
    {"label": "sentence", "start": 6.1, "end": 6.5},
    {"label": "for", "start": 6.5, "end": 6.6},
    {"label": "alignment.", "start": 6.6, "end": 7.2},
]

# 桩：纯转写分段（无原文时的路径 B）
FAKE_TRANSCRIPTION = [
    {"start": 0.5, "end": 3.0, "text": "Hello and welcome to the listening practice."},
    {"start": 3.2, "end": 5.0, "text": "This is the second line of the transcript."},
]


# ── 环境 ────────────────────────────────────────────────────


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DB / output / uploads，并把两条转写路径都换成确定性桩。

    同时把 `enqueue_local_import` 桩成抛错：**不勾选时绝不能排队任何转写**
    （这是返修前就有的硬约束，`test_cet_base_import.py` 的 fixture 也这么做）。
    """
    import webapp.services.v2_lessons as lesson_service
    from webapp.storage import user_assets

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    monkeypatch.setattr(user_assets, "GLOBAL_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(lesson_service, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        lesson_service, "_probe_uploaded_media", lambda path: (60.0, "local_audio"),
    )

    def _no_transcribe(*args, **kwargs):
        raise AssertionError("不得走 v2_lessons 的无条件转写（返修前的硬约束）")

    monkeypatch.setattr(lesson_service, "enqueue_local_import", _no_transcribe)

    # 自动切句的两条转写路径：默认换成桩，个别用例自己再覆盖
    monkeypatch.setattr(
        cet_import, "_transcribe_words_local",
        lambda audio_path, model_name: list(FAKE_WORDS),
    )
    monkeypatch.setattr(
        cet_import, "_transcribe_segments_local",
        lambda audio_path, model_name: list(FAKE_TRANSCRIPTION),
    )
    # 能力探测：假定可用（真实探测在别的用例里单独验）
    monkeypatch.setattr(cet_import, "auto_split_capability", lambda refresh=False: {
        "available": True, "installed": True, "ffmpeg": True,
        "model": "large-v3-turbo", "modelCached": True,
        "weightsBytes": 1024, "reason": "", "hint": "",
    })
    cet_import.reset_auto_split_capability_cache()
    cet_import._AUTO_SPLIT_TASKS.clear()

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


def _post_import(client, *, subtitle=None, auto_split=None, title="自动切句材料"):
    files = {"audio": ("listening.mp3", b"fake-mp3-bytes", "audio/mpeg")}
    data = {"title": title, "level": "cet6"}
    if subtitle is not None:
        files["subtitle"] = subtitle
    if auto_split is not None:
        data["auto_split"] = "true" if auto_split else "false"
    return client.post("/api/cet/base/import", files=files, data=data)


def _wait_auto_split(lesson_id: int, timeout: float = 5.0) -> dict:
    """等后台线程收敛（桩转写是瞬时的，几毫秒就完）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = cet_import.auto_split_state(lesson_id)
        if state.get("status") in ("ready", "failed"):
            return state
        time.sleep(0.01)
    return cet_import.auto_split_state(lesson_id)


# ── ① 默认不勾：行为与返修前完全一致 ────────────────────────


def test_default_is_off_and_queues_nothing(client, env):
    """不传 auto_split → 不排队、不写 autoSplit.queued、无相关 warning。"""
    resp = _post_import(client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["autoSplit"]["queued"] is False
    lesson_id = data["material"]["lessonId"]
    assert cet_import.auto_split_state(lesson_id) == {}
    assert not any("自动切句" in w for w in data["warnings"])
    # 纯文本仍然只作原文、不伪造时间轴（返修前的正确行为）
    assert data["subtitle"]["timed"] is False
    assert db.get_v2_subtitle_segments(lesson_id) == []
    assert db.get_v2_lesson(lesson_id)["subtitle_status"] == "unavailable"


def test_explicit_false_behaves_like_default(client, env):
    """显式传 auto_split=false 与不传完全一致。"""
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=False,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["autoSplit"]["queued"] is False


def test_audio_only_without_auto_split_stays_unavailable(client, env):
    """仅音频 + 不勾选 → 仍是 unavailable，且绝不排队转写。"""
    resp = _post_import(client)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subtitle"]["timed"] is False
    assert data["material"]["subtitleStatus"] == "unavailable"
    assert cet_import.auto_split_state(data["material"]["lessonId"]) == {}


# ── ② 有用户字幕 → 后端硬拒（不能只靠前端）─────────────────


def test_srt_plus_auto_split_is_rejected_by_backend(client, env):
    """直接调 API 带 SRT + auto_split=true → 422，且不建任何材料。"""
    resp = _post_import(
        client, subtitle=("listening.srt", VALID_SRT, "text/plain"), auto_split=True,
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "IMPORT_VALIDATION_FAILED"
    assert detail["errors"][0]["reason"] == "auto_split_conflicts_with_subtitle"
    assert "覆盖" in detail["errors"][0]["message"]

    # 关键：拒绝后不留任何材料、不留任何上传
    assert db.list_v2_lessons(include_archived=True) == []
    assert db.list_v2_media_uploads() == [] if hasattr(db, "list_v2_media_uploads") else True


def test_sentence_json_plus_auto_split_is_rejected(client, env):
    resp = _post_import(
        client,
        subtitle=("material.json", json.dumps(SENTENCE_JSON), "application/json"),
        auto_split=True,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["errors"][0]["reason"] == "auto_split_conflicts_with_subtitle"


def test_vtt_plus_auto_split_is_rejected(client, env):
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nHello there.\n"
    resp = _post_import(client, subtitle=("a.vtt", vtt, "text/plain"), auto_split=True)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["errors"][0]["reason"] == "auto_split_conflicts_with_subtitle"


def test_existing_subtitle_never_overwritten_by_auto_split(client, env):
    """竞态从根上避开：有字幕时根本不允许排队，字幕逐段与上传一致。"""
    resp = _post_import(client, subtitle=("listening.srt", VALID_SRT, "text/plain"))
    assert resp.status_code == 200, resp.text
    lesson_id = resp.json()["material"]["lessonId"]
    before = db.get_v2_subtitle_segments(lesson_id)
    assert [seg["text"] for seg in before] == [
        "Hello and welcome to the listening practice.",
        "This is the second line of the transcript.",
    ]
    # 再试一次带 auto_split 的同音频（不同标题避开去重）→ 被拒，原字幕不变
    resp2 = _post_import(
        client, subtitle=("listening.srt", VALID_SRT, "text/plain"),
        auto_split=True, title="另一份",
    )
    assert resp2.status_code == 422
    after = db.get_v2_subtitle_segments(lesson_id)
    assert after == before


# ── ③ 有 TXT → 优先文本对齐 ─────────────────────────────────


def test_txt_plus_auto_split_uses_text_alignment(client, env):
    """MP3 + TXT + 勾选 → 走文本对齐，句子文本来自**用户原文**。"""
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["autoSplit"]["queued"] is True
    assert data["autoSplit"]["path"] == "text_alignment"

    lesson_id = data["material"]["lessonId"]
    state = _wait_auto_split(lesson_id)
    assert state["status"] == "ready", state

    segments = db.get_v2_subtitle_segments(lesson_id)
    assert len(segments) == 3
    # 文本逐字来自原文（不是 ASR 改写）
    assert [seg["text"] for seg in segments] == [
        "Hello and welcome to the listening practice.",
        "This is the second line of the transcript.",
        "And here is a third sentence for alignment.",
    ]
    # 时间轴来自词级对齐（首句从 Hello 的 1.0 开始）
    assert segments[0]["start"] == pytest.approx(1.0, abs=0.01)
    assert segments[-1]["end"] <= 7.2 + 0.01


def test_text_alignment_marks_status_ready_and_sets_timeline(client, env):
    """验收 ①：状态变 ready、句子数 > 0、工作区可逐句。"""
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    lesson_id = resp.json()["material"]["lessonId"]
    _wait_auto_split(lesson_id)

    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["subtitle_status"] == "ready"
    assert lesson["subtitle_error"] == ""

    material = cet_materials.material_item(lesson)
    assert material["hasTimeline"] is True
    assert material["sentenceCount"] == 3
    assert material["availability"]["isGenerating"] is False

    # 工作区接口：subtitles 有内容、reading 不再 409
    subtitles = client.get(f"/api/v2/lessons/{lesson_id}/subtitles")
    assert subtitles.status_code == 200
    assert len(subtitles.json()["segments"]) == 3
    reading = client.get(f"/api/v2/lessons/{lesson_id}/reading")
    assert reading.status_code == 200


def test_auto_split_meta_records_path_and_model(client, env):
    """「怎么来的」可追溯：sidecar 记录路径/模型/句数。"""
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    lesson_id = resp.json()["material"]["lessonId"]
    _wait_auto_split(lesson_id)

    meta = cet_import.read_auto_split_meta(lesson_id)
    assert meta["mode"] == "auto_split"
    assert meta["path"] == "text_alignment"
    assert meta["textSource"] == "user_transcript"
    assert meta["sentenceCount"] == 3
    assert meta["model"] == "large-v3-turbo"


# ── ④ 没有原文 → 纯转写 ─────────────────────────────────────


def test_audio_only_with_auto_split_uses_transcription(client, env):
    """仅音频 + 勾选 → 纯转写（句子文本来自 ASR）。"""
    resp = _post_import(client, auto_split=True)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["autoSplit"]["queued"] is True
    assert data["autoSplit"]["path"] == "transcribe"

    lesson_id = data["material"]["lessonId"]
    state = _wait_auto_split(lesson_id)
    assert state["status"] == "ready", state

    segments = db.get_v2_subtitle_segments(lesson_id)
    assert [seg["text"] for seg in segments] == [
        "Hello and welcome to the listening practice.",
        "This is the second line of the transcript.",
    ]
    assert db.get_v2_lesson(lesson_id)["subtitle_status"] == "ready"
    meta = cet_import.read_auto_split_meta(lesson_id)
    assert meta["path"] == "transcribe"
    assert meta["textSource"] == "local_asr"


# ── ⑤ 进度复用现有状态枚举 + 失败给明确原因 ─────────────────


def test_pending_status_is_set_before_worker_runs(client, env, monkeypatch):
    """排队瞬间状态是 pending（= 前端「正在准备原文」），不新造状态。"""
    observed: dict[str, str] = {}

    def _slow_words(audio_path, model_name):
        observed["during"] = db.get_v2_lesson(observed["lesson_id"])["subtitle_status"]
        return list(FAKE_WORDS)

    monkeypatch.setattr(cet_import, "_transcribe_words_local", _slow_words)

    original_enqueue = cet_import.enqueue_auto_split

    def _capture(lesson_id, **kwargs):
        observed["lesson_id"] = lesson_id
        return original_enqueue(lesson_id, **kwargs)

    monkeypatch.setattr(cet_import, "enqueue_auto_split", _capture)

    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    assert resp.status_code == 200, resp.text
    lesson_id = resp.json()["material"]["lessonId"]
    _wait_auto_split(lesson_id)
    # 后台线程运行时读到的状态必须是 pending（不是 ready，也不是新枚举）
    assert observed.get("during") == "pending"


def test_transcription_failure_reports_explicit_reason(client, env, monkeypatch):
    """转写失败 → subtitle_status='failed' + 明确原因，不留 pending。"""

    def _boom(audio_path, model_name):
        raise RuntimeError("模型加载失败：磁盘空间不足")

    monkeypatch.setattr(cet_import, "_transcribe_words_local", _boom)

    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    assert resp.status_code == 200, resp.text
    lesson_id = resp.json()["material"]["lessonId"]
    state = _wait_auto_split(lesson_id)
    assert state["status"] == "failed"
    assert "磁盘空间不足" in state["error"]

    lesson = db.get_v2_lesson(lesson_id)
    assert lesson["subtitle_status"] == "failed"
    assert lesson["subtitle_status"] != "pending"
    assert "自动切句失败" in lesson["subtitle_error"]
    assert "磁盘空间不足" in lesson["subtitle_error"]


def test_failed_material_does_not_claim_subtitle_failure_popup(client, env, monkeypatch):
    """失败文案必须是「原文准备失败」口径，不能是诱导重试的「字幕失败」。"""
    monkeypatch.setattr(
        cet_import, "_transcribe_words_local",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模型不可用")),
    )
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    lesson_id = resp.json()["material"]["lessonId"]
    _wait_auto_split(lesson_id)

    # 材料视图的 subtitleStatus 是 failed（前端 cetAvailability 映射成「原文准备失败」）
    material = cet_materials.material_item(db.get_v2_lesson(lesson_id))
    assert material["subtitleStatus"] == "failed"
    # 索引页模板里 failed → 「原文准备失败」，而不是「字幕失败」
    index_html = (ROOT / "frontend" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "原文准备失败" in index_html
    assert "if (item.subtitleStatus === 'failed') return {key: 'failed', label: '原文准备失败'}" in index_html


def test_empty_transcription_is_a_failure_not_a_fake_success(client, env, monkeypatch):
    """转写返回空 → 明确失败，不能写成「ready 但 0 句」。"""
    monkeypatch.setattr(
        cet_import, "_transcribe_segments_local", lambda audio_path, model_name: [],
    )
    resp = _post_import(client, auto_split=True)
    lesson_id = resp.json()["material"]["lessonId"]
    state = _wait_auto_split(lesson_id)
    assert state["status"] == "failed"
    assert "静音" in state["error"] or "没有识别" in state["error"]
    assert db.get_v2_lesson(lesson_id)["subtitle_status"] == "failed"
    assert db.get_v2_subtitle_segments(lesson_id) == []


def test_unreadable_audio_fails_with_reason(client, env, monkeypatch):
    """音频文件不可读 → 明确原因，不冒成 pending。"""
    monkeypatch.setattr(cet_import, "_resolve_lesson_media_path", lambda lesson: None)
    resp = _post_import(client, auto_split=True)
    lesson_id = resp.json()["material"]["lessonId"]
    state = _wait_auto_split(lesson_id)
    assert state["status"] == "failed"
    assert "不可读" in state["error"]


# ── ⑥ faster_whisper 不可用 → 能力探测报不可用 + 后端拒绝 ────


def test_capability_reports_unavailable_when_not_installed(monkeypatch):
    """`faster_whisper` 未装 → available=False + 人话原因 + 安装提示。"""
    cet_import.reset_auto_split_capability_cache()
    monkeypatch.setattr(cet_import, "faster_whisper_installed", lambda: False)
    capability = cet_import.auto_split_capability(refresh=True)
    assert capability["available"] is False
    assert capability["installed"] is False
    assert "未安装" in capability["reason"]
    assert "pip install faster-whisper" in capability["hint"]


def test_capability_reports_unavailable_without_ffmpeg(monkeypatch):
    cet_import.reset_auto_split_capability_cache()
    monkeypatch.setattr(cet_import, "faster_whisper_installed", lambda: True)
    monkeypatch.setattr(cet_import, "ffmpeg_available", lambda: False)
    capability = cet_import.auto_split_capability(refresh=True)
    assert capability["available"] is False
    assert "ffmpeg" in capability["reason"]


def test_capability_warns_when_weights_not_cached(monkeypatch):
    """权重不在本地缓存时仍然可用（会自动下载），但要如实提示。"""
    cet_import.reset_auto_split_capability_cache()
    monkeypatch.setattr(cet_import, "faster_whisper_installed", lambda: True)
    monkeypatch.setattr(cet_import, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        cet_import, "_model_weights_present",
        lambda name: {"present": False, "cacheDir": "", "snapshotDir": "", "weightsBytes": 0},
    )
    capability = cet_import.auto_split_capability(refresh=True)
    assert capability["available"] is True
    assert capability["modelCached"] is False
    assert "下载" in capability["hint"]


def test_capability_detects_real_cached_weights(monkeypatch, tmp_path):
    """真实探测口径：有 `model.bin` 才算缓存可用（只有 config 不算）。

    这里把缓存根**完全隔离**到 tmp_path —— 否则会回退到本机真实的
    `~/.cache/huggingface/hub`（那里确实有 large-v3 权重），断言就没意义了。
    """
    cet_import.reset_auto_split_capability_cache()
    hub = tmp_path / "hub"
    cache_dir = hub / "models--Systran--faster-whisper-large-v3" / "snapshots" / "abc123"
    cache_dir.mkdir(parents=True)
    (cache_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cet_import, "_hf_cache_roots", lambda: [hub])

    # 只有 config.json，没有 model.bin → 不算可用
    assert cet_import._model_weights_present("large-v3")["present"] is False

    (cache_dir / "model.bin").write_bytes(b"x" * 128)
    info = cet_import._model_weights_present("large-v3")
    assert info["present"] is True
    assert info["weightsBytes"] == 128


def test_missing_weights_alone_does_not_grey_out_option(monkeypatch, tmp_path):
    """只有 config 没有 model.bin 的模型**不能**被当成可用缓存。

    这是本轮实测踩到的坑：`models--Systran--faster-whisper-small` 缺
    `model.bin`，若只看目录存在就会误报可用，然后转写卡在联网下载。
    """
    cet_import.reset_auto_split_capability_cache()
    hub = tmp_path / "hub"
    small = hub / "models--Systran--faster-whisper-small" / "snapshots" / "s1"
    small.mkdir(parents=True)
    (small / "config.json").write_text("{}", encoding="utf-8")
    (small / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cet_import, "_hf_cache_roots", lambda: [hub])
    monkeypatch.setattr(cet_import, "faster_whisper_installed", lambda: True)
    monkeypatch.setattr(cet_import, "ffmpeg_available", lambda: True)
    monkeypatch.setenv("ELT_CET_AUTO_SPLIT_MODEL", "small")

    capability = cet_import.auto_split_capability(refresh=True)
    # 仍然可用（会自动下载），但必须如实报「未缓存」而不是假装缓存好了
    assert capability["available"] is True
    assert capability["modelCached"] is False
    assert "下载" in capability["hint"]


def test_auto_split_rejected_when_capability_unavailable(client, env, monkeypatch):
    """能力不可用时直接调 API → 422 明确原因（前端灰掉之外的第二道）。"""
    monkeypatch.setattr(cet_import, "auto_split_capability", lambda refresh=False: {
        "available": False, "installed": False, "ffmpeg": True,
        "model": "", "modelCached": False, "weightsBytes": 0,
        "reason": "本机未安装 faster-whisper，无法用本地模型自动切句。",
        "hint": "pip install faster-whisper",
    })
    resp = _post_import(client, auto_split=True)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["errors"][0]["reason"] == "auto_split_unavailable"
    assert "faster-whisper" in detail["errors"][0]["message"]
    assert db.list_v2_lessons(include_archived=True) == []


def test_formats_endpoint_exposes_auto_split_capability(client, env):
    """界面靠这个端点决定是否灰掉，所以必须下发 autoSplit。"""
    resp = client.get("/api/cet/base/import/formats")
    assert resp.status_code == 200
    data = resp.json()
    assert "autoSplit" in data
    assert set(data["autoSplit"]) >= {
        "available", "installed", "ffmpeg", "model", "modelCached", "reason", "hint",
    }


# ── ⑦ 纯函数：对齐与组装 ────────────────────────────────────


def test_align_text_to_words_maps_sentences_to_word_timestamps():
    aligned = cet_import.align_text_to_words(
        ["Hello and welcome to the listening practice.",
         "This is the second line of the transcript."],
        FAKE_WORDS,
    )
    assert len(aligned) == 2
    assert aligned[0]["start"] == pytest.approx(1.0, abs=0.01)
    assert aligned[0]["end"] == pytest.approx(3.0, abs=0.01)
    assert aligned[1]["start"] == pytest.approx(3.2, abs=0.01)
    assert aligned[1]["end"] == pytest.approx(5.0, abs=0.01)
    assert all(item["coverage"] == 1.0 for item in aligned)


def test_align_text_to_words_is_monotonic_and_non_overlapping():
    aligned = cet_import.align_text_to_words(
        [sentence for sentence in
         "Hello and welcome to the listening practice. "
         "This is the second line of the transcript. "
         "And here is a third sentence for alignment.".split(".") if sentence.strip()],
        FAKE_WORDS,
    )
    for previous, current in zip(aligned, aligned[1:]):
        assert current["start"] >= previous["end"] - 1e-9
        assert current["end"] > current["start"]


def test_align_text_to_words_without_words_returns_empty():
    """无词数据 → 空列表（调用方据此判失败，绝不伪造时间轴）。"""
    assert cet_import.align_text_to_words(["A sentence."], []) == []
    assert cet_import.align_text_to_words([], FAKE_WORDS) == []


def test_align_text_to_words_reports_low_coverage():
    """原文与录音完全对不上 → coverage=0，但仍给出不越界的时间轴。"""
    aligned = cet_import.align_text_to_words(
        ["Completely unrelated text about quantum physics."], FAKE_WORDS,
    )
    assert len(aligned) == 1
    assert aligned[0]["coverage"] == 0.0
    assert aligned[0]["end"] <= FAKE_WORDS[-1]["end"] + 1e-6


def test_text_to_sentences_splits_and_strips_markers():
    sentences = cet_import._text_to_sentences(
        "W: Hello there. How are you?\n\nM: Fine, thanks!\n"
    )
    assert sentences == ["Hello there.", "How are you?", "Fine, thanks!"]


def test_build_segments_from_transcription_dedupes_and_cleans():
    segments = cet_import.build_segments_from_transcription([
        {"start": 0.0, "end": 1.0, "text": "Hi there."},
        {"start": 1.0, "end": 2.0, "text": "Hi there."},          # 重复段
        {"start": 2.2, "end": 3.0, "text": "W: Bye now."},        # 带标记
        {"start": 3.0, "end": 3.0, "text": "Zero length."},       # end == start
    ])
    assert [seg["text"] for seg in segments] == ["Hi there.", "Bye now.", "Zero length."]
    assert segments[2]["end"] > segments[2]["start"]
    assert [seg["index"] for seg in segments] == [0, 1, 2]


# ── ⑧ 通用模式（ELT_CET_MODE=0）不回归 ──────────────────────


def test_auto_split_off_is_mode_independent(client, env, monkeypatch):
    """通用模式（ELT_CET_MODE=0）下不勾选时行为不变：纯文本仍只作原文。"""
    monkeypatch.setenv("ELT_CET_MODE", "0")
    resp = _post_import(client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["autoSplit"]["queued"] is False
    assert data["subtitle"]["timed"] is False
    assert db.get_v2_subtitle_segments(data["material"]["lessonId"]) == []


def test_auto_split_works_in_generic_mode_when_explicitly_enabled(client, env, monkeypatch):
    """通用模式显式勾选也能用（不是 CET 专属能力，只是 CET 界面才暴露入口）。"""
    monkeypatch.setenv("ELT_CET_MODE", "0")
    resp = _post_import(
        client, subtitle=("transcript.txt", PLAIN_TEXT, "text/plain"), auto_split=True,
    )
    assert resp.status_code == 200, resp.text
    lesson_id = resp.json()["material"]["lessonId"]
    assert _wait_auto_split(lesson_id)["status"] == "ready"
    assert len(db.get_v2_subtitle_segments(lesson_id)) == 3
